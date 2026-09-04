'use client';
import { useEffect, useRef, useState, useCallback } from 'react';
import { motion } from 'framer-motion';
import { BibleVersionSelectors } from './BibleVersionSelectors';
import { ScripturePopover } from './ScripturePopover';
import { useBibleVersions } from '@/lib/useBibleVersions';
import { getWebSocketBaseUrl } from '@/lib/wsBaseUrl';
import { float32ChunksToBase64Payloads } from '@/lib/audioUtils';
import { useTranslationFeed, type VerseDetection, type VerseSuggestion } from '@/lib/useTranslationFeed';
import {
  createInitialStreamDebugState,
  useBrowserDiagnosticsTelemetry,
  type BrowserAudioDiagnostics as AudioDiagnostics,
  type BrowserStreamDebugState,
} from '@/lib/browserDiagnostics';

type Status = 'idle' | 'connecting' | 'active' | 'reconnecting' | 'error';
type AudioPreset = 'quiet-room' | 'noisy-church' | 'near-speaker' | 'manual';

interface MobileTestProps {
  churchId: string;
}

interface AudioCaptureOptions {
  noiseSuppression: boolean;
  echoCancellation: boolean;
  autoGainControl: boolean;
}

const BATCH_INTERVAL_MS = 100;
const MAX_RETRY_DELAY_MS = 30_000;
const TARGET_SAMPLE_RATE = 16000;
const AUDIO_PRESETS: Record<Exclude<AudioPreset, 'manual'>, AudioCaptureOptions> = {
  'quiet-room': { noiseSuppression: true, echoCancellation: false, autoGainControl: true },
  'noisy-church': { noiseSuppression: true, echoCancellation: true, autoGainControl: true },
  'near-speaker': { noiseSuppression: true, echoCancellation: true, autoGainControl: false },
};

function getEffectiveOptions(preset: AudioPreset, manualOptions: AudioCaptureOptions): AudioCaptureOptions {
  return preset === 'manual' ? manualOptions : AUDIO_PRESETS[preset];
}

function computeSignalMetrics(samples: Float32Array): { rms: number; peak: number } {
  let sumSquares = 0;
  let peak = 0;
  for (const sample of samples) {
    const abs = Math.abs(sample);
    if (abs > peak) peak = abs;
    sumSquares += sample * sample;
  }
  return {
    rms: Math.sqrt(sumSquares / Math.max(samples.length, 1)),
    peak,
  };
}

function formatAge(now: number, ts: number | null): string {
  if (!ts) return 'No signal yet';
  const seconds = Math.max(0, Math.round((now - ts) / 1000));
  return seconds < 2 ? 'Just now' : `${seconds}s ago`;
}

function formatPercent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

// ── Audio capture + streaming ─────────────────────────────────────────────────

function useAudioStream(
  churchId: string,
  sourceScriptureVersion: string,
  displayScriptureVersion: string,
  audioOptions: AudioCaptureOptions,
) {
  const [status, setStatus] = useState<Status>('idle');
  const [errorMsg, setErrorMsg] = useState('');
  const [diagnostics, setDiagnostics] = useState<AudioDiagnostics>({
    inputLevel: 0,
    noiseFloor: 0,
    clipping: false,
    speechDetected: false,
    batchesSent: 0,
    lastBatchAt: null,
    audioContextSampleRate: null,
    requestedSampleRate: TARGET_SAMPLE_RATE,
    inputDeviceLabel: '',
    lastSpeechAt: null,
    appliedOptions: null,
  });
  const [streamDebug, setStreamDebug] = useState<BrowserStreamDebugState>(createInitialStreamDebugState);

  const wsRef = useRef<WebSocket | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const workletRef = useRef<AudioWorkletNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const sendBufferRef = useRef<Float32Array[]>([]);
  const sendBufferSampleCountRef = useRef(0);
  const sendTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const retryDelayRef = useRef(1000);
  const shouldReconnectRef = useRef(false);
  const sampleRateRef = useRef(TARGET_SAMPLE_RATE);
  const connectRef = useRef<() => void>(() => {});
  const noiseFloorRef = useRef(0);
  const speechHoldTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flushBuffer = useCallback(() => {
    if (!sendBufferRef.current.length || wsRef.current?.readyState !== WebSocket.OPEN) return;
    const chunks = sendBufferRef.current;
    const bufferedChunkCount = chunks.length;
    const bufferedSampleCount = sendBufferSampleCountRef.current;
    sendBufferRef.current = [];
    sendBufferSampleCountRef.current = 0;
    const payloads = float32ChunksToBase64Payloads(chunks);
    for (const payload of payloads) {
      wsRef.current.send(JSON.stringify({ type: 'audio', audio: payload }));
    }
    const now = Date.now();
    setDiagnostics(prev => ({
      ...prev,
      batchesSent: prev.batchesSent + payloads.length,
      lastBatchAt: now,
    }));
    setStreamDebug(prev => ({
      ...prev,
      payloadsSent: prev.payloadsSent + payloads.length,
      bufferedChunkCount: Math.max(0, prev.bufferedChunkCount - bufferedChunkCount),
      bufferedSampleCount: Math.max(0, prev.bufferedSampleCount - bufferedSampleCount),
    }));
  }, []);

  const connect = useCallback(() => {
    const ws = new WebSocket(`${getWebSocketBaseUrl()}/api/stream/v1?church_id=${encodeURIComponent(churchId)}`);
    wsRef.current = ws;

    ws.onopen = () => {
      setStatus('active');
      setErrorMsg('');
      retryDelayRef.current = 1000;
      setStreamDebug(prev => ({
        ...prev,
        socketOpenCount: prev.socketOpenCount + 1,
        lastSocketOpenAt: Date.now(),
      }));
      ws.send(JSON.stringify({
        type: 'session.start',
        sampleRate: sampleRateRef.current,
        topic: '',
        sourceScriptureVersion,
        displayScriptureVersion,
      }));
    };

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === 'error') {
        setErrorMsg(msg.message);
        setStatus('error');
        setStreamDebug(prev => ({
          ...prev,
          lastErrorMessage: String(msg.message ?? ''),
          lastSocketErrorAt: Date.now(),
        }));
      }
    };

    ws.onerror = () => {
      setStreamDebug(prev => ({
        ...prev,
        socketErrorCount: prev.socketErrorCount + 1,
        lastSocketErrorAt: Date.now(),
      }));
    };

    ws.onclose = (event) => {
      setStreamDebug(prev => ({
        ...prev,
        socketCloseCount: prev.socketCloseCount + 1,
        reconnectCount: shouldReconnectRef.current ? prev.reconnectCount + 1 : prev.reconnectCount,
        lastSocketCloseAt: Date.now(),
        lastSocketCloseCode: event.code,
        lastSocketCloseReason: event.reason,
      }));
      if (!shouldReconnectRef.current) return;
      setStatus('reconnecting');
      setTimeout(() => { if (shouldReconnectRef.current) connectRef.current(); }, retryDelayRef.current);
      retryDelayRef.current = Math.min(retryDelayRef.current * 2, MAX_RETRY_DELAY_MS);
    };
  }, [churchId, displayScriptureVersion, sourceScriptureVersion]);

  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  const start = useCallback(async () => {
    setStatus('connecting');
    setErrorMsg('');
    setStreamDebug(createInitialStreamDebugState());
    sendBufferRef.current = [];
    sendBufferSampleCountRef.current = 0;
    try {
      const mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: audioOptions.echoCancellation,
          noiseSuppression: audioOptions.noiseSuppression,
          autoGainControl: audioOptions.autoGainControl,
        },
        video: false,
      });
      streamRef.current = mediaStream;
      const ctx = new AudioContext();
      ctxRef.current = ctx;
      // Worklet downsamples to 16 kHz before posting chunks; advertise that rate
      // so the server's resample_float32_to_pcm16 hits the fast no-op path.
      sampleRateRef.current = TARGET_SAMPLE_RATE;
      if (ctx.state === 'suspended') await ctx.resume();
      await ctx.audioWorklet.addModule('/worklets/recorder-worklet.js');
      const source = ctx.createMediaStreamSource(mediaStream);
      const worklet = new AudioWorkletNode(ctx, 'recorder-processor');
      workletRef.current = worklet;
      noiseFloorRef.current = 0;
      const track = mediaStream.getAudioTracks()[0];
      setDiagnostics(prev => ({
        ...prev,
        inputLevel: 0,
        noiseFloor: 0,
        clipping: false,
        speechDetected: false,
        batchesSent: 0,
        lastBatchAt: null,
        lastSpeechAt: null,
        audioContextSampleRate: ctx.sampleRate,
        requestedSampleRate: TARGET_SAMPLE_RATE,
        inputDeviceLabel: track?.label ?? '',
        appliedOptions: { ...audioOptions },
      }));
      worklet.port.onmessage = (e) => {
        if (e.data.type !== 'chunk') return;
        const samples = e.data.samples as Float32Array;
        sendBufferRef.current.push(samples);
        sendBufferSampleCountRef.current += samples.length;
        setStreamDebug(prev => ({
          ...prev,
          bufferedChunkCount: sendBufferRef.current.length,
          bufferedSampleCount: sendBufferSampleCountRef.current,
        }));
        const { rms, peak } = computeSignalMetrics(samples);
        const nextNoiseFloor = noiseFloorRef.current === 0
          ? rms
          : rms < noiseFloorRef.current
            ? (noiseFloorRef.current * 0.8) + (rms * 0.2)
            : (noiseFloorRef.current * 0.98) + (rms * 0.02);
        noiseFloorRef.current = nextNoiseFloor;
        const speechThreshold = Math.max(nextNoiseFloor * 2.5, 0.02);
        const speechDetected = rms >= speechThreshold;
        const now = Date.now();

        if (speechDetected) {
          if (speechHoldTimerRef.current) {
            clearTimeout(speechHoldTimerRef.current);
            speechHoldTimerRef.current = null;
          }
          setDiagnostics(prev => ({
            ...prev,
            inputLevel: rms,
            noiseFloor: nextNoiseFloor,
            clipping: peak >= 0.98,
            speechDetected: true,
            lastSpeechAt: now,
          }));
          return;
        }

        setDiagnostics(prev => ({
          ...prev,
          inputLevel: rms,
          noiseFloor: nextNoiseFloor,
          clipping: peak >= 0.98,
        }));
        if (!speechHoldTimerRef.current) {
          speechHoldTimerRef.current = setTimeout(() => {
            setDiagnostics(prev => ({ ...prev, speechDetected: false }));
            speechHoldTimerRef.current = null;
          }, 1200);
        }
      };
      source.connect(worklet);
      worklet.connect(ctx.destination);
      worklet.port.postMessage('start');
      sendTimerRef.current = setInterval(flushBuffer, BATCH_INTERVAL_MS);
      shouldReconnectRef.current = true;
      connect();
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : 'Failed to access microphone');
      setStatus('error');
    }
  }, [audioOptions, connect, flushBuffer]);

  const stop = useCallback(() => {
    shouldReconnectRef.current = false;
    if (sendTimerRef.current) { clearInterval(sendTimerRef.current); sendTimerRef.current = null; }
    if (speechHoldTimerRef.current) { clearTimeout(speechHoldTimerRef.current); speechHoldTimerRef.current = null; }
    workletRef.current?.port.postMessage('stop');
    workletRef.current = null;
    if (wsRef.current) {
      if (wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: 'session.stop' }));
        wsRef.current.close();
      }
      wsRef.current = null;
    }
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    ctxRef.current?.close();
    ctxRef.current = null;
    sendBufferRef.current = [];
    sendBufferSampleCountRef.current = 0;
    noiseFloorRef.current = 0;
    setDiagnostics(prev => ({ ...prev, speechDetected: false, inputLevel: 0, clipping: false }));
    setStreamDebug(prev => ({
      ...prev,
      bufferedChunkCount: 0,
      bufferedSampleCount: 0,
    }));
    setStatus('idle');
  }, []);

  useEffect(() => () => stop(), [stop]);

  return { status, errorMsg, diagnostics, streamDebug, start, stop };
}

function DiagnosticsBar({
  label,
  value,
  color,
  detail,
}: {
  label: string;
  value: number;
  color: string;
  detail: string;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-xs">
        <span className="text-gray-300">{label}</span>
        <span className="text-gray-500">{detail}</span>
      </div>
      <div className="h-2 rounded-full bg-gray-800">
        <div className={`h-2 rounded-full ${color}`} style={{ width: `${Math.max(4, Math.round(value * 100))}%` }} />
      </div>
    </div>
  );
}

function ToggleRow({
  label,
  description,
  checked,
  disabled,
  onToggle,
}: {
  label: string;
  description: string;
  checked: boolean;
  disabled: boolean;
  onToggle: () => void;
}) {
  return (
    <label className={`flex items-center justify-between gap-4 rounded-2xl border px-4 py-3 ${disabled ? 'border-gray-800 bg-gray-900/60 opacity-60' : 'border-gray-800 bg-gray-900'}`}>
      <div className="space-y-1">
        <p className="text-sm font-medium text-white">{label}</p>
        <p className="text-xs text-gray-400">{description}</p>
      </div>
      <button
        type="button"
        disabled={disabled}
        onClick={onToggle}
        className={`relative h-7 w-12 rounded-full transition-colors ${checked ? 'bg-green-500' : 'bg-gray-700'} ${disabled ? 'cursor-not-allowed' : ''}`}
      >
        <span className={`absolute top-1 h-5 w-5 rounded-full bg-white transition-transform ${checked ? 'translate-x-6' : 'translate-x-1'}`} />
      </button>
    </label>
  );
}

function TranslationTestPanel({
  open,
  onClose,
  preset,
  onPresetChange,
  options,
  onOptionChange,
  diagnostics,
  status,
  displayConnected,
  recordingActive,
  now,
  lastInterimSpanish,
  lastFinalSpanish,
  lastCommittedEnglish,
  lastInterimAt,
  lastFinalAt,
  lastTranslationAt,
  remoteTelemetryEnabled,
  telemetrySessionKey,
  telemetrySending,
  telemetryLastPostedAt,
  telemetryLastPostStatus,
  telemetryLastPostError,
  telemetryWarningFlags,
  onToggleRemoteTelemetry,
  onSendTelemetrySnapshot,
}: {
  open: boolean;
  onClose: () => void;
  preset: AudioPreset;
  onPresetChange: (preset: AudioPreset) => void;
  options: AudioCaptureOptions;
  onOptionChange: (key: keyof AudioCaptureOptions, value: boolean) => void;
  diagnostics: AudioDiagnostics;
  status: Status;
  displayConnected: boolean;
  recordingActive: boolean;
  now: number;
  lastInterimSpanish: string;
  lastFinalSpanish: string;
  lastCommittedEnglish: string;
  lastInterimAt: number | null;
  lastFinalAt: number | null;
  lastTranslationAt: number | null;
  remoteTelemetryEnabled: boolean;
  telemetrySessionKey: string;
  telemetrySending: boolean;
  telemetryLastPostedAt: number | null;
  telemetryLastPostStatus: string;
  telemetryLastPostError: string;
  telemetryWarningFlags: string[];
  onToggleRemoteTelemetry: () => void;
  onSendTelemetrySnapshot: () => void;
}) {
  const sttHealthy = !!(lastFinalAt && now - lastFinalAt < 10_000);
  const translationHealthy = !!(lastTranslationAt && now - lastTranslationAt < 12_000);

  return (
    <div className={`fixed inset-0 z-40 transition ${open ? 'pointer-events-auto' : 'pointer-events-none'}`}>
      <div onClick={onClose} className={`absolute inset-0 bg-black/55 transition-opacity ${open ? 'opacity-100' : 'opacity-0'}`} />
      <div className={`absolute inset-x-0 bottom-0 mx-auto max-w-2xl rounded-t-3xl border border-gray-800 bg-gray-950/98 px-5 pb-6 pt-5 shadow-2xl backdrop-blur-md transition-transform duration-300 ${open ? 'translate-y-0' : 'translate-y-full'}`}>
        <div className="mx-auto mb-4 h-1.5 w-14 rounded-full bg-gray-700" />
        <div className="mb-4 flex items-start justify-between gap-3">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.24em] text-gray-500">Translation Test</p>
            <h2 className="text-xl font-semibold text-white">Settings & Diagnostics</h2>
            <p className="mt-1 text-sm text-gray-400">Tune phone capture for the room, then watch each pipeline stage.</p>
          </div>
          <button type="button" onClick={onClose} className="rounded-full border border-gray-700 px-3 py-1.5 text-xs font-medium text-gray-300 transition-colors hover:border-gray-500 hover:text-white">
            Close
          </button>
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <div className="rounded-2xl border border-gray-800 bg-gray-900 p-3">
            <p className="text-xs uppercase tracking-wide text-gray-500">Capture</p>
            <p className="mt-1 text-sm font-medium text-white">{status === 'active' ? 'Mic live' : status === 'reconnecting' ? 'Reconnecting' : status === 'error' ? 'Mic error' : 'Ready to start'}</p>
            <p className="mt-1 text-xs text-gray-400">{diagnostics.speechDetected ? 'Speech detected' : 'Listening for speech'}</p>
          </div>
          <div className="rounded-2xl border border-gray-800 bg-gray-900 p-3">
            <p className="text-xs uppercase tracking-wide text-gray-500">Speech to Text</p>
            <p className="mt-1 text-sm font-medium text-white">{sttHealthy ? 'Receiving finals' : 'Watching for finals'}</p>
            <p className="mt-1 text-xs text-gray-400">Last final: {formatAge(now, lastFinalAt)}</p>
          </div>
          <div className="rounded-2xl border border-gray-800 bg-gray-900 p-3">
            <p className="text-xs uppercase tracking-wide text-gray-500">Translation</p>
            <p className="mt-1 text-sm font-medium text-white">{translationHealthy ? 'English flowing' : 'Waiting on translation'}</p>
            <p className="mt-1 text-xs text-gray-400">Last English: {formatAge(now, lastTranslationAt)}</p>
          </div>
        </div>

        <div className="mt-5 space-y-4">
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-white">Capture Settings</h3>
              {recordingActive && <span className="text-[11px] text-amber-400">Changes apply on next start</span>}
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              {([
                ['quiet-room', 'Phone - Quiet Room'],
                ['noisy-church', 'Phone - Noisy Church'],
                ['near-speaker', 'Near Speaker / PA'],
                ['manual', 'Manual'],
              ] as const).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => onPresetChange(value)}
                  className={`rounded-2xl border px-4 py-3 text-left transition-colors ${preset === value ? 'border-green-500 bg-green-500/10 text-white' : 'border-gray-800 bg-gray-900 text-gray-300 hover:border-gray-600'}`}
                >
                  <p className="text-sm font-medium">{label}</p>
                </button>
              ))}
            </div>
            <div className="space-y-3">
              <ToggleRow label="Noise Suppression" description="Cuts steady room wash and ambient noise." checked={options.noiseSuppression} disabled={preset !== 'manual'} onToggle={() => onOptionChange('noiseSuppression', !options.noiseSuppression)} />
              <ToggleRow label="Echo Cancellation" description="Helps when house speakers bleed into the phone mic." checked={options.echoCancellation} disabled={preset !== 'manual'} onToggle={() => onOptionChange('echoCancellation', !options.echoCancellation)} />
              <ToggleRow label="Auto Gain Control" description="Boosts quiet speech, but can also raise room noise." checked={options.autoGainControl} disabled={preset !== 'manual'} onToggle={() => onOptionChange('autoGainControl', !options.autoGainControl)} />
            </div>
          </div>

          <div className="space-y-3">
            <h3 className="text-sm font-semibold text-white">Live Diagnostics</h3>
            <div className="rounded-2xl border border-gray-800 bg-gray-900 p-4 space-y-4">
              <DiagnosticsBar label="Input level" value={diagnostics.inputLevel} color={diagnostics.clipping ? 'bg-red-500' : diagnostics.speechDetected ? 'bg-green-500' : 'bg-sky-500'} detail={diagnostics.clipping ? 'Clipping' : formatPercent(diagnostics.inputLevel)} />
              <DiagnosticsBar label="Noise floor" value={diagnostics.noiseFloor} color="bg-amber-400" detail={formatPercent(diagnostics.noiseFloor)} />
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-2xl border border-gray-800 bg-gray-950/80 p-3">
                  <p className="text-xs uppercase tracking-wide text-gray-500">Capture path</p>
                  <p className="mt-1 text-sm text-white">{diagnostics.inputDeviceLabel || 'Phone microphone'}</p>
                  <p className="mt-1 text-xs text-gray-400">Browser DSP: {diagnostics.appliedOptions ? `${diagnostics.appliedOptions.noiseSuppression ? 'NS on' : 'NS off'} · ${diagnostics.appliedOptions.echoCancellation ? 'EC on' : 'EC off'} · ${diagnostics.appliedOptions.autoGainControl ? 'AGC on' : 'AGC off'}` : 'Will apply on start'}</p>
                </div>
                <div className="rounded-2xl border border-gray-800 bg-gray-950/80 p-3">
                  <p className="text-xs uppercase tracking-wide text-gray-500">Streaming</p>
                  <p className="mt-1 text-sm text-white">{displayConnected ? 'Display feed connected' : 'Display feed reconnecting'}</p>
                  <p className="mt-1 text-xs text-gray-400">Audio batches sent: {diagnostics.batchesSent}</p>
                  <p className="mt-1 text-xs text-gray-500">Last batch: {formatAge(now, diagnostics.lastBatchAt)}</p>
                </div>
              </div>
              <div className="grid gap-3 sm:grid-cols-3">
                <div className="rounded-2xl border border-gray-800 bg-gray-950/80 p-3">
                  <p className="text-xs uppercase tracking-wide text-gray-500">Speech</p>
                  <p className="mt-1 text-sm text-white">{diagnostics.speechDetected ? 'Detected' : 'Idle / room tone'}</p>
                  <p className="mt-1 text-xs text-gray-400">Last speech: {formatAge(now, diagnostics.lastSpeechAt)}</p>
                </div>
                <div className="rounded-2xl border border-gray-800 bg-gray-950/80 p-3">
                  <p className="text-xs uppercase tracking-wide text-gray-500">STT interim</p>
                  <p className="mt-1 text-sm text-white">{formatAge(now, lastInterimAt)}</p>
                  <p className="mt-1 text-xs text-gray-400 line-clamp-3">{lastInterimSpanish || 'Waiting for interim Spanish...'}</p>
                </div>
                <div className="rounded-2xl border border-gray-800 bg-gray-950/80 p-3">
                  <p className="text-xs uppercase tracking-wide text-gray-500">Translation</p>
                  <p className="mt-1 text-sm text-white">{formatAge(now, lastTranslationAt)}</p>
                  <p className="mt-1 text-xs text-gray-400 line-clamp-3">{lastCommittedEnglish || 'Waiting for committed English...'}</p>
                </div>
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-2xl border border-gray-800 bg-gray-950/80 p-3">
                  <p className="text-xs uppercase tracking-wide text-gray-500">Last final Spanish</p>
                  <p className="mt-2 text-sm text-gray-200">{lastFinalSpanish || 'No final phrase yet.'}</p>
                </div>
                <div className="rounded-2xl border border-gray-800 bg-gray-950/80 p-3">
                  <p className="text-xs uppercase tracking-wide text-gray-500">Sample rate</p>
                  <p className="mt-2 text-sm text-gray-200">Requested {diagnostics.requestedSampleRate} Hz{diagnostics.audioContextSampleRate ? ` · Context ${diagnostics.audioContextSampleRate} Hz` : ''}</p>
                </div>
              </div>
            </div>
          </div>

          <div className="space-y-3">
            <h3 className="text-sm font-semibold text-white">Remote Telemetry</h3>
            <div className="rounded-2xl border border-gray-800 bg-gray-900 p-4 space-y-4">
              <ToggleRow
                label="Browser Telemetry"
                description="Post live browser snapshots to the diagnostics dashboard during manual tests."
                checked={remoteTelemetryEnabled}
                disabled={false}
                onToggle={onToggleRemoteTelemetry}
              />
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-2xl border border-gray-800 bg-gray-950/80 p-3">
                  <p className="text-xs uppercase tracking-wide text-gray-500">Session key</p>
                  <p className="mt-2 break-all font-mono text-xs text-gray-300">{telemetrySessionKey}</p>
                  <p className="mt-2 text-xs text-gray-500">
                    Last post: {telemetryLastPostedAt ? formatAge(now, telemetryLastPostedAt) : 'Not sent yet'}
                  </p>
                </div>
                <div className="rounded-2xl border border-gray-800 bg-gray-950/80 p-3">
                  <p className="text-xs uppercase tracking-wide text-gray-500">Status</p>
                  <p className="mt-2 text-sm text-white">
                    {telemetrySending ? 'Posting snapshot...' : telemetryLastPostStatus || 'Idle'}
                  </p>
                  <p className="mt-2 text-xs text-red-300">
                    {telemetryLastPostError || (telemetryWarningFlags.length > 0 ? telemetryWarningFlags.join(', ') : 'No current warnings')}
                  </p>
                </div>
              </div>
              <button
                type="button"
                onClick={onSendTelemetrySnapshot}
                disabled={!remoteTelemetryEnabled || telemetrySending}
                className="w-full rounded-2xl border border-sky-700 bg-sky-900/40 px-4 py-3 text-sm font-medium text-sky-100 transition-colors hover:bg-sky-900/60 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {telemetrySending ? 'Posting...' : 'Post Snapshot Now'}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Component ─────────────────────────────────────────────────────────────────

export function MobileTest({ churchId }: MobileTestProps) {
  const [sourceScriptureVersion, setSourceScriptureVersion] = useState('rvr1960');
  const [displayScriptureVersion, setDisplayScriptureVersion] = useState('kjv');
  const [panelOpen, setPanelOpen] = useState(false);
  const [preset, setPreset] = useState<AudioPreset>('noisy-church');
  const [manualOptions, setManualOptions] = useState<AudioCaptureOptions>(AUDIO_PRESETS['noisy-church']);
  const [remoteTelemetryEnabled, setRemoteTelemetryEnabled] = useState(true);
  const [now, setNow] = useState(() => Date.now());
  const { versions, loading: versionsLoading, error: versionsError } = useBibleVersions(churchId);
  const effectiveOptions = getEffectiveOptions(preset, manualOptions);
  const { status, errorMsg, diagnostics, streamDebug, start, stop } = useAudioStream(
    churchId,
    sourceScriptureVersion,
    displayScriptureVersion,
    effectiveOptions,
  );
  const {
    segments,
    spanishLines,
    partialSpanish,
    liveEnglish,
    liveStableEnglish,
    liveDraftEnglish,
    connected: displayConnected,
    flashingId,
    lastInterimAt,
    lastFinalAt,
    lastTranslationAt,
    lastInterimSpanish,
    lastFinalSpanish,
    lastCommittedEnglish,
    debug: feedDebug,
  } = useTranslationFeed(churchId);
  const [popover, setPopover] = useState<{
    title: string;
    color: 'cited' | 'recommended';
    explanation?: string;
    sourcePassage?: VerseDetection['source_passage'] | VerseSuggestion['source_passage'];
    displayPassage?: VerseDetection['display_passage'] | VerseSuggestion['display_passage'];
  } | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const [scrolledUp, setScrolledUp] = useState(false);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  const telemetry = useBrowserDiagnosticsTelemetry({
    churchId,
    enabled: remoteTelemetryEnabled,
    streamStatus: status,
    streamError: errorMsg,
    displayConnected,
    audioDiagnostics: diagnostics,
    streamDebug,
    feedDebug,
    lastInterimAt,
    lastFinalAt,
    lastTranslationAt,
    lastInterimSpanish,
    lastFinalSpanish,
    lastCommittedEnglish,
  });

  const handlePresetChange = useCallback((nextPreset: AudioPreset) => {
    setPreset(nextPreset);
    if (nextPreset !== 'manual') {
      setManualOptions(AUDIO_PRESETS[nextPreset]);
    }
  }, []);

  const handleOptionChange = useCallback((key: keyof AudioCaptureOptions, value: boolean) => {
    setPreset('manual');
    setManualOptions(prev => ({ ...prev, [key]: value }));
  }, []);

  const handleSendTelemetrySnapshot = useCallback(() => {
    void telemetry.sendSnapshotNow();
  }, [telemetry]);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const isAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    atBottomRef.current = isAtBottom;
    setScrolledUp(!isAtBottom);
  }, []);

  useEffect(() => {
    if (!atBottomRef.current) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [segments, partialSpanish, spanishLines]);

  const scrollToLive = useCallback(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, []);

  const isLive = status === 'active' || status === 'reconnecting';
  const panelTone = status === 'error' || diagnostics.clipping
    ? 'red'
    : status === 'reconnecting' || (lastFinalAt !== null && now - lastFinalAt > 15_000)
      ? 'yellow'
      : 'green';
  const waitingHint = diagnostics.clipping
    ? 'Input is clipping. Move the phone farther away or lower the source level.'
    : diagnostics.inputLevel < 0.015
      ? 'Input is very low. Move the phone closer to the speaker or PA.'
      : lastInterimAt && (!lastFinalAt || lastFinalAt < lastInterimAt) && now - lastInterimAt > 6_000
        ? 'Interim speech is arriving, but STT is not settling on a final phrase yet.'
        : lastFinalAt && (!lastTranslationAt || lastTranslationAt < lastFinalAt) && now - lastFinalAt > 6_000
          ? 'Spanish finals are arriving, but the translation stage is lagging behind.'
          : diagnostics.speechDetected && !lastInterimAt
            ? 'Speech is reaching the mic, but STT has not responded yet.'
            : 'Waiting for speech...';
  const activeSpanish =
    spanishLines.join(' ') + (spanishLines.length > 0 && partialSpanish ? ' ' : '') + partialSpanish;
  const liveStableText = liveStableEnglish.trim();
  const liveDraftText = liveDraftEnglish.trim();
  const panelButton = (
    <button
      type="button"
      onClick={() => setPanelOpen(true)}
      className="inline-flex items-center gap-2 rounded-full border border-gray-700 bg-gray-900/80 px-3 py-2 text-xs font-medium text-gray-200 transition-colors hover:border-gray-500 hover:text-white"
    >
      <span className={`h-2 w-2 rounded-full ${panelTone === 'green' ? 'bg-green-400' : panelTone === 'yellow' ? 'bg-yellow-400' : 'bg-red-400'}`} />
      Settings & Diagnostics
    </button>
  );

  if (!isLive && status !== 'connecting') {
    return (
      <div className="min-h-screen bg-gray-950 text-white px-6 py-6">
        <div className="mx-auto flex min-h-[calc(100vh-3rem)] w-full max-w-xl flex-col justify-center gap-6">
          <div className="flex justify-end">{panelButton}</div>
          <div className="text-center space-y-2">
            <p className="text-gray-400 text-sm">Church: <span className="text-white">{churchId}</span></p>
            <h1 className="text-2xl font-semibold">Translation Test</h1>
            <p className="text-gray-500 text-sm">Speak in Spanish. Translation appears in real time.</p>
            <p className="text-xs text-gray-600">
              Preset: <span className="text-gray-300">{preset === 'quiet-room' ? 'Phone - Quiet Room' : preset === 'noisy-church' ? 'Phone - Noisy Church' : preset === 'near-speaker' ? 'Near Speaker / PA' : 'Manual'}</span>
            </p>
          </div>
        {versionsError ? (
          <p className="text-red-400 text-sm text-center">{versionsError}</p>
        ) : versions.length > 0 ? (
          <div className="w-full">
            <BibleVersionSelectors
              versions={versions}
              sourceVersion={sourceScriptureVersion}
              displayVersion={displayScriptureVersion}
              onSourceVersionChange={setSourceScriptureVersion}
              onDisplayVersionChange={setDisplayScriptureVersion}
              disabled={false}
            />
          {versionsLoading && <p className="mt-2 text-xs text-gray-500 text-center">Loading versions...</p>}
          </div>
        ) : null}
        <div className="rounded-2xl border border-gray-800 bg-gray-900 p-4">
          <p className="text-xs uppercase tracking-wide text-gray-500">Ready for mobile testing</p>
          <p className="mt-2 text-sm text-gray-300">
            Start with <span className="text-white">Phone - Noisy Church</span> unless the phone is very close to the speaker.
          </p>
        </div>
        {errorMsg && <p className="text-red-400 text-sm text-center">{errorMsg}</p>}
        <button
          onClick={start}
          className="w-full py-4 rounded-2xl bg-green-600 hover:bg-green-500 active:bg-green-700 text-white text-lg font-semibold transition-colors"
        >
          Start Recording
        </button>
        <p className="text-gray-600 text-xs text-center">This page uses your microphone and streams audio to the server.</p>
        </div>

        <TranslationTestPanel
          open={panelOpen}
          onClose={() => setPanelOpen(false)}
          preset={preset}
          onPresetChange={handlePresetChange}
          options={effectiveOptions}
          onOptionChange={handleOptionChange}
          diagnostics={diagnostics}
          status={status}
          displayConnected={displayConnected}
          recordingActive={false}
          now={now}
          lastInterimSpanish={lastInterimSpanish}
          lastFinalSpanish={lastFinalSpanish}
          lastCommittedEnglish={lastCommittedEnglish}
          lastInterimAt={lastInterimAt}
          lastFinalAt={lastFinalAt}
          lastTranslationAt={lastTranslationAt}
          remoteTelemetryEnabled={remoteTelemetryEnabled}
          telemetrySessionKey={telemetry.sessionKey}
          telemetrySending={telemetry.sending}
          telemetryLastPostedAt={telemetry.lastPostedAt}
          telemetryLastPostStatus={telemetry.lastPostStatus}
          telemetryLastPostError={telemetry.lastPostError}
          telemetryWarningFlags={telemetry.warningFlags}
          onToggleRemoteTelemetry={() => setRemoteTelemetryEnabled(prev => !prev)}
          onSendTelemetrySnapshot={handleSendTelemetrySnapshot}
        />
      </div>
    );
  }

  if (status === 'connecting') {
    return (
      <div className="h-screen bg-gray-950 text-white flex flex-col items-center justify-center gap-4">
        <div className="w-8 h-8 border-2 border-white/20 border-t-white rounded-full animate-spin" />
        <p className="text-gray-400 text-sm">Connecting...</p>
      </div>
    );
  }

  return (
    <div className="h-screen bg-gray-950 text-white flex flex-col overflow-hidden">
      <div className="flex-none flex items-center justify-between gap-3 px-4 py-3 bg-gray-900 border-b border-gray-800">
        <div className="flex min-w-0 items-center gap-2">
          <span className={`w-2 h-2 rounded-full ${
            status === 'active'       ? 'bg-green-400 animate-pulse' :
            status === 'reconnecting' ? 'bg-yellow-400 animate-pulse' :
                                        'bg-red-400'
          }`} />
          <span className="text-xs text-gray-400">
            {status === 'active' ? 'Live' : status === 'reconnecting' ? 'Reconnecting...' : 'Error'}
          </span>
          {!displayConnected && (
            <span className="text-xs text-yellow-500 ml-1">· reconnecting to server</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {panelButton}
          <button
            onClick={stop}
            className="bg-red-600/80 hover:bg-red-600 active:bg-red-700 text-white text-xs font-medium px-3 py-2 rounded-full transition-colors"
          >
            End
          </button>
        </div>
      </div>

      <div className="flex-none border-b border-gray-900 bg-gray-950/90 px-4 py-3">
        <div className="flex flex-wrap gap-2 text-[11px]">
          <span className={`rounded-full border px-2 py-1 ${diagnostics.speechDetected ? 'border-green-500/50 bg-green-500/10 text-green-200' : 'border-gray-800 bg-gray-900 text-gray-400'}`}>
            {diagnostics.speechDetected ? 'Speech detected' : 'Listening for speech'}
          </span>
          <span className={`rounded-full border px-2 py-1 ${lastFinalAt && now - lastFinalAt < 10_000 ? 'border-sky-500/50 bg-sky-500/10 text-sky-200' : 'border-gray-800 bg-gray-900 text-gray-400'}`}>
            STT final: {formatAge(now, lastFinalAt)}
          </span>
          <span className={`rounded-full border px-2 py-1 ${lastTranslationAt && now - lastTranslationAt < 12_000 ? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-200' : 'border-gray-800 bg-gray-900 text-gray-400'}`}>
            English: {formatAge(now, lastTranslationAt)}
          </span>
          {diagnostics.clipping && (
            <span className="rounded-full border border-red-500/50 bg-red-500/10 px-2 py-1 text-red-200">
              Clipping detected
            </span>
          )}
        </div>
      </div>

      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto relative"
      >
        <div className="min-h-full flex flex-col justify-end px-5 pt-12 pb-6 gap-4">
          {segments.length === 0 && !activeSpanish && !liveEnglish && (
            <p className="text-gray-500 text-sm text-center mb-4">{waitingHint}</p>
          )}
          {segments.map((s) => (
            <motion.div
              key={s.id}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1, transition: { duration: 0.4 } }}
              className="space-y-0.5"
            >
              <div className="flex flex-wrap items-center gap-2">
                <p
                  data-testid="committed-english"
                  className={`text-xl font-semibold leading-snug transition-colors duration-[600ms] ${
                  flashingId === s.id ? 'text-blue-200' : ''
                  }`}
                >
                  {s.english}
                </p>
                {s.verseDetected && (
                  <button
                    onClick={() => setPopover({
                      title: s.verseDetected!.reference,
                      color: 'cited',
                      explanation: s.verseDetected!.explanation,
                      sourcePassage: s.verseDetected!.source_passage,
                      displayPassage: s.verseDetected!.display_passage,
                    })}
                    className="rounded-full border border-amber-400/40 bg-amber-400/10 px-2 py-0.5 text-xs font-semibold text-amber-300"
                  >
                    {s.verseDetected.reference}
                  </button>
                )}
                {s.verseSuggestions?.map((suggestion) => (
                  <button
                    key={suggestion.reference}
                    onClick={() => setPopover({
                      title: suggestion.reference,
                      color: 'recommended',
                      explanation: suggestion.explanation ?? suggestion.relevance_note,
                      sourcePassage: suggestion.source_passage,
                      displayPassage: suggestion.display_passage,
                    })}
                    className="rounded-full border border-sky-400/40 bg-sky-400/10 px-2 py-0.5 text-xs font-semibold text-sky-300"
                  >
                    {suggestion.reference}
                  </button>
                ))}
              </div>
              <p data-testid="committed-spanish" className="text-sm text-gray-500 leading-snug">{s.spanish}</p>
            </motion.div>
          ))}
        </div>

        {scrolledUp && (
          <button
            onClick={scrollToLive}
            className="fixed bottom-6 right-4 z-20 bg-white/10 hover:bg-white/20 backdrop-blur-sm text-white text-xs px-3 py-2 rounded-full border border-white/20 transition-colors"
          >
            ↓ Live
          </button>
        )}
      </div>

      <div className="flex-none border-t border-gray-800 bg-gray-950/95 px-4 py-4 backdrop-blur">
        <p className="text-[11px] uppercase tracking-[0.24em] text-gray-500">Live Translation</p>
        {liveEnglish ? (
          <p data-testid="live-translation" className="mt-2 text-xl font-semibold leading-snug text-white">
            {liveDraftText ? (
              <>
                {liveStableText && <span className="text-white">{liveStableText} </span>}
                <span className="text-blue-100/85 italic">{liveDraftText}</span>
              </>
            ) : (
              liveEnglish
            )}<span className="animate-pulse text-blue-400 ml-1">▌</span>
          </p>
        ) : (
          <p data-testid="live-translation-placeholder" className="mt-2 text-sm text-gray-500">Waiting for live translation...</p>
        )}
      </div>

      {errorMsg && (
        <div className="flex-none px-4 py-2 bg-red-900/50 border-t border-red-800">
          <p className="text-red-300 text-xs">{errorMsg}</p>
        </div>
      )}
      <TranslationTestPanel
        open={panelOpen}
        onClose={() => setPanelOpen(false)}
        preset={preset}
        onPresetChange={handlePresetChange}
        options={effectiveOptions}
        onOptionChange={handleOptionChange}
        diagnostics={diagnostics}
        status={status}
        displayConnected={displayConnected}
        recordingActive
        now={now}
        lastInterimSpanish={lastInterimSpanish}
        lastFinalSpanish={lastFinalSpanish}
        lastCommittedEnglish={lastCommittedEnglish}
        lastInterimAt={lastInterimAt}
        lastFinalAt={lastFinalAt}
        lastTranslationAt={lastTranslationAt}
        remoteTelemetryEnabled={remoteTelemetryEnabled}
        telemetrySessionKey={telemetry.sessionKey}
        telemetrySending={telemetry.sending}
        telemetryLastPostedAt={telemetry.lastPostedAt}
        telemetryLastPostStatus={telemetry.lastPostStatus}
        telemetryLastPostError={telemetry.lastPostError}
        telemetryWarningFlags={telemetry.warningFlags}
        onToggleRemoteTelemetry={() => setRemoteTelemetryEnabled(prev => !prev)}
        onSendTelemetrySnapshot={handleSendTelemetrySnapshot}
      />
      <ScripturePopover
        open={popover !== null}
        title={popover?.title ?? ''}
        color={popover?.color ?? 'cited'}
        explanation={popover?.explanation}
        sourcePassage={popover?.sourcePassage}
        displayPassage={popover?.displayPassage}
        onClose={() => setPopover(null)}
      />
    </div>
  );
}


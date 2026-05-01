'use client';
import { useRef, useState, useCallback, useEffect } from 'react';
import { BibleVersionSelectors } from './BibleVersionSelectors';
import { VUMeter } from './VUMeter';
import { useBibleVersions } from '@/lib/useBibleVersions';
import { getWebSocketBaseUrl } from '@/lib/wsBaseUrl';
import { float32ChunksToBase64Payloads } from '@/lib/audioUtils';

type Status = 'idle' | 'connecting' | 'active' | 'reconnecting' | 'error';

interface SoundboardAdminProps {
  churchId: string;
}

const BATCH_INTERVAL_MS = 100;
const MAX_RETRY_DELAY_MS = 30_000;

export function SoundboardAdmin({ churchId }: SoundboardAdminProps) {
  const [status, setStatus] = useState<Status>('idle');
  const [errorMsg, setErrorMsg] = useState('');
  const [stream, setStream] = useState<MediaStream | null>(null);
  const [sermonTopic, setSermonTopic] = useState('');
  const [sourceScriptureVersion, setSourceScriptureVersion] = useState('rvr1960');
  const [displayScriptureVersion, setDisplayScriptureVersion] = useState('kjv');
  const { versions, loading: versionsLoading, error: versionsError } = useBibleVersions(churchId);

  const wsRef = useRef<WebSocket | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const workletRef = useRef<AudioWorkletNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const sendBufferRef = useRef<Float32Array[]>([]);
  const sendTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const retryDelayRef = useRef(1000);
  const shouldReconnectRef = useRef(false);
  const sampleRateRef = useRef(48000);
  const sermonTopicRef = useRef('');
  const connectRef = useRef<() => void>(() => {});

  useEffect(() => {
    sermonTopicRef.current = sermonTopic;
  }, [sermonTopic]);

  const flushBuffer = useCallback(() => {
    if (sendBufferRef.current.length === 0 || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;

    const chunks = sendBufferRef.current;
    sendBufferRef.current = [];
    const payloads = float32ChunksToBase64Payloads(chunks);
    for (const payload of payloads) {
      wsRef.current.send(JSON.stringify({ type: 'audio', audio: payload }));
    }
  }, []);

  const connect = useCallback(() => {
    const ws = new WebSocket(`${getWebSocketBaseUrl()}/api/stream/v1?church_id=${encodeURIComponent(churchId)}`);
    wsRef.current = ws;

    ws.onopen = () => {
      setStatus('active');
      setErrorMsg('');
      retryDelayRef.current = 1000;
      ws.send(JSON.stringify({
        type: 'session.start',
        sampleRate: sampleRateRef.current,
        topic: sermonTopicRef.current,
        sourceScriptureVersion,
        displayScriptureVersion,
      }));
    };

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === 'error') {
        setErrorMsg(msg.message);
        setStatus('error');
      }
    };

    ws.onclose = () => {
      if (!shouldReconnectRef.current) return;
      setStatus('reconnecting');
      setTimeout(() => {
        if (shouldReconnectRef.current) connectRef.current();
      }, retryDelayRef.current);
      retryDelayRef.current = Math.min(retryDelayRef.current * 2, MAX_RETRY_DELAY_MS);
    };

    ws.onerror = () => {
      setErrorMsg('Connection error — retrying...');
    };
  }, [churchId, displayScriptureVersion, sourceScriptureVersion]);

  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  const start = useCallback(async () => {
    setStatus('connecting');
    setErrorMsg('');

    try {
      const mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false,   // Chrome's processing destroys signal for STT
          noiseSuppression: false,   // let Deepgram handle noise, not the browser
          autoGainControl: true,
        },
        video: false,
      });
      streamRef.current = mediaStream;
      setStream(mediaStream);

      const ctx = new AudioContext();
      ctxRef.current = ctx;
      // Worklet downsamples to 16 kHz before posting chunks; advertise that rate
      // so the server's resample_float32_to_pcm16 hits the fast no-op path.
      sampleRateRef.current = 16000;

      await ctx.audioWorklet.addModule('/worklets/recorder-worklet.js');
      const source = ctx.createMediaStreamSource(mediaStream);
      const worklet = new AudioWorkletNode(ctx, 'recorder-processor');
      workletRef.current = worklet;

      worklet.port.onmessage = (e) => {
        if (e.data.type === 'chunk') sendBufferRef.current.push(e.data.samples);
      };

      source.connect(worklet);
      worklet.connect(ctx.destination);
      worklet.port.postMessage('start');

      sendTimerRef.current = setInterval(flushBuffer, BATCH_INTERVAL_MS);

      shouldReconnectRef.current = true;
      connect();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to start';
      setErrorMsg(msg);
      setStatus('error');
    }
  }, [connect, flushBuffer]);

  const stop = useCallback(() => {
    shouldReconnectRef.current = false;

    if (sendTimerRef.current) {
      clearInterval(sendTimerRef.current);
      sendTimerRef.current = null;
    }

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
    setStream(null);

    ctxRef.current?.close();
    ctxRef.current = null;

    sendBufferRef.current = [];
    setStatus('idle');
  }, []);

  useEffect(() => () => stop(), [stop]);

  const isLive = status === 'active' || status === 'reconnecting';

  const statusLabel: Record<Status, string> = {
    idle: 'Ready',
    connecting: 'Connecting...',
    active: 'Live',
    reconnecting: 'Reconnecting...',
    error: 'Error',
  };

  const statusColor: Record<Status, string> = {
    idle: 'text-gray-400',
    connecting: 'text-yellow-400',
    active: 'text-green-400',
    reconnecting: 'text-yellow-400',
    error: 'text-red-400',
  };

  return (
    <div className="bg-gray-800 rounded-xl p-6 space-y-4 max-w-md w-full">
      <div className="flex items-center justify-between">
        <h2 className="text-white font-semibold text-lg">Soundboard Admin</h2>
        <span className={`text-sm font-medium ${statusColor[status]}`}>
          {statusLabel[status]}
        </span>
      </div>

      {/* Sermon topic — editable before going live, read-only during session */}
      <div className="space-y-1">
        <label className="text-gray-400 text-xs font-medium uppercase tracking-wide">
          Sermon Topic
        </label>
        {isLive ? (
          <p className="text-gray-300 text-sm bg-gray-700 rounded px-3 py-2">
            {sermonTopic || <span className="text-gray-500 italic">No topic set</span>}
          </p>
        ) : (
          <input
            type="text"
            value={sermonTopic}
            onChange={(e) => setSermonTopic(e.target.value)}
            placeholder="e.g. The Great Commission — Matthew 28"
            className="w-full bg-gray-700 text-white text-sm rounded px-3 py-2 placeholder-gray-500 border border-gray-600 focus:border-blue-500 focus:outline-none"
          />
        )}
        <p className="text-gray-500 text-xs">
          Helps the AI resolve ambiguous theological terms correctly.
        </p>
      </div>

      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <p className="text-gray-400 text-xs font-medium uppercase tracking-wide">Bible Versions</p>
          {versionsLoading && <span className="text-[11px] text-gray-500">Loading…</span>}
        </div>
        {versionsError ? (
          <p className="text-red-400 text-xs">{versionsError}</p>
        ) : versions.length > 0 ? (
          <BibleVersionSelectors
            versions={versions}
            sourceVersion={sourceScriptureVersion}
            displayVersion={displayScriptureVersion}
            onSourceVersionChange={setSourceScriptureVersion}
            onDisplayVersionChange={setDisplayScriptureVersion}
            disabled={isLive}
          />
        ) : null}
        <p className="text-gray-500 text-xs">
          Defaults are RVR1960 for detected scripture and KJV for displayed passages.
        </p>
      </div>

      <VUMeter stream={stream} />

      {errorMsg && (
        <p className="text-red-400 text-sm">{errorMsg}</p>
      )}

      <button
        onClick={isLive ? stop : start}
        disabled={status === 'connecting' || status === 'reconnecting'}
        className={`w-full py-3 rounded-lg font-semibold text-white transition-colors disabled:opacity-50 ${
          isLive
            ? 'bg-red-600 hover:bg-red-700'
            : 'bg-green-600 hover:bg-green-700'
        }`}
      >
        {isLive ? 'End Session' : 'Go Live'}
      </button>

      <p className="text-gray-500 text-xs text-center">Church: {churchId}</p>
    </div>
  );
}

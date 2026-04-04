'use client';
import { useEffect, useRef, useState, useCallback } from 'react';
import { motion } from 'framer-motion';
import { getWebSocketBaseUrl } from '@/lib/wsBaseUrl';
import { float32ToBase64 } from '@/lib/audioUtils';
import { useTranslationFeed } from '@/lib/useTranslationFeed';

type Status = 'idle' | 'connecting' | 'active' | 'reconnecting' | 'error';

interface MobileTestProps {
  churchId: string;
}

const BATCH_INTERVAL_MS = 100;
const MAX_RETRY_DELAY_MS = 30_000;

// ── Audio capture + streaming ─────────────────────────────────────────────────

function useAudioStream(churchId: string) {
  const [status, setStatus] = useState<Status>('idle');
  const [errorMsg, setErrorMsg] = useState('');

  const wsRef = useRef<WebSocket | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const workletRef = useRef<AudioWorkletNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const sendBufferRef = useRef<Float32Array[]>([]);
  const sendTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const retryDelayRef = useRef(1000);
  const shouldReconnectRef = useRef(false);
  const sampleRateRef = useRef(48000);

  const flushBuffer = useCallback(() => {
    if (!sendBufferRef.current.length || wsRef.current?.readyState !== WebSocket.OPEN) return;
    const chunks = sendBufferRef.current;
    sendBufferRef.current = [];
    wsRef.current.send(JSON.stringify({ type: 'audio', audio: float32ToBase64(chunks) }));
  }, []);

  const connect = useCallback(() => {
    const ws = new WebSocket(`${getWebSocketBaseUrl()}/api/stream/v1?church_id=${encodeURIComponent(churchId)}`);
    wsRef.current = ws;

    ws.onopen = () => {
      setStatus('active');
      setErrorMsg('');
      retryDelayRef.current = 1000;
      ws.send(JSON.stringify({ type: 'session.start', sampleRate: sampleRateRef.current, topic: '' }));
    };

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === 'error') { setErrorMsg(msg.message); setStatus('error'); }
    };

    ws.onclose = () => {
      if (!shouldReconnectRef.current) return;
      setStatus('reconnecting');
      setTimeout(() => { if (shouldReconnectRef.current) connect(); }, retryDelayRef.current);
      retryDelayRef.current = Math.min(retryDelayRef.current * 2, MAX_RETRY_DELAY_MS);
    };
  }, [churchId]);

  const start = useCallback(async () => {
    setStatus('connecting');
    setErrorMsg('');
    try {
      const mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: true },
        video: false,
      });
      streamRef.current = mediaStream;
      const ctx = new AudioContext();
      ctxRef.current = ctx;
      sampleRateRef.current = ctx.sampleRate;
      if (ctx.state === 'suspended') await ctx.resume();
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
      setErrorMsg(err instanceof Error ? err.message : 'Failed to access microphone');
      setStatus('error');
    }
  }, [connect, flushBuffer]);

  const stop = useCallback(() => {
    shouldReconnectRef.current = false;
    if (sendTimerRef.current) { clearInterval(sendTimerRef.current); sendTimerRef.current = null; }
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
    setStatus('idle');
  }, []);

  useEffect(() => () => stop(), [stop]);

  return { status, errorMsg, start, stop };
}

// ── Component ─────────────────────────────────────────────────────────────────

export function MobileTest({ churchId }: MobileTestProps) {
  const { status, errorMsg, start, stop } = useAudioStream(churchId);
  const { segments, spanishLines, partialSpanish, partialEnglish, connected: displayConnected, flashingId } = useTranslationFeed(churchId);

  const scrollRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const [scrolledUp, setScrolledUp] = useState(false);

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
  }, [segments, partialEnglish, partialSpanish, spanishLines]);

  const scrollToLive = useCallback(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, []);

  const isLive = status === 'active' || status === 'reconnecting';
  const activeSpanish =
    spanishLines.join(' ') + (spanishLines.length > 0 && partialSpanish ? ' ' : '') + partialSpanish;

  if (!isLive && status !== 'connecting') {
    return (
      <div className="h-screen bg-gray-950 text-white flex flex-col items-center justify-center gap-6 px-8">
        <div className="text-center space-y-2">
          <p className="text-gray-400 text-sm">Church: <span className="text-white">{churchId}</span></p>
          <h1 className="text-2xl font-semibold">Translation Test</h1>
          <p className="text-gray-500 text-sm">Speak in Spanish. Translation appears in real time.</p>
        </div>
        {errorMsg && <p className="text-red-400 text-sm text-center">{errorMsg}</p>}
        <button
          onClick={start}
          className="w-full max-w-xs py-4 rounded-2xl bg-green-600 hover:bg-green-500 active:bg-green-700 text-white text-lg font-semibold transition-colors"
        >
          Start Recording
        </button>
        <p className="text-gray-600 text-xs text-center">
          This page uses your microphone and streams audio to the server.
        </p>
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
      <div className="flex-none flex items-center justify-between px-4 py-3 bg-gray-900 border-b border-gray-800">
        <div className="flex items-center gap-2">
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
        <button
          onClick={stop}
          className="bg-red-600/80 hover:bg-red-600 active:bg-red-700 text-white text-xs font-medium px-3 py-1.5 rounded-full transition-colors"
        >
          End
        </button>
      </div>

      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto relative"
      >
        <div className="min-h-full flex flex-col justify-end px-5 pt-12 pb-6 gap-4">
          {segments.length === 0 && !activeSpanish && !partialEnglish && (
            <p className="text-gray-600 text-sm text-center mb-4">Waiting for speech...</p>
          )}
          {segments.map((s) => (
            <motion.div
              key={s.id}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1, transition: { duration: 0.4 } }}
              className="space-y-0.5"
            >
              <p className={`text-xl font-semibold leading-snug transition-colors duration-[600ms] ${
                flashingId === s.id ? 'text-blue-200' : ''
              }`}>{s.english}</p>
              <p className="text-sm text-gray-500 leading-snug">{s.spanish}</p>
            </motion.div>
          ))}
          <div className="space-y-0.5 min-h-[2rem]">
            {partialEnglish && (
              <p className="text-xl font-semibold leading-snug text-gray-400">
                {partialEnglish}<span className="animate-pulse text-blue-400 ml-0.5">▌</span>
              </p>
            )}
            {activeSpanish && (
              <p className="text-sm text-gray-600 leading-snug">{activeSpanish}</p>
            )}
          </div>
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

      {errorMsg && (
        <div className="flex-none px-4 py-2 bg-red-900/50 border-t border-red-800">
          <p className="text-red-300 text-xs">{errorMsg}</p>
        </div>
      )}
    </div>
  );
}

'use client';
import { useRef, useState, useCallback, useEffect } from 'react';
import { VUMeter } from './VUMeter';

type Status = 'idle' | 'connecting' | 'active' | 'reconnecting' | 'error';

interface SoundboardAdminProps {
  churchId: string;
}

const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? 'ws://localhost:8000';
const BATCH_INTERVAL_MS = 100;
const MAX_RETRY_DELAY_MS = 30_000;

export function SoundboardAdmin({ churchId }: SoundboardAdminProps) {
  const [status, setStatus] = useState<Status>('idle');
  const [errorMsg, setErrorMsg] = useState('');
  const [stream, setStream] = useState<MediaStream | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const workletRef = useRef<AudioWorkletNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const sendBufferRef = useRef<Float32Array[]>([]);
  const sendTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const retryDelayRef = useRef(1000);
  const pendingBufferRef = useRef<ArrayBuffer[]>([]);  // buffer while disconnected
  const shouldReconnectRef = useRef(false);
  const sampleRateRef = useRef(48000);

  const flushBuffer = useCallback(() => {
    if (sendBufferRef.current.length === 0 || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;

    const chunks = sendBufferRef.current;
    sendBufferRef.current = [];

    const totalLen = chunks.reduce((s, c) => s + c.length, 0);
    const merged = new Float32Array(totalLen);
    let offset = 0;
    for (const c of chunks) { merged.set(c, offset); offset += c.length; }

    // Encode to bytes then base64
    const bytes = new Uint8Array(merged.buffer, merged.byteOffset, merged.byteLength);
    let binary = '';
    for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    const b64 = btoa(binary);

    if (wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'audio', audio: b64 }));
    } else {
      pendingBufferRef.current.push(merged.buffer);
    }
  }, []);

  const connect = useCallback(() => {
    const ws = new WebSocket(`${WS_URL}/api/stream/v1?church_id=${encodeURIComponent(churchId)}`);
    wsRef.current = ws;

    ws.onopen = () => {
      setStatus('active');
      setErrorMsg('');
      retryDelayRef.current = 1000;
      ws.send(JSON.stringify({ type: 'session.start', sampleRate: sampleRateRef.current }));

      // Flush any audio buffered during reconnect
      for (const buf of pendingBufferRef.current) {
        const bytes = new Uint8Array(buf);
        let binary = '';
        for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
        ws.send(JSON.stringify({ type: 'audio', audio: btoa(binary) }));
      }
      pendingBufferRef.current = [];
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
        if (shouldReconnectRef.current) connect();
      }, retryDelayRef.current);
      retryDelayRef.current = Math.min(retryDelayRef.current * 2, MAX_RETRY_DELAY_MS);
    };

    ws.onerror = () => {
      setErrorMsg('Connection error — retrying...');
    };
  }, [churchId]);

  const start = useCallback(async () => {
    setStatus('connecting');
    setErrorMsg('');

    try {
      const mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      streamRef.current = mediaStream;
      setStream(mediaStream);

      const ctx = new AudioContext();
      ctxRef.current = ctx;
      sampleRateRef.current = ctx.sampleRate;

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
    pendingBufferRef.current = [];
    setStatus('idle');
  }, []);

  // Cleanup on unmount
  useEffect(() => () => stop(), [stop]);

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

      <VUMeter stream={stream} />

      {errorMsg && (
        <p className="text-red-400 text-sm">{errorMsg}</p>
      )}

      <button
        onClick={status === 'idle' || status === 'error' ? start : stop}
        disabled={status === 'connecting' || status === 'reconnecting'}
        className={`w-full py-3 rounded-lg font-semibold text-white transition-colors disabled:opacity-50 ${
          status === 'active' || status === 'reconnecting'
            ? 'bg-red-600 hover:bg-red-700'
            : 'bg-green-600 hover:bg-green-700'
        }`}
      >
        {status === 'active' || status === 'reconnecting' ? 'End Session' : 'Go Live'}
      </button>

      <p className="text-gray-500 text-xs text-center">Church: {churchId}</p>
    </div>
  );
}

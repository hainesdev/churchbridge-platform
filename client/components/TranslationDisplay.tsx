'use client';
import { useEffect, useRef, useState } from 'react';

interface Segment {
  id: number;
  spanish: string;
  english: string;
}

interface TranslationDisplayProps {
  churchId: string;
  mode?: 'full' | 'lowerthird';
}

const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? 'ws://localhost:8000';

export function TranslationDisplay({ churchId, mode = 'full' }: TranslationDisplayProps) {
  const [segments, setSegments] = useState<Segment[]>([]);
  const [partialSpanish, setPartialSpanish] = useState('');
  const [partialEnglish, setPartialEnglish] = useState('');
  const [connected, setConnected] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const connect = () => {
      const ws = new WebSocket(`${WS_URL}/api/display/v1?church_id=${encodeURIComponent(churchId)}`);
      wsRef.current = ws;

      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        setTimeout(connect, 2000);
      };

      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);

        if (msg.type === 'interim') {
          setPartialSpanish(msg.text);
        } else if (msg.type === 'interim_translation') {
          setPartialEnglish(msg.text);
        } else if (msg.type === 'translation') {
          setSegments((prev) => [
            ...prev.slice(-30),   // keep last 30 segments
            { id: msg.ts, spanish: msg.spanish, english: msg.english },
          ]);
          setPartialSpanish('');
          setPartialEnglish('');
        }
      };
    };

    connect();
    return () => wsRef.current?.close();
  }, [churchId]);

  // Auto-scroll to bottom
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [segments, partialEnglish]);

  if (mode === 'lowerthird') {
    // Transparent overlay — last 2 lines only, for OBS/ProPresenter
    const recent = segments.slice(-2);
    return (
      <div className="fixed bottom-0 left-0 right-0 p-6 space-y-2">
        {recent.map((s) => (
          <div key={s.id} className="bg-black/70 px-4 py-2 rounded text-white text-2xl font-medium">
            {s.english}
          </div>
        ))}
        {partialEnglish && (
          <div className="bg-black/70 px-4 py-2 rounded text-white/80 text-2xl italic">
            {partialEnglish}
            <span className="animate-pulse">▌</span>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col bg-black text-white overflow-hidden">
      {/* Status bar */}
      <div className="flex-none px-6 py-2 bg-gray-900 flex items-center gap-2">
        <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-400 animate-pulse' : 'bg-red-500'}`} />
        <span className="text-xs text-gray-400">{connected ? 'Live' : 'Connecting...'}</span>
      </div>

      {/* Scrollable transcript */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
        {segments.length === 0 && !partialEnglish && (
          <p className="text-gray-600 text-center mt-8 text-lg">Waiting for service to begin...</p>
        )}

        {segments.map((s) => (
          <div key={s.id} className="space-y-1">
            <p className="text-4xl font-bold leading-tight">{s.english}</p>
            <p className="text-gray-500 text-lg">{s.spanish}</p>
          </div>
        ))}

        {partialEnglish && (
          <div className="space-y-1 opacity-80">
            <p className="text-4xl font-bold leading-tight text-gray-300">
              {partialEnglish}
              <span className="animate-pulse text-blue-400">▌</span>
            </p>
            {partialSpanish && (
              <p className="text-gray-600 text-lg italic">{partialSpanish}</p>
            )}
          </div>
        )}

        <div ref={bottomRef} />
      </div>
    </div>
  );
}

'use client';
import { useEffect, useRef, useState } from 'react';

interface Segment {
  id: number;
  spanish: string;
  english: string;
}

interface TranslationDisplayProps {
  churchId: string;
  mode?: 'full' | 'lowerthird' | 'spanish' | 'bilingual';
}

const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? 'ws://localhost:8000';
const MAX_CAPTION_SEGMENTS = 3; // rolling window for spanish / bilingual modes

export function TranslationDisplay({ churchId, mode = 'full' }: TranslationDisplayProps) {
  const [segments, setSegments] = useState<Segment[]>([]);
  const [spanishLines, setSpanishLines] = useState<string[]>([]); // stt_final commits
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
        } else if (msg.type === 'stt_final') {
          // Raw Deepgram final — append to rolling Spanish caption
          setSpanishLines((prev) => {
            const all = [...prev, msg.text];
            return all.slice(-MAX_CAPTION_SEGMENTS);
          });
          setPartialSpanish('');
        } else if (msg.type === 'interim_translation') {
          setPartialEnglish((prev) => prev ? prev + ' ' + msg.text : msg.text);
        } else if (msg.type === 'translation') {
          setSegments((prev) => [
            ...prev.slice(-30),
            { id: msg.ts, spanish: msg.spanish, english: msg.english },
          ]);
          setPartialSpanish('');
          setPartialEnglish('');
          // Clear rolling Spanish lines when a full sentence commits (bilingual mode)
          setSpanishLines([]);
        } else if (msg.type === 'correction') {
          setSegments((prev) =>
            prev.map((s) => s.id === msg.ts ? { ...s, english: msg.english } : s)
          );
        }
      };
    };

    connect();
    return () => wsRef.current?.close();
  }, [churchId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [segments, spanishLines, partialEnglish, partialSpanish]);

  const statusDot = (
    <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-400 animate-pulse' : 'bg-red-500'}`} />
  );

  // ── Lower-thirds overlay ──────────────────────────────────────────────────
  if (mode === 'lowerthird') {
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

  // ── Spanish captions only ─────────────────────────────────────────────────
  if (mode === 'spanish') {
    return (
      <div className="h-full flex flex-col bg-black text-white overflow-hidden">
        <div className="flex-none px-6 py-2 bg-gray-900 flex items-center gap-2">
          {statusDot}
          <span className="text-xs text-gray-400">{connected ? 'En vivo' : 'Conectando...'}</span>
        </div>

        <div className="flex-1 flex flex-col justify-end px-8 py-6 space-y-2">
          {spanishLines.length === 0 && !partialSpanish && (
            <p className="text-gray-600 text-center text-lg mb-8">
              Esperando que comience el servicio...
            </p>
          )}

          {spanishLines.map((line, i) => (
            <p key={i} className="text-4xl font-semibold leading-snug text-white">
              {line}
            </p>
          ))}

          {partialSpanish && (
            <p className="text-4xl font-semibold leading-snug text-gray-400 italic">
              {partialSpanish}
              <span className="animate-pulse text-yellow-400">▌</span>
            </p>
          )}
        </div>
      </div>
    );
  }

  // ── Spanish + English bilingual ───────────────────────────────────────────
  if (mode === 'bilingual') {
    return (
      <div className="h-full flex flex-col bg-black text-white overflow-hidden">
        <div className="flex-none px-6 py-2 bg-gray-900 flex items-center gap-2">
          {statusDot}
          <span className="text-xs text-gray-400">{connected ? 'Live' : 'Connecting...'}</span>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-5">
          {segments.length === 0 && !partialSpanish && (
            <p className="text-gray-600 text-center mt-8 text-lg">Waiting for service to begin...</p>
          )}

          {segments.map((s) => (
            <div key={s.id} className="space-y-1 border-l-2 border-gray-700 pl-4">
              <p className="text-3xl font-bold leading-tight">{s.spanish}</p>
              <p className="text-xl text-blue-300 leading-snug">{s.english}</p>
            </div>
          ))}

          {/* Live partial — Spanish from stt_final + interim, English building up */}
          {(spanishLines.length > 0 || partialSpanish || partialEnglish) && (
            <div className="space-y-1 border-l-2 border-yellow-600/50 pl-4 opacity-80">
              <p className="text-3xl font-bold leading-tight text-gray-200">
                {[...spanishLines, partialSpanish].filter(Boolean).join(' ')}
                {partialSpanish && <span className="animate-pulse text-yellow-400">▌</span>}
              </p>
              {partialEnglish && (
                <p className="text-xl text-blue-400/70 leading-snug italic">
                  {partialEnglish}
                </p>
              )}
            </div>
          )}

          <div ref={bottomRef} />
        </div>
      </div>
    );
  }

  // ── Full mode (English primary, Spanish secondary) ────────────────────────
  return (
    <div className="h-full flex flex-col bg-black text-white overflow-hidden">
      <div className="flex-none px-6 py-2 bg-gray-900 flex items-center gap-2">
        {statusDot}
        <span className="text-xs text-gray-400">{connected ? 'Live' : 'Connecting...'}</span>
      </div>

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

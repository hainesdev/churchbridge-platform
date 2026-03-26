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

// How many committed segments to keep visible above the active partial.
// Older segments fade out to guide eyes toward the bottom.
const VISIBLE_SEGMENTS = 2;
const VISIBLE_CAPTION_LINES = 3;

export function TranslationDisplay({ churchId, mode = 'full' }: TranslationDisplayProps) {
  const [segments, setSegments] = useState<Segment[]>([]);
  const [spanishLines, setSpanishLines] = useState<string[]>([]);
  const [partialSpanish, setPartialSpanish] = useState('');
  const [partialEnglish, setPartialEnglish] = useState('');
  const [connected, setConnected] = useState(false);
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
          setSpanishLines((prev) => [...prev, msg.text].slice(-VISIBLE_CAPTION_LINES));
          setPartialSpanish('');
        } else if (msg.type === 'interim_translation') {
          setPartialEnglish((prev) => prev ? prev + ' ' + msg.text : msg.text);
        } else if (msg.type === 'translation') {
          setSegments((prev) => [...prev, { id: msg.ts, spanish: msg.spanish, english: msg.english }]);
          setSpanishLines([]);
          setPartialSpanish('');
          setPartialEnglish('');
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
    const committed = segments.slice(-1);          // last committed sentence
    const activeText = spanishLines.join(' ');     // current accumulating fragments

    return (
      <div className="h-full flex flex-col bg-black text-white overflow-hidden">
        <div className="flex-none px-6 py-2 bg-gray-900 flex items-center gap-2">
          {statusDot}
          <span className="text-xs text-gray-400">{connected ? 'En vivo' : 'Conectando...'}</span>
        </div>

        {/* Fixed bottom-anchored display — no scroll */}
        <div className="flex-1 flex flex-col justify-end px-8 pb-10 pt-4 gap-4 overflow-hidden">
          {/* Previously committed sentence — dimmed to signal it's been read */}
          {committed.map((s) => (
            <p key={s.id} className="text-3xl font-semibold leading-snug text-gray-500 transition-opacity duration-500">
              {s.spanish}
            </p>
          ))}

          {/* Active line: committed fragments + in-progress partial */}
          {(activeText || partialSpanish) && (
            <p className="text-4xl font-bold leading-snug text-white animate-slide-up">
              {activeText}
              {activeText && partialSpanish ? ' ' : ''}
              {partialSpanish && (
                <span className="text-gray-300 italic">{partialSpanish}</span>
              )}
              <span className="animate-pulse text-yellow-400">▌</span>
            </p>
          )}

          {!activeText && !partialSpanish && segments.length === 0 && (
            <p className="text-gray-600 text-center text-lg">
              Esperando que comience el servicio...
            </p>
          )}
        </div>
      </div>
    );
  }

  // ── Bilingual: Spanish primary + English secondary ────────────────────────
  if (mode === 'bilingual') {
    const visible = segments.slice(-VISIBLE_SEGMENTS);
    const activeSpanish = spanishLines.join(' ');

    return (
      <div className="h-full flex flex-col bg-black text-white overflow-hidden">
        <div className="flex-none px-6 py-2 bg-gray-900 flex items-center gap-2">
          {statusDot}
          <span className="text-xs text-gray-400">{connected ? 'Live' : 'Connecting...'}</span>
        </div>

        <div className="flex-1 flex flex-col justify-end px-8 pb-10 pt-4 gap-5 overflow-hidden">
          {visible.length === 0 && !activeSpanish && !partialSpanish && (
            <p className="text-gray-600 text-center text-lg">Waiting for service to begin...</p>
          )}

          {/* Committed bilingual pairs — older ones are dimmer */}
          {visible.map((s, i) => {
            const isNewest = i === visible.length - 1;
            return (
              <div
                key={s.id}
                className={`space-y-1 border-l-2 pl-4 transition-opacity duration-500 ${
                  isNewest
                    ? 'border-gray-500 opacity-100 animate-slide-up'
                    : 'border-gray-700 opacity-40'
                }`}
              >
                <p className="text-3xl font-bold leading-tight">{s.spanish}</p>
                <p className="text-lg text-blue-300 leading-snug">{s.english}</p>
              </div>
            );
          })}

          {/* Active partial — Spanish builds fast, English follows */}
          {(activeSpanish || partialSpanish || partialEnglish) && (
            <div className="space-y-1 border-l-2 border-yellow-600/60 pl-4 animate-slide-up">
              <p className="text-3xl font-bold leading-tight text-white">
                {activeSpanish}
                {activeSpanish && partialSpanish ? ' ' : ''}
                {partialSpanish && <span className="text-gray-400 italic">{partialSpanish}</span>}
                <span className="animate-pulse text-yellow-400">▌</span>
              </p>
              {partialEnglish && (
                <p className="text-lg text-blue-400/70 leading-snug italic">{partialEnglish}</p>
              )}
            </div>
          )}
        </div>
      </div>
    );
  }

  // ── Full mode: English primary, Spanish secondary ─────────────────────────
  const visible = segments.slice(-VISIBLE_SEGMENTS);

  return (
    <div className="h-full flex flex-col bg-black text-white overflow-hidden">
      <div className="flex-none px-6 py-2 bg-gray-900 flex items-center gap-2">
        {statusDot}
        <span className="text-xs text-gray-400">{connected ? 'Live' : 'Connecting...'}</span>
      </div>

      <div className="flex-1 flex flex-col justify-end px-8 pb-10 pt-4 gap-6 overflow-hidden">
        {visible.length === 0 && !partialEnglish && (
          <p className="text-gray-600 text-center text-lg">Waiting for service to begin...</p>
        )}

        {/* Committed segments — older one dimmed */}
        {visible.map((s, i) => {
          const isNewest = i === visible.length - 1;
          return (
            <div
              key={s.id}
              className={`space-y-1 transition-opacity duration-500 ${
                isNewest ? 'opacity-100 animate-slide-up' : 'opacity-35'
              }`}
            >
              <p className="text-4xl font-bold leading-tight">{s.english}</p>
              <p className="text-gray-500 text-lg">{s.spanish}</p>
            </div>
          );
        })}

        {/* Active partial */}
        {partialEnglish && (
          <div className="space-y-1">
            <p className="text-4xl font-bold leading-tight text-gray-300">
              {partialEnglish}
              <span className="animate-pulse text-blue-400">▌</span>
            </p>
            {partialSpanish && (
              <p className="text-gray-600 text-lg italic">{partialSpanish}</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

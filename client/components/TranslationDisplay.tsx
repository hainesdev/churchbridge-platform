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

// How many committed sentences to keep in the visible history trail
const HISTORY_LINES = 2;

/**
 * Word-level stable text renderer.
 *
 * Unchanged words keep their DOM node (no animation). Only words that are
 * new or changed get a new key and trigger animate-fade-in. This makes text
 * feel "sticky" — corrections shimmer only the changed words, new words
 * fade in at their natural position, nothing jumps.
 */
function StableText({
  text,
  className = '',
  cursorColor = '',
  showCursor = false,
}: {
  text: string;
  className?: string;
  cursorColor?: string;
  showCursor?: boolean;
}) {
  type WordEntry = { word: string; uid: number; isNew: boolean };
  const [entries, setEntries] = useState<WordEntry[]>([]);
  const uidRef = useRef(0);

  useEffect(() => {
    const incoming = text.split(/\s+/).filter(Boolean);
    setEntries((prev) =>
      incoming.map((word, i) => {
        const existing = prev[i];
        if (existing && existing.word === word) {
          // Same word at same position — keep stable, no animation
          return { ...existing, isNew: false };
        }
        // New or changed word — assign fresh uid to trigger fade-in
        return { word, uid: ++uidRef.current, isNew: true };
      })
    );
  }, [text]);

  return (
    <span className={className}>
      {entries.map((e) => (
        <span key={e.uid} className={e.isNew ? 'animate-fade-in' : ''}>
          {e.word}{' '}
        </span>
      ))}
      {showCursor && (
        <span className={`animate-pulse ${cursorColor}`}>▌</span>
      )}
    </span>
  );
}

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
      ws.onclose = () => { setConnected(false); setTimeout(connect, 2000); };

      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === 'interim') {
          setPartialSpanish(msg.text);
        } else if (msg.type === 'stt_final') {
          setSpanishLines((prev) => [...prev, msg.text].slice(-4));
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

  const statusBar = (label: string) => (
    <div className="flex-none px-6 py-2 bg-gray-900 flex items-center gap-2">
      <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-400 animate-pulse' : 'bg-red-500'}`} />
      <span className="text-xs text-gray-400">{connected ? label : 'Connecting...'}</span>
    </div>
  );

  // History trail: last HISTORY_LINES committed sentences, oldest → dimmest
  const history = segments.slice(-HISTORY_LINES);

  // Opacity steps for history lines: oldest is most faded
  const historyOpacity = ['opacity-20', 'opacity-45'];

  // ── Lower-thirds overlay ──────────────────────────────────────────────────
  if (mode === 'lowerthird') {
    return (
      <div className="fixed bottom-0 left-0 right-0 p-6 space-y-2">
        {segments.slice(-2).map((s) => (
          <div key={s.id} className="bg-black/70 px-4 py-2 rounded text-white text-2xl font-medium">
            {s.english}
          </div>
        ))}
        {partialEnglish && (
          <div className="bg-black/70 px-4 py-2 rounded text-white/80 text-2xl italic">
            {partialEnglish}<span className="animate-pulse">▌</span>
          </div>
        )}
      </div>
    );
  }

  // ── Spanish captions ──────────────────────────────────────────────────────
  if (mode === 'spanish') {
    const activeSpanish = spanishLines.join(' ');
    return (
      <div className="h-full flex flex-col bg-black text-white overflow-hidden">
        {statusBar('En vivo')}
        <div className="flex-1" />

        <div className="flex-none px-10 pb-12 space-y-3">
          {/* History trail */}
          {history.map((s, i) => {
            const opacity = historyOpacity[i + (HISTORY_LINES - history.length)] ?? 'opacity-20';
            return (
              <div key={s.id} className={`${opacity} transition-opacity duration-700`}>
                <StableText text={s.spanish} className="text-2xl font-semibold leading-snug" />
              </div>
            );
          })}

          {/* Active line — fragments + in-progress partial */}
          <div className="text-4xl font-bold leading-snug text-white">
            <StableText
              text={[activeSpanish, partialSpanish].filter(Boolean).join(' ')}
              className="text-white"
              showCursor
              cursorColor="text-yellow-400"
            />
          </div>
        </div>
      </div>
    );
  }

  // ── Bilingual: Spanish primary + English secondary ────────────────────────
  if (mode === 'bilingual') {
    const activeSpanish = spanishLines.join(' ');
    return (
      <div className="h-full flex flex-col bg-black text-white overflow-hidden">
        {statusBar('Live')}
        <div className="flex-1" />

        <div className="flex-none px-10 pb-12 space-y-4">
          {history.map((s, i) => {
            const opacity = historyOpacity[i + (HISTORY_LINES - history.length)] ?? 'opacity-20';
            return (
              <div key={s.id} className={`${opacity} transition-opacity duration-700 space-y-0.5`}>
                <StableText text={s.spanish} className="text-2xl font-bold leading-snug block" />
                <StableText text={s.english} className="text-lg text-blue-300 leading-snug block" />
              </div>
            );
          })}

          {/* Active pair */}
          <div className="space-y-1">
            <div className="text-3xl font-bold leading-snug text-white">
              <StableText
                text={[activeSpanish, partialSpanish].filter(Boolean).join(' ')}
                showCursor
                cursorColor="text-yellow-400"
              />
            </div>
            {partialEnglish && (
              <div className="text-xl text-blue-300 leading-snug">
                <StableText text={partialEnglish} />
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  // ── Full mode: English primary ────────────────────────────────────────────
  return (
    <div className="h-full flex flex-col bg-black text-white overflow-hidden">
      {statusBar('Live')}
      <div className="flex-1" />

      <div className="flex-none px-10 pb-12 space-y-4">
        {history.map((s, i) => {
          const opacity = historyOpacity[i + (HISTORY_LINES - history.length)] ?? 'opacity-20';
          return (
            <div key={s.id} className={`${opacity} transition-opacity duration-700 space-y-0.5`}>
              <StableText text={s.english} className="text-2xl font-bold leading-snug block" />
              <StableText text={s.spanish} className="text-base text-gray-500 block" />
            </div>
          );
        })}

        {/* Active partial */}
        <div className="text-4xl font-bold leading-snug text-white">
          <StableText
            text={partialEnglish}
            showCursor
            cursorColor="text-blue-400"
          />
        </div>
      </div>
    </div>
  );
}

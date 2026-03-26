'use client';
import { useEffect, useRef, useState } from 'react';
import { motion, AnimatePresence, LayoutGroup } from 'framer-motion';

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

// How many committed sentences remain visible above the active line
const HISTORY_LINES = 2;

// Spring physics — controls the "friction" feel of blocks moving through space.
// Lower stiffness = more inertia (sluggish). Higher damping = less bounce.
const SPRING = { type: 'spring' as const, stiffness: 180, damping: 38 };
const FADE   = { duration: 1.1, ease: [0.4, 0, 0.2, 1] as [number,number,number,number] };

// Opacity of history slots — oldest first
const HISTORY_OPACITY = [0.18, 0.48];

/**
 * Word-level stable text.
 *
 * Each word is a motion.span with layout={true}. When new words arrive and
 * cause line-wrapping, existing words physically slide to their new grid
 * position (spring). Only new or corrected words fade in from zero —
 * unchanged words are spatially stable with no animation.
 */
function StableText({
  text,
  className = '',
  showCursor = false,
  cursorColor = 'text-blue-400',
}: {
  text: string;
  className?: string;
  showCursor?: boolean;
  cursorColor?: string;
}) {
  type Entry = { word: string; uid: number; isNew: boolean };
  const [entries, setEntries] = useState<Entry[]>([]);
  const uidRef = useRef(0);

  useEffect(() => {
    const incoming = text.split(/\s+/).filter(Boolean);
    if (incoming.length === 0) { setEntries([]); return; }
    setEntries((prev) =>
      incoming.map((word, i) => {
        const existing = prev[i];
        if (existing && existing.word === word) return { ...existing, isNew: false };
        return { word, uid: ++uidRef.current, isNew: true };
      })
    );
  }, [text]);

  return (
    <span className={`inline ${className}`}>
      <AnimatePresence mode="popLayout">
        {entries.map((e) => (
          <motion.span
            key={e.uid}
            layout
            initial={e.isNew ? { opacity: 0 } : false}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ layout: SPRING, opacity: { duration: 0.5 } }}
            className="inline"
          >
            {e.word}{' '}
          </motion.span>
        ))}
      </AnimatePresence>
      {showCursor && <span className={`animate-pulse ${cursorColor}`}>▌</span>}
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

  // Visible history: last HISTORY_LINES segments
  const history = segments.slice(-HISTORY_LINES);

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
        <LayoutGroup>
          <div className="flex-none px-10 pb-12 space-y-3">
            <AnimatePresence mode="popLayout">
              {history.map((s, i) => {
                const targetOpacity = HISTORY_OPACITY[i + (HISTORY_LINES - history.length)];
                return (
                  <motion.div
                    key={s.id}
                    layout
                    layoutId={`seg-es-${s.id}`}
                    initial={{ opacity: 0, y: 12 }}
                    animate={{ opacity: targetOpacity, y: 0 }}
                    exit={{ opacity: 0 }}
                    transition={{ layout: SPRING, opacity: FADE }}
                  >
                    <StableText text={s.spanish} className="text-2xl font-semibold leading-snug" />
                  </motion.div>
                );
              })}
            </AnimatePresence>

            <motion.div layout transition={{ layout: SPRING }}>
              <StableText
                text={[activeSpanish, partialSpanish].filter(Boolean).join(' ')}
                className="text-4xl font-bold leading-snug text-white"
                showCursor
                cursorColor="text-yellow-400"
              />
            </motion.div>
          </div>
        </LayoutGroup>
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
        <LayoutGroup>
          <div className="flex-none px-10 pb-12 space-y-4">
            <AnimatePresence mode="popLayout">
              {history.map((s, i) => {
                const targetOpacity = HISTORY_OPACITY[i + (HISTORY_LINES - history.length)];
                return (
                  <motion.div
                    key={s.id}
                    layout
                    layoutId={`seg-bi-${s.id}`}
                    initial={{ opacity: 0, y: 12 }}
                    animate={{ opacity: targetOpacity, y: 0 }}
                    exit={{ opacity: 0 }}
                    transition={{ layout: SPRING, opacity: FADE }}
                    className="space-y-0.5"
                  >
                    <StableText text={s.spanish} className="text-2xl font-bold leading-snug block" />
                    <StableText text={s.english} className="text-base text-blue-300 leading-snug block" />
                  </motion.div>
                );
              })}
            </AnimatePresence>

            <motion.div layout transition={{ layout: SPRING }} className="space-y-1">
              <StableText
                text={[activeSpanish, partialSpanish].filter(Boolean).join(' ')}
                className="text-3xl font-bold leading-snug text-white"
                showCursor
                cursorColor="text-yellow-400"
              />
              {partialEnglish && (
                <StableText text={partialEnglish} className="text-xl text-blue-300 leading-snug" />
              )}
            </motion.div>
          </div>
        </LayoutGroup>
      </div>
    );
  }

  // ── Full mode: English primary ────────────────────────────────────────────
  return (
    <div className="h-full flex flex-col bg-black text-white overflow-hidden">
      {statusBar('Live')}
      <div className="flex-1" />
      <LayoutGroup>
        <div className="flex-none px-10 pb-12 space-y-4">
          <AnimatePresence mode="popLayout">
            {history.map((s, i) => {
              const targetOpacity = HISTORY_OPACITY[i + (HISTORY_LINES - history.length)];
              return (
                <motion.div
                  key={s.id}
                  layout
                  layoutId={`seg-en-${s.id}`}
                  initial={{ opacity: 0, y: 12 }}
                  animate={{ opacity: targetOpacity, y: 0 }}
                  exit={{ opacity: 0 }}
                  transition={{ layout: SPRING, opacity: FADE }}
                  className="space-y-0.5"
                >
                  <StableText text={s.english} className="text-2xl font-bold leading-snug block" />
                  <StableText text={s.spanish} className="text-sm text-gray-500 block" />
                </motion.div>
              );
            })}
          </AnimatePresence>

          <motion.div layout transition={{ layout: SPRING }}>
            <StableText
              text={partialEnglish}
              className="text-4xl font-bold leading-snug text-white"
              showCursor
              cursorColor="text-blue-400"
            />
          </motion.div>
        </div>
      </LayoutGroup>
    </div>
  );
}

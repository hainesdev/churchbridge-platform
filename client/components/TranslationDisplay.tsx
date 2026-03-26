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

// Number of committed sentences kept visible above the active line.
// Slots are always rendered (even empty) so the layout height never changes.
const HISTORY_LINES = 2;

// Opacity for each history slot — slot 0 is oldest/dimmest
const SLOT_OPACITY = ['opacity-20', 'opacity-50'];

// Reserved minimum heights keep layout stable before content arrives.
// Increase if your font size causes wrapping that clips the history.
const HISTORY_SLOT_MIN_H = 'min-h-[3.5rem]'; // ~56px — 1 line of text-2xl
const ACTIVE_SLOT_MIN_H  = 'min-h-[4.5rem]'; // ~72px — 1 line of text-4xl

/**
 * Word-level stable text.
 *
 * Words that haven't changed keep their DOM node — no animation, no jump.
 * Only new or corrected words get a fresh uid and trigger animate-fade-in.
 * This makes the text feel spatially anchored: corrections shimmer only the
 * changed words, new words appear in place without displacing anything.
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
    <span className={className}>
      {entries.map((e) => (
        <span key={e.uid} className={e.isNew ? 'animate-fade-in' : ''}>
          {e.word}{' '}
        </span>
      ))}
      {showCursor && <span className={`animate-pulse ${cursorColor}`}>▌</span>}
    </span>
  );
}

/**
 * A single history slot.
 *
 * The outer div always occupies HISTORY_SLOT_MIN_H — even when empty.
 * This is the key to preventing vertical jumps: the layout height is
 * reserved from page load. Content fades in within the fixed space.
 *
 * key={segment.id} on the inner div triggers animate-fade-in whenever
 * the content changes, without remounting the outer reserved slot.
 */
function HistorySlot({
  segment,
  opacity,
  renderContent,
}: {
  segment: Segment | null;
  opacity: string;
  renderContent: (s: Segment) => React.ReactNode;
}) {
  return (
    <div className={`${HISTORY_SLOT_MIN_H} flex items-end overflow-hidden`}>
      {segment && (
        <div
          key={segment.id}
          className={`w-full ${opacity} animate-fade-in`}
          style={{ transition: 'opacity 1400ms ease-out' }}
        >
          {renderContent(segment)}
        </div>
      )}
    </div>
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

  // Always exactly HISTORY_LINES slots, null when not yet filled
  const historySlots = Array.from({ length: HISTORY_LINES }, (_, i) => {
    const dataIndex = segments.length - HISTORY_LINES + i;
    return dataIndex >= 0 ? segments[dataIndex] : null;
  });

  const statusBar = (label: string) => (
    <div className="flex-none px-6 py-2 bg-gray-900 flex items-center gap-2">
      <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-400 animate-pulse' : 'bg-red-500'}`} />
      <span className="text-xs text-gray-400">{connected ? label : 'Connecting...'}</span>
    </div>
  );

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
          {historySlots.map((seg, i) => (
            <HistorySlot
              key={i}
              segment={seg}
              opacity={SLOT_OPACITY[i]}
              renderContent={(s) => (
                <StableText text={s.spanish} className="text-2xl font-semibold leading-snug" />
              )}
            />
          ))}
          <div className={`${ACTIVE_SLOT_MIN_H} flex items-start`}>
            <StableText
              text={[activeSpanish, partialSpanish].filter(Boolean).join(' ')}
              className="text-4xl font-bold leading-snug text-white"
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
        <div className="flex-none px-10 pb-12 space-y-3">
          {historySlots.map((seg, i) => (
            <HistorySlot
              key={i}
              segment={seg}
              opacity={SLOT_OPACITY[i]}
              renderContent={(s) => (
                <div className="space-y-0.5">
                  <StableText text={s.spanish} className="text-2xl font-bold leading-snug block" />
                  <StableText text={s.english} className="text-base text-blue-300 leading-snug block" />
                </div>
              )}
            />
          ))}
          <div className={`${ACTIVE_SLOT_MIN_H} flex flex-col justify-start gap-1`}>
            <StableText
              text={[activeSpanish, partialSpanish].filter(Boolean).join(' ')}
              className="text-3xl font-bold leading-snug text-white"
              showCursor
              cursorColor="text-yellow-400"
            />
            {partialEnglish && (
              <StableText text={partialEnglish} className="text-xl text-blue-300 leading-snug" />
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
      <div className="flex-none px-10 pb-12 space-y-3">
        {historySlots.map((seg, i) => (
          <HistorySlot
            key={i}
            segment={seg}
            opacity={SLOT_OPACITY[i]}
            renderContent={(s) => (
              <div className="space-y-0.5">
                <StableText text={s.english} className="text-2xl font-bold leading-snug block" />
                <StableText text={s.spanish} className="text-sm text-gray-500 block" />
              </div>
            )}
          />
        ))}
        <div className={`${ACTIVE_SLOT_MIN_H} flex items-start`}>
          <StableText
            text={partialEnglish}
            className="text-4xl font-bold leading-snug text-white"
            showCursor
            cursorColor="text-blue-400"
          />
        </div>
      </div>
    </div>
  );
}

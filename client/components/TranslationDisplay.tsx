'use client';
import { useEffect, useRef, useState, useCallback } from 'react';

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

/**
 * Scrollable feed anchored to the bottom of the screen.
 *
 * Committed sentences accumulate in a list — they never move or change opacity
 * after appearing. New sentences fade in at the bottom. The active partial sits
 * below all committed lines and updates in place. Auto-scroll keeps the view at
 * the bottom; a "↓ Live" button appears when the user has scrolled up into history.
 *
 * The core readability rule: text that is already on screen must never move,
 * reorder, or change opacity. Only new content animates.
 */

export function TranslationDisplay({ churchId, mode = 'full' }: TranslationDisplayProps) {
  const [segments, setSegments] = useState<Segment[]>([]);
  const [spanishLines, setSpanishLines] = useState<string[]>([]);
  const [partialSpanish, setPartialSpanish] = useState('');
  const [partialEnglish, setPartialEnglish] = useState('');
  const [connected, setConnected] = useState(false);
  const [scrolledUp, setScrolledUp] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);

  // ── WebSocket ────────────────────────────────────────────────────────────────
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
          setSpanishLines((prev) => [...prev, msg.text].slice(-8));
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

  // ── Auto-scroll ──────────────────────────────────────────────────────────────
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

  // ── Lower-thirds overlay (not a full-screen display) ─────────────────────────
  if (mode === 'lowerthird') {
    return (
      <div className="fixed bottom-0 left-0 right-0 p-6 space-y-2">
        {segments.slice(-2).map((s) => (
          <div key={s.id} className="bg-black/70 px-4 py-2 rounded text-white text-2xl font-medium animate-fade-in">
            {s.english}
          </div>
        ))}
        {partialEnglish && (
          <div className="bg-black/70 px-4 py-2 rounded text-white/70 text-2xl italic">
            {partialEnglish}<span className="animate-pulse">▌</span>
          </div>
        )}
      </div>
    );
  }

  // ── Shared UI pieces ─────────────────────────────────────────────────────────
  const statusBar = (label: string) => (
    <div className="flex-none px-6 py-2 bg-gray-900/80 flex items-center gap-2 z-10">
      <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-400 animate-pulse' : 'bg-red-500'}`} />
      <span className="text-xs text-gray-400">{connected ? label : 'Connecting...'}</span>
    </div>
  );

  const liveButton = scrolledUp && (
    <button
      onClick={scrollToLive}
      className="absolute bottom-6 right-6 z-20 bg-white/10 hover:bg-white/20 backdrop-blur-sm text-white text-sm px-4 py-2 rounded-full border border-white/20 transition-colors"
    >
      ↓ Live
    </button>
  );

  const activeSpanish =
    spanishLines.join(' ') +
    (spanishLines.length > 0 && partialSpanish ? ' ' : '') +
    partialSpanish;

  // ── Spanish captions ──────────────────────────────────────────────────────────
  if (mode === 'spanish') {
    return (
      <div className="h-full flex flex-col bg-black text-white overflow-hidden relative">
        {statusBar('En vivo')}
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          className="flex-1 overflow-y-auto"
        >
          <div className="min-h-full flex flex-col justify-end px-10 pt-20 pb-8 gap-4">
            {segments.map((s) => (
              <p key={s.id} className="text-4xl font-semibold leading-snug animate-fade-in">
                {s.spanish}
              </p>
            ))}
            {/* Active partial — updates in place, no animation */}
            <p className="text-4xl font-semibold leading-snug text-gray-400 min-h-[3rem]">
              {activeSpanish}
              {activeSpanish && <span className="animate-pulse text-yellow-500 ml-0.5">▌</span>}
            </p>
          </div>
        </div>
        {liveButton}
      </div>
    );
  }

  // ── Bilingual: Spanish primary + English secondary ────────────────────────────
  if (mode === 'bilingual') {
    return (
      <div className="h-full flex flex-col bg-black text-white overflow-hidden relative">
        {statusBar('Live')}
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          className="flex-1 overflow-y-auto"
        >
          <div className="min-h-full flex flex-col justify-end px-10 pt-20 pb-8 gap-5">
            {segments.map((s) => (
              <div key={s.id} className="animate-fade-in space-y-1">
                <p className="text-3xl font-semibold leading-snug">{s.spanish}</p>
                <p className="text-lg text-blue-300 leading-snug">{s.english}</p>
              </div>
            ))}
            {/* Active partial */}
            <div className="space-y-1 min-h-[4rem]">
              <p className="text-3xl font-semibold leading-snug text-gray-400">
                {activeSpanish}
                {activeSpanish && <span className="animate-pulse text-yellow-500 ml-0.5">▌</span>}
              </p>
              {partialEnglish && (
                <p className="text-lg text-blue-400/60 leading-snug">{partialEnglish}</p>
              )}
            </div>
          </div>
        </div>
        {liveButton}
      </div>
    );
  }

  // ── Full mode: English primary ────────────────────────────────────────────────
  return (
    <div className="h-full flex flex-col bg-black text-white overflow-hidden relative">
      {statusBar('Live')}
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto"
      >
        <div className="min-h-full flex flex-col justify-end px-10 pt-20 pb-8 gap-5">
          {segments.map((s) => (
            <div key={s.id} className="animate-fade-in space-y-1">
              <p className="text-3xl font-semibold leading-snug">{s.english}</p>
              <p className="text-base text-gray-500 leading-snug">{s.spanish}</p>
            </div>
          ))}
          {/* Active partial — updates in place, no animation */}
          <p className="text-3xl font-semibold leading-snug text-gray-400 min-h-[2.5rem]">
            {partialEnglish}
            {partialEnglish && <span className="animate-pulse text-blue-400 ml-0.5">▌</span>}
          </p>
        </div>
      </div>
      {liveButton}
    </div>
  );
}

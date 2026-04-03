'use client';
import { useEffect, useRef, useState, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { prepare, layout } from '@chenglou/pretext';

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
const PARTIAL_FLUSH_MS = 80;
const DRAIN_MS = 300;
const FLASH_MS = 600;

export function TranslationDisplay({ churchId, mode = 'full' }: TranslationDisplayProps) {
  const [segments, setSegments] = useState<Segment[]>([]);
  const [spanishLines, setSpanishLines] = useState<string[]>([]);
  const [partialSpanish, setPartialSpanish] = useState('');
  const [partialEnglish, setPartialEnglish] = useState('');
  const [connected, setConnected] = useState(false);
  const [scrolledUp, setScrolledUp] = useState(false);
  const [drainingEnglish, setDrainingEnglish] = useState('');
  const [flashingId, setFlashingId] = useState<number | null>(null);
  const [slotMinH, setSlotMinH] = useState(42);

  const scrollRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const partialEnglishRef = useRef('');
  const partialQueueRef = useRef('');
  const fontProbeRef = useRef<HTMLParagraphElement>(null);
  const fontSpecRef = useRef('');
  const lineHeightRef = useRef(42);
  const containerWidthRef = useRef(0);
  const flashTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const drainTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Keep partial ref in sync for WS handler closure
  useEffect(() => { partialEnglishRef.current = partialEnglish; }, [partialEnglish]);

  // Capture font metrics once fonts are loaded (full mode only — probe is rendered there)
  useEffect(() => {
    if (mode !== 'full') return;
    document.fonts.ready.then(() => {
      if (!fontProbeRef.current) return;
      const cs = getComputedStyle(fontProbeRef.current);
      fontSpecRef.current = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
      lineHeightRef.current = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.375;
    });
  }, [mode]);

  // Track container width for pretext layout calculations
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      containerWidthRef.current = entry.contentRect.width;
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Flush buffered partial tokens to state on a fixed interval
  useEffect(() => {
    const timer = setInterval(() => {
      const q = partialQueueRef.current;
      setPartialEnglish(prev => (prev !== q ? q : prev));
    }, PARTIAL_FLUSH_MS);
    return () => clearInterval(timer);
  }, []);

  // WebSocket
  useEffect(() => {
    let ws: WebSocket;
    let stopped = false;

    const connect = () => {
      if (stopped) return;
      ws = new WebSocket(`${WS_URL}/api/display/v1?church_id=${encodeURIComponent(churchId)}`);
      ws.onopen = () => setConnected(true);
      ws.onclose = () => { setConnected(false); setTimeout(connect, 2000); };

      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);

        if (msg.type === 'interim') {
          setPartialSpanish(msg.text);

        } else if (msg.type === 'stt_final') {
          setSpanishLines(prev => [...prev, msg.text].slice(-8));
          setPartialSpanish('');

        } else if (msg.type === 'interim_translation') {
          partialQueueRef.current = partialQueueRef.current
            ? partialQueueRef.current + ' ' + msg.text
            : msg.text;

        } else if (msg.type === 'translation') {
          // Pre-measure the committed segment's height so the slot doesn't jump
          if (mode === 'full' && fontSpecRef.current && containerWidthRef.current > 0) {
            try {
              const contentW = containerWidthRef.current - 80; // px-10 = 40px each side
              const p = prepare(msg.english, fontSpecRef.current);
              const { height } = layout(p, contentW, lineHeightRef.current);
              setSlotMinH(height);
            } catch { /* font not ready yet — slot keeps previous height */ }
          }

          // Drain the committed text (not the partial fragment) — avoids a truncated near-duplicate
          if (drainTimerRef.current) clearTimeout(drainTimerRef.current);
          setDrainingEnglish(msg.english);
          drainTimerRef.current = setTimeout(() => {
            setDrainingEnglish('');
            setSlotMinH(lineHeightRef.current); // reset to one-line height
          }, DRAIN_MS + 50);

          setSegments(prev => [...prev, { id: msg.ts, spanish: msg.spanish, english: msg.english }]);
          setSpanishLines([]);
          setPartialSpanish('');
          partialQueueRef.current = '';
          setPartialEnglish('');

        } else if (msg.type === 'correction') {
          setSegments(prev => prev.map(s => s.id === msg.ts ? { ...s, english: msg.english } : s));
          setFlashingId(msg.ts);
          if (flashTimerRef.current) clearTimeout(flashTimerRef.current);
          flashTimerRef.current = setTimeout(() => setFlashingId(null), FLASH_MS);
        }
      };
    };

    connect();
    return () => { stopped = true; ws?.close(); };
  }, [churchId, mode]);

  // Auto-scroll
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

  const activeSpanish =
    spanishLines.join(' ') +
    (spanishLines.length > 0 && partialSpanish ? ' ' : '') +
    partialSpanish;

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

  // ── Lower-thirds overlay ──────────────────────────────────────────────────────
  if (mode === 'lowerthird') {
    return (
      <div className="fixed bottom-0 left-0 right-0 p-6 space-y-2">
        <AnimatePresence mode="popLayout">
          {segments.slice(-2).map((s) => (
            <motion.div
              key={s.id}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.25 }}
              className="bg-black/70 px-4 py-2 rounded text-white text-2xl font-medium"
            >
              {s.english}
            </motion.div>
          ))}
        </AnimatePresence>
        {partialEnglish && (
          <div className="bg-black/70 px-4 py-2 rounded text-white/70 text-2xl italic">
            {partialEnglish}<span className="animate-pulse">▌</span>
          </div>
        )}
      </div>
    );
  }

  // ── Spanish captions ──────────────────────────────────────────────────────────
  if (mode === 'spanish') {
    return (
      <div className="h-full flex flex-col bg-black text-white overflow-hidden relative">
        {statusBar('En vivo')}
        <motion.div layoutScroll ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto">
          <div className="min-h-full flex flex-col justify-end px-10 pt-20 pb-8 gap-4">
            {segments.map((s) => (
              <motion.p
                key={s.id}
                layout
                initial={{ opacity: 0, y: 28 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{
                  layout: { type: 'spring', damping: 30, stiffness: 280 },
                  opacity: { duration: 0.3 },
                  y: { duration: 0.4, ease: 'easeOut' },
                }}
                className="text-4xl font-semibold leading-snug"
              >
                {s.spanish}
              </motion.p>
            ))}
            <motion.p
              layout
              transition={{ layout: { type: 'spring', damping: 30, stiffness: 280 } }}
              className="text-4xl font-semibold leading-snug text-gray-400 min-h-[3rem]"
            >
              {activeSpanish}
              {activeSpanish && <span className="animate-pulse text-yellow-500 ml-0.5">▌</span>}
            </motion.p>
          </div>
        </motion.div>
        {liveButton}
      </div>
    );
  }

  // ── Bilingual: Spanish primary + English secondary ────────────────────────────
  if (mode === 'bilingual') {
    return (
      <div className="h-full flex flex-col bg-black text-white overflow-hidden relative">
        {statusBar('Live')}
        <motion.div layoutScroll ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto">
          <div className="min-h-full flex flex-col justify-end px-10 pt-20 pb-8 gap-5">
            {segments.map((s) => (
              <motion.div
                key={s.id}
                layout
                initial={{ opacity: 0, y: 28 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{
                  layout: { type: 'spring', damping: 30, stiffness: 280 },
                  opacity: { duration: 0.3 },
                  y: { duration: 0.4, ease: 'easeOut' },
                }}
                className="space-y-1"
              >
                <p className="text-3xl font-semibold leading-snug">{s.spanish}</p>
                <p className={`text-lg text-blue-300 leading-snug transition-colors duration-500 ${
                  flashingId === s.id ? 'text-blue-100' : ''
                }`}>{s.english}</p>
              </motion.div>
            ))}
            <motion.div
              layout
              transition={{ layout: { type: 'spring', damping: 30, stiffness: 280 } }}
              className="space-y-1 min-h-[4rem]"
            >
              <p className="text-3xl font-semibold leading-snug text-gray-400">
                {activeSpanish}
                {activeSpanish && <span className="animate-pulse text-yellow-500 ml-0.5">▌</span>}
              </p>
              <div className="relative">
                <AnimatePresence>
                  {drainingEnglish && (
                    <motion.p
                      key="drain"
                      exit={{ opacity: 0, y: -3 }}
                      transition={{ duration: DRAIN_MS / 1000, ease: 'easeOut' }}
                      className="absolute inset-x-0 top-0 text-lg text-blue-400/60 leading-snug pointer-events-none"
                    >
                      {drainingEnglish}
                    </motion.p>
                  )}
                </AnimatePresence>
                {partialEnglish && (
                  <p className="text-lg text-blue-400/60 leading-snug">{partialEnglish}</p>
                )}
              </div>
            </motion.div>
          </div>
        </motion.div>
        {liveButton}
      </div>
    );
  }

  // ── Full mode: English primary ────────────────────────────────────────────────
  return (
    <div className="h-full flex flex-col bg-black text-white overflow-hidden relative">
      {/* Probe element — always rendered, invisible — gives getComputedStyle the live font metrics */}
      <p
        ref={fontProbeRef}
        className="text-3xl font-semibold leading-snug absolute invisible pointer-events-none"
        aria-hidden="true"
      >M</p>

      {statusBar('Live')}
      <motion.div layoutScroll ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto">
        <div className="min-h-full flex flex-col justify-end px-10 pt-20 pb-8 gap-5">
          {segments.map((s) => (
            <motion.div
              key={s.id}
              layout
              initial={{ opacity: 0, y: 28 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{
                layout: { type: 'spring', damping: 30, stiffness: 280 },
                opacity: { duration: 0.3 },
                y: { duration: 0.4, ease: 'easeOut' },
              }}
              className="space-y-1"
            >
              <p className={`text-3xl font-semibold leading-snug transition-colors duration-[600ms] ${
                flashingId === s.id ? 'text-blue-200' : ''
              }`}>
                {s.english}
              </p>
              <p className="text-base text-gray-500 leading-snug">{s.spanish}</p>
            </motion.div>
          ))}

          {/* Partial slot — pre-sized to incoming segment height via pretext */}
          <motion.div
            layout
            style={{ minHeight: slotMinH }}
            transition={{
              layout: { type: 'spring', damping: 30, stiffness: 280 },
              minHeight: { duration: 0.15 },
            }}
            className="relative"
          >
            {/* Drain: absolutely positioned so it overlaps the partial instead of stacking above it */}
            <AnimatePresence>
              {drainingEnglish && (
                <motion.p
                  key="drain"
                  exit={{ opacity: 0, y: -3 }}
                  transition={{ duration: DRAIN_MS / 1000, ease: 'easeOut' }}
                  className="absolute inset-x-0 top-0 text-3xl font-semibold leading-snug text-gray-400 pointer-events-none"
                >
                  {drainingEnglish}
                </motion.p>
              )}
            </AnimatePresence>
            {partialEnglish && (
              <p className="text-3xl font-semibold leading-snug text-gray-400">
                {partialEnglish}
                <span className="animate-pulse text-blue-400 ml-0.5">▌</span>
              </p>
            )}
          </motion.div>
        </div>
      </motion.div>
      {liveButton}
    </div>
  );
}

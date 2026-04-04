'use client';
import { useEffect, useRef, useState, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useTranslationFeed } from '@/lib/useTranslationFeed';
import { VersePanel } from './VersePanel';

interface TranslationDisplayProps {
  churchId: string;
  mode?: 'full' | 'lowerthird' | 'spanish' | 'bilingual';
}

export function TranslationDisplay({ churchId, mode = 'full' }: TranslationDisplayProps) {
  const {
    segments, spanishLines, partialSpanish, partialEnglish,
    connected, flashingId,
    verses, suggestions,
  } = useTranslationFeed(churchId);

  const [scrolledUp, setScrolledUp] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);

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

  // ── Lower-thirds overlay — no verse panel (OBS/projection use) ───────────────
  if (mode === 'lowerthird') {
    return (
      <div className="fixed bottom-0 left-0 right-0 p-6 space-y-2">
        <AnimatePresence mode="popLayout">
          {segments.slice(-2).map((s) => (
            <motion.div
              key={s.id}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1, transition: { duration: 0.3 } }}
              exit={{ opacity: 0, transition: { duration: 0.2 } }}
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
      <div className="h-full flex bg-black text-white overflow-hidden relative">
        <div className="flex-1 flex flex-col overflow-hidden">
          {statusBar('En vivo')}
          <div ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto">
            <div className="min-h-full flex flex-col justify-end px-10 pt-20 pb-8 gap-4">
              {segments.map((s) => (
                <motion.p
                  key={s.id}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1, transition: { duration: 0.4 } }}
                  className="text-4xl font-semibold leading-snug"
                >
                  {s.spanish}
                </motion.p>
              ))}
              <p className="text-4xl font-semibold leading-snug text-gray-400 min-h-[3rem]">
                {activeSpanish}
                {activeSpanish && <span className="animate-pulse text-yellow-500 ml-0.5">▌</span>}
              </p>
            </div>
          </div>
          {liveButton}
        </div>
        <VersePanel verses={verses} suggestions={suggestions} />
      </div>
    );
  }

  // ── Bilingual: Spanish primary + English secondary ────────────────────────────
  if (mode === 'bilingual') {
    return (
      <div className="h-full flex bg-black text-white overflow-hidden relative">
        <div className="flex-1 flex flex-col overflow-hidden">
          {statusBar('Live')}
          <div ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto">
            <div className="min-h-full flex flex-col justify-end px-10 pt-20 pb-8 gap-5">
              {segments.map((s) => (
                <motion.div
                  key={s.id}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1, transition: { duration: 0.4 } }}
                  className="space-y-1"
                >
                  <p className="text-3xl font-semibold leading-snug">{s.spanish}</p>
                  <div className="flex items-baseline gap-3">
                    <p className={`text-lg text-blue-300 leading-snug transition-colors duration-500 ${
                      flashingId === s.id ? 'text-blue-100' : ''
                    }`}>{s.english}</p>
                    {s.verseDetected && (
                      <span className="text-amber-400 text-xs font-mono shrink-0">
                        {s.verseDetected.reference}
                      </span>
                    )}
                  </div>
                </motion.div>
              ))}
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
        <VersePanel verses={verses} suggestions={suggestions} />
      </div>
    );
  }

  // ── Full mode: English primary ────────────────────────────────────────────────
  return (
    <div className="h-full flex bg-black text-white overflow-hidden relative">
      <div className="flex-1 flex flex-col overflow-hidden">
        {statusBar('Live')}
        <div ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto">
          <div className="min-h-full flex flex-col justify-end px-10 pt-20 pb-8 gap-5">
            {segments.map((s) => (
              <motion.div
                key={s.id}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1, transition: { duration: 0.4 } }}
                className="space-y-1"
              >
                <div className="flex items-baseline gap-3">
                  <p className={`text-3xl font-semibold leading-snug transition-colors duration-[600ms] ${
                    flashingId === s.id ? 'text-blue-200' : ''
                  }`}>
                    {s.english}
                  </p>
                  {s.verseDetected && (
                    <span className="text-amber-400 text-sm font-mono shrink-0">
                      {s.verseDetected.reference}
                    </span>
                  )}
                </div>
                <p className="text-base text-gray-500 leading-snug">{s.spanish}</p>
              </motion.div>
            ))}
            <div className="min-h-[2.5rem]">
              {partialEnglish && (
                <p className="text-3xl font-semibold leading-snug text-gray-400">
                  {partialEnglish}
                  <span className="animate-pulse text-blue-400 ml-0.5">▌</span>
                </p>
              )}
            </div>
          </div>
        </div>
        {liveButton}
      </div>
      <VersePanel verses={verses} suggestions={suggestions} />
    </div>
  );
}

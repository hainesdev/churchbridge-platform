'use client';
import { useEffect, useRef, useState, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { ScripturePopover } from './ScripturePopover';
import { useTranslationFeed, type VerseDetection, type VerseSuggestion } from '@/lib/useTranslationFeed';

interface TranslationDisplayProps {
  churchId: string;
  mode?: 'full' | 'lowerthird' | 'spanish' | 'bilingual';
}

export function TranslationDisplay({ churchId, mode = 'full' }: TranslationDisplayProps) {
  const {
    segments, spanishLines, partialSpanish, partialEnglish,
    connected, flashingId,
  } = useTranslationFeed(churchId);
  const [popover, setPopover] = useState<{
    title: string;
    color: 'cited' | 'recommended';
    explanation?: string;
    sourcePassage?: VerseDetection['source_passage'] | VerseSuggestion['source_passage'];
    displayPassage?: VerseDetection['display_passage'] | VerseSuggestion['display_passage'];
  } | null>(null);

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

  const renderVerseChips = useCallback((segment: typeof segments[number]) => (
    <div className="flex flex-wrap items-center gap-2">
      {segment.verseDetected && (
        <button
          onClick={() => setPopover({
            title: segment.verseDetected!.reference,
            color: 'cited',
            explanation: segment.verseDetected!.explanation,
            sourcePassage: segment.verseDetected!.source_passage,
            displayPassage: segment.verseDetected!.display_passage,
          })}
          className="rounded-full border border-amber-400/40 bg-amber-400/10 px-2 py-0.5 text-xs font-semibold text-amber-300 shrink-0"
        >
          {segment.verseDetected.reference}
        </button>
      )}
      {segment.verseSuggestions?.map((suggestion) => (
        <button
          key={suggestion.reference}
          onClick={() => setPopover({
            title: suggestion.reference,
            color: 'recommended',
            explanation: suggestion.explanation ?? suggestion.relevance_note,
            sourcePassage: suggestion.source_passage,
            displayPassage: suggestion.display_passage,
          })}
          className="rounded-full border border-sky-400/40 bg-sky-400/10 px-2 py-0.5 text-xs font-semibold text-sky-300 shrink-0"
        >
          {suggestion.reference}
        </button>
      ))}
    </div>
  ), []);

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
              className={`bg-black/70 px-4 py-2 rounded text-2xl font-medium transition-opacity duration-300 ${
                s.pendingCompletion ? 'text-white/50 italic' : 'text-white'
              }`}
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
                <motion.div
                  key={s.id}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1, transition: { duration: 0.4 } }}
                  className="space-y-2"
                >
                  <span className="block text-4xl font-semibold leading-snug">{s.spanish}</span>
                  {renderVerseChips(s)}
                </motion.div>
              ))}
              <p className="text-4xl font-semibold leading-snug text-gray-400 min-h-[3rem]">
                {activeSpanish}
                {activeSpanish && <span className="animate-pulse text-yellow-500 ml-0.5">▌</span>}
              </p>
            </div>
          </div>
          {liveButton}
        </div>
        <ScripturePopover
          open={popover !== null}
          title={popover?.title ?? ''}
          color={popover?.color ?? 'cited'}
          explanation={popover?.explanation}
          sourcePassage={popover?.sourcePassage}
          displayPassage={popover?.displayPassage}
          onClose={() => setPopover(null)}
        />
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
                    <p className={`text-lg leading-snug transition-all duration-500 ${
                      s.pendingCompletion
                        ? 'text-blue-300/50 italic'
                        : flashingId === s.id
                          ? 'text-blue-100'
                          : 'text-blue-300'
                    }`}>{s.english}</p>
                  </div>
                  {renderVerseChips(s)}
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
        <ScripturePopover
          open={popover !== null}
          title={popover?.title ?? ''}
          color={popover?.color ?? 'cited'}
          explanation={popover?.explanation}
          sourcePassage={popover?.sourcePassage}
          displayPassage={popover?.displayPassage}
          onClose={() => setPopover(null)}
        />
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
                  <p className={`text-3xl font-semibold leading-snug transition-all duration-[600ms] ${
                    s.pendingCompletion
                      ? 'text-white/40 italic'
                      : flashingId === s.id
                        ? 'text-blue-200'
                        : ''
                  }`}>
                    {s.english}
                  </p>
                </div>
                {renderVerseChips(s)}
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
      <ScripturePopover
        open={popover !== null}
        title={popover?.title ?? ''}
        color={popover?.color ?? 'cited'}
        explanation={popover?.explanation}
        sourcePassage={popover?.sourcePassage}
        displayPassage={popover?.displayPassage}
        onClose={() => setPopover(null)}
      />
    </div>
  );
}

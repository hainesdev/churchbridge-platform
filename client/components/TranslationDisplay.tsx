'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { ScripturePopover } from './ScripturePopover';
import { useTranslationFeed, type VerseDetection, type VerseSuggestion } from '@/lib/useTranslationFeed';

interface TranslationDisplayProps {
  churchId: string;
  mode?: 'full' | 'lowerthird' | 'spanish' | 'bilingual';
}

export function TranslationDisplay({ churchId, mode = 'full' }: TranslationDisplayProps) {
  const {
    segments,
    spanishLines,
    partialSpanish,
    liveEnglish,
    connected,
    flashingId,
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
  }, [segments, partialSpanish, spanishLines]);

  const scrollToLatest = useCallback(() => {
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
      onClick={scrollToLatest}
      className="absolute bottom-6 right-6 z-20 bg-white/10 hover:bg-white/20 backdrop-blur-sm text-white text-sm px-4 py-2 rounded-full border border-white/20 transition-colors"
    >
      Latest
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

  const liveDock = liveEnglish ? (
    <div className="flex-none border-t border-gray-800 bg-gray-950/95 px-6 py-4 backdrop-blur">
      <p className="text-[11px] uppercase tracking-[0.24em] text-gray-500">Live Translation</p>
      <p className="mt-2 text-2xl font-semibold leading-snug text-white">
        {liveEnglish}
        <span className="animate-pulse text-blue-400 ml-1">▌</span>
      </p>
    </div>
  ) : (
    <div className="flex-none border-t border-gray-900 bg-gray-950/80 px-6 py-3">
      <p className="text-sm text-gray-500">Waiting for live translation...</p>
    </div>
  );

  if (mode === 'lowerthird') {
    return (
      <div className="fixed bottom-0 left-0 right-0 p-6 space-y-2">
        <AnimatePresence mode="popLayout">
          {segments.slice(-2).map((segment) => (
            <motion.div
              key={segment.id}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1, transition: { duration: 0.3 } }}
              exit={{ opacity: 0, transition: { duration: 0.2 } }}
              className={`bg-black/70 px-4 py-2 rounded text-2xl font-medium transition-opacity duration-300 ${
                segment.pendingCompletion ? 'text-white/50 italic' : 'text-white'
              }`}
            >
              {segment.english}
            </motion.div>
          ))}
        </AnimatePresence>
        {liveEnglish && (
          <div className="bg-black/70 px-4 py-2 rounded text-white/80 text-2xl italic">
            {liveEnglish}
            <span className="animate-pulse ml-1">▌</span>
          </div>
        )}
      </div>
    );
  }

  if (mode === 'spanish') {
    return (
      <div className="h-full flex bg-black text-white overflow-hidden relative">
        <div className="flex-1 flex flex-col overflow-hidden">
          {statusBar('En vivo')}
          <div ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto">
            <div className="min-h-full flex flex-col justify-end px-10 pt-20 pb-8 gap-4">
              {segments.map((segment) => (
                <motion.div
                  key={segment.id}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1, transition: { duration: 0.4 } }}
                  className="space-y-2"
                >
                  <span className="block text-4xl font-semibold leading-snug">{segment.spanish}</span>
                  {renderVerseChips(segment)}
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

  if (mode === 'bilingual') {
    return (
      <div className="h-full flex bg-black text-white overflow-hidden relative">
        <div className="flex-1 flex flex-col overflow-hidden">
          {statusBar('Live')}
          <div ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto">
            <div className="min-h-full flex flex-col justify-end px-10 pt-20 pb-8 gap-5">
              {segments.map((segment) => (
                <motion.div
                  key={segment.id}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1, transition: { duration: 0.4 } }}
                  className="space-y-1"
                >
                  <p className="text-3xl font-semibold leading-snug">{segment.spanish}</p>
                  <p className={`text-lg leading-snug transition-all duration-500 ${
                    segment.pendingCompletion
                      ? 'text-blue-300/50 italic'
                      : flashingId === segment.id
                        ? 'text-blue-100'
                        : 'text-blue-300'
                  }`}>
                    {segment.english}
                  </p>
                  {renderVerseChips(segment)}
                </motion.div>
              ))}
            </div>
          </div>
          {liveDock}
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

  return (
    <div className="h-full flex bg-black text-white overflow-hidden relative">
      <div className="flex-1 flex flex-col overflow-hidden">
        {statusBar('Live')}
        <div ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto">
          <div className="min-h-full flex flex-col justify-end px-10 pt-20 pb-8 gap-5">
            {segments.map((segment) => (
              <motion.div
                key={segment.id}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1, transition: { duration: 0.4 } }}
                className="space-y-1"
              >
                <p className={`text-3xl font-semibold leading-snug transition-all duration-[600ms] ${
                  segment.pendingCompletion
                    ? 'text-white/40 italic'
                    : flashingId === segment.id
                      ? 'text-blue-200'
                      : ''
                }`}>
                  {segment.english}
                </p>
                {renderVerseChips(segment)}
                <p className="text-base text-gray-500 leading-snug">{segment.spanish}</p>
              </motion.div>
            ))}
          </div>
        </div>
        {liveDock}
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

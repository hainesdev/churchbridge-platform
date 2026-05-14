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
    liveSource,
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
  const [revealedPhraseBySegment, setRevealedPhraseBySegment] = useState<Record<number, string | null>>({});
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
  const latestCommittedSpanish = segments.at(-1)?.spanish ?? '';
  const draftSpanish = partialSpanish || latestCommittedSpanish;

  const draftSourceLabel = (() => {
    switch (liveSource) {
      case 'google_interim':
        return 'Draft · interim';
      case 'google_fragment':
        return 'Draft · fragment';
      case 'google_sentence':
        return 'Draft · sentence';
      case 'llm':
        return 'Draft · refined';
      case 'stt_passthrough':
        return 'Draft · passthrough';
      default:
        return 'Draft';
    }
  })();

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

  const togglePhraseReveal = useCallback((segmentId: number, phraseId: string) => {
    setRevealedPhraseBySegment(prev => ({
      ...prev,
      [segmentId]: prev[segmentId] === phraseId ? null : phraseId,
    }));
  }, []);

  const renderAlignedEnglish = useCallback((segment: typeof segments[number], tone: string) => {
    const phraseAlignment = segment.phraseAlignment ?? [];
    if (phraseAlignment.length === 0) {
      return (
        <p className={tone}>
          {segment.english}
        </p>
      );
    }

    const revealedId = revealedPhraseBySegment[segment.id] ?? null;
    const revealedPhrase = phraseAlignment.find((phrase) => `${phrase.english_text}|${phrase.spanish_text}` === revealedId);

    return (
      <div className="space-y-3">
        <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-gray-500">
          Tap English to reveal the Spanish phrase
        </p>
        <div className="flex flex-wrap gap-2">
          {phraseAlignment.map((phrase) => {
            const phraseId = `${phrase.english_text}|${phrase.spanish_text}`;
            const active = revealedId === phraseId;
            return (
              <button
                key={phraseId}
                onClick={() => togglePhraseReveal(segment.id, phraseId)}
                className={`rounded-2xl border px-3 py-2 text-left text-sm font-semibold transition-colors ${
                  active
                    ? 'border-blue-200 bg-blue-100 text-black'
                    : 'border-white/10 bg-white/5 text-white hover:bg-white/10'
                }`}
              >
                {phrase.english_text}
              </button>
            );
          })}
        </div>
        {revealedPhrase ? (
          <div className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3">
            <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-gray-500">Spanish</p>
            <p className="mt-2 text-base leading-snug text-gray-300">{revealedPhrase.spanish_text}</p>
          </div>
        ) : (
          <p className="text-sm text-gray-500">Spanish stays hidden until you tap a phrase.</p>
        )}
      </div>
    );
  }, [revealedPhraseBySegment, togglePhraseReveal]);

  const draftPanel = (
    <div className="flex-none border-b border-sky-300/10 bg-gradient-to-b from-sky-300/10 via-sky-300/5 to-transparent px-6 py-5 backdrop-blur-sm">
      <div className="flex items-center justify-between gap-4">
        <div>
          <p className="text-[11px] uppercase tracking-[0.24em] text-sky-200/60">{draftSourceLabel}</p>
          <p className="mt-1 text-[11px] uppercase tracking-[0.18em] text-stone-500">Updates as speech comes in</p>
        </div>
        <div className="rounded-full border border-sky-300/15 bg-sky-300/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-sky-100/80">
          {liveEnglish ? 'Active' : partialSpanish ? 'Listening' : 'Standby'}
        </div>
      </div>
      <div className="mt-4 space-y-3">
        <p className={`min-h-[3.5rem] text-3xl font-semibold leading-tight tracking-[-0.02em] ${
          liveEnglish ? 'text-white' : 'text-white/35'
        }`}>
          {liveEnglish || 'Waiting for draft translation...'}
          {(liveEnglish || partialSpanish) && <span className="animate-pulse text-sky-300 ml-1">▌</span>}
        </p>
        <p className="min-h-[1.5rem] text-sm leading-relaxed text-stone-400">
          {draftSpanish || 'Spanish source will appear here while the draft builds.'}
        </p>
      </div>
    </div>
  );

  const committedLabel = (
    <div className="flex items-center justify-between gap-4 pt-6">
      <div>
        <p className="text-[11px] uppercase tracking-[0.24em] text-stone-500">Confirmed</p>
        <p className="mt-1 text-[11px] uppercase tracking-[0.18em] text-stone-600">Stable captions after buffering and correction</p>
      </div>
      <div className="rounded-full border border-white/10 bg-white/[0.03] px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-stone-300">
        {segments.length} committed
      </div>
    </div>
  );

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
        {false ? liveDock : null}
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
          {draftPanel}
          <div ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto">
            <div className="min-h-full flex flex-col justify-end px-10 pb-8 gap-5">
              {committedLabel}
              {segments.map((segment) => (
                <motion.div
                  key={segment.id}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1, transition: { duration: 0.4 } }}
                  className="space-y-1"
                >
                  <p className="text-3xl font-semibold leading-snug">{segment.spanish}</p>
                  {renderAlignedEnglish(
                    segment,
                    `text-lg leading-snug transition-all duration-500 ${
                      segment.pendingCompletion
                        ? 'text-blue-300/50 italic'
                        : flashingId === segment.id
                          ? 'text-blue-100'
                          : 'text-blue-300'
                    }`,
                  )}
                  {renderVerseChips(segment)}
                </motion.div>
              ))}
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

  return (
    <div className="h-full flex bg-black text-white overflow-hidden relative">
      <div className="flex-1 flex flex-col overflow-hidden">
        {statusBar('Live')}
        {draftPanel}
        <div ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto">
          <div className="min-h-full flex flex-col justify-end px-10 pb-8 gap-5">
            {committedLabel}
            {segments.map((segment) => (
              <motion.div
                key={segment.id}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1, transition: { duration: 0.4 } }}
                className="space-y-1"
              >
                {renderAlignedEnglish(
                  segment,
                  `text-3xl font-semibold leading-snug transition-all duration-[600ms] ${
                    segment.pendingCompletion
                      ? 'text-white/40 italic'
                      : flashingId === segment.id
                        ? 'text-blue-200'
                        : ''
                  }`,
                )}
                {renderVerseChips(segment)}
                {(!segment.phraseAlignment || segment.phraseAlignment.length === 0) && (
                  <p className="text-base text-gray-500 leading-snug">{segment.spanish}</p>
                )}
              </motion.div>
            ))}
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

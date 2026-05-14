'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { ScripturePopover } from './ScripturePopover';
import {
  useTranslationFeed,
  type VerseDetection,
  type VerseSuggestion,
} from '@/lib/useTranslationFeed';

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
  const [hoveredPhraseBySegment, setHoveredPhraseBySegment] = useState<Record<number, string | null>>({});
  const [lockedPhraseBySegment, setLockedPhraseBySegment] = useState<Record<number, string | null>>({});
  const [selectedSegmentId, setSelectedSegmentId] = useState<number | null>(null);
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

  const setHoveredPhrase = useCallback((segmentId: number, phraseId: string | null) => {
    setHoveredPhraseBySegment(prev => ({
      ...prev,
      [segmentId]: phraseId,
    }));
  }, []);

  const toggleLockedPhrase = useCallback((segmentId: number, phraseId: string) => {
    setLockedPhraseBySegment(prev => ({
      ...prev,
      [segmentId]: prev[segmentId] === phraseId ? null : phraseId,
    }));
    setSelectedSegmentId(segmentId);
  }, []);

  const resolvePhraseId = useCallback((segmentId: number, phraseId: string | null | undefined) => {
    if (!phraseId) return null;
    const segment = segments.find((entry) => entry.id === segmentId);
    if (!segment) return phraseId;
    const phraseAlignment = segment.phraseAlignment ?? [];
    const currentIds = new Set(
      phraseAlignment.flatMap((phrase) => (
        phrase.chunk_id && phrase.chunk_id.trim() ? [phrase.chunk_id.trim()] : []
      )),
    );
    if (currentIds.has(phraseId)) return phraseId;
    const safeDescendants = phraseAlignment.filter((phrase) => {
      if (!phrase.chunk_id || !phrase.derived_from_chunk_ids?.includes(phraseId)) {
        return false;
      }
      const ambiguityReason = phrase.ambiguity_reason?.trim() ?? null;
      return ambiguityReason === null || ambiguityReason === 'adjacent_merge';
    });
    if (safeDescendants.length === 1) {
      return safeDescendants[0].chunk_id ?? phraseId;
    }
    return phraseId;
  }, [segments]);

  const activePhraseId = useCallback((segmentId: number) => {
    return (
      resolvePhraseId(segmentId, lockedPhraseBySegment[segmentId])
      ?? resolvePhraseId(segmentId, hoveredPhraseBySegment[segmentId])
      ?? null
    );
  }, [hoveredPhraseBySegment, lockedPhraseBySegment, resolvePhraseId]);

  const escapeRegExp = useCallback((value: string) => (
    value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  ), []);

  const buildFlexiblePattern = useCallback((phrase: string) => {
    const tokens = phrase.match(/[A-Za-z0-9']+|[^A-Za-z0-9'\s]+/g) ?? [];
    if (tokens.length === 0) return null;
    return tokens
      .map((token) => (
        /^[A-Za-z0-9']+$/.test(token)
          ? escapeRegExp(token)
          : escapeRegExp(token).replace(/\\\./g, '.?')
      ))
      .join(`[\\s\\u00A0,.;:!?'"“”‘’()\\-–—]*`);
  }, [escapeRegExp]);

  const buildAlignedRuns = useCallback((text: string, phrases: Array<{ id: string; text: string }>) => {
    const runs: Array<{ text: string; id?: string }> = [];
    let cursor = 0;

    for (const phrase of phrases) {
      const directIndex = text.toLowerCase().indexOf(phrase.text.toLowerCase(), cursor);
      let index = directIndex;
      let matchLength = phrase.text.length;

      if (index < 0) {
        const pattern = buildFlexiblePattern(phrase.text);
        if (!pattern) {
          return null;
        }
        const matcher = new RegExp(pattern, 'i');
        const searchSlice = text.slice(cursor);
        const matched = matcher.exec(searchSlice);
        if (!matched || matched.index === undefined) {
          return null;
        }
        index = cursor + matched.index;
        matchLength = matched[0].length;
      }

      if (index < 0) {
        return null;
      }
      if (index > cursor) {
        runs.push({ text: text.slice(cursor, index) });
      }
      runs.push({
        text: text.slice(index, index + matchLength),
        id: phrase.id,
      });
      cursor = index + matchLength;
    }

    if (cursor < text.length) {
      runs.push({ text: text.slice(cursor) });
    }

    return runs;
  }, [buildFlexiblePattern]);

  const renderInteractiveLine = useCallback((
    text: string,
    segmentId: number,
    lineId: string,
    lineClass: string,
    idleInteractiveClass: string,
    activeInteractiveClass: string,
    lockedInteractiveClass: string,
  ) => {
    const active = activePhraseId(segmentId) === lineId;
    const locked = resolvePhraseId(segmentId, lockedPhraseBySegment[segmentId]) === lineId;
    return (
      <button
        type="button"
        aria-pressed={locked}
        onMouseEnter={() => setHoveredPhrase(segmentId, lineId)}
        onMouseLeave={() => setHoveredPhrase(segmentId, null)}
        onClick={() => toggleLockedPhrase(segmentId, lineId)}
        className={`${lineClass} rounded-2xl px-2 py-1 text-left transition-colors ${
          locked ? lockedInteractiveClass : active ? activeInteractiveClass : idleInteractiveClass
        }`}
      >
        {text}
      </button>
    );
  }, [activePhraseId, lockedPhraseBySegment, resolvePhraseId, setHoveredPhrase, toggleLockedPhrase]);

  const buildAlignedRunsFromSpans = useCallback((
    text: string,
    phrases: Array<{ id: string; span?: { start: number; end: number } | null }>,
  ) => {
    const sorted = phrases
      .flatMap((phrase) => {
        if (!phrase.span) return [];
        const { start, end } = phrase.span;
        if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start || end > text.length) {
          return [];
        }
        return [{ ...phrase, span: { start, end } }];
      })
      .sort((left, right) => left.span.start - right.span.start);

    if (sorted.length !== phrases.length) {
      return null;
    }

    const runs: Array<{ text: string; id?: string }> = [];
    let cursor = 0;
    for (const phrase of sorted) {
      if (!phrase.span || phrase.span.start < cursor) {
        return null;
      }
      if (phrase.span.start > cursor) {
        runs.push({ text: text.slice(cursor, phrase.span.start) });
      }
      runs.push({
        text: text.slice(phrase.span.start, phrase.span.end),
        id: phrase.id,
      });
      cursor = phrase.span.end;
    }
    if (cursor < text.length) {
      runs.push({ text: text.slice(cursor) });
    }
    return runs;
  }, []);

  const renderAlignedLine = useCallback((
    text: string,
    phrases: Array<{ id: string; text: string; span?: { start: number; end: number } | null }>,
    segmentId: number,
    lineClass: string,
    idleInteractiveClass: string,
    activeInteractiveClass: string,
    lockedInteractiveClass: string,
  ) => {
    const runs = buildAlignedRunsFromSpans(text, phrases) ?? buildAlignedRuns(text, phrases);
    if (!runs) {
      return null;
    }

    return (
      <div className={lineClass}>
        {runs.map((run, index) => {
          if (!run.id) {
            return <span key={`${segmentId}-text-${index}`} className="whitespace-pre-wrap">{run.text}</span>;
          }
          const active = activePhraseId(segmentId) === run.id;
          const locked = resolvePhraseId(segmentId, lockedPhraseBySegment[segmentId]) === run.id;
          return (
            <button
              key={run.id}
              type="button"
              data-chunk-id={run.id}
              aria-pressed={locked}
              onMouseEnter={() => setHoveredPhrase(segmentId, run.id ?? null)}
              onMouseLeave={() => setHoveredPhrase(segmentId, null)}
              onClick={() => toggleLockedPhrase(segmentId, run.id!)}
              className={`rounded-xl px-1 py-0.5 whitespace-pre-wrap transition-colors ${
                locked ? lockedInteractiveClass : active ? activeInteractiveClass : idleInteractiveClass
              }`}
            >
              {run.text}
            </button>
          );
        })}
      </div>
    );
  }, [activePhraseId, buildAlignedRuns, buildAlignedRunsFromSpans, lockedPhraseBySegment, resolvePhraseId, setHoveredPhrase, toggleLockedPhrase]);

  const renderLinkedPair = useCallback((segment: typeof segments[number], variant: 'full' | 'bilingual') => {
    const phraseAlignment = segment.phraseAlignment ?? [];
    const segmentSelected = selectedSegmentId === null || selectedSegmentId === segment.id;
    const cardClass = variant === 'full'
      ? `rounded-[28px] border px-5 py-4 transition-colors ${
          segmentSelected
            ? 'border-white/10 bg-white/[0.03]'
            : 'border-white/5 bg-white/[0.015]'
        }`
      : `rounded-[28px] border px-5 py-4 transition-colors ${
          segmentSelected
            ? 'border-sky-300/20 bg-sky-300/[0.05]'
            : 'border-white/5 bg-white/[0.015]'
        }`;
    const englishLineClass = variant === 'full'
      ? `text-[2rem] font-semibold leading-tight tracking-[-0.02em] transition-all duration-[600ms] ${
          segment.pendingCompletion
            ? 'text-white/40 italic'
            : flashingId === segment.id
              ? 'text-blue-200'
              : 'text-white'
        }`
      : `text-2xl font-semibold leading-tight tracking-[-0.015em] transition-all duration-500 ${
          segment.pendingCompletion
            ? 'text-white/45 italic'
            : flashingId === segment.id
              ? 'text-white'
              : 'text-white/95'
        }`;
    const spanishLineClass = variant === 'full'
      ? 'text-sm leading-relaxed text-stone-400'
      : 'text-lg leading-relaxed text-sky-100/80';
    const englishIdleClass = variant === 'full' ? 'hover:bg-white/8' : 'hover:bg-sky-200/10';
    const englishActiveClass = variant === 'full' ? 'bg-amber-200 text-black' : 'bg-emerald-200 text-slate-950';
    const englishLockedClass = variant === 'full' ? 'bg-amber-300 text-black ring-2 ring-amber-100/60' : 'bg-emerald-300 text-slate-950 ring-2 ring-emerald-100/70';
    const spanishIdleClass = variant === 'full' ? 'hover:bg-white/5' : 'hover:bg-sky-200/8';
    const spanishActiveClass = variant === 'full' ? 'bg-amber-200 text-black' : 'bg-emerald-200 text-slate-950';
    const spanishLockedClass = variant === 'full' ? 'bg-amber-300 text-black ring-2 ring-amber-100/60' : 'bg-emerald-300 text-slate-950 ring-2 ring-emerald-100/70';
    const linePairId = '__line__';

    if (phraseAlignment.length === 0) {
      return (
        <div className={`${cardClass} space-y-2`}>
          <p className={`text-[11px] font-semibold uppercase tracking-[0.24em] ${
            variant === 'full' ? 'text-stone-500' : 'text-sky-200/55'
          }`}>
            {variant === 'full' ? 'English First' : 'Linked Pair'}
          </p>
          {renderInteractiveLine(
            segment.english,
            segment.id,
            linePairId,
            englishLineClass,
            englishIdleClass,
            englishActiveClass,
            englishLockedClass,
          )}
          {renderInteractiveLine(
            segment.spanish,
            segment.id,
            linePairId,
            spanishLineClass,
            spanishIdleClass,
            spanishActiveClass,
            spanishLockedClass,
          )}
        </div>
      );
    }

    const normalizedPhrases = phraseAlignment.map((phrase) => ({
      id: phrase.chunk_id?.trim() || `${phrase.english_text}|${phrase.spanish_text}`,
      english: phrase.english_text,
      spanish: phrase.spanish_text,
      englishSpan: phrase.english_span,
      spanishSpan: phrase.spanish_span,
    }));
    const englishLine = renderAlignedLine(
      segment.english,
      normalizedPhrases.map((phrase) => ({ id: phrase.id, text: phrase.english, span: phrase.englishSpan })),
      segment.id,
      englishLineClass,
      englishIdleClass,
      englishActiveClass,
      englishLockedClass,
    );
    const spanishLine = renderAlignedLine(
      segment.spanish,
      normalizedPhrases.map((phrase) => ({ id: phrase.id, text: phrase.spanish, span: phrase.spanishSpan })),
      segment.id,
      spanishLineClass,
      spanishIdleClass,
      spanishActiveClass,
      spanishLockedClass,
    );

    if (!englishLine || !spanishLine) {
      return (
        <div className={`${cardClass} space-y-2`}>
          <p className={`text-[11px] font-semibold uppercase tracking-[0.24em] ${
            variant === 'full' ? 'text-stone-500' : 'text-sky-200/55'
          }`}>
            Whole-Line Pair
          </p>
          {renderInteractiveLine(
            segment.english,
            segment.id,
            linePairId,
            englishLineClass,
            englishIdleClass,
            englishActiveClass,
            englishLockedClass,
          )}
          {renderInteractiveLine(
            segment.spanish,
            segment.id,
            linePairId,
            spanishLineClass,
            spanishIdleClass,
            spanishActiveClass,
            spanishLockedClass,
          )}
        </div>
      );
    }

    return (
      <div className={`${cardClass} space-y-2`}>
        <p className={`text-[11px] font-semibold uppercase tracking-[0.24em] ${
          variant === 'full' ? 'text-stone-500' : 'text-sky-200/55'
        }`}>
          {variant === 'full' ? 'Linked Pair' : 'Phrase Study'}
        </p>
        {englishLine}
        {spanishLine}
      </div>
    );
  }, [flashingId, renderAlignedLine, renderInteractiveLine, selectedSegmentId]);

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
                  className="space-y-2"
                >
                  {renderLinkedPair(segment, 'bilingual')}
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
                className="space-y-2"
              >
                {renderLinkedPair(segment, 'full')}
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

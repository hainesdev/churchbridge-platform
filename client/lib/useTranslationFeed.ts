'use client';
import { useEffect, useRef, useState } from 'react';
import { getWebSocketBaseUrl } from '@/lib/wsBaseUrl';
import { attachVerseToVisibleSegment, resolveMergedSegmentId } from '@/lib/mergedVerseRouting';
import {
  createInitialFeedDebugState,
  type BrowserFeedDebugState,
} from '@/lib/browserDiagnostics';

export interface VerseDetection {
  book: string;
  chapter: number;
  verse_start: number;
  verse_end: number | null;
  spanish_text: string;
  canonical_english: string;
  reference: string;
  confidence: 'explicit' | 'quoted';
  explanation?: string;
  source_version_slug?: string;
  display_version_slug?: string;
  source_passage?: ScripturePassage | null;
  display_passage?: ScripturePassage | null;
}

export interface VerseSuggestion {
  reference: string;
  canonical_english: string;
  relevance_note: string;
  explanation?: string;
  source_version_slug?: string;
  display_version_slug?: string;
  source_passage?: ScripturePassage | null;
  display_passage?: ScripturePassage | null;
}

export interface PhraseAlignment {
  chunk_id?: string;
  english_text: string;
  spanish_text: string;
  english_span?: { start: number; end: number } | null;
  spanish_span?: { start: number; end: number } | null;
  ordinal?: number;
  derived_from_chunk_ids?: string[];
  remap_decision?: string;
  ambiguity_reason?: string | null;
}

export interface ScripturePassageVerse {
  verse: number;
  text: string;
  reference: string;
}

export interface ScripturePassage {
  version: { slug: string; name: string };
  book: string;
  chapter: number;
  verse_start: number;
  verse_end: number | null;
  reference: string;
  verses: ScripturePassageVerse[];
}

export type SermonMode =
  | 'scripture' | 'exposition' | 'illustration'
  | 'application' | 'exhortation' | 'procedural';

export type TranslationRegister = 'scripture' | 'expository' | 'narrative' | 'exhortation';

export interface Segment {
  id: number;
  spanish: string;
  english: string;
  phraseAlignment?: PhraseAlignment[];
  alignmentVersion?: number;
  previousAlignmentVersion?: number | null;
  rootSegmentId?: number;
  mergedFromSegmentIds?: number[];
  verseDetected?: VerseDetection;
  verseSuggestions?: VerseSuggestion[];
  register?: TranslationRegister;
  paragraphBreak?: boolean;
  sourceQuality?: 'clean' | 'noisy' | 'fragmented';
  pendingCompletion?: boolean;
  terminalIncomplete?: boolean;
}

const FLASH_MS = 600;

export interface TranslationFeed {
  segments: Segment[];
  spanishLines: string[];
  partialSpanish: string;
  liveEnglish: string;
  liveStableEnglish: string;
  liveDraftEnglish: string;
  liveSource: string;
  liveSegmentId: number | null;
  liveUpdatedAt: number | null;
  connected: boolean;
  flashingId: number | null;
  verses: VerseDetection[];
  suggestions: VerseSuggestion[];
  activeVerseTs: number | null;
  sermonMode: SermonMode;
  lastInterimAt: number | null;
  lastFinalAt: number | null;
  lastTranslationAt: number | null;
  lastInterimSpanish: string;
  lastFinalSpanish: string;
  lastCommittedEnglish: string;
  debug: BrowserFeedDebugState;
}

type TranslationFeedTestHarness = {
  subscribe: (listener: (message: Record<string, unknown>) => void) => () => void;
};

function messageSegmentId(msg: Record<string, unknown>): number | null {
  const raw = msg.segment_id ?? msg.ts;
  if (raw === undefined || raw === null) return null;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

function messageMergeRef(msg: Record<string, unknown>): { keep: number | null; absorb: number | null } {
  const keepRaw = msg.segment_id_keep ?? msg.ts_keep;
  const absorbRaw = msg.segment_id_absorb ?? msg.ts_absorb;
  const keep = keepRaw === undefined || keepRaw === null ? null : Number(keepRaw);
  const absorb = absorbRaw === undefined || absorbRaw === null ? null : Number(absorbRaw);
  return {
    keep: Number.isFinite(keep) ? keep : null,
    absorb: Number.isFinite(absorb) ? absorb : null,
  };
}

function messagePhraseAlignment(msg: Record<string, unknown>): PhraseAlignment[] | undefined {
  const raw = msg.phrase_alignment;
  if (!Array.isArray(raw)) return undefined;
  const alignment = raw.flatMap((item) => {
    if (!item || typeof item !== 'object') return [];
    const english = typeof item.english_text === 'string' ? item.english_text.trim() : '';
    const spanish = typeof item.spanish_text === 'string' ? item.spanish_text.trim() : '';
    if (!english || !spanish) return [];
    const chunkId = typeof item.chunk_id === 'string' ? item.chunk_id.trim() : '';
    const englishSpan = (
      item.english_span
      && typeof item.english_span === 'object'
      && typeof (item.english_span as Record<string, unknown>).start === 'number'
      && typeof (item.english_span as Record<string, unknown>).end === 'number'
    )
      ? {
          start: Number((item.english_span as Record<string, unknown>).start),
          end: Number((item.english_span as Record<string, unknown>).end),
        }
      : null;
    const spanishSpan = (
      item.spanish_span
      && typeof item.spanish_span === 'object'
      && typeof (item.spanish_span as Record<string, unknown>).start === 'number'
      && typeof (item.spanish_span as Record<string, unknown>).end === 'number'
    )
      ? {
          start: Number((item.spanish_span as Record<string, unknown>).start),
          end: Number((item.spanish_span as Record<string, unknown>).end),
        }
      : null;
    const ordinal = typeof item.ordinal === 'number' ? Number(item.ordinal) : undefined;
    const derivedFromChunkIds = Array.isArray(item.derived_from_chunk_ids)
      ? item.derived_from_chunk_ids.flatMap((candidate: unknown) => (
          typeof candidate === 'string' && candidate.trim() ? [candidate.trim()] : []
        ))
      : undefined;
    const remapDecision = typeof item.remap_decision === 'string' ? item.remap_decision.trim() : undefined;
    const ambiguityReason = item.ambiguity_reason === null
      ? null
      : typeof item.ambiguity_reason === 'string'
        ? item.ambiguity_reason.trim()
        : undefined;
    return [{
      chunk_id: chunkId || undefined,
      english_text: english,
      spanish_text: spanish,
      english_span: englishSpan,
      spanish_span: spanishSpan,
      ordinal,
      derived_from_chunk_ids: derivedFromChunkIds,
      remap_decision: remapDecision || undefined,
      ambiguity_reason: ambiguityReason,
    }];
  });
  return alignment;
}

function messageAlignmentVersion(msg: Record<string, unknown>): number | undefined {
  const raw = msg.alignment_version;
  if (raw === undefined || raw === null) return undefined;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function messagePreviousAlignmentVersion(msg: Record<string, unknown>): number | null | undefined {
  const raw = msg.previous_alignment_version;
  if (raw === undefined) return undefined;
  if (raw === null) return null;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

function messageRootSegmentId(msg: Record<string, unknown>): number | undefined {
  const raw = msg.root_segment_id;
  if (raw === undefined || raw === null) return undefined;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function messageMergedFromSegmentIds(msg: Record<string, unknown>): number[] | undefined {
  const raw = msg.merged_from_segment_ids;
  if (!Array.isArray(raw)) return undefined;
  const values = raw.flatMap((item) => {
    const parsed = Number(item);
    return Number.isFinite(parsed) ? [parsed] : [];
  });
  return values.length > 0 ? values : undefined;
}

function defaultLiveMergeStrategy(source: string): 'append' | 'replace' {
  switch (source) {
    case 'google_sentence':
    case 'google_correction':
    case 'google_interim':
    case 'llm':
      return 'replace';
    default:
      return 'append';
  }
}

function mergeLiveEnglish(current: string, incoming: string): string {
  const currentTrimmed = current.trim();
  const incomingTrimmed = incoming.trim();
  if (!incomingTrimmed) return currentTrimmed;
  if (!currentTrimmed) return incomingTrimmed;
  if (incomingTrimmed.startsWith(currentTrimmed)) return incomingTrimmed;
  if (currentTrimmed.startsWith(incomingTrimmed) || currentTrimmed.includes(incomingTrimmed)) {
    return currentTrimmed;
  }
  return `${currentTrimmed} ${incomingTrimmed}`;
}

function messageLiveTextPart(
  msg: Record<string, unknown>,
  key: 'stable_text' | 'draft_text',
): string | undefined {
  const raw = msg[key];
  if (typeof raw !== 'string') return undefined;
  return raw.trim();
}

function combineLiveEnglish(stable: string, draft: string, fallback: string): string {
  const stableTrimmed = stable.trim();
  const draftTrimmed = draft.trim();
  if (stableTrimmed && draftTrimmed) {
    return `${stableTrimmed} ${draftTrimmed}`;
  }
  if (stableTrimmed) return stableTrimmed;
  if (draftTrimmed) return draftTrimmed;
  return fallback.trim();
}

export function useTranslationFeed(churchId: string): TranslationFeed {
  const [segments, setSegments] = useState<Segment[]>([]);
  const [spanishLines, setSpanishLines] = useState<string[]>([]);
  const [partialSpanish, setPartialSpanish] = useState('');
  const [liveEnglish, setLiveEnglish] = useState('');
  const [liveStableEnglish, setLiveStableEnglish] = useState('');
  const [liveDraftEnglish, setLiveDraftEnglish] = useState('');
  const [liveSource, setLiveSource] = useState('');
  const [liveSegmentId, setLiveSegmentId] = useState<number | null>(null);
  const [liveUpdatedAt, setLiveUpdatedAt] = useState<number | null>(null);
  const [connected, setConnected] = useState(false);
  const [flashingId, setFlashingId] = useState<number | null>(null);
  const [verses, setVerses] = useState<VerseDetection[]>([]);
  const [suggestions, setSuggestions] = useState<VerseSuggestion[]>([]);
  const [activeVerseTs, setActiveVerseTs] = useState<number | null>(null);
  const [sermonMode, setSermonMode] = useState<SermonMode>('exposition');
  const [lastInterimAt, setLastInterimAt] = useState<number | null>(null);
  const [lastFinalAt, setLastFinalAt] = useState<number | null>(null);
  const [lastTranslationAt, setLastTranslationAt] = useState<number | null>(null);
  const [lastInterimSpanish, setLastInterimSpanish] = useState('');
  const [lastFinalSpanish, setLastFinalSpanish] = useState('');
  const [lastCommittedEnglish, setLastCommittedEnglish] = useState('');
  const [debug, setDebug] = useState<BrowserFeedDebugState>(createInitialFeedDebugState);

  const flashTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mergedIntoRef = useRef<Map<number, number>>(new Map());
  const liveSegmentRef = useRef<number | null>(null);

  useEffect(() => {
    liveSegmentRef.current = liveSegmentId;
  }, [liveSegmentId]);

  useEffect(() => {
    let ws: WebSocket;
    let stopped = false;

    const flashSegment = (segmentId: number) => {
      setFlashingId(segmentId);
      if (flashTimerRef.current) clearTimeout(flashTimerRef.current);
      flashTimerRef.current = setTimeout(() => setFlashingId(null), FLASH_MS);
    };

    const clearLiveIfMatches = (segmentId: number | null) => {
      setLiveEnglish(prev => {
        if (segmentId === null || liveSegmentRef.current === null || segmentId === liveSegmentRef.current) {
          return '';
        }
        return prev;
      });
      setLiveStableEnglish(prev => {
        if (segmentId === null || liveSegmentRef.current === null || segmentId === liveSegmentRef.current) {
          return '';
        }
        return prev;
      });
      setLiveDraftEnglish(prev => {
        if (segmentId === null || liveSegmentRef.current === null || segmentId === liveSegmentRef.current) {
          return '';
        }
        return prev;
      });
      setLiveSource(prev => {
        if (segmentId === null || liveSegmentRef.current === null || segmentId === liveSegmentRef.current) {
          return '';
        }
        return prev;
      });
      setLiveSegmentId(prev => {
        if (segmentId === null || prev === null || segmentId === prev) {
          return null;
        }
        return prev;
      });
      setLiveUpdatedAt(Date.now());
    };

    const handleMessage = (msg: Record<string, unknown>) => {
      setDebug(prev => ({
        ...prev,
        totalEvents: prev.totalEvents + 1,
        lastEventType: String(msg.type ?? 'unknown'),
        lastEventAt: Date.now(),
      }));

      if (msg.type === 'interim') {
        const text = String(msg.text ?? '');
        setPartialSpanish(text);
        setLastInterimSpanish(text);
        setLastInterimAt(Date.now());
        return;
      }

      if (msg.type === 'stt_final') {
        const text = String(msg.text ?? '');
        setSpanishLines(prev => [...prev, text].slice(-8));
        setPartialSpanish('');
        setLastFinalSpanish(text);
        setLastFinalAt(Date.now());
        return;
      }

      if (msg.type === 'live_translation') {
        const text = String(msg.text ?? '').trim();
        const stableText = messageLiveTextPart(msg, 'stable_text');
        const draftText = messageLiveTextPart(msg, 'draft_text');
        const hasSplitPreview = stableText !== undefined || draftText !== undefined;
        if (!text && !hasSplitPreview) return;
        const source = typeof msg.source === 'string' ? msg.source : 'live_translation';
        const mergeStrategy = msg.merge_strategy === 'replace' || msg.merge_strategy === 'append'
          ? msg.merge_strategy
          : defaultLiveMergeStrategy(source);
        if (hasSplitPreview) {
          const nextStable = stableText ?? '';
          const nextDraft = draftText ?? '';
          const combined = combineLiveEnglish(nextStable, nextDraft, text);
          if (!combined) return;
          setLiveEnglish(combined);
          setLiveStableEnglish(nextStable);
          setLiveDraftEnglish(nextDraft);
        } else {
          setLiveEnglish(prev => (
            mergeStrategy === 'replace'
              ? text
              : mergeLiveEnglish(prev, text)
          ));
          setLiveStableEnglish(prev => (
            mergeStrategy === 'replace'
              ? text
              : mergeLiveEnglish(prev, text)
          ));
          setLiveDraftEnglish('');
        }
        setLiveSource(source);
        setLiveSegmentId(messageSegmentId(msg));
        setLiveUpdatedAt(Date.now());
        return;
      }

      if (msg.type === 'live_translation_clear') {
        clearLiveIfMatches(messageSegmentId(msg));
        return;
      }

      if (msg.type === 'feed_commit') {
        const segmentId = messageSegmentId(msg);
        if (segmentId === null) {
          console.warn('[useTranslationFeed] feed_commit missing segment_id:', msg);
          return;
        }
        const english = String(msg.english ?? '');
        const spanish = String(msg.spanish ?? '');
        const phraseAlignment = messagePhraseAlignment(msg) ?? [];
        const alignmentVersion = messageAlignmentVersion(msg);
        const previousAlignmentVersion = messagePreviousAlignmentVersion(msg);
        const rootSegmentId = messageRootSegmentId(msg) ?? segmentId;
        const mergedFromSegmentIds = messageMergedFromSegmentIds(msg) ?? [segmentId];
        setSegments(prev => {
          const existing = prev.some(segment => segment.id === segmentId);
          if (existing) {
            return prev.map(segment =>
              segment.id === segmentId
                ? {
                    ...segment,
                    english,
                    spanish,
                    phraseAlignment,
                    alignmentVersion,
                    previousAlignmentVersion,
                    rootSegmentId,
                    mergedFromSegmentIds,
                    pendingCompletion: false,
                  }
                : segment,
            );
          }
          return [...prev.slice(-99), {
            id: segmentId,
            english,
            spanish,
            phraseAlignment,
            alignmentVersion,
            previousAlignmentVersion,
            rootSegmentId,
            mergedFromSegmentIds,
          }];
        });
        clearLiveIfMatches(segmentId);
        setLastCommittedEnglish(english);
        setLastTranslationAt(Date.now());
        return;
      }

      if (msg.type === 'feed_revision') {
        const segmentId = messageSegmentId(msg);
        if (segmentId === null) {
          console.warn('[useTranslationFeed] feed_revision missing segment_id:', msg);
          return;
        }
        const english = String(msg.english ?? '');
        const spanish = msg.spanish === undefined ? null : String(msg.spanish ?? '');
        const phraseAlignment = messagePhraseAlignment(msg);
        const alignmentVersion = messageAlignmentVersion(msg);
        const previousAlignmentVersion = messagePreviousAlignmentVersion(msg);
        const rootSegmentId = messageRootSegmentId(msg) ?? segmentId;
        const mergedFromSegmentIds = messageMergedFromSegmentIds(msg) ?? [segmentId];
        let found = false;
        setSegments(prev => {
          const updated = prev.map(segment => {
            if (segment.id !== segmentId) return segment;
            found = true;
            return {
              ...segment,
              english,
              spanish: spanish ?? segment.spanish,
              phraseAlignment: phraseAlignment ?? segment.phraseAlignment,
              alignmentVersion: alignmentVersion ?? segment.alignmentVersion,
              previousAlignmentVersion: previousAlignmentVersion ?? segment.previousAlignmentVersion,
              rootSegmentId,
              mergedFromSegmentIds,
              pendingCompletion: false,
            };
          });
          if (found) return updated;
          return [...updated.slice(-99), {
            id: segmentId,
            english,
            spanish: spanish ?? '',
            phraseAlignment: phraseAlignment ?? [],
            alignmentVersion,
            previousAlignmentVersion,
            rootSegmentId,
            mergedFromSegmentIds,
            pendingCompletion: false,
          }];
        });
        setLastCommittedEnglish(english);
        setLastTranslationAt(Date.now());
        flashSegment(segmentId);
        return;
      }

      if (msg.type === 'verse_detected') {
        const segmentId = messageSegmentId(msg);
        if (segmentId === null) return;
        const verse = msg.verse as VerseDetection;
        const targetSegmentId = resolveMergedSegmentId(mergedIntoRef.current, segmentId);
        setSegments(prev => attachVerseToVisibleSegment(prev, targetSegmentId, verse));
        setVerses(prev => [...prev, verse]);
        setActiveVerseTs(targetSegmentId);
        return;
      }

      if (msg.type === 'verse_range_update') {
        const segmentId = messageSegmentId(msg);
        if (segmentId === null) return;
        const verse = msg.verse as VerseDetection;
        const targetSegmentId = resolveMergedSegmentId(mergedIntoRef.current, segmentId);
        setVerses(prev => prev.map(existing =>
          existing.book === verse.book && existing.chapter === verse.chapter
            ? verse
            : existing,
        ));
        setSegments(prev => attachVerseToVisibleSegment(prev, targetSegmentId, verse));
        return;
      }

      if (msg.type === 'verse_suggestion') {
        const segmentId = messageSegmentId(msg);
        if (segmentId === null) return;
        const nextSuggestions = msg.suggestions as VerseSuggestion[];
        const targetSegmentId = resolveMergedSegmentId(mergedIntoRef.current, segmentId);
        setSuggestions(nextSuggestions);
        setSegments(prev => prev.map(segment =>
          segment.id === targetSegmentId
            ? { ...segment, verseSuggestions: nextSuggestions }
            : segment,
        ));
        setActiveVerseTs(targetSegmentId);
        return;
      }

      if (msg.type === 'caption_merge') {
        if (msg.reason && msg.reason !== 'segmentation_repair') {
          console.warn('[useTranslationFeed] Ignoring unexpected caption_merge reason:', msg.reason);
          return;
        }
        const { keep, absorb } = messageMergeRef(msg);
        if (keep === null || absorb === null) {
          console.warn('[useTranslationFeed] caption_merge missing segment refs:', msg);
          return;
        }
        setSegments(prev => {
          const resolvedKeep = resolveMergedSegmentId(mergedIntoRef.current, keep);
          const absorbedSegment = prev.find(segment => segment.id === absorb);
          const rootSegmentId = messageRootSegmentId(msg) ?? resolvedKeep;
          const mergedFromSegmentIds = messageMergedFromSegmentIds(msg) ?? [resolvedKeep, absorb];
          mergedIntoRef.current.set(absorb, resolvedKeep);
          const filtered = prev.filter(segment => segment.id !== absorb);
          return filtered.map(segment =>
            segment.id === resolvedKeep
              ? {
                  ...segment,
                  english: String(msg.english ?? segment.english),
                  spanish: String(msg.spanish ?? segment.spanish),
                  phraseAlignment: [],
                  alignmentVersion: undefined,
                  previousAlignmentVersion: segment.alignmentVersion ?? null,
                  rootSegmentId,
                  mergedFromSegmentIds,
                  pendingCompletion: false,
                  verseDetected: segment.verseDetected ?? absorbedSegment?.verseDetected,
                  verseSuggestions: segment.verseSuggestions ?? absorbedSegment?.verseSuggestions,
                }
              : segment,
          );
        });
        flashSegment(keep);
        return;
      }

      if (msg.type === 'segment_metadata') {
        const segmentId = messageSegmentId(msg);
        if (segmentId === null) return;
        const targetSegmentId = resolveMergedSegmentId(mergedIntoRef.current, segmentId);
        setSegments(prev => prev.map(segment =>
          segment.id === targetSegmentId
            ? {
                ...segment,
                register: msg.translation_register as TranslationRegister | undefined,
                paragraphBreak: msg.paragraph_break as boolean | undefined,
                sourceQuality: msg.source_quality as 'clean' | 'noisy' | 'fragmented' | undefined,
                pendingCompletion: msg.pending_completion as boolean | undefined,
                terminalIncomplete: (msg.terminal_incomplete as boolean | undefined) ?? segment.terminalIncomplete,
              }
            : segment,
        ));
        return;
      }

      if (msg.type === 'mode_change') {
        setSermonMode(msg.to as SermonMode);
      }
    };

    const testHarness = typeof window !== 'undefined'
      ? (window.__cbDisplayTestHarness as TranslationFeedTestHarness | undefined)
      : undefined;
    if (testHarness) {
      const unsubscribe = testHarness.subscribe(handleMessage);
      return () => {
        stopped = true;
        unsubscribe();
        if (flashTimerRef.current) clearTimeout(flashTimerRef.current);
      };
    }

    const connect = () => {
      if (stopped) return;
      ws = new WebSocket(`${getWebSocketBaseUrl()}/api/display/v1?church_id=${encodeURIComponent(churchId)}`);
      ws.onopen = () => {
        setConnected(true);
        setDebug(prev => ({
          ...prev,
          displaySocketOpenCount: prev.displaySocketOpenCount + 1,
          lastSocketOpenAt: Date.now(),
        }));
      };
      ws.onerror = () => {
        setDebug(prev => ({
          ...prev,
          displaySocketErrorCount: prev.displaySocketErrorCount + 1,
          lastSocketErrorAt: Date.now(),
        }));
      };
      ws.onclose = (event) => {
        setConnected(false);
        setDebug(prev => ({
          ...prev,
          displaySocketCloseCount: prev.displaySocketCloseCount + 1,
          displayReconnectCount: prev.displayReconnectCount + 1,
          lastSocketCloseAt: Date.now(),
          lastSocketCloseCode: event.code,
          lastSocketCloseReason: event.reason,
        }));
        setTimeout(connect, 2000);
      };
      ws.onmessage = (e) => {
        let msg: Record<string, unknown>;
        try {
          msg = JSON.parse(e.data);
        } catch {
          console.warn('[useTranslationFeed] Malformed WebSocket message:', e.data);
          return;
        }
        handleMessage(msg);
      };
    };

    connect();
    return () => {
      stopped = true;
      ws?.close();
      if (flashTimerRef.current) clearTimeout(flashTimerRef.current);
    };
  }, [churchId]);

  return {
    segments,
    spanishLines,
    partialSpanish,
    liveEnglish,
    liveStableEnglish,
    liveDraftEnglish,
    liveSource,
    liveSegmentId,
    liveUpdatedAt,
    connected,
    flashingId,
    verses,
    suggestions,
    activeVerseTs,
    sermonMode,
    lastInterimAt,
    lastFinalAt,
    lastTranslationAt,
    lastInterimSpanish,
    lastFinalSpanish,
    lastCommittedEnglish,
    debug,
  };
}

declare global {
  interface Window {
    __cbDisplayTestHarness?: TranslationFeedTestHarness;
  }
}

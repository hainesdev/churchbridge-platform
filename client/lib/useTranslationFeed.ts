'use client';
import { useEffect, useRef, useState } from 'react';
import { getWebSocketBaseUrl } from '@/lib/wsBaseUrl';
import { attachVerseToVisibleSegment, resolveMergedSegmentId } from '@/lib/mergedVerseRouting';

export interface VerseDetection {
  book: string;
  chapter: number;
  verse_start: number;
  verse_end: number | null;
  spanish_text: string;
  canonical_english: string;
  reference: string;
  confidence: 'explicit' | 'quoted';
}

export interface VerseSuggestion {
  reference: string;
  canonical_english: string;
  relevance_note: string;
}

export type SermonMode =
  | 'scripture' | 'exposition' | 'illustration'
  | 'application' | 'exhortation' | 'procedural';

export type TranslationRegister = 'scripture' | 'expository' | 'narrative' | 'exhortation';

export interface Segment {
  id: number;
  spanish: string;
  english: string;
  verseDetected?: VerseDetection;
  // TODO: use register to pull exact verse text once Bible versions are stored
  register?: TranslationRegister;
  paragraphBreak?: boolean;
  sourceQuality?: 'clean' | 'noisy' | 'fragmented';
  /** True while the segment is pending a merge or deferred translation release.
   *  Set by segment_metadata when display_ready=false; cleared on translation_update or caption_merge. */
  pendingCompletion?: boolean;
  /** True when the buffer had to flush an incomplete tail because the audio chunk/session ended. */
  terminalIncomplete?: boolean;
}

const PARTIAL_FLUSH_MS = 80;
const FLASH_MS = 600;
const DEBUG_ENDPOINT = 'http://127.0.0.1:7272/ingest/21d235ae-7f16-4db5-a1b3-9bc0bbde2463';

const debugLog = (hypothesisId: string, location: string, message: string, data: Record<string, unknown>) => {
  // #region agent log
  fetch(DEBUG_ENDPOINT, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Debug-Session-Id': '233415' },
    body: JSON.stringify({
      sessionId: '233415',
      runId: 'pre-fix',
      hypothesisId,
      location,
      message,
      data,
      timestamp: Date.now(),
    }),
  }).catch(() => {});
  // #endregion
};

export interface TranslationFeed {
  segments: Segment[];
  spanishLines: string[];
  partialSpanish: string;
  partialEnglish: string;
  connected: boolean;
  flashingId: number | null;
  verses: VerseDetection[];
  suggestions: VerseSuggestion[];
  activeVerseTs: number | null;
  sermonMode: SermonMode;
}

export function useTranslationFeed(churchId: string): TranslationFeed {
  const [segments, setSegments] = useState<Segment[]>([]);
  const [spanishLines, setSpanishLines] = useState<string[]>([]);
  const [partialSpanish, setPartialSpanish] = useState('');
  const [partialEnglish, setPartialEnglish] = useState('');
  const [connected, setConnected] = useState(false);
  const [flashingId, setFlashingId] = useState<number | null>(null);
  const [verses, setVerses] = useState<VerseDetection[]>([]);
  const [suggestions, setSuggestions] = useState<VerseSuggestion[]>([]);
  const [activeVerseTs, setActiveVerseTs] = useState<number | null>(null);
  const [sermonMode, setSermonMode] = useState<SermonMode>('exposition');

  const partialQueueRef = useRef('');
  const lastInterimTextRef = useRef<string | null>(null);
  const flashTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mergedIntoRef = useRef<Map<number, number>>(new Map());

  // Flush buffered partial tokens to state on a fixed interval
  useEffect(() => {
    const timer = setInterval(() => {
      const q = partialQueueRef.current;
      setPartialEnglish(prev => prev !== q ? q : prev);
    }, PARTIAL_FLUSH_MS);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    let ws: WebSocket;
    let stopped = false;

    const connect = () => {
      if (stopped) return;
      // #region agent log
      debugLog('H2', 'useTranslationFeed.ts:connect', 'Opening display websocket', { churchId });
      // #endregion
      ws = new WebSocket(`${getWebSocketBaseUrl()}/api/display/v1?church_id=${encodeURIComponent(churchId)}`);
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        // #region agent log
        debugLog('H2', 'useTranslationFeed.ts:onclose', 'Display websocket closed; scheduling reconnect', { churchId });
        // #endregion
        setConnected(false);
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

        if (msg.type === 'interim') {
          setPartialSpanish(String(msg.text ?? ''));

        } else if (msg.type === 'stt_final') {
          setSpanishLines(prev => [...prev, String(msg.text ?? '')].slice(-8));
          setPartialSpanish('');

        } else if (msg.type === 'interim_translation') {
          const t = String(msg.text ?? '').trim();
          if (t && t !== lastInterimTextRef.current) {
            lastInterimTextRef.current = t;
            partialQueueRef.current = partialQueueRef.current
              ? partialQueueRef.current + ' ' + t
              : t;
          }

        } else if (msg.type === 'translation') {
          setSegments(prev => {
            const ts = Number(msg.ts);
            const duplicateCount = prev.filter(s => s.id === ts).length;
            // #region agent log
            debugLog('H1', 'useTranslationFeed.ts:translation', 'Received translation event', {
              ts,
              duplicateCountBeforeAppend: duplicateCount,
              segmentCountBeforeAppend: prev.length,
            });
            // #endregion
            // Deduplicate by ts to keep React keys stable when duplicate delivery/replay occurs.
            if (duplicateCount > 0) {
              return prev.map(s =>
                s.id === ts
                  ? { ...s, spanish: String(msg.spanish ?? ''), english: String(msg.english ?? '') }
                  : s
              );
            }
            return [...prev.slice(-99), { id: ts, spanish: String(msg.spanish ?? ''), english: String(msg.english ?? '') }];
          });
          setSpanishLines([]);
          setPartialSpanish('');
          lastInterimTextRef.current = null;
          partialQueueRef.current = '';
          setPartialEnglish('');

        } else if (msg.type === 'correction' || msg.type === 'translation_update') {
          // Both events update a committed segment's English text and flash it.
          // translation_update is the LLM-improved version (or deferred release); correction is Google's dual-pass.
          // Clear pendingCompletion — the segment is now finalised.
          const english = String(msg.english ?? '');
          setSegments(prev => prev.map(s =>
            s.id === msg.ts ? { ...s, english, pendingCompletion: false } : s
          ));
          // #region agent log
          debugLog('H4', 'useTranslationFeed.ts:correction_or_update', 'Applied correction/translation_update', {
            type: msg.type,
            ts: Number(msg.ts),
            reason: typeof msg.reason === 'string' ? msg.reason : '',
          });
          // #endregion
          const isMergedRebaseline = msg.type === 'correction' && msg.reason === 'merged_rebaseline';
          if (!isMergedRebaseline) {
            setFlashingId(Number(msg.ts));
            if (flashTimerRef.current) clearTimeout(flashTimerRef.current);
            flashTimerRef.current = setTimeout(() => setFlashingId(null), FLASH_MS);
          }

        } else if (msg.type === 'verse_detected') {
          const verse = msg.verse as VerseDetection;
          const targetTs = resolveMergedSegmentId(mergedIntoRef.current, Number(msg.ts));
          setSegments(prev => attachVerseToVisibleSegment(prev, targetTs, verse));
          setVerses(prev => [...prev, verse]);
          setActiveVerseTs(targetTs);

        } else if (msg.type === 'verse_range_update') {
          const verse = msg.verse as VerseDetection;
          const targetTs = resolveMergedSegmentId(mergedIntoRef.current, Number(msg.ts));
          // Replace the existing verse entry for this book+chapter with the
          // expanded range as the scratch pad accumulates more detections.
          setVerses(prev => prev.map(v =>
            v.book === verse.book && v.chapter === verse.chapter
              ? verse
              : v
          ));
          setSegments(prev => attachVerseToVisibleSegment(prev, targetTs, verse));

        } else if (msg.type === 'verse_suggestion') {
          setSuggestions(msg.suggestions as VerseSuggestion[]);
          setActiveVerseTs(resolveMergedSegmentId(mergedIntoRef.current, Number(msg.ts)));

        } else if (msg.type === 'caption_merge') {
          // Remove the absorbed segment and update the kept segment with merged content.
          // Clear pendingCompletion on the kept segment — it is now the merged final caption.
          setSegments(prev => {
            // #region agent log
            debugLog('H3', 'useTranslationFeed.ts:caption_merge', 'Applying caption merge', {
              tsKeep: Number(msg.ts_keep),
              tsAbsorb: Number(msg.ts_absorb),
              segmentCountBeforeMerge: prev.length,
            });
            // #endregion
            const tsKeep = Number(msg.ts_keep);
            const tsAbsorb = Number(msg.ts_absorb);
            const resolvedKeep = resolveMergedSegmentId(mergedIntoRef.current, tsKeep);
            const carriedVerse = prev.find(s => s.id === tsAbsorb)?.verseDetected;
            mergedIntoRef.current.set(tsAbsorb, resolvedKeep);
            const filtered = prev.filter(s => s.id !== tsAbsorb);
            return filtered.map(s =>
              s.id === resolvedKeep
                ? {
                    ...s,
                    spanish: msg.spanish as string,
                    english: msg.english as string,
                    pendingCompletion: false,
                    verseDetected: s.verseDetected ?? carriedVerse,
                  }
                : s
            );
          });

        } else if (msg.type === 'segment_metadata') {
          // Update a segment with scaffolding fields.
          // pendingCompletion drives visual dimming until merge or deferred release fires.
          setSegments(prev => prev.map(s =>
            s.id === msg.ts
              ? {
                  ...s,
                  register: msg.translation_register as TranslationRegister | undefined,
                  paragraphBreak: msg.paragraph_break as boolean | undefined,
                  sourceQuality: msg.source_quality as 'clean' | 'noisy' | 'fragmented' | undefined,
                  pendingCompletion: msg.pending_completion as boolean | undefined,
                  terminalIncomplete: (msg.terminal_incomplete as boolean | undefined) ?? s.terminalIncomplete,
                }
              : s
          ));

        } else if (msg.type === 'mode_change') {
          setSermonMode(msg.to as SermonMode);
        }
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
    segments, spanishLines, partialSpanish, partialEnglish,
    connected, flashingId,
    verses, suggestions, activeVerseTs,
    sermonMode,
  };
}

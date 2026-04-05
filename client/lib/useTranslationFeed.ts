'use client';
import { useEffect, useRef, useState } from 'react';
import { getWebSocketBaseUrl } from '@/lib/wsBaseUrl';

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

export interface Segment {
  id: number;
  spanish: string;
  english: string;
  verseDetected?: VerseDetection;
}

const PARTIAL_FLUSH_MS = 80;
const FLASH_MS = 600;

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
      ws = new WebSocket(`${getWebSocketBaseUrl()}/api/display/v1?church_id=${encodeURIComponent(churchId)}`);
      ws.onopen = () => setConnected(true);
      ws.onclose = () => { setConnected(false); setTimeout(connect, 2000); };
      ws.onmessage = (e) => {
        let msg: Record<string, unknown>;
        try {
          msg = JSON.parse(e.data);
        } catch {
          console.warn('[useTranslationFeed] Malformed WebSocket message:', e.data);
          return;
        }

        if (msg.type === 'interim') {
          setPartialSpanish(msg.text);

        } else if (msg.type === 'stt_final') {
          setSpanishLines(prev => [...prev, msg.text].slice(-8));
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
          setSegments(prev => [...prev.slice(-99), { id: msg.ts, spanish: msg.spanish, english: msg.english }]);
          setSpanishLines([]);
          setPartialSpanish('');
          lastInterimTextRef.current = null;
          partialQueueRef.current = '';
          setPartialEnglish('');

        } else if (msg.type === 'correction' || msg.type === 'translation_update') {
          // Both events update a committed segment's English text and flash it.
          // translation_update is the LLM-improved version; correction is Google's dual-pass.
          setSegments(prev => prev.map(s => s.id === msg.ts ? { ...s, english: msg.english } : s));
          setFlashingId(msg.ts);
          if (flashTimerRef.current) clearTimeout(flashTimerRef.current);
          flashTimerRef.current = setTimeout(() => setFlashingId(null), FLASH_MS);

        } else if (msg.type === 'verse_detected') {
          setSegments(prev => prev.map(s =>
            s.id === msg.ts ? { ...s, verseDetected: msg.verse } : s
          ));
          setVerses(prev => [...prev, msg.verse]);
          setActiveVerseTs(msg.ts);

        } else if (msg.type === 'verse_range_update') {
          // Replace the existing verse entry for this book+chapter with the
          // expanded range as the scratch pad accumulates more detections.
          setVerses(prev => prev.map(v =>
            v.book === msg.verse.book && v.chapter === msg.verse.chapter
              ? msg.verse
              : v
          ));
          setSegments(prev => prev.map(s =>
            s.id === msg.ts ? { ...s, verseDetected: msg.verse } : s
          ));

        } else if (msg.type === 'verse_suggestion') {
          setSuggestions(msg.suggestions);
          setActiveVerseTs(msg.ts);

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

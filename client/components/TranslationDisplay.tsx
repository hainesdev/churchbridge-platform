'use client';
import { useEffect, useRef, useState } from 'react';

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
 * Two-slot spatial model:
 *
 *  ┌─────────────────────────────┐
 *  │   flexible empty space      │
 *  ├─────────────────────────────┤
 *  │  SLOT A — fixed height      │  "just read" — dim, eye dismisses it
 *  ├─────────────────────────────┤
 *  │  SLOT B — fixed height      │  active reading zone — eye lives here
 *  ├─────────────────────────────┤
 *  │   bottom padding            │
 *  └─────────────────────────────┘
 *
 * Slots never move or resize. Content transitions within them.
 * When a sentence commits: old Slot B cross-fades into Slot A, Slot B clears.
 * Words flowing into Slot B need no animation — the fixed boundary contains them.
 */

// Slot height constants — adjust to match your screen and font size preferences
const SLOT_A_H = 'h-24';   // ~96px  — previous sentence (smaller, dimmed)
const SLOT_B_H = 'h-32';   // ~128px — active sentence   (larger, bright)

export function TranslationDisplay({ churchId, mode = 'full' }: TranslationDisplayProps) {
  // Full segment history kept only for correction events
  const [segments, setSegments] = useState<Segment[]>([]);
  const [spanishLines, setSpanishLines] = useState<string[]>([]);
  const [partialSpanish, setPartialSpanish] = useState('');
  const [partialEnglish, setPartialEnglish] = useState('');
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

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
          setSpanishLines((prev) => [...prev, msg.text].slice(-4));
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

  const statusBar = (label: string) => (
    <div className="flex-none px-6 py-2 bg-gray-900 flex items-center gap-2">
      <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-400 animate-pulse' : 'bg-red-500'}`} />
      <span className="text-xs text-gray-400">{connected ? label : 'Connecting...'}</span>
    </div>
  );

  // Slot A = last committed sentence. Slot B = current partial.
  // key on Slot A content triggers fade-in animation on each new sentence.
  const slotA = segments[segments.length - 1] ?? null;
  const activeSpanish = spanishLines.join(' ');

  // ── Lower-thirds overlay ──────────────────────────────────────────────────
  if (mode === 'lowerthird') {
    const recent = segments.slice(-2);
    return (
      <div className="fixed bottom-0 left-0 right-0 p-6 space-y-2">
        {recent.map((s) => (
          <div key={s.id} className="bg-black/70 px-4 py-2 rounded text-white text-2xl font-medium">
            {s.english}
          </div>
        ))}
        {partialEnglish && (
          <div className="bg-black/70 px-4 py-2 rounded text-white/80 text-2xl italic">
            {partialEnglish}<span className="animate-pulse">▌</span>
          </div>
        )}
      </div>
    );
  }

  // ── Spanish captions ──────────────────────────────────────────────────────
  if (mode === 'spanish') {
    return (
      <div className="h-full flex flex-col bg-black text-white overflow-hidden">
        {statusBar('En vivo')}

        {/* Flexible empty space — eye should not be here */}
        <div className="flex-1" />

        {/* Fixed caption area */}
        <div className="flex-none px-10 pb-12 space-y-4">

          {/* Slot A — previous committed Spanish sentence, dim */}
          <div className={`${SLOT_A_H} overflow-hidden flex items-end`}>
            {slotA && (
              <p key={slotA.id} className="text-3xl font-semibold leading-snug text-gray-500 animate-fade-in">
                {slotA.spanish}
              </p>
            )}
          </div>

          {/* Slot B — active: committed fragments + in-progress partial */}
          <div className={`${SLOT_B_H} overflow-hidden flex items-start`}>
            {(activeSpanish || partialSpanish) ? (
              <p className="text-4xl font-bold leading-snug text-white">
                {activeSpanish}
                {activeSpanish && partialSpanish ? ' ' : ''}
                <span className="text-gray-300 italic">{partialSpanish}</span>
                <span className="animate-pulse text-yellow-400">▌</span>
              </p>
            ) : (
              <p className="text-gray-700 text-2xl">Esperando...</p>
            )}
          </div>

        </div>
      </div>
    );
  }

  // ── Bilingual: Spanish primary + English secondary ────────────────────────
  if (mode === 'bilingual') {
    return (
      <div className="h-full flex flex-col bg-black text-white overflow-hidden">
        {statusBar('Live')}

        <div className="flex-1" />

        <div className="flex-none px-10 pb-12 space-y-4">

          {/* Slot A — previous committed pair, dim */}
          <div className={`${SLOT_A_H} overflow-hidden flex flex-col justify-end gap-1`}>
            {slotA && (
              <div key={slotA.id} className="animate-fade-in">
                <p className="text-2xl font-bold leading-snug text-gray-500">{slotA.spanish}</p>
                <p className="text-lg text-blue-400/50 leading-snug">{slotA.english}</p>
              </div>
            )}
          </div>

          {/* Slot B — active pair */}
          <div className={`${SLOT_B_H} overflow-hidden flex flex-col justify-start gap-1`}>
            {(activeSpanish || partialSpanish || partialEnglish) ? (
              <>
                <p className="text-3xl font-bold leading-snug text-white">
                  {activeSpanish}
                  {activeSpanish && partialSpanish ? ' ' : ''}
                  <span className="text-gray-300 italic">{partialSpanish}</span>
                  <span className="animate-pulse text-yellow-400">▌</span>
                </p>
                {partialEnglish && (
                  <p className="text-xl text-blue-300 leading-snug">{partialEnglish}</p>
                )}
              </>
            ) : (
              <p className="text-gray-700 text-2xl">Waiting...</p>
            )}
          </div>

        </div>
      </div>
    );
  }

  // ── Full mode: English primary ────────────────────────────────────────────
  return (
    <div className="h-full flex flex-col bg-black text-white overflow-hidden">
      {statusBar('Live')}

      <div className="flex-1" />

      <div className="flex-none px-10 pb-12 space-y-4">

        {/* Slot A — previous English sentence, dim */}
        <div className={`${SLOT_A_H} overflow-hidden flex flex-col justify-end gap-1`}>
          {slotA && (
            <div key={slotA.id} className="animate-fade-in">
              <p className="text-2xl font-bold leading-snug text-gray-500">{slotA.english}</p>
              <p className="text-base text-gray-600">{slotA.spanish}</p>
            </div>
          )}
        </div>

        {/* Slot B — active English partial */}
        <div className={`${SLOT_B_H} overflow-hidden flex items-start`}>
          {partialEnglish ? (
            <p className="text-4xl font-bold leading-snug text-white">
              {partialEnglish}
              <span className="animate-pulse text-blue-400">▌</span>
            </p>
          ) : (
            <p className="text-gray-700 text-2xl">Waiting...</p>
          )}
        </div>

      </div>
    </div>
  );
}

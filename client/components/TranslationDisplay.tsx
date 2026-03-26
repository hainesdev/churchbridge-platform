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
 * Three fixed zones, anchored to the bottom of the screen.
 * Zones never move or resize — they are permanent spatial regions.
 * Only the content within zones changes, fading in when replaced.
 *
 *  ┌───────────────────────────────┐
 *  │  flexible empty space         │
 *  ├───────────────────────────────┤
 *  │  ZONE 0  dim  (oldest)        │  opacity 0.18
 *  ├───────────────────────────────┤
 *  │  ZONE 1  dim  (recent)        │  opacity 0.48
 *  ├───────────────────────────────┤
 *  │  ZONE 2  full (active)        │  opacity 1.0
 *  └───────────────────────────────┘
 *
 * When a sentence commits it appears in Zone 1, Zone 1's old content
 * moves to Zone 0, Zone 0's old content fades out.
 * Zone 2 rebuilds from empty as the next sentence arrives.
 *
 * Content in each zone fades in via CSS animation when replaced.
 * Opacity on zones is static — no layout or position animation anywhere.
 */

// Minimum height reserved for each zone so layout never shifts.
// Increase if wrapped text is taller than these values.
const ZONE_H = ['min-h-[3.2rem]', 'min-h-[3.2rem]', 'min-h-[5rem]'];

// CSS fade-in is applied by remounting inner content (key change).
// Duration is controlled in globals.css: .animate-fade-in

function Zone({
  children,
  minH,
  opacity,
  className = '',
}: {
  children?: React.ReactNode;
  minH: string;
  opacity: number;
  className?: string;
}) {
  return (
    <div
      className={`${minH} overflow-hidden flex items-end ${className}`}
      style={{ opacity, transition: 'opacity 1300ms ease-out' }}
    >
      {children}
    </div>
  );
}

export function TranslationDisplay({ churchId, mode = 'full' }: TranslationDisplayProps) {
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

  // Slot assignment: last two committed segments fill zones 0 and 1.
  // Always two slots — null when not yet filled (zone stays reserved but empty).
  const slot0 = segments.length >= 2 ? segments[segments.length - 2] : null;
  const slot1 = segments.length >= 1 ? segments[segments.length - 1] : null;

  // ── Lower-thirds overlay ──────────────────────────────────────────────────
  if (mode === 'lowerthird') {
    return (
      <div className="fixed bottom-0 left-0 right-0 p-6 space-y-2">
        {segments.slice(-2).map((s) => (
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
    const activeSpanish = spanishLines.join(' ');
    return (
      <div className="h-full flex flex-col bg-black text-white overflow-hidden">
        {statusBar('En vivo')}
        <div className="flex-1" />
        <div className="flex-none px-10 pb-12 space-y-3">
          <Zone minH={ZONE_H[0]} opacity={0.18}>
            {slot0 && (
              <p key={slot0.id} className="text-2xl font-semibold leading-snug animate-fade-in">
                {slot0.spanish}
              </p>
            )}
          </Zone>
          <Zone minH={ZONE_H[1]} opacity={0.5}>
            {slot1 && (
              <p key={slot1.id} className="text-2xl font-semibold leading-snug animate-fade-in">
                {slot1.spanish}
              </p>
            )}
          </Zone>
          <Zone minH={ZONE_H[2]} opacity={1} className="items-start">
            <p className="text-4xl font-bold leading-snug">
              {activeSpanish}{activeSpanish && partialSpanish ? ' ' : ''}
              <span className="text-gray-300">{partialSpanish}</span>
              <span className="animate-pulse text-yellow-400">▌</span>
            </p>
          </Zone>
        </div>
      </div>
    );
  }

  // ── Bilingual: Spanish primary + English secondary ────────────────────────
  if (mode === 'bilingual') {
    const activeSpanish = spanishLines.join(' ');
    return (
      <div className="h-full flex flex-col bg-black text-white overflow-hidden">
        {statusBar('Live')}
        <div className="flex-1" />
        <div className="flex-none px-10 pb-12 space-y-3">
          <Zone minH={ZONE_H[0]} opacity={0.18}>
            {slot0 && (
              <div key={slot0.id} className="animate-fade-in space-y-0.5 w-full">
                <p className="text-2xl font-bold leading-snug">{slot0.spanish}</p>
                <p className="text-base text-blue-300 leading-snug">{slot0.english}</p>
              </div>
            )}
          </Zone>
          <Zone minH={ZONE_H[1]} opacity={0.5}>
            {slot1 && (
              <div key={slot1.id} className="animate-fade-in space-y-0.5 w-full">
                <p className="text-2xl font-bold leading-snug">{slot1.spanish}</p>
                <p className="text-base text-blue-300 leading-snug">{slot1.english}</p>
              </div>
            )}
          </Zone>
          <Zone minH={ZONE_H[2]} opacity={1} className="items-start">
            <div className="space-y-1 w-full">
              <p className="text-3xl font-bold leading-snug">
                {activeSpanish}{activeSpanish && partialSpanish ? ' ' : ''}
                <span className="text-gray-300">{partialSpanish}</span>
                <span className="animate-pulse text-yellow-400">▌</span>
              </p>
              {partialEnglish && (
                <p className="text-xl text-blue-300 leading-snug">{partialEnglish}</p>
              )}
            </div>
          </Zone>
        </div>
      </div>
    );
  }

  // ── Full mode: English primary ────────────────────────────────────────────
  return (
    <div className="h-full flex flex-col bg-black text-white overflow-hidden">
      {statusBar('Live')}
      <div className="flex-1" />
      <div className="flex-none px-10 pb-12 space-y-3">
        <Zone minH={ZONE_H[0]} opacity={0.18}>
          {slot0 && (
            <div key={slot0.id} className="animate-fade-in space-y-0.5 w-full">
              <p className="text-2xl font-bold leading-snug">{slot0.english}</p>
              <p className="text-sm text-gray-500">{slot0.spanish}</p>
            </div>
          )}
        </Zone>
        <Zone minH={ZONE_H[1]} opacity={0.5}>
          {slot1 && (
            <div key={slot1.id} className="animate-fade-in space-y-0.5 w-full">
              <p className="text-2xl font-bold leading-snug">{slot1.english}</p>
              <p className="text-sm text-gray-500">{slot1.spanish}</p>
            </div>
          )}
        </Zone>
        <Zone minH={ZONE_H[2]} opacity={1} className="items-start">
          <p className="text-4xl font-bold leading-snug">
            {partialEnglish}
            <span className="animate-pulse text-blue-400">▌</span>
          </p>
        </Zone>
      </div>
    </div>
  );
}

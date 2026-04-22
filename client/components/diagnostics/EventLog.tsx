'use client';
import { RawEvent } from '@/lib/useEventLog';

const EVENT_COLOR: Record<string, string> = {
  interim: 'text-gray-600',
  stt_final: 'text-gray-400',
  final_spanish: 'text-gray-400',
  translation: 'text-green-400',
  translation_update: 'text-green-300',
  correction: 'text-sky-400',
  verse_detected: 'text-yellow-400',
  verse_range_update: 'text-yellow-400',
  verse_suggestion: 'text-yellow-500',
  mode_change: 'text-purple-400',
  caption_merge: 'text-gray-500',
  segment_metadata: 'text-gray-600',
  interim_translation: 'text-gray-600',
};

function formatTs(ts: number): string {
  try {
    return new Date(ts).toISOString().slice(11, 23);
  } catch {
    return '--:--:--.---';
  }
}

function excerptPayload(raw: Record<string, unknown>): string {
  const { type: _type, ts: _ts, ...rest } = raw;
  const str = JSON.stringify(rest);
  return str.length > 80 ? str.slice(0, 80) + '…' : str;
}

interface EventLogProps {
  events: RawEvent[];
  connected: boolean;
}

export function EventLog({ events, connected }: EventLogProps) {
  const reversed = [...events].reverse();

  return (
    <div className="rounded-2xl border border-gray-800 bg-gray-900 p-4 space-y-2">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
          Event Stream
        </h3>
        <span className={`text-xs ${connected ? 'text-green-500' : 'text-gray-600'}`}>
          {connected ? '● live' : '○ disconnected'}
        </span>
      </div>
      <div className="h-64 overflow-y-auto space-y-0.5 font-mono">
        {reversed.length === 0 ? (
          <p className="text-xs text-gray-600 py-2">No events yet</p>
        ) : (
          reversed.map((ev, i) => (
            <div key={i} className="flex gap-2 text-xs leading-5">
              <span className="text-gray-600 shrink-0">{formatTs(ev.ts)}</span>
              <span className={`shrink-0 ${EVENT_COLOR[ev.type] ?? 'text-gray-400'}`}>
                {ev.type}
              </span>
              <span className="text-gray-600 truncate">{excerptPayload(ev.raw)}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

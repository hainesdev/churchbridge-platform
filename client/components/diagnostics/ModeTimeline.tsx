'use client';

export interface ModeTransition {
  from_mode: string;
  to_mode: string;
  segment_ts: number;
  occurred_at: string; // ISO string from DB
}

const MODE_COLOR: Record<string, string> = {
  scripture: 'bg-yellow-500',
  exposition: 'bg-sky-600',
  illustration: 'bg-emerald-600',
  application: 'bg-violet-600',
  exhortation: 'bg-orange-500',
  procedural: 'bg-gray-600',
};

const DEFAULT_COLOR = 'bg-gray-700';

interface ModeTimelineProps {
  transitions: ModeTransition[];
  sessionStart: string; // ISO string
  sessionEnd: string | null; // ISO string or null (→ now)
}

export function ModeTimeline({ transitions, sessionStart, sessionEnd }: ModeTimelineProps) {
  const startMs = new Date(sessionStart).getTime();
  const endMs = sessionEnd ? new Date(sessionEnd).getTime() : Date.now();
  const spanMs = Math.max(endMs - startMs, 1);

  if (transitions.length === 0) {
    // No transitions recorded — show a single bar for the default mode
    return (
      <div className="h-6 flex overflow-hidden rounded-full">
        <div
          className={`h-full flex-1 ${MODE_COLOR['exposition'] ?? DEFAULT_COLOR}`}
          title="exposition (default)"
        />
      </div>
    );
  }

  // Build segments: each transition marks a mode change at occurred_at
  // Segment i runs from transitions[i].occurred_at to transitions[i+1].occurred_at
  // The final segment runs to sessionEnd
  // The initial segment runs from sessionStart to transitions[0].occurred_at with from_mode
  const segments: Array<{ mode: string; startMs: number; endMs: number }> = [];

  // Initial segment (before first transition)
  const firstTransitionMs = new Date(transitions[0].occurred_at).getTime();
  if (firstTransitionMs > startMs) {
    segments.push({
      mode: transitions[0].from_mode,
      startMs,
      endMs: firstTransitionMs,
    });
  }

  // Segments for each transition
  for (let i = 0; i < transitions.length; i++) {
    const segStart = new Date(transitions[i].occurred_at).getTime();
    const segEnd =
      i + 1 < transitions.length
        ? new Date(transitions[i + 1].occurred_at).getTime()
        : endMs;
    segments.push({
      mode: transitions[i].to_mode,
      startMs: segStart,
      endMs: segEnd,
    });
  }

  return (
    <div className="space-y-1">
      <div className="h-6 flex overflow-hidden rounded-full">
        {segments.map((seg, i) => {
          const widthPct = ((seg.endMs - seg.startMs) / spanMs) * 100;
          return (
            <div
              key={i}
              className={`h-full ${MODE_COLOR[seg.mode] ?? DEFAULT_COLOR}`}
              style={{ width: `${widthPct}%` }}
              title={seg.mode}
            />
          );
        })}
      </div>
      <div className="flex flex-wrap gap-2">
        {Object.entries(MODE_COLOR).map(([mode, color]) => (
          <span key={mode} className="flex items-center gap-1 text-xs text-gray-500">
            <span className={`inline-block w-2 h-2 rounded-sm ${color}`} />
            {mode}
          </span>
        ))}
      </div>
    </div>
  );
}

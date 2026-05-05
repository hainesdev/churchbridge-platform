'use client';
import { useEffect, useRef, useState } from 'react';
import { RawEvent } from '@/lib/useEventLog';

const STAGE_COLOR: Record<string, string> = {
  'stt.final': 'border-amber-800 bg-amber-950/40 text-amber-200',
  'sentence.flush': 'border-orange-800 bg-orange-950/40 text-orange-200',
  'translation.google': 'border-cyan-800 bg-cyan-950/40 text-cyan-200',
  'translation.google_correction': 'border-cyan-800 bg-cyan-950/40 text-cyan-200',
  'translation.llm_update': 'border-emerald-800 bg-emerald-950/40 text-emerald-200',
  'translation.passthrough': 'border-emerald-900 bg-emerald-950/20 text-emerald-300',
  structural: 'border-fuchsia-800 bg-fuchsia-950/40 text-fuchsia-200',
  repair: 'border-rose-800 bg-rose-950/40 text-rose-200',
  alignment: 'border-sky-800 bg-sky-950/40 text-sky-200',
  'alignment.emit': 'border-sky-800 bg-sky-950/40 text-sky-200',
  'display.feed_commit': 'border-lime-800 bg-lime-950/40 text-lime-200',
  'display.feed_revision': 'border-lime-800 bg-lime-950/40 text-lime-200',
  'caption.merge': 'border-violet-800 bg-violet-950/40 text-violet-200',
  'verse.detected': 'border-yellow-800 bg-yellow-950/40 text-yellow-200',
  verse_suggestions: 'border-yellow-800 bg-yellow-950/40 text-yellow-200',
  'summary.prompt': 'border-pink-800 bg-pink-950/40 text-pink-200',
  'summary.response': 'border-pink-800 bg-pink-950/40 text-pink-200',
  'summary.applied': 'border-pink-800 bg-pink-950/40 text-pink-200',
  'summary.error': 'border-red-800 bg-red-950/40 text-red-200',
};

const LANE_COLOR: Record<string, string> = {
  ingest: 'border-amber-800/70 bg-amber-950/30 text-amber-200',
  llm: 'border-fuchsia-800/70 bg-fuchsia-950/30 text-fuchsia-200',
  summary: 'border-pink-800/70 bg-pink-950/30 text-pink-200',
  display: 'border-lime-800/70 bg-lime-950/30 text-lime-200',
  runtime: 'border-sky-800/70 bg-sky-950/30 text-sky-200',
  system: 'border-gray-700 bg-gray-900 text-gray-300',
};

const NOISE_EVENT_TYPES = new Set([
  'isrManifest',
  'turbopack-connected',
  'building',
  'built',
  'sync',
]);

interface TraceData {
  [key: string]: unknown;
}

interface TimelineEvent {
  key: string;
  ts: number;
  type: string;
  stage: string;
  traceKind: string;
  lane: string;
  summary: string;
  segmentId: number | null;
  callId: string | null;
  raw: Record<string, unknown>;
  data: TraceData;
  detailSections: Array<{ label: string; value: unknown }>;
}

function formatTs(ts: number): string {
  try {
    return new Date(ts).toISOString().slice(11, 23);
  } catch {
    return '--:--:--.---';
  }
}

function formatElapsed(ts: number, originTs: number | null): string {
  if (originTs == null) return '--:--.---';
  const diff = Math.max(0, ts - originTs);
  const minutes = Math.floor(diff / 60000);
  const seconds = Math.floor((diff % 60000) / 1000);
  const millis = diff % 1000;
  return `+${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(millis).padStart(3, '0')}`;
}

function stringifyValue(value: unknown, pretty = true): string {
  if (typeof value === 'string') return value;
  try {
    return pretty ? JSON.stringify(value, null, 2) : JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function toNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function buildSummary(raw: Record<string, unknown>, type: string): string {
  if (type === 'pipeline_trace') {
    return String(raw.summary ?? raw.trace_stage ?? 'trace');
  }
  if (typeof raw.text === 'string' && raw.text.trim()) return raw.text;
  if (typeof raw.english === 'string' && raw.english.trim()) return raw.english;
  if (typeof raw.message === 'string' && raw.message.trim()) return raw.message;
  if (typeof raw.reason === 'string' && raw.reason.trim()) return raw.reason;
  if (typeof raw.source === 'string' && raw.source.trim()) return `source: ${raw.source}`;
  return type;
}

function getLane(type: string, stage: string): string {
  if (stage.startsWith('summary.') || type === 'summary_update') return 'summary';
  if (
    stage === 'structural'
    || stage === 'repair'
    || stage === 'alignment'
    || stage === 'translation.llm_update'
    || stage === 'verse_suggestions'
  ) {
    return 'llm';
  }
  if (
    stage === 'display.feed_commit'
    || stage === 'display.feed_revision'
    || stage === 'alignment.emit'
    || stage === 'caption.merge'
    || type === 'feed_commit'
    || type === 'feed_revision'
    || type === 'live_translation'
    || type === 'live_translation_clear'
    || type === 'final_spanish'
    || type === 'caption_merge'
  ) {
    return 'display';
  }
  if (
    stage === 'stt.final'
    || stage === 'sentence.flush'
    || stage === 'translation.google'
    || stage === 'translation.google_correction'
    || stage === 'translation.passthrough'
    || type === 'stt_final'
    || type === 'interim'
  ) {
    return 'ingest';
  }
  if (NOISE_EVENT_TYPES.has(type) || type === 'mode_change') return 'system';
  return 'runtime';
}

function buildDetailSections(raw: Record<string, unknown>, type: string, data: TraceData) {
  if (type === 'pipeline_trace') {
    return [
      { label: 'System Prompt', value: data.system },
      { label: 'User Prompt', value: data.user },
      { label: 'Raw Response', value: data.raw_response },
      { label: 'Parsed JSON', value: data.parsed_json },
      { label: 'Payload', value: data },
      { label: 'Raw Event', value: raw },
    ].filter(section => section.value != null);
  }
  return [
    { label: 'Payload', value: raw },
  ];
}

function normalizeEvent(event: RawEvent, index: number): TimelineEvent {
  const raw = event.raw;
  const type = String(raw.type ?? event.type ?? 'unknown');
  const stage =
    type === 'pipeline_trace'
      ? String(raw.trace_stage ?? 'pipeline')
      : type;
  const traceKind =
    type === 'pipeline_trace'
      ? String(raw.trace_kind ?? 'event')
      : 'event';
  const segmentId = toNumber(raw.segment_id ?? null);
  const callId = typeof raw.call_id === 'string' ? raw.call_id : null;
  const data =
    raw.data && typeof raw.data === 'object'
      ? (raw.data as TraceData)
      : {};
  return {
    key: `${event.ts}-${type}-${stage}-${callId ?? index}`,
    ts: Number(raw.ts ?? event.ts),
    type,
    stage,
    traceKind,
    lane: getLane(type, stage),
    summary: buildSummary(raw, type),
    segmentId,
    callId,
    raw,
    data,
    detailSections: buildDetailSections(raw, type, data),
  };
}

function FilterButton({
  active,
  label,
  onClick,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`rounded-full border px-3 py-1.5 text-xs transition-colors ${
        active
          ? 'border-emerald-700 bg-emerald-950/60 text-emerald-200'
          : 'border-gray-800 bg-gray-950/60 text-gray-500 hover:text-gray-300'
      }`}
    >
      {label}
    </button>
  );
}

function StatCard({
  label,
  value,
  accent,
}: {
  label: string;
  value: string | number;
  accent: string;
}) {
  return (
    <div className="rounded-2xl border border-gray-800 bg-gray-950/60 px-4 py-3">
      <p className="text-[10px] uppercase tracking-[0.18em] text-gray-500">{label}</p>
      <p className={`mt-2 text-lg font-semibold ${accent}`}>{value}</p>
    </div>
  );
}

function DetailSection({
  label,
  value,
}: {
  label: string;
  value: unknown;
}) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<'pretty' | 'raw'>('pretty');

  if (value == null) return null;
  const text = stringifyValue(value, mode === 'pretty');
  if (!text.trim()) return null;

  const lineCount = text.split('\n').length;
  const canToggleMode = typeof value !== 'string';

  return (
    <div className="rounded-2xl border border-gray-800 bg-gray-950/45">
      <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
        <button
          onClick={() => setOpen(current => !current)}
          className="flex items-center gap-2 text-left"
        >
          <span className="text-xs text-gray-400">{open ? 'Hide' : 'Show'}</span>
          <p className="text-[10px] uppercase tracking-[0.18em] text-gray-500">{label}</p>
        </button>
        <div className="flex items-center gap-2 text-[10px] text-gray-600">
          <span>{lineCount} lines</span>
          {canToggleMode && (
            <div className="rounded-full border border-gray-800 bg-gray-900/70 p-0.5">
              <button
                onClick={() => setMode('pretty')}
                className={`rounded-full px-2 py-1 ${mode === 'pretty' ? 'bg-emerald-950/70 text-emerald-200' : 'text-gray-500'}`}
              >
                pretty
              </button>
              <button
                onClick={() => setMode('raw')}
                className={`rounded-full px-2 py-1 ${mode === 'raw' ? 'bg-emerald-950/70 text-emerald-200' : 'text-gray-500'}`}
              >
                raw
              </button>
            </div>
          )}
        </div>
      </div>
      {open && (
        <div className="border-t border-gray-800 px-3 pb-3 pt-2">
          <pre className="overflow-x-auto rounded-xl border border-gray-800 bg-gray-950/80 p-3 text-[11px] leading-5 text-gray-300 whitespace-pre-wrap break-words">
            {text}
          </pre>
        </div>
      )}
      {!open && (
        <div className="border-t border-gray-800 px-3 py-2 text-xs text-gray-600">
          Hidden until expanded. Full payload preserved for this session.
        </div>
      )}
    </div>
  );
}

export function PipelineTrace({
  events,
  connected,
}: {
  events: RawEvent[];
  connected: boolean;
}) {
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [mode, setMode] = useState<'all' | 'trace' | 'runtime'>('all');
  const [stageFilter, setStageFilter] = useState('all');
  const [includeNoise, setIncludeNoise] = useState(false);
  const [followLive, setFollowLive] = useState(true);
  const listRef = useRef<HTMLDivElement | null>(null);

  const timelineEvents = events.map(normalizeEvent);
  const sessionOriginTs = timelineEvents[0]?.ts ?? null;
  const stageOptions = ['all', ...Array.from(new Set(timelineEvents.map(event => event.stage)))];

  const filteredEvents = timelineEvents.filter(event => {
    if (!includeNoise && NOISE_EVENT_TYPES.has(event.type)) return false;
    if (mode === 'trace' && event.type !== 'pipeline_trace') return false;
    if (mode === 'runtime' && event.type === 'pipeline_trace') return false;
    if (stageFilter !== 'all' && event.stage !== stageFilter) return false;
    if (!search.trim()) return true;
    const haystack = `${event.type}\n${event.stage}\n${event.summary}\n${stringifyValue(event.raw)}`.toLowerCase();
    return haystack.includes(search.toLowerCase());
  });

  const latestEvent = filteredEvents[filteredEvents.length - 1] ?? null;
  const selectedEvent = followLive
    ? latestEvent
    : filteredEvents.find(event => event.key === selectedKey)
      ?? latestEvent
      ?? null;

  useEffect(() => {
    if (!followLive || !selectedEvent || !listRef.current) return;
    const node = listRef.current.querySelector<HTMLElement>(`[data-event-key="${CSS.escape(selectedEvent.key)}"]`);
    node?.scrollIntoView({ block: 'nearest' });
  }, [followLive, selectedEvent]);

  const visibleTraceCount = filteredEvents.filter(event => event.type === 'pipeline_trace').length;
  const visibleRuntimeCount = filteredEvents.length - visibleTraceCount;
  const laneCounts = filteredEvents.reduce<Record<string, number>>((counts, event) => {
    counts[event.lane] = (counts[event.lane] ?? 0) + 1;
    return counts;
  }, {});
  const errorCount = filteredEvents.filter(
    event => event.stage.endsWith('.error') || event.type.toLowerCase().includes('error'),
  ).length;

  return (
    <div className="rounded-[2rem] border border-gray-800 bg-gray-900/95 p-4 space-y-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <h3 className="text-sm font-semibold uppercase tracking-[0.2em] text-gray-400">
            Observability Timeline
          </h3>
          <p className="mt-1 text-sm text-gray-500">
            Full event stream with prompts, responses, summary generation, and runtime emissions on one timeline.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className={`${connected ? 'text-green-400' : 'text-gray-600'}`}>
            {connected ? 'live stream connected' : 'stream disconnected'}
          </span>
          <span className="text-gray-600">
            {filteredEvents.length}
            {' / '}
            {timelineEvents.length}
            {' '}
            visible
          </span>
          <button
            onClick={() => setFollowLive(current => !current)}
            className={`rounded-full border px-3 py-1.5 ${
              followLive
                ? 'border-emerald-700 bg-emerald-950/60 text-emerald-200'
                : 'border-gray-800 bg-gray-950/60 text-gray-400'
            }`}
          >
            {followLive ? 'Following Live' : 'Paused'}
          </button>
          <button
            onClick={() => {
              const newest = latestEvent;
              if (!newest) return;
              setSelectedKey(newest.key);
              setFollowLive(true);
            }}
            className="rounded-full border border-gray-800 bg-gray-950/60 px-3 py-1.5 text-gray-400 hover:text-gray-200"
          >
            Jump to Latest
          </button>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
        <StatCard label="Visible Events" value={filteredEvents.length} accent="text-white" />
        <StatCard label="Trace Events" value={visibleTraceCount} accent="text-emerald-300" />
        <StatCard label="Runtime Events" value={visibleRuntimeCount} accent="text-sky-300" />
        <StatCard label="Summary Events" value={laneCounts.summary ?? 0} accent="text-pink-300" />
        <StatCard label="Errors Visible" value={errorCount} accent={errorCount > 0 ? 'text-red-300' : 'text-gray-300'} />
      </div>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1.4fr)_minmax(24rem,0.9fr)]">
        <div className="space-y-3">
          <div className="rounded-2xl border border-gray-800 bg-gray-950/60 p-3 space-y-3">
            <div className="flex flex-wrap gap-2">
              <FilterButton active={mode === 'all'} label="All Events" onClick={() => setMode('all')} />
              <FilterButton active={mode === 'trace'} label="Trace Only" onClick={() => setMode('trace')} />
              <FilterButton active={mode === 'runtime'} label="Runtime Only" onClick={() => setMode('runtime')} />
              <FilterButton active={!includeNoise} label="Hide Noise" onClick={() => setIncludeNoise(false)} />
              <FilterButton active={includeNoise} label="Show Noise" onClick={() => setIncludeNoise(true)} />
            </div>
            <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_14rem]">
              <input
                value={search}
                onChange={event => setSearch(event.target.value)}
                placeholder="Search text, stage, payload, or response..."
                className="rounded-xl border border-gray-800 bg-gray-900 px-3 py-2 text-sm text-gray-200 outline-none placeholder:text-gray-600 focus:border-emerald-700"
              />
              <select
                value={stageFilter}
                onChange={event => setStageFilter(event.target.value)}
                className="rounded-xl border border-gray-800 bg-gray-900 px-3 py-2 text-sm text-gray-200 outline-none focus:border-emerald-700"
              >
                {stageOptions.map(option => (
                  <option key={option} value={option}>
                    {option === 'all' ? 'All Stages' : option}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex flex-wrap gap-2">
              {Object.entries(laneCounts).map(([lane, count]) => (
                <span
                  key={lane}
                  className={`rounded-full border px-2.5 py-1 text-[10px] uppercase tracking-[0.16em] ${LANE_COLOR[lane] ?? LANE_COLOR.system}`}
                >
                  {lane} {count}
                </span>
              ))}
            </div>
          </div>

          <div ref={listRef} className="max-h-[72vh] overflow-y-auto rounded-2xl border border-gray-800 bg-gray-950/50 p-3">
            {filteredEvents.length === 0 ? (
              <p className="p-4 text-sm text-gray-500">No events match the current filters.</p>
            ) : (
              <div className="relative pl-5">
                <div className="absolute bottom-2 left-[0.58rem] top-2 w-px bg-gradient-to-b from-emerald-700/70 via-gray-800 to-transparent" />
                <div className="space-y-3">
                  {filteredEvents.map(event => {
                    const active = selectedEvent?.key === event.key;
                    const color =
                      STAGE_COLOR[event.stage]
                      ?? (event.type === 'pipeline_trace'
                        ? 'border-gray-700 bg-gray-900/70 text-gray-200'
                        : 'border-gray-800 bg-gray-900/40 text-gray-300');
                    return (
                      <button
                        key={event.key}
                        data-event-key={event.key}
                        onClick={() => {
                          setSelectedKey(event.key);
                          setFollowLive(false);
                        }}
                        className={`relative block w-full rounded-2xl border px-4 py-3 text-left transition-colors ${
                          active ? 'border-emerald-600 bg-gray-900' : 'border-gray-800 bg-gray-950/70 hover:bg-gray-900/80'
                        }`}
                      >
                        <span className={`absolute -left-[1.18rem] top-5 h-3 w-3 rounded-full border border-gray-950 ${active ? 'bg-emerald-400' : 'bg-gray-700'}`} />
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-mono text-[11px] text-gray-500">{formatTs(event.ts)}</span>
                          <span className="font-mono text-[11px] text-gray-600">{formatElapsed(event.ts, sessionOriginTs)}</span>
                          <span className={`rounded-full border px-2 py-1 text-[10px] uppercase tracking-[0.16em] ${LANE_COLOR[event.lane] ?? LANE_COLOR.system}`}>
                            {event.lane}
                          </span>
                          <span className={`rounded-full border px-2 py-1 text-[10px] uppercase tracking-[0.16em] ${color}`}>
                            {event.stage}
                          </span>
                          <span className="rounded-full border border-gray-800 px-2 py-1 text-[10px] uppercase tracking-[0.16em] text-gray-500">
                            {event.traceKind}
                          </span>
                          {event.segmentId != null && (
                            <span className="rounded-full border border-gray-800 px-2 py-1 text-[10px] text-gray-500">
                              segment {event.segmentId}
                            </span>
                          )}
                        </div>
                        <p className="mt-2 text-sm text-gray-200">{event.summary}</p>
                        {event.callId && (
                          <p className="mt-2 font-mono text-[11px] text-gray-600">{event.callId}</p>
                        )}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="xl:sticky xl:top-4 xl:self-start">
          <div className="max-h-[80vh] overflow-y-auto rounded-2xl border border-gray-800 bg-gray-950/70 p-4 space-y-4">
            {selectedEvent ? (
              <>
                <div className="space-y-3 rounded-2xl border border-gray-800 bg-gray-900/65 p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs text-gray-500">{formatTs(selectedEvent.ts)}</span>
                    <span className="font-mono text-xs text-gray-600">{formatElapsed(selectedEvent.ts, sessionOriginTs)}</span>
                    <span className={`rounded-full border px-2 py-1 text-[10px] uppercase tracking-[0.16em] ${LANE_COLOR[selectedEvent.lane] ?? LANE_COLOR.system}`}>
                      {selectedEvent.lane}
                    </span>
                    <span className={`rounded-full border px-2 py-1 text-[10px] uppercase tracking-[0.16em] ${STAGE_COLOR[selectedEvent.stage] ?? 'border-gray-800 bg-gray-900 text-gray-300'}`}>
                      {selectedEvent.stage}
                    </span>
                    <span className="rounded-full border border-gray-800 px-2 py-1 text-[10px] uppercase tracking-[0.16em] text-gray-500">
                      {selectedEvent.traceKind}
                    </span>
                  </div>
                  <p className="text-base font-medium text-white">{selectedEvent.summary}</p>
                  <div className="grid gap-2 sm:grid-cols-2">
                    <div className="rounded-xl border border-gray-800 bg-gray-950/70 px-3 py-2 text-xs text-gray-400">
                      <span className="text-gray-600">event type</span>
                      <p className="mt-1 text-gray-200">{selectedEvent.type}</p>
                    </div>
                    <div className="rounded-xl border border-gray-800 bg-gray-950/70 px-3 py-2 text-xs text-gray-400">
                      <span className="text-gray-600">segment</span>
                      <p className="mt-1 text-gray-200">{selectedEvent.segmentId ?? 'n/a'}</p>
                    </div>
                    <div className="rounded-xl border border-gray-800 bg-gray-950/70 px-3 py-2 text-xs text-gray-400 sm:col-span-2">
                      <span className="text-gray-600">call id</span>
                      <p className="mt-1 font-mono text-[11px] text-gray-200 break-all">{selectedEvent.callId ?? 'n/a'}</p>
                    </div>
                  </div>
                </div>

                {selectedEvent.detailSections.map(section => (
                  <DetailSection
                    key={`${selectedEvent.key}-${section.label}`}
                    label={section.label}
                    value={section.value}
                  />
                ))}
              </>
            ) : (
              <p className="text-sm text-gray-500">Select an event to inspect its full payload.</p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

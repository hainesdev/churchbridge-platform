'use client';
import { useState } from 'react';
import { useSessionStats, type SessionStats } from '@/lib/useSessionStats';
import { useEventLog } from '@/lib/useEventLog';
import { LatencyBars } from './LatencyBars';
import { EnrichmentHealth } from './EnrichmentHealth';
import { EventLog } from './EventLog';
import { PipelineTrace } from './PipelineTrace';
import { RemoteReports } from './RemoteReports';
import { getApiBaseUrl } from '@/lib/wsBaseUrl';

interface LiveViewProps {
  churchId: string;
}

export function LiveView({ churchId }: LiveViewProps) {
  const { stats, idle } = useSessionStats(churchId);
  const { events, connected } = useEventLog(churchId);
  const [captureLoading, setCaptureLoading] = useState(false);

  const toggleCapture = async () => {
    if (!stats) return;
    setCaptureLoading(true);
    try {
      const endpoint = stats.capture_active ? 'stop' : 'start';
      await fetch(
        `${getApiBaseUrl()}/api/churches/${encodeURIComponent(churchId)}/capture/${endpoint}`,
        { method: 'POST' }
      );
      // Stats will update on next poll (≤2s)
    } finally {
      setCaptureLoading(false);
    }
  };

  if (idle && !stats) {
    return (
      <div className="flex flex-col items-center justify-center py-24 space-y-3">
        <p className="text-gray-500 text-sm">No active session</p>
        <p className="text-gray-600 text-xs">Start a session from the Soundboard Admin to see live metrics</p>
        <div className="pt-4">
          <EventLog events={events} connected={connected} />
        </div>
      </div>
    );
  }

  const emptyLatency = {
    stt_to_sentence: { p50: null, p90: null, count: 0 },
    sentence_to_translation: { p50: null, p90: null, count: 0 },
    translation_to_enrichment: { p50: null, p90: null, count: 0 },
  };

  const emptyEnrichment: SessionStats['enrichment'] = {
    noisy_input_detected: 0,
    reconstruction_risk: 0,
    parse_failed: 0,
    parse_retry_success: 0,
    verse_suggestion_triggered: 0,
    verse_suggestion_gated: 0,
    fragment_merge_count: 0,
    long_sentence_handled_count: 0,
    merge_chain_max_length: 0,
    merge_requested: 0,
    merge_blocked_segment_structure: 0,
    merge_chain_opened: 0,
    merge_chain_extended: 0,
    merge_chain_closed: 0,
    repair_triggered: 0,
    repair_skipped_hidden_merge: 0,
    suppressed_translation_update: 0,
    suppressed_translation_update_for_merge: 0,
    deferred_release_emitted: 0,
    deferred_release_cancelled_for_merge: 0,
    display_suppressed_terminal_incomplete: 0,
    display_suppressed_incomplete: 0,
    display_suppressed_continuation: 0,
    display_suppressed_fragmented: 0,
    display_suppressed_quote_intro: 0,
    display_suppressed_llm_false: 0,
  };

  const emptyBuffer = {
    structural_flush_block_count: 0,
    forced_release_count: 0,
    conditional_flush_block_count: 0,
  };

  const latency = stats?.latency_ms ?? emptyLatency;
  const enrichment = stats?.enrichment ?? emptyEnrichment;
  const buffer = stats?.sentence_buffer ?? emptyBuffer;
  const noiseCount = stats?.stt_noise_removed_count ?? 0;

  return (
    <div className="space-y-4">
      {/* Session header */}
      <div className="rounded-2xl border border-gray-800 bg-gray-900 px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span
            className={`inline-flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full ${
              stats ? 'bg-green-900 text-green-300' : 'bg-gray-800 text-gray-500'
            }`}
          >
            <span className={`w-1.5 h-1.5 rounded-full ${stats ? 'bg-green-400' : 'bg-gray-600'}`} />
            {stats ? 'Active' : 'Loading…'}
          </span>
          {stats?.session_id != null && (
            <span className="text-xs text-gray-500">Session #{stats.session_id}</span>
          )}
        </div>
        <button
          onClick={toggleCapture}
          disabled={!stats || captureLoading}
          className={`text-xs font-medium px-3 py-1.5 rounded-lg transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
            stats?.capture_active
              ? 'bg-red-800 hover:bg-red-700 text-red-200'
              : 'bg-green-900 hover:bg-green-800 text-green-300'
          }`}
        >
          {captureLoading
            ? '…'
            : stats?.capture_active
            ? 'Stop Recording'
            : 'Start Recording'}
        </button>
      </div>

      {/* Latency */}
      <LatencyBars latency={latency} />

      {/* Enrichment + buffer health */}
      <EnrichmentHealth
        enrichment={enrichment}
        bufferCounts={buffer}
        noiseCount={noiseCount}
      />

      <PipelineTrace events={events} connected={connected} />

      {/* Event stream */}
      <EventLog events={events} connected={connected} />

      {/* Remote browser snapshots */}
      <RemoteReports churchId={churchId} />
    </div>
  );
}

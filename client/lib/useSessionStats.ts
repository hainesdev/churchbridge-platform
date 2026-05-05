'use client';
import { useEffect, useRef, useState } from 'react';
import { getApiBaseUrl } from '@/lib/wsBaseUrl';

export interface LatencyBucket {
  p50: number | null;
  p90: number | null;
  count: number;
}

export interface SessionStats {
  session_id: number | null;
  capture_active: boolean;
  sentence_buffer: {
    structural_flush_block_count: number;
    forced_release_count: number;
    conditional_flush_block_count: number;
  };
  enrichment: {
    noisy_input_detected: number;
    reconstruction_risk: number;
    parse_failed: number;
    parse_retry_success: number;
    verse_suggestion_triggered: number;
    verse_suggestion_gated: number;
    fragment_merge_count: number;
    long_sentence_handled_count: number;
    merge_chain_max_length: number;
    merge_requested: number;
    merge_blocked_segment_structure: number;
    merge_chain_opened: number;
    merge_chain_extended: number;
    merge_chain_closed: number;
    repair_triggered: number;
    repair_skipped_hidden_merge: number;
    suppressed_translation_update: number;
    suppressed_translation_update_for_merge: number;
    deferred_release_emitted: number;
    deferred_release_cancelled_for_merge: number;
    display_suppressed_terminal_incomplete: number;
    display_suppressed_incomplete: number;
    display_suppressed_continuation: number;
    display_suppressed_fragmented: number;
    display_suppressed_quote_intro: number;
    display_suppressed_llm_false: number;
  };
  stt_noise_removed_count: number;
  _enrichment_settled_size: number;
  latency_ms: {
    stt_to_sentence: LatencyBucket;
    sentence_to_translation: LatencyBucket;
    translation_to_enrichment: LatencyBucket;
  };
}

/**
 * Polls /api/churches/{churchId}/stats every intervalMs milliseconds.
 *
 * Returns:
 *   stats  — current stats, or null when loading / no active session
 *   idle   — true when the server returned 404 (no active session)
 */
export function useSessionStats(churchId: string, intervalMs = 2000) {
  const [stats, setStats] = useState<SessionStats | null>(null);
  const [idle, setIdle] = useState(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    const poll = async () => {
      try {
        const res = await fetch(
          `${getApiBaseUrl()}/api/churches/${encodeURIComponent(churchId)}/stats`
        );
        if (!res.ok) return;
        const data = await res.json();
        if (!data.active) {
          setStats(null);
          setIdle(true);
          return;
        }
        setStats(data);
        setIdle(false);
      } catch {
        // Keep stale data on transient network errors
      }
    };
    poll();
    timer.current = setInterval(poll, intervalMs);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [churchId, intervalMs]);

  return { stats, idle };
}

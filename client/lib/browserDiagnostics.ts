'use client';
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { getApiBaseUrl } from '@/lib/wsBaseUrl';

export type BrowserStreamStatus = 'idle' | 'connecting' | 'active' | 'reconnecting' | 'error';

export interface BrowserAudioDiagnostics {
  inputLevel: number;
  noiseFloor: number;
  clipping: boolean;
  speechDetected: boolean;
  batchesSent: number;
  lastBatchAt: number | null;
  audioContextSampleRate: number | null;
  requestedSampleRate: number;
  inputDeviceLabel: string;
  lastSpeechAt: number | null;
  appliedOptions: {
    noiseSuppression: boolean;
    echoCancellation: boolean;
    autoGainControl: boolean;
  } | null;
}

export interface BrowserStreamDebugState {
  socketOpenCount: number;
  socketCloseCount: number;
  reconnectCount: number;
  socketErrorCount: number;
  payloadsSent: number;
  bufferedChunkCount: number;
  bufferedSampleCount: number;
  lastSocketOpenAt: number | null;
  lastSocketCloseAt: number | null;
  lastSocketCloseCode: number | null;
  lastSocketCloseReason: string;
  lastSocketErrorAt: number | null;
  lastErrorMessage: string;
}

export interface BrowserFeedDebugState {
  displaySocketOpenCount: number;
  displaySocketCloseCount: number;
  displayReconnectCount: number;
  displaySocketErrorCount: number;
  totalEvents: number;
  lastEventType: string;
  lastEventAt: number | null;
  lastSocketOpenAt: number | null;
  lastSocketCloseAt: number | null;
  lastSocketCloseCode: number | null;
  lastSocketCloseReason: string;
  lastSocketErrorAt: number | null;
}

export interface BrowserDiagnosticsTelemetryInput {
  churchId: string;
  enabled: boolean;
  streamStatus: BrowserStreamStatus;
  streamError: string;
  displayConnected: boolean;
  audioDiagnostics: BrowserAudioDiagnostics;
  streamDebug: BrowserStreamDebugState;
  feedDebug: BrowserFeedDebugState;
  lastInterimAt: number | null;
  lastFinalAt: number | null;
  lastTranslationAt: number | null;
  lastInterimSpanish: string;
  lastFinalSpanish: string;
  lastCommittedEnglish: string;
}

export interface BrowserDiagnosticsTelemetryState {
  sessionKey: string;
  enabled: boolean;
  sending: boolean;
  lastPostedAt: number | null;
  lastPostStatus: string;
  lastPostError: string;
  warningFlags: string[];
  sendSnapshotNow: () => Promise<void>;
}

const SNAPSHOT_INTERVAL_MS = 12_000;
const MAX_TEXT_LEN = 180;

function trimText(value: string): string {
  const normalized = value.trim();
  if (normalized.length <= MAX_TEXT_LEN) return normalized;
  return `${normalized.slice(0, MAX_TEXT_LEN)}...`;
}

function ageMs(ts: number | null): number | null {
  return ts === null ? null : Math.max(0, Date.now() - ts);
}

function toFixedNumber(value: number, digits = 4): number {
  return Number(value.toFixed(digits));
}

export function createInitialStreamDebugState(): BrowserStreamDebugState {
  return {
    socketOpenCount: 0,
    socketCloseCount: 0,
    reconnectCount: 0,
    socketErrorCount: 0,
    payloadsSent: 0,
    bufferedChunkCount: 0,
    bufferedSampleCount: 0,
    lastSocketOpenAt: null,
    lastSocketCloseAt: null,
    lastSocketCloseCode: null,
    lastSocketCloseReason: '',
    lastSocketErrorAt: null,
    lastErrorMessage: '',
  };
}

export function createInitialFeedDebugState(): BrowserFeedDebugState {
  return {
    displaySocketOpenCount: 0,
    displaySocketCloseCount: 0,
    displayReconnectCount: 0,
    displaySocketErrorCount: 0,
    totalEvents: 0,
    lastEventType: '',
    lastEventAt: null,
    lastSocketOpenAt: null,
    lastSocketCloseAt: null,
    lastSocketCloseCode: null,
    lastSocketCloseReason: '',
    lastSocketErrorAt: null,
  };
}

export function useBrowserDiagnosticsTelemetry(
  input: BrowserDiagnosticsTelemetryInput,
): BrowserDiagnosticsTelemetryState {
  const telemetryInstanceId = useId().replace(/[:]/g, '');
  const sessionKeyRef = useRef(
    `browser-${input.churchId}-${telemetryInstanceId}`,
  );
  const [sending, setSending] = useState(false);
  const [lastPostedAt, setLastPostedAt] = useState<number | null>(null);
  const [lastPostStatus, setLastPostStatus] = useState('');
  const [lastPostError, setLastPostError] = useState('');
  const lastStreamStatusRef = useRef<BrowserStreamStatus>(input.streamStatus);
  const lastWarningSignatureRef = useRef('');
  const wasEnabledRef = useRef(input.enabled);

  const warningFlags = useMemo(() => {
    const flags: string[] = [];
    const finalAge = ageMs(input.lastFinalAt);
    const translationAge = ageMs(input.lastTranslationAt);
    const interimAge = ageMs(input.lastInterimAt);

    if (input.streamStatus === 'reconnecting') flags.push('stream_reconnecting');
    if (input.streamStatus === 'error') flags.push('stream_error');
    if (input.streamError.trim()) flags.push('stream_message');
    if (!input.displayConnected) flags.push('display_disconnected');
    if (input.audioDiagnostics.clipping) flags.push('input_clipping');
    if (
      input.audioDiagnostics.speechDetected &&
      input.audioDiagnostics.inputLevel < 0.015
    ) {
      flags.push('input_low');
    }
    if (input.audioDiagnostics.speechDetected && input.lastInterimAt === null) {
      flags.push('no_interim_seen');
    }
    if (interimAge !== null && finalAge !== null && finalAge < interimAge) {
      flags.push('stt_final_behind_interim');
    }
    if (finalAge !== null && finalAge > 15_000) flags.push('stt_stalled');
    if (translationAge !== null && translationAge > 18_000) flags.push('translation_stalled');
    if (input.streamDebug.bufferedChunkCount > 40) flags.push('buffer_backlog');
    return flags;
  }, [
    input.audioDiagnostics.clipping,
    input.audioDiagnostics.inputLevel,
    input.audioDiagnostics.speechDetected,
    input.displayConnected,
    input.lastFinalAt,
    input.lastInterimAt,
    input.lastTranslationAt,
    input.streamDebug.bufferedChunkCount,
    input.streamError,
    input.streamStatus,
  ]);

  const buildSnapshotPayload = useCallback(() => {
    const statusSummary =
      input.streamStatus === 'error'
        ? 'error'
        : warningFlags.length > 0
          ? 'warning'
          : input.streamStatus === 'idle'
            ? 'idle'
            : 'active';

    return {
      session_key: sessionKeyRef.current,
      summary_status: statusSummary,
      stream_status: input.streamStatus,
      display_connected: input.displayConnected,
      warning_flags: warningFlags,
      ages_ms: {
        last_interim: ageMs(input.lastInterimAt),
        last_final: ageMs(input.lastFinalAt),
        last_translation: ageMs(input.lastTranslationAt),
        last_batch: ageMs(input.audioDiagnostics.lastBatchAt),
        last_speech: ageMs(input.audioDiagnostics.lastSpeechAt),
        stream_socket_open: ageMs(input.streamDebug.lastSocketOpenAt),
        stream_socket_close: ageMs(input.streamDebug.lastSocketCloseAt),
        display_socket_open: ageMs(input.feedDebug.lastSocketOpenAt),
        display_socket_close: ageMs(input.feedDebug.lastSocketCloseAt),
      },
      audio: {
        ...input.audioDiagnostics,
        inputLevel: toFixedNumber(input.audioDiagnostics.inputLevel),
        noiseFloor: toFixedNumber(input.audioDiagnostics.noiseFloor),
      },
      stream: {
        ...input.streamDebug,
        error_message: trimText(input.streamError || input.streamDebug.lastErrorMessage),
      },
      display_feed: input.feedDebug,
      recent_text: {
        interim_spanish: trimText(input.lastInterimSpanish),
        final_spanish: trimText(input.lastFinalSpanish),
        committed_english: trimText(input.lastCommittedEnglish),
      },
    };
  }, [
    input.audioDiagnostics,
    input.displayConnected,
    input.feedDebug,
    input.lastCommittedEnglish,
    input.lastFinalAt,
    input.lastFinalSpanish,
    input.lastInterimAt,
    input.lastInterimSpanish,
    input.lastTranslationAt,
    input.streamDebug,
    input.streamError,
    input.streamStatus,
    warningFlags,
  ]);

  const postReport = useCallback(async (reportType: string, status: string) => {
    if (!input.enabled) return;

    setSending(true);
    setLastPostError('');
    try {
      const device = {
        kind: 'browser',
        session_key: sessionKeyRef.current,
        page: 'translation_test',
        user_agent: navigator.userAgent,
        language: navigator.language,
        url: window.location.href,
      };
      const app = {
        pathname: window.location.pathname,
        origin: window.location.origin,
      };
      const response = await fetch(
        `${getApiBaseUrl()}/api/churches/${encodeURIComponent(input.churchId)}/mobile-diagnostics/reports`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            report_type: reportType,
            status,
            payload: buildSnapshotPayload(),
            device,
            app,
          }),
        },
      );
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      setLastPostedAt(Date.now());
      setLastPostStatus(status);
    } catch (error) {
      setLastPostError(error instanceof Error ? error.message : 'Unknown telemetry error');
    } finally {
      setSending(false);
    }
  }, [buildSnapshotPayload, input.churchId, input.enabled]);

  useEffect(() => {
    if (input.enabled && !wasEnabledRef.current) {
      void postReport('browser_telemetry_enabled', 'info');
    }
    if (!input.enabled && wasEnabledRef.current) {
      setLastPostStatus('paused');
    }
    wasEnabledRef.current = input.enabled;
  }, [input.enabled, postReport]);

  useEffect(() => {
    if (!input.enabled) return;
    if (lastStreamStatusRef.current === input.streamStatus) return;

    const previous = lastStreamStatusRef.current;
    lastStreamStatusRef.current = input.streamStatus;
    void postReport(
      'browser_stream_status',
      input.streamStatus === 'error'
        ? 'error'
        : input.streamStatus === 'reconnecting'
          ? 'warning'
          : 'info',
    );

    if (previous !== 'idle' && input.streamStatus === 'idle') {
      void postReport('browser_session_stopped', 'info');
    }
    if (previous === 'idle' && input.streamStatus === 'active') {
      void postReport('browser_session_started', 'info');
    }
  }, [input.enabled, input.streamStatus, postReport]);

  useEffect(() => {
    if (!input.enabled) return;
    const warningSignature = warningFlags.join('|');
    if (!warningSignature) {
      lastWarningSignatureRef.current = '';
      return;
    }
    if (warningSignature === lastWarningSignatureRef.current) return;

    lastWarningSignatureRef.current = warningSignature;
    void postReport(
      'browser_warning',
      warningFlags.includes('stream_error') ? 'error' : 'warning',
    );
  }, [input.enabled, postReport, warningFlags]);

  useEffect(() => {
    if (!input.enabled) return;
    if (input.streamStatus === 'idle' && warningFlags.length === 0) return;

    const timer = window.setInterval(() => {
      void postReport(
        'browser_live_snapshot',
        warningFlags.length > 0 ? 'warning' : 'info',
      );
    }, SNAPSHOT_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [input.enabled, input.streamStatus, postReport, warningFlags]);

  const sendSnapshotNow = useCallback(async () => {
    await postReport(
      'browser_manual_snapshot',
      warningFlags.length > 0 ? 'warning' : 'info',
    );
  }, [postReport, warningFlags]);

  return {
    sessionKey: sessionKeyRef.current,
    enabled: input.enabled,
    sending,
    lastPostedAt,
    lastPostStatus,
    lastPostError,
    warningFlags,
    sendSnapshotNow,
  };
}

'use client';
import { useState, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { ModeTimeline, ModeTransition } from './ModeTimeline';
import { getApiBaseUrl } from '@/lib/wsBaseUrl';

export interface Session {
  id: number;
  started_at: string;
  ended_at: string | null;
  summary: string | null;
}

export interface Capture {
  id: number;
  session_id: number;
  audio_path: string;
  events_path: string;
  duration_s: number | null;
  segment_count: number | null;
  started_at: string;
  ended_at: string | null;
}

interface TranscriptSegment {
  spanish: string;
  english: string;
  ts: string;
}

interface VerseDetection {
  reference: string;
  confidence: string;
  spanish_text: string;
  canonical_english: string;
  segment_ts: number;
  detected_at: string;
}

interface SessionDetail {
  transcript: TranscriptSegment[];
  verses: VerseDetection[];
  transitions: ModeTransition[];
}

function formatDuration(startedAt: string, endedAt: string | null): string {
  const start = new Date(startedAt).getTime();
  const end = endedAt ? new Date(endedAt).getTime() : null;
  if (!end) return 'ongoing';
  const secs = Math.round((end - start) / 1000);
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return iso;
  }
}

interface SessionRowProps {
  session: Session;
  capture: Capture | undefined;
  churchId: string;
  expanded: boolean;
  onToggle: () => void;
}

export function SessionRow({ session, capture, churchId, expanded, onToggle }: SessionRowProps) {
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const fetched = useRef(false);

  const handleToggle = async () => {
    onToggle();
    if (!fetched.current) {
      fetched.current = true;
      const base = `${getApiBaseUrl()}/api/churches/${encodeURIComponent(churchId)}/sessions/${session.id}`;
      const [transcriptRes, versesRes, modesRes] = await Promise.all([
        fetch(`${base}/transcript`),
        fetch(`${base}/verses`),
        fetch(`${base}/modes`),
      ]);
      const [t, v, m] = await Promise.all([
        transcriptRes.ok ? transcriptRes.json() : { segments: [] },
        versesRes.ok ? versesRes.json() : { verses: [] },
        modesRes.ok ? modesRes.json() : { transitions: [] },
      ]);
      setDetail({
        transcript: t.segments ?? [],
        verses: v.verses ?? [],
        transitions: m.transitions ?? [],
      });
    }
  };

  return (
    <div className="border border-gray-800 rounded-2xl overflow-hidden">
      {/* Collapsed row */}
      <button
        onClick={handleToggle}
        className="w-full flex items-center justify-between px-4 py-3 hover:bg-gray-900 transition-colors text-left"
      >
        <div className="flex items-center gap-4">
          <span className="text-sm text-white font-medium">
            #{session.id}
          </span>
          <span className="text-xs text-gray-400">
            {formatTime(session.started_at)}
          </span>
          <span className="text-xs text-gray-500">
            {formatDuration(session.started_at, session.ended_at)}
          </span>
          {capture && (
            <span className="text-xs bg-sky-900 text-sky-300 px-2 py-0.5 rounded-full">
              {capture.segment_count ?? '?'} segments
            </span>
          )}
          {capture?.audio_path && (
            <span className="text-xs bg-green-900 text-green-300 px-2 py-0.5 rounded-full">
              audio captured
            </span>
          )}
        </div>
        <span className="text-gray-600 text-xs">{expanded ? '▲' : '▼'}</span>
      </button>

      {/* Expanded detail */}
      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="px-4 pb-4 space-y-4 border-t border-gray-800">
              {!detail ? (
                <p className="text-xs text-gray-500 pt-4">Loading…</p>
              ) : (
                <>
                  {/* Mode timeline */}
                  <div className="pt-4 space-y-1">
                    <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
                      Sermon Mode Timeline
                    </p>
                    <ModeTimeline
                      transitions={detail.transitions}
                      sessionStart={session.started_at}
                      sessionEnd={session.ended_at}
                    />
                  </div>

                  {/* Verse detections */}
                  {detail.verses.length > 0 && (
                    <div className="space-y-1">
                      <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
                        Verse Detections ({detail.verses.length})
                      </p>
                      <div className="space-y-1">
                        {detail.verses.map((v, i) => (
                          <div key={i} className="flex items-start gap-3 text-xs">
                            <span className="text-yellow-400 font-medium shrink-0">
                              {v.reference}
                            </span>
                            <span className={`shrink-0 px-1.5 py-0.5 rounded text-xs ${v.confidence === 'explicit' ? 'bg-yellow-900 text-yellow-300' : 'bg-gray-800 text-gray-400'}`}>
                              {v.confidence}
                            </span>
                            <span className="text-gray-500 line-clamp-1">{v.canonical_english}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Capture record */}
                  {capture && (
                    <div className="space-y-1">
                      <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
                        Capture
                      </p>
                      <div className="text-xs text-gray-400 space-y-0.5">
                        {capture.audio_path && (
                          <p><span className="text-gray-600">audio:</span> {capture.audio_path}</p>
                        )}
                        {capture.events_path && (
                          <p><span className="text-gray-600">events:</span> {capture.events_path}</p>
                        )}
                        {capture.duration_s != null && (
                          <p><span className="text-gray-600">duration:</span> {capture.duration_s.toFixed(1)}s</p>
                        )}
                      </div>
                    </div>
                  )}

                  {/* Transcript */}
                  {detail.transcript.length > 0 && (
                    <div className="space-y-1">
                      <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
                        Transcript ({detail.transcript.length} segments)
                      </p>
                      <div className="max-h-64 overflow-y-auto space-y-2">
                        {detail.transcript.map((seg, i) => (
                          <div key={i} className="grid grid-cols-2 gap-3 text-xs border-b border-gray-800 pb-2">
                            <p className="text-gray-400">{seg.spanish}</p>
                            <p className="text-gray-300">{seg.english}</p>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

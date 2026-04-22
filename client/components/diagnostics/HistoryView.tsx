'use client';
import { useEffect, useState } from 'react';
import { SessionRow, Session, Capture } from './SessionRow';
import { getApiBaseUrl } from '@/lib/wsBaseUrl';

interface HistoryViewProps {
  churchId: string;
}

export function HistoryView({ churchId }: HistoryViewProps) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [captureMap, setCaptureMap] = useState<Map<number, Capture>>(new Map());
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const base = `${getApiBaseUrl()}/api/churches/${encodeURIComponent(churchId)}`;
    const load = async () => {
      try {
        const [sessRes, capRes] = await Promise.all([
          fetch(`${base}/sessions?limit=50`),
          fetch(`${base}/captures?limit=100`),
        ]);
        const [sessData, capData] = await Promise.all([
          sessRes.ok ? sessRes.json() : { sessions: [] },
          capRes.ok ? capRes.json() : { captures: [] },
        ]);
        setSessions(sessData.sessions ?? []);
        const map = new Map<number, Capture>();
        for (const c of (capData.captures ?? []) as Capture[]) {
          // If multiple captures per session, keep the most recent (first in desc order)
          if (!map.has(c.session_id)) {
            map.set(c.session_id, c);
          }
        }
        setCaptureMap(map);
      } finally {
        setLoading(false);
      }
    };
    load();
  }, [churchId]);

  if (loading) {
    return <p className="text-sm text-gray-500 py-8 text-center">Loading…</p>;
  }

  if (sessions.length === 0) {
    return (
      <p className="text-sm text-gray-500 py-8 text-center">
        No past sessions found for this church.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between px-1">
        <p className="text-xs text-gray-600">{sessions.length} sessions</p>
        <p className="text-xs text-gray-600">
          {captureMap.size} with captures
        </p>
      </div>
      {sessions.map(session => (
        <SessionRow
          key={session.id}
          session={session}
          capture={captureMap.get(session.id)}
          churchId={churchId}
          expanded={expandedId === session.id}
          onToggle={() =>
            setExpandedId(prev => (prev === session.id ? null : session.id))
          }
        />
      ))}
    </div>
  );
}

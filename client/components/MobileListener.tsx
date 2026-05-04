'use client';
import { useEffect, useRef, useState } from 'react';
import { getWebSocketBaseUrl } from '@/lib/wsBaseUrl';

interface MobileListenerProps {
  churchId: string;
}

interface ListenerLine {
  id: number;
  text: string;
}

function messageSegmentId(msg: Record<string, unknown>): number | null {
  const raw = msg.segment_id ?? msg.ts;
  if (raw === undefined || raw === null) return null;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

export function MobileListener({ churchId }: MobileListenerProps) {
  const [lines, setLines] = useState<ListenerLine[]>([]);
  const [liveText, setLiveText] = useState('');
  const [liveSegmentId, setLiveSegmentId] = useState<number | null>(null);
  const [connected, setConnected] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const liveSegmentRef = useRef<number | null>(null);

  useEffect(() => {
    liveSegmentRef.current = liveSegmentId;
  }, [liveSegmentId]);

  useEffect(() => {
    let ws: WebSocket;
    let stopped = false;

    const clearLiveIfMatches = (segmentId: number | null) => {
      setLiveText(prev => {
        if (segmentId === null || liveSegmentRef.current === null || segmentId === liveSegmentRef.current) {
          return '';
        }
        return prev;
      });
      setLiveSegmentId(prev => {
        if (segmentId === null || prev === null || segmentId === prev) {
          return null;
        }
        return prev;
      });
    };

    const connect = () => {
      if (stopped) return;
      ws = new WebSocket(`${getWebSocketBaseUrl()}/api/listen/v1?church_id=${encodeURIComponent(churchId)}`);

      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        setTimeout(connect, 2000);
      };

      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);

        if (msg.type === 'live_translation') {
          setLiveText(String(msg.text ?? ''));
          setLiveSegmentId(messageSegmentId(msg));
          return;
        }

        if (msg.type === 'live_translation_clear') {
          clearLiveIfMatches(messageSegmentId(msg));
          return;
        }

        if (msg.type === 'feed_commit') {
          const segmentId = messageSegmentId(msg);
          if (segmentId === null) return;
          const english = String(msg.english ?? '');
          setLines(prev => {
            const existing = prev.some(line => line.id === segmentId);
            if (existing) {
              return prev.map(line => line.id === segmentId ? { ...line, text: english } : line);
            }
            return [...prev.slice(-50), { id: segmentId, text: english }];
          });
          clearLiveIfMatches(segmentId);
          return;
        }

        if (msg.type === 'feed_revision') {
          const segmentId = messageSegmentId(msg);
          if (segmentId === null) return;
          const english = String(msg.english ?? '');
          setLines(prev => {
            const existing = prev.some(line => line.id === segmentId);
            if (existing) {
              return prev.map(line => line.id === segmentId ? { ...line, text: english } : line);
            }
            return [...prev.slice(-50), { id: segmentId, text: english }];
          });
          return;
        }
      };
    };

    connect();
    return () => { stopped = true; ws?.close(); };
  }, [churchId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [lines, liveText]);

  return (
    <div className="min-h-screen bg-gray-950 text-white flex flex-col">
      <div className="flex-none bg-gray-900 px-4 py-3 flex items-center gap-2">
        <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-400 animate-pulse' : 'bg-gray-500'}`} />
        <span className="text-sm text-gray-400">
          {connected ? 'Live translation' : 'Connecting...'}
        </span>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
        {lines.length === 0 && !liveText && (
          <p data-testid="listener-empty-state" className="text-gray-600 text-center mt-12 text-base">
            Translation will appear here when the service begins.
          </p>
        )}

        {lines.map((line) => (
          <p key={line.id} data-testid="listener-committed-line" className="text-xl leading-relaxed text-gray-200">
            {line.text}
          </p>
        ))}

        {liveText && (
          <p data-testid="listener-live-line" className="text-xl leading-relaxed text-gray-400 italic">
            {liveText}
            <span className="animate-pulse ml-1">▌</span>
          </p>
        )}

        <div ref={bottomRef} />
      </div>
    </div>
  );
}


'use client';
import { useEffect, useState } from 'react';
import { getWebSocketBaseUrl } from '@/lib/wsBaseUrl';

export interface RawEvent {
  type: string;
  ts: number; // wall-clock ms from the event payload, or Date.now() as fallback
  raw: Record<string, unknown>;
}

const RING_SIZE = 30;

/**
 * Opens a WebSocket to /api/display/v1 and maintains a ring buffer of the
 * last RING_SIZE events. Lightweight alternative to useTranslationFeed —
 * only captures raw events, no translation state machine.
 */
export function useEventLog(churchId: string) {
  const [events, setEvents] = useState<RawEvent[]>([]);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let stopped = false;
    let ws: WebSocket;

    const connect = () => {
      if (stopped) return;
      ws = new WebSocket(
        `${getWebSocketBaseUrl()}/api/display/v1?church_id=${encodeURIComponent(churchId)}`
      );
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        setTimeout(connect, 2000);
      };
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data as string) as Record<string, unknown>;
          const entry: RawEvent = {
            type: String(msg.type ?? 'unknown'),
            ts: Number(msg.ts ?? Date.now()),
            raw: msg,
          };
          setEvents(prev => [...prev.slice(-(RING_SIZE - 1)), entry]);
        } catch {
          // Ignore malformed messages
        }
      };
    };

    connect();
    return () => {
      stopped = true;
      ws?.close();
    };
  }, [churchId]);

  return { events, connected };
}

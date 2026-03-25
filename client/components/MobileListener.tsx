'use client';
import { useEffect, useRef, useState } from 'react';

interface MobileListenerProps {
  churchId: string;
}

const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? 'ws://localhost:8000';

export function MobileListener({ churchId }: MobileListenerProps) {
  const [lines, setLines] = useState<string[]>([]);
  const [partial, setPartial] = useState('');
  const [connected, setConnected] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const connect = () => {
      const ws = new WebSocket(`${WS_URL}/api/listen/v1?church_id=${encodeURIComponent(churchId)}`);

      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        setTimeout(connect, 2000);
      };

      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === 'interim_translation') {
          setPartial(msg.text);
        } else if (msg.type === 'translation') {
          setLines((prev) => [...prev.slice(-50), msg.english]);
          setPartial('');
        }
      };
    };

    connect();
  }, [churchId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [lines, partial]);

  return (
    <div className="min-h-screen bg-gray-950 text-white flex flex-col">
      {/* Header */}
      <div className="flex-none bg-gray-900 px-4 py-3 flex items-center gap-2">
        <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-400 animate-pulse' : 'bg-gray-500'}`} />
        <span className="text-sm text-gray-400">
          {connected ? 'Live translation' : 'Connecting...'}
        </span>
      </div>

      {/* Translation lines */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
        {lines.length === 0 && !partial && (
          <p className="text-gray-600 text-center mt-12 text-base">
            Translation will appear here when the service begins.
          </p>
        )}

        {lines.map((line, i) => (
          <p key={i} className="text-xl leading-relaxed text-gray-200">
            {line}
          </p>
        ))}

        {partial && (
          <p className="text-xl leading-relaxed text-gray-400 italic">
            {partial}
            <span className="animate-pulse">▌</span>
          </p>
        )}

        <div ref={bottomRef} />
      </div>
    </div>
  );
}

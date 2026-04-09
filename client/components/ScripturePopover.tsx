'use client';

import { useEffect } from 'react';
import type { ScripturePassage } from '@/lib/useTranslationFeed';

interface ScripturePopoverProps {
  open: boolean;
  title: string;
  color: 'cited' | 'recommended';
  explanation?: string;
  sourcePassage?: ScripturePassage | null;
  displayPassage?: ScripturePassage | null;
  onClose: () => void;
}

function PassageBlock({ label, passage }: { label: string; passage?: ScripturePassage | null }) {
  if (!passage) return null;
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs uppercase tracking-wide text-gray-500">{label}</p>
        <span className="text-[11px] text-gray-400">{passage.version.name}</span>
      </div>
      <div className="rounded-lg border border-white/10 bg-black/20 p-3 space-y-2">
        <p className="text-sm font-semibold text-white">{passage.reference}</p>
        <div className="space-y-1">
          {passage.verses.map((verse) => (
            <p key={verse.reference} className="text-sm text-gray-200 leading-relaxed">
              <span className="text-gray-500 mr-2">{verse.verse}</span>
              {verse.text}
            </p>
          ))}
        </div>
      </div>
    </div>
  );
}

export function ScripturePopover({
  open,
  title,
  color,
  explanation,
  sourcePassage,
  displayPassage,
  onClose,
}: ScripturePopoverProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  const accent = color === 'cited' ? 'text-amber-300 border-amber-400/30' : 'text-sky-300 border-sky-400/30';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className={`w-full max-w-2xl rounded-2xl border bg-gray-950 shadow-2xl ${accent}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4 border-b border-white/10 px-5 py-4">
          <div>
            <p className={`text-sm font-semibold ${color === 'cited' ? 'text-amber-300' : 'text-sky-300'}`}>{title}</p>
            {explanation && <p className="mt-1 text-sm text-gray-300">{explanation}</p>}
          </div>
          <button
            onClick={onClose}
            className="rounded-md px-2 py-1 text-sm text-gray-400 hover:bg-white/10 hover:text-white"
          >
            Close
          </button>
        </div>

        <div className="grid gap-4 px-5 py-4 md:grid-cols-2">
          <PassageBlock label="Source" passage={sourcePassage} />
          <PassageBlock label="Display" passage={displayPassage} />
        </div>
      </div>
    </div>
  );
}

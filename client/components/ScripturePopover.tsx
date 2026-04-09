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
    <section className="min-h-0 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs uppercase tracking-wide text-gray-500">{label}</p>
        <span className="text-[11px] text-gray-400">{passage.version.name}</span>
      </div>
      <div className="max-h-[40vh] overflow-y-auto rounded-lg border border-white/10 bg-black/20 p-3 space-y-2 overscroll-contain">
        <p className="sticky top-0 z-10 -mx-3 -mt-3 border-b border-white/10 bg-gray-950/95 px-3 py-2 text-sm font-semibold text-white backdrop-blur">
          {passage.reference}
        </p>
        <div className="space-y-2">
          {passage.verses.map((verse) => (
            <p key={verse.reference} className="text-sm text-gray-200 leading-relaxed">
              <span className="text-gray-500 mr-2">{verse.verse}</span>
              {verse.text}
            </p>
          ))}
        </div>
      </div>
    </section>
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
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener('keydown', onKey);
    };
  }, [open, onClose]);

  if (!open) return null;

  const accent = color === 'cited' ? 'text-amber-300 border-amber-400/30' : 'text-sky-300 border-sky-400/30';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-3 sm:p-4" onClick={onClose}>
      <div
        className={`flex max-h-[calc(100vh-1.5rem)] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border bg-gray-950 shadow-2xl sm:max-h-[calc(100vh-2rem)] ${accent}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 z-20 flex items-start justify-between gap-4 border-b border-white/10 bg-gray-950/95 px-4 py-4 backdrop-blur sm:px-5">
          <div>
            <p className={`text-sm font-semibold ${color === 'cited' ? 'text-amber-300' : 'text-sky-300'}`}>{title}</p>
            {explanation && <p className="mt-1 text-sm text-gray-300">{explanation}</p>}
          </div>
          <button
            onClick={onClose}
            className="shrink-0 rounded-md px-2 py-1 text-sm text-gray-400 hover:bg-white/10 hover:text-white"
            aria-label="Close scripture popover"
          >
            Close
          </button>
        </div>

        <div className="min-h-0 overflow-y-auto px-4 py-4 sm:px-5">
          <div className="grid min-h-0 gap-4 md:grid-cols-2">
          <PassageBlock label="Source" passage={sourcePassage} />
          <PassageBlock label="Display" passage={displayPassage} />
          </div>
        </div>
      </div>
    </div>
  );
}

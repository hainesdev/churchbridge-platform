'use client';
import { motion, AnimatePresence } from 'framer-motion';
import type { VerseDetection, VerseSuggestion } from '@/lib/useTranslationFeed';

interface VersePanelProps {
  verses: VerseDetection[];
  suggestions: VerseSuggestion[];
}

export function VersePanel({ verses, suggestions }: VersePanelProps) {
  const isEmpty = verses.length === 0 && suggestions.length === 0;

  return (
    <AnimatePresence>
      {!isEmpty && (
        <motion.div
          key="verse-panel"
          initial={{ opacity: 0, x: 40 }}
          animate={{ opacity: 1, x: 0, transition: { duration: 0.3 } }}
          exit={{ opacity: 0, x: 40, transition: { duration: 0.2 } }}
          className="flex-none w-72 flex flex-col bg-gray-900/80 border-l border-gray-800 overflow-hidden"
        >
          <div className="flex-none px-4 py-3 border-b border-gray-800">
            <p className="text-xs font-semibold text-gray-400 uppercase tracking-wider">Scripture</p>
          </div>

          <div className="flex-1 overflow-y-auto px-4 py-3 space-y-5">

            {/* Detected verses */}
            {verses.length > 0 && (
              <div className="space-y-3">
                <p className="text-xs text-gray-500 uppercase tracking-wide">Cited</p>
                {verses.map((v, i) => (
                  <div key={i} className="space-y-1">
                    <div className="flex items-center gap-2">
                      <span className="text-amber-400 text-sm font-semibold">{v.reference}</span>
                      {v.confidence === 'quoted' && (
                        <span className="text-gray-600 text-xs italic">quoted</span>
                      )}
                    </div>
                    <p className="text-gray-300 text-xs leading-relaxed">{v.canonical_english}</p>
                  </div>
                ))}
              </div>
            )}

            {/* Suggestions */}
            <AnimatePresence mode="wait">
              {suggestions.length > 0 && (
                <motion.div
                  key={suggestions.map(s => s.reference).join(',')}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1, transition: { duration: 0.4 } }}
                  exit={{ opacity: 0, transition: { duration: 0.15 } }}
                  className="space-y-3"
                >
                  <p className="text-xs text-gray-500 uppercase tracking-wide">Related</p>
                  {suggestions.map((s, i) => (
                    <div key={i} className="space-y-1">
                      <span className="text-blue-400 text-sm font-semibold">{s.reference}</span>
                      <p className="text-gray-300 text-xs leading-relaxed">{s.canonical_english}</p>
                      <p className="text-gray-600 text-xs italic">{s.relevance_note}</p>
                    </div>
                  ))}
                </motion.div>
              )}
            </AnimatePresence>

          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

'use client';
import { SessionStats } from '@/lib/useSessionStats';

interface TileProps {
  label: string;
  value: number;
  alert?: boolean;
  warning?: boolean;
}

function Tile({ label, value, alert, warning }: TileProps) {
  const borderColor = alert
    ? 'border-red-800'
    : warning
    ? 'border-amber-800'
    : 'border-gray-800';

  return (
    <div className={`rounded-2xl border ${borderColor} bg-gray-900 p-3 space-y-1`}>
      <p className="text-xs text-gray-500">{label}</p>
      <p className={`text-2xl font-semibold tabular-nums ${alert ? 'text-red-400' : warning ? 'text-amber-400' : 'text-white'}`}>
        {value}
      </p>
    </div>
  );
}

interface EnrichmentHealthProps {
  enrichment: SessionStats['enrichment'];
  bufferCounts: SessionStats['sentence_buffer'];
  noiseCount: number;
}

export function EnrichmentHealth({ enrichment, bufferCounts, noiseCount }: EnrichmentHealthProps) {
  return (
    <div className="space-y-3">
      <div className="rounded-2xl border border-gray-800 bg-gray-900 p-4 space-y-3">
        <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
          Enrichment
        </h3>
        <div className="grid grid-cols-2 gap-2">
          <Tile
            label="Parse Failures"
            value={enrichment.parse_failed}
            alert={enrichment.parse_failed > 0}
          />
          <Tile
            label="Noisy Input"
            value={enrichment.noisy_input_detected}
            warning={enrichment.noisy_input_detected > 3}
          />
          <Tile
            label="Fragment Merges"
            value={enrichment.fragment_merge_count}
          />
          <Tile
            label="Verse Suggestions"
            value={enrichment.verse_suggestion_triggered}
          />
        </div>
      </div>

      <div className="rounded-2xl border border-gray-800 bg-gray-900 p-4 space-y-3">
        <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
          Buffer / STT
        </h3>
        <div className="grid grid-cols-3 gap-2">
          <Tile
            label="Struct. Blocks"
            value={bufferCounts.structural_flush_block_count}
          />
          <Tile
            label="Forced Releases"
            value={bufferCounts.forced_release_count}
          />
          <Tile
            label="STT Noise Removed"
            value={noiseCount}
          />
        </div>
      </div>
    </div>
  );
}

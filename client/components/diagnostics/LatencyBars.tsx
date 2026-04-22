'use client';
import { LatencyBucket } from '@/lib/useSessionStats';

interface LatencyRowProps {
  label: string;
  bucket: LatencyBucket;
  maxMs?: number;
}

function LatencyRow({ label, bucket, maxMs = 3000 }: LatencyRowProps) {
  const pct = (ms: number | null) =>
    ms == null ? 0 : Math.min(100, (ms / maxMs) * 100);

  const p90Color =
    bucket.p90 != null && bucket.p90 > 1500 ? 'bg-red-500' : 'bg-amber-400';

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-xs text-gray-400">{label}</span>
        <span className="text-xs text-gray-500">
          {bucket.p50 != null ? `${bucket.p50}ms` : '—'} p50
          {' / '}
          {bucket.p90 != null ? `${bucket.p90}ms` : '—'} p90
        </span>
      </div>
      <div className="space-y-0.5">
        <div className="h-1.5 rounded-full bg-gray-800">
          <div
            className="h-1.5 rounded-full bg-sky-500 transition-all duration-500"
            style={{ width: `${pct(bucket.p50)}%` }}
          />
        </div>
        <div className="h-1.5 rounded-full bg-gray-800">
          <div
            className={`h-1.5 rounded-full transition-all duration-500 ${p90Color}`}
            style={{ width: `${pct(bucket.p90)}%` }}
          />
        </div>
      </div>
      <p className="text-xs text-gray-600">{bucket.count} samples</p>
    </div>
  );
}

interface LatencyBarsProps {
  latency: {
    stt_to_sentence: LatencyBucket;
    sentence_to_translation: LatencyBucket;
    translation_to_enrichment: LatencyBucket;
  };
}

export function LatencyBars({ latency }: LatencyBarsProps) {
  return (
    <div className="rounded-2xl border border-gray-800 bg-gray-900 p-4 space-y-4">
      <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
        Pipeline Latency
      </h3>
      <LatencyRow label="STT → Sentence" bucket={latency.stt_to_sentence} />
      <LatencyRow label="Sentence → Translation" bucket={latency.sentence_to_translation} />
      <LatencyRow label="Translation → Enrichment" bucket={latency.translation_to_enrichment} />
      <p className="text-xs text-gray-600">
        Blue = p50 &nbsp;·&nbsp;
        <span className="text-amber-400">Amber</span> = p90 &nbsp;·&nbsp;
        <span className="text-red-500">Red</span> = p90 &gt; 1500ms
      </p>
    </div>
  );
}

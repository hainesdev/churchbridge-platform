'use client';

import type { BibleVersionOption } from '@/lib/useBibleVersions';

interface BibleVersionSelectorsProps {
  versions: BibleVersionOption[];
  sourceVersion: string;
  displayVersion: string;
  onSourceVersionChange: (value: string) => void;
  onDisplayVersionChange: (value: string) => void;
  disabled?: boolean;
}

export function BibleVersionSelectors({
  versions,
  sourceVersion,
  displayVersion,
  onSourceVersionChange,
  onDisplayVersionChange,
  disabled = false,
}: BibleVersionSelectorsProps) {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <label className="space-y-1">
        <span className="text-gray-400 text-xs font-medium uppercase tracking-wide">Source Scripture</span>
        <select
          value={sourceVersion}
          onChange={(e) => onSourceVersionChange(e.target.value)}
          disabled={disabled}
          className="w-full bg-gray-700 text-white text-sm rounded px-3 py-2 border border-gray-600 focus:border-amber-500 focus:outline-none disabled:opacity-60"
        >
          {versions.map((version) => (
            <option key={version.slug} value={version.slug}>
              {version.name} ({version.slug})
            </option>
          ))}
        </select>
      </label>

      <label className="space-y-1">
        <span className="text-gray-400 text-xs font-medium uppercase tracking-wide">Display Scripture</span>
        <select
          value={displayVersion}
          onChange={(e) => onDisplayVersionChange(e.target.value)}
          disabled={disabled}
          className="w-full bg-gray-700 text-white text-sm rounded px-3 py-2 border border-gray-600 focus:border-sky-500 focus:outline-none disabled:opacity-60"
        >
          {versions.map((version) => (
            <option key={version.slug} value={version.slug}>
              {version.name} ({version.slug})
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

'use client';

import { useEffect, useState } from 'react';
import { getApiBaseUrl } from '@/lib/wsBaseUrl';

export interface BibleVersionOption {
  slug: string;
  name: string;
  language_code: string;
}

export function useBibleVersions(churchId: string) {
  const [versions, setVersions] = useState<BibleVersionOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    async function run() {
      setLoading(true);
      setError('');
      try {
        const res = await fetch(`${getApiBaseUrl()}/api/churches/${encodeURIComponent(churchId)}/bibles`);
        if (!res.ok) throw new Error(`Failed to load Bible versions (${res.status})`);
        const data = await res.json() as { versions: BibleVersionOption[] };
        if (!cancelled) setVersions(data.versions);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load Bible versions');
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    run();
    return () => {
      cancelled = true;
    };
  }, [churchId]);

  return { versions, loading, error };
}

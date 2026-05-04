'use client';
import { useEffect, useState } from 'react';
import { getApiBaseUrl } from '@/lib/wsBaseUrl';

export interface DiagnosticsReport {
  church_id: string;
  command_id?: string | null;
  report_type: string;
  status: string;
  payload: Record<string, unknown>;
  device: Record<string, unknown>;
  app: Record<string, unknown>;
  received_at: string;
}

export function useDiagnosticsReports(churchId: string, limit = 20, intervalMs = 5_000) {
  const [reports, setReports] = useState<DiagnosticsReport[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const response = await fetch(
          `${getApiBaseUrl()}/api/churches/${encodeURIComponent(churchId)}/mobile-diagnostics/reports?limit=${limit}`,
        );
        if (!response.ok) return;
        const data = await response.json();
        if (!cancelled) {
          setReports(Array.isArray(data.reports) ? data.reports : []);
        }
      } catch {
        // Keep stale data on transient failures.
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    };

    void load();
    const timer = window.setInterval(load, intervalMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [churchId, intervalMs, limit]);

  return { reports, loading };
}

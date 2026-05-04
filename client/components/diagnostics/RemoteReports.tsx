'use client';
import { useMemo } from 'react';
import { useDiagnosticsReports, type DiagnosticsReport } from '@/lib/useDiagnosticsReports';

interface RemoteReportsProps {
  churchId: string;
}

function formatReceivedAt(value: string): string {
  try {
    return new Date(value).toLocaleTimeString();
  } catch {
    return value;
  }
}

function summarizeReport(report: DiagnosticsReport): string {
  const payload = report.payload ?? {};
  const warningFlags = Array.isArray(payload.warning_flags)
    ? (payload.warning_flags as string[])
    : [];
  const streamStatus =
    typeof payload.stream_status === 'string' ? payload.stream_status : 'unknown';
  const displayConnected =
    payload.display_connected === true ? 'display up' : 'display down';
  if (warningFlags.length > 0) {
    return `${streamStatus} | ${warningFlags.join(', ')}`;
  }
  return `${streamStatus} | ${displayConnected}`;
}

export function RemoteReports({ churchId }: RemoteReportsProps) {
  const { reports, loading } = useDiagnosticsReports(churchId, 25, 5_000);

  const browserReports = useMemo(
    () =>
      reports
        .filter(report => report.device?.kind === 'browser')
        .sort((a, b) => Date.parse(b.received_at) - Date.parse(a.received_at)),
    [reports],
  );
  const latest = browserReports[0];

  return (
    <div className="rounded-2xl border border-gray-800 bg-gray-900 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-500">
            Remote Browser Reports
          </h3>
          <p className="mt-1 text-xs text-gray-600">
            Open the real browser on <code>/test/{churchId}</code> and leave Browser
            {' '}
            Telemetry enabled.
          </p>
        </div>
        <span className="text-xs text-gray-600">
          {loading
            ? 'loading...'
            : `${browserReports.length} report${browserReports.length === 1 ? '' : 's'}`}
        </span>
      </div>

      {latest ? (
        <div className="rounded-2xl border border-gray-800 bg-gray-950/80 p-3 space-y-2">
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm font-medium text-white">{latest.report_type}</p>
            <span
              className={`text-xs ${
                latest.status === 'error'
                  ? 'text-red-300'
                  : latest.status === 'warning'
                    ? 'text-yellow-300'
                    : 'text-green-300'
              }`}
            >
              {latest.status}
            </span>
          </div>
          <p className="font-mono text-xs text-gray-400">
            {String(latest.device.session_key ?? 'unknown-session')}
          </p>
          <p className="text-xs text-gray-300">{summarizeReport(latest)}</p>
          <p className="text-xs text-gray-500">
            Received
            {' '}
            {formatReceivedAt(latest.received_at)}
          </p>
        </div>
      ) : (
        <p className="text-sm text-gray-500">No browser telemetry reports yet.</p>
      )}

      <div className="space-y-2">
        {browserReports.slice(0, 6).map((report, index) => (
          <div
            key={`${report.received_at}-${report.report_type}-${index}`}
            className="flex items-start justify-between gap-3 rounded-xl border border-gray-800 bg-gray-950/60 px-3 py-2"
          >
            <div className="min-w-0">
              <p className="text-xs font-medium text-white">{report.report_type}</p>
              <p className="mt-1 truncate text-xs text-gray-500">{summarizeReport(report)}</p>
            </div>
            <p className="shrink-0 text-xs text-gray-600">
              {formatReceivedAt(report.received_at)}
            </p>
          </div>
        ))}
      </div>
    </div>
  );
}

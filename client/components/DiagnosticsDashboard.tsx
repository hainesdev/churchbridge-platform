'use client';
import Link from 'next/link';
import { useState } from 'react';
import { LiveView } from './diagnostics/LiveView';
import { HistoryView } from './diagnostics/HistoryView';

type Tab = 'live' | 'history';

interface DiagnosticsDashboardProps {
  churchId: string;
}

export function DiagnosticsDashboard({ churchId }: DiagnosticsDashboardProps) {
  const [tab, setTab] = useState<Tab>('live');

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      <div className="max-w-7xl mx-auto px-4 py-6 space-y-5">
        <div className="flex items-baseline justify-between">
          <div>
            <h1 className="text-lg font-semibold">Diagnostics</h1>
            <p className="text-xs text-gray-500 mt-0.5">{churchId}</p>
          </div>
          <Link
            href="/"
            className="text-xs text-gray-600 hover:text-gray-400 transition-colors"
          >
            Home
          </Link>
        </div>

        <div className="flex gap-1 p-1 bg-gray-900 rounded-xl">
          {(['live', 'history'] as Tab[]).map(tabName => (
            <button
              key={tabName}
              onClick={() => setTab(tabName)}
              className={`flex-1 py-2 text-sm font-medium rounded-lg transition-colors capitalize ${
                tab === tabName
                  ? 'bg-gray-800 text-white'
                  : 'text-gray-500 hover:text-gray-300'
              }`}
            >
              {tabName}
            </button>
          ))}
        </div>

        {tab === 'live' ? (
          <LiveView churchId={churchId} />
        ) : (
          <HistoryView churchId={churchId} />
        )}
      </div>
    </div>
  );
}

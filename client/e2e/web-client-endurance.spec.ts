import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { installReplayHarness, readReplayMetrics } from './replayTestUtils';

const ENDURANCE_DURATION_MS = Number(process.env.CHURCHBRIDGE_ENDURANCE_MS ?? 180_000);
const POLL_INTERVAL_MS = 12_000;
const ENDURANCE_ENABLED = process.env.CHURCHBRIDGE_ENABLE_LONG_REPLAY === '1';

type DiagnosticsReport = {
  report_type?: string;
  status?: string;
  received_at?: string;
  payload?: {
    session_key?: string;
    stream_status?: string;
    display_connected?: boolean;
    warning_flags?: string[];
    ages_ms?: {
      last_interim?: number | null;
      last_final?: number | null;
      last_translation?: number | null;
      last_batch?: number | null;
    };
    stream?: {
      payloadsSent?: number;
    };
    recent_text?: {
      final_spanish?: string;
      committed_english?: string;
    };
  };
};

async function startReplay(page: Page, churchId: string): Promise<void> {
  await page.goto(`/test/${churchId}`);
  await page.getByRole('button', { name: 'Start Recording' }).click();
  await expect(page.getByRole('button', { name: 'End' })).toBeVisible({ timeout: 15_000 });
}

async function fetchReports(request: APIRequestContext, churchId: string): Promise<DiagnosticsReport[]> {
  const response = await request.get(`/api/churches/${churchId}/mobile-diagnostics/reports?limit=10`);
  expect(response.ok()).toBeTruthy();
  const body = (await response.json()) as { reports?: DiagnosticsReport[] };
  return body.reports ?? [];
}

test.describe('web client endurance', () => {
  test.skip(!ENDURANCE_ENABLED, 'Set CHURCHBRIDGE_ENABLE_LONG_REPLAY=1 to run the long endurance replay.');

  test('keeps STT and translation advancing during a long live browser replay', async ({ page, request }) => {
    test.setTimeout(ENDURANCE_DURATION_MS + 120_000);

    await installReplayHarness(page, { fakeMic: true, fakeMicLoop: true });

    const churchId = `playwright-endurance-${Date.now()}`;
    await startReplay(page, churchId);

    await page.waitForFunction(() => {
      return (window.__cbReplayMetrics.messageCounts.feed_commit ?? 0) >= 1;
    }, undefined, { timeout: 45_000 });

    const startedAt = Date.now();
    const samples: Array<{
      receivedAt: string;
      warningFlags: string[];
      streamStatus: string;
      lastBatchAgeMs: number | null;
      lastFinalAgeMs: number | null;
      lastTranslationAgeMs: number | null;
      payloadsSent: number;
      finalSpanish: string;
      committedEnglish: string;
    }> = [];
    let stallSample: (typeof samples)[number] | null = null;

    while (Date.now() - startedAt < ENDURANCE_DURATION_MS) {
      await page.waitForTimeout(POLL_INTERVAL_MS);
      const reports = await fetchReports(request, churchId);
      const latest = reports[0];
      if (!latest?.payload) {
        continue;
      }

      const warningFlags = latest.payload.warning_flags ?? [];
      const ages = latest.payload.ages_ms ?? {};
      const stream = latest.payload.stream ?? {};
      const recentText = latest.payload.recent_text ?? {};

      const sample = {
        receivedAt: latest.received_at ?? '',
        warningFlags,
        streamStatus: latest.payload.stream_status ?? '',
        lastBatchAgeMs: ages.last_batch ?? null,
        lastFinalAgeMs: ages.last_final ?? null,
        lastTranslationAgeMs: ages.last_translation ?? null,
        payloadsSent: stream.payloadsSent ?? 0,
        finalSpanish: recentText.final_spanish ?? '',
        committedEnglish: recentText.committed_english ?? '',
      };
      samples.push(sample);

      const audioIsStillFlowing =
        latest.payload.stream_status === 'active' &&
        (ages.last_batch ?? Number.POSITIVE_INFINITY) < 2_500 &&
        (stream.payloadsSent ?? 0) > 50;
      const stalled =
        warningFlags.includes('stt_stalled') ||
        warningFlags.includes('translation_stalled');

      expect.soft(latest.payload.display_connected).toBe(true);
      if (audioIsStillFlowing && stalled) {
        stallSample = sample;
        break;
      }
    }

    const metrics = await readReplayMetrics(page);
    const latestSample = samples.at(-1);

    await test.info().attach('web-client-endurance-reports', {
      body: JSON.stringify(samples, null, 2),
      contentType: 'application/json',
    });
    await test.info().attach('web-client-endurance-browser-metrics', {
      body: JSON.stringify(metrics, null, 2),
      contentType: 'application/json',
    });

    expect(samples.length).toBeGreaterThan(0);
    expect(metrics.messageCounts.feed_commit ?? 0).toBeGreaterThanOrEqual(1);
    expect(latestSample?.streamStatus).toBe('active');
    expect(stallSample).toBeNull();
    expect(latestSample?.warningFlags ?? []).not.toContain('stt_stalled');
    expect(latestSample?.warningFlags ?? []).not.toContain('translation_stalled');
  });
});

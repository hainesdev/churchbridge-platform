import { expect, test, type Page } from '@playwright/test';
import {
  closeLatestSocket,
  DRAIN_MS,
  installReplayHarness,
  readReplayMetrics,
  TEST_DURATION_MS,
} from './replayTestUtils';

async function startReplay(page: Page, churchId: string): Promise<void> {
  await page.goto(`/test/${churchId}`);
  await page.getByRole('button', { name: 'Start Recording' }).click();
  await expect(page.getByRole('button', { name: 'End' })).toBeVisible({ timeout: 15_000 });
}

test.describe('web client replay', () => {
  test('streams 60 seconds through the real browser client path', async ({ page }) => {
    test.setTimeout(150_000);
    await installReplayHarness(page, { fakeMic: true });

    await startReplay(page, process.env.CHURCHBRIDGE_TEST_CHURCH_ID ?? 'playwright-web-client-replay');
    await page.waitForTimeout(TEST_DURATION_MS + DRAIN_MS);

    const metrics = await readReplayMetrics(page);
    const streamSockets = metrics.socketRecords.filter((record) => record.kind === 'stream');
    const displaySockets = metrics.socketRecords.filter((record) => record.kind === 'display');

    await test.info().attach('web-client-replay-metrics', {
      body: JSON.stringify(metrics, null, 2),
      contentType: 'application/json',
    });

    expect(metrics.fakeMicStartedAtMs).not.toBeNull();
    expect(metrics.messageCounts.session_started ?? 0).toBeGreaterThanOrEqual(1);
    expect(metrics.messageCounts.feed_commit ?? 0).toBeGreaterThanOrEqual(3);
    expect(metrics.messageCounts.live_translation ?? 0).toBeGreaterThanOrEqual(1);
    expect(streamSockets.length).toBeGreaterThanOrEqual(1);
    expect(displaySockets.length).toBeGreaterThanOrEqual(1);
    expect(streamSockets.filter((record) => record.closedAtMs !== null)).toHaveLength(0);
    expect(displaySockets.filter((record) => record.closedAtMs !== null)).toHaveLength(0);
    expect(
      metrics.consoleErrors.filter((message) => !message.includes('Failed to load resource')),
    ).toHaveLength(0);
    expect(metrics.pageErrors).toHaveLength(0);
  });

  test('reconnects the stream socket and continues translating', async ({ page }) => {
    test.setTimeout(120_000);
    await installReplayHarness(page, { fakeMic: true });

    const churchId = `${process.env.CHURCHBRIDGE_TEST_CHURCH_ID ?? 'playwright-stream-reconnect'}-${Date.now()}`;
    await startReplay(page, churchId);

    await page.waitForFunction(() => {
      return (window.__cbReplayMetrics.messageCounts.feed_commit ?? 0) >= 1;
    }, undefined, { timeout: 45_000 });

    const preDropMetrics = await readReplayMetrics(page);
    const preDropCommitCount = preDropMetrics.messageCounts.feed_commit ?? 0;

    expect(await closeLatestSocket(page, 'stream')).toBe(true);

    await page.waitForFunction(() => {
      const metrics = window.__cbReplayMetrics;
      const streamSockets = metrics.socketRecords.filter((record) => record.kind === 'stream');
      return (
        streamSockets.length >= 2 &&
        (metrics.messageCounts.session_started ?? 0) >= 2
      );
    }, undefined, { timeout: 45_000 });

    await page.waitForFunction((beforeCount) => {
      return (window.__cbReplayMetrics.messageCounts.feed_commit ?? 0) >= beforeCount + 1;
    }, preDropCommitCount, { timeout: 45_000 });

    const metrics = await readReplayMetrics(page);
    const streamSockets = metrics.socketRecords.filter((record) => record.kind === 'stream');

    await test.info().attach('web-client-stream-reconnect-metrics', {
      body: JSON.stringify(metrics, null, 2),
      contentType: 'application/json',
    });

    expect(metrics.messageCounts.session_started ?? 0).toBeGreaterThanOrEqual(2);
    expect(metrics.messageCounts.feed_commit ?? 0).toBeGreaterThanOrEqual(preDropCommitCount + 1);
    expect(streamSockets.some((record) => record.closedAtMs !== null)).toBe(true);
    expect(streamSockets.some((record) => record.messageCount > 0 && record.closedAtMs === null)).toBe(true);
    expect(metrics.pageErrors).toHaveLength(0);
  });

  test('reconnects the display socket and keeps rendering translations', async ({ page }) => {
    test.setTimeout(120_000);
    await installReplayHarness(page, { fakeMic: true });

    const churchId = `${process.env.CHURCHBRIDGE_TEST_CHURCH_ID ?? 'playwright-display-reconnect'}-${Date.now()}`;
    await startReplay(page, churchId);

    await page.waitForFunction(() => {
      return (window.__cbReplayMetrics.messageCounts.feed_commit ?? 0) >= 1;
    }, undefined, { timeout: 45_000 });

    const preDropMetrics = await readReplayMetrics(page);
    const preDropCommitCount = preDropMetrics.messageCounts.feed_commit ?? 0;

    expect(await closeLatestSocket(page, 'display')).toBe(true);

    await page.waitForFunction(() => {
      const displaySockets = window.__cbReplayMetrics.socketRecords.filter((record) => record.kind === 'display');
      return displaySockets.length >= 2;
    }, undefined, { timeout: 45_000 });

    await page.waitForFunction((beforeCount) => {
      return (window.__cbReplayMetrics.messageCounts.feed_commit ?? 0) >= beforeCount + 1;
    }, preDropCommitCount, { timeout: 45_000 });

    const metrics = await readReplayMetrics(page);
    const displaySockets = metrics.socketRecords.filter((record) => record.kind === 'display');

    await test.info().attach('web-client-display-reconnect-metrics', {
      body: JSON.stringify(metrics, null, 2),
      contentType: 'application/json',
    });

    expect(displaySockets.some((record) => record.closedAtMs !== null)).toBe(true);
    expect(displaySockets.some((record) => record.messageCount > 0 && record.closedAtMs === null)).toBe(true);
    expect(metrics.messageCounts.feed_commit ?? 0).toBeGreaterThanOrEqual(preDropCommitCount + 1);
    expect(metrics.pageErrors).toHaveLength(0);
  });
});

import { expect, test, type Page } from '@playwright/test';
import { installReplayHarness, readReplayMetrics } from './replayTestUtils';

async function startReplay(page: Page, churchId: string): Promise<void> {
  await page.goto(`/test/${churchId}`);
  await page.getByRole('button', { name: 'Start Recording' }).click();
  await expect(page.getByRole('button', { name: 'End' })).toBeVisible({ timeout: 15_000 });
}

async function expectVisibleListenerTranslation(page: Page): Promise<void> {
  await expect.poll(async () => {
    const texts = await page.getByTestId('listener-committed-line').allInnerTexts();
    return texts.some((text) => text.trim().length >= 12);
  }).toBe(true);
}

async function expectLiveOrCommittedListenerText(page: Page): Promise<void> {
  await expect.poll(async () => {
    const liveTexts = await page.getByTestId('listener-live-line').allInnerTexts();
    const committedTexts = await page.getByTestId('listener-committed-line').allInnerTexts();
    return [...liveTexts, ...committedTexts].some((text) => text.trim().length >= 12);
  }).toBe(true);
}

test.describe('mobile listener replay', () => {
  test('receives live and committed translation events on the listener socket', async ({ browser }) => {
    test.setTimeout(120_000);

    const churchId = `${process.env.CHURCHBRIDGE_TEST_CHURCH_ID ?? 'playwright-mobile-listener'}-${Date.now()}`;
    const listenerPage = await browser.newPage();
    const recordingPage = await browser.newPage();

    await installReplayHarness(listenerPage);
    await installReplayHarness(recordingPage, { fakeMic: true });

    await listenerPage.goto(`/listen/${churchId}`);
    await startReplay(recordingPage, churchId);

    await listenerPage.waitForFunction(() => {
      const counts = window.__cbReplayMetrics.messageCounts;
      return (counts.live_translation ?? 0) >= 1 && (counts.feed_commit ?? 0) >= 1;
    }, undefined, { timeout: 60_000 });

    await expect(listenerPage.getByTestId('listener-empty-state')).toHaveCount(0);
    await expectLiveOrCommittedListenerText(listenerPage);
    await expectVisibleListenerTranslation(listenerPage);

    const listenerMetrics = await readReplayMetrics(listenerPage);
    const recordingMetrics = await readReplayMetrics(recordingPage);

    await test.info().attach('mobile-listener-metrics', {
      body: JSON.stringify({ listenerMetrics, recordingMetrics }, null, 2),
      contentType: 'application/json',
    });

    expect(listenerMetrics.messageCounts.live_translation ?? 0).toBeGreaterThanOrEqual(1);
    expect(listenerMetrics.messageCounts.feed_commit ?? 0).toBeGreaterThanOrEqual(1);
    expect(listenerMetrics.socketRecords.filter((record) => record.kind === 'listen')).toHaveLength(1);
    expect(listenerMetrics.pageErrors).toHaveLength(0);

    await listenerPage.close();
    await recordingPage.close();
  });
});

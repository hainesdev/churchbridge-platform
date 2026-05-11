import { expect, test, type Page } from '@playwright/test';

async function installDisplaySocketHarness(page: Page): Promise<void> {
  await page.addInitScript(() => {
    type DisplayMessage = Record<string, unknown>;
    const listeners = new Set<(message: DisplayMessage) => void>();
    window.__cbDisplayHarness = {
      listenerCount() {
        return listeners.size;
      },
      dispatch(payload: DisplayMessage) {
        for (const listener of listeners) {
          listener(payload);
        }
      },
      subscribe(listener: (message: DisplayMessage) => void) {
        listeners.add(listener);
        return () => {
          listeners.delete(listener);
        };
      },
    };
    window.__cbDisplayTestHarness = {
      subscribe(listener: (message: DisplayMessage) => void) {
        return window.__cbDisplayHarness.subscribe(listener);
      },
    };
  });
}

async function openDisplay(page: Page, churchId: string): Promise<void> {
  await page.goto(`/display/${churchId}?mode=full`);
  await page.waitForFunction(() => window.__cbDisplayHarness.listenerCount() > 0);
}

async function dispatchDisplayMessage(page: Page, payload: Record<string, unknown>): Promise<void> {
  await page.evaluate((message) => {
    window.__cbDisplayHarness.dispatch(message);
  }, payload);
}

test.describe('display chunk continuity', () => {
  test('keeps a locked phrase active through adjacent-merge lineage revisions', async ({ page }) => {
    await installDisplaySocketHarness(page);
    await openDisplay(page, `playwright-display-lineage-${Date.now()}`);

    await dispatchDisplayMessage(page, {
      type: 'feed_commit',
      segment_id: 1000,
      ts: 1000,
      english: "because he lived with Christ, didn't he?",
      spanish: 'porque él convivió con Cristo, ¿no es cierto?',
      root_segment_id: 1000,
      merged_from_segment_ids: [1000],
      phrase_alignment: [
        {
          chunk_id: 'seg1000-v1-c1',
          english_text: 'because he lived with Christ',
          spanish_text: 'porque él convivió con Cristo',
          english_span: { start: 0, end: 28 },
          spanish_span: { start: 0, end: 29 },
          ordinal: 0,
          derived_from_chunk_ids: [],
          remap_decision: 'fresh',
          ambiguity_reason: null,
        },
        {
          chunk_id: 'seg1000-v1-c2',
          english_text: "didn't he?",
          spanish_text: '¿no es cierto?',
          english_span: { start: 30, end: 40 },
          spanish_span: { start: 31, end: 44 },
          ordinal: 1,
          derived_from_chunk_ids: [],
          remap_decision: 'fresh',
          ambiguity_reason: null,
        },
      ],
    });

    const originalPhrase = page.locator('[data-chunk-id="seg1000-v1-c2"]').first();
    await expect(originalPhrase).toHaveAttribute('aria-pressed', 'false');
    await originalPhrase.click();
    await expect(originalPhrase).toHaveAttribute('aria-pressed', 'true');

    await dispatchDisplayMessage(page, {
      type: 'feed_revision',
      segment_id: 1000,
      ts: 1000,
      english: "because he lived with Christ, didn't he?",
      spanish: 'porque él convivió con Cristo, ¿no es cierto?',
      source: 'llm',
      reason: 'phrase_alignment',
      alignment_version: 2,
      previous_alignment_version: 1,
      root_segment_id: 1000,
      merged_from_segment_ids: [1000],
      phrase_alignment: [
        {
          chunk_id: 'seg1000-v2-c1',
          english_text: "because he lived with Christ, didn't he?",
          spanish_text: 'porque él convivió con Cristo, ¿no es cierto?',
          english_span: { start: 0, end: 40 },
          spanish_span: { start: 0, end: 44 },
          ordinal: 0,
          derived_from_chunk_ids: ['seg1000-v1-c1', 'seg1000-v1-c2'],
          remap_decision: 'lineage_only',
          ambiguity_reason: 'adjacent_merge',
        },
      ],
    });

    const mergedPhrase = page.locator('[data-chunk-id="seg1000-v2-c1"]').first();
    await expect(mergedPhrase).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('[data-chunk-id="seg1000-v1-c2"]')).toHaveCount(0);
  });

  test('does not transfer a locked phrase through ambiguous lineage', async ({ page }) => {
    await installDisplaySocketHarness(page);
    await openDisplay(page, `playwright-display-ambiguity-${Date.now()}`);

    await dispatchDisplayMessage(page, {
      type: 'feed_commit',
      segment_id: 1000,
      ts: 1000,
      english: 'Alpha beta. Gamma delta.',
      spanish: 'Uno dos. Tres cuatro.',
      root_segment_id: 1000,
      merged_from_segment_ids: [1000],
      phrase_alignment: [
        {
          chunk_id: 'seg1000-v1-c1',
          english_text: 'Alpha beta',
          spanish_text: 'Uno dos',
          english_span: { start: 0, end: 10 },
          spanish_span: { start: 0, end: 7 },
          ordinal: 0,
          derived_from_chunk_ids: [],
          remap_decision: 'fresh',
          ambiguity_reason: null,
        },
        {
          chunk_id: 'seg1000-v1-c2',
          english_text: 'Gamma delta',
          spanish_text: 'Tres cuatro',
          english_span: { start: 12, end: 23 },
          spanish_span: { start: 9, end: 20 },
          ordinal: 1,
          derived_from_chunk_ids: [],
          remap_decision: 'fresh',
          ambiguity_reason: null,
        },
      ],
    });

    const originalPhrase = page.locator('[data-chunk-id="seg1000-v1-c1"]').first();
    await originalPhrase.click();
    await expect(originalPhrase).toHaveAttribute('aria-pressed', 'true');

    await dispatchDisplayMessage(page, {
      type: 'feed_revision',
      segment_id: 1000,
      ts: 1000,
      english: 'Alpha beta gamma delta.',
      spanish: 'Uno dos tres cuatro.',
      source: 'llm',
      reason: 'phrase_alignment',
      alignment_version: 2,
      previous_alignment_version: 1,
      root_segment_id: 1000,
      merged_from_segment_ids: [1000],
      phrase_alignment: [
        {
          chunk_id: 'seg1000-v2-c1',
          english_text: 'Alpha beta gamma delta',
          spanish_text: 'Uno dos tres cuatro',
          english_span: { start: 0, end: 22 },
          spanish_span: { start: 0, end: 19 },
          ordinal: 0,
          derived_from_chunk_ids: ['seg1000-v1-c1', 'seg1000-v1-c2'],
          remap_decision: 'lineage_only',
          ambiguity_reason: 'close_competition',
        },
      ],
    });

    const revisedPhrase = page.locator('[data-chunk-id="seg1000-v2-c1"]').first();
    await expect(revisedPhrase).toHaveAttribute('aria-pressed', 'false');
    await expect(page.locator('[data-chunk-id][aria-pressed="true"]')).toHaveCount(0);
  });
});

declare global {
  interface Window {
    __cbDisplayHarness: {
      listenerCount: () => number;
      dispatch: (payload: Record<string, unknown>) => void;
      subscribe: (listener: (payload: Record<string, unknown>) => void) => () => void;
    };
    __cbDisplayTestHarness?: {
      subscribe: (listener: (payload: Record<string, unknown>) => void) => () => void;
    };
  }
}

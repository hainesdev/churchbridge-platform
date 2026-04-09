import test from 'node:test';
import assert from 'node:assert/strict';

import { attachVerseToVisibleSegment, resolveMergedSegmentId } from './mergedVerseRouting.ts';

test('resolveMergedSegmentId follows merge chains to the visible kept segment', () => {
  const mergeMap = new Map([
    [200, 100],
    [300, 200],
  ]);

  assert.equal(resolveMergedSegmentId(mergeMap, 300), 100);
  assert.equal(resolveMergedSegmentId(mergeMap, 200), 100);
  assert.equal(resolveMergedSegmentId(mergeMap, 100), 100);
});

test('attachVerseToVisibleSegment updates the kept segment after a late verse event', () => {
  const verse = {
    book: '1 John',
    chapter: 1,
    verse_start: 6,
    verse_end: 7,
    spanish_text: 'si decimos que tenemos comunion con el',
    canonical_english: 'if we say we have fellowship with him',
    reference: '1 John 1:6-7',
    confidence: 'quoted' as const,
  };

  const segments = [
    { id: 100, spanish: 'kept', english: 'kept english' },
    { id: 300, spanish: 'absorbed', english: 'absorbed english' },
  ];
  const mergeMap = new Map([[300, 100]]);
  const targetTs = resolveMergedSegmentId(mergeMap, 300);
  const next = attachVerseToVisibleSegment(segments, targetTs, verse);

  assert.deepEqual(next, [
    { id: 100, spanish: 'kept', english: 'kept english', verseDetected: verse },
    { id: 300, spanish: 'absorbed', english: 'absorbed english' },
  ]);
});

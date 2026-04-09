export function resolveMergedSegmentId(mergeMap: Map<number, number>, ts: number): number {
  let current = ts;
  const seen = new Set<number>();
  while (mergeMap.has(current) && !seen.has(current)) {
    seen.add(current);
    current = mergeMap.get(current)!;
  }
  return current;
}

export function attachVerseToVisibleSegment<
  V,
  T extends { id: number; verseDetected?: V }
>(
  prev: T[],
  ts: number,
  verse: V,
): T[] {
  return prev.map(segment =>
    segment.id === ts ? { ...segment, verseDetected: verse } : segment
  );
}

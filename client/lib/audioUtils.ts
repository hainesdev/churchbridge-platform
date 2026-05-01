/**
 * Merge Float32Array chunks and encode as base64.
 * Used by both SoundboardAdmin and MobileTest to batch-send audio over WebSocket.
 */
export function float32ToBase64(chunks: Float32Array[]): string {
  const totalLen = chunks.reduce((s, c) => s + c.length, 0);
  const merged = new Float32Array(totalLen);
  let offset = 0;
  for (const c of chunks) { merged.set(c, offset); offset += c.length; }
  const bytes = new Uint8Array(merged.buffer, merged.byteOffset, merged.byteLength);
  let binary = '';
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

/**
 * Split buffered Float32 samples into bounded base64 payloads so reconnect
 * recovery cannot dump an oversized audio frame into the backend.
 */
export function float32ChunksToBase64Payloads(
  chunks: Float32Array[],
  maxSamplesPerPayload = 6_000,
): string[] {
  const payloads: string[] = [];
  let pending: Float32Array[] = [];
  let pendingSamples = 0;

  const flushPending = () => {
    if (!pendingSamples) return;
    payloads.push(float32ToBase64(pending));
    pending = [];
    pendingSamples = 0;
  };

  for (const chunk of chunks) {
    let offset = 0;
    while (offset < chunk.length) {
      const remainingCapacity = maxSamplesPerPayload - pendingSamples;
      const take = Math.min(remainingCapacity, chunk.length - offset);
      pending.push(chunk.subarray(offset, offset + take));
      pendingSamples += take;
      offset += take;

      if (pendingSamples >= maxSamplesPerPayload) {
        flushPending();
      }
    }
  }

  flushPending();
  return payloads;
}

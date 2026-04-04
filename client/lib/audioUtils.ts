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

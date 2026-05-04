import fs from 'node:fs';
import path from 'node:path';
import type { Page } from '@playwright/test';

export const TEST_DURATION_MS = 60_000;
export const DRAIN_MS = 12_000;

export type SocketKind = 'stream' | 'display' | 'listen' | 'other';

export type SocketRecord = {
  id: number;
  kind: SocketKind;
  url: string;
  openedAtMs: number | null;
  closedAtMs: number | null;
  closeCode: number | null;
  closeReason: string;
  messageCount: number;
  sessionStartedCount: number;
  errorMessages: string[];
};

export type ReplayMetrics = {
  fakeMicRequestedAtMs: number | null;
  fakeMicStartedAtMs: number | null;
  fakeMicEndedAtMs: number | null;
  messageCounts: Record<string, number>;
  socketRecords: SocketRecord[];
  consoleErrors: string[];
  pageErrors: string[];
};

function resolveFakeAudioFile(): string {
  const envPath = process.env.CHURCHBRIDGE_FAKE_AUDIO_FILE;
  if (envPath) {
    return path.resolve(envPath);
  }

  const audioDir = path.resolve(__dirname, '..', '..', 'tests', 'audio', '1');
  const candidate = fs
    .readdirSync(audioDir)
    .filter((name) => name.endsWith('.mp3') || name.endsWith('.wav'))
    .sort()[0];

  if (!candidate) {
    throw new Error(`No fake audio file found in ${audioDir}`);
  }
  return path.join(audioDir, candidate);
}

export function getFakeAudioBase64(): string {
  return fs.readFileSync(resolveFakeAudioFile()).toString('base64');
}

export async function installReplayHarness(
  page: Page,
  options: { fakeMic?: boolean; fakeMicLoop?: boolean } = {},
): Promise<void> {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const fakeMic = options.fakeMic ?? false;
  const fakeMicLoop = options.fakeMicLoop ?? false;
  const base64Audio = fakeMic ? getFakeAudioBase64() : null;

  page.on('console', (message) => {
    if (message.type() === 'error') {
      consoleErrors.push(message.text());
    }
  });
  page.on('pageerror', (error) => {
    pageErrors.push(String(error));
  });

  await page.addInitScript(
    ({ base64Audio: injectedAudio, fakeMic: enableFakeMic, fakeMicLoop: shouldLoopFakeMic }) => {
      const metrics: ReplayMetrics = {
        fakeMicRequestedAtMs: null,
        fakeMicStartedAtMs: null,
        fakeMicEndedAtMs: null,
        messageCounts: {},
        socketRecords: [],
        consoleErrors: [],
        pageErrors: [],
      };
      let nextSocketId = 1;
      const trackedSockets: Array<{ kind: string; ws: WebSocket }> = [];

      const classifySocket = (url: string): SocketKind => {
        if (url.includes('/api/stream/')) return 'stream';
        if (url.includes('/api/display/')) return 'display';
        if (url.includes('/api/listen/')) return 'listen';
        return 'other';
      };

      window.__cbReplayMetrics = metrics;
      window.__cbReplayControl = {
        closeLatestSocket(kind: SocketKind, code = 4100, reason = 'playwright-close') {
          for (let index = trackedSockets.length - 1; index >= 0; index -= 1) {
            const socket = trackedSockets[index];
            if (!socket || socket.kind !== kind) continue;
            if (socket.ws.readyState !== socket.ws.OPEN) continue;
            socket.ws.close(code, reason);
            return true;
          }
          return false;
        },
      };

      const NativeWebSocket = window.WebSocket;
      class InstrumentedWebSocket extends NativeWebSocket {
        constructor(url: string | URL, protocols?: string | string[]) {
          super(url, protocols);
          const urlString = String(url);
          const record: SocketRecord = {
            id: nextSocketId,
            kind: classifySocket(urlString),
            url: urlString,
            openedAtMs: null,
            closedAtMs: null,
            closeCode: null,
            closeReason: '',
            messageCount: 0,
            sessionStartedCount: 0,
            errorMessages: [],
          };
          nextSocketId += 1;
          trackedSockets.push({ kind: record.kind, ws: this });
          metrics.socketRecords.push(record);

          this.addEventListener('open', () => {
            record.openedAtMs = Date.now();
          });

          this.addEventListener('message', (event) => {
            record.messageCount += 1;
            if (typeof event.data !== 'string') return;
            try {
              const payload = JSON.parse(event.data);
              const type = String(payload?.type ?? 'unknown');
              metrics.messageCounts[type] = (metrics.messageCounts[type] ?? 0) + 1;
              if (type === 'session_started') {
                record.sessionStartedCount += 1;
              }
              if (type === 'error') {
                record.errorMessages.push(String(payload?.message ?? 'unknown error'));
              }
            } catch {
              // Ignore non-JSON payloads.
            }
          });

          this.addEventListener('close', (event) => {
            record.closedAtMs = Date.now();
            record.closeCode = event.code;
            record.closeReason = event.reason;
          });
        }
      }

      Object.defineProperty(InstrumentedWebSocket, 'CONNECTING', { value: NativeWebSocket.CONNECTING });
      Object.defineProperty(InstrumentedWebSocket, 'OPEN', { value: NativeWebSocket.OPEN });
      Object.defineProperty(InstrumentedWebSocket, 'CLOSING', { value: NativeWebSocket.CLOSING });
      Object.defineProperty(InstrumentedWebSocket, 'CLOSED', { value: NativeWebSocket.CLOSED });
      window.WebSocket = InstrumentedWebSocket;

      if (!enableFakeMic || !injectedAudio) {
        return;
      }

      const decodeBase64 = (value: string): Uint8Array => {
        const binary = atob(value);
        const bytes = new Uint8Array(binary.length);
        for (let index = 0; index < binary.length; index += 1) {
          bytes[index] = binary.charCodeAt(index);
        }
        return bytes;
      };

      const audioBytes = decodeBase64(injectedAudio);
      let cachedStreamPromise: Promise<MediaStream> | null = null;

      const buildFakeStream = async (): Promise<MediaStream> => {
        metrics.fakeMicRequestedAtMs = Date.now();
        const context = new AudioContext();
        const arrayBuffer = audioBytes.slice().buffer as ArrayBuffer;
        const decoded = await context.decodeAudioData(arrayBuffer);
        const destination = context.createMediaStreamDestination();
        const source = context.createBufferSource();
        source.buffer = decoded;
        source.loop = shouldLoopFakeMic;
        source.connect(destination);
        source.onended = () => {
          metrics.fakeMicEndedAtMs = Date.now();
        };
        source.start();
        metrics.fakeMicStartedAtMs = Date.now();
        window.__cbFakeMicContext = context;
        window.__cbFakeMicSource = source;
        return destination.stream;
      };

      if (!navigator.mediaDevices) {
        Object.defineProperty(navigator, 'mediaDevices', {
          value: {},
          configurable: true,
        });
      }

      navigator.mediaDevices.getUserMedia = async () => {
        if (!cachedStreamPromise) {
          cachedStreamPromise = buildFakeStream();
        }
        return cachedStreamPromise;
      };
    },
    { base64Audio, fakeMic, fakeMicLoop },
  );

  await page.exposeFunction('__cbAttachErrors', () => {
    return {
      consoleErrors,
      pageErrors,
    };
  });
}

export async function readReplayMetrics(page: Page): Promise<ReplayMetrics> {
  return page.evaluate(async () => {
    const metrics = window.__cbReplayMetrics;
    const attachErrors = await window.__cbAttachErrors();
    return {
      ...metrics,
      consoleErrors: attachErrors.consoleErrors,
      pageErrors: attachErrors.pageErrors,
    };
  });
}

export async function closeLatestSocket(page: Page, kind: SocketKind): Promise<boolean> {
  return page.evaluate((socketKind) => {
    return window.__cbReplayControl.closeLatestSocket(socketKind);
  }, kind);
}

declare global {
  interface Window {
    __cbAttachErrors: () => Promise<{ consoleErrors: string[]; pageErrors: string[] }>;
    __cbFakeMicContext?: AudioContext;
    __cbFakeMicSource?: AudioBufferSourceNode;
    __cbReplayControl: {
      closeLatestSocket: (kind: SocketKind, code?: number, reason?: string) => boolean;
    };
    __cbReplayMetrics: ReplayMetrics;
  }
}

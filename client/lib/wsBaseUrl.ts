/**
 * WebSocket base URL (no path). Used when opening /api/stream/v1, /api/display/v1, etc.
 *
 * Defaulting to ws://localhost:8000 breaks when the page is opened as
 * http://<LAN-IP>:3000 or another hostname: the browser would connect to
 * localhost on the client device, not the machine running the API.
 */
function normalizeEnvWs(envWs: string): string {
  const trimmed = envWs.replace(/\/$/, '');
  if (typeof window === 'undefined') return trimmed;
  const host = window.location.hostname;
  const pageIsLocal = host === 'localhost' || host === '127.0.0.1';
  if (!pageIsLocal && /^wss?:\/\/(localhost|127\.0\.0\.1)(:\d+)?\/?$/i.test(trimmed)) {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const portMatch = trimmed.match(/:(\d+)(?:\/|$)/);
    const port = portMatch ? portMatch[1] : '8000';
    return `${proto}//${host}:${port}`;
  }
  return trimmed;
}

function normalizeEnvApi(envApi: string): string {
  const trimmed = envApi.replace(/\/$/, '');
  if (typeof window === 'undefined') return trimmed;
  const host = window.location.hostname;
  const pageIsLocal = host === 'localhost' || host === '127.0.0.1';
  if (!pageIsLocal && /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?\/?$/i.test(trimmed)) {
    const proto = window.location.protocol;
    const portMatch = trimmed.match(/:(\d+)(?:\/|$)/);
    const port = portMatch ? portMatch[1] : '8000';
    return `${proto}//${host}:${port}`;
  }
  return trimmed;
}

export function getWebSocketBaseUrl(): string {
  const envWs = process.env.NEXT_PUBLIC_WS_URL?.trim();
  if (envWs) return normalizeEnvWs(envWs);

  const envApi = process.env.NEXT_PUBLIC_API_URL?.trim();
  if (envApi) {
    try {
      const u = new URL(envApi);
      const wsProto = u.protocol === 'https:' ? 'wss:' : 'ws:';
      return `${wsProto}//${u.host}`;
    } catch {
      /* ignore invalid URL */
    }
  }

  if (typeof window === 'undefined') {
    return 'ws://localhost:8000';
  }

  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.hostname}:8000`;
}

export function getApiBaseUrl(): string {
  const envApi = process.env.NEXT_PUBLIC_API_URL?.trim();
  if (envApi) return normalizeEnvApi(envApi);

  const envWs = process.env.NEXT_PUBLIC_WS_URL?.trim();
  if (envWs) {
    try {
      const u = new URL(normalizeEnvWs(envWs));
      const apiProto = u.protocol === 'wss:' ? 'https:' : 'http:';
      return `${apiProto}//${u.host}`;
    } catch {
      /* ignore invalid URL */
    }
  }

  if (typeof window === 'undefined') {
    return 'http://localhost:8000';
  }

  return `${window.location.protocol}//${window.location.hostname}:8000`;
}

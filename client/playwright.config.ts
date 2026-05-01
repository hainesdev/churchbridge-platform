import { defineConfig } from '@playwright/test';

const port = Number(process.env.PLAYWRIGHT_CLIENT_PORT ?? 3001);
const liveBaseURL = process.env.PLAYWRIGHT_BASE_URL?.trim();
const useExternalBaseURL = Boolean(liveBaseURL);
const baseURL = useExternalBaseURL ? liveBaseURL! : `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './e2e',
  timeout: 140_000,
  expect: {
    timeout: 10_000,
  },
  fullyParallel: false,
  retries: 0,
  reporter: 'line',
  use: {
    baseURL,
    browserName: 'chromium',
    headless: true,
    trace: 'retain-on-failure',
  },
  webServer: useExternalBaseURL
    ? undefined
    : {
        command: `npm run dev -- --hostname 127.0.0.1 --port ${port}`,
        url: baseURL,
        cwd: __dirname,
        reuseExistingServer: true,
        stdout: 'pipe',
        stderr: 'pipe',
        env: {
          ...process.env,
          NEXT_PUBLIC_API_URL: process.env.CHURCHBRIDGE_API_URL ?? 'http://127.0.0.1:8000',
          NEXT_PUBLIC_WS_URL: process.env.CHURCHBRIDGE_WS_URL ?? 'ws://127.0.0.1:8000',
        },
      },
});

import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';

const testDataDirectory = path.join(__dirname, '.playwright-data');

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI
    ? [['line'], ['html', { open: 'never' }]]
    : [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: 'http://127.0.0.1:8502',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command:
      'python scripts/seed_playwright_receipt.py && python -m streamlit run app.py --server.headless true --server.address 127.0.0.1 --server.port 8502',
    url: 'http://127.0.0.1:8502',
    timeout: 120_000,
    reuseExistingServer: false,
    stdout: 'pipe',
    stderr: 'pipe',
    env: {
      ...process.env,
      RECEIPT_DATA_DIR: testDataDirectory,
    },
  },
});

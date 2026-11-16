import { defineConfig } from '@playwright/test';

/** 直接加载真实 dist 和合成 API；全部请求经 fixture 处理，不启动本地服务。 */
export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.spec.ts',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  outputDir: '../../../output/playwright/refactor-ci',
  reporter: [['list'], ['html', { open: 'never', outputFolder: '../../../output/playwright/refactor-ci-report' }]],
  use: {
    baseURL: process.env.QUANT_E2E_BASE_URL || 'http://quant-e2e.invalid',
    browserName: 'chromium',
    channel: process.env.PLAYWRIGHT_CHANNEL || undefined,
    headless: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    serviceWorkers: 'block',
  },
});

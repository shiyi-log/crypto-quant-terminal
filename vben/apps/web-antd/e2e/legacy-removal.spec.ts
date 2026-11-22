import { expect, test } from '@playwright/test';

import { installDemoFixture } from './demo-fixture';
import { installWorkspaceFixture } from './workspace-fixture';

const retired = [
  'overview',
  'positions',
  'market',
  'data',
  'backtest',
  'detail',
  'tasks',
  'live',
  'strategy-focus',
  'research-ledger',
  'iteration',
  'ops',
  'pairlist',
  'logs',
];

const aliases = [
  ['trade-desk', 'desk', 'desk'],
  ['acceptance', 'governance', 'acceptance'],
  ['factors', 'research', 'factors'],
  ['experiments', 'research', 'experiments'],
  ['data-quality', 'governance', 'data-quality'],
  ['operations', 'governance', 'operations'],
  ['orderflow', 'research', 'orderflow'],
  ['price-spikes', 'research', 'orderflow'],
  ['research', 'research', 'research'],
  ['strategy-lab', 'research', 'research'],
  ['binance-demo', 'governance', 'settings'],
  ['security', 'governance', 'settings'],
] as const;

for (const viewport of [
  { width: 1440, height: 1000 },
  { width: 390, height: 844 },
]) {
  test(`旧业务详情路径仍返回 404 · ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await installWorkspaceFixture(page);
    await page.goto('/#/quant/trade-desk');
    await expect(page.locator('.preview-shell')).toBeVisible();

    for (const path of retired) {
      await page.goto(`/#/quant/${path}?run_id=retired-record`);
      await expect(page.locator('.system-state h1')).toHaveText(
        '找不到这个页面',
      );
      await expect(page.locator('[data-current-location]')).toHaveText(
        `/quant/${path}`,
      );
      await expect(page.locator('.preview-shell')).toHaveCount(0);
    }
  });

  test(`兼容入口直接显示固定演示且不请求后端 · ${viewport.width}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    const fixture = await installDemoFixture(page);
    await page.addInitScript(() =>
      sessionStorage.setItem('ft_manual_logout', '1'),
    );

    for (const [path, preview, workspace] of aliases) {
      await page.goto(`/#/quant/${path}?run_id=compatibility-record`);
      await expect(page).toHaveURL(
        new RegExp(
          `#/quant/demo-preview\?.*preview=${preview}.*workspace=${workspace}`,
        ),
      );
      await expect(
        page
          .locator('.preview-scope')
          .getByText('本地演示数据', { exact: true }),
      ).toBeVisible();
      await expect(
        page.getByText('不读取账户，不发送命令', { exact: true }),
      ).toBeVisible();
      await expect(page.locator('[data-layout-region="sidebar"]')).toHaveCount(
        0,
      );
      const dimensions = await page.evaluate(() => ({
        document: document.documentElement.scrollWidth,
        viewport: window.innerWidth,
      }));
      expect(dimensions.document).toBeLessThanOrEqual(dimensions.viewport);
    }

    expect(fixture.authRequests).toEqual([]);
    expect(fixture.readRequests).toEqual([]);
    expect(fixture.mutations).toEqual([]);
    expect(fixture.unexpected).toEqual([]);
    expect(fixture.pageErrors).toEqual([]);
  });
}

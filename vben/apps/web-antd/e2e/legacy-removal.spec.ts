import { expect, test } from '@playwright/test';

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

for (const viewport of [
  { width: 1440, height: 1000 },
  { width: 390, height: 844 },
]) {
  test(`旧版页面全部退役且新版导航可用 · ${viewport.width}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    const fixture = await installWorkspaceFixture(page);
    await page.goto('/#/quant/trade-desk');
    await expect(page.locator('.desk-page')).toBeVisible();
    for (const path of retired) {
      await page.evaluate((retiredPath) => {
        location.hash = `/quant/${retiredPath}?run_id=retired-record`;
      }, path);
      await expect(page.locator('.system-state h1')).toHaveText(
        '找不到这个页面',
      );
      await expect(page.locator('[data-current-location]')).toHaveText(
        `/quant/${path}`,
      );
      await expect(page.locator('.quant-shell-legacy')).toHaveCount(0);
      await expect(
        page.getByRole('button', { name: '启动', exact: true }),
      ).toHaveCount(0);
    }
    await page.getByRole('link', { name: '返回交易台', exact: true }).click();
    await expect(page.locator('.quant-shell-environment')).toContainText(
      'Binance Demo',
    );
    const navigation = page.locator('#quant-workspace-navigation');
    await expect(navigation.locator('a')).toHaveCount(8);
    const addresses = await navigation
      .locator('a')
      .evaluateAll((links) => links.map((link) => link.getAttribute('href')));
    expect(addresses).toEqual([
      '#/quant/trade-desk',
      '#/quant/factors',
      '#/quant/orderflow',
      '#/quant/research',
      '#/quant/experiments',
      '#/quant/acceptance',
      '#/quant/data-quality',
      '#/quant/operations',
    ]);
    expect(fixture.mutations).toEqual([]);
    expect(fixture.unexpected).toEqual([]);
    expect(fixture.pageErrors).toEqual([]);
    expect(
      fixture.readRequests.some(
        (path) =>
          path.startsWith('/api/v1/show_config') ||
          path.startsWith('/api/v1/count') ||
          path.startsWith('/api/ws-token'),
      ),
    ).toBe(false);
  });
}

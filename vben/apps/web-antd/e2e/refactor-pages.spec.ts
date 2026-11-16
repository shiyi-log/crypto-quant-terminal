import { expect, test } from '@playwright/test';

import { installDemoFixture, SYNTHETIC_ACCOUNT, SYNTHETIC_SESSION } from './demo-fixture';

const viewports = [
  { width: 1920, height: 1080 }, { width: 1440, height: 1000 },
  { width: 768, height: 1024 }, { width: 390, height: 844 },
];

for (const viewport of viewports) {
  for (const view of ['trade-desk', 'acceptance']) {
    test(`${view} · ${viewport.width} · 真实构建与只读合成事实`, async ({ page }) => {
      await page.setViewportSize(viewport);
      const fixture = await installDemoFixture(page);
      await page.goto(`/#/quant/${view}`);
      const content = page.locator(view === 'trade-desk' ? '.desk-page' : '.acceptance-page');
      await expect(content.getByText(SYNTHETIC_ACCOUNT, { exact: true })).toBeVisible();
      await expect(content.getByText(SYNTHETIC_SESSION, { exact: true })).toBeVisible();
      await expect(content.getByText('风险证据不足', { exact: true })).toBeVisible();
      if (view === 'trade-desk') {
        await expect(content.getByText('998.12345678901234567890', { exact: true })).toBeVisible();
        await expect(content.getByText('提交结果待确认', { exact: true })).toBeVisible();
        await expect(content.getByText('未取得有效条件单证据', { exact: true })).toBeVisible();
      } else {
        await expect(content.getByText('尚未形成 30 天裁定', { exact: true })).toBeVisible();
        await expect(content.getByText('证据缺失', { exact: true })).toBeVisible();
        await expect(content.getByText('需要人工复核', { exact: true })).toBeVisible();
      }
      const width = await page.evaluate(() => ({
        document: document.documentElement.scrollWidth, viewport: window.innerWidth,
      }));
      expect(width.document).toBeLessThanOrEqual(width.viewport);
      expect(fixture.requestedSessions.length).toBeGreaterThan(0);
      expect(fixture.requestedSessions.every((session) => session === SYNTHETIC_SESSION)).toBe(true);
      expect(fixture.mutations).toEqual([]);
      expect(fixture.unexpected).toEqual([]);
      await expect(page.locator('#__app-loading__')).toHaveCount(0);
      const loginNoticeClose = page.locator('.ant-notification-notice-close');
      if (await loginNoticeClose.isVisible()) await loginNoticeClose.click();
      await expect(page.locator('.ant-notification-notice')).toHaveCount(0);
      await page.screenshot({ path: test.info().outputPath(`${view}-${viewport.width}.png`), fullPage: true });
    });
  }
}

test('默认登录落到交易台，刷新失败保留精确旧权益', async ({ page }) => {
  const fixture = await installDemoFixture(page);
  await page.goto('/');
  await expect(page).toHaveURL(/#\/quant\/trade-desk$/);
  const content = page.locator('.desk-page');
  await expect(content.getByText('998.12345678901234567890', { exact: true })).toBeVisible();
  fixture.fail = true;
  await content.getByRole('button', { name: '刷新事实' }).click();
  await expect(content.getByText('读取失败 · 保留旧数据', { exact: true })).toBeVisible();
  await expect(content.getByText('998.12345678901234567890', { exact: true })).toBeVisible();
  expect(fixture.mutations).toEqual([]);
  expect(fixture.unexpected).toEqual([]);
});

test('未知净权益与实际配置保持未知，不能用零或通过替代', async ({ page }) => {
  const fixture = await installDemoFixture(page);
  fixture.unknown = true;
  await page.goto('/#/quant/trade-desk');
  const content = page.locator('.desk-page');
  await expect(content.getByText(SYNTHETIC_ACCOUNT, { exact: true })).toBeVisible();
  await expect(content.locator('.risk-metrics > div').first().locator('dd')).toHaveText('未知');
  await expect(content.locator('.protection-rail').getByText('未知', { exact: true }).first()).toBeVisible();
  await expect(content.getByText('风险证据不足', { exact: true })).toBeVisible();
  expect(fixture.mutations).toEqual([]);
  expect(fixture.unexpected).toEqual([]);
});

test('刷新期间会话变化丢弃整组迟到事实，重新刷新才显示新身份', async ({ page }) => {
  const fixture = await installDemoFixture(page);
  await page.goto('/#/quant/trade-desk');
  const content = page.locator('.desk-page');
  await expect(content.getByText(SYNTHETIC_SESSION, { exact: true })).toBeVisible();
  fixture.equity = '111.22222222222222222222';
  const delayed = fixture.holdNextAccount();
  await content.getByRole('button', { name: '刷新事实' }).click();
  await delayed.waiting;
  fixture.session = 'synthetic-session-b';
  fixture.account = '合成测试 · strategy-b';
  fixture.equity = '777.88888888888888888888';
  delayed.release();
  await expect(content.getByText('Demo 会话已切换，旧会话响应已丢弃，请刷新', { exact: true })).toBeVisible();
  await expect(content.getByText(SYNTHETIC_SESSION, { exact: true })).toBeVisible();
  await expect(content.getByText('998.12345678901234567890', { exact: true })).toBeVisible();
  await expect(content.getByText('111.22222222222222222222', { exact: true })).toHaveCount(0);
  await expect(content.getByText('synthetic-session-b', { exact: true })).toHaveCount(0);
  await content.getByRole('button', { name: '刷新事实' }).click();
  await expect(content.getByText('synthetic-session-b', { exact: true })).toBeVisible();
  await expect(content.getByText('777.88888888888888888888', { exact: true })).toBeVisible();
  expect(fixture.mutations).toEqual([]);
  expect(fixture.unexpected).toEqual([]);
});

test('验收刷新失败保留旧会话，仍无正式通过结论', async ({ page }) => {
  const fixture = await installDemoFixture(page);
  await page.goto('/#/quant/acceptance');
  const content = page.locator('.acceptance-page');
  await expect(content.getByText(SYNTHETIC_SESSION, { exact: true })).toBeVisible();
  fixture.fail = true;
  await content.getByRole('button', { name: '刷新证据' }).click();
  await expect(content.getByText('保留上次数据，刷新失败', { exact: true })).toBeVisible();
  await expect(content.getByText(SYNTHETIC_SESSION, { exact: true })).toBeVisible();
  await expect(content.getByText('尚未形成 30 天裁定', { exact: true })).toBeVisible();
  expect(fixture.mutations).toEqual([]);
  expect(fixture.unexpected).toEqual([]);
});

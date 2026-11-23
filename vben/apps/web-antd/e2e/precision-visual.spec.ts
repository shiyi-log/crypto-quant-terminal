import { expect, test } from '@playwright/test';

import { installDemoFixture } from './demo-fixture';

const viewports = [
  { width: 1920, height: 1080 },
  { width: 1440, height: 1000 },
  { width: 768, height: 1024 },
  { width: 390, height: 844 },
];

for (const viewport of viewports) {
  test(`固定演示数据四视口布局与只读边界 · ${viewport.width}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    const fixture = await installDemoFixture(page);
    await page.goto('/#/quant/trade-desk');

    await expect
      .poll(() => page.evaluate(() => window.location.hash))
      .toBe('#/quant/demo-preview?preview=desk&workspace=desk');
    await expect(
      page.getByRole('heading', { name: '演示交易台', exact: true }),
    ).toBeVisible();
    await expect(page.getByTestId('selected-price')).toHaveText('67,842.10');
    await expect(page.locator('.desk-metrics > div').first()).toContainText(
      '1,000.00',
    );
    await expect(
      page.getByText('仅展示固定示例，不对应交易所账户', { exact: true }),
    ).toBeVisible();

    await page.getByRole('button', { name: '部分成交', exact: true }).click();
    await expect(page.getByTestId('position-state')).toContainText(
      '0.001 BTC 已成交',
    );
    await expect(page.getByTestId('position-protection')).toContainText(
      '确认中',
    );
    await expect(page.getByTestId('position-protection')).not.toContainText(
      '保护已确认',
    );
    await page.getByRole('button', { name: '空仓', exact: true }).click();
    await expect(page.getByTestId('positions-empty')).toContainText(
      '当前没有持仓',
    );
    await page.getByRole('button', { name: '未知', exact: true }).click();
    await expect(page.getByTestId('desk-snapshot-status')).toContainText(
      '账户快照未知',
    );
    await expect(page.getByTestId('position-state')).toContainText(
      'BTCUSDT · 上次样本保留',
    );
    await expect(page.getByTestId('entry-permission')).toContainText(
      '状态未知：禁止新增风险',
    );
    await page.getByRole('button', { name: '过期', exact: true }).click();
    await expect(page.getByTestId('desk-snapshot-status')).toContainText(
      '快照已过期',
    );
    await page.getByRole('button', { name: '部分成交', exact: true }).click();
    await page.getByRole('button', { name: '冷却中', exact: true }).click();
    await expect(page.getByTestId('entry-permission')).toContainText('43 分钟');
    await expect(page.getByTestId('terminal-risk-strip')).toContainText(
      '冷却中',
    );
    await page.getByRole('button', { name: '硬失败', exact: true }).click();
    await expect(page.getByTestId('entry-permission')).toContainText(
      '本轮已终止',
    );
    await expect(page.getByTestId('terminal-risk-strip')).toContainText(
      '本轮硬失败',
    );

    const dimensions = await page.evaluate(() => ({
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
      shellWidth:
        document.querySelector('.preview-shell')?.getBoundingClientRect()
          .width ?? 0,
    }));
    expect(dimensions.document).toBeLessThanOrEqual(dimensions.viewport);
    expect(dimensions.shellWidth).toBeLessThanOrEqual(dimensions.viewport);
    expect(fixture.readRequests).toEqual([]);
    expect(fixture.authRequests).toEqual([]);
    expect(fixture.mutations).toEqual([]);
    expect(fixture.unexpected).toEqual([]);
    expect(fixture.pageErrors).toEqual([]);
    await page.getByRole('button', { name: '读取失败', exact: true }).click();
    await expect(page.getByTestId('desk-snapshot-status')).toContainText(
      '保留上次样本',
    );
    expect(fixture.readRequests).toEqual([]);
    expect(fixture.authRequests).toEqual([]);
    expect(fixture.mutations).toEqual([]);
    expect(fixture.unexpected).toEqual([]);
    expect(fixture.pageErrors).toEqual([]);
    await page.screenshot({
      path: test.info().outputPath(`demo-trade-desk-${viewport.width}.png`),
      fullPage: true,
    });
  });

  test(`验收演示固定证据在视口内可读 · ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const fixture = await installDemoFixture(page);
    await page.goto('/#/quant/acceptance');

    await expect
      .poll(() => page.evaluate(() => window.location.hash))
      .toBe('#/quant/demo-preview?preview=governance&workspace=acceptance');
    await expect(
      page.getByRole('heading', { name: '验收与上线', exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText('Demo 验收证据', { exact: true }),
    ).toBeVisible();
    await expect(page.getByTestId('calendar-day')).toHaveCount(30);
    await expect(page.getByTestId('acceptance-segment')).toHaveCount(3);
    await page.getByRole('button', { name: '硬失败' }).click();
    await expect(page.getByTestId('acceptance-verdict')).toContainText(
      '硬失败场景演示',
    );
    await expect(page.getByTestId('production-eligibility')).toHaveText(
      '未授权',
    );
    const scenarioLog = page.getByTestId('acceptance-run-log');
    await expect(scenarioLog).toContainText('验收场景演练');
    await expect(scenarioLog).toContainText(
      'daily_net_loss_hard_limit_exceeded',
    );
    await expect(scenarioLog).toContainText('页面内处理耗时');

    const approvalTrigger = page.getByRole('button', { name: '演练审批记录' });
    await approvalTrigger.focus();
    await approvalTrigger.click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('button', { name: '取消' })).toBeFocused();
    await dialog.press('Escape');
    await expect(dialog).toBeHidden();
    await expect(approvalTrigger).toBeFocused();
    await expect(page.getByTestId('production-eligibility')).toBeVisible();
    await expect(page.getByText('本地演示数据')).toBeVisible();
    await expect(page.getByText('不读取账户，不发送命令')).toBeVisible();

    const dimensions = await page.evaluate(() => ({
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }));
    expect(dimensions.document).toBeLessThanOrEqual(dimensions.viewport);
    expect(fixture.readRequests).toEqual([]);
    expect(fixture.authRequests).toEqual([]);
    expect(fixture.mutations).toEqual([]);
    expect(fixture.unexpected).toEqual([]);
    expect(fixture.pageErrors).toEqual([]);
    await page.screenshot({
      path: test.info().outputPath(`demo-acceptance-${viewport.width}.png`),
      fullPage: true,
    });
  });
}

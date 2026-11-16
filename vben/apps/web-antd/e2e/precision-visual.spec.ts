import { expect, test } from '@playwright/test';

import { installDemoFixture, SYNTHETIC_ACCOUNT } from './demo-fixture';

const viewports = [
  { width: 1920, height: 1080 },
  { width: 1440, height: 1000 },
  { width: 768, height: 1024 },
  { width: 390, height: 844 },
];
const exactEquity = '1000.1234567890123456789';

for (const viewport of viewports) {
  test(`完整精度权益 · ${viewport.width} · 金额不断行且可键盘查看`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    const fixture = await installDemoFixture(page);
    fixture.equity = exactEquity;
    await page.goto('/#/quant/trade-desk');
    const content = page.locator('.desk-page');
    await expect(
      content.getByText(SYNTHETIC_ACCOUNT, { exact: true }),
    ).toBeVisible();
    const equity = content.locator('.risk-metrics > div').first().locator('dd');
    await expect(equity).toHaveText(exactEquity);
    await expect(page.locator('#__app-loading__')).toHaveCount(0);
    await expect(page.getByText('登录成功', { exact: true })).toBeVisible();
    const noticeClose = page.locator('.ant-notification-notice-close');
    await noticeClose.click();
    await expect(page.locator('.ant-notification-notice')).toHaveCount(0);
    await page.screenshot({
      path: test.info().outputPath(`precision-${viewport.width}.png`),
      fullPage: true,
    });
    const measured = await equity.evaluate((element) => {
      const range = document.createRange();
      range.selectNodeContents(element);
      return {
        lines: new Set(
          Array.from(range.getClientRects(), (rect) => Math.round(rect.top)),
        ).size,
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
        fontSize: parseFloat(getComputedStyle(element).fontSize),
      };
    });
    // 恢复任意数字换行会真实出现多行，而非仅断言 CSS 文本。
    expect(measured.lines).toBe(1);
    expect(measured.fontSize).toBeGreaterThanOrEqual(20);
    if (measured.scrollWidth > measured.clientWidth) {
      await equity.focus();
      await expect(equity).toBeFocused();
      await page.keyboard.press('ArrowRight');
      await expect
        .poll(() => equity.evaluate((element) => element.scrollLeft))
        .toBeGreaterThan(0);
      // 水平金额区域使用方向键到达尾数；End 在部分浏览器只移动页面纵向位置。
      for (let step = 0; step < 24; step += 1) {
        await page.keyboard.press('ArrowRight');
      }
      await expect
        .poll(() =>
          equity.evaluate(
            (element) =>
              element.scrollWidth - element.clientWidth - element.scrollLeft,
          ),
        )
        .toBeLessThanOrEqual(1);
      await expect(equity).toHaveText(exactEquity);
      await equity.screenshot({
        path: test.info().outputPath(`precision-tail-${viewport.width}.png`),
      });
    }
    const width = await page.evaluate(() => ({
      document: document.documentElement.scrollWidth,
      viewport: innerWidth,
    }));
    expect(width.document).toBeLessThanOrEqual(width.viewport);
    expect(fixture.mutations).toEqual([]);
    expect(fixture.unexpected).toEqual([]);
    expect(fixture.pageErrors).toEqual([]);
  });
}

for (const width of [1440, 390]) {
  for (const view of [
    {
      route: '/quant/strategy-lab',
      selector: '.paper-workspace',
      title: '纸面策略档案',
    },
    {
      route: '/quant/binance-demo',
      selector: '.demo-page',
      title: 'Demo 账户与会话设置',
    },
    { route: '/profile', selector: '.identity-workspace', title: '个人身份' },
  ]) {
    test(`补充页面布局 · ${view.route} · ${width} · 只读合成事实`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      const fixture = await installDemoFixture(page);
      const strategyId = 's03_tsmom_vol_target_B15m_R50_k1.0_100x_maker';
      const account = {
        strategy_id: strategyId,
        label: '合成测试纸面账户',
        initial_capital: '10000',
        cash: '10000',
        equity: '10000',
        realized_pnl: '0',
        unrealized_pnl: '0',
        fees_paid: '0',
        open_position_count: 0,
        closed_trade_count: 0,
        open_positions: [],
        rule_hash: 'a'.repeat(64),
        execution_hash: 'b'.repeat(64),
        definition: {
          name: '合成测试纸面账户',
          family: 'momentum',
          timeframe: '15m',
          mechanism: '合成夹具机制，不构成研究结论',
          factorial_design_version: 'v1',
          factorial_factors: { risk_budget: 'R50', stop_width_k: 1 },
          params_frozen: { risk_stop_atr_mult: 1 },
        },
      };
      fixture.reads.set('/api/locals/strategy-lab', (url: URL) =>
        url.searchParams.has('strategy_id')
          ? { account, orders: [], trades: [], equity_curve: [], events: [] }
          : {
              accounts: [account],
              service: { running: false },
              limitations: ['合成测试，不构成真实研究或批准'],
            },
      );
      for (const kind of ['comparisons', 'sessions']) {
        fixture.reads.set(`/api/locals/binance-demo/${kind}`, {
          environment: 'binance_demo',
          endpoint: 'demo-fapi.binance.com',
          items: [],
          limit: 100,
        });
      }
      await page.goto(`/#${view.route}`);
      const content = page.locator(view.selector);
      await expect(
        content.getByRole('heading', { name: view.title, exact: true }),
      ).toBeVisible();
      if (view.route === '/quant/strategy-lab') {
        await expect(content.locator('[data-selected-paper]')).toContainText(
          strategyId,
        );
        await expect(content.locator('.strategy-detail')).toBeVisible();
      } else if (view.route === '/quant/binance-demo') {
        await expect(content.locator('.settings-boundary')).toBeVisible();
        await expect(
          content.locator('#demo-credentials-heading'),
        ).toBeVisible();
        await expect.poll(() => fixture.statusCalls).toBeGreaterThanOrEqual(2);
      } else {
        await expect(content).toContainText('synthetic-test-user');
      }
      await expect(page.locator('#__app-loading__')).toHaveCount(0);
      await expect(page.getByText('登录成功', { exact: true })).toBeVisible();
      const noticeClose = page.locator('.ant-notification-notice-close');
      await noticeClose.click();
      await expect(page.locator('.ant-notification-notice')).toHaveCount(0);
      const dimensions = await page.evaluate(() => ({
        document: document.documentElement.scrollWidth,
        viewport: innerWidth,
      }));
      expect(dimensions.document).toBeLessThanOrEqual(dimensions.viewport);
      expect(fixture.mutations).toEqual([]);
      expect(fixture.unexpected).toEqual([]);
      expect(fixture.pageErrors).toEqual([]);
      await page.screenshot({
        path: test
          .info()
          .outputPath(`${view.route.split('/').at(-1)}-${width}.png`),
        fullPage: true,
      });
    });
  }
}

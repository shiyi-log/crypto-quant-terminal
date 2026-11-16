import type { Page } from '@playwright/test';

import { expect, test } from '@playwright/test';

import {
  API_PATHS,
  FACTOR_NAME,
  installWorkspaceFixture,
  RUN_A,
  RUN_B,
  SYNTHETIC_SERVICE,
} from './workspace-fixture';

const viewports = [
  { width: 1920, height: 1080 },
  { width: 1440, height: 1000 },
  { width: 768, height: 1024 },
  { width: 390, height: 844 },
];
const workspaces = [
  {
    route: 'factors',
    label: '因子实验室',
    selector: '.factor-workspace',
    marker: FACTOR_NAME,
  },
  {
    route: 'experiments',
    label: '实验任务',
    selector: '.experiments-workspace',
    marker: RUN_A,
  },
  {
    route: 'data-quality',
    label: '数据中心',
    selector: '.data-workspace',
    marker: 'BTC/USDT:USDT',
  },
  {
    route: 'operations',
    label: '运维与证据',
    selector: '.operations-workspace',
    marker: SYNTHETIC_SERVICE,
  },
];

async function readyForScreenshot(page: Page) {
  await expect(page.locator('#__app-loading__')).toHaveCount(0);
  const close = page.locator('.ant-notification-notice-close');
  if (await close.isVisible()) await close.click();
  await expect(page.locator('.ant-notification-notice')).toHaveCount(0);
}

function assertFixtureReadOnly(
  fixture: Awaited<ReturnType<typeof installWorkspaceFixture>>,
) {
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
}

for (const viewport of viewports) {
  for (const workspace of workspaces) {
    test(`${workspace.route} · ${viewport.width} · 真实工作区与证据边界`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      const fixture = await installWorkspaceFixture(page);
      await page.goto(`/#/quant/${workspace.route}`);
      const content = page.locator(workspace.selector);
      await expect(content).toHaveCount(1);
      if (workspace.route === 'data-quality')
        await expect(content.locator('[data-dataset-inspector] h3')).toHaveText(
          workspace.marker,
        );
      else
        await expect(
          content.getByText(workspace.marker, { exact: true }).first(),
        ).toBeVisible();
      await expect(content).toHaveCount(1);
      await expect(page.locator('.quant-shell-header')).toBeVisible();
      await expect(
        page.locator('#quant-workspace-navigation a[aria-current=page]'),
      ).toHaveText(workspace.label);
      await expect(page.locator('.quant-shell-environment')).toContainText(
        '历史研究与证据',
      );
      await expect(page.locator('.quant-shell-legacy')).toHaveCount(0);
      await expect(
        page.getByRole('button', { name: '启动', exact: true }),
      ).toHaveCount(0);
      if (workspace.route === 'factors') {
        await expect(
          content.getByText('版本身份未知', { exact: true }),
        ).toBeVisible();
        await expect(
          content.getByText('不授予 Demo 准入', { exact: true }),
        ).toBeVisible();
      } else if (workspace.route === 'experiments') {
        await expect(content.locator('.run-inspector')).toContainText(
          '执行成功',
        );
        await expect(content.locator('.run-inspector')).toContainText(
          '研究拒绝',
        );
        await expect(content.locator('.run-inspector')).toContainText('未知');
        await expect(content).toContainText('不构成上线批准');
      } else if (workspace.route === 'data-quality') {
        await expect(content.locator('[data-quality-gates]')).toContainText(
          '未取得证据',
        );
        await expect(content.locator('[data-quality-gates]')).toContainText(
          '365 天逐频率覆盖',
        );
      } else {
        await expect(content.locator('[data-account-gates]')).toContainText(
          '尚未取得账户执行资格证据',
        );
        await expect(content.locator('[data-account-gates]')).toContainText(
          '唯一写者',
        );
        await expect(content).not.toContainText(
          'synthetic-private-payload-must-not-render',
        );
      }
      await readyForScreenshot(page);
      const width = await page.evaluate(() => ({
        document: document.documentElement.scrollWidth,
        viewport: innerWidth,
      }));
      expect(width.document).toBeLessThanOrEqual(width.viewport);
      const historyTargets = await content
        .locator('a[href]')
        .evaluateAll((links) =>
          links
            .map((link) => link.getAttribute('href') || '')
            .filter((href) => href.includes('/quant/')),
        );
      expect(historyTargets.length).toBeGreaterThan(0);
      expect(historyTargets.every((href) => href.includes('#/quant/'))).toBe(
        true,
      );
      assertFixtureReadOnly(fixture);
      await page.screenshot({
        path: test
          .info()
          .outputPath(`${workspace.route}-${viewport.width}.png`),
        fullPage: true,
      });
      const scrollRegion = page.locator('[data-layout-region=scroll]');
      const maxScroll = await scrollRegion.evaluate(
        (element) => element.scrollHeight - element.clientHeight,
      );
      if (maxScroll > 0) {
        for (const [segment, fraction] of [
          ['middle', 0.5],
          ['bottom', 1],
        ] as const) {
          await scrollRegion.evaluate((element, ratio) => {
            element.scrollTop =
              (element.scrollHeight - element.clientHeight) * ratio;
          }, fraction);
          await expect
            .poll(() =>
              page.locator('.quant-shell-environment').evaluate((element) => {
                const bounds = element.getBoundingClientRect();
                return bounds.top >= 0 && bounds.bottom <= innerHeight;
              }),
            )
            .toBe(true);
          await page.screenshot({
            path: test
              .info()
              .outputPath(
                `${workspace.route}-${viewport.width}-${segment}.png`,
              ),
          });
        }
        await scrollRegion.evaluate((element) => {
          element.scrollTop = 0;
        });
      }
    });
  }

  test(`工作区键盘导航与活动焦点 · ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const fixture = await installWorkspaceFixture(page);
    await page.goto('/#/quant/trade-desk');
    await expect(page.locator('.desk-page')).toBeVisible();
    const shell = page.locator('.quant-shell-header');
    await readyForScreenshot(page);
    const initialShellTop = await shell.evaluate(
      (element) => element.getBoundingClientRect().top,
    );
    const scroll = page.locator('#__vben_layout_scroll');
    await scroll.evaluate((element) => {
      element.scrollTop = 450;
    });
    await expect
      .poll(() => scroll.evaluate((element) => element.scrollTop))
      .toBeGreaterThan(0);
    const scrolledShellTop = await shell.evaluate(
      (element) => element.getBoundingClientRect().top,
    );
    expect(Math.abs(scrolledShellTop - initialShellTop)).toBeLessThanOrEqual(1);
    await scroll.evaluate((element) => {
      element.scrollTop = 0;
    });
    const navigation = page.getByRole('navigation', {
      name: '工作区',
      exact: true,
    });
    const toggle = page.getByRole('button', { name: '工作区', exact: true });
    if (viewport.width <= 900) {
      await expect(navigation).toBeHidden();
      await toggle.focus();
      await page.keyboard.press('Enter');
      await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    }
    const factorLink = navigation.getByRole('link', {
      name: '因子实验室',
      exact: true,
    });
    await factorLink.focus();
    await page.keyboard.press('Tab');
    await page.keyboard.press('Shift+Tab');
    await expect(factorLink).toBeFocused();
    const outline = await factorLink.evaluate(
      (element) => getComputedStyle(element).outlineStyle,
    );
    expect(outline).not.toBe('none');
    if (viewport.width <= 900) {
      await page.keyboard.press('Escape');
      await expect(toggle).toBeFocused();
      await expect(toggle).toHaveAttribute('aria-expanded', 'false');
      await expect(navigation).toBeHidden();
      await page.keyboard.press('Enter');
      await factorLink.focus();
    }
    await page.keyboard.press('Enter');
    await expect(page).toHaveURL(new RegExp('#/quant/factors$'));
    await expect(page.locator('.factor-workspace')).toContainText(FACTOR_NAME);
    if (viewport.width <= 900) {
      await expect(toggle).toHaveAttribute('aria-expanded', 'false');
      await toggle.click();
    }
    await expect(
      navigation.getByRole('link', { name: '因子实验室', exact: true }),
    ).toHaveAttribute('aria-current', 'page');
    await expect(navigation.locator('[aria-current=page]')).toHaveCount(1);
    assertFixtureReadOnly(fixture);
  });
}

for (const viewport of viewports) {
  test(`因子失败刷新保留精确统计，键盘筛选保留未知 · ${viewport.width}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    const fixture = await installWorkspaceFixture(page);
    await page.goto('/#/quant/factors');
    const content = page.locator('.factor-workspace');
    await expect(content).toContainText('-0.0400');
    fixture.readFailures.add(API_PATHS.factors);
    await content.getByTestId('refresh-factors').click();
    await expect(content.getByRole('alert')).toContainText('保留上次成功快照');
    await expect(content).toContainText('-0.0400');
    await content
      .getByRole('combobox', { name: '因子分组' })
      .selectOption('成交量');
    await expect(content.locator('tbody')).not.toContainText(FACTOR_NAME);
    await content.locator('[data-factor=synthetic_volume_ratio]').focus();
    await page.keyboard.press('Enter');
    await expect(content.getByTestId('factor-inspector')).toContainText(
      '版本身份未知',
    );
    assertFixtureReadOnly(fixture);
  });

  test(`数据损坏刷新保留清单，筛选不把端点跨度当连续覆盖 · ${viewport.width}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    const fixture = await installWorkspaceFixture(page);
    await page.goto('/#/quant/data-quality');
    const content = page.locator('.data-workspace');
    await expect(content.locator('[data-count=candles]')).toContainText(
      '100,003',
    );
    fixture.reads.set(API_PATHS.data, { engine: 'postgresql' });
    await content.locator('[data-refresh]').click();
    await expect(content).toContainText('数据清单合同不完整');
    await expect(content.locator('[data-count=candles]')).toContainText(
      '100,003',
    );
    await content.locator('select[name=pair]').selectOption('ETH/USDT:USDT');
    await expect(
      content.locator('[data-inventory-table] tbody'),
    ).not.toContainText('BTC/USDT:USDT');
    await expect(content.locator('[data-dataset-inspector]')).toContainText(
      'ETH/USDT:USDT',
    );
    await expect(content.locator('[data-quality-gates]')).toContainText(
      '未取得证据',
    );
    assertFixtureReadOnly(fixture);
  });

  test(`运行列表读取失败保留完整总数，切换详情丢弃迟到旧运行 · ${viewport.width}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    const fixture = await installWorkspaceFixture(page);
    await page.goto(`/#/quant/experiments?run_id=${RUN_B}`);
    const content = page.locator('.experiments-workspace');
    await expect(content).toHaveCount(1);
    await expect(content.getByTestId('selected-run')).toHaveText(RUN_B);
    const delayed = fixture.holdNextRead(`${API_PATHS.runs}/${RUN_A}`);
    await content.locator(`[data-run-id=${RUN_A}]`).click();
    await delayed.waiting;
    await expect(content).toHaveCount(1);
    await content.locator(`[data-run-id=${RUN_B}]`).click();
    await expect(content).toHaveCount(1);
    await expect(content.getByTestId('selected-run')).toHaveText(RUN_B);
    await expect(content.locator('.run-inspector')).toContainText(
      `synthetic-proof-${RUN_B}`,
    );
    const oldResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === `${API_PATHS.runs}/${RUN_A}`,
    );
    delayed.release();
    await oldResponse;
    await page.evaluate(
      () =>
        new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        ),
    );
    await expect(content.getByTestId('selected-run')).toHaveText(RUN_B);
    await expect(content.locator('.run-inspector')).not.toContainText(
      `synthetic-proof-${RUN_A}`,
    );
    fixture.readFailures.add(API_PATHS.runs);
    await content.getByTestId('refresh-runs').click();
    await expect(content.getByRole('alert').first()).toContainText(
      '保留上次成功数据',
    );
    await expect(content).toContainText('2 / 19');
    await expect(content.locator('[data-run-id]')).toHaveCount(2);
    assertFixtureReadOnly(fixture);
  });

  test(`运维筛选失败标出保留事实的原筛选，健康不代表账户准入 · ${viewport.width}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    const fixture = await installWorkspaceFixture(page);
    await page.goto('/#/quant/operations');
    const content = page.locator('.operations-workspace');
    await expect(content).toContainText(`synthetic-event-${RUN_A}`);
    fixture.readFailures.add(API_PATHS.events);
    await content.locator('input[name=research_id]').fill(RUN_B);
    await content.locator('input[name=research_id]').press('Enter');
    await expect(content).toContainText(`当前请求筛选：${RUN_B}`);
    await expect(content).toContainText('保留事实筛选：全部研究');
    await expect(content).toContainText(`synthetic-event-${RUN_A}`);
    await expect(content.locator('[data-account-gates]')).toContainText(
      '尚未取得账户执行资格证据',
    );
    fixture.readFailures.add(API_PATHS.services);
    await content.locator('[data-refresh-all]').click();
    await expect(content).toContainText('读取失败，保留上次成功事实');
    await expect(content).toContainText(SYNTHETIC_SERVICE);
    assertFixtureReadOnly(fixture);
  });
}

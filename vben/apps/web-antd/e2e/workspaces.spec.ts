import { expect, test } from '@playwright/test';

import { installDemoFixture } from './demo-fixture';

const workspaces = [
  {
    route: 'factors',
    preview: 'research',
    workspace: 'factors',
    heading: '因子实验室',
    marker: '成交量突破',
  },
  {
    route: 'experiments',
    preview: 'research',
    workspace: 'experiments',
    heading: '实验任务',
    marker: '波动窗口复核',
  },
  {
    route: 'data-quality',
    preview: 'governance',
    workspace: 'data-quality',
    heading: '数据中心',
    marker: 'BTCUSDT',
  },
  {
    route: 'operations',
    preview: 'governance',
    workspace: 'operations',
    heading: '运维与证据',
    marker: '事件详情',
  },
] as const;

for (const viewport of [
  { width: 1440, height: 1000 },
  { width: 390, height: 844 },
]) {
  for (const item of workspaces) {
    test(`${item.route} 进入本地演示工作区 · ${viewport.width}`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      const fixture = await installDemoFixture(page);
      await page.goto(`/#/quant/${item.route}?identifier=demo-record`);
      await expect(page).toHaveURL(
        new RegExp(
          `#/quant/demo-preview\?.*preview=${item.preview}.*workspace=${item.workspace}`,
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
      await expect(
        page.getByRole('heading', { name: item.heading, exact: true }),
      ).toBeVisible();
      if (item.route === 'data-quality') {
        await expect(
          page.getByTestId('coverage-table').getByText(item.marker, {
            exact: true,
          }),
        ).toBeVisible();
      } else if (item.route === 'experiments') {
        await expect(page.getByTestId('experiment-detail')).toContainText(
          item.marker,
        );
        await expect(page.getByTestId('experiment-detail')).toContainText(
          '进度未知',
        );
        await expect(page.getByTestId('experiment-detail')).toContainText(
          'worker 未验证',
        );
      } else {
        await expect(
          page.getByRole('heading', { name: item.marker, exact: true }),
        ).toBeVisible();
      }

      if (item.route === 'factors') {
        await page.getByRole('tab', { name: '策略研究' }).click();
        await expect(
          page.getByRole('heading', { name: '策略研究', exact: true }),
        ).toBeVisible();
        await page.getByRole('tab', { name: '因子目录' }).click();
      } else if (item.route === 'data-quality') {
        await page.getByRole('button', { name: '加入本地补采演练' }).click();
        await expect(page.getByTestId('collection-queue')).toContainText(
          'BTCUSDT',
        );
      } else if (item.route === 'operations') {
        await expect(page.getByTestId('log-detail')).toContainText(
          'demo_snapshot_recorded',
        );
        await page
          .getByRole('combobox', { name: '日志级别' })
          .selectOption('warning');
        await expect(page.getByTestId('log-list')).toContainText(
          '资金费观测延迟',
        );
      }

      const dimensions = await page.evaluate(() => ({
        document: document.documentElement.scrollWidth,
        viewport: window.innerWidth,
      }));
      expect(dimensions.document).toBeLessThanOrEqual(dimensions.viewport);
      expect(fixture.authRequests).toEqual([]);
      expect(fixture.readRequests).toEqual([]);
      expect(fixture.mutations).toEqual([]);
      expect(fixture.unexpected).toEqual([]);
      expect(fixture.pageErrors).toEqual([]);
      await page.screenshot({
        path: test.info().outputPath(`${item.route}-${viewport.width}.png`),
        fullPage: true,
      });
    });
  }
}

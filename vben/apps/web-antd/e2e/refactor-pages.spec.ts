import { expect, test } from '@playwright/test';

import { installDemoFixture } from './demo-fixture';

const aliases = [
  {
    path: 'trade-desk',
    preview: 'desk',
    workspace: 'desk',
    heading: '演示交易台',
  },
  {
    path: 'acceptance',
    preview: 'governance',
    workspace: 'acceptance',
    heading: '验收与上线',
  },
] as const;

for (const viewport of [
  { width: 1440, height: 1000 },
  { width: 390, height: 844 },
]) {
  for (const alias of aliases) {
    test(`量化兼容入口进入固定演示 · ${alias.path} · ${viewport.width}`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      const fixture = await installDemoFixture(page);
      await page.goto(`/#/quant/${alias.path}?run_id=demo-contract`);

      await expect
        .poll(() => page.evaluate(() => window.location.hash))
        .toBe(
          `#/quant/demo-preview?run_id=demo-contract&preview=${alias.preview}&workspace=${alias.workspace}`,
        );
      await expect(page.locator('#__app-loading__')).toHaveCount(0);
      await expect(
        page.getByRole('heading', { name: alias.heading, exact: true }),
      ).toBeVisible();
      await expect(page.locator('.preview-scope strong')).toHaveText(
        '本地演示数据',
      );
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
      expect(fixture.readRequests).toEqual([]);
      expect(fixture.authRequests).toEqual([]);
      expect(fixture.mutations).toEqual([]);
      expect(fixture.unexpected).toEqual([]);
      expect(fixture.pageErrors).toEqual([]);
      await page.screenshot({
        path: test.info().outputPath(`${alias.path}-${viewport.width}.png`),
        fullPage: true,
      });
    });
  }
}

test('演示入口保留未知查询参数且不读取业务数据', async ({ page }) => {
  const fixture = await installDemoFixture(page);
  await page.goto('/#/quant/trade-desk?identifier=local-sample&unknown=kept');
  await expect
    .poll(() => page.evaluate(() => window.location.hash))
    .toBe(
      '#/quant/demo-preview?identifier=local-sample&unknown=kept&preview=desk&workspace=desk',
    );
  await expect(
    page.getByRole('heading', { name: '演示交易台', exact: true }),
  ).toBeVisible();
  expect(fixture.readRequests).toEqual([]);
  expect(fixture.authRequests).toEqual([]);
  expect(fixture.mutations).toEqual([]);
  expect(fixture.unexpected).toEqual([]);
  expect(fixture.pageErrors).toEqual([]);
});

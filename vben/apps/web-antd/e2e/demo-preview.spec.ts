import { expect, test } from '@playwright/test';

import { installDemoFixture } from './demo-fixture';

const viewports = [
  { width: 1920, height: 1080 },
  { width: 1440, height: 1000 },
  { width: 768, height: 1024 },
  { width: 390, height: 844 },
];

for (const viewport of viewports) {
  test(`本地演示预览只读且不横向溢出 · ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const fixture = await installDemoFixture(page);
    await page.goto('/#/');
    await expect(page).toHaveURL(/#\/quant\/demo-preview$/);
    await expect(page.locator('#__app-loading__')).toHaveCount(0);

    await expect(page.getByText('本地演示数据', { exact: true })).toBeVisible();
    await expect(
      page.getByText('不读取账户，不发送命令', { exact: true }),
    ).toBeVisible();
    await expect(page.getByText('演示交易台', { exact: true })).toBeVisible();
    await expect(page.locator('[data-layout-region="sidebar"]')).toHaveCount(0);
    const homeLayout = await page.evaluate(() => {
      const shell = document.querySelector('.preview-shell');
      const footer = document.querySelector('.preview-footer');
      return {
        footerBottom: footer?.getBoundingClientRect().bottom ?? 0,
        shellHeight: shell?.getBoundingClientRect().height ?? 0,
        viewport: window.innerHeight,
      };
    });
    expect(homeLayout.shellHeight).toBeGreaterThanOrEqual(homeLayout.viewport);
    expect(homeLayout.footerBottom).toBeGreaterThanOrEqual(
      homeLayout.viewport - 1,
    );
    await page.screenshot({
      path: test.info().outputPath(`demo-preview-home-${viewport.width}.png`),
      fullPage: true,
    });

    await page.getByRole('button', { name: '研究工作区' }).click();
    // 页面导航和工作区标题都会出现“因子实验室”，这里锁定实际内容标题避免严格模式歧义。
    await expect(
      page.getByRole('heading', { name: '因子实验室', exact: true }),
    ).toBeVisible();
    await page.getByRole('button', { name: '验收与运维' }).click();
    await expect(
      page.getByRole('heading', { name: '验收与上线', exact: true }),
    ).toBeVisible();
    await page.getByRole('button', { name: '运维与证据' }).click();
    await expect(
      page.getByRole('heading', { name: '运维与证据', exact: true }),
    ).toBeVisible();
    await expect(page.getByTestId('preview-content')).toContainText(
      '固定原因码',
    );
    await page.locator('[data-log="evt-1042"]').click();
    await expect(page.getByTestId('preview-content')).toContainText(
      'funding_fee_observation_delayed',
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
      path: test.info().outputPath(`demo-preview-${viewport.width}.png`),
      fullPage: true,
    });
  });
}

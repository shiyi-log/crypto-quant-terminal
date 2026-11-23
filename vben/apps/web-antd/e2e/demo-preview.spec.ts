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
    await expect(page.getByTestId('preview-sample-time')).toBeVisible();
    await expect(page.getByTestId('terminal-risk-strip')).toBeVisible();
    const shellTokens = await page
      .locator('.preview-shell')
      .evaluate((shell) => {
        const styles = getComputedStyle(shell);
        return {
          ocean: styles.getPropertyValue('--qt-ocean').trim(),
          workspace: styles.getPropertyValue('--qt-workspace').trim(),
          divider: styles.getPropertyValue('--qt-divider').trim(),
          interactive: styles.getPropertyValue('--qt-interactive').trim(),
        };
      });
    expect(shellTokens).toEqual({
      ocean: '#0c1b2a',
      workspace: '#132b3e',
      divider: '#29485c',
      interactive: '#69b4e4',
    });
    await expect(page.getByTestId('preview-sample-time')).toHaveText(
      '样本时间 · 2026-11-23 14:32:18 北京时间',
    );
    const riskPosition = await page
      .getByTestId('terminal-risk-strip')
      .boundingBox();
    expect(riskPosition).not.toBeNull();
    expect(riskPosition!.y).toBeGreaterThan(0);
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
    const factorSearch = page.getByRole('searchbox', { name: '搜索因子' });
    await factorSearch.fill('盘口');
    await expect(page.getByTestId('factor-detail')).toContainText(
      'book_imbalance',
    );
    await factorSearch.fill('没有匹配项');
    await expect(page.getByTestId('factor-empty')).toContainText(
      '没有符合条件的因子',
    );
    await expect(page.getByTestId('factor-detail')).toHaveCount(0);
    await page.getByRole('button', { name: '实验任务', exact: true }).click();
    await expect(
      page.getByRole('heading', { name: '实验任务', exact: true }),
    ).toBeVisible();
    const experimentSearch = page.getByRole('searchbox', {
      name: '搜索实验任务',
    });
    await experimentSearch.fill('动量');
    await expect(page.getByTestId('experiment-detail')).toContainText(
      'EXP-023',
    );
    await expect(page.getByTestId('experiment-detail')).toContainText(
      '动量参数搜索',
    );
    await experimentSearch.fill('没有匹配项');
    await expect(page.getByTestId('experiment-empty')).toContainText(
      '没有符合条件的实验任务',
    );
    await expect(page.getByTestId('experiment-detail')).toHaveCount(0);
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
    const historyUrl = await page
      .getByTestId('history-locator')
      .getAttribute('href');
    expect(historyUrl).toBe('/#/quant/operations?event=evt-1042');
    await page.goto(historyUrl!);
    await expect(page.getByTestId('log-detail')).toContainText('evt-1042');
    await expect(page.getByTestId('log-detail')).toContainText(
      'funding_fee_observation_delayed',
    );
    await page.goto('/#/quant/operations?event=evt-9999');
    await expect(page.getByTestId('history-missing')).toContainText('evt-9999');
    await expect(page.getByTestId('log-detail')).toHaveCount(0);
    await page.goto('/#/quant/operations?event=../../evt-1041');
    await expect(page.getByTestId('history-invalid')).toContainText('无效');
    await expect(page.getByTestId('log-detail')).toHaveCount(0);

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

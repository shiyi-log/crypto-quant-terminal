import { expect, test } from '@playwright/test';

import { installDemoFixture } from './demo-fixture';

const viewports = [
  { width: 1920, height: 1080 },
  { width: 1440, height: 1000 },
  { width: 768, height: 1024 },
  { width: 390, height: 844 },
];
const retiredAuthenticationRoutes = [
  'code-login',
  'qrcode-login',
  'register',
  'forget-password',
];

for (const viewport of viewports) {
  for (const method of retiredAuthenticationRoutes) {
    test(`旧认证入口 ${method} · ${viewport.width} · 已认证访问进入404且不额外认证`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      const fixture = await installDemoFixture(page);
      await page.goto('/#/quant/trade-desk');
      await expect(page.locator('.desk-page')).toBeVisible();
      const authenticationBefore = [...fixture.authRequests];
      // 保持同一合成会话，仅切换路由；未知页面仍遵循现有访问守卫。
      await page.evaluate((retiredRoute) => {
        location.hash = `/auth/${retiredRoute}?redirect=%2Fquant%2Facceptance`;
      }, method);
      const content = page.locator('.system-state');
      await expect(content.getByRole('heading')).toHaveText('找不到这个页面');
      await expect(content.locator('.state-code')).toHaveText('404');
      await expect(content.locator('[data-current-location]')).toHaveText(
        `/auth/${method}`,
      );
      await expect(content.locator('form,input,canvas,img')).toHaveCount(0);
      await expect(page.locator('.unsupported-method')).toHaveCount(0);
      await expect(page).toHaveURL(new RegExp(`#/auth/${method}\?`));
      await expect(
        content.getByRole('link', { name: '返回交易台', exact: true }),
      ).toHaveAttribute('href', '#/quant/trade-desk');
      await expect(page.locator('#__app-loading__')).toHaveCount(0);
      const widths = await page.evaluate(() => ({
        document: document.documentElement.scrollWidth,
        viewport: innerWidth,
      }));
      expect(widths.document).toBeLessThanOrEqual(widths.viewport);
      expect(fixture.authRequests).toEqual(authenticationBefore);
      expect(fixture.mutations).toEqual([]);
      expect(fixture.unexpected).toEqual([]);
      expect(fixture.pageErrors).toEqual([]);
      await page.screenshot({
        path: test.info().outputPath(`${method}-${viewport.width}.png`),
        fullPage: true,
      });
    });
  }

  test(`手动登录与本机恢复入口 · ${viewport.width} · 保留原目标`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    const fixture = await installDemoFixture(page);
    await page.addInitScript(() =>
      sessionStorage.setItem('ft_manual_logout', '1'),
    );
    await page.goto('/#/auth/login?redirect=%2Fquant%2Facceptance');
    const form = page.getByRole('form', {
      name: '用户名密码登录',
      exact: true,
    });
    await expect(form).toBeVisible();
    await expect(page.locator('#__app-loading__')).toHaveCount(0);
    await page.screenshot({
      path: test.info().outputPath(`login-${viewport.width}.png`),
      fullPage: true,
    });
    expect(fixture.authRequests).toEqual([]);
    await form.locator('[autocomplete=username]').fill('synthetic-test-user');
    await page
      .getByRole('button', { name: '本机恢复密码', exact: true })
      .click();
    await expect(
      page.getByRole('form', { name: '本机恢复密码', exact: true }),
    ).toBeVisible();
    await expect(page.locator('[name=recovery-username]')).toHaveValue(
      'synthetic-test-user',
    );
    await page.screenshot({
      path: test.info().outputPath(`local-recovery-${viewport.width}.png`),
      fullPage: true,
    });
    expect(fixture.mutations).toEqual([]);
    await page.getByRole('button', { name: '返回登录', exact: true }).click();
    await form.locator('[autocomplete=username]').fill('synthetic-test-user');
    await form
      .locator('[autocomplete=current-password]')
      .fill('synthetic-password-no-real-account');
    await form.getByRole('button', { name: '登录', exact: true }).click();
    await expect(page).toHaveURL(new RegExp('#/quant/acceptance$'));
    await expect(page.locator('.acceptance-page')).toContainText(
      'synthetic-session-a',
    );
    expect(
      fixture.authRequests.filter((request) => !request.startsWith('GET ')),
    ).toEqual(['POST /auth/login']);
    expect(fixture.authRequests).toContain('GET /auth/me');
    expect(fixture.mutations).toEqual([]);
    expect(fixture.unexpected).toEqual([]);
    expect(fixture.pageErrors).toEqual([]);
  });
}

test('会话过期需要显式重新登录，本机恢复深链不自动提交', async ({ page }) => {
  const fixture = await installDemoFixture(page);
  await page.addInitScript(() =>
    sessionStorage.setItem('ft_session_expired', '1'),
  );
  await page.goto('/#/auth/login?redirect=%2Fquant%2Facceptance');
  await expect(page.locator('.access-panel')).toContainText('登录已失效');
  expect(fixture.authRequests).toEqual([]);
  await page.goto(
    '/#/auth/login?recovery=local&redirect=%2Fquant%2Facceptance',
  );
  await expect(
    page.getByRole('form', { name: '本机恢复密码', exact: true }),
  ).toBeVisible();
  expect(fixture.authRequests).toEqual([]);
  expect(fixture.mutations).toEqual([]);
  expect(fixture.unexpected).toEqual([]);
  expect(fixture.pageErrors).toEqual([]);
});

test('未知地址展示真实路径并排除查询内容，恢复操作仍在应用内', async ({
  page,
}) => {
  const fixture = await installDemoFixture(page);
  await page.goto('/#/quant/trade-desk');
  await expect(page.locator('.desk-page')).toBeVisible();
  await page.goto('/#/synthetic-missing-route?reason=synthetic-query-secret');
  const content = page.locator('.system-state');
  await expect(
    content.getByRole('heading', { name: '找不到这个页面' }),
  ).toBeVisible();
  await expect(content.locator('[data-current-location]')).toHaveText(
    '/synthetic-missing-route',
  );
  await expect(content).not.toContainText('synthetic-query-secret');
  await expect(content.locator('form,input')).toHaveCount(0);
  await content.getByRole('link', { name: '返回交易台', exact: true }).focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('.desk-page')).toBeVisible();
  expect(fixture.mutations).toEqual([]);
  expect(fixture.unexpected).toEqual([]);
  expect(fixture.pageErrors).toEqual([]);
});

const systemStates = [
  { route: 'forbidden', title: '当前账户无权访问', retry: false },
  { route: 'internal-error', title: '页面暂时无法读取', retry: true },
  { route: 'offline', title: '暂时无法连接服务', retry: true },
  { route: 'coming-soon', title: '此功能尚未开放', retry: false },
];
for (const viewport of viewports) {
  for (const state of systemStates) {
    test(`系统状态 ${state.route} · ${viewport.width} · 只恢复页面`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      const fixture = await installDemoFixture(page);
      await page.goto(`/#/system/${state.route}?reason=synthetic-query-secret`);
      const content = page.locator('.system-state');
      await expect(
        content.getByRole('heading', { name: state.title, exact: true }),
      ).toBeVisible();
      await expect(content).not.toContainText('synthetic-query-secret');
      await expect(content.locator('form,input')).toHaveCount(0);
      if (state.retry) {
        const original = page.url();
        await Promise.all([
          page.waitForEvent('domcontentloaded'),
          content.locator('[data-action=retry]').click(),
        ]);
        await expect(
          content.getByRole('heading', { name: state.title, exact: true }),
        ).toBeVisible();
        expect(page.url()).toBe(original);
      } else await expect(content.locator('[data-action=back]')).toBeVisible();
      await expect(page.locator('#__app-loading__')).toHaveCount(0);
      const surface = await content.boundingBox();
      expect(surface).not.toBeNull();
      expect(surface!.height).toBeGreaterThanOrEqual(viewport.height);
      const widths = await page.evaluate(() => ({
        document: document.documentElement.scrollWidth,
        viewport: innerWidth,
      }));
      expect(widths.document).toBeLessThanOrEqual(widths.viewport);
      expect(fixture.authRequests).toEqual([]);
      expect(fixture.mutations).toEqual([]);
      expect(fixture.unexpected).toEqual([]);
      expect(fixture.pageErrors).toEqual([]);
      await page.screenshot({
        path: test.info().outputPath(`${state.route}-${viewport.width}.png`),
        fullPage: true,
      });
    });
  }
}

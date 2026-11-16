# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: system-pages.spec.ts >> 系统状态 offline · 390 · 只恢复页面
- Location: e2e/system-pages.spec.ts:165:5

# Error details

```
Error: expect(received).toBeGreaterThanOrEqual(expected)

Expected: >= 844
Received:    570.1875
```

# Page snapshot

```yaml
- main [ref=f1e4]:
  - generic [ref=f1e5]:
    - generic [ref=f1e6]:
      - generic [ref=f1e7]: 量化交易终端
      - generic [ref=f1e8]: 连接中断
    - generic [ref=f1e9]:
      - heading "暂时无法连接服务" [level=1] [ref=f1e10]
      - paragraph [ref=f1e11]: 页面当前无法取得服务响应。请检查网络与本机服务，再重试原地址。
      - generic [ref=f1e12]:
        - term [ref=f1e13]: 当前地址
        - definition [ref=f1e14]: /system/offline
      - paragraph [ref=f1e15]: 连接恢复前，账户、风险与订单状态保持未知，不能据此判断交易已停止。
      - navigation "页面恢复操作" [ref=f1e16]:
        - button "重试当前页面" [ref=f1e17] [cursor=pointer]
        - link "返回交易台" [ref=f1e18] [cursor=pointer]:
          - /url: "#/quant/trade-desk"
    - generic [ref=f1e19]: 交易状态以账户、订单与风险证据为准
```

# Test source

```ts
  91  |     });
  92  |     expect(fixture.mutations).toEqual([]);
  93  |     await page.getByRole('button', { name: '返回登录', exact: true }).click();
  94  |     await form.locator('[autocomplete=username]').fill('synthetic-test-user');
  95  |     await form
  96  |       .locator('[autocomplete=current-password]')
  97  |       .fill('synthetic-password-no-real-account');
  98  |     await form.getByRole('button', { name: '登录', exact: true }).click();
  99  |     await expect(page).toHaveURL(new RegExp('#/quant/acceptance$'));
  100 |     await expect(page.locator('.acceptance-page')).toContainText(
  101 |       'synthetic-session-a',
  102 |     );
  103 |     expect(
  104 |       fixture.authRequests.filter((request) => !request.startsWith('GET ')),
  105 |     ).toEqual(['POST /auth/login']);
  106 |     expect(fixture.authRequests).toContain('GET /auth/me');
  107 |     expect(fixture.mutations).toEqual([]);
  108 |     expect(fixture.unexpected).toEqual([]);
  109 |     expect(fixture.pageErrors).toEqual([]);
  110 |   });
  111 | }
  112 | 
  113 | test('会话过期需要显式重新登录，本机恢复深链不自动提交', async ({ page }) => {
  114 |   const fixture = await installDemoFixture(page);
  115 |   await page.addInitScript(() =>
  116 |     sessionStorage.setItem('ft_session_expired', '1'),
  117 |   );
  118 |   await page.goto('/#/auth/login?redirect=%2Fquant%2Facceptance');
  119 |   await expect(page.locator('.access-panel')).toContainText('登录已失效');
  120 |   expect(fixture.authRequests).toEqual([]);
  121 |   await page.goto(
  122 |     '/#/auth/login?recovery=local&redirect=%2Fquant%2Facceptance',
  123 |   );
  124 |   await expect(
  125 |     page.getByRole('form', { name: '本机恢复密码', exact: true }),
  126 |   ).toBeVisible();
  127 |   expect(fixture.authRequests).toEqual([]);
  128 |   expect(fixture.mutations).toEqual([]);
  129 |   expect(fixture.unexpected).toEqual([]);
  130 |   expect(fixture.pageErrors).toEqual([]);
  131 | });
  132 | 
  133 | test('未知地址展示真实路径并排除查询内容，恢复操作仍在应用内', async ({
  134 |   page,
  135 | }) => {
  136 |   const fixture = await installDemoFixture(page);
  137 |   await page.goto('/#/quant/trade-desk');
  138 |   await expect(page.locator('.desk-page')).toBeVisible();
  139 |   await page.goto('/#/synthetic-missing-route?reason=synthetic-query-secret');
  140 |   const content = page.locator('.system-state');
  141 |   await expect(
  142 |     content.getByRole('heading', { name: '找不到这个页面' }),
  143 |   ).toBeVisible();
  144 |   await expect(content.locator('[data-current-location]')).toHaveText(
  145 |     '/synthetic-missing-route',
  146 |   );
  147 |   await expect(content).not.toContainText('synthetic-query-secret');
  148 |   await expect(content.locator('form,input')).toHaveCount(0);
  149 |   await content.getByRole('link', { name: '返回交易台', exact: true }).focus();
  150 |   await page.keyboard.press('Enter');
  151 |   await expect(page.locator('.desk-page')).toBeVisible();
  152 |   expect(fixture.mutations).toEqual([]);
  153 |   expect(fixture.unexpected).toEqual([]);
  154 |   expect(fixture.pageErrors).toEqual([]);
  155 | });
  156 | 
  157 | const systemStates = [
  158 |   { route: 'forbidden', title: '当前账户无权访问', retry: false },
  159 |   { route: 'internal-error', title: '页面暂时无法读取', retry: true },
  160 |   { route: 'offline', title: '暂时无法连接服务', retry: true },
  161 |   { route: 'coming-soon', title: '此功能尚未开放', retry: false },
  162 | ];
  163 | for (const viewport of viewports) {
  164 |   for (const state of systemStates) {
  165 |     test(`系统状态 ${state.route} · ${viewport.width} · 只恢复页面`, async ({
  166 |       page,
  167 |     }) => {
  168 |       await page.setViewportSize(viewport);
  169 |       const fixture = await installDemoFixture(page);
  170 |       await page.goto(`/#/system/${state.route}?reason=synthetic-query-secret`);
  171 |       const content = page.locator('.system-state');
  172 |       await expect(
  173 |         content.getByRole('heading', { name: state.title, exact: true }),
  174 |       ).toBeVisible();
  175 |       await expect(content).not.toContainText('synthetic-query-secret');
  176 |       await expect(content.locator('form,input')).toHaveCount(0);
  177 |       if (state.retry) {
  178 |         const original = page.url();
  179 |         await Promise.all([
  180 |           page.waitForEvent('domcontentloaded'),
  181 |           content.locator('[data-action=retry]').click(),
  182 |         ]);
  183 |         await expect(
  184 |           content.getByRole('heading', { name: state.title, exact: true }),
  185 |         ).toBeVisible();
  186 |         expect(page.url()).toBe(original);
  187 |       } else await expect(content.locator('[data-action=back]')).toBeVisible();
  188 |       await expect(page.locator('#__app-loading__')).toHaveCount(0);
  189 |       const surface = await content.boundingBox();
  190 |       expect(surface).not.toBeNull();
> 191 |       expect(surface!.height).toBeGreaterThanOrEqual(viewport.height);
      |                               ^ Error: expect(received).toBeGreaterThanOrEqual(expected)
  192 |       const widths = await page.evaluate(() => ({
  193 |         document: document.documentElement.scrollWidth,
  194 |         viewport: innerWidth,
  195 |       }));
  196 |       expect(widths.document).toBeLessThanOrEqual(widths.viewport);
  197 |       expect(fixture.authRequests).toEqual([]);
  198 |       expect(fixture.mutations).toEqual([]);
  199 |       expect(fixture.unexpected).toEqual([]);
  200 |       expect(fixture.pageErrors).toEqual([]);
  201 |       await page.screenshot({
  202 |         path: test.info().outputPath(`${state.route}-${viewport.width}.png`),
  203 |         fullPage: true,
  204 |       });
  205 |     });
  206 |   }
  207 | }
  208 | 
```
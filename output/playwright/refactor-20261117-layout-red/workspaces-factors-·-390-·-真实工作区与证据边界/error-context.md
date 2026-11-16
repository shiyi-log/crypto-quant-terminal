# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: workspaces.spec.ts >> factors · 390 · 真实工作区与证据边界
- Location: e2e/workspaces.spec.ts:72:5

# Error details

```
Error: expect(received).toBe(expected) // Object.is equality

Expected: true
Received: false

Call Log:
- Timeout 10000ms exceeded while waiting on the predicate
```

# Page snapshot

```yaml
- generic [ref=e4]:
  - generic [ref=e5]:
    - generic [ref=e6]:
      - banner [ref=e7]:
        - link "终端 量化交易终端" [ref=e10] [cursor=pointer]:
          - /url: javascript:void 0
          - generic [ref=e11]: 终端
          - generic: 量化交易终端
        - button [ref=e14] [cursor=pointer]
        - generic [ref=e16]:
          - generic [ref=e18] [cursor=pointer]
          - button "light" [ref=e23] [cursor=pointer]
          - button [ref=e32] [cursor=pointer]
          - button [ref=e38] [cursor=pointer]
          - button [ref=e40] [cursor=pointer]
          - button [ref=e46] [cursor=pointer]
          - button [ref=e50] [cursor=pointer]
          - button [ref=e55] [cursor=pointer]
          - button "DE" [ref=e59] [cursor=pointer]
      - generic [ref=e66]:
        - generic [ref=e71]:
          - generic [ref=e72] [cursor=pointer]: 交易台
          - generic [ref=e87] [cursor=pointer]: 因子实验室
        - generic [ref=e103]:
          - button [ref=e104] [cursor=pointer]
          - generic [ref=e111] [cursor=pointer]
          - generic [ref=e115] [cursor=pointer]
    - main [ref=e123]:
      - generic [ref=e124]:
        - generic [ref=e125]:
          - generic [ref=e126]:
            - generic [ref=e127]: 量
            - generic [ref=e128]:
              - strong [ref=e129]: 量化交易终端
              - generic [ref=e130]: 因子实验室
          - status [ref=e131]:
            - strong [ref=e132]: 历史研究与证据
            - generic [ref=e133]: 交易账户不适用
          - link "设置与安全" [ref=e134] [cursor=pointer]:
            - /url: "#/quant/security"
          - button "工作区" [ref=e135] [cursor=pointer]
        - paragraph [ref=e136]: 数据来源与完整性按各工作区分别核对。
      - main [ref=e138]:
        - generic [ref=e139]:
          - generic [ref=e140]:
            - paragraph [ref=e141]: 研究 / 历史横截面监控
            - heading "因子实验室" [level=1] [ref=e142]
            - paragraph [ref=e143]: 比较历史基线与近期统计，逐项查看漂移和证据。这里不产生交易信号。
          - button "刷新监控快照" [ref=e144] [cursor=pointer]
        - region "来源与时效" [ref=e145]:
          - generic [ref=e146]:
            - generic [ref=e147]: 监控范围
            - strong [ref=e148]: 旧研究 / 日线横截面
          - generic [ref=e149]:
            - generic [ref=e150]: 源快照时间
            - strong [ref=e151]: 2026-11-16T19:20:15.213Z
          - generic [ref=e152]:
            - generic [ref=e153]: 读取状态
            - strong [ref=e154]: 一小时内历史快照
          - generic [ref=e155]:
            - generic [ref=e156]: 明确告警
            - strong [ref=e157]: 1 / 2 个因子
        - generic [ref=e158]:
          - region [ref=e159]:
            - generic [ref=e160]:
              - generic [ref=e161]:
                - heading "因子观察表" [level=2] [ref=e162]
                - paragraph [ref=e163]: 零值按零显示；缺字段保留未知。
              - generic [ref=e164]:
                - generic [ref=e165]:
                  - text: 分组
                  - combobox "因子分组" [ref=e166]:
                    - option "全部" [selected]
                    - option "动量"
                    - option "成交量"
                - generic [ref=e167]:
                  - checkbox "只看明确告警" [ref=e168]
                  - text: 只看明确告警
            - generic "因子统计表，可横向滚动" [ref=e169]:
              - table [ref=e170]:
                - rowgroup [ref=e171]:
                  - row [ref=e172]:
                    - columnheader "因子 / 分组" [ref=e173]
                    - columnheader "基线 IC" [ref=e174]
                    - columnheader "近期 IC" [ref=e175]
                    - columnheader "近期 t 值" [ref=e176]
                    - columnheader "告警事实" [ref=e177]
                - rowgroup [ref=e178]:
                  - row [ref=e179]:
                    - rowheader "synthetic_momentum_7 动量" [ref=e180]:
                      - button "synthetic_momentum_7" [pressed] [ref=e181] [cursor=pointer]
                      - generic [ref=e182]: 动量
                    - cell "0.1200" [ref=e183]
                    - cell "-0.0400" [ref=e184]
                    - cell "-2.3000" [ref=e185]
                    - cell "有告警" [ref=e186]
                  - row [ref=e187]:
                    - rowheader "synthetic_volume_ratio 成交量" [ref=e188]:
                      - button "synthetic_volume_ratio" [ref=e189] [cursor=pointer]
                      - generic [ref=e190]: 成交量
                    - cell "未知" [ref=e191]
                    - cell "0" [ref=e192]
                    - cell "未知" [ref=e193]
                    - cell "未知" [ref=e194]
          - complementary [ref=e195]:
            - heading "synthetic_momentum_7" [level=2] [ref=e196]
            - paragraph [ref=e197]: 动量
            - generic [ref=e198]:
              - generic [ref=e199]:
                - term [ref=e200]: 源状态文本
                - definition [ref=e201]: 符号反转
              - generic [ref=e202]:
                - term [ref=e203]: IC 差异
                - definition [ref=e204]: "-0.1600"
              - generic [ref=e205]:
                - term [ref=e206]: 弱信号标记
                - definition [ref=e207]: 否
              - generic [ref=e208]:
                - term [ref=e209]: 符号反转 / 衰减
                - definition [ref=e210]: 反转 / 衰减
            - generic [ref=e211]:
              - strong [ref=e212]: 版本身份未知
              - paragraph [ref=e213]: 此接口没有因子版本、输入数据指纹或完整统计产物，不能据统计摘要补造身份。
            - heading "进入新研究前仍需补证" [level=3] [ref=e214]
            - list [ref=e215]:
              - listitem [ref=e216]:
                - generic [ref=e217]: 逐币 / 逐周期时序评估
                - emphasis [ref=e218]: 未接通
              - listitem [ref=e219]:
                - generic [ref=e220]: 相关性、市场阶段与消融
                - emphasis [ref=e221]: 未接通
              - listitem [ref=e222]:
                - generic [ref=e223]: 时间块统计与全族校正
                - emphasis [ref=e224]: 未接通
              - listitem [ref=e225]:
                - generic [ref=e226]: 一次性最终留出
                - emphasis [ref=e227]: 未接通
            - paragraph [ref=e228]: 不授予 Demo 准入
            - paragraph [ref=e229]: 历史横截面 IC 不替代 BTC、ETH、TRX 的单币时序研究；也不证明完整成本后收益。
            - navigation "历史研究入口" [ref=e230]:
              - link "旧监控详情" [ref=e231] [cursor=pointer]:
                - /url: "#/quant/ops"
              - link "历史策略研究" [ref=e232] [cursor=pointer]:
                - /url: "#/quant/research"
              - link "研究证据账" [ref=e233] [cursor=pointer]:
                - /url: "#/quant/research-ledger"
  - button "偏好设置" [ref=e235] [cursor=pointer]
  - button [ref=e239] [cursor=pointer]
```

# Test source

```ts
  78  |       const content = page.locator(workspace.selector);
  79  |       await expect(content).toHaveCount(1);
  80  |       if (workspace.route === 'data-quality')
  81  |         await expect(content.locator('[data-dataset-inspector] h3')).toHaveText(
  82  |           workspace.marker,
  83  |         );
  84  |       else
  85  |         await expect(
  86  |           content.getByText(workspace.marker, { exact: true }).first(),
  87  |         ).toBeVisible();
  88  |       await expect(content).toHaveCount(1);
  89  |       await expect(page.locator('.quant-shell-header')).toBeVisible();
  90  |       await expect(
  91  |         page.locator('#quant-workspace-navigation a[aria-current=page]'),
  92  |       ).toHaveText(workspace.label);
  93  |       await expect(page.locator('.quant-shell-environment')).toContainText(
  94  |         '历史研究与证据',
  95  |       );
  96  |       await expect(page.locator('.quant-shell-legacy')).toHaveCount(0);
  97  |       await expect(
  98  |         page.getByRole('button', { name: '启动', exact: true }),
  99  |       ).toHaveCount(0);
  100 |       if (workspace.route === 'factors') {
  101 |         await expect(
  102 |           content.getByText('版本身份未知', { exact: true }),
  103 |         ).toBeVisible();
  104 |         await expect(
  105 |           content.getByText('不授予 Demo 准入', { exact: true }),
  106 |         ).toBeVisible();
  107 |       } else if (workspace.route === 'experiments') {
  108 |         await expect(content.locator('.run-inspector')).toContainText(
  109 |           '执行成功',
  110 |         );
  111 |         await expect(content.locator('.run-inspector')).toContainText(
  112 |           '研究拒绝',
  113 |         );
  114 |         await expect(content.locator('.run-inspector')).toContainText('未知');
  115 |         await expect(content).toContainText('不构成上线批准');
  116 |       } else if (workspace.route === 'data-quality') {
  117 |         await expect(content.locator('[data-quality-gates]')).toContainText(
  118 |           '未取得证据',
  119 |         );
  120 |         await expect(content.locator('[data-quality-gates]')).toContainText(
  121 |           '365 天逐频率覆盖',
  122 |         );
  123 |       } else {
  124 |         await expect(content.locator('[data-account-gates]')).toContainText(
  125 |           '尚未取得账户执行资格证据',
  126 |         );
  127 |         await expect(content.locator('[data-account-gates]')).toContainText(
  128 |           '唯一写者',
  129 |         );
  130 |         await expect(content).not.toContainText(
  131 |           'synthetic-private-payload-must-not-render',
  132 |         );
  133 |       }
  134 |       await readyForScreenshot(page);
  135 |       const width = await page.evaluate(() => ({
  136 |         document: document.documentElement.scrollWidth,
  137 |         viewport: innerWidth,
  138 |       }));
  139 |       expect(width.document).toBeLessThanOrEqual(width.viewport);
  140 |       const historyTargets = await content
  141 |         .locator('a[href]')
  142 |         .evaluateAll((links) =>
  143 |           links
  144 |             .map((link) => link.getAttribute('href') || '')
  145 |             .filter((href) => href.includes('/quant/')),
  146 |         );
  147 |       expect(historyTargets.length).toBeGreaterThan(0);
  148 |       expect(historyTargets.every((href) => href.includes('#/quant/'))).toBe(
  149 |         true,
  150 |       );
  151 |       assertFixtureReadOnly(fixture);
  152 |       await page.screenshot({
  153 |         path: test
  154 |           .info()
  155 |           .outputPath(`${workspace.route}-${viewport.width}.png`),
  156 |         fullPage: true,
  157 |       });
  158 |       const scrollRegion = page.locator('[data-layout-region=scroll]');
  159 |       const maxScroll = await scrollRegion.evaluate(
  160 |         (element) => element.scrollHeight - element.clientHeight,
  161 |       );
  162 |       if (maxScroll > 0) {
  163 |         for (const [segment, fraction] of [
  164 |           ['middle', 0.5],
  165 |           ['bottom', 1],
  166 |         ] as const) {
  167 |           await scrollRegion.evaluate((element, ratio) => {
  168 |             element.scrollTop =
  169 |               (element.scrollHeight - element.clientHeight) * ratio;
  170 |           }, fraction);
  171 |           await expect
  172 |             .poll(() =>
  173 |               page.locator('.quant-shell-environment').evaluate((element) => {
  174 |                 const bounds = element.getBoundingClientRect();
  175 |                 return bounds.top >= 0 && bounds.bottom <= innerHeight;
  176 |               }),
  177 |             )
> 178 |             .toBe(true);
      |              ^ Error: expect(received).toBe(expected) // Object.is equality
  179 |           await page.screenshot({
  180 |             path: test
  181 |               .info()
  182 |               .outputPath(
  183 |                 `${workspace.route}-${viewport.width}-${segment}.png`,
  184 |               ),
  185 |           });
  186 |         }
  187 |         await scrollRegion.evaluate((element) => {
  188 |           element.scrollTop = 0;
  189 |         });
  190 |       }
  191 |     });
  192 |   }
  193 | 
  194 |   test(`工作区键盘导航与活动焦点 · ${viewport.width}`, async ({ page }) => {
  195 |     await page.setViewportSize(viewport);
  196 |     const fixture = await installWorkspaceFixture(page);
  197 |     await page.goto('/#/quant/trade-desk');
  198 |     await expect(page.locator('.desk-page')).toBeVisible();
  199 |     const shell = page.locator('.quant-shell-header');
  200 |     await readyForScreenshot(page);
  201 |     const initialShellTop = await shell.evaluate(
  202 |       (element) => element.getBoundingClientRect().top,
  203 |     );
  204 |     const scroll = page.locator('#__vben_layout_scroll');
  205 |     await scroll.evaluate((element) => {
  206 |       element.scrollTop = 450;
  207 |     });
  208 |     await expect
  209 |       .poll(() => scroll.evaluate((element) => element.scrollTop))
  210 |       .toBeGreaterThan(0);
  211 |     const scrolledShellTop = await shell.evaluate(
  212 |       (element) => element.getBoundingClientRect().top,
  213 |     );
  214 |     expect(Math.abs(scrolledShellTop - initialShellTop)).toBeLessThanOrEqual(1);
  215 |     await scroll.evaluate((element) => {
  216 |       element.scrollTop = 0;
  217 |     });
  218 |     const navigation = page.getByRole('navigation', {
  219 |       name: '工作区',
  220 |       exact: true,
  221 |     });
  222 |     const toggle = page.getByRole('button', { name: '工作区', exact: true });
  223 |     if (viewport.width <= 900) {
  224 |       await expect(navigation).toBeHidden();
  225 |       await toggle.focus();
  226 |       await page.keyboard.press('Enter');
  227 |       await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  228 |     }
  229 |     const factorLink = navigation.getByRole('link', {
  230 |       name: '因子实验室',
  231 |       exact: true,
  232 |     });
  233 |     await factorLink.focus();
  234 |     await page.keyboard.press('Tab');
  235 |     await page.keyboard.press('Shift+Tab');
  236 |     await expect(factorLink).toBeFocused();
  237 |     const outline = await factorLink.evaluate(
  238 |       (element) => getComputedStyle(element).outlineStyle,
  239 |     );
  240 |     expect(outline).not.toBe('none');
  241 |     if (viewport.width <= 900) {
  242 |       await page.keyboard.press('Escape');
  243 |       await expect(toggle).toBeFocused();
  244 |       await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  245 |       await expect(navigation).toBeHidden();
  246 |       await page.keyboard.press('Enter');
  247 |       await factorLink.focus();
  248 |     }
  249 |     await page.keyboard.press('Enter');
  250 |     await expect(page).toHaveURL(new RegExp('#/quant/factors$'));
  251 |     await expect(page.locator('.factor-workspace')).toContainText(FACTOR_NAME);
  252 |     if (viewport.width <= 900) {
  253 |       await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  254 |       await toggle.click();
  255 |     }
  256 |     await expect(
  257 |       navigation.getByRole('link', { name: '因子实验室', exact: true }),
  258 |     ).toHaveAttribute('aria-current', 'page');
  259 |     await expect(navigation.locator('[aria-current=page]')).toHaveCount(1);
  260 |     assertFixtureReadOnly(fixture);
  261 |   });
  262 | }
  263 | 
  264 | for (const viewport of viewports) {
  265 |   test(`因子失败刷新保留精确统计，键盘筛选保留未知 · ${viewport.width}`, async ({
  266 |     page,
  267 |   }) => {
  268 |     await page.setViewportSize(viewport);
  269 |     const fixture = await installWorkspaceFixture(page);
  270 |     await page.goto('/#/quant/factors');
  271 |     const content = page.locator('.factor-workspace');
  272 |     await expect(content).toContainText('-0.0400');
  273 |     fixture.readFailures.add(API_PATHS.factors);
  274 |     await content.getByTestId('refresh-factors').click();
  275 |     await expect(content.getByRole('alert')).toContainText('保留上次成功快照');
  276 |     await expect(content).toContainText('-0.0400');
  277 |     await content
  278 |       .getByRole('combobox', { name: '因子分组' })
```
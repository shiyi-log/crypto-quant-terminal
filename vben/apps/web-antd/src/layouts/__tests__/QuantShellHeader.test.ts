import type { App } from 'vue';

import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

import QuantShellHeader from '../components/QuantShellHeader.vue';

const mounted: { app: App; element: HTMLDivElement }[] = [];
const paths = [
  'factors',
  'experiments',
  'data-quality',
  'operations',
  'trade-desk',
  'acceptance',
  'binance-demo',
  'overview',
  'positions',
  'market',
  'live',
  'pairlist',
  'logs',
  'research',
  'strategy-lab',
  'strategy-focus',
  'backtest',
  'detail',
  'tasks',
  'iteration',
  'orderflow',
  'price-spikes',
  'data',
  'ops',
  'security',
  'unknown-future-page',
];

async function open(path: string) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      ...paths.map((item) => ({
        path: '/quant/' + item,
        component: { template: '<div />' },
      })),
      { path: '/profile', component: { template: '<div />' } },
    ],
  });
  await router.push(path);
  await router.isReady();
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({
    render: () =>
      h(
        QuantShellHeader,
        {},
        {
          'legacy-controls': () => h('button', '启动旧引擎'),
        },
      ),
  });
  app.use(router).mount(element);
  mounted.push({ app, element });
  return {
    router,
    element,
    get(selector: string) {
      const item = element.querySelector<HTMLElement>(selector);
      if (!item) throw new Error('Expected rendered element: ' + selector);
      return item;
    },
  };
}

afterEach(() =>
  mounted.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  }),
);

describe('quant workspace shell boundaries', () => {
  it.each(['trade-desk', 'acceptance', 'binance-demo'])(
    '%s labels Binance Demo and does not expose legacy engine commands',
    async (path) => {
      const { element, get } = await open('/quant/' + path);
      expect(get('[role="status"]').textContent).toContain('Binance Demo');
      expect(element.textContent).toContain('账户身份未核验');
      expect(element.textContent).not.toContain('启动旧引擎');
      expect(element.textContent).not.toContain('旧交易引擎操作');
    },
  );

  it.each(['overview', 'positions', 'market', 'live', 'pairlist', 'logs'])(
    '%s keeps legacy commands within an explicitly named engine boundary',
    async (path) => {
      const { element, get } = await open('/quant/' + path);
      expect(get('[role="status"]').textContent).toContain('旧交易引擎');
      expect(get('[role="status"]').textContent).toContain('账户模式未核验');
      expect(element.textContent).toContain('旧交易引擎操作');
      expect(
        get('section[aria-label="旧交易引擎操作"] button').textContent,
      ).toBe('启动旧引擎');
    },
  );

  it.each(['research', 'orderflow', 'backtest', 'data', 'ops'])(
    '%s does not imply a live or Demo trading account',
    async (path) => {
      const { element, get } = await open('/quant/' + path);
      expect(get('[role="status"]').textContent).toContain('历史研究');
      expect(get('[role="status"]').textContent).not.toContain('Binance Demo');
      expect(element.textContent).not.toContain('启动旧引擎');
    },
  );

  it('identifies paper views and keeps focus filtering in the route', async () => {
    const { router, element, get } = await open(
      '/quant/strategy-focus?strategy=s03',
    );
    expect(get('[role="status"]').textContent).toContain('纸面研究');
    expect(router.currentRoute.value.fullPath).toBe(
      '/quant/strategy-focus?strategy=s03',
    );
    expect(element.textContent).not.toContain('启动旧引擎');
  });

  it('exposes reachable links and keeps the current area visible after navigation', async () => {
    const { router, element, get } = await open('/quant/trade-desk');
    const links = [
      ...element.querySelectorAll<HTMLAnchorElement>(
        'nav[aria-label="工作区"] a',
      ),
    ];
    expect(links).toHaveLength(8);
    for (const link of links)
      expect(
        router.resolve(link.getAttribute('href')!).matched,
      ).not.toHaveLength(0);
    expect(
      links
        .find((link) => link.getAttribute('aria-current') === 'page')
        ?.textContent?.trim(),
    ).toBe('交易台');
    get('button[aria-controls="quant-workspace-navigation"]').click();
    await nextTick();
    get('a[href="/quant/acceptance"]').click();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.path).toBe('/quant/acceptance'),
    );
    expect(
      get('nav[aria-label="工作区"] [aria-current="page"]').textContent?.trim(),
    ).toBe('验收与上线');
    expect(
      get('button[aria-controls="quant-workspace-navigation"]').getAttribute(
        'aria-expanded',
      ),
    ).toBe('false');
  });

  it('opens and closes workspace navigation without adding a write action', async () => {
    const { element, get } = await open('/quant/trade-desk');
    const toggle = get('button[aria-controls="quant-workspace-navigation"]');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    toggle.click();
    await nextTick();
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    get('nav').dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    );
    await nextTick();
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(element.querySelectorAll('button')).toHaveLength(1);
    await nextTick();
    expect(document.activeElement).toBe(toggle);
  });

  it('distinguishes the temporary factor monitor from the operations workspace', async () => {
    const { router, element, get } = await open('/quant/ops?workspace=factors');
    expect(element.querySelectorAll('nav [aria-current="page"]')).toHaveLength(
      1,
    );
    expect(get('nav [aria-current="page"]').textContent?.trim()).toBe(
      '因子实验室',
    );
    await router.push('/quant/ops');
    await nextTick();
    expect(element.querySelectorAll('nav [aria-current="page"]')).toHaveLength(
      1,
    );
    expect(get('nav [aria-current="page"]').textContent?.trim()).toBe(
      '运维与证据',
    );
  });

  it('fails closed for an unmapped quant view', async () => {
    const { element, get } = await open('/quant/unknown-future-page');
    expect(get('[role="status"]').textContent).toContain('环境未确认');
    expect(element.textContent).not.toContain('启动旧引擎');
  });

  it('keeps unrelated profile pages outside the quant workspace header', async () => {
    const { element } = await open('/profile');
    expect(element.querySelector('header')).toBeNull();
  });
});

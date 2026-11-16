import type { App } from 'vue';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

const api = vi.hoisted(() => ({
  config: vi.fn(),
  open: vi.fn(),
  whitelist: vi.fn(),
}));
vi.mock('#/api/freqtrade', () => ({
  getConfig: api.config,
  getOpenTrades: api.open,
  getWhitelist: api.whitelist,
}));
// External chart/stream IO is covered by PairChart's own contract tests.
vi.mock('./PairChart.vue', () => ({
  default: { props: ['pair', 'timeframe'], template: '<div />' },
}));
import MarketWorkspace from './index.vue';

const mounted: { app: App; element: HTMLDivElement }[] = [];
async function open(query = '') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/quant/market', component: MarketWorkspace }],
  });
  await router.push('/quant/market' + query);
  await router.isReady();
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({ render: () => h(MarketWorkspace) });
  app.use(router).mount(element);
  mounted.push({ app, element });
  await vi.waitFor(() =>
    expect(element.textContent).toContain('BTC/USDT:USDT'),
  );
  return {
    router,
    element,
    get<T extends HTMLElement = HTMLElement>(selector: string) {
      const item = element.querySelector<T>(selector);
      if (!item) throw new Error('Expected ' + selector);
      return item;
    },
  };
}
function button(element: HTMLElement, label: string) {
  const found = [...element.querySelectorAll<HTMLButtonElement>('button')].find(
    (item) => item.textContent?.includes(label),
  );
  if (!found) throw new Error('Expected button ' + label);
  return found;
}
beforeEach(() => {
  api.config.mockReset().mockResolvedValue({
    dry_run: true,
    timeframe: '1h',
    strategy: 'LegacyStrategy',
    state: 'running',
  });
  api.whitelist
    .mockReset()
    .mockResolvedValue({ whitelist: ['BTC/USDT:USDT', 'ETH/USDT:USDT'] });
  api.open
    .mockReset()
    .mockResolvedValue([{ trade_id: 9, pair: 'SOL/USDT:USDT' }]);
});
afterEach(() =>
  mounted.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  }),
);

describe('legacy market workspace evidence scopes', () => {
  it('identifies legacy dry run and uses the requested exact pair and timeframe', async () => {
    const view = await open('?pair=ETH%2FUSDT%3AUSDT&timeframe=4h');
    expect(view.element.textContent).toContain('旧 Freqtrade 交易引擎');
    expect(view.element.textContent).toContain('模拟 dry-run');
    expect(view.get('[data-testid=market-selection]').textContent).toContain(
      'ETH/USDT:USDT',
    );
    expect(view.get('[data-testid=market-selection]').textContent).toContain(
      '4h',
    );
    expect(view.element.querySelectorAll('button[data-pair]')).toHaveLength(3);
  });
  it('retains a previously observed open-only pair when its source fails', async () => {
    const view = await open();
    api.open.mockRejectedValueOnce(
      new Error('postgres://synthetic:secret@example/test'),
    );
    api.whitelist.mockResolvedValueOnce({ whitelist: ['BTC/USDT:USDT'] });
    button(view.element, '刷新目录').click();
    await vi.waitFor(() =>
      expect(view.element.querySelector('[role=alert]')?.textContent).toContain(
        '持仓目录读取失败',
      ),
    );
    expect(view.element.textContent).toContain('SOL/USDT:USDT');
    expect(view.element.textContent).not.toContain('secret');
    expect(view.get('[data-pair="SOL/USDT:USDT"]').textContent).toContain(
      '旧快照',
    );
  });
  it('rejects malformed lists without changing the last known directory into empty', async () => {
    const view = await open();
    api.whitelist.mockResolvedValueOnce({ whitelist: {} });
    button(view.element, '刷新目录').click();
    await vi.waitFor(() =>
      expect(view.element.querySelector('[role=alert]')?.textContent).toContain(
        '白名单目录读取失败',
      ),
    );
    expect(view.element.textContent).toContain('ETH/USDT:USDT');
  });
  it('keeps an unavailable exact target visible without substituting another pair', async () => {
    const view = await open('?pair=XRP%2FUSDT%3AUSDT');
    expect(view.get('[data-testid=market-selection]').textContent).toContain(
      'XRP/USDT:USDT',
    );
    expect(view.element.querySelector('[role=alert]')?.textContent).toContain(
      '不在最近成功的目录',
    );
    expect(view.element.querySelector('[data-testid=market-chart]')).toBeNull();
  });
  it('keeps an unavailable requested pair when only its timeframe changes', async () => {
    const view = await open('?pair=XRP%2FUSDT%3AUSDT');
    const select = view.get<HTMLSelectElement>(
      'select[aria-label=行情蜡烛周期]',
    );
    select.value = '4h';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    await vi.waitFor(() =>
      expect(view.router.currentRoute.value.query.timeframe).toBe('4h'),
    );
    expect(view.router.currentRoute.value.query.pair).toBe('XRP/USDT:USDT');
    expect(view.element.querySelector('[data-testid=market-chart]')).toBeNull();
  });
  it('takes reused route defaults from the URL and blocks invalid repeated parameters', async () => {
    const view = await open('?pair=ETH%2FUSDT%3AUSDT&timeframe=4h');
    await view.router.push('/quant/market');
    await nextTick();
    expect(view.get('[data-testid=market-selection]').textContent).toContain(
      'BTC/USDT:USDT',
    );
    expect(view.get('[data-testid=market-selection]').textContent).toContain(
      '1h',
    );
    for (const query of [
      '?pair=',
      '?pair=a&pair=b',
      '?timeframe=2h',
      '?timeframe=1h&timeframe=4h',
    ]) {
      await view.router.push('/quant/market' + query);
      await nextTick();
      expect(view.element.querySelector('[role=alert]')?.textContent).toContain(
        '查询参数无效',
      );
      expect(
        view.element.querySelector('[data-testid=market-chart]'),
      ).toBeNull();
    }
  });
  it('does not infer real trading when dry_run is absent or not boolean', async () => {
    api.config.mockResolvedValueOnce({ dry_run: 'false', timeframe: '1h' });
    const view = await open();
    expect(view.get('[data-testid=market-environment]').textContent).toContain(
      '环境未知',
    );
    expect(
      view.get('[data-testid=market-environment]').textContent,
    ).not.toContain('实盘');
  });
  it('writes pair selection into the URL so a reload retains the same scope', async () => {
    const view = await open();
    view.get<HTMLButtonElement>('[data-pair="ETH/USDT:USDT"]').click();
    await vi.waitFor(() =>
      expect(view.router.currentRoute.value.query.pair).toBe('ETH/USDT:USDT'),
    );
    expect(view.get('[data-testid=market-selection]').textContent).toContain(
      'ETH/USDT:USDT',
    );
  });
});

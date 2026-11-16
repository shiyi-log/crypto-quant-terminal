import type { App } from 'vue';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

const api = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock('#/api/database', () => ({ getOrderflowPriceSpikes: api.fetch }));
vi.mock('@vben/plugins/echarts', () => ({
  EchartsUI: { template: '<div />' },
  useEcharts: () => ({ renderEcharts: () => {} }),
}));
import PriceWorkspace from './price-spikes.vue';

const pair = 'BTC/USDT:USDT';
function fixture() {
  return {
    items: [
      {
        event_id: 'exact-event',
        pair,
        timestamp: 1000,
        status: 'unknown',
        observation: { volume_ratio: 4, ratio_band: '4to8' },
        outcome: null,
        profile: null,
        profile_status: null,
      },
      {
        event_id: 'none-event',
        pair,
        timestamp: 2000,
        status: 'matured',
        observation: { volume_ratio: 1, ratio_band: '1to2' },
        outcome: { direction: 'none', return_pct: 0.001 },
        profile: null,
        profile_status: null,
      },
    ],
    market: 'futures',
    timeframe: '5m',
    direction: 'any',
    sample_scope: 'live_online_only',
    method_version: 'orderflow-price-spike-close3-v4',
    limitations: [],
    warmup: {},
    stats: {
      method_version: 'orderflow-price-spike-close3-v4',
      event_threshold_pct: 0.015,
      forward_bars: 3,
      ratio_bands: ['lt1', '1to2', '2to4', '4to8', '8to20', 'ge20'],
      sample_scope: 'live_online_only',
      unknown_excluded: true,
      invalid_excluded: true,
      by_pair: {
        [pair]: {
          candidate_count: 2,
          matured_count: 1,
          invalid_count: 0,
          base_p_any: 0,
          bands: {},
        },
      } as Record<
        string,
        {
          candidate_count: number;
          matured_count: number;
          invalid_count: number;
          base_p_any: number;
          bands: Record<string, unknown>;
        }
      >,
      profile_by_pair: { [pair]: { up: [], down: [] } } as Record<
        string,
        { up: unknown[]; down: unknown[] }
      >,
    },
  };
}
const mounted: { app: App; element: HTMLDivElement }[] = [];
async function open(query = '') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/quant/price-spikes', component: PriceWorkspace },
      { path: '/quant/orderflow', component: { template: '<div />' } },
    ],
  });
  await router.push('/quant/price-spikes' + query);
  await router.isReady();
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({ render: () => h(PriceWorkspace) });
  app.use(router).mount(element);
  mounted.push({ app, element });
  await vi.waitFor(() => expect(element.textContent).toContain('exact-event'));
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
beforeEach(() => {
  api.fetch
    .mockReset()
    .mockImplementation(async (query: { pair: string; direction: string }) => {
      const data = fixture();
      data.direction = query.direction;
      data.items = data.items.map((item) => ({ ...item, pair: query.pair }));
      data.stats.by_pair = { [query.pair]: data.stats.by_pair[pair]! };
      data.stats.profile_by_pair = {
        [query.pair]: data.stats.profile_by_pair[pair]!,
      };
      return data;
    });
});
afterEach(() => {
  mounted.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  });
});

describe('price workspace real evidence interaction', () => {
  it('uses URL defaults when a reused route removes its previous pair', async () => {
    const view = await open('?pair=ETH%2FUSDT%3AUSDT');
    expect(
      view.get<HTMLSelectElement>('select[aria-label=价格研究交易对]').value,
    ).toBe('ETH/USDT:USDT');
    await view.router.push('/quant/price-spikes');
    await vi.waitFor(() =>
      expect(
        view.get('[data-testid=price-snapshot-scope]').textContent,
      ).toContain('BTC/USDT:USDT'),
    );
    expect(
      view.get<HTMLSelectElement>('select[aria-label=价格研究交易对]').value,
    ).toBe('BTC/USDT:USDT');
  });
  it('does not request or silently reuse facts for unsupported or repeated URL filters', async () => {
    const view = await open();
    for (const query of [
      '?pair=TRX',
      '?pair=',
      '?pair=BTC%2FUSDT%3AUSDT&pair=ETH%2FUSDT%3AUSDT',
      '?direction=sideways',
      '?event_id=a&event_id=b',
    ]) {
      const before = api.fetch.mock.calls.length;
      await view.router.push('/quant/price-spikes' + query);
      await nextTick();
      expect(view.element.querySelector('[role=alert]')?.textContent).toContain(
        '查询参数无效',
      );
      expect(api.fetch.mock.calls.length).toBe(before);
      expect(
        view.get('[data-testid=price-snapshot-scope]').textContent,
      ).toContain('旧快照');
    }
  });
  it('opens exact event identity and does not relabel an absent threshold as downward', async () => {
    const view = await open('?event_id=none-event');
    expect(view.get('[data-testid=price-inspector]').textContent).toContain(
      'none-event',
    );
    expect(view.get('[data-testid=price-inspector]').textContent).toContain(
      '未达到阈值',
    );
    view.get('button[data-event=exact-event]').click();
    await nextTick();
    await vi.waitFor(() =>
      expect(view.router.currentRoute.value.query.event_id).toBe('exact-event'),
    );
    expect(view.get('[data-testid=price-inspector]').textContent).toContain(
      '待成熟',
    );
  });
  it('reports missing history identity instead of selecting the newest event', async () => {
    const view = await open('?event_id=missing');
    expect(view.get('[data-testid=price-inspector]').textContent).toContain(
      '未找到事件',
    );
    expect(view.get('[data-testid=price-inspector]').textContent).not.toContain(
      'exact-event',
    );
    expect(view.element.textContent).toContain('3 根后续');
    expect(view.element.textContent).toContain('1/5/15/30/60');
    expect(view.element.textContent).toContain('成本与成交能力尚未核验');
  });
  it('keeps failed refresh facts labelled with original pair and does not claim new scope success', async () => {
    const view = await open();
    api.fetch.mockRejectedValueOnce(new Error('offline'));
    const select = view.get<HTMLSelectElement>(
      'select[aria-label=价格研究交易对]',
    );
    select.value = 'ETH/USDT:USDT';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    await vi.waitFor(() =>
      expect(view.element.querySelector('[role=alert]')?.textContent).toContain(
        '读取失败',
      ),
    );
    expect(
      view.get('[data-testid=price-snapshot-scope]').textContent,
    ).toContain('BTC/USDT:USDT');
    expect(
      view.get('[data-testid=price-snapshot-scope]').textContent,
    ).toContain('旧快照');
    expect(view.get('[data-testid=price-events]').textContent).toContain(
      'exact-event',
    );
  });
});

import type { App } from 'vue';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';
import SurgeWorkspace from './SurgeWorkspace.vue';

const mounted: { app: App; element: HTMLDivElement }[] = [];
const row = (id = 'event-a', pair = 'BTC/USDT:USDT') => ({
  event_id: id,
  pair,
  timeframe: '5m',
  timestamp: 300000,
  status: 'unknown',
  method_version: 'frozen-method',
  data_fingerprint: 'fingerprint-a',
  observation: {
    volume_ratio: 4,
    quote_volume: 800,
    baseline_quote_volume: 200,
    num_trades: 5,
    flow_imbal: 0,
  },
});
const stats = () => ({
  by_pair: [
    {
      pair: 'BTC/USDT:USDT',
      total: 8,
      unknown: 8,
      matured: 0,
      statistics_horizon: '1h',
      gross_return_mean: null,
      horizons: { '5m': { sample_count: 1, gross_return_mean: 0.01 } },
    },
  ],
  warmup: {
    'BTC/USDT:USDT': {
      valid_bars: 280,
      required_bars: 288,
      ready: false,
      last_timestamp: 300000,
    },
  },
});
async function open(
  props: {
    loadList: (query: any, signal: AbortSignal) => Promise<unknown>;
    loadStats: (signal: AbortSignal) => Promise<unknown>;
    loadDetail: (id: string, signal: AbortSignal) => Promise<unknown>;
  },
  path = '/quant/orderflow',
) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:pathMatch(.*)*', component: { template: '<div />' } }],
  });
  await router.push(path);
  await router.isReady();
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({ render: () => h(SurgeWorkspace, props) });
  app.use(router).mount(element);
  mounted.push({ app, element });
  const get = <T extends HTMLElement = HTMLElement>(selector: string) => {
    const item = element.querySelector<T>(selector);
    if (!item) throw new Error('Missing ' + selector);
    return item;
  };
  return { app, element, router, get };
}
afterEach(() =>
  mounted.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  }),
);

describe('订单流事件账本', () => {
  it('保留的旧事件打开其原筛选身份，不生成当前币种的错误深链', async () => {
    const view = await open(
      {
        loadList: async (query) => {
          if (query.pair === 'ETH/USDT:USDT') throw new Error('offline');
          return { items: [row()] };
        },
        loadStats: async () => stats(),
        loadDetail: async (id) => ({ ...row(id), revisions: [] }),
      },
      '/quant/orderflow?pair=BTC%2FUSDT%3AUSDT',
    );
    await vi.waitFor(() =>
      expect(view.element.textContent).toContain('event-a'),
    );
    await view.router.push('/quant/orderflow?pair=ETH%2FUSDT%3AUSDT');
    await vi.waitFor(() =>
      expect(
        view.get('[data-testid="surge-read-status"]').textContent,
      ).toContain('保留'),
    );
    view.get('button[data-event="event-a"]').click();
    await vi.waitFor(() =>
      expect(view.router.currentRoute.value.query.pair).toBe('BTC/USDT:USDT'),
    );
    expect(view.router.currentRoute.value.query.event_id).toBe('event-a');
  });
  it('不支持或重复的筛选不默认读取另一范围，跨币种详情也拒绝', async () => {
    const view = await open(
      {
        loadList: async (query) => ({
          items: [row('scope-event', query.pair ?? 'BTC/USDT:USDT')],
        }),
        loadStats: async () => stats(),
        loadDetail: async (id) => ({ ...row(id), revisions: [] }),
      },
      '/quant/orderflow?pair=TRX%2FUSDT%3AUSDT&event_id=scope-event',
    );
    await vi.waitFor(() =>
      expect(view.element.textContent).toContain('筛选范围无效'),
    );
    expect(
      view.get('[data-testid="surge-inspector"]').textContent,
    ).not.toContain('frozen-method');
    await view.router.push(
      '/quant/orderflow?pair=ETH%2FUSDT%3AUSDT&event_id=scope-event',
    );
    await vi.waitFor(() =>
      expect(view.element.textContent).toContain('指定事件未取得'),
    );
    expect(
      view.get('[data-testid="surge-inspector"]').textContent,
    ).not.toContain('frozen-method');
  });
  it('同页路由筛选和界面筛选保持同一范围', async () => {
    const view = await open(
      {
        loadList: async (query) => ({
          items: [row('scope-event', query.pair ?? 'BTC/USDT:USDT')],
        }),
        loadStats: async () => stats(),
        loadDetail: async (id) => ({ ...row(id), revisions: [] }),
      },
      '/quant/orderflow?pair=BTC%2FUSDT%3AUSDT',
    );
    await vi.waitFor(() =>
      expect(view.element.textContent).toContain('scope-event'),
    );
    await view.router.push(
      '/quant/orderflow?pair=ETH%2FUSDT%3AUSDT&status=unknown',
    );
    await vi.waitFor(() =>
      expect(
        view.get<HTMLSelectElement>('select[aria-label="观测交易对"]').value,
      ).toBe('ETH/USDT:USDT'),
    );
    const pair = view.get<HTMLSelectElement>('select[aria-label="观测交易对"]');
    pair.value = 'BTC/USDT:USDT';
    pair.dispatchEvent(new Event('change', { bubbles: true }));
    await vi.waitFor(() =>
      expect(view.router.currentRoute.value.query.pair).toBe('BTC/USDT:USDT'),
    );
    expect(view.router.currentRoute.value.query.status).toBe('unknown');
    await view.router.push('/quant/orderflow');
    await vi.waitFor(() =>
      expect(
        view.get<HTMLSelectElement>('select[aria-label="观测交易对"]').value,
      ).toBe('all'),
    );
    expect(
      view.get<HTMLSelectElement>('select[aria-label="成熟状态"]').value,
    ).toBe('all');
  });
  it('筛选失败保留旧统计和来源身份', async () => {
    let n = 0;
    const view = await open({
      loadList: async (query) => {
        if (query.pair === 'ETH/USDT:USDT') throw new Error('offline');
        return { items: [row()] };
      },
      loadStats: async () => {
        n++;
        if (n > 1) throw new Error('offline');
        return stats();
      },
      loadDetail: async (id) => ({ ...row(id), revisions: [] }),
    });
    await vi.waitFor(() =>
      expect(view.element.textContent).toContain('event-a'),
    );
    expect(view.element.textContent).toContain('280 / 288');
    const pair = view.get<HTMLSelectElement>('select[aria-label="观测交易对"]');
    pair.value = 'ETH/USDT:USDT';
    pair.dispatchEvent(new Event('change', { bubbles: true }));
    await vi.waitFor(() =>
      expect(
        view.get('[data-testid="surge-read-status"]').textContent,
      ).toContain('保留'),
    );
    expect(view.element.textContent).toContain('event-a');
    expect(view.element.textContent).toContain('旧筛选');
    expect(view.element.textContent).toContain('BTC/USDT:USDT');
    expect(view.element.textContent).toContain('280 / 288');
    expect(view.element.textContent).toContain('历史统计');
  });
  it('不存在的事件深链不显示别的事件，选中后提供全部修订', async () => {
    const view = await open(
      {
        loadList: async () => ({ items: [row()] }),
        loadStats: async () => stats(),
        loadDetail: async (id) => {
          if (id === 'missing') throw new Error('notfound');
          return {
            ...row(id),
            revisions: [
              {
                event_id: id,
                revision_no: 2,
                event_type: 'maturity',
                result: { horizon: '5m', status: 'matured', return: 0.01 },
              },
            ],
          };
        },
      },
      '/quant/orderflow?event_id=missing',
    );
    await vi.waitFor(() =>
      expect(view.element.textContent).toContain('指定事件未取得'),
    );
    expect(
      view.get('[data-testid="surge-inspector"]').textContent,
    ).not.toContain('frozen-method');
    view.get('button[data-event="event-a"]').click();
    await vi.waitFor(() =>
      expect(view.get('[data-testid="surge-inspector"]').textContent).toContain(
        'frozen-method',
      ),
    );
    expect(view.router.currentRoute.value.query.event_id).toBe('event-a');
    expect(view.get('[data-testid="surge-inspector"]').textContent).toContain(
      'fingerprint-a',
    );
    const raw = view.get<HTMLDetailsElement>(
      'details[data-testid="surge-raw"]',
    );
    raw.open = true;
    await nextTick();
    expect(raw.textContent).toContain('revision_no');
    expect(raw.textContent).toContain('maturity');
  });
  it('迟到旧详情和无效窗口不能制造当前事件收益', async () => {
    let resolve!: (v: unknown) => void;
    const late = new Promise((a) => {
      resolve = a;
    });
    const view = await open({
      loadList: async () => ({ items: [row('event-a'), row('event-b')] }),
      loadStats: async () => stats(),
      loadDetail: async (id) =>
        id === 'event-a'
          ? late
          : {
              ...row(id),
              revisions: [
                {
                  event_id: id,
                  revision_no: 2,
                  result: { horizon: '5m', status: 'invalid', return: 999 },
                },
              ],
            },
    });
    await vi.waitFor(() =>
      expect(view.element.textContent).toContain('event-b'),
    );
    view.get('button[data-event="event-a"]').click();
    await nextTick();
    await view.router.push('/quant/orderflow?event_id=event-b');
    await vi.waitFor(() =>
      expect(view.get('[data-testid="surge-inspector"]').textContent).toContain(
        'event-b',
      ),
    );
    resolve({ ...row('event-a'), revisions: [] });
    await nextTick();
    await nextTick();
    expect(view.get('[data-testid="surge-inspector"]').textContent).toContain(
      'event-b',
    );
    expect(
      view.get('[data-testid="surge-inspector"]').textContent,
    ).not.toContain('99900');
    expect(view.element.textContent).toContain('1 / 5 / 15 / 30 / 60');
  });
});

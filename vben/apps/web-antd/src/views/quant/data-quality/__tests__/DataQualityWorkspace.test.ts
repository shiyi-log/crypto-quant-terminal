import type { App } from 'vue';

import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick } from 'vue';
import {
  createMemoryHistory,
  createRouter,
  createWebHashHistory,
} from 'vue-router';

import DataQualityWorkspace from '../DataQualityWorkspace.vue';

const mounted: { app: App; element: HTMLDivElement }[] = [];
function response() {
  return {
    engine: 'postgresql',
    database: { name: 'quant_terminal', schema_version: 'v2' },
    counts: { candles: 105122, ticks: 0 },
    datasets: [
      {
        exchange: 'binance',
        market: 'futures',
        pair: 'BTC/USDT:USDT',
        timeframe: '5m',
        candle_type: 'futures',
        rows: 105121,
        first_timestamp: '2025-01-01T00:00:00Z',
        last_timestamp: '2026-01-01T00:00:00Z',
      },
      {
        exchange: 'binance',
        market: 'futures',
        pair: 'ETH/USDT:USDT',
        timeframe: '1m',
        candle_type: 'futures',
        rows: 1,
        first_timestamp: null,
        last_timestamp: null,
      },
    ],
    sources: [
      {
        key: 'source-1',
        kind: 'feather',
        row_count: 105121,
        updated_at: '2026-01-01T00:01:00Z',
        error: null,
      },
    ],
    sync: { running: true, errors: 0 },
    collector: { state: 'running' },
  };
}
async function flush() {
  await Promise.resolve();
  await Promise.resolve();
  await nextTick();
}
async function open(
  loadInventory: (signal: AbortSignal) => Promise<unknown>,
  hash = false,
) {
  const router = createRouter({
    history: hash ? createWebHashHistory('/') : createMemoryHistory(),
    routes: ['data-quality', 'operations'].map((path) => ({
      path: '/quant/' + path,
      component: { template: '<div />' },
    })),
  });
  await router.push('/quant/data-quality');
  await router.isReady();
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({
    render: () => h(DataQualityWorkspace, { loadInventory }),
  });
  app.use(router).mount(element);
  mounted.push({ app, element });
  await flush();
  return {
    element,
    router,
    get<T extends HTMLElement = HTMLElement>(selector: string): T {
      const item = element.querySelector<T>(selector);
      if (!item) throw new Error('Expected element: ' + selector);
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

describe('数据质量工作区', () => {
  it('从采集来源进入新运维工作区，保留生产 hash 路由', async () => {
    const { element, get, router } = await open(async () => response(), true);
    expect(
      [...element.querySelectorAll('a')].map((link) =>
        link.getAttribute('href'),
      ),
    ).toEqual(['#/quant/operations']);
    const link = get<HTMLAnchorElement>('a[href$="#/quant/operations"]');
    link.click();
    await flush();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.path).toBe('/quant/operations'),
    );
  });
  it('呈现清单与证据限制，不把一年端点或运行状态提升为研究通过', async () => {
    const { element, get } = await open(async () => response());
    expect(element.textContent).toContain('BTC/USDT:USDT');
    expect(get('[data-quality-gates]').textContent).toContain('未取得证据');
    expect(get('[data-quality-gates]').textContent).toContain(
      '365 天逐频率覆盖',
    );
    expect(get('[data-quality-gates]').textContent).toContain('闭盘与可用时间');
    expect(element.textContent).toContain('首末时间与记录数无法证明连续覆盖');
    expect(get('[data-dataset-inspector]').textContent).toContain('105,121');
    expect(element.querySelectorAll('button')).toHaveLength(3);
    expect(element.querySelector('a[href="/quant/operations"]')).not.toBeNull();
  });

  it('精确筛选交易对和频率，并把所选事实交给明细检查器', async () => {
    const { element, get } = await open(async () => response());
    const pair = get<HTMLSelectElement>('select[name=pair]');
    pair.value = 'ETH/USDT:USDT';
    pair.dispatchEvent(new Event('change', { bubbles: true }));
    await flush();
    expect(get('[data-inventory-table] tbody').textContent).toContain(
      'ETH/USDT:USDT',
    );
    expect(get('[data-inventory-table] tbody').textContent).not.toContain(
      'BTC/USDT:USDT',
    );
    expect(get('[data-dataset-inspector]').textContent).toContain(
      'ETH/USDT:USDT',
    );
    expect(get('[data-dataset-inspector]').textContent).toContain('未提供');
    const timeframe = get<HTMLSelectElement>('select[name=timeframe]');
    timeframe.value = '5m';
    timeframe.dispatchEvent(new Event('change', { bubbles: true }));
    await flush();
    expect(element.textContent).toContain('当前筛选没有清单记录');
    expect(element.textContent).not.toContain('数据源尚无清单记录');
  });

  it('失败刷新保留已知清单并展示旧快照，安全错误不会渲染内部地址', async () => {
    let fail = false;
    const { element, get } = await open(async () => {
      if (fail) throw new Error('postgresql://secret');
      return response();
    });
    fail = true;
    get('[data-refresh]').click();
    await flush();
    expect(element.textContent).toContain('BTC/USDT:USDT');
    expect(element.textContent).toContain('保留上次成功快照');
    expect(element.textContent).toContain('读取失败');
    expect(element.textContent).not.toContain('postgresql://secret');
  });

  it('初次合同损坏显示无法判定，缺失字段不能当作真实空结果', async () => {
    const { element } = await open(async () => ({ engine: 'postgresql' }));
    expect(element.textContent).toContain('数据清单合同不完整');
    expect(element.textContent).toContain('清单尚未取得');
    expect(element.textContent).not.toContain('数据源尚无清单记录');
  });

  it('真实空数组和未知计数保持不同，页面只在进入与刷新时读取', async () => {
    let calls = 0;
    const { element, get } = await open(async () => {
      calls += 1;
      return { engine: 'postgresql', datasets: [], sources: [] };
    });
    expect(element.textContent).toContain('数据源尚无清单记录');
    expect(get('[data-count=candles]').textContent).toContain('未提供');
    expect(calls).toBe(1);
    get('[data-refresh]').click();
    await flush();
    expect(calls).toBe(2);
    expect(
      [...element.querySelectorAll('button')].some((button) =>
        /下载|同步|删除|启动|升级/.test(button.textContent ?? ''),
      ),
    ).toBe(false);
  });
});

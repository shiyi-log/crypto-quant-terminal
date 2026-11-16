import type { App } from 'vue';
import { afterEach, expect, it, vi } from 'vitest';
import { createApp, h, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';
import OverviewWorkspace from '../OverviewWorkspace.vue';
import { createOverviewState } from '../overviewState';
const mounts: { app: App; el: HTMLDivElement }[] = [];
afterEach(() =>
  mounts.splice(0).forEach(({ app, el }) => {
    app.unmount();
    el.remove();
  }),
);
function view() {
  const requests = {
    config: vi.fn(async () => ({
      dry_run: true,
      strategy: 'LegacyTrend',
      state: 'running',
    })),
    profit: vi.fn(async () => ({
      profit_all_coin: 2,
      closed_trade_count: 0,
      winrate: 0,
      winning_trades: 0,
      losing_trades: 0,
    })),
    balance: vi.fn(async () => ({ total: 1000 })),
    positions: vi.fn(async () => [
      {
        trade_id: 7,
        pair: 'ETH/USDT:USDT',
        profit_abs: 2,
        profit_ratio: 0.02,
        is_open: true,
      },
    ]),
    trades: vi.fn(async () => ({ trades: [], total_trades: 0 })),
    ops: vi.fn(async () => ({ factors: [] })),
    iteration: vi.fn(async () => ({ live: {} })),
    progress: vi.fn(async () => null),
    researchProgress: vi.fn(async () => null),
    jobs: vi.fn(async () => []),
    services: vi.fn(async () => ({ services: [] })),
    whitelist: vi.fn(async () => ({ whitelist: [] })),
  };
  const state = createOverviewState(requests);
  const el = document.createElement('div');
  document.body.append(el);
  const app = createApp({ render: () => h(OverviewWorkspace, { state }) });
  app.use(
    createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { render: () => h('div') } },
        { path: '/quant/:page', component: { render: () => h('div') } },
        { path: '/quant/:page/:detail', component: { render: () => h('div') } },
      ],
    }),
  );
  app.mount(el);
  mounts.push({ app, el });
  return { el, state, requests };
}
it('reads actual legacy facts, links exact trade ID and renders refresh errors without upstream diagnostics or invented zero', async () => {
  const { el, state, requests } = view();
  expect(el.textContent).toContain('运行环境未知');
  await state.refresh();
  await nextTick();
  expect(el.querySelector('h1')?.textContent).toBe('旧引擎总览');
  expect(el.textContent).toContain('模拟盘 dry-run');
  expect(el.textContent).toContain('1,000.00');
  expect(el.textContent).toContain('ETH/USDT:USDT');
  expect(el.textContent).toContain('已平仓胜率');
  expect(el.textContent).toContain('已平仓样本');
  expect(el.textContent).toContain('胜 0 / 负 0');
  expect(el.textContent).toContain('已平仓胜率');
  el.querySelector<HTMLButtonElement>('button[data-trade="7"]')!.click();
  await nextTick();
  expect(
    el.querySelector('a[data-trade-detail]')?.getAttribute('href'),
  ).toContain('trade_id=7');
  requests.balance.mockRejectedValueOnce(new Error('postgres://secret'));
  await state.refresh();
  await nextTick();
  expect(el.textContent).toContain('1,000.00');
  expect(el.querySelector('[role=alert]')?.textContent).toContain(
    '上次成功快照',
  );
  expect(el.textContent).not.toContain('secret');
  expect(el.textContent).toContain('金额未知');
});
it('keeps history, factors, observed tasks and process facts reachable from native buttons without issuing write commands', async () => {
  const { el, state, requests } = view();
  await state.refresh();
  await nextTick();
  el.querySelector<HTMLButtonElement>('button[data-ledger=recent]')!.click();
  await nextTick();
  expect(el.textContent).toContain('最近已平仓');
  el.querySelector<HTMLButtonElement>('button[data-monitor=tasks]')!.click();
  await nextTick();
  expect(el.textContent).toContain('没有可读取的任务快照');
  expect(
    Object.values(requests).every((request) => request.mock.calls.length === 1),
  ).toBe(true);
});

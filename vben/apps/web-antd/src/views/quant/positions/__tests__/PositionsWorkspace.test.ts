import type { App } from 'vue';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick } from 'vue';
import { createMemoryHistory, createRouter, RouterView } from 'vue-router';
import PositionsWorkspace from '../PositionsWorkspace.vue';
import { createPositionsState } from '../positionsState';
vi.mock('../../utils/useFtWs', () => ({
  useFtWs: () => ({ connected: { value: false } }),
  onFtWsMessage: () => () => {},
}));
vi.mock('../TradeDetailDrawer.vue', () => ({
  default: { template: '<div />' },
}));
const mounted: { app: App; element: HTMLDivElement }[] = [];
afterEach(() =>
  mounted.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  }),
);
const btc = {
  trade_id: 7,
  pair: 'BTC/USDT:USDT',
  is_short: false,
  is_open: true,
  profit_abs: 12.5,
  amount: 2,
};
const eth = {
  trade_id: 8,
  pair: 'ETH/USDT:USDT',
  is_short: true,
  is_open: false,
  profit_abs: -3,
  orders: [],
};
async function open(path = '/positions') {
  const api = {
    config: async () => ({ dry_run: true, strategy: 'Trend' }),
    positions: async () => [btc],
    history: async () => ({ trades: [eth], total_trades: 1 }),
    balance: async () => ({
      currencies: [{ currency: 'USDT', free: 900, used: 100, balance: 1000 }],
      total: 1000,
      stake: 'USDT',
    }),
    detail: vi.fn(async (id: string) => ({
      ...(id === '7' ? btc : eth),
      orders: [],
    })),
    custom: async () => ({ custom_data: { rank: 1 } }),
    exit: vi.fn(async () => ({ result: 'Created exit order' })),
    cancel: async () => ({ status: 'success' }),
    reload: async () => ({ ...btc, orders: [] }),
  };
  const state = createPositionsState(api);
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/positions',
        component: { render: () => h(PositionsWorkspace, { state }) },
      },
    ],
  });
  await router.push(path);
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({ render: () => h(RouterView) });
  app.use(router).mount(element);
  mounted.push({ app, element });
  await state.refresh();
  await nextTick();
  await Promise.resolve();
  await nextTick();
  return {
    state,
    router,
    element,
    api,
    get(selector: string) {
      const value = element.querySelector<HTMLElement>(selector);
      if (!value) throw new Error(selector);
      return value;
    },
  };
}
describe('旧引擎持仓账本工作区', () => {
  it('renders environment, original trade identity and the retired deletion explanation without a delete action', async () => {
    const view = await open();
    expect(view.element.textContent).toContain('旧 Freqtrade 交易引擎');
    expect(view.element.textContent).toContain('模拟 dry-run');
    expect(view.element.textContent).toContain('BTC/USDT:USDT');
    expect(view.element.textContent).toContain('删除交易记录不等同于平仓');
    expect(
      [...view.element.querySelectorAll('button')].some((button) =>
        button.textContent?.includes('删除'),
      ),
    ).toBe(false);
    view.get('[data-trade="7"]').click();
    await vi.waitFor(() => expect(view.state.data.selectedId).toBe('7'));
    expect(view.router.currentRoute.value.query.trade_id).toBe('7');
    expect(view.get('[aria-label="所选交易检查区"]').textContent).toContain(
      '原始交易 ID',
    );
  });
  it('restores focus after cancelling confirmation and does not submit a command before explicit confirmation', async () => {
    const view = await open('/positions?trade_id=7');
    await vi.waitFor(() => expect(view.state.data.detail).not.toBeNull());
    await nextTick();
    const trigger = view.get('[data-command="market"]');
    trigger.focus();
    trigger.click();
    await nextTick();
    expect(view.api.exit).not.toHaveBeenCalled();
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]');
    expect(dialog?.textContent).toContain('回执不代表交易所已执行');
    const cancel = dialog?.querySelector<HTMLElement>('button');
    cancel?.click();
    await nextTick();
    expect(view.element.querySelector('[role="alertdialog"]')).toBeNull();
    expect(view.api.exit).not.toHaveBeenCalled();
  });
  it('query changes clear selection and never select the newest row for duplicate or missing ids', async () => {
    const view = await open('/positions?trade_id=8');
    await vi.waitFor(() => expect(view.state.data.detail?.trade_id).toBe(8));
    expect(view.get('[aria-label="所选交易检查区"]').textContent).toContain(
      'ETH/USDT:USDT',
    );
    await view.router.push({
      path: '/positions',
      query: { trade_id: ['7', '8'] },
    });
    await nextTick();
    await vi.waitFor(() => expect(view.state.data.selectedId).toBeNull());
    expect(view.element.textContent).toContain('重复参数无效');
    await view.router.push('/positions');
    await nextTick();
    expect(view.state.data.selectedId).toBeNull();
  });
  it('keeps the known ledger visible with a stale warning when the next read fails', async () => {
    const view = await open();
    view.state.data.errors.positions = '当前持仓读取失败，保留上次成功快照。';
    await nextTick();
    expect(view.element.textContent).toContain('BTC/USDT:USDT');
    expect(view.element.querySelector('[role="alert"]')?.textContent).toContain(
      '保留上次成功快照',
    );
  });
  it('disables every old-engine command when config facts are stale or unknown', async () => {
    const view = await open('/positions?trade_id=7');
    await vi.waitFor(() => expect(view.state.data.detail).not.toBeNull());
    view.state.data.errors.config = '引擎环境读取失败。';
    await nextTick();
    expect(view.element.textContent).toContain('环境读取失败');
    for (const button of view.element.querySelectorAll<HTMLButtonElement>(
      '[data-command]',
    )) {
      expect(button.disabled).toBe(true);
    }
    expect(view.element.textContent).toContain('BTC/USDT:USDT');
  });
});

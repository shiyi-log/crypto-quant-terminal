import type { App } from 'vue';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

const api = vi.hoisted(() => ({ get: vi.fn(), requireSession: vi.fn() }));
vi.mock('#/api/freqtrade/client', () => ({
  ftClient: { get: api.get },
  unwrap: (value: unknown) => value,
  errText: () => 'upstream raw secret',
}));
vi.mock('#/api/session', () => ({ requireSession: api.requireSession }));
vi.mock('@vben/plugins/echarts', () => ({
  EchartsUI: { template: '<div />' },
  useEcharts: () => ({ renderEcharts: async () => undefined }),
}));

import PaperWorkspace from '../index.vue';
const first = 's03_tsmom_vol_target_B15m_R50_k1.0_100x_maker';
const second = 's05_bollinger_fade_B15m_R50_k1.0_100x_maker';
function account(strategy_id: string, label: string) {
  return {
    strategy_id,
    label,
    initial_capital: '10000',
    cash: '10000',
    equity: '10000',
    realized_pnl: '0',
    unrealized_pnl: '0',
    fees_paid: '0',
    open_position_count: 0,
    closed_trade_count: 0,
    open_positions: [],
    definition: {
      name: label,
      family: 'momentum',
      timeframe: '15m',
      mechanism: label + '定义',
      factorial_design_version: 'v1',
      factorial_factors: { risk_budget: 'R50', stop_width_k: 1 },
      params_frozen: { risk_stop_atr_mult: 1 },
    },
    rule_hash: 'a'.repeat(64),
    execution_hash: 'b'.repeat(64),
  };
}
const accounts = [
  account(first, '动量纸面账户'),
  account(second, '回归纸面账户'),
];
function details(strategyId: string) {
  return {
    account: accounts.find((item) => item.strategy_id === strategyId),
    orders: [],
    trades: [],
    equity_curve: [],
    events: [
      {
        strategy_id: strategyId,
        event_id: 'event-' + strategyId,
        event_type: 'account_created',
        observed_at_ms: 1794800000000,
      },
    ],
  };
}
const mounted: { app: App; element: HTMLDivElement }[] = [];
function get(element: Element, selector: string) {
  const target = element.querySelector<HTMLElement>(selector);
  if (!target) throw Error('Missing rendered element ' + selector);
  return target;
}
async function open() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/quant/strategy-lab',
        name: 'QuantStrategyLab',
        component: { template: '<div />' },
      },
    ],
  });
  await router.push('/quant/strategy-lab');
  await router.isReady();
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp(PaperWorkspace);
  app.use(router).mount(element);
  mounted.push({ app, element });
  return { element, router };
}
beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockImplementation(async (_url, options) =>
    options?.params?.strategy_id
      ? details(options.params.strategy_id)
      : { accounts, service: { running: true } },
  );
});
afterEach(() =>
  mounted.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  }),
);

describe('纸面策略档案工作区', () => {
  it('风险档筛选切换保留独立账户，成功空筛选不是读取失败', async () => {
    const higher = {
      ...account(
        's03_tsmom_vol_target_B15m_R200_k1.0_100x_maker',
        'R200独立账户',
      ),
      definition: {
        factorial_design_version: 'v1',
        factorial_factors: { risk_budget: 'R200', stop_width_k: 1 },
      },
    };
    api.get.mockImplementation(async (_url, options) =>
      options?.params?.strategy_id
        ? {
            ...details(options.params.strategy_id),
            account: [...accounts, higher].find(
              (item) => item.strategy_id === options.params.strategy_id,
            ),
          }
        : { accounts: [...accounts, higher] },
    );
    const { element } = await open();
    await vi.waitFor(() =>
      expect(element.textContent).toContain('R200独立账户'),
    );
    const select = get(
      element,
      'select[aria-label="计划风险预算"]',
    ) as HTMLSelectElement;
    select.value = 'R200';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();
    expect(get(element, '[data-account-ledger]').textContent).toContain(
      'R200独立账户',
    );
    expect(get(element, '[data-account-ledger]').textContent).not.toContain(
      '动量纸面账户',
    );
    select.value = 'R1000';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();
    expect(element.textContent).toContain('当前接口返回的筛选中没有账户');
    expect(element.textContent).not.toContain('读取失败');
  });
  it('当前账户明细刷新损坏时保留已读取事件和原身份', async () => {
    const { element } = await open();
    await vi.waitFor(() => expect(element.textContent).toContain(first));
    get(element, '[data-inspector-tab="archive"]').click();
    await nextTick();
    const decisionTab = [
      ...element.querySelectorAll<HTMLElement>('.ant-tabs-tab'),
    ].find((tab) => tab.textContent?.includes('决策记录'))!;
    decisionTab.click();
    await vi.waitFor(() =>
      expect(element.textContent).toContain('独立模拟账户初始化'),
    );
    api.get.mockImplementation(async (_url, options) =>
      options?.params?.strategy_id ? {} : { accounts },
    );
    get(element, '[data-refresh-paper]').click();
    await vi.waitFor(() =>
      expect(element.textContent).toContain('明细读取失败，保留上次成功事实'),
    );
    expect(element.textContent).toContain('独立模拟账户初始化');
    expect(get(element, '[data-selected-paper]').textContent).toContain(first);
  });
  it('所选盘中止损和分层强平规则不被旧收盘或全损说明覆盖', async () => {
    const selected = {
      ...accounts[0],
      execution: {
        leverage: 10,
        risk_stop_atr_mult: 2,
        max_loss_per_trade_usdt: 50,
        stop_execution_mode: 'intrabar',
        liquidation_model: 'binance_tiered',
        tier_snapshot_hash: 'c'.repeat(64),
        fill_model: 'next_closed_bar_open_proxy',
      },
    };
    api.get.mockImplementation(async (_url, options) =>
      options?.params?.strategy_id
        ? { ...details(first), account: selected }
        : { accounts: [selected] },
    );
    const { element } = await open();
    await vi.waitFor(() =>
      expect(element.textContent).toContain('分层维持保证金'),
    );
    expect(element.textContent).toContain('盘中高低价');
    expect(element.textContent).toContain('止损价或跳空开盘价代理');
    expect(element.textContent).toContain('next_closed_bar_open_proxy');
    expect(element.textContent).not.toContain('保证金按规则全额损失');
    expect(element.textContent).not.toContain('止损由收盘价确认');
    expect(element.textContent).not.toContain('触发只认收盘价');
  });
  it('账户台账与证据分区不把健康或收益表示为执行资格', async () => {
    const { element } = await open();
    await vi.waitFor(() =>
      expect(element.textContent).toContain('动量纸面账户'),
    );
    expect(element.textContent).toContain('纸面策略档案');
    expect(get(element, '[data-account-ledger]').textContent).toContain(
      '动量纸面账户',
    );
    for (const tab of ['definition', 'time', 'cost', 'execution', 'archive'])
      expect(
        element.querySelector('[data-inspector-tab="' + tab + '"]'),
      ).not.toBeNull();
    expect(element.textContent).toContain('账户资格未核验');
    expect(element.textContent).toContain('滑点与资金费未知');
  });
  it('损坏概览保留已知账户且不会显示成功空结果或原始错误', async () => {
    const { element } = await open();
    await vi.waitFor(() =>
      expect(element.textContent).toContain('动量纸面账户'),
    );
    api.get.mockResolvedValue({});
    get(element, '[data-refresh-paper]').click();
    await vi.waitFor(() =>
      expect(element.textContent).toContain('概览读取失败，保留上次成功事实'),
    );
    expect(element.textContent).toContain('动量纸面账户');
    expect(element.textContent).not.toContain('upstream raw secret');
  });
  it('切换账户后迟到旧明细不能覆盖新定义', async () => {
    let resolveOld!: (value: unknown) => void;
    api.get.mockImplementation(async (_url, options) => {
      const id = options?.params?.strategy_id;
      if (id === first)
        return new Promise((resolve) => {
          resolveOld = resolve;
        });
      return id ? details(id) : { accounts };
    });
    const { element } = await open();
    await vi.waitFor(() =>
      expect(element.textContent).toContain('动量纸面账户'),
    );
    get(element, '[data-account-id="' + second + '"]').click();
    await vi.waitFor(() =>
      expect(get(element, '[data-selected-paper]').textContent).toContain(
        second,
      ),
    );
    resolveOld({
      ...details(first),
      account: { ...accounts[0], label: '迟到旧账户' },
    });
    await nextTick();
    expect(get(element, '[data-selected-paper]').textContent).not.toContain(
      '迟到旧账户',
    );
    expect(get(element, '[data-selected-paper]').textContent).toContain(second);
  });
  it('唯一策略档案入口保留全部账户，选择非重点账户后仍可查看原身份', async () => {
    const other = account(
      's11_tsmom_bollinger_pullback_B15m_R50_k1.0_100x_maker',
      '其他账户',
    );
    api.get.mockImplementation(async (_url, options) =>
      options?.params?.strategy_id
        ? {
            ...details(options.params.strategy_id),
            account: [...accounts, other].find(
              (item) => item.strategy_id === options.params.strategy_id,
            ),
          }
        : { accounts: [...accounts, other] },
    );
    const { element, router } = await open();
    await vi.waitFor(() =>
      expect(element.textContent).toContain('动量纸面账户'),
    );
    expect(get(element, '[data-account-ledger]').textContent).toContain(
      '其他账户',
    );
    get(element, '[data-account-id="' + other.strategy_id + '"]').click();
    await vi.waitFor(() =>
      expect(get(element, '[data-selected-paper]').textContent).toContain(
        other.strategy_id,
      ),
    );
    expect(router.currentRoute.value.fullPath).toBe('/quant/strategy-lab');
    expect(get(element, '#paper-title').textContent).toContain('纸面策略档案');
    expect(get(element, '#paper-title').textContent).not.toContain('重点');
  });
});

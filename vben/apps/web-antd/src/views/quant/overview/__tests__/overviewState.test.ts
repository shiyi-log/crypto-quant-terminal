import { describe, expect, it, vi } from 'vitest';
import {
  createOverviewState,
  environmentLabel,
  finiteNumber,
  sumKnown,
} from '../overviewState';

function fixture() {
  return {
    config: vi.fn(async () => ({
      dry_run: true,
      state: 'running',
      strategy: 'Trend',
    })),
    profit: vi.fn(async () => ({ profit_all_coin: 5, winrate: 0.5 })),
    balance: vi.fn(async () => ({ total: 1000, currencies: [] })),
    positions: vi.fn(async () => [
      { trade_id: 7, pair: 'BTC/USDT:USDT', profit_abs: 2 },
    ]),
    trades: vi.fn(async () => ({ trades: [], total_trades: 0 })),
    ops: vi.fn(async () => ({ factors: [], retrain: {} })),
    iteration: vi.fn(async () => ({ live: {} })),
    progress: vi.fn(async () => null),
    researchProgress: vi.fn(async () => null),
    jobs: vi.fn(async () => []),
    services: vi.fn(async () => ({ services: [] })),
    whitelist: vi.fn(async () => ({ whitelist: ['BTC/USDT:USDT'] })),
  };
}

describe('旧引擎总览事实边界', () => {
  it('uses only actual boolean dry_run and never interprets missing or malformed configuration as real trading', () => {
    expect(environmentLabel(null)).toBe('运行环境未知');
    expect(environmentLabel({})).toBe('运行环境未知');
    expect(environmentLabel({ dry_run: 'false' })).toBe('运行环境未知');
    expect(environmentLabel({ dry_run: true })).toBe('模拟盘 dry-run');
    expect(environmentLabel({ dry_run: false })).toBe('实盘配置');
  });
  it('does not turn blank, boolean or malformed amount into zero and requires every component of a sum', () => {
    for (const value of [
      null,
      undefined,
      '',
      ' ',
      true,
      false,
      [],
      {},
      '0x10',
      Infinity,
    ])
      expect(finiteNumber(value)).toBeNull();
    expect(finiteNumber('0')).toBe(0);
    expect(
      sumKnown([{ profit_abs: 3 }, { profit_abs: null }], 'profit_abs'),
    ).toBeNull();
    expect(sumKnown([], 'profit_abs')).toBe(0);
  });
  it('refreshes independent sources, retains known trades and balance on malformed or failed refresh, and accepts genuine empty', async () => {
    const requests = fixture();
    const state = createOverviewState(requests);
    await state.refresh();
    requests.positions.mockResolvedValueOnce({
      error: 'postgres://secret',
    } as never);
    requests.balance.mockRejectedValueOnce(new Error('postgres://secret'));
    await state.refresh();
    expect(state.facts.positions?.[0]?.trade_id).toBe(7);
    expect(state.facts.balance?.total).toBe(1000);
    expect(state.errors.positions).toBe('当前持仓读取失败，保留上次成功快照。');
    expect(JSON.stringify(state.errors)).not.toContain('secret');
    requests.positions.mockResolvedValueOnce([]);
    await state.refresh();
    expect(state.facts.positions).toEqual([]);
    expect(state.errors.positions).toBeUndefined();
    expect(state.updated.positions).toBeTruthy();
  });
  it('rejects malformed history, service arrays and optional numeric fields rather than showing fabricated empty/zero', async () => {
    const requests = fixture();
    requests.trades.mockResolvedValueOnce({
      trades: {},
      total_trades: 0,
    } as never);
    requests.services.mockResolvedValueOnce({ services: null } as never);
    requests.profit.mockResolvedValueOnce({ winrate: true } as never);
    const state = createOverviewState(requests);
    await state.refresh();
    expect(state.facts.trades).toBeNull();
    expect(state.facts.services).toBeNull();
    expect(state.facts.profit).toBeNull();
    expect(Object.keys(state.errors)).toEqual(
      expect.arrayContaining(['trades', 'services', 'profit']),
    );
  });
  it('ignores all late results after disposal and blocks subsequent automatic reads', async () => {
    const requests = fixture();
    let finish!: (value: unknown) => void;
    requests.balance.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }) as never,
    );
    const state = createOverviewState(requests);
    const pending = state.refresh();
    state.dispose();
    finish({ total: 77, currencies: [] });
    await pending;
    expect(state.facts.balance).toBeNull();
    await state.refresh();
    expect(requests.balance).toHaveBeenCalledTimes(1);
    expect(state.loading).toBe(false);
  });
});

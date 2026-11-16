import { describe, expect, it, vi } from 'vitest';
import { createPositionsState, numberValue } from '../positionsState';

const btc = {
  trade_id: 7,
  pair: 'BTC/USDT:USDT',
  is_open: true,
  is_short: false,
  amount: 2,
  profit_abs: 12.5,
};
const eth = {
  trade_id: 8,
  pair: 'ETH/USDT:USDT',
  is_open: false,
  is_short: true,
  profit_abs: -3,
};
function transport() {
  return {
    config: vi.fn(async (): Promise<unknown> => ({
      dry_run: true,
      state: 'running',
      strategy: 'Trend',
    })),
    balance: vi.fn(async (): Promise<unknown> => ({
      total: 1000,
      stake: 'USDT',
      currencies: [],
    })),
    positions: vi.fn(async (): Promise<unknown> => [btc]),
    history: vi.fn(async (): Promise<unknown> => ({
      trades: [eth],
      total_trades: 1,
    })),
    detail: vi.fn(async (id: string): Promise<unknown> => ({
      ...(id === '7' ? btc : eth),
      orders: [],
    })),
    custom: vi.fn(async (): Promise<unknown> => ({
      custom_data: { score: 3 },
    })),
    exit: vi.fn(async (): Promise<unknown> => ({
      result: 'Created exit order',
    })),
    cancel: vi.fn(async (): Promise<unknown> => ({ status: 'success' })),
    reload: vi.fn(async (): Promise<unknown> => ({ ...btc, orders: [] })),
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  return {
    promise: new Promise<T>((r) => {
      resolve = r;
    }),
    resolve,
  };
}

describe('旧 Freqtrade 持仓事实与命令', () => {
  it('distinguishes unknown environment and malformed lists from a genuine empty account', async () => {
    const api = transport();
    api.config.mockResolvedValue({ dry_run: 'false' });
    api.positions.mockResolvedValue({});
    const state = createPositionsState(api);
    await state.refresh();
    expect(state.data.environment).toBe('unknown');
    expect(state.data.positions).toBeNull();
    expect(state.data.history).toHaveLength(1);
    expect(state.requestCommand('market', '7')).toBe(false);
    api.config.mockResolvedValue({ dry_run: false });
    api.positions.mockResolvedValue([]);
    await state.refresh();
    expect(state.data.environment).toBe('live');
    expect(state.data.positions).toEqual([]);
  });
  it('retains good facts and source times after failed or malformed refresh, using fixed safe errors', async () => {
    const api = transport();
    const state = createPositionsState(api);
    await state.refresh();
    const readAt = state.data.readAt.positions;
    api.positions.mockResolvedValue([{}]);
    api.balance.mockRejectedValue(new Error('postgres://secret@host/database'));
    await state.refresh();
    expect(state.data.positions?.[0]?.trade_id).toBe(7);
    expect(state.data.balance?.total).toBe(1000);
    expect(state.data.readAt.positions).toBe(readAt);
    expect(Object.values(state.data.errors).join()).not.toContain('secret');
    expect(state.requestCommand('market', '7')).toBe(false);
  });
  it('uses exact trade identity and ignores late detail and custom replies after another selection', async () => {
    const api = transport();
    const a = deferred<unknown>();
    const c = deferred<unknown>();
    api.detail.mockImplementationOnce(() => a.promise);
    api.custom.mockImplementationOnce(() => c.promise);
    const state = createPositionsState(api);
    await state.refresh();
    const old = state.select('7');
    const custom = state.loadCustom();
    await state.select('8');
    a.resolve({ ...btc, orders: [] });
    c.resolve({ custom_data: 'old private payload' });
    await Promise.all([old, custom]);
    expect(state.data.selectedId).toBe('8');
    expect(state.data.detail?.trade_id).toBe(8);
    expect(state.data.custom).toBe('');
    await state.select('8');
    api.detail.mockResolvedValue({ ...btc, orders: [] });
    await state.select('8');
    expect(state.data.detail?.trade_id).toBe(8);
    expect(state.data.detailError).toBeTruthy();
  });
  it('never substitutes another latest trade for an invalid or missing requested trade', async () => {
    const api = transport();
    const state = createPositionsState(api);
    await state.refresh();
    await state.select(['7', '8']);
    expect(state.data.selectedId).toBeNull();
    expect(state.data.detailError).toBeTruthy();
    api.detail.mockRejectedValue(new Error('not found'));
    await state.select('999');
    expect(state.data.selectedId).toBe('999');
    expect(state.data.detail).toBeNull();
    expect(state.data.detailError).toBeTruthy();
  });
  it('requires explicit confirmation and sends market, limit and partial exit with the frozen real trade id', async () => {
    const api = transport();
    const state = createPositionsState(api);
    await state.refresh();
    expect(api.exit).not.toHaveBeenCalled();
    expect(state.requestCommand('market', '7')).toBe(true);
    expect(api.exit).not.toHaveBeenCalled();
    await state.confirmCommand();
    expect(api.exit).toHaveBeenLastCalledWith({
      tradeid: '7',
      ordertype: 'market',
    });
    expect(state.data.receipt).toContain('回执');
    expect(state.data.receipt).not.toContain('已平仓');
    state.requestCommand('limit', '7');
    state.data.commandValue = '20';
    await state.confirmCommand();
    expect(api.exit).toHaveBeenLastCalledWith({
      tradeid: '7',
      ordertype: 'limit',
      price: 20,
    });
    state.requestCommand('partial', '7');
    state.data.commandValue = '1';
    await state.confirmCommand();
    expect(api.exit).toHaveBeenLastCalledWith({
      tradeid: '7',
      ordertype: 'market',
      amount: 1,
    });
  });
  it('treats a created-exit-order response as a request receipt, not as a completed fill', async () => {
    const api = transport();
    const state = createPositionsState(api);
    await state.refresh();
    api.exit.mockResolvedValue({ status: 'Created exit order', trade_id: 7 });
    state.requestCommand('market', '7');
    await state.confirmCommand();
    expect(state.data.receipt).toContain('收到指令回执');
    expect(state.data.receipt).toContain('实际委托、撤单或成交状态');
    expect(state.data.receipt).not.toContain('已经平仓');
  });
  it('rejects excess partial amounts, unavailable environment and failed receipts without resubmission', async () => {
    const api = transport();
    const state = createPositionsState(api);
    await state.refresh();
    state.requestCommand('partial', '7');
    state.data.commandValue = '2';
    await state.confirmCommand();
    expect(api.exit).not.toHaveBeenCalled();
    expect(state.data.commandError).toContain('小于');
    state.cancelCommand();
    state.requestCommand('partial', '7');
    state.data.commandValue = '3';
    await state.confirmCommand();
    expect(api.exit).not.toHaveBeenCalled();
    expect(state.data.commandError).toBeTruthy();
    state.cancelCommand();
    api.config.mockRejectedValue(new Error('offline'));
    await state.refresh();
    expect(state.requestCommand('market', '7')).toBe(false);
    api.config.mockResolvedValue({ dry_run: true });
    await state.refresh();
    api.exit.mockResolvedValue({ status: 'error' });
    state.requestCommand('market', '7');
    await state.confirmCommand();
    expect(state.data.receipt).toContain('未知');
    expect(api.exit).toHaveBeenCalledTimes(1);
  });
  it('keeps cancel and reload separate and never implements trade-record deletion', async () => {
    const api = transport();
    const state = createPositionsState(api);
    await state.refresh();
    state.requestCommand('cancel', '7');
    await state.confirmCommand();
    expect(api.cancel).toHaveBeenCalledWith('7');
    state.requestCommand('reload', '7');
    await state.confirmCommand();
    expect(api.reload).toHaveBeenCalledWith('7');
    expect(state.requestCommand('delete' as never, '7')).toBe(false);
  });
  it('does not let disposed requests update facts or display late command results', async () => {
    const api = transport();
    const a = deferred<unknown>();
    api.positions.mockImplementationOnce(() => a.promise);
    const state = createPositionsState(api);
    const work = state.refresh();
    state.dispose();
    a.resolve([btc]);
    await work;
    expect(state.data.positions).toBeNull();
  });
  it.each([null, undefined, '', ' ', false, true, {}, [], '0x10', Infinity])(
    'keeps invalid numeric field %j unknown',
    (value) => {
      expect(numberValue(value)).toBeNull();
    },
  );
});

import { beforeEach, describe, expect, it, vi } from 'vitest';

const transport = vi.hoisted(() => ({ get: vi.fn(), auth: vi.fn() }));
vi.mock('#/api/freqtrade/client', () => ({
  ftClient: { get: transport.get },
  unwrap: (value: unknown) => value,
}));
vi.mock('#/api/session', () => ({ requireSession: transport.auth }));
import {
  readDemoAccount,
  readDemoComparisons,
  readDemoEvidence,
  readDemoFills,
  readDemoOrders,
  readDemoPositions,
  readDemoSessions,
  readDemoStrategies,
} from '../settingsApi';

beforeEach(() => {
  vi.resetAllMocks();
  transport.get.mockResolvedValue({
    environment: 'binance_demo',
    items: [],
    limit: 100,
    account: {},
    observedAt: null,
  });
});
describe('Demo 设置原始读取合同', () => {
  it('只向支持分页的端点发送 limit，账户与持仓不能收到多余查询字段', async () => {
    const signal = new AbortController().signal;
    await readDemoPositions(signal);
    await readDemoAccount(signal);
    expect(transport.get.mock.calls).toEqual([
      ['/locals/binance-demo/positions', { signal }],
      ['/locals/binance-demo/account', { signal }],
    ]);
  });
  it('交易记录按稳定会话读取，历史列表独立读取且不带会话', async () => {
    const signal = new AbortController().signal;
    await readDemoOrders(signal, 'demo_history');
    await readDemoFills(signal, 'demo_history');
    await readDemoComparisons(signal, 'demo_history', 'audit');
    await readDemoSessions(signal);
    await readDemoStrategies(signal);
    expect(transport.get.mock.calls).toEqual([
      [
        '/locals/binance-demo/orders',
        { signal, params: { limit: 100, session: 'demo_history' } },
      ],
      [
        '/locals/binance-demo/fills',
        { signal, params: { limit: 100, session: 'demo_history' } },
      ],
      [
        '/locals/binance-demo/comparisons',
        {
          signal,
          params: { limit: 100, session: 'demo_history', view: 'audit' },
        },
      ],
      ['/locals/binance-demo/sessions', { signal, params: { limit: 100 } }],
      ['/locals/binance-demo/strategies', { signal, params: { limit: 100 } }],
    ]);
  });
  it('损坏原始记录抛出错误，不能走兼容空数组分支', async () => {
    transport.get.mockResolvedValue({ environment: 'binance_demo' });
    await expect(readDemoOrders()).rejects.toThrow();
    transport.get.mockResolvedValue({
      environment: 'binance_demo',
      items: [null],
    });
    await expect(readDemoPositions()).rejects.toThrow();
  });
  it('证据必须包含三个真实目标对象，不给证据接口发送 limit', async () => {
    const signal = new AbortController().signal;
    transport.get.mockResolvedValue({
      environment: 'binance_demo',
      endpoint: 'demo-fapi.binance.com',
      scope: 'selected_candle_strategy_execution_only',
      questions: { fees: {}, secondsProfit: {}, execution: {} },
    });
    await readDemoEvidence(signal, 'demo_history');
    expect(transport.get.mock.calls).toEqual([
      [
        '/locals/binance-demo/evidence',
        { signal, params: { session: 'demo_history' } },
      ],
    ]);
    transport.get.mockResolvedValue({
      environment: 'binance_demo',
      scope: 'selected_candle_strategy_execution_only',
      questions: {},
    });
    await expect(readDemoEvidence()).rejects.toThrow();
  });
});

describe('策略列表完整性', () => {
  it('缺失会话身份或错误布尔不能成为可操作会话', async () => {
    transport.get.mockResolvedValue({
      environment: 'binance_demo',
      items: [
        {
          virtualAccountKey: 'demo-a',
          strategyId: 's-a',
          primary: false,
          member: false,
          entriesEnabled: 'false',
          status: 'historical',
          ownedPositions: {},
          metrics: {},
          roundTrips: [],
        },
      ],
    });
    await expect(readDemoStrategies()).rejects.toThrow();
    transport.get.mockResolvedValue({
      environment: 'binance_demo',
      items: [{ virtualAccountKey: 'demo-a', active: false }],
    });
    await expect(readDemoSessions()).rejects.toThrow();
  });
});

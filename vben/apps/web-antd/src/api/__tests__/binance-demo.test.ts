import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), requireSession: vi.fn() }));

vi.mock('../freqtrade/client', () => ({ ftClient: { get: mocks.get, post: mocks.post }, unwrap: (value: unknown) => value }));
vi.mock('../session', () => ({ requireSession: mocks.requireSession }));

import { adoptBinanceDemoSession, attachBinanceDemoStrategy, checkBinanceDemoConnection, getBinanceDemoAccount, getBinanceDemoComparisons, getBinanceDemoEvidence, getBinanceDemoFills, getBinanceDemoOrders, getBinanceDemoPositions, getBinanceDemoSessions, getBinanceDemoStatus, getBinanceDemoStrategies, saveBinanceDemoCredentials, startBinanceDemo, startBinanceDemoSession, stopBinanceDemo, stopBinanceDemoSession, switchBinanceDemo } from '../binance-demo';

describe('authenticated Binance Demo API contract', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.post.mockResolvedValue({ environment: 'binance_demo', ok: true });
  });

  it('submits credentials only in a POST body and sends an exact account for manual start', async () => {
    await saveBinanceDemoCredentials('demo-test-key', 'demo-test-secret');
    await checkBinanceDemoConnection();
    await startBinanceDemo('s03_R50_k1.5');
    await stopBinanceDemo();
    expect(mocks.get).not.toHaveBeenCalled();
    expect(mocks.post.mock.calls).toEqual([
      ['/locals/binance-demo/credentials', { apiKey: 'demo-test-key', secret: 'demo-test-secret' }],
      ['/locals/binance-demo/check', {}],
      ['/locals/binance-demo/start', { accountId: 's03_R50_k1.5' }],
      ['/locals/binance-demo/stop', {}],
    ]);
    expect(mocks.requireSession).toHaveBeenCalledTimes(4);
  });

  it('rejects a real or testnet environment before returning its data to the page', async () => {
    mocks.get.mockResolvedValue({ environment: 'binance_demo', endpoint: 'fapi.binance.com' });
    await expect(getBinanceDemoStatus()).rejects.toThrow('端点不匹配');
    mocks.get.mockResolvedValue({ environment: 'testnet', items: [] });
    await expect(getBinanceDemoFills()).rejects.toThrow('不是币安 Demo');
  });

  it('retains Binance decimal strings and absent commissions without applying configured fees', async () => {
    const fill = { id: '9007199254740995', orderId: '9007199254740996', price: '12345.12345678', qty: '0.00000001', commission: null, maker: null };
    mocks.get.mockResolvedValue({ environment: 'binance_demo', items: [fill], limit: 100 });
    const response = await getBinanceDemoFills();
    expect(response.items).toEqual([fill]);
    expect(mocks.get).toHaveBeenCalledWith('/locals/binance-demo/fills', { params: { limit: 100 }, signal: undefined });
  });

  it('keeps account commission quotations distinct from actual fill charges', async () => {
    const quote = { symbol: 'BTCUSDT', makerCommissionRate: '0.00020000', takerCommissionRate: '0.00050000' };
    mocks.get.mockResolvedValue({ environment: 'binance_demo', endpoint: 'demo-fapi.binance.com', lastCheck: {
      commissionRates: [quote], commissionRateObservedAt: 1000,
      commissionRateSource: 'demo_signed_account_commission_rate_quote_not_fill',
    } });
    const response = await getBinanceDemoStatus();
    expect(response.lastCheck?.commissionRates).toEqual([quote]);
    expect(response.lastCheck?.commissionRateSource).toBe('demo_signed_account_commission_rate_quote_not_fill');
    expect(response.lastCheck?.commissionRateObservedAt).toBe(1000);
  });

  it('does not issue network requests without an authenticated session', async () => {
    mocks.requireSession.mockImplementationOnce(() => { throw new Error('请先登录'); });
    await expect(saveBinanceDemoCredentials('key', 'secret')).rejects.toThrow('请先登录');
    expect(mocks.post).not.toHaveBeenCalled();
  });

  it('requests a strategy switch separately from start and never automatically enables the new strategy', async () => {
    await switchBinanceDemo('s05_R50_k1.5');
    expect(mocks.post.mock.calls).toEqual([
      ['/locals/binance-demo/switch', { accountId: 's05_R50_k1.5' }],
    ]);
    expect(mocks.requireSession).toHaveBeenCalledTimes(1);
  });

  it('attaches a strategy paused without issuing a start or sending credentials again', async () => {
    const paused = { environment: 'binance_demo', virtualAccountKey: 'demo_strategy_new', entriesEnabled: false };
    mocks.post.mockResolvedValue(paused);
    expect(await attachBinanceDemoStrategy('s07_R1000_k1.0')).toEqual(paused);
    expect(mocks.post.mock.calls).toEqual([
      ['/locals/binance-demo/attach', { accountId: 's07_R1000_k1.0' }],
    ]);
    expect(mocks.requireSession).toHaveBeenCalledTimes(1);
  });

  it('targets one immutable namespace for start, pause and paused historical adoption', async () => {
    const session = 'demo_strategy_owned_session';
    await startBinanceDemoSession(session);
    await stopBinanceDemoSession(session);
    await adoptBinanceDemoSession(session);
    expect(mocks.post.mock.calls).toEqual([
      ['/locals/binance-demo/start-session', { session }],
      ['/locals/binance-demo/stop-session', { session }],
      ['/locals/binance-demo/adopt-session', { session }],
    ]);
    expect(mocks.requireSession).toHaveBeenCalledTimes(3);
    expect(mocks.get).not.toHaveBeenCalled();
  });

  it('reads all strategy namespaces and their actual ledgers without a record limit or mutations', async () => {
    const signal = new AbortController().signal;
    const session = { virtualAccountKey: 'demo_strategy_first', strategyId: 's35', primary: false,
      member: true, entriesEnabled: true, status: 'running', ownedPositions: [{ pair: 'ETH/USDT:USDT', quantity: '0.01' }],
      metrics: { fillCount: 2, actualNetTradingPnlUsdt: null }, roundTrips: [] };
    mocks.get.mockResolvedValue({ environment: 'binance_demo', items: [session] });
    expect((await getBinanceDemoStrategies(signal)).items).toEqual([session]);
    expect(mocks.get.mock.calls).toEqual([['/locals/binance-demo/strategies', { signal }]]);
    expect(mocks.post).not.toHaveBeenCalled();
  });

  it('rejects parallel operations before sending a request when authentication is unavailable', async () => {
    mocks.requireSession.mockImplementation(() => { throw new Error('请先登录'); });
    await expect(attachBinanceDemoStrategy('s07')).rejects.toThrow('请先登录');
    await expect(startBinanceDemoSession('demo_strategy_s07')).rejects.toThrow('请先登录');
    await expect(stopBinanceDemoSession('demo_strategy_s07')).rejects.toThrow('请先登录');
    await expect(adoptBinanceDemoSession('demo_strategy_history')).rejects.toThrow('请先登录');
    await expect(getBinanceDemoStrategies()).rejects.toThrow('请先登录');
    expect(mocks.post).not.toHaveBeenCalled();
    expect(mocks.get).not.toHaveBeenCalled();
    mocks.requireSession.mockReset();
  });

  it('reads historical session records without changing the live account or sending mutations', async () => {
    const session = 'demo_strategy_1234567890';
    const history = { environment: 'binance_demo', endpoint: 'demo-fapi.binance.com',
      scope: 'selected_candle_strategy_execution_only', items: [] };
    mocks.get.mockResolvedValue(history);
    await getBinanceDemoOrders(undefined, session);
    await getBinanceDemoFills(undefined, session);
    await getBinanceDemoComparisons(undefined, session);
    await getBinanceDemoEvidence(undefined, session);
    await getBinanceDemoSessions();
    await getBinanceDemoAccount();
    await getBinanceDemoPositions();
    expect(mocks.get.mock.calls).toEqual([
      ['/locals/binance-demo/orders', { params: { limit: 100, session }, signal: undefined }],
      ['/locals/binance-demo/fills', { params: { limit: 100, session }, signal: undefined }],
      ['/locals/binance-demo/comparisons', { params: { limit: 100, session, view: 'signals' }, signal: undefined }],
      ['/locals/binance-demo/evidence', { params: { session }, signal: undefined }],
      ['/locals/binance-demo/sessions', { signal: undefined }],
      ['/locals/binance-demo/account', { signal: undefined }],
      ['/locals/binance-demo/positions', { signal: undefined }],
    ]);
    expect(mocks.post).not.toHaveBeenCalled();
  });

  it('queries signals separately from routine audit history with the same captured session and abort signal', async () => {
    const session = 'demo_strategy_history';
    const signal = new AbortController().signal;
    mocks.get.mockResolvedValue({ environment: 'binance_demo', items: [] });
    await getBinanceDemoComparisons(signal, session);
    await getBinanceDemoComparisons(signal, session, 'audit');
    expect(mocks.get.mock.calls).toEqual([
      ['/locals/binance-demo/comparisons', { params: { limit: 100, session, view: 'signals' }, signal }],
      ['/locals/binance-demo/comparisons', { params: { limit: 100, session, view: 'audit' }, signal }],
    ]);
    expect(mocks.post).not.toHaveBeenCalled();
  });

  it('does not put a limit query on current account and position snapshots', async () => {
    mocks.get.mockResolvedValue({ environment: 'binance_demo', items: [] });
    await getBinanceDemoAccount();
    await getBinanceDemoPositions();
    expect(mocks.get.mock.calls).toEqual([
      ['/locals/binance-demo/account', { signal: undefined }],
      ['/locals/binance-demo/positions', { signal: undefined }],
    ]);
  });

  it('loads all-history evidence without a 100-record limit and preserves unknown net and fee values', async () => {
    const value = { environment: 'binance_demo', endpoint: 'demo-fapi.binance.com',
      scope: 'selected_candle_strategy_execution_only', snapshotAt: '2026-10-12T03:00:00+08:00', questions: {
        fees: { state: 'actual_fees_partially_known', fillCount: 2, knownFeeFillCount: 1, unknownFeeFillCount: 1, rates: [{ symbol: 'BTCUSDT', liquidity: 'unknown', role: 'entry', fillCount: 2, knownCommissionUsdt: null, actualReportedQuoteUsdt: null, effectiveFeeRate: null }] },
        secondsProfit: { state: 'seconds_experiment_not_configured', closedNetTradingPnlUsdt: null, liveProfitabilityVerified: false },
      } };
    mocks.get.mockResolvedValue(value);
    const response = await getBinanceDemoEvidence();
    expect(response.questions.fees.rates[0]?.effectiveFeeRate).toBeNull();
    expect(response.questions.secondsProfit.closedNetTradingPnlUsdt).toBeNull();
    expect(response.questions.secondsProfit.liveProfitabilityVerified).toBe(false);
    expect(mocks.get).toHaveBeenCalledWith('/locals/binance-demo/evidence', { signal: undefined });
  });

  it('does not display a report claiming an unsupported evidence scope', async () => {
    mocks.get.mockResolvedValue({ environment: 'binance_demo', endpoint: 'demo-fapi.binance.com', scope: 'seconds_profitability_verified' });
    await expect(getBinanceDemoEvidence()).rejects.toThrow('证据范围不匹配');
  });
});

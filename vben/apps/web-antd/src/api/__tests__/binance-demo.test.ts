import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), requireSession: vi.fn() }));

vi.mock('../freqtrade/client', () => ({ ftClient: { get: mocks.get, post: mocks.post }, unwrap: (value: unknown) => value }));
vi.mock('../session', () => ({ requireSession: mocks.requireSession }));

import { checkBinanceDemoConnection, getBinanceDemoAccount, getBinanceDemoFills, getBinanceDemoPositions, getBinanceDemoStatus, saveBinanceDemoCredentials, startBinanceDemo, stopBinanceDemo } from '../binance-demo';

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

  it('does not issue network requests without an authenticated session', async () => {
    mocks.requireSession.mockImplementationOnce(() => { throw new Error('请先登录'); });
    await expect(saveBinanceDemoCredentials('key', 'secret')).rejects.toThrow('请先登录');
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
});

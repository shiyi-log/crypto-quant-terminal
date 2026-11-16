import { describe, expect, it } from 'vitest';
import * as research from './surgeResearch';

const event = (id = 'surge-a', pair = 'BTC/USDT:USDT') => ({
  event_id: id,
  pair,
  timeframe: '5m',
  timestamp: 300000,
  status: 'unknown',
  observation: {
    timestamp: 300000,
    pair,
    volume_ratio: '4.000',
    quote_volume: '800',
    baseline_quote_volume: '200',
    num_trades: 5,
    flow_imbal: '0',
  },
  method_version: 'volume-v1',
  data_fingerprint: 'source-a',
  completed_horizons: ['5m'],
  required_horizons: ['5m', '30m', '1h', '4h'],
});
const deferred = () => {
  let resolve!: (v: unknown) => void;
  let reject!: (r: unknown) => void;
  const promise = new Promise((a, b) => {
    resolve = a;
    reject = b;
  });
  return { promise, resolve, reject };
};

describe('订单流事实和读取代次', () => {
  it('空白和非十进制文本不是零观测', () => {
    expect(research.finite('   ')).toBeNull();
    expect(research.finite('0x10')).toBeNull();
    expect(research.finite('0')).toBe(0);
  });
  it('错误预热布尔和负计数不能显示基线就绪', () => {
    for (const warmup of [
      { ready: 'true' },
      { valid_bars: -9 },
      { required_bars: true },
      { last_timestamp: '2026-02-31 00:00:00' },
    ]) {
      expect(() =>
        research.parseSurgeStats({
          by_pair: [],
          warmup: { 'BTC/USDT:USDT': warmup },
        }),
      ).toThrow();
    }
    expect(() =>
      research.parseSurgeStats({
        by_pair: [
          { pair: 'BTC/USDT:USDT', horizons: { '5m': { sample_count: true } } },
        ],
      }),
    ).toThrow();
  });
  it('修订事实已无效时，嵌套旧matured标签不能恢复收益', () => {
    const detail = research.parseSurgeDetail(
      {
        ...event(),
        revisions: [
          {
            event_id: 'surge-a',
            revision_no: 2,
            status: 'invalid',
            result: { horizon: '5m', status: 'matured', return: 99 },
          },
        ],
      },
      'surge-a',
    );
    expect(research.maturityWindows(detail)[0]).toEqual({
      horizon: '5m',
      status: 'invalid',
    });
  });
  it('查询交易对不匹配的成功响应不能冒充当前筛选事实', async () => {
    const reader = research.createSurgeReader(async () => ({
      items: [event()],
    }));
    await reader.refresh({ pair: 'ETH/USDT:USDT', timeframe: '5m' });
    expect(reader.state.status).toBe('failed');
    expect(reader.state.snapshot).toBeNull();
  });
  it('缺失或损坏列表不会被转换为真实空集', () => {
    expect(research.parseSurgeList({ items: [] })).toEqual([]);
    for (const bad of [
      {},
      { items: {} },
      { items: [event(), event()] },
      { items: [{ ...event(), timestamp: true }] },
      { items: [{ ...event(), event_id: '' }] },
    ])
      expect(() => research.parseSurgeList(bad)).toThrow();
    expect(
      research.parseSurgeList({ items: [event()] })[0]?.observation
        ?.volume_ratio,
    ).toBe('4.000');
  });
  it('详情精确绑定查询事件并保留全部原始修订', () => {
    const raw = {
      ...event(),
      revisions: [
        { event_id: 'surge-a', revision_no: 1, revision_kind: 'observed' },
        {
          event_id: 'surge-a',
          revision_no: 2,
          revision_kind: 'maturity',
          status: 'matured',
          result: { horizon: '5m', status: 'matured', return: 0.01 },
        },
      ],
    };
    expect(research.parseSurgeDetail(raw, 'surge-a').events).toHaveLength(2);
    expect(() => research.parseSurgeDetail(raw, 'surge-b')).toThrow();
    expect(() =>
      research.parseSurgeDetail({ ...raw, revisions: undefined }, 'surge-a'),
    ).toThrow();
    expect(() =>
      research.parseSurgeDetail(
        { ...raw, revisions: [{ event_id: 'foreign' }] },
        'surge-a',
      ),
    ).toThrow();
  });
  it('最新无效修订移除旧收益且缺失窗口不填零', () => {
    const detail = research.parseSurgeDetail(
      {
        ...event(),
        revisions: [
          {
            event_id: 'surge-a',
            revision_no: 2,
            revision_kind: 'maturity',
            status: 'matured',
            result: { horizon: '5m', status: 'matured', return: 0.1, mfe: 0.2 },
          },
          {
            event_id: 'surge-a',
            revision_no: 3,
            revision_kind: 'maturity',
            status: 'invalid',
            result: { horizon: '5m', status: 'invalid', return: 999 },
          },
        ],
      },
      'surge-a',
    );
    expect(
      research
        .maturityWindows(detail)
        .map((r: any) => [r.horizon, r.status, r.return]),
    ).toEqual([
      ['5m', 'invalid', undefined],
      ['30m', 'unknown', undefined],
      ['1h', 'unknown', undefined],
      ['4h', 'unknown', undefined],
    ]);
  });
  it('统计缺失不是零样本且原生by_pair列表可识别', () => {
    expect(() => research.parseSurgeStats({})).toThrow();
    const stats = research.parseSurgeStats({
      by_pair: [
        {
          pair: 'BTC/USDT:USDT',
          total: 0,
          horizons: { '5m': { sample_count: 0, gross_return_mean: null } },
        },
      ],
    });
    expect(research.pairStats(stats, 'BTC/USDT:USDT')?.total).toBe(0);
    expect(research.pairStats(stats, 'ETH/USDT:USDT')).toBeNull();
    expect(() =>
      research.parseSurgeStats({
        by_pair: [{ pair: 'BTC/USDT:USDT' }, { pair: 'BTC/USDT:USDT' }],
      }),
    ).toThrow();
  });
  it('选择变更中止旧读取，迟到成功不能覆盖新身份', async () => {
    const old = deferred(),
      next = deferred();
    let signal!: AbortSignal;
    const reader = research.createSurgeReader((query: any, s: AbortSignal) => {
      if (query.pair === 'BTC/USDT:USDT') {
        signal = s;
        return old.promise;
      }
      return next.promise;
    });
    const first = reader.refresh({ pair: 'BTC/USDT:USDT' }),
      second = reader.refresh({ pair: 'ETH/USDT:USDT' });
    expect(signal.aborted).toBe(true);
    next.resolve({ items: [event('surge-eth', 'ETH/USDT:USDT')] });
    await second;
    old.resolve({ items: [event()] });
    await first;
    expect(reader.state.snapshot?.[0]?.event_id).toBe('surge-eth');
    expect(reader.state.query.pair).toBe('ETH/USDT:USDT');
    expect(reader.state.error).toBe('');
  });
  it('失败保留旧事实和旧查询，卸载使迟到结果失效', async () => {
    const pending = deferred();
    let n = 0;
    const reader = research.createSurgeReader(() => {
      n++;
      if (n === 1) return Promise.resolve({ items: [event()] });
      if (n === 2) return Promise.reject(new Error('secret upstream'));
      return pending.promise;
    });
    await reader.refresh({ pair: 'BTC/USDT:USDT' });
    await reader.refresh({ pair: 'ETH/USDT:USDT' });
    expect(reader.state.snapshot?.[0]?.event_id).toBe('surge-a');
    expect(reader.state.snapshotQuery?.pair).toBe('BTC/USDT:USDT');
    expect(reader.state.status).toBe('stale');
    expect(reader.state.error).not.toContain('secret upstream');
    const last = reader.refresh({ pair: 'ETH/USDT:USDT' });
    reader.dispose();
    pending.resolve({ items: [event('later')] });
    await last;
    expect(reader.state.snapshot?.[0]?.event_id).toBe('surge-a');
  });
});

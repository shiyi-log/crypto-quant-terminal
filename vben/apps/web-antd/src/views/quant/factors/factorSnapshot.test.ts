import { describe, expect, it } from 'vitest';

import {
  createFactorReader,
  parseFactorSnapshot,
  selectFactors,
  snapshotFreshness,
} from './factorSnapshot';

const raw = () => ({
  generated_at: '2026-11-16 14:00:00',
  factors: [
    {
      factor: 'momentum_7',
      group: '动量',
      ic_base: 0.12,
      ic_recent: '-0.0400',
      t_recent: -2.3,
      drift: '-0.1600',
      alert: true,
      weak: false,
      flip: true,
      decay: false,
      status: '符号反转',
    },
    {
      factor: 'volume_ratio',
      group: '成交量',
      ic_base: null,
      ic_recent: 0,
      t_recent: null,
    },
  ],
});

describe('历史因子快照合同', () => {
  it('保留真实零与未知统计值，缺失告警不能变成正常', () => {
    const snapshot = parseFactorSnapshot(raw());
    expect(snapshot.factors[0]).toMatchObject({
      name: 'momentum_7',
      recent: '-0.0400',
      alert: true,
      group: '动量',
    });
    expect(snapshot.factors[1]).toMatchObject({
      baseline: null,
      recent: '0',
      t: null,
      alert: null,
    });
    expect(snapshot.generatedMs).toBe(Date.UTC(2026, 10, 16, 6));
  });
  it('真正空快照有效，但缺少或损坏的列表不能伪装为空', () => {
    expect(parseFactorSnapshot({ factors: [] }).factors).toEqual([]);
    for (const value of [
      null,
      {},
      { factors: null },
      { factors: 'empty' },
      { factors: [null] },
    ]) {
      expect(() => parseFactorSnapshot(value)).toThrow();
    }
  });
  it('重复身份、坏统计值和坏标志拒绝整份快照', () => {
    const first = raw().factors[0]!;
    for (const row of [
      { ...first, ic_recent: true },
      { ...first, t_recent: Number.NaN },
      { ...first, alert: 'false' },
      { ...first, factor: '' },
    ]) {
      expect(() => parseFactorSnapshot({ factors: [row] })).toThrow();
    }
    expect(() => parseFactorSnapshot({ factors: [first, first] })).toThrow();
  });
  it('筛选不改写数据，未知告警不进入已告警列表', () => {
    const snapshot = parseFactorSnapshot(raw());
    expect(
      selectFactors(snapshot, '全部', true).map((row) => row.name),
    ).toEqual(['momentum_7']);
    expect(
      selectFactors(snapshot, '成交量', false).map((row) => row.name),
    ).toEqual(['volume_ratio']);
    expect(snapshot.factors).toHaveLength(2);
  });
  it('没有时间、未来时间和过期时间不被显示为新鲜', () => {
    const now = Date.UTC(2026, 10, 16, 7);
    expect(snapshotFreshness(null, now)).toBe('unknown');
    expect(snapshotFreshness(now + 1, now)).toBe('unknown');
    expect(snapshotFreshness(now - 3_600_001, now)).toBe('stale');
    expect(snapshotFreshness(now - 3_600_000, now)).toBe('fresh');
  });
  it('无效日历时间与缺少时区的非旧协议文本保持未知', () => {
    for (const generated_at of [
      '2026-02-30 14:00:00',
      '2026-02-30T06:00:00Z',
      '2026-11-16T06:00:00',
    ]) {
      expect(
        parseFactorSnapshot({ factors: [], generated_at }).generatedMs,
      ).toBeNull();
    }
    expect(
      parseFactorSnapshot({ factors: [], generated_at: '2024-02-29 14:00:00' })
        .generatedMs,
    ).toBe(Date.UTC(2024, 1, 29, 6));
  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

describe('因子读取发布边界', () => {
  it('错误和损坏响应保留成功快照，并显式标记读取失败', async () => {
    const reader = createFactorReader();
    await reader.load(async () => raw());
    await reader.load(async () => {
      throw new Error('signed transport secret');
    });
    expect(reader.state.snapshot?.factors).toHaveLength(2);
    expect(reader.state.error).toBe('读取失败，保留上次成功快照');
    await reader.load(async () => ({ malformed: true }));
    expect(reader.state.snapshot?.factors[0]?.name).toBe('momentum_7');
    expect(reader.state.error).not.toContain('secret');
  });
  it('后发请求优先，晚到旧响应不得覆盖当前筛选上下文', async () => {
    const reader = createFactorReader();
    const old = deferred<unknown>();
    const first = reader.load(() => old.promise);
    await reader.load(async () => ({
      factors: [{ factor: 'current', ic_recent: '0.5' }],
    }));
    old.resolve(raw());
    await first;
    expect(reader.state.snapshot?.factors.map((row) => row.name)).toEqual([
      'current',
    ]);
    expect(reader.state.loading).toBe(false);
  });
  it('取消后不发布旧响应或错误，新的请求仍可正常完成', async () => {
    const reader = createFactorReader();
    const old = deferred<unknown>();
    let signal: AbortSignal | undefined;
    const first = reader.load((current) => {
      signal = current;
      return old.promise;
    });
    reader.cancel();
    expect(signal?.aborted).toBe(true);
    old.reject(new Error('old failure'));
    await first;
    expect(reader.state.snapshot).toBe(null);
    expect(reader.state.error).toBe('');
    await reader.load(async () => ({ factors: [] }));
    expect(reader.state.snapshot?.factors).toEqual([]);
  });
});

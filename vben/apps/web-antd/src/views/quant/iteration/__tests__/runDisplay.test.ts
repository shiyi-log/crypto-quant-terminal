import { describe, expect, it } from 'vitest';

import {
  createRunDetail,
  createRunSnapshot,
  executionView,
  formatRunMetric,
  formatRunTime,
  refreshRunDetail,
  refreshRunSnapshot,
  researchView,
  runMetrics,
} from '../runDisplay';

describe('iteration execution and research presentation', () => {
  it('uses normalized gate values when raw metrics only contain oos aliases', () => {
    const metrics = runMetrics({
      run_id: 'run-a',
      metrics: { oos_trades: 120, oos_profit_pct: 5 },
      gate: { values: { trades: 120, profit_pct: 5 } },
    });
    expect(metrics.trades).toBe(120);
    expect(metrics.profit_pct).toBe(5);
    expect(
      runMetrics({
        run_id: 'run-b',
        metrics: { sharpe: 99, profit_factor: 2 },
        gate: { values: { sharpe: null } },
      }),
    ).toMatchObject({ sharpe: null, profit_factor: 2 });
  });

  it('shows the same instant in Beijing time across UTC and offset inputs', () => {
    expect(formatRunTime('2026-11-15T16:10:00Z')).toBe('2026-11-16 00:10:00');
    expect(formatRunTime('2026-11-16T00:10:00+08:00')).toBe(
      '2026-11-16 00:10:00',
    );
    expect(formatRunTime(undefined)).toBe('未记录');
    expect(formatRunTime('invalid')).toBe('未记录');
  });

  it('shows an unprofitable completed run as successful execution and rejected research', () => {
    expect(executionView('succeeded')).toEqual({
      label: '执行成功',
      color: 'green',
    });
    expect(researchView('rejected')).toEqual({
      label: '研究拒绝',
      color: 'orange',
    });
  });

  it('keeps execution failures distinct from an insufficient research sample', () => {
    expect(executionView('failed')).toEqual({
      label: '执行失败',
      color: 'red',
    });
    expect(researchView('not_estimable')).toEqual({
      label: '不可评估',
      color: 'default',
    });
    expect(researchView(undefined).label).toBe('尚无裁定');
    expect(executionView('unexpected').label).toBe('状态未知');
  });

  it('preserves a genuine zero and never displays missing or nonfinite metrics as zero', () => {
    expect(formatRunMetric(0, '%')).toBe('0.00%');
    expect(formatRunMetric(-2.5, '%')).toBe('-2.50%');
    for (const value of [null, undefined, Number.NaN, Infinity, '0']) {
      expect(formatRunMetric(value, '%')).toBe('未知');
    }
  });
});

describe('iteration evidence selection', () => {
  it('never displays a previous run while loading another run', async () => {
    const state = createRunDetail();
    await refreshRunDetail(state, 'run-a', async () => ({
      run_id: 'run-a',
      execution_status: 'succeeded',
      research_verdict: 'rejected',
    }));
    let complete!: (value: {
      run_id: string;
      execution_status: string;
      research_verdict: string;
    }) => void;
    const pending = refreshRunDetail(
      state,
      'run-b',
      () =>
        new Promise((resolve) => {
          complete = resolve;
        }),
    );
    expect(state.item).toBeNull();
    expect(state.selectedRunId).toBe('run-b');
    complete({
      run_id: 'run-b',
      execution_status: 'failed',
      research_verdict: 'not_estimable',
    });
    await pending;
    expect(state.item?.run_id).toBe('run-b');
  });

  it('rejects detail belonging to another run', async () => {
    const state = createRunDetail();
    await refreshRunDetail(state, 'run-a', async () => ({
      run_id: 'run-other',
      execution_status: 'succeeded',
      research_verdict: 'passed',
    }));
    expect(state.item).toBeNull();
    expect(state.error).toBeTruthy();
  });

  it('ignores an older detail response that arrives after a later selection', async () => {
    const state = createRunDetail();
    let complete!: (value: {
      run_id: string;
      execution_status: string;
      research_verdict: string;
    }) => void;
    const pending = refreshRunDetail(
      state,
      'run-a',
      () =>
        new Promise((resolve) => {
          complete = resolve;
        }),
    );
    await refreshRunDetail(state, 'run-b', async () => ({
      run_id: 'run-b',
      execution_status: 'failed',
      research_verdict: 'not_estimable',
    }));
    complete({
      run_id: 'run-a',
      execution_status: 'succeeded',
      research_verdict: 'passed',
    });
    await pending;
    expect(state.item?.run_id).toBe('run-b');
    expect(state.loading).toBe(false);
  });
});

describe('iteration run snapshot refresh', () => {
  it('retains last successful run identities after a request failure', async () => {
    const state = createRunSnapshot();
    await refreshRunSnapshot(state, async () => ({
      items: [
        {
          run_id: 'run-a',
          execution_status: 'failed',
          research_verdict: 'not_estimable',
        },
      ],
      total: 1,
    }));
    await refreshRunSnapshot(state, async () => {
      throw new Error('offline');
    });
    expect(state.items.map((run) => run.run_id)).toEqual(['run-a']);
    expect(state.hasSnapshot).toBe(true);
    expect(state.error).toBeTruthy();
    expect(state.loading).toBe(false);
  });

  it('rejects a malformed list without replacing the existing evidence', async () => {
    const state = createRunSnapshot();
    await refreshRunSnapshot(state, async () => ({
      items: [
        {
          run_id: 'run-a',
          execution_status: 'succeeded',
          research_verdict: 'rejected',
        },
      ],
      total: 1,
    }));
    await refreshRunSnapshot(
      state,
      async () => ({ items: [{ status: 'done' }], total: 1 }) as never,
    );
    expect(state.items.map((run) => run.run_id)).toEqual(['run-a']);
    expect(state.error).toBeTruthy();
  });

  it('distinguishes a real empty list from an unavailable first response', async () => {
    const state = createRunSnapshot();
    await refreshRunSnapshot(state, async () => {
      throw new Error('offline');
    });
    expect(state.hasSnapshot).toBe(false);
    await refreshRunSnapshot(state, async () => ({ items: [], total: 0 }));
    expect(state.hasSnapshot).toBe(true);
    expect(state.items).toEqual([]);
    expect(state.error).toBe('');
  });

  it('does not let a concurrent refresh replace the first in-flight snapshot', async () => {
    const state = createRunSnapshot();
    let complete!: (value: { items: []; total: number }) => void;
    const first = refreshRunSnapshot(
      state,
      () =>
        new Promise((resolve) => {
          complete = resolve;
        }),
    );
    await refreshRunSnapshot(state, async () => ({
      items: [
        {
          run_id: 'run-other',
          execution_status: 'running',
          research_verdict: 'not_estimable',
        },
      ],
      total: 1,
    }));
    complete({ items: [], total: 0 });
    await first;
    expect(state.items).toEqual([]);
    expect(state.total).toBe(0);
  });
});

describe('historical run contract hardening', () => {
  it('treats inherited dictionary names as unknown statuses', () => {
    expect(executionView('toString')).toEqual({
      label: '状态未知',
      color: 'default',
    });
    expect(researchView('__proto__')).toEqual({
      label: '尚无裁定',
      color: 'default',
    });
  });
  it('rejects duplicate identities or malformed typed fields while retaining known history', async () => {
    const state = createRunSnapshot();
    await refreshRunSnapshot(state, async () => ({
      total: 1,
      items: [
        { run_id: 'known', strategy: null, model: null, timerange: null },
      ],
    }));
    expect(state.items[0]?.run_id).toBe('known');
    for (const items of [
      [{ run_id: 'same' }, { run_id: 'same' }],
      [{ run_id: 'bad', execution_status: { running: true } }],
      [{ run_id: 'bad', gate: { reasons: [7] } }],
    ]) {
      await refreshRunSnapshot(state, async () => ({
        total: items.length,
        items,
      }));
      expect(state.items[0]?.run_id).toBe('known');
      expect(state.error).toBeTruthy();
    }
  });
});

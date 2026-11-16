import { describe, expect, it } from 'vitest';

const source = () => import('../experimentState');
const run = (id = 'run-a') => ({
  run_id: id,
  execution_status: 'succeeded',
  research_verdict: 'rejected',
  metrics: { profit_pct: '-1.234567890123456789' },
  evidence: { code_hash: 'abc' },
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe('实验任务事实', () => {
  it('保留十进制原文，执行成功不推导研究通过', async () => {
    const { parseRun, executionLabel, verdictLabel, metricText } =
      await source();
    const item = parseRun(run());
    expect(executionLabel(item.execution_status)).toBe('执行成功');
    expect(verdictLabel(item.research_verdict)).toBe('研究拒绝');
    expect(metricText(item.metrics?.profit_pct)).toBe('-1.234567890123456789');
    expect(metricText(undefined)).toBe('未知');
  });

  it('后端无策略的合法空字段保留为未知，不拒绝整份列表', async () => {
    const { parseRunList } = await source();
    const result = parseRunList({
      items: [
        {
          run_id: 'run-disabled',
          execution_status: 'disabled',
          research_verdict: 'not_estimable',
          strategy: null,
          model: null,
          timerange: null,
        },
      ],
      total: 1,
    });
    expect(result.items[0]?.run_id).toBe('run-disabled');
    expect(result.items[0]?.strategy).toBeNull();
  });

  it('未知状态不能读取原型属性，超出有限数值范围的指标显示未知', async () => {
    const { executionLabel, verdictLabel, metricText } = await source();
    for (const key of ['toString', '__proto__', 'constructor']) {
      expect(executionLabel(key)).toBe('执行状态未知');
      expect(verdictLabel(key)).toBe('尚无研究裁定');
    }
    expect(metricText('1e9999')).toBe('未知');
  });

  it('合法空记录与损坏合同分开，拒绝重复身份或缺少总数', async () => {
    const { parseRunList } = await source();
    expect(parseRunList({ items: [], total: 0 })).toEqual({
      items: [],
      total: 0,
      generated_at: null,
    });
    for (const value of [
      null,
      {},
      { items: [], total: null },
      { items: [run()], total: 0 },
      { items: [run(), run()], total: 2 },
      { items: [{ run_id: '' }], total: 1 },
      { items: [{ ...run(), evidence: [] }], total: 1 },
    ]) {
      expect(() => parseRunList(value)).toThrow();
    }
  });

  it('刷新失败或损坏响应保留已知列表，不把总数变零', async () => {
    const { createExperimentState, refreshRuns } = await source();
    const state = createExperimentState();
    await refreshRuns(state, async () => ({ items: [run()], total: 12 }));
    await refreshRuns(state, async () => {
      throw new Error('offline');
    });
    expect(state.snapshot?.total).toBe(12);
    expect(state.listError).toContain('保留');
    await refreshRuns(state, async () => ({ items: [] }));
    expect(state.snapshot?.items[0]?.run_id).toBe('run-a');
    expect(state.listError).toContain('合同');
  });

  it('新刷新取消上一请求，旧列表迟到不能覆盖新列表', async () => {
    const { createExperimentState, refreshRuns } = await source();
    const state = createExperimentState();
    const first = deferred<unknown>();
    let oldSignal: AbortSignal | undefined;
    const pending = refreshRuns(state, async (signal) => {
      oldSignal = signal;
      return first.promise;
    });
    await refreshRuns(state, async () => ({ items: [run('run-b')], total: 1 }));
    first.resolve({ items: [run()], total: 1 });
    await pending;
    expect(oldSignal?.aborted).toBe(true);
    expect(state.snapshot?.items[0]?.run_id).toBe('run-b');
  });

  it('切换轮次清除旧身份详情，迟到返回与错误身份都不发布', async () => {
    const { createExperimentState, selectRun } = await source();
    const state = createExperimentState();
    const first = deferred<unknown>();
    const pending = selectRun(state, 'run-a', async () => first.promise);
    await selectRun(state, 'run-b', async () => run('run-b'));
    first.resolve(run());
    await pending;
    expect(state.detail?.run_id).toBe('run-b');
    await selectRun(state, 'run-c', async () => run('run-b'));
    expect(state.detail).toBeNull();
    expect(state.detailError).toContain('身份');
  });

  it('同轮读取失败保留详情，销毁后忽略迟到数据', async () => {
    const { createExperimentState, selectRun, cancelExperiments } =
      await source();
    const state = createExperimentState();
    await selectRun(state, 'run-a', async () => run());
    await selectRun(state, 'run-a', async () => {
      throw new Error('offline');
    });
    expect(state.detail?.run_id).toBe('run-a');
    expect(state.detailError).toContain('保留');
    const late = deferred<unknown>();
    const pending = selectRun(state, 'run-b', async () => late.promise);
    cancelExperiments(state);
    late.resolve(run('run-b'));
    await pending;
    expect(state.detail).toBeNull();
  });

  it('仅为已记录的产物创建精确历史明细链接', async () => {
    const { artifactLink, identityFacts } = await source();
    expect(artifactLink(run())).toBeNull();
    expect(artifactLink({ ...run(), identifier: 'archive a/b' })).toBe(
      '/quant/detail?identifier=archive%20a%2Fb',
    );
    expect(
      identityFacts(run()).find((fact) => fact.label === '数据指纹')?.value,
    ).toBe('未知');
  });
});

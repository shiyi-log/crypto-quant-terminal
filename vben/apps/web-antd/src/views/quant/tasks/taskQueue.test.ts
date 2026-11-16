// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest';

import {
  createTaskQueue,
  parseAnalysisResult,
  parseHistory,
  parseJob,
  parseStatus,
} from './taskQueue';

const status = (running = false) => ({
  running,
  status: running ? 'running' : 'ended',
  progress: 0,
  status_msg: '',
  step: '',
});
const history = (notes = '保留证据') => [
  {
    filename: 'result-a.json',
    strategy: 'StrategyA',
    run_id: 'run-a',
    backtest_start_time: 1,
    notes,
  },
];
function api() {
  return {
    getBacktestStatus: vi.fn(async () => status()),
    getBacktestHistory: vi.fn(async () => history()),
    getWebStrategies: vi.fn(async () => ({ strategies: ['StrategyA'] })),
    getWebAvailablePairs: vi.fn(async () => ({ pairs: ['BTC/USDT:USDT'] })),
    getWhitelist: vi.fn(async () => ({ whitelist: ['BTC/USDT:USDT'] })),
    postBacktest: vi.fn(async () => status(true)),
    abortBacktest: vi.fn(async () => status()),
    resetBacktest: vi.fn(async () => status()),
    postDownloadData: vi.fn(async () => ({
      job_id: 'job-a',
      status: 'started',
    })),
    postLookaheadAnalysis: vi.fn(async () => ({
      job_id: 'job-a',
      status: 'started',
    })),
    postRecursiveAnalysis: vi.fn(async () => ({
      job_id: 'job-a',
      status: 'started',
    })),
    getBackgroundJob: vi.fn(async (id: string) => ({
      job_id: id,
      running: false,
      status: 'success',
    })),
    getLookaheadResult: vi.fn(async () => ({
      status: 'ended',
      running: false,
      status_msg: '',
      result: { has_bias: false },
    })),
    getRecursiveResult: vi.fn(async () => ({
      status: 'ended',
      running: false,
      status_msg: '',
      result: { recommended: 100 },
    })),
    setBacktestNotes: vi.fn(async () => history('新备注')),
  };
}

describe('历史研究任务合同与隔离', () => {
  it('损坏状态、历史和错任务身份不能变为真实空结果', () => {
    expect(() => parseStatus({ status: 'running' })).toThrow();
    expect(() => parseHistory({ history: [] })).toThrow();
    expect(() =>
      parseJob({ job_id: 'other', running: false, status: 'ended' }, 'asked'),
    ).toThrow();
    expect(parseHistory([])).toEqual([]);
  });
  it('分析结果必须是已完成且包含分析数据的响应，不接纳running或失败封套', () => {
    expect(
      parseAnalysisResult(
        { status: 'running', running: true, result: { has_bias: false } },
        'lookahead',
      ),
    ).toBeNull();
    expect(
      parseAnalysisResult(
        { status: 'failed', running: false, result: { has_bias: false } },
        'lookahead',
      ),
    ).toBeNull();
    expect(
      parseAnalysisResult(
        {
          status: 'ended',
          running: false,
          status_msg: '',
          result: { has_bias: false },
        },
        'lookahead',
      ),
    ).toEqual({ has_bias: false });
    expect(
      parseAnalysisResult(
        {
          status: 'error',
          running: false,
          status_msg: 'failed',
          result: { has_bias: false },
        },
        'lookahead',
      ),
    ).toBeNull();
    expect(
      parseAnalysisResult(
        { status: 'ended', running: true, result: { has_bias: false } },
        'lookahead',
      ),
    ).toBeNull();
    expect(parseAnalysisResult({ arbitrary: true }, 'lookahead')).toBeNull();
  });
  it('首次读取只查询，失败刷新保留各来源旧快照且不暴露内部错误', async () => {
    const calls = api();
    const q = createTaskQueue(calls);
    await q.refresh();
    expect(q.state.history).toHaveLength(1);
    expect(calls.postBacktest).not.toHaveBeenCalled();
    expect(calls.abortBacktest).not.toHaveBeenCalled();
    calls.getBacktestHistory.mockRejectedValueOnce(
      new Error('postgresql://u:secret@private/db'),
    );
    await q.refresh();
    expect(q.state.history[0]?.notes).toBe('保留证据');
    expect(q.state.errors.history).toContain('保留');
    expect(JSON.stringify(q.state)).not.toContain('secret');
    q.dispose();
  });
  it('两次刷新迟到响应不能替换最新历史和状态', async () => {
    const calls = api();
    let resolve!: (v: ReturnType<typeof history>) => void;
    calls.getBacktestHistory.mockImplementationOnce(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    const q = createTaskQueue(calls);
    const old = q.refresh();
    await q.refresh();
    resolve(history('旧响应'));
    await old;
    expect(q.state.history[0]?.notes).toBe('保留证据');
    q.dispose();
  });
  it('重复点击只提交一次，提交后的旧只读状态不能覆盖新命令', async () => {
    const calls = api();
    let resolveStatus!: (v: ReturnType<typeof status>) => void;
    let resolvePost!: (v: ReturnType<typeof status>) => void;
    calls.getBacktestStatus.mockImplementationOnce(
      () =>
        new Promise((r) => {
          resolveStatus = r;
        }),
    );
    calls.postBacktest.mockImplementationOnce(
      () =>
        new Promise((r) => {
          resolvePost = r;
        }),
    );
    const q = createTaskQueue(calls);
    const refresh = q.refresh();
    const first = q.startBacktest({
      strategy: 'StrategyA',
      enable_protections: false,
    });
    await q.startBacktest({ strategy: 'StrategyA', enable_protections: false });
    resolvePost(status(true));
    await first;
    resolveStatus(status());
    await refresh;
    expect(calls.postBacktest).toHaveBeenCalledTimes(1);
    expect(q.state.backtest?.running).toBe(true);
    q.dispose();
  });
  it('中止是显式命令且ACK后仍读取事实，不能直接说已中止', async () => {
    const calls = api();
    calls.abortBacktest.mockResolvedValueOnce(status(true));
    const q = createTaskQueue(calls);
    await q.abort();
    expect(calls.abortBacktest).toHaveBeenCalledTimes(1);
    expect(q.state.backtest?.running).toBe(true);
    expect(q.state.outcome).toContain('已请求');
    expect(q.state.outcome).not.toContain('已中止');
    q.dispose();
  });
  it('下载保留erase等原参数，错身份结果未知且不自动再次POST', async () => {
    const calls = api();
    calls.getBackgroundJob.mockResolvedValueOnce({
      job_id: 'other',
      running: false,
      status: 'ended',
    });
    const q = createTaskQueue(calls);
    const payload = {
      pairs: ['BTC/USDT:USDT'],
      erase: true,
      prepend_data: true,
      download_trades: true,
      days: 30,
    };
    await q.startJob('download', payload);
    await q.poll();
    expect(calls.postDownloadData).toHaveBeenCalledWith(payload);
    expect(q.state.errors.download).toContain('无法确认');
    expect(calls.postDownloadData).toHaveBeenCalledTimes(1);
    q.dispose();
  });
  it('POST回执丢失后将动作锁跨刷新持久化，直到用户核对后明确解除', async () => {
    sessionStorage.clear();
    const calls = api();
    calls.postDownloadData.mockRejectedValueOnce(new Error('response lost'));
    const q = createTaskQueue(calls);
    await q.startJob('download', { pairs: ['BTC/USDT:USDT'], erase: true });
    expect(q.state.commandsUnknown.download).toBe(true);
    await q.startJob('download', { pairs: ['BTC/USDT:USDT'], erase: true });
    expect(calls.postDownloadData).toHaveBeenCalledTimes(1);
    q.dispose();
    const restored = createTaskQueue(calls);
    expect(restored.state.commandsUnknown.download).toBe(true);
    await restored.confirmUnknown('download', {
      reviewed: true,
      phrase: '后台里没有看见',
    });
    expect(restored.state.commandsUnknown.download).toBe(true);
    await restored.confirmUnknown('download', {
      reviewed: false,
      phrase: '接受重复执行风险',
    });
    expect(restored.state.commandsUnknown.download).toBe(true);
    await restored.confirmUnknown('download', {
      reviewed: true,
      phrase: '接受重复执行风险',
    });
    await restored.startJob('download', {
      pairs: ['BTC/USDT:USDT'],
      erase: true,
    });
    expect(calls.postDownloadData).toHaveBeenCalledTimes(2);
    restored.dispose();
    sessionStorage.clear();
  });
  it('分析结果只读当前精确job，失败新分析保留前一份结果来源', async () => {
    const calls = api();
    const q = createTaskQueue(calls);
    await q.startJob('lookahead', { strategy: 'StrategyA' });
    await q.poll();
    expect(q.state.results.lookahead?.jobId).toBe('job-a');
    calls.postLookaheadAnalysis.mockRejectedValueOnce(new Error('secret'));
    await q.startJob('lookahead', { strategy: 'StrategyB' });
    expect(q.state.results.lookahead?.jobId).toBe('job-a');
    expect(q.state.errors.lookahead).toContain('结果未知');
    q.dispose();
  });
  it('回测、下载、前瞻和递归的丢失回执都持久锁定重发', async () => {
    sessionStorage.clear();
    const calls = api();
    calls.postBacktest.mockRejectedValueOnce(new Error('lost'));
    calls.postDownloadData.mockRejectedValueOnce(new Error('lost'));
    calls.postLookaheadAnalysis.mockRejectedValueOnce(new Error('lost'));
    calls.postRecursiveAnalysis.mockRejectedValueOnce(new Error('lost'));
    const q = createTaskQueue(calls);
    await q.startBacktest({ strategy: 'StrategyA' });
    await q.startJob('download', { pairs: ['BTC/USDT:USDT'] });
    await q.startJob('lookahead', { strategy: 'StrategyA' });
    await q.startJob('recursive', { strategy: 'StrategyA' });
    expect(q.state.commandsUnknown).toEqual({
      backtest: true,
      download: true,
      lookahead: true,
      recursive: true,
    });
    const restored = createTaskQueue(calls);
    expect(restored.state.commandsUnknown).toEqual({
      backtest: true,
      download: true,
      lookahead: true,
      recursive: true,
    });
    await restored.startJob('lookahead', { strategy: 'StrategyA' });
    expect(calls.postLookaheadAnalysis).toHaveBeenCalledTimes(1);
    q.dispose();
    restored.dispose();
    sessionStorage.clear();
  });
  it('备注带精确filename与strategy，响应损坏保留历史不冒充保存成功', async () => {
    const calls = api();
    const q = createTaskQueue(calls);
    await q.refresh();
    await q.saveNotes(history()[0]!, '新备注');
    expect(calls.setBacktestNotes).toHaveBeenCalledWith(
      'result-a.json',
      'StrategyA',
      '新备注',
    );
    expect(q.state.history[0]?.notes).toBe('新备注');
    calls.setBacktestNotes.mockResolvedValueOnce({} as never);
    await q.saveNotes(history()[0]!, '再修改');
    expect(q.state.history[0]?.notes).toBe('新备注');
    expect(q.state.outcome).not.toBe('备注已保存');
    q.dispose();
  });
  it('dispose后迟到的请求不会写状态或启动轮询', async () => {
    const calls = api();
    let resolve!: (v: ReturnType<typeof status>) => void;
    calls.postBacktest.mockImplementationOnce(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    const q = createTaskQueue(calls);
    const p = q.startBacktest({ strategy: 'StrategyA' });
    q.dispose();
    resolve(status(true));
    await p;
    expect(q.state.backtest).toBeNull();
  });
  it('pending加running false继续等待，只有ended终态才读取分析结果', async () => {
    const calls = api();
    const q = createTaskQueue(calls);
    await q.startJob('lookahead', { strategy: 'StrategyA' });
    calls.getBackgroundJob.mockResolvedValueOnce({
      job_id: 'job-a',
      running: false,
      status: 'pending',
    });
    await q.poll();
    expect(calls.getLookaheadResult).not.toHaveBeenCalled();
    expect(q.needsPoll()).toBe(true);
    await q.poll();
    expect(calls.getLookaheadResult).toHaveBeenCalledWith('job-a');
    q.dispose();
  });
  it('HTTP成功但任务状态error按失败显示且不读取伪造结果', async () => {
    const calls = api();
    const q = createTaskQueue(calls);
    await q.startJob('lookahead', { strategy: 'StrategyA' });
    calls.getBackgroundJob.mockResolvedValueOnce({
      job_id: 'job-a',
      running: false,
      status: 'error',
      error: 'private',
    } as any);
    await q.poll();
    expect(calls.getLookaheadResult).not.toHaveBeenCalled();
    expect(q.state.errors.lookahead).toContain('失败');
    expect(q.needsPoll()).toBe(false);
    q.dispose();
  });
  it('中止stopping假false仍轮询，reset running不能声称清空成功', async () => {
    const calls = api();
    calls.abortBacktest.mockResolvedValueOnce({
      ...status(),
      status: 'stopping',
    });
    const q = createTaskQueue(calls);
    await q.abort();
    expect(q.needsPoll()).toBe(true);
    calls.resetBacktest.mockResolvedValueOnce(status(true));
    await q.reset();
    expect(q.state.outcome).toContain('未清空');
    q.dispose();
  });
  it('命令进行中的手动刷新不能废弃POST响应或关闭实际运行任务轮询', async () => {
    const calls = api();
    let resolve!: (v: ReturnType<typeof status>) => void;
    calls.postBacktest.mockImplementationOnce(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    const q = createTaskQueue(calls);
    const command = q.startBacktest({ strategy: 'StrategyA' });
    await q.refresh();
    resolve(status(true));
    await command;
    expect(q.state.backtest?.running).toBe(true);
    expect(q.needsPoll()).toBe(true);
    q.dispose();
  });
  it('状态或最终结果暂时失败后刷新只重读同一job，不重复提交', async () => {
    const calls = api();
    const q = createTaskQueue(calls);
    await q.startJob('lookahead', { strategy: 'StrategyA' });
    calls.getBackgroundJob.mockRejectedValueOnce(new Error('offline'));
    await q.poll();
    await q.refresh();
    await q.poll();
    expect(q.state.results.lookahead?.jobId).toBe('job-a');
    expect(calls.postLookaheadAnalysis).toHaveBeenCalledTimes(1);
    calls.postRecursiveAnalysis.mockResolvedValueOnce({
      job_id: 'recursive-a',
      status: 'started',
    });
    calls.getRecursiveResult.mockRejectedValueOnce(new Error('offline'));
    await q.startJob('recursive', { strategy: 'StrategyA' });
    await q.poll();
    expect(q.state.results.recursive).toBeUndefined();
    await q.refresh();
    await q.poll();
    expect(q.state.results.recursive?.jobId).toBe('recursive-a');
    expect(calls.postRecursiveAnalysis).toHaveBeenCalledTimes(1);
    q.dispose();
  });
  it('页面重建后恢复精确分析job与最后成功结果，刷新只轮询该job', async () => {
    sessionStorage.clear();
    const calls = api();
    const first = createTaskQueue(calls);
    await first.startJob('lookahead', { strategy: 'StrategyA' });
    await first.poll();
    expect(first.state.results.lookahead?.jobId).toBe('job-a');
    first.dispose();
    const restored = createTaskQueue(calls);
    expect(restored.state.jobs.lookahead?.job_id).toBe('job-a');
    expect(restored.state.results.lookahead?.jobId).toBe('job-a');
    calls.getBackgroundJob.mockResolvedValueOnce({
      job_id: 'job-a',
      running: false,
      status: 'success',
    });
    calls.getLookaheadResult.mockRejectedValueOnce(new Error('offline'));
    await restored.poll();
    expect(restored.state.results.lookahead?.data).toEqual({ has_bias: false });
    expect(calls.postLookaheadAnalysis).toHaveBeenCalledTimes(1);
    restored.dispose();
    sessionStorage.clear();
  });
  it('分析POST一旦返回job_id立即持久化，刷新时从pending继续轮询', async () => {
    sessionStorage.clear();
    const calls = api();
    const first = createTaskQueue(calls);
    await first.startJob('recursive', { strategy: 'StrategyA' });
    first.dispose();
    const restored = createTaskQueue(calls);
    expect(restored.state.jobs.recursive?.job_id).toBe('job-a');
    expect(restored.needsPoll()).toBe(true);
    restored.dispose();
    sessionStorage.clear();
  });
});

import type { App } from 'vue';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick } from 'vue';
import { createMemoryHistory, createRouter, RouterView } from 'vue-router';

const api = vi.hoisted(() => ({
  list: vi.fn(),
  detail: vi.fn(),
  run: vi.fn(),
  job: vi.fn(),
  lookahead: vi.fn(),
  recursive: vi.fn(),
}));
vi.mock('#/api/freqtrade', () => ({
  getBacktestList: api.list,
  getBacktestDetail: api.detail,
  getIterationRun: api.run,
  getBackgroundJob: api.job,
  getLookaheadResult: api.lookahead,
  getRecursiveResult: api.recursive,
  modelLabel: (value: string) => value,
  EXIT_LABELS: { roi: '收益目标' },
}));
vi.mock('@vben/plugins/echarts', () => ({
  EchartsUI: { render: () => h('div') },
  useEcharts: () => ({ renderEcharts: vi.fn() }),
}));

import Workspace from '../index.vue';

const mounts: { app: App; element: HTMLElement }[] = [];
const listing = () => ({
  items: [
    { identifier: 'btc-model', strategy: 'Trend', summary: { profit_abs: 12 } },
    { identifier: 'eth-model', strategy: 'Trend', summary: { profit_abs: -2 } },
  ],
});
const detail = (identifier: string, profit = 12) => ({
  identifier,
  strategy: 'Trend',
  summary: {
    profit_abs: profit,
    profit_pct: 1.2,
    trades: 2,
    starting_balance: 1000,
    final_balance: 1012,
  },
  equity: [
    ['2026-01-01', 1000],
    ['2026-01-02', 1012],
  ],
  thresholds: { entry: 0.2, exit: -0.1 },
  by_pair: [{ pair: 'ETH/USDT', trades: 2, wins: 1, profit_abs: profit }],
  by_exit: [{ reason: 'roi', trades: 2, profit_abs: profit }],
  trades: [
    {
      pair: 'ETH/USDT',
      open_date: '2026-01-01T00:00:00Z',
      close_date: '2026-01-01T01:00:00Z',
      open_rate: 100,
      close_rate: 102,
      stake_amount: 100,
      profit_abs: profit,
      profit_pct: 2,
      is_short: false,
      side: '多',
      exit_reason: 'roi',
      model_pred_pct: 0.3,
      decision: '预测高于入场阈值',
      duration_h: 1,
    },
  ],
});
async function flush() {
  for (let i = 0; i < 7; i += 1) await Promise.resolve();
  await nextTick();
}
async function open(path: string) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/quant/detail', component: Workspace },
      { path: '/quant/experiments', component: { render: () => h('div') } },
    ],
  });
  await router.push(path);
  await router.isReady();
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({ render: () => h(RouterView) });
  app.use(router).mount(element);
  mounts.push({ app, element });
  await flush();
  return { element, router };
}
beforeEach(() => {
  vi.resetAllMocks();
  api.list.mockResolvedValue(listing());
  api.detail.mockImplementation(async (id: string) => detail(id));
});
afterEach(() => {
  for (const item of mounts.splice(0)) {
    item.app.unmount();
    item.element.remove();
  }
});

describe('历史产物明细的精确定位', () => {
  it('按 identifier 精确定位，而不是默认 BTC 或最近产物', async () => {
    const { element } = await open('/quant/detail?identifier=eth-model');
    expect(api.detail.mock.calls.map((call) => call[0])).toEqual(['eth-model']);
    expect(element.textContent).toContain('eth-model');
    expect(element.textContent).toContain('其它成本未齐');
    expect(element.querySelector('[data-testid=equity-chart]')).not.toBeNull();
    expect(
      element.querySelector('[data-testid=prediction-chart]'),
    ).not.toBeNull();
  });

  it('缺少指定 identifier 时保留原目标并说明缺记录，不读取另一产物', async () => {
    api.detail.mockRejectedValue(
      new Error('private postgresql://u:secret@host/db'),
    );
    const { element } = await open('/quant/detail?identifier=missing-model');
    expect(api.detail.mock.calls.map((call) => call[0])).toEqual([
      'missing-model',
    ]);
    expect(element.textContent).toContain('missing-model');
    expect(element.textContent).toContain('指定产物无法读取');
    expect(element.textContent).not.toContain('secret');
  });

  it.each([
    'identifier=eth-model&identifier=btc-model',
    'identifier=',
    'identifier=..%2Fsecret',
    'run_id=run-a&job_id=job-a',
    'identifier=eth-model&source=other',
    'identifier=%20eth-model%20',
  ])('拒绝含糊或非法目标 %s', async (query) => {
    const { element } = await open('/quant/detail?' + query);
    expect(element.textContent).toContain('目标参数无效');
    expect(api.detail).not.toHaveBeenCalled();
    expect(api.run).not.toHaveBeenCalled();
    expect(api.job).not.toHaveBeenCalled();
  });

  it('没有目标时要求选择，不自动读取默认产物', async () => {
    const { element } = await open('/quant/detail');
    expect(api.detail).not.toHaveBeenCalled();
    expect(element.textContent).toContain('选择一份历史产物');
  });

  it('返回的 identifier 与请求不同则拒绝展示', async () => {
    api.detail.mockResolvedValue(detail('btc-model', 999));
    const { element } = await open('/quant/detail?identifier=eth-model');
    expect(element.textContent).toContain('返回产物身份不匹配');
    expect(element.querySelector('[data-testid=trade-table]')).toBeNull();
  });

  it('精确读取 run_id，未提供映射时不猜测模型产物', async () => {
    api.run.mockResolvedValue({
      run_id: 'run-a',
      execution_status: 'succeeded',
      research_verdict: 'rejected',
      strategy: 'Trend',
      metrics: { profit_abs: 1 },
    });
    const { element } = await open('/quant/detail?run_id=run-a');
    expect(api.run.mock.calls[0]?.[0]).toBe('run-a');
    expect(api.detail).not.toHaveBeenCalled();
    expect(element.textContent).toContain('执行成功');
    expect(element.textContent).toContain('研究拒绝');
    expect(element.textContent).toContain('没有可核验的回测产物映射');
  });

  it('后台任务按 job_id 精确定位，结果读取保持同一身份', async () => {
    api.job.mockResolvedValue({
      job_id: 'job-a',
      running: false,
      status: 'success',
      job_category: 'lookahead_analysis',
    });
    api.lookahead.mockResolvedValue({ job_id: 'job-a', bias: false });
    const { element } = await open('/quant/detail?job_id=job-a');
    expect(api.job).toHaveBeenCalledWith('job-a');
    expect(api.lookahead).toHaveBeenCalledWith('job-a');
    expect(api.detail).not.toHaveBeenCalled();
    expect(element.textContent).toContain('job-a');
    expect(element.textContent).toContain('没有因任务完成而授予通过');
  });

  it('切换目标后迟到的旧响应不能覆盖当前产物，移除 query 恢复待选择', async () => {
    let resolveOld!: (value: unknown) => void;
    api.detail.mockImplementation((id: string) =>
      id === 'btc-model'
        ? new Promise((resolve) => {
            resolveOld = resolve;
          })
        : Promise.resolve(detail(id, 22)),
    );
    const { element, router } = await open(
      '/quant/detail?identifier=btc-model',
    );
    await router.push('/quant/detail?identifier=eth-model');
    await flush();
    resolveOld(detail('btc-model', 999));
    await flush();
    expect(
      element.querySelector('[data-testid=selected-identity]')?.textContent,
    ).toContain('eth-model');
    expect(
      element.querySelector('[data-testid=trade-table]')?.textContent,
    ).not.toContain('999');
    await router.push('/quant/detail');
    await flush();
    expect(element.querySelector('[data-testid=trade-table]')).toBeNull();
    expect(element.textContent).toContain('选择一份历史产物');
  });

  it('刷新失败保留同一产物的上次成功证据并标旧快照', async () => {
    const { element } = await open('/quant/detail?identifier=eth-model');
    api.detail.mockRejectedValue(new Error('network secret'));
    (
      element.querySelector('[data-testid=refresh-detail]') as HTMLButtonElement
    ).click();
    await flush();
    expect(element.querySelector('[data-testid=trade-table]')).not.toBeNull();
    expect(element.textContent).toContain('保留该目标上次成功快照');
    expect(element.textContent).not.toContain('network secret');
  });

  it('同一运行刷新失败仍保留精确运行证据并标旧快照', async () => {
    api.run.mockResolvedValue({
      run_id: 'run-a',
      execution_status: 'succeeded',
      research_verdict: 'rejected',
    });
    const { element } = await open('/quant/detail?run_id=run-a');
    api.run.mockRejectedValue(new Error('network down'));
    (
      element.querySelector('[data-testid=refresh-detail]') as HTMLButtonElement
    ).click();
    await flush();
    expect(element.textContent).toContain('run-a');
    expect(element.textContent).toContain('旧快照');
    expect(element.textContent).toContain('保留该目标上次成功快照');
  });

  it('损坏的目录刷新保留之前的真实目录并区分错误状态', async () => {
    const { element } = await open('/quant/detail?identifier=eth-model');
    api.list.mockResolvedValue({ items: null });
    (
      element.querySelector('[data-testid=refresh-detail]') as HTMLButtonElement
    ).click();
    await flush();
    expect(element.textContent).toContain('btc-model');
    expect(element.textContent).toContain('eth-model');
    expect(element.textContent).toContain('保留上次成功快照');
  });

  it('未识别的后台任务类型不会显示为已完成研究结果', async () => {
    api.job.mockResolvedValue({
      job_id: 'job-a',
      running: false,
      status: 'success',
      job_category: 'download_data',
    });
    const { element } = await open('/quant/detail?job_id=job-a');
    expect(element.textContent).toContain('指定运行或后台任务无法读取');
    expect(api.lookahead).not.toHaveBeenCalled();
    expect(api.recursive).not.toHaveBeenCalled();
    expect(element.textContent).not.toContain('没有因任务完成而授予通过');
  });

  it('后台分析执行失败或结果未知时不读取结果端点', async () => {
    api.job.mockResolvedValue({
      job_id: 'job-a',
      running: false,
      status: 'failed',
      error: 'private failure detail',
      job_category: 'lookahead_analysis',
    });
    const { element } = await open('/quant/detail?job_id=job-a');
    expect(api.lookahead).not.toHaveBeenCalled();
    expect(api.recursive).not.toHaveBeenCalled();
    expect(element.textContent).not.toContain('private failure detail');
    expect(element.textContent).toContain('执行失败');
  });

  it('跨目标切换不把上一个运行或后台结果冒充为当前目标', async () => {
    api.run.mockResolvedValue({
      run_id: 'run-a',
      execution_status: 'succeeded',
    });
    const { element, router } = await open('/quant/detail?run_id=run-a');
    expect(element.textContent).toContain('run-a');
    api.job.mockResolvedValue({
      job_id: 'job-a',
      running: true,
      status: 'running',
      job_category: 'lookahead_analysis',
    });
    await router.push('/quant/detail?job_id=job-a');
    await flush();
    expect(
      element.querySelector('[data-testid=selected-identity]')?.textContent,
    ).toContain('job-a');
    expect(element.textContent).not.toContain('run-a');
    expect(element.textContent).toContain('后台任务仍在运行');
  });
});

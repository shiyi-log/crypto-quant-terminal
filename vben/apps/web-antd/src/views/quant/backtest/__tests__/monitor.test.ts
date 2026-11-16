import type { App } from 'vue';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick } from 'vue';

const api = vi.hoisted(() => ({
  web: vi.fn(),
  research: vi.fn(),
  file: vi.fn(),
}));
vi.mock('#/api/freqtrade', () => ({
  FT_SERVER_ORIGIN: '',
  FT_TOKEN_KEY: 'test-ft-token',
  EXIT_LABELS: { roi: '止盈' },
  getBacktestStatus: api.web,
  getResearchProgress: api.research,
}));
vi.mock('#/views/quant/tasks/index.vue', async () => {
  const { h } = await import('vue');
  return {
    __esModule: true,
    default: { render: () => h('p', { 'data-task-page': '' }, '研究任务入口') },
  };
});
vi.mock('#/views/quant/iteration/index.vue', async () => {
  const { h } = await import('vue');
  return {
    __esModule: true,
    default: {
      render: () => h('p', { 'data-iteration-page': '' }, '模型迭代入口'),
    },
  };
});
const wrappers: Array<{ app: App; element: HTMLDivElement }> = [];
const script = (model = 'script-a') => ({
  status: 'done',
  phase: '完成',
  percent: 100,
  model,
  timerange: '20250101-20250201',
  result: {
    strategy: model,
    profit_abs: '12.3400',
    profit_pct: 2,
    trades: 5,
    pairs: [
      { pair: 'BTC/USDT', trades: 5, profit_abs: '12.3400', profit_pct: 2 },
    ],
    exits: [{ reason: 'roi', trades: 5, profit_abs: '12.3400' }],
  },
});
const research = () => ({
  progress: {
    status: 'done',
    run_id: 'research-a',
    percent: 100,
    result: { passed: true, n_trials: 1 },
  },
  trials: {
    total: 1,
    runs: 1,
    recent: [
      {
        config: { kind: 'gru', seq_len: 8 },
        t_period: 0,
        ic_period: null,
        pos_windows: 0,
        n_windows: 3,
        phase: 'done',
      },
    ],
  },
});
beforeEach(() => {
  localStorage.removeItem('test-ft-token');
  vi.stubGlobal('fetch', api.file);
  api.file.mockImplementation(
    async () => new Response(JSON.stringify(script()), { status: 200 }),
  );
  api.research.mockResolvedValue(research());
  api.web.mockResolvedValue({
    running: false,
    progress: 0,
    status: 'idle',
    status_msg: '',
    step: '',
  });
});
afterEach(() => {
  wrappers.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  });
  vi.resetAllMocks();
  vi.unstubAllGlobals();
});
async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await nextTick();
}
async function page() {
  const { default: Component } = await import('../index.vue');
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp(Component);
  app.mount(element);
  wrappers.push({ app, element });
  await settle();
  const click = async (selector: string) => {
    const item = element.querySelector<HTMLElement>(selector);
    if (!item) throw new Error('Missing rendered control: ' + selector);
    item.click();
    await settle();
  };
  return { app, element, click };
}
describe('回测来源监视', () => {
  it('保留三分页但只挂载当前页；事实、研究结论与成本边界分别显示', async () => {
    const { element, click } = await page();
    expect(element.querySelector('[data-task-page]')).toBeNull();
    expect(element.querySelector('[data-iteration-page]')).toBeNull();
    expect(element.textContent).toContain('12.3400');
    expect(element.textContent).toContain('成本完整性未知');
    expect(element.textContent).toContain('执行完成不等于研究通过');
    expect(api.web).not.toHaveBeenCalled();
    await click('[data-source=research]');
    expect(element.querySelector('[data-inspector]')?.textContent).toContain(
      'research-a',
    );
    expect(element.querySelector('[data-inspector]')?.textContent).toContain(
      '旧判据达标',
    );
    expect(element.querySelector('[data-inspector]')?.textContent).toContain(
      '0 / 3',
    );
    await click('[data-tab=tasks]');
    expect(element.querySelector('[data-task-page]')).not.toBeNull();
    await click('[data-tab=iter]');
    expect(element.querySelector('[data-task-page]')).toBeNull();
    expect(element.querySelector('[data-iteration-page]')).not.toBeNull();
  });
  it('只有确认进度文件不存在才回落研究实例，并正确换算比率', async () => {
    api.file.mockResolvedValue(new Response('', { status: 404 }));
    api.web.mockResolvedValue({
      running: false,
      progress: 1,
      status: 'ended',
      status_msg: '',
      step: '',
      backtest_result: {
        strategy: {
          S: {
            profit_total_abs: 2,
            profit_total: 0.1234,
            winrate: 0,
            total_trades: 1,
          },
        },
      },
    });
    const { element } = await page();
    expect(element.textContent).toContain('研究实例回测');
    expect(element.textContent).toContain('12.34');
    expect(element.textContent).toContain('已确认无脚本进度文件');
  });
  it('文件读取失败不会伪装成不存在或切换来源，保留已有事实且错误脱敏', async () => {
    const { element, click } = await page();
    api.file.mockRejectedValue(new Error('postgresql://u:secret@private/db'));
    await click('[data-refresh]');
    expect(element.querySelector('[role=alert]')?.textContent).toContain(
      '保留',
    );
    expect(element.textContent).toContain('script-a');
    expect(element.textContent).not.toContain('secret');
    expect(api.web).not.toHaveBeenCalled();
  });
  it('合同坏与真实无研究进度分开，失败保留上次 trials', async () => {
    const { element, click } = await page();
    api.research.mockResolvedValue({ progress: null, trials: null });
    await click('[data-refresh]');
    await click('[data-source=research]');
    expect(element.querySelector('[role=alert]')?.textContent).toContain(
      '合同',
    );
    expect(element.textContent).toContain('research-a');
    api.research.mockResolvedValue({
      progress: null,
      trials: { total: 0, runs: 0, recent: [] },
    });
    await click('[data-refresh]');
    expect(element.textContent).toContain('当前没有已记录的模型迭代任务');
  });
  it('当前研究进度为空时仍显示已有历史 trial 账本', async () => {
    api.research.mockResolvedValue({
      progress: null,
      trials: research().trials,
    });
    const { element, click } = await page();
    await click('[data-source=research]');
    expect(element.textContent).toContain('暂无当前运行进度');
    expect(element.textContent).toContain('gru');
    expect(element.textContent).toContain('8');
    expect(element.textContent).toContain('账本累计 1 条 trial / 1 轮');
  });
  it('手动刷新丢弃迟到的旧文件与旧研究响应', async () => {
    let resolveOld!: (value: Response) => void;
    api.file.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    );
    const { element, click } = await page();
    api.file.mockResolvedValue(
      new Response(JSON.stringify(script('new-run')), { status: 200 }),
    );
    await click('[data-refresh]');
    resolveOld(
      new Response(JSON.stringify(script('old-run')), { status: 200 }),
    );
    await settle();
    expect(element.querySelector('[data-inspector]')?.textContent).toContain(
      'new-run',
    );
    expect(
      element.querySelector('[data-inspector]')?.textContent,
    ).not.toContain('old-run');
  });
  it('非有限数值与未知执行状态不能伪装成有效事实，原始错误也不泄漏凭据', async () => {
    api.file.mockImplementation(
      async () =>
        new Response(
          JSON.stringify({
            ...script(),
            status: 'toString',
            result: {
              profit_abs: null,
              error: 'postgresql://u:secret@private/db',
            },
          }),
          { status: 200 },
        ),
    );
    const { element } = await page();
    expect(element.querySelector('[data-inspector]')?.textContent).toContain(
      '状态未知',
    );
    expect(element.textContent).not.toContain('secret');
    expect(element.textContent).not.toContain('private');
  });
  it('卸载中断静态请求并停止所有监视轮询', async () => {
    let signal: AbortSignal | undefined;
    api.file.mockImplementation((_url: string, options: RequestInit) => {
      signal = options.signal as AbortSignal;
      return new Promise(() => {});
    });
    const { app } = await page();
    expect(signal?.aborted).toBe(false);
    app.unmount();
    wrappers.splice(0).forEach(({ element }) => element.remove());
    expect(signal?.aborted).toBe(true);
  });
  it('登录身份改变清除前一身份旧事实，新请求失败也不能保留前人快照', async () => {
    localStorage.setItem('test-ft-token', 'session-a');
    const { element, click } = await page();
    expect(element.textContent).toContain('script-a');
    localStorage.setItem('test-ft-token', 'session-b');
    api.file.mockRejectedValue(new Error('offline'));
    api.research.mockRejectedValue(new Error('offline'));
    await click('[data-refresh]');
    expect(element.textContent).not.toContain('script-a');
    expect(element.textContent).not.toContain('research-a');
    expect(element.textContent).toContain('尚无成功快照');
  });
  it('研究实例错误与停止请求保持不同状态，私密错误文本不透出', async () => {
    api.file.mockResolvedValue(new Response('', { status: 404 }));
    api.web.mockResolvedValue({
      running: false,
      status: 'error',
      status_msg: 'postgres://u:secret@private/db',
      progress: 0,
    });
    const { element } = await page();
    expect(element.textContent).toContain('执行失败');
    expect(element.textContent).not.toContain('secret');
    expect(element.textContent).not.toContain('private');
    api.web.mockResolvedValue({
      running: false,
      status: 'stopping',
      status_msg: 'stop request accepted',
      progress: 0,
    });
    const refresh = element.querySelector<HTMLElement>('[data-refresh]');
    refresh?.click();
    await settle();
    expect(element.querySelector('[data-inspector]')?.textContent).toContain(
      '等待停止确认',
    );
  });
  it('模型进度非空时必须有已知执行状态，损坏合同保留之前账本', async () => {
    const { element, click } = await page();
    api.research.mockResolvedValue({
      progress: { phase: 'future-shape' },
      trials: research().trials,
    });
    await click('[data-refresh]');
    await click('[data-source=research]');
    expect(element.querySelector('[role=alert]')?.textContent).toContain(
      '合同不完整',
    );
    expect(element.querySelector('[data-inspector]')?.textContent).toContain(
      'research-a',
    );
  });
});

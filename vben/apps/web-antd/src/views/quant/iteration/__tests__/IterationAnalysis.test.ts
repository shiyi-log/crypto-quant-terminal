import type { App } from 'vue';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick } from 'vue';

const api = vi.hoisted(() => ({
  iteration: vi.fn(),
  ml: vi.fn(),
}));
vi.mock('#/api/freqtrade', () => ({
  getIteration: api.iteration,
  getMlStatus: api.ml,
}));

import IterationAnalysis from '../IterationAnalysis.vue';

const mounted: Array<{ app: App; element: HTMLDivElement }> = [];
const summary = (improvement: string) => ({
  conclusion: { key_improvement: improvement, evidence: [], caveats: [] },
  universe: {},
  risk_curve: null,
});
const mlStatus = () => ({
  conclusion: {
    walkforward_ic_t: null,
    rules: [],
    framing: '历史记录',
    verdict: '未知',
  },
  iteration: { recent: [], passing: [], n_trials: 0, n_passing: 0 },
  seq: {},
  ic_models: [],
  regime_groups: [],
  rounds: 0,
  experiments: 0,
});
async function flush() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
  await nextTick();
}
async function mount() {
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp(IterationAnalysis);
  app.mount(element);
  mounted.push({ app, element });
  await flush();
  return element;
}
function refresh(element: Element) {
  const button = [...element.querySelectorAll('button')].find((item) =>
    item.textContent?.includes('刷新迭代数据'),
  );
  if (!button) throw new Error('analysis refresh button not found');
  (button as HTMLButtonElement).click();
}
beforeEach(() => {
  vi.resetAllMocks();
  api.iteration.mockResolvedValue(summary('已保留的历史结论'));
  api.ml.mockResolvedValue(mlStatus());
});
afterEach(() => {
  mounted.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  });
});

describe('历史走查分析读取', () => {
  it('保留已知研究快照且不显示异常文本', async () => {
    const element = await mount();
    expect(element.textContent).toContain('已保留的历史结论');
    api.iteration.mockRejectedValueOnce(
      new Error('postgresql://operator:private-secret@internal/research'),
    );
    refresh(element);
    await flush();
    expect(element.textContent).toContain('已保留的历史结论');
    expect(element.textContent).toContain('保留上一次成功快照');
    expect(element.textContent).not.toContain('private-secret');
    expect(element.textContent).not.toContain('internal/research');
  });

  it('区分首次读取失败与真实空结果', async () => {
    api.iteration.mockRejectedValueOnce(new Error('secret'));
    const element = await mount();
    expect(element.textContent).toContain('读取失败不表示记录为空');
    expect(element.textContent).not.toContain('secret');
    expect(element.textContent).not.toContain('暂无迭代汇总记录');
  });

  it('格式错误刷新保留上次成功分析快照', async () => {
    const element = await mount();
    api.iteration.mockResolvedValueOnce({ conclusion: null });
    refresh(element);
    await flush();
    expect(element.textContent).toContain('已保留的历史结论');
    expect(element.textContent).toContain('请求失败或响应格式不完整');
  });

  it('离开分析区时清除自动刷新并忽略未完成请求', async () => {
    const setIntervalSpy = vi.spyOn(globalThis, 'setInterval');
    const clearIntervalSpy = vi.spyOn(globalThis, 'clearInterval');
    let finishIteration!: (value: ReturnType<typeof summary>) => void;
    let finishMl!: (value: ReturnType<typeof mlStatus>) => void;
    api.iteration.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishIteration = resolve;
        }),
    );
    api.ml.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishMl = resolve;
        }),
    );
    const element = document.createElement('div');
    document.body.append(element);
    const app = createApp(IterationAnalysis);
    app.mount(element);
    mounted.push({ app, element });
    const timer = setIntervalSpy.mock.results.at(-1)?.value;
    expect(setIntervalSpy).toHaveBeenCalledWith(expect.any(Function), 60_000);
    app.unmount();
    expect(clearIntervalSpy).toHaveBeenCalledWith(timer);
    mounted.splice(
      mounted.findIndex((item) => item.app === app),
      1,
    );
    finishIteration(summary('迟到响应'));
    finishMl(mlStatus());
    await flush();
    expect(api.iteration).toHaveBeenCalledTimes(1);
    expect(api.ml).toHaveBeenCalledTimes(1);
    element.remove();
  });
});

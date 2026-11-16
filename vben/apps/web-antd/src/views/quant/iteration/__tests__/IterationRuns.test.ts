import type { App } from 'vue';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick } from 'vue';
import { createMemoryHistory, createRouter, RouterView } from 'vue-router';
const api = vi.hoisted(() => ({ list: vi.fn(), detail: vi.fn() }));
vi.mock('#/api/freqtrade', () => ({
  getIterationRuns: api.list,
  getIterationRun: api.detail,
}));
import Workspace from '../IterationRuns.vue';
const mounts: { app: App; el: HTMLElement }[] = [];
const run = (id: string, profit = 1) => ({
  run_id: id,
  execution_status: 'succeeded',
  research_verdict: 'rejected',
  strategy: null,
  model: null,
  timerange: null,
  metrics: { profit_pct: profit },
  gate: {
    values: { profit_pct: profit, sharpe: null },
    reasons: ['样本外亏损'],
  },
  evidence: { source: 'cache' },
});
async function flush() {
  for (let i = 0; i < 8; i++) await Promise.resolve();
  await nextTick();
}
async function open(path = '/quant/iteration') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/quant/iteration', component: Workspace },
      { path: '/quant/detail', component: { render: () => h('div') } },
    ],
  });
  await router.push(path);
  await router.isReady();
  const el = document.createElement('div');
  document.body.append(el);
  const app = createApp({ render: () => h(RouterView) });
  app.use(router).mount(el);
  mounts.push({ app, el });
  await flush();
  return { router, el };
}
beforeEach(() => {
  vi.resetAllMocks();
  api.list.mockResolvedValue({
    total: 2,
    items: [run('run-a'), run('run-b', 2)],
  });
  api.detail.mockImplementation(async (id: string) => run(id));
});
afterEach(() => {
  mounts.splice(0).forEach(({ app, el }) => {
    app.unmount();
    el.remove();
  });
});
describe('historical run queue and selected evidence', () => {
  it('selects the exact requested run independently from list order', async () => {
    const { el } = await open('/quant/iteration?run_id=run-b');
    expect(api.detail).toHaveBeenCalledWith('run-b');
    expect(
      el.querySelector('[data-testid=run-inspector]')?.textContent,
    ).toContain('run-b');
    expect(el.textContent).toContain('执行成功');
    expect(el.textContent).toContain('研究拒绝');
    expect(el.textContent).toContain('费用证据未齐');
  });
  it('does not substitute another run when requested evidence is missing', async () => {
    api.detail.mockRejectedValue(new Error('postgresql://u:secret@private/db'));
    const { el } = await open('/quant/iteration?run_id=missing');
    expect(api.detail).toHaveBeenCalledWith('missing');
    expect(el.textContent).toContain('missing');
    expect(el.textContent).toContain('指定运行证据无法读取');
    expect(el.textContent).not.toContain('secret');
  });
  it.each(['run_id=', 'run_id=run-a&run_id=run-b', 'run_id=..%2Fprivate'])(
    'rejects invalid query without reading another run: %s',
    async (query) => {
      const { el } = await open('/quant/iteration?' + query);
      expect(el.textContent).toContain('运行参数无效');
      expect(api.detail).not.toHaveBeenCalled();
    },
  );
  it('removing query clears selected evidence and late old response is ignored', async () => {
    let complete!: (v: unknown) => void;
    api.detail.mockImplementation((id: string) =>
      id === 'run-a'
        ? new Promise((r) => {
            complete = r;
          })
        : Promise.resolve(run(id, 2)),
    );
    const { el, router } = await open('/quant/iteration?run_id=run-a');
    await router.push('/quant/iteration?run_id=run-b');
    await flush();
    complete(run('run-a', 999));
    await flush();
    expect(
      el.querySelector('[data-testid=run-inspector]')?.textContent,
    ).toContain('run-b');
    expect(
      el.querySelector('[data-testid=run-inspector]')?.textContent,
    ).not.toContain('999');
    await router.push('/quant/iteration');
    await flush();
    expect(
      el.querySelector('[data-testid=run-inspector]')?.textContent,
    ).toContain('选择一轮历史运行');
  });
  it('keeps known selected evidence on a failed refresh without exposing exception', async () => {
    const { el } = await open('/quant/iteration?run_id=run-b');
    api.detail.mockRejectedValue(new Error('secret'));
    (
      el.querySelector('[data-testid=refresh-runs]') as HTMLButtonElement
    ).click();
    await flush();
    expect(
      el.querySelector('[data-testid=run-inspector]')?.textContent,
    ).toContain('run-b');
    expect(el.textContent).toContain('保留该轮上次成功快照');
    expect(el.textContent).not.toContain('secret');
  });
  it('writes exact run identity to the URL when a queue row is chosen', async () => {
    const { el, router } = await open();
    (el.querySelector('[data-run-id=run-b]') as HTMLButtonElement).click();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.query.run_id).toBe('run-b'),
    );
    await flush();
    expect(
      el.querySelector('[data-testid=run-inspector]')?.textContent,
    ).toContain('run-b');
  });
});

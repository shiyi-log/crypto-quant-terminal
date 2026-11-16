import type { App } from 'vue';

import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

const api = vi.hoisted(() => ({ list: vi.fn(), detail: vi.fn() }));
vi.mock('#/api/freqtrade', () => ({
  getIterationRuns: api.list,
  getIterationRun: api.detail,
}));
const wrappers: Array<{ app: App; element: HTMLDivElement }> = [];
afterEach(() => {
  wrappers.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  });
  vi.resetAllMocks();
});
const row = (id: string) => ({
  run_id: id,
  execution_status: 'succeeded',
  research_verdict: 'rejected',
  strategy: '研究策略',
  metrics: { profit_pct: '-1.234567890123456789' },
});
async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await nextTick();
}

async function page(path = '/quant/experiments') {
  const { default: Component } = await import('../index.vue');
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:pathMatch(.*)*', component: Component }],
  });
  await router.push(path);
  await router.isReady();
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp(Component);
  app.use(router).mount(element);
  wrappers.push({ app, element });
  await settle();
  const wrapper = {
    get(selector: string) {
      const item = element.querySelector<HTMLElement>(selector);
      if (!item) throw new Error('Expected rendered element: ' + selector);
      return {
        text: () => item.textContent ?? '',
        trigger: async (event: string) => {
          item.dispatchEvent(new Event(event, { bubbles: true }));
          await nextTick();
        },
      };
    },
    text: () => element.textContent ?? '',
    findAll: (selector: string) => [...element.querySelectorAll(selector)],
  };
  return { wrapper, router };
}

describe('实验任务页面', () => {
  it('深链按指定运行读取详情，分开展示执行和研究结果以及缺失指纹', async () => {
    api.list.mockResolvedValue({
      items: [row('run-a'), row('run-b')],
      total: 19,
    });
    api.detail.mockImplementation(async (id: string) => row(id));
    const { wrapper } = await page('/quant/experiments?run_id=run-b');
    expect(wrapper.get('[data-testid="selected-run"]').text()).toBe('run-b');
    expect(wrapper.get('.run-inspector').text()).toContain('执行成功');
    expect(wrapper.get('.run-inspector').text()).toContain('研究拒绝');
    expect(wrapper.get('.run-inspector').text()).toContain(
      '-1.234567890123456789',
    );
    expect(wrapper.text()).toContain('2 / 19');
    expect(wrapper.text()).toContain('成本指纹');
    expect(wrapper.text()).toContain('未知');
    expect(wrapper.text()).toContain('不构成上线批准');
  });

  it('任务按钮更新深链与详情，失败刷新保留原记录', async () => {
    api.list.mockResolvedValue({
      items: [row('run-a'), row('run-b')],
      total: 2,
    });
    api.detail.mockImplementation(async (id: string) => row(id));
    const { wrapper, router } = await page();
    await wrapper.get('[data-run-id="run-b"]').trigger('click');
    await settle();
    expect(router.currentRoute.value.query.run_id).toBe('run-b');
    expect(wrapper.get('[data-testid="selected-run"]').text()).toBe('run-b');
    api.list.mockRejectedValue(new Error('offline'));
    await wrapper.get('[data-testid="refresh-runs"]').trigger('click');
    await settle();
    expect(wrapper.get('[role="alert"]').text()).toContain('保留');
    expect(wrapper.findAll('[data-run-id]')).toHaveLength(2);
  });

  it('损坏合同显示失败而非没有任务，真实空记录才展示空态', async () => {
    api.list.mockResolvedValue({ items: [] });
    const { wrapper } = await page();
    expect(wrapper.get('[role="alert"]').text()).toContain('合同');
    expect(wrapper.text()).not.toContain('尚无运行记录');
    api.list.mockResolvedValue({ items: [], total: 0 });
    await wrapper.get('[data-testid="refresh-runs"]').trigger('click');
    await settle();
    expect(wrapper.text()).toContain('尚无运行记录');
  });
  it('重复的深链身份不能悄悄回退到第一轮', async () => {
    api.list.mockResolvedValue({ items: [row('run-a')], total: 1 });
    const { wrapper } = await page(
      '/quant/experiments?run_id=run-a&run_id=run-b',
    );
    expect(api.detail).not.toHaveBeenCalled();
    expect(wrapper.get('[role="alert"]').text()).toContain('身份');
    expect(wrapper.get('[data-testid="selected-run"]').text()).toBe(
      '无效运行查询',
    );
  });
  it('规范判据统计覆盖原始字段，明确未知也不能回退成旧值', async () => {
    api.list.mockResolvedValue({ items: [row('run-a')], total: 1 });
    api.detail.mockResolvedValue({
      ...row('run-a'),
      metrics: { profit_pct: 999, trade_count: 55 },
      gate: {
        values: {
          profit_pct: null,
          trades: 0,
          profit_factor: '1.234567890123456789',
          max_drawdown_pct: '3.125',
        },
      },
    });
    const { wrapper } = await page('/quant/experiments?run_id=run-a');
    expect(wrapper.get('.metric-grid').text()).toContain(
      '1.234567890123456789',
    );
    expect(wrapper.get('.metric-grid').text()).toContain('3.125');
    expect(wrapper.get('.metric-grid').text()).toContain('未知');
    expect(wrapper.get('.metric-grid').text()).not.toContain('999');
    expect(wrapper.get('.metric-grid').text()).not.toContain('55');
  });
  it('切换运行后迟到的旧详情不能出现在当前检查器', async () => {
    api.list.mockResolvedValue({
      items: [row('run-a'), row('run-b')],
      total: 2,
    });
    let resolveOld!: (value: ReturnType<typeof row>) => void;
    api.detail.mockImplementation((id: string) =>
      id === 'run-a'
        ? new Promise((resolve) => {
            resolveOld = resolve;
          })
        : Promise.resolve(row(id)),
    );
    const { wrapper } = await page('/quant/experiments?run_id=run-a');
    await wrapper.get('[data-run-id="run-b"]').trigger('click');
    await settle();
    resolveOld(row('run-a'));
    await settle();
    expect(wrapper.get('[data-testid="selected-run"]').text()).toBe('run-b');
    expect(wrapper.get('.run-inspector').text()).not.toContain('run-a');
  });
});

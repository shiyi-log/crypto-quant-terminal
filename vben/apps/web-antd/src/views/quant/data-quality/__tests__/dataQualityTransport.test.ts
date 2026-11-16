import type { App } from 'vue';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

const boundary = vi.hoisted(() => ({ get: vi.fn(), requireSession: vi.fn() }));
vi.mock('#/api/freqtrade/client', () => ({
  ftClient: { get: boundary.get },
  unwrap: (value: unknown) => value,
}));
vi.mock('#/api/session', () => ({ requireSession: boundary.requireSession }));

import DataQualityPage from '../index.vue';

const mounted: { app: App; element: HTMLDivElement }[] = [];
async function open() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: ['data-quality', 'data', 'logs'].map((path) => ({
      path: '/quant/' + path,
      component: { template: '<div />' },
    })),
  });
  await router.push('/quant/data-quality');
  await router.isReady();
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({ render: () => h(DataQualityPage) });
  app.use(router).mount(element);
  mounted.push({ app, element });
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await nextTick();
  return element;
}
beforeEach(() => {
  boundary.get.mockReset();
  boundary.requireSession.mockReset();
});
afterEach(() =>
  mounted.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  }),
);

describe('数据页真实读取边界', () => {
  it('使用已有认证客户端只读端点并传递中断信号', async () => {
    boundary.get.mockResolvedValue({
      engine: 'postgresql',
      datasets: [],
      sources: [],
    });
    const element = await open();
    expect(boundary.requireSession).toHaveBeenCalledOnce();
    expect(boundary.get).toHaveBeenCalledWith('/locals/database/status', {
      signal: expect.any(AbortSignal),
    });
    expect(element.textContent).toContain('数据源尚无清单记录');
  });
  it('会话守卫拒绝后不发送数据请求', async () => {
    boundary.requireSession.mockImplementation(() => {
      throw new Error('session_missing');
    });
    const element = await open();
    expect(boundary.get).not.toHaveBeenCalled();
    expect(element.textContent).toContain('清单尚未取得');
    expect(element.textContent).not.toContain('数据源尚无清单记录');
  });
  it('损坏原始集合进入合同错误而不是便利适配器的空数据路径', async () => {
    boundary.get.mockResolvedValue({
      engine: 'postgresql',
      datasets: null,
      sources: [],
    });
    const element = await open();
    expect(element.textContent).toContain('数据清单合同不完整');
    expect(element.textContent).not.toContain('数据源尚无清单记录');
  });
});

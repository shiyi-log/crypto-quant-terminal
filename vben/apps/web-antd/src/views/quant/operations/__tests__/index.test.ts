import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

const api = vi.hoisted(() => ({
  health: vi.fn(),
  services: vi.fn(),
  get: vi.fn(),
  requireSession: vi.fn(),
}));
vi.mock('#/api/freqtrade', () => ({ getServices: api.services }));
vi.mock('#/api/freqtrade/client', () => ({
  ftClient: { get: api.get },
  unwrap: (value: unknown) => value,
}));
vi.mock('#/api/runtime-health', () => ({ getRuntimeHealth: api.health }));
vi.mock('#/api/session', () => ({ requireSession: api.requireSession }));

import OperationsPage from '../index.vue';

const cleanups: Array<() => void> = [];
beforeEach(() => {
  vi.clearAllMocks();
  api.health.mockResolvedValue({
    generated_at: '2026-11-16T01:00:00Z',
    ok: false,
    components: [],
  });
  api.services.mockResolvedValue({
    generated_at: '2026-11-16 09:00:00',
    running: 0,
    total: 0,
    services: [],
  });
  api.get.mockResolvedValue({ total: 0, events: [] });
  api.requireSession.mockImplementation(() => undefined);
});
afterEach(() => cleanups.splice(0).forEach((cleanup) => cleanup()));
async function open() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: ['operations', 'logs', 'ops', 'research-ledger'].map((path) => ({
      path: '/quant/' + path,
      component: { template: '<div />' },
    })),
  });
  await router.push('/quant/operations?research_id=run-b');
  await router.isReady();
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp(OperationsPage);
  app.use(router).mount(element);
  cleanups.push(() => {
    app.unmount();
    element.remove();
  });
  return element;
}

describe('运维真实来源读取边界', () => {
  it('三个来源在本地无会话时均不发送读取请求', async () => {
    api.requireSession.mockImplementation(() => {
      throw Error('session missing');
    });
    const element = await open();
    await vi.waitFor(() =>
      expect(element.textContent).toContain('读取失败，尚无可用事实'),
    );
    expect(api.health).not.toHaveBeenCalled();
    expect(api.services).not.toHaveBeenCalled();
    expect(api.get).not.toHaveBeenCalled();
  });
  it('研究查询使用路由身份、限额与取消信号且保留原始响应语义', async () => {
    const element = await open();
    await vi.waitFor(() =>
      expect(element.textContent).toContain('该筛选没有研究事件'),
    );
    expect(api.health).toHaveBeenCalledOnce();
    expect(api.services).toHaveBeenCalledOnce();
    expect(api.get).toHaveBeenCalledWith('/locals/research-ledger-events', {
      params: { limit: 50, research_id: 'run-b' },
      signal: expect.any(AbortSignal),
    });
    expect(api.requireSession).toHaveBeenCalledTimes(3);
  });
});

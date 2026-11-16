import type { App } from 'vue';

import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

import OperationsWorkspace from '../OperationsWorkspace.vue';

const health = {
  generated_at: '2026-11-16T01:00:02Z',
  ok: true,
  components: [
    {
      key: 'collection',
      label: '行情采集',
      state: 'ok',
      updated_at: null,
      last_success_at: null,
      age_seconds: null,
      error_codes: [],
      metrics: {},
    },
  ],
};
const services = {
  generated_at: '2026-11-16 09:00:02',
  running: 1,
  total: 1,
  services: [
    {
      key: 'collector',
      name: '采集服务',
      running: true,
      pid: '123',
      port: null,
    },
  ],
};
const events = {
  total: 1,
  events: [
    {
      event_id: 'event-1',
      timestamp: '2026-11-16T01:00:00Z',
      event_type: 'research_started',
      research_id: 'run-1',
      payload: { sha256: 'a'.repeat(64), api_key: 'must-not-render' },
    },
  ],
};
type Loaders = {
  loadHealth: (signal: AbortSignal) => Promise<unknown>;
  loadServices: (signal: AbortSignal) => Promise<unknown>;
  loadEvents: (researchId: string, signal: AbortSignal) => Promise<unknown>;
};
const mounted: { app: App; element: HTMLDivElement }[] = [];

function open(loaders: Partial<Loaders> = {}) {
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp(OperationsWorkspace, {
    loadHealth: async () => health,
    loadServices: async () => services,
    loadEvents: async () => events,
    ...loaders,
  });
  const router = createRouter({
    history: createMemoryHistory('/#'),
    routes: [
      { path: '/', component: { template: '<div />' } },
      ...['logs', 'ops', 'research-ledger'].map((path) => ({
        path: '/quant/' + path,
        component: { template: '<div />' },
      })),
    ],
  });
  app.use(router);
  app.mount(element);
  mounted.push({ app, element });
  return {
    element,
    get(selector: string) {
      const item = element.querySelector<HTMLElement>(selector);
      if (!item) throw new Error('Expected rendered element: ' + selector);
      return item;
    },
  };
}

afterEach(() =>
  mounted.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  }),
);

describe('运维与证据工作区', () => {
  it('历史入口遵循应用的 hash 路由', async () => {
    const { element, get } = open();
    await vi.waitFor(() => expect(element.textContent).toContain('event-1'));
    expect(get('[aria-label="历史运维入口"] a').getAttribute('href')).toContain(
      '#/quant/logs',
    );
  });
  it('展示真实服务与事件身份，健康状态不改变账户门禁', async () => {
    const { element, get } = open();
    await vi.waitFor(() => expect(element.textContent).toContain('采集服务'));
    expect(element.textContent).toContain('event-1');
    expect(element.textContent).toContain('a'.repeat(64));
    expect(element.textContent).not.toContain('must-not-render');
    expect(get('[data-account-gates]').textContent).toContain(
      '尚未取得账户执行资格证据',
    );
    expect(get('[data-account-gates]').textContent).toContain('唯一写者');
    expect(
      [...element.querySelectorAll('button')].some((button) =>
        /启动交易|申请升级|恢复交易/.test(button.textContent ?? ''),
      ),
    ).toBe(false);
  });
  it('失败保留服务事实，损坏事件不显示为空记录', async () => {
    let fail = false;
    const { element, get } = open({
      loadServices: async () => {
        if (fail) throw Error('offline');
        return services;
      },
      loadEvents: async () => (fail ? {} : events),
    });
    await vi.waitFor(() => expect(element.textContent).toContain('event-1'));
    fail = true;
    get('[data-refresh-all]').click();
    await vi.waitFor(() =>
      expect(element.textContent).toContain('读取失败，保留上次成功事实'),
    );
    expect(element.textContent).toContain('采集服务');
    expect(element.textContent).toContain('event-1');
    expect(element.textContent).not.toContain('该筛选没有研究事件');
  });
  it('提交新研究筛选后旧事件不能冒充新筛选事实', async () => {
    const { element, get } = open({
      loadEvents: async (researchId) =>
        researchId === 'run-b' ? { total: 0, events: [] } : events,
    });
    await vi.waitFor(() => expect(element.textContent).toContain('event-1'));
    const input = get('input[name=research_id]') as HTMLInputElement;
    input.value = 'run-b';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await nextTick();
    get('form').dispatchEvent(
      new Event('submit', { bubbles: true, cancelable: true }),
    );
    await vi.waitFor(() =>
      expect(element.textContent).toContain('该筛选没有研究事件'),
    );
    expect(element.textContent).not.toContain('event-1');
  });
  it('筛选读取失败时标出旧事实筛选身份', async () => {
    const { element, get } = open({
      loadEvents: async (researchId) => {
        if (researchId) throw Error('offline');
        return events;
      },
    });
    await vi.waitFor(() => expect(element.textContent).toContain('event-1'));
    const input = get('input[name=research_id]') as HTMLInputElement;
    input.value = 'run-b';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await nextTick();
    get('form').dispatchEvent(
      new Event('submit', { bubbles: true, cancelable: true }),
    );
    await vi.waitFor(() =>
      expect(element.textContent).toContain('当前请求筛选：run-b'),
    );
    expect(element.textContent).toContain('保留事实筛选：全部研究');
    expect(element.textContent).toContain('event-1');
  });
  it('未知来源与明确成功的空列表分别呈现', async () => {
    const { element } = open({
      loadHealth: async () => null,
      loadServices: async () => {
        throw Error('offline');
      },
      loadEvents: async () => ({ events: [], total: 0 }),
    });
    await vi.waitFor(() =>
      expect(element.textContent).toContain('读取失败，尚无可用事实'),
    );
    expect(element.textContent).toContain('该筛选没有研究事件');
    expect(element.textContent).not.toContain('全部服务正常');
  });
});

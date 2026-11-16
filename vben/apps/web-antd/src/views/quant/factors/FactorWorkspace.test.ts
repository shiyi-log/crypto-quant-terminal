import type { App } from 'vue';

import { afterEach, describe, expect, it } from 'vitest';
import { createApp, h, nextTick, reactive } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

import FactorWorkspace from './FactorWorkspace.vue';
import { parseFactorSnapshot } from './factorSnapshot';

const mounted: { app: App; element: HTMLDivElement }[] = [];
const snapshot = () =>
  parseFactorSnapshot({
    generated_at: '2026-11-16 14:00:00',
    factors: [
      {
        factor: 'momentum_7',
        group: '动量',
        ic_base: 0.12,
        ic_recent: '-0.0400',
        t_recent: -2.3,
        alert: true,
        status: '符号反转',
      },
      {
        factor: 'volume_ratio',
        group: '成交量',
        ic_base: null,
        ic_recent: 0,
        alert: null,
      },
    ],
  });

function open(initial: {
  snapshot: ReturnType<typeof snapshot> | null;
  loading: boolean;
  error: string;
  now: number;
}) {
  const props = reactive(initial);
  const element = document.createElement('div');
  document.body.append(element);
  let refreshes = 0;
  const app = createApp({
    render: () =>
      h(FactorWorkspace, {
        ...props,
        onRefresh: () => {
          refreshes += 1;
        },
      }),
  });
  app.use(
    createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/:pathMatch(.*)*', component: { template: '<div />' } },
      ],
    }),
  );
  app.mount(element);
  mounted.push({ app, element });
  return {
    element,
    props,
    refreshes: () => refreshes,
    get<T extends HTMLElement = HTMLElement>(selector: string): T {
      const item = element.querySelector<T>(selector);
      if (!item) throw new Error('Expected element: ' + selector);
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

describe('因子实验室真实读取展示', () => {
  it('筛选只显示对应因子，检查器保留未知版本与研究边界', async () => {
    const view = open({
      snapshot: snapshot(),
      loading: false,
      error: '',
      now: Date.UTC(2026, 10, 16, 6),
    });
    expect(view.get('table').textContent).toContain('momentum_7');
    const group = view.get<HTMLSelectElement>('select[aria-label="因子分组"]');
    group.value = '成交量';
    group.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();
    expect(view.get('tbody').textContent).toContain('volume_ratio');
    expect(view.get('tbody').textContent).not.toContain('momentum_7');
    view.get('button[data-factor="volume_ratio"]').click();
    await nextTick();
    expect(view.get('[data-testid="factor-inspector"]').textContent).toContain(
      '版本身份未知',
    );
    expect(view.element.textContent).toContain('历史横截面监控');
    expect(view.element.textContent).toContain('不授予 Demo 准入');
    expect(view.get('tbody').textContent).toContain('0');
    expect(view.get('tbody').textContent).toContain('未知');
    expect(
      [...view.element.querySelectorAll('a')].map((link) =>
        link.getAttribute('href'),
      ),
    ).toEqual(['/quant/operations', '/quant/research', '/quant/experiments']);
  });
  it('失败时保留表格，刷新按钮只发读取事件', async () => {
    const view = open({
      snapshot: snapshot(),
      loading: false,
      error: '读取失败，保留上次成功快照',
      now: Date.UTC(2026, 10, 16, 8),
    });
    expect(view.get('[role="alert"]').textContent).toContain('读取失败');
    expect(view.get('tbody').textContent).toContain('momentum_7');
    expect(view.element.textContent).toContain('快照已过期');
    view.get('button[data-testid="refresh-factors"]').click();
    await nextTick();
    expect(view.refreshes()).toBe(1);
  });
  it('未读取、读取失败与成功空列表分开呈现', async () => {
    const view = open({ snapshot: null, loading: false, error: '', now: 0 });
    expect(view.element.textContent).toContain('尚未读取因子快照');
    view.props.error = '因子快照读取失败，请重试';
    await nextTick();
    expect(view.element.textContent).toContain('因子快照读取失败，请重试');
    expect(view.element.textContent).not.toContain('保留旧快照');
    view.props.snapshot = parseFactorSnapshot({ factors: [] });
    view.props.error = '';
    await nextTick();
    expect(view.element.textContent).toContain('本次成功快照没有因子记录');
  });
});

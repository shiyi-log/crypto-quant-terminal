import type { App } from 'vue';

import { afterEach, describe, expect, it } from 'vitest';
import { createApp, h, nextTick } from 'vue';

import DeskPreview from './DeskPreview.vue';

const mounted: { app: App; element: HTMLDivElement }[] = [];
function open(scenario: 'normal' | 'stale' = 'normal') {
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({ render: () => h(DeskPreview, { scenario }) });
  app.mount(element);
  mounted.push({ app, element });
  return {
    element,
    get<T extends HTMLElement = HTMLElement>(selector: string): T {
      const item = element.querySelector<T>(selector);
      if (!item) throw new Error('Missing element: ' + selector);
      return item;
    },
  };
}
afterEach(() => mounted.splice(0).forEach(({ app, element }) => {
  app.unmount();
  element.remove();
}));

describe('演示交易台本地交互', () => {
  it('切换交易对同时更新价格图与行情详情', async () => {
    const view = open();
    view.get('[data-symbol="ETHUSDT"]').click();
    await nextTick();
    expect(view.get('[data-testid="chart-symbol"]').textContent).toContain('ETH');
    expect(view.get('[data-testid="selected-price"]').textContent).toContain('3,142.80');
    view.get('[data-timeframe="1h"]').click();
    await nextTick();
    expect(view.get('[data-timeframe="1h"]').getAttribute('aria-pressed')).toBe('true');
  });

  it('暂停操作只在确认后改变本地演练状态，取消不改变', async () => {
    const view = open();
    view.get('[data-action="pause"]').click();
    await nextTick();
    expect(view.get('[role="dialog"]').textContent).toContain('仅影响演示数据');
    view.get('[data-action="cancel"]').click();
    await nextTick();
    expect(view.get('[data-testid="strategy-state"]').textContent).toContain('运行中');
    view.get('[data-action="pause"]').click();
    await nextTick();
    view.get('[data-action="confirm"]').click();
    await nextTick();
    expect(view.get('[data-testid="strategy-state"]').textContent).toContain('已暂停');
    expect(view.element.querySelector('[role="dialog"]')).toBeNull();
  });

  it('过期和失败场景保留快照并明确标明非当前行情', () => {
    const view = open('stale');
    expect(view.get('[role="status"]').textContent).toContain('快照已过期');
    expect(view.element.querySelectorAll('[data-symbol]')).toHaveLength(3);
  });
});

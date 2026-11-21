import type { App } from 'vue';

import { afterEach, describe, expect, it } from 'vitest';
import { createApp, h, nextTick } from 'vue';

import DemoPreview from './DemoPreview.vue';

const mounted: { app: App; element: HTMLDivElement }[] = [];

function open() {
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({ render: () => h(DemoPreview) });
  app.mount(element);
  mounted.push({ app, element });
  return {
    element,
    get<T extends HTMLElement = HTMLElement>(selector: string) {
      const node = element.querySelector<T>(selector);
      if (!node) throw new Error('找不到元素 ' + selector);
      return node;
    },
  };
}

afterEach(() => mounted.splice(0).forEach(({ app, element }) => {
  app.unmount();
  element.remove();
}));

describe('本地演示预览壳', () => {
  it('明确隔离账户与命令，并能切换各个页面样本', async () => {
    const view = open();
    expect(view.get('[role="status"]').textContent).toContain('本地演示数据');
    expect(view.element.textContent).toContain('不读取账户，不发送命令');
    expect(view.get('[data-testid="preview-content"]').textContent).toContain('演示交易台');

    view.get<HTMLButtonElement>('[data-workspace="research"]').click();
    await nextTick();
    expect(view.get('[data-workspace="research"]').getAttribute('aria-selected')).toBe('true');
    expect(view.get('[data-testid="preview-content"]').textContent).toContain('因子实验室');

    view.get<HTMLButtonElement>('[data-workspace="governance"]').click();
    await nextTick();
    expect(view.get('[data-testid="preview-content"]').textContent).toContain('验收与上线');
  });
});

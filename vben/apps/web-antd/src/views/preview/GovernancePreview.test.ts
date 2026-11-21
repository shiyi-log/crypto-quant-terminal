import type { App } from 'vue';

import { afterEach, describe, expect, it } from 'vitest';
import { createApp, h, nextTick } from 'vue';

import GovernancePreview from './GovernancePreview.vue';

const mounted: { app: App; element: HTMLDivElement }[] = [];

function open(workspace: string) {
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({ render: () => h(GovernancePreview, { workspace }) });
  app.mount(element);
  mounted.push({ app, element });
  return {
    element,
    get<T extends HTMLElement = HTMLElement>(selector: string) {
      const result = element.querySelector<T>(selector);
      if (!result) throw new Error('找不到元素 ' + selector);
      return result;
    },
  };
}

afterEach(() => {
  mounted.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  });
});

describe('治理工作区演示交互', () => {
  it('嵌入应用主内容区时不创建嵌套 main 地标', () => {
    const element = document.createElement('div');
    document.body.append(element);
    const app = createApp({
      render: () => h('main', [h(GovernancePreview, { workspace: 'acceptance' })]),
    });
    app.mount(element);
    mounted.push({ app, element });

    expect(element.querySelectorAll('main')).toHaveLength(1);
  });

  it('查看验收项时切换对应证据，审批演练不会改变真实准入', async () => {
    const view = open('acceptance');
    view.get<HTMLButtonElement>('[data-evidence="trades"]').click();
    await nextTick();
    expect(view.get('[data-testid="evidence-detail"]').textContent).toContain('73 / 100');
    view.get<HTMLButtonElement>('[data-action="preview-approval"]').click();
    await nextTick();
    expect(view.get('[role="dialog"]').textContent).toContain('仅本地演练');
    view.get<HTMLButtonElement>('[data-action="confirm-approval"]').click();
    await nextTick();
    expect(view.get('[role="status"]').textContent).toContain('已记录演示审批');
    expect(view.get('[data-testid="production-eligibility"]').textContent).toContain('未授权');
  });

  it('数据中心按交易对筛选，本地补采不声称完成真实采集', async () => {
    const view = open('data-quality');
    const filter = view.get<HTMLSelectElement>('select[aria-label="交易对筛选"]');
    filter.value = 'ETHUSDT';
    filter.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();
    expect(view.get('[data-testid="coverage-table"] tbody').textContent).toContain('ETHUSDT');
    expect(view.get('[data-testid="coverage-table"] tbody').textContent).not.toContain('BTCUSDT');
    view.get<HTMLButtonElement>('[data-action="queue-collection"]').click();
    await nextTick();
    expect(view.get('[data-testid="collection-queue"]').textContent).toContain('ETHUSDT');
    expect(view.get('[role="status"]').textContent).toContain('仅本地演练');
  });

  it('运维日志级别筛选与详情保留选中事件身份', async () => {
    const view = open('operations');
    const filter = view.get<HTMLSelectElement>('select[aria-label="日志级别"]');
    filter.value = 'warning';
    filter.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();
    const list = view.get('[data-testid="log-list"]');
    expect(list.textContent).toContain('资金费观测延迟');
    expect(list.textContent).not.toContain('账户快照已封存');
    view.get<HTMLButtonElement>('[data-log="evt-1042"]').click();
    await nextTick();
    expect(view.get('[data-testid="log-detail"]').textContent).toContain('evt-1042');
    expect(view.get('[data-testid="log-detail"]').textContent).toContain('保留最后成功快照');
  });

  it('设置保存只更新本地偏好回执，可重置为初始值', async () => {
    const view = open('settings');
    const density = view.get<HTMLSelectElement>('select[aria-label="信息密度"]');
    density.value = 'comfortable';
    density.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();
    view.get<HTMLButtonElement>('[data-action="save-preferences"]').click();
    await nextTick();
    expect(view.get('[role="status"]').textContent).toContain('已保存本地演示偏好');
    expect(view.get('[data-testid="preference-summary"]').textContent).toContain('舒适');
    view.get<HTMLButtonElement>('[data-action="reset-preferences"]').click();
    await nextTick();
    expect(view.get<HTMLSelectElement>('select[aria-label="信息密度"]').value).toBe('compact');
  });
});

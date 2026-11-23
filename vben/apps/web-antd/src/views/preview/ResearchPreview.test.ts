import type { App } from 'vue';
import { afterEach, describe, expect, it } from 'vitest';
import { createApp, h, nextTick } from 'vue';
import ResearchPreview from './ResearchPreview.vue';

const mounts: { app: App; element: HTMLDivElement }[] = [];
function open(workspace: string) {
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({ render: () => h(ResearchPreview, { workspace }) });
  app.mount(element);
  mounts.push({ app, element });
  return {
    element,
    get<T extends HTMLElement = HTMLElement>(selector: string) {
      const node = element.querySelector<T>(selector);
      if (!node) throw new Error('Missing element ' + selector);
      return node;
    },
  };
}
afterEach(() =>
  mounts.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  }),
);

describe('演示研究工作区交互', () => {
  it('嵌入应用主内容区时不创建嵌套 main 地标，并关联当前研究标签', () => {
    const element = document.createElement('div');
    document.body.append(element);
    const app = createApp({
      render: () => h('main', [h(ResearchPreview, { workspace: 'factors' })]),
    });
    app.mount(element);
    mounts.push({ app, element });

    expect(element.querySelectorAll('main')).toHaveLength(1);
    const activeTab = element.querySelector<HTMLButtonElement>(
      '[role="tab"][aria-selected="true"]',
    );
    expect(activeTab?.id).toBe('research-tab-factors');
    expect(activeTab?.getAttribute('aria-controls')).toBe(
      'research-panel-factors',
    );
    expect(
      element
        .querySelector('[role="tabpanel"]')
        ?.getAttribute('aria-labelledby'),
    ).toBe('research-tab-factors');
  });

  it('可在因子、策略、实验和订单流工作区之间切换', async () => {
    const view = open('factors');
    view.get<HTMLButtonElement>('[data-workspace="research"]').click();
    await nextTick();
    expect(view.get('#strategy-title').textContent).toContain('策略研究');
    view.get<HTMLButtonElement>('[data-workspace="experiments"]').click();
    await nextTick();
    expect(view.get('#experiment-title').textContent).toContain('实验任务');
    view.get<HTMLButtonElement>('[data-workspace="orderflow"]').click();
    await nextTick();
    expect(view.get('#flow-title').textContent).toContain('订单流与价格');
  });

  it('因子搜索限制结果，选择后展示对应因子说明', async () => {
    const view = open('factors');
    const search = view.get<HTMLInputElement>('input[aria-label="搜索因子"]');
    search.value = '成交量';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    await nextTick();
    expect(view.get('tbody').textContent).toContain('成交量突破');
    expect(view.get('tbody').textContent).not.toContain('动量延续');
    view.get<HTMLButtonElement>('[data-factor="volume_breakout"]').click();
    await nextTick();
    expect(view.get('[data-testid="factor-detail"]').textContent).toContain(
      'volume_breakout',
    );
    expect(view.get('[data-testid="factor-detail"]').textContent).toContain(
      '近 20 根 K 线',
    );
  });

  it('因子搜索后的详情跟随可见结果，无匹配时显示空状态', async () => {
    const view = open('factors');
    view
      .get<HTMLButtonElement>('[data-factor="momentum_continuation"]')
      .click();
    await nextTick();
    const search = view.get<HTMLInputElement>('input[aria-label="搜索因子"]');

    search.value = '盘口';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    await nextTick();
    expect(view.get('[data-testid="factor-detail"]').textContent).toContain(
      'book_imbalance',
    );
    expect(
      view
        .get('[data-factor="book_imbalance"]')
        .closest('tr')
        ?.classList.contains('selected'),
    ).toBe(true);

    search.value = '没有匹配项';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    await nextTick();
    expect(view.get('[data-testid="factor-empty"]').textContent).toContain(
      '没有符合条件的因子',
    );
    expect(
      view.element.querySelector('[data-testid="factor-detail"]'),
    ).toBeNull();
  });

  it('策略选择同时切换参数与曲线说明', async () => {
    const view = open('research');
    view.get<HTMLButtonElement>('[data-strategy="range"]').click();
    await nextTick();
    expect(view.get('[data-testid="strategy-detail"]').textContent).toContain(
      '均值回归',
    );
    expect(view.get('[data-testid="strategy-detail"]').textContent).toContain(
      '布林带偏离',
    );
    expect(
      view.get('[data-testid="strategy-detail"]').textContent,
    ).not.toContain('ATR 跟踪止损');
  });

  it('实验队列可筛选失败任务并检查失败原因', async () => {
    const view = open('experiments');
    const filter = view.get<HTMLSelectElement>('select[aria-label="任务状态"]');
    filter.value = 'running';
    filter.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();
    expect(view.get('[data-testid="experiment-detail"]').textContent).toContain(
      'EXP-023',
    );

    filter.value = 'failed';
    filter.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();
    expect(view.get('[data-testid="experiment-list"]').textContent).toContain(
      '压力成本复核',
    );
    expect(
      view.get('[data-testid="experiment-list"]').textContent,
    ).not.toContain('动量参数搜索');
    view.get<HTMLButtonElement>('[data-experiment="EXP-024"]').click();
    await nextTick();
    expect(view.get('[data-testid="experiment-detail"]').textContent).toContain(
      '资金费数据存在 2 处缺口',
    );
  });

  it('订单流交易对切换更新盘口与逐笔成交', async () => {
    const view = open('orderflow');
    view.get<HTMLButtonElement>('[data-symbol="ETHUSDT"]').click();
    await nextTick();
    expect(view.get('[data-testid="flow-price"]').textContent).toContain(
      '3,184.62',
    );
    expect(view.get('[data-testid="flow-trades"]').textContent).toContain(
      '3,184.58',
    );
    expect(view.get('[data-testid="flow-trades"]').textContent).not.toContain(
      '91,482',
    );
  });
});

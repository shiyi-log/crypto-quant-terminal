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

  it('因子详情按固定演示样本展示完整、过期和未知输入质量及原因', async () => {
    const view = open('factors');
    const detail = () => view.get('[data-testid="factor-detail"]').textContent;

    expect(detail()).toContain('演示样本完整');
    expect(detail()).toContain('20 根已收盘 K 线基线齐全');

    view.get<HTMLButtonElement>('[data-factor="momentum_continuation"]').click();
    await nextTick();
    expect(detail()).toContain('演示样本过期');
    expect(detail()).toContain('最后一段样本超过演示有效窗口');

    view.get<HTMLButtonElement>('[data-factor="book_imbalance"]').click();
    await nextTick();
    expect(detail()).toContain('演示样本未知');
    expect(detail()).toContain('盘口序列含断档，不能判定输入完整性');
    expect(detail()).not.toContain('已通过');
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
    const firstDetail = view.get('[data-testid="strategy-detail"]');
    expect(firstDetail.textContent).toContain('候选身份');
    expect(firstDetail.textContent).toContain('数据窗口');
    expect(firstDetail.textContent).toContain('成本假设');
    expect(firstDetail.textContent).toContain('留出集');
    expect(firstDetail.textContent).toContain('未评估');
    expect(firstDetail.textContent).toContain('未冻结 · 不进入 Demo 预检');
    const firstIdentity = view.get('[data-testid="strategy-identity"]');
    expect(firstIdentity.textContent).toContain('STRAT-DEMO-TREND-01');

    view.get<HTMLButtonElement>('[data-strategy="range"]').click();
    await nextTick();
    const selectedDetail = view.get('[data-testid="strategy-detail"]');
    expect(selectedDetail.textContent).toContain('STRAT-DEMO-RANGE-01');
    expect(selectedDetail.textContent).toContain('2026-10-01 至 2026-10-31');
    expect(selectedDetail.textContent).toContain('每边 10 bps');
    expect(selectedDetail.textContent).toContain('未冻结 · 不进入 Demo 预检');
    expect(selectedDetail.textContent).toContain(
      '均值回归',
    );
    expect(selectedDetail.textContent).toContain(
      '布林带偏离',
    );
    expect(
      selectedDetail.textContent,
    ).not.toContain('ATR 跟踪止损');
    expect(selectedDetail.textContent).not.toContain('已通过');
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

  it('实验未知状态不推断进度、暂停或成功且标明 worker 未验证', async () => {
    const view = open('experiments');
    const filter = view.get<HTMLSelectElement>('select[aria-label="任务状态"]');
    filter.value = 'unknown';
    filter.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();

    expect(view.get('[data-testid="experiment-list"]').textContent).toContain(
      '任务状态未知',
    );
    expect(view.get('[data-testid="experiment-detail"]').textContent).toContain(
      'worker 未验证',
    );
    expect(view.get('[data-testid="experiment-detail"]').textContent).toContain(
      '无法判定任务是否停止或仍在运行',
    );
    expect(view.get('[data-testid="experiment-detail"]').textContent).toContain(
      '进度未知',
    );
    expect(view.get('[data-testid="experiment-detail"]').textContent).not.toContain(
      '0%',
    );
    expect(view.get('[data-testid="experiment-detail"]').textContent).not.toContain(
      '已暂停',
    );
    expect(view.get('[data-testid="experiment-detail"]').textContent).not.toContain(
      '已完成',
    );
  });

  it('实验任务搜索跟随可见结果并为空结果清除详情', async () => {
    const view = open('experiments');
    const search = view.get<HTMLInputElement>(
      'input[aria-label="搜索实验任务"]',
    );
    search.value = '动量';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    await nextTick();

    expect(view.get('[data-testid="experiment-list"]').textContent).toContain(
      'EXP-023',
    );
    expect(view.get('[data-testid="experiment-detail"]').textContent).toContain(
      '动量参数搜索',
    );

    const status = view.get<HTMLSelectElement>(
      'select[aria-label="任务状态"]',
    );
    status.value = 'completed';
    status.dispatchEvent(new Event('change', { bubbles: true }));
    search.value = '订单流';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    await nextTick();
    expect(view.get('[data-testid="experiment-list"]').textContent).toContain(
      'EXP-021',
    );
    expect(view.get('[data-testid="experiment-list"]').textContent).not.toContain(
      'EXP-023',
    );
    expect(view.get('[data-testid="experiment-detail"]').textContent).toContain(
      '订单流覆盖盘点',
    );

    search.value = '没有匹配项';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    await nextTick();
    expect(view.get('[data-testid="experiment-empty"]').textContent).toContain(
      '没有符合条件的实验任务',
    );
    expect(
      view.element.querySelector('[data-testid="experiment-detail"]'),
    ).toBeNull();
  });

  it('订单流交易对切换更新盘口与逐笔成交', async () => {
    const view = open('orderflow');
    expect(view.get('[data-testid="flow-evidence"]').textContent).toContain(
      '成交额',
    );
    expect(view.get('[data-testid="flow-evidence"]').textContent).toContain(
      '主动买',
    );
    expect(view.get('[data-testid="flow-evidence"]').textContent).toContain(
      '事件成熟度已成熟',
    );
    expect(view.get('[data-testid="flow-evidence"]').textContent).toContain(
      '成交率',
    );
    expect(view.get('[data-testid="flow-evidence"]').textContent).toContain(
      '逆向选择',
    );

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
    expect(view.get('[data-testid="flow-evidence"]').textContent).toContain(
      '数据过期',
    );
    expect(view.get('[data-testid="flow-evidence"]').textContent).toContain(
      '观测时间 2026-11-23 14:31',
    );

    view.get<HTMLButtonElement>('[data-symbol="TRXUSDT"]').click();
    await nextTick();
    const unknownEvidence = view.get('[data-testid="flow-evidence"]').textContent;
    expect(unknownEvidence).toContain('未知');
    expect(unknownEvidence).not.toContain('成交率0%');
    expect(unknownEvidence).not.toContain('已通过');
  });
});

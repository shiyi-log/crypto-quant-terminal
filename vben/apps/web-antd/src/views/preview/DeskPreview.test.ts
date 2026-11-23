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
afterEach(() =>
  mounted.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  }),
);

describe('演示交易台本地交互', () => {
  it('切换交易对同时更新价格图与行情详情', async () => {
    const view = open();
    view.get('[data-symbol="ETHUSDT"]').click();
    await nextTick();
    expect(view.get('[data-testid="chart-symbol"]').textContent).toContain(
      'ETH',
    );
    expect(view.get('[data-testid="selected-price"]').textContent).toContain(
      '3,142.80',
    );
    view.get('[data-timeframe="1h"]').click();
    await nextTick();
    expect(view.get('[data-timeframe="1h"]').getAttribute('aria-pressed')).toBe(
      'true',
    );
  });

  it('暂停操作只在确认后改变本地演练状态，取消不改变', async () => {
    const view = open();
    view.get('[data-action="pause"]').click();
    await nextTick();
    expect(view.get('[role="dialog"]').textContent).toContain('仅影响演示数据');
    view.get('[data-action="cancel"]').click();
    await nextTick();
    expect(view.get('[data-testid="strategy-state"]').textContent).toContain(
      '运行中',
    );
    view.get('[data-action="pause"]').click();
    await nextTick();
    view.get('[data-action="confirm"]').click();
    await nextTick();
    expect(view.get('[data-testid="strategy-state"]').textContent).toContain(
      '已暂停',
    );
    expect(view.element.querySelector('[role="dialog"]')).toBeNull();
  });

  it('过期和失败场景保留快照并明确标明非当前行情', () => {
    const view = open('stale');
    expect(view.get('[role="status"]').textContent).toContain('快照已过期');
    expect(view.element.querySelectorAll('[data-symbol]')).toHaveLength(3);
  });

  it('部分成交时只显示已成交数量且保护保持确认中', async () => {
    const view = open();
    view.get('[data-scenario="partial-fill"]').click();
    await nextTick();
    expect(view.get('[data-testid="position-state"]').textContent).toContain(
      '0.001 BTC 已成交',
    );
    expect(
      view.get('[data-testid="position-protection"]').textContent,
    ).toContain('确认中');
    expect(
      view.get('[data-testid="position-protection"]').textContent,
    ).toContain('尚未确认有效保护');
    expect(view.get('[data-testid="desk-reason"]').textContent).toContain(
      'partial_fill_protection_pending',
    );
  });

  it('未知和读取失败保留最后样本并注明快照时间与原因码', async () => {
    const view = open();
    const scenarios = [
      ['unknown', '账户快照未知', 'account_snapshot_unknown'],
      [
        'read-failure',
        '读取失败：保留上次样本',
        'account_snapshot_read_failed',
      ],
    ];
    for (const [scenario, label, reason] of scenarios) {
      view.get('[data-scenario="' + scenario + '"]').click();
      await nextTick();
      expect(
        view.get('[data-testid="desk-snapshot-status"]').textContent,
      ).toContain(label);
      expect(view.get('[data-testid="snapshot-time"]').textContent).toContain(
        '14:32:18',
      );
      expect(view.get('[data-testid="position-state"]').textContent).toContain(
        'BTCUSDT',
      );
      expect(view.get('[data-testid="desk-reason"]').textContent).toContain(
        reason,
      );
    }
  });

  it('空仓、冷却和硬失败不会显示持仓或允许新增风险', async () => {
    const view = open();
    view.get('[data-scenario="flat"]').click();
    await nextTick();
    expect(view.get('[data-testid="positions-empty"]').textContent).toContain(
      '当前没有持仓',
    );
    view.get('[data-scenario="cooldown"]').click();
    await nextTick();
    expect(view.get('[data-testid="entry-permission"]').textContent).toContain(
      '禁止新增风险',
    );
    expect(view.get('[data-testid="entry-permission"]').textContent).toContain(
      '43 分钟',
    );
    expect(view.get('[data-testid="desk-reason"]').textContent).toContain(
      'daily_net_loss_cooldown',
    );
    view.get('[data-scenario="hard-failure"]').click();
    await nextTick();
    expect(view.get('[data-testid="entry-permission"]').textContent).toContain(
      '本轮已终止',
    );
    expect(view.get('[data-testid="desk-reason"]').textContent).toContain(
      'daily_net_loss_hard_limit_exceeded',
    );
    expect(
      view.get('[data-testid="strategy-state"]').textContent,
    ).not.toContain('运行中');
  });
});

import type { App } from 'vue';

import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick } from 'vue';

import AcceptancePreview from './AcceptancePreview.vue';

const mounted: { app: App; element: HTMLDivElement }[] = [];

function open() {
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({ render: () => h(AcceptancePreview) });
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

describe('固定演示验收证据视图', () => {
  it('展示完整固定窗口，停机日仍留在日历且没有证据的日期保持未知', () => {
    const view = open();

    expect(
      view.element.querySelectorAll('[data-testid="calendar-day"]'),
    ).toHaveLength(30);
    expect(view.get('[data-testid="acceptance-window"]').textContent).toContain(
      '2026-10-25 至 2026-11-23',
    );
    expect(view.get('h1').textContent).toBe('验收与上线');
    expect(view.get('h2').textContent).toContain('Demo 验收证据');
    expect(
      view.get('[data-testid="calendar-day"][data-day="7"]').textContent,
    ).toContain('停机');
    expect(
      view.get('[data-testid="calendar-day"][data-day="24"]').textContent,
    ).toContain('未知');
    expect(
      view
        .get('[data-testid="acceptance-calendar"]')
        .getAttribute('aria-label'),
    ).toContain('北京时间');
  });

  it('选择某天后显示权益、净入金、日损益、完整往返、成本状态和原因码', async () => {
    const view = open();

    view
      .get<HTMLButtonElement>('[data-testid="calendar-day"][data-day="18"]')
      .click();
    await nextTick();

    const detail = view.get('[data-testid="day-evidence"]');
    expect(detail.textContent).toContain('1,097.00 USDT');
    expect(detail.textContent).toContain('0.00 USDT');
    expect(detail.textContent).toContain('+38.20 USDT');
    expect(detail.textContent).toContain('4 笔');
    expect(detail.textContent).toContain('成本完整');
    expect(detail.textContent).toContain('demo_day_observed');
    expect(detail.textContent).toContain('2026-11-11');
  });

  it('已知日的日末权益等于日初权益加净入金与净损益，证据缺口不伪造权益', async () => {
    const view = open();

    for (const day of [6, 8, 12, 14, 18, 23]) {
      view
        .get<HTMLButtonElement>(
          `[data-testid="calendar-day"][data-day="${day}"]`,
        )
        .click();
      await nextTick();
      const detail = view.get('[data-testid="day-evidence"]');
      expect(detail.textContent).toContain('日初净权益');
      const opening = Number(
        detail
          .querySelector('[data-testid="opening-equity"]')
          ?.getAttribute('data-value'),
      );
      const closing = Number(
        detail
          .querySelector('[data-testid="closing-equity"]')
          ?.getAttribute('data-value'),
      );
      const deposit = Number(
        detail
          .querySelector('[data-testid="net-deposit"]')
          ?.getAttribute('data-value'),
      );
      const pnl = Number(
        detail
          .querySelector('[data-testid="daily-pnl"]')
          ?.getAttribute('data-value'),
      );
      expect(closing).toBeCloseTo(opening + deposit + pnl, 2);
    }

    view
      .get<HTMLButtonElement>('[data-testid="calendar-day"][data-day="13"]')
      .click();
    await nextTick();
    expect(view.get('[data-testid="closing-equity"]').textContent).toContain(
      '未知',
    );
    expect(view.get('[data-testid="daily-pnl"]').textContent).toContain('未知');
  });

  it('硬失败优先于缺证据，三个分段与准入条件按固定规则展示', async () => {
    const view = open();

    expect(
      view.element.querySelectorAll('[data-testid="acceptance-segment"]'),
    ).toHaveLength(3);
    expect(view.get('[data-testid="acceptance-gates"]').textContent).toContain(
      '净利润至少 1,500 USDT',
    );
    expect(view.get('[data-testid="acceptance-gates"]').textContent).toContain(
      '100 笔完整往返',
    );

    view.get<HTMLButtonElement>('[data-scenario="hard-failure"]').click();
    await nextTick();

    expect(
      view.get('[data-testid="acceptance-verdict"]').textContent,
    ).toContain('硬失败场景演示');
    expect(
      view.get('[data-testid="acceptance-verdict"]').textContent,
    ).toContain('daily_net_loss_hard_limit_exceeded');
    expect(
      view.get('[data-testid="production-eligibility"]').textContent,
    ).toContain('未授权');
  });

  it('证据过期时保留最后快照与原因码，验收和生产资格仍保持未知', async () => {
    const view = open();

    view.get<HTMLButtonElement>('[data-scenario="expired"]').click();
    await nextTick();

    const verdict = view.get('[data-testid="acceptance-verdict"]');
    const snapshot = view.get('[data-testid="expired-evidence"]');
    expect(verdict.textContent).toContain('证据已过期，验收未知');
    expect(verdict.textContent).toContain('demo_equity_snapshot_expired');
    expect(snapshot.textContent).toContain('1,097.00 USDT');
    expect(snapshot.textContent).toContain('2026-11-11 14:32 北京时间');
    expect(snapshot.textContent).toContain('demo_equity_snapshot_expired');
    expect(view.get('[data-testid="production-eligibility"]').textContent).toContain(
      '未授权',
    );
    expect(view.get('[data-testid="acceptance-gates"]').textContent).toContain(
      '未知',
    );
    expect(view.get('[data-testid="acceptance-run-log"]').textContent).toContain(
      'demo_equity_snapshot_expired',
    );
    expect(view.get('[data-testid="acceptance-run-log"]').textContent).toContain(
      '生产资格保持未授权',
    );
  });

  it('冷却演示显示剩余时间、禁止新增风险且不形成验收或生产资格', async () => {
    const view = open();

    view.get<HTMLButtonElement>('[data-scenario="cooldown"]').click();
    await nextTick();

    const evidence = view.get('[data-testid="cooldown-evidence"]');
    expect(view.get('[data-testid="acceptance-verdict"]').textContent).toContain(
      '冷却中，验收未知',
    );
    expect(evidence.textContent).toContain('00:47:30');
    expect(evidence.textContent).toContain('连续三笔完整往返净亏');
    expect(evidence.textContent).toContain('禁止新增风险');
    expect(evidence.textContent).toContain('冷却结束后只接受新鲜信号');
    expect(view.get('[data-testid="production-eligibility"]').textContent).toContain(
      '未授权',
    );
    expect(view.get('[data-testid="acceptance-run-log"]').textContent).toContain(
      '本地固定演示冷却',
    );
    expect(view.get('[data-testid="acceptance-run-log"]').textContent).toContain(
      'cooldown_active',
    );
  });

  it('部分成交保护待确认时保留旧保护、停止入口风险且验收保持未知', async () => {
    const view = open();

    view
      .get<HTMLButtonElement>('[data-scenario="partial-fill-protection"]')
      .click();
    await nextTick();

    const evidence = view.get('[data-testid="partial-fill-evidence"]');
    expect(view.get('[data-testid="acceptance-verdict"]').textContent).toContain(
      '部分成交保护待确认，验收未知',
    );
    expect(evidence.textContent).toContain('0.012 / 0.020 ETH');
    expect(evidence.textContent).toContain('剩余未成交 0.008 ETH');
    expect(evidence.textContent).toContain('保护覆盖量未知');
    expect(evidence.textContent).toContain('保留唯一有效保护');
    expect(evidence.textContent).toContain('撤销剩余入口');
    expect(evidence.textContent).toContain('停止加仓');
    expect(evidence.textContent).toContain('减仓与对账');
    expect(evidence.textContent).toContain('禁止新增风险');
    expect(view.get('[data-testid="production-eligibility"]').textContent).toContain(
      '未授权',
    );
    expect(view.get('[data-testid="acceptance-run-log"]').textContent).toContain(
      'partial_fill_protection_unconfirmed',
    );
  });

  it('审批按钮只触发本地演练回调并始终保留未授权状态', async () => {
    const onAction = vi.fn();
    const onApprove = vi.fn();
    const element = document.createElement('div');
    document.body.append(element);
    const app = createApp({
      render: () => h(AcceptancePreview, { onAction, onApprove }),
    });
    app.mount(element);
    mounted.push({ app, element });

    element
      .querySelector<HTMLButtonElement>('[data-action="preview-approval"]')!
      .click();
    await nextTick();
    element
      .querySelector<HTMLButtonElement>('[data-action="confirm-approval"]')!
      .click();
    await nextTick();

    expect(onApprove).toHaveBeenCalledWith('small-live');
    expect(onAction).toHaveBeenCalled();
    expect(
      element.querySelector('[data-testid="production-eligibility"]')
        ?.textContent,
    ).toContain('未授权');
    expect(
      element.querySelector('[data-testid="acceptance-run-log"]')?.textContent,
    ).toContain('approval_preview_only');
    expect(
      element.querySelector('[data-testid="acceptance-run-log"]')?.textContent,
    ).toContain('审批流程演练');
  });

  it('审批弹窗支持 Escape 关闭并将键盘焦点交还给打开按钮', async () => {
    const view = open();
    const trigger = view.get<HTMLButtonElement>(
      '[data-action="preview-approval"]',
    );

    trigger.focus();
    trigger.click();
    await nextTick();
    await nextTick();
    expect(view.get('[role="dialog"]').contains(document.activeElement)).toBe(
      true,
    );

    view
      .get('[role="dialog"]')
      .dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
      );
    await nextTick();
    await nextTick();

    expect(view.element.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it('场景演练追加中文运行记录且保留等级、阶段、结果、原因码和耗时', async () => {
    const view = open();
    view.get<HTMLButtonElement>('[data-scenario="hard-failure"]').click();
    await nextTick();

    const log = view.get('[data-testid="acceptance-run-log"]');
    expect(log.textContent).toContain('硬失败');
    expect(log.textContent).toContain('验收场景演练');
    expect(log.textContent).toContain('本地演练完成');
    expect(log.textContent).toContain('daily_net_loss_hard_limit_exceeded');
    expect(log.textContent).toContain('耗时');
    expect(log.textContent).toContain('不写服务器日志');
  });
});

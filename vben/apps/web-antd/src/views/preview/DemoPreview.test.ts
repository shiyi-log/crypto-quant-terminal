import type { App } from 'vue';

import { createApp, h, nextTick } from 'vue';

import { afterEach, describe, expect, it, vi } from 'vitest';

import DemoPreview from './DemoPreview.vue';

const mounted: { app: App; element: HTMLDivElement }[] = [];

function open(props: { preview?: unknown; workspace?: unknown; eventId?: unknown } = {}) {
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({ render: () => h(DemoPreview, props) });
  app.mount(element);
  mounted.push({ app, element });
  return {
    element,
    get<T extends HTMLElement = HTMLElement>(selector: string) {
      const node = element.querySelector<T>(selector);
      if (!node) throw new Error(`找不到元素 ${selector}`);
      return node;
    },
  };
}

afterEach(() => {
  mounted.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  });
  vi.unstubAllEnvs();
});

describe('本地演示预览壳', () => {
  it('在样本提示与页脚同步显示构建注入的项目版本', () => {
    vi.stubEnv('VITE_QUANT_VERSION', '9.8.7');
    const view = open();

    expect(view.get('.scope-version').textContent).toBe('预览样本 · 9.8.7');
    expect(view.get('.footer-build').textContent).toBe('QT · 9.8.7');
  });

  it('按量化深链接选择指定的固定演示子工作区', async () => {
    const view = open({ preview: 'research', workspace: 'experiments' });
    await nextTick();
    expect(
      view.get('[data-workspace="research"]').getAttribute('aria-current'),
    ).toBe('true');
    expect(
      view.get('[data-workspace="experiments"]').getAttribute('aria-current'),
    ).toBe('true');
    expect(view.get('[data-testid="preview-content"]').textContent).toContain(
      'EXP-024',
    );
  });

  it('将当前工作区语义与演示区域标签正确关联', () => {
    const view = open({ preview: 'governance', workspace: 'operations' });
    const tab = view.get('[data-workspace="governance"]');
    const panel = view.get('#preview-panel');

    expect(tab.getAttribute('aria-current')).toBe('true');
    expect(panel.getAttribute('role')).toBe('region');
    expect(panel.getAttribute('aria-label')).toContain('验收与运维');
  });

  it('明确隔离账户与命令，并能切换各个页面样本', async () => {
    const view = open();
    expect(view.get('[role="status"]').textContent).toContain('本地演示数据');
    expect(view.element.textContent).toContain('不读取账户，不发送命令');
    expect(view.get('[data-testid="terminal-sidebar"]').textContent).toContain(
      '因子实验室',
    );
    expect(view.get('[data-testid="terminal-topbar"]').textContent).toContain(
      'Binance Demo',
    );
    expect(
      view.get('[data-testid="terminal-risk-strip"]').textContent,
    ).toContain('保护单状态');
    expect(view.get('[data-testid="preview-content"]').textContent).toContain(
      '演示交易台',
    );

    view.get<HTMLButtonElement>('[data-workspace="research"]').click();
    await nextTick();
    expect(
      view.get('[data-workspace="research"]').getAttribute('aria-current'),
    ).toBe('true');
    expect(view.get('[data-testid="preview-content"]').textContent).toContain(
      '因子实验室',
    );

    view.get<HTMLButtonElement>('[data-workspace="governance"]').click();
    await nextTick();
    expect(view.get('[data-testid="preview-content"]').textContent).toContain(
      '验收与上线',
    );
    expect(view.get('[data-testid="acceptance-window"]').textContent).toContain(
      '北京时间',
    );
  });

  it('交易台风险横栏随冷却与硬失败演示状态同步变化', async () => {
    const view = open();
    view.get('[data-scenario="cooldown"]').click();
    await nextTick();
    expect(
      view.get('[data-testid="terminal-risk-strip"]').textContent,
    ).toContain('冷却中 · 43 分钟');
    expect(
      view.get('[data-testid="terminal-risk-strip"]').textContent,
    ).toContain('保护状态未知');

    view.get('[data-scenario="hard-failure"]').click();
    await nextTick();
    expect(
      view.get('[data-testid="terminal-risk-strip"]').textContent,
    ).toContain('本轮硬失败');
    expect(
      view.get('[data-testid="terminal-risk-strip"]').textContent,
    ).toContain('daily_net_loss_hard_limit_exceeded');
  });

  it('常驻风险栏与交易台指标对同一场景显示一致的损益、预算和保护状态', async () => {
    const view = open();
    const desk = () => view.get('[data-testid="desk-metrics"]');
    const strip = () => view.get('[data-testid="terminal-risk-strip"]');

    for (const scenario of [
      'normal',
      'flat',
      'unknown',
      'stale',
      'read-failure',
      'partial-fill',
      'cooldown',
      'hard-failure',
    ]) {
      view.get('[data-scenario="' + scenario + '"]').click();
      await nextTick();
      for (const [deskId, stripId] of [
        ['desk-pnl', 'risk-pnl'],
        ['desk-risk-budget', 'risk-budget'],
      ]) {
        expect(desk().querySelector('[data-testid="' + deskId + '"]')?.textContent)
          .toBe(strip().querySelector('[data-testid="' + stripId + '"]')?.textContent);
      }
      const protection = strip().querySelector(
        '[data-testid="risk-protection"]',
      )?.textContent;
      const deskProtection = view.get(
        '[data-testid="desk-risk-protection"]',
      ).textContent;
      expect(deskProtection).toContain(protection);
      const positionProtection = view.element.querySelector(
        '[data-testid="position-protection"]',
      )?.textContent;
      if (positionProtection) expect(positionProtection).toContain(protection);
    }
  });

  it('按研究 query 直接打开实验任务工作区', () => {
    const view = open({ preview: 'research', workspace: 'experiments' });

    expect(
      view.get('[data-workspace="research"]').getAttribute('aria-current'),
    ).toBe('true');
    expect(view.get('[data-testid="preview-content"]').textContent).toContain(
      '实验任务',
    );
    expect(view.get('#experiment-title').textContent).toContain('实验任务');
  });

  it('按治理 query 直接打开运维与证据工作区', () => {
    const view = open({ preview: 'governance', workspace: 'operations' });

    expect(
      view.get('[data-workspace="governance"]').getAttribute('aria-current'),
    ).toBe('true');
    expect(view.get('[data-testid="preview-content"]').textContent).toContain(
      '运维与证据',
    );
    expect(view.get('[data-testid="log-list"]').textContent).toContain(
      '本地风险快照已封存',
    );
  });

  it('通过历史事件 query 精确打开固定演示日志详情', () => {
    const view = open({ preview: 'governance', workspace: 'operations', eventId: 'evt-1042' });

    expect(view.get('[data-testid="log-detail"]').textContent).toContain('evt-1042');
    expect(view.get('[data-testid="history-locator"]').getAttribute('href')).toBe(
      '/#/quant/operations?event=evt-1042',
    );
  });

  it('重复历史事件 query 不会静默回退到最新演示日志', () => {
    const view = open({
      preview: 'governance',
      workspace: 'operations',
      eventId: ['evt-1041', 'evt-1042'],
    });

    expect(view.get('[data-testid="history-invalid"]').textContent).toContain('无效');
    expect(view.element.querySelector('[data-testid="log-detail"]')).toBeNull();
  });

  it('对未知 query 值回退到安全的默认工作区', () => {
    const view = open({ preview: 'research', workspace: 'unknown' });

    expect(
      view.get('[data-workspace="research"]').getAttribute('aria-current'),
    ).toBe('true');
    expect(view.get('#factor-title').textContent).toContain('因子实验室');
  });
});

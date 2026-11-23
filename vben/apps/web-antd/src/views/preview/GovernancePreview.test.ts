import type { App } from 'vue';

import { afterEach, describe, expect, it } from 'vitest';
import { createApp, h, nextTick } from 'vue';

import GovernancePreview from './GovernancePreview.vue';

const mounted: { app: App; element: HTMLDivElement }[] = [];

function open(workspace: string, eventId?: unknown) {
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({ render: () => h(GovernancePreview, { workspace, eventId }) });
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

  it('数据中心筛选无固定样本时显示空态，重置后恢复', async () => {
    const view = open('data-quality');
    const symbol = view.get<HTMLSelectElement>(
      'select[aria-label="交易对筛选"]',
    );
    const timeframe = view.get<HTMLSelectElement>(
      'select[aria-label="周期筛选"]',
    );
    symbol.value = 'TRXUSDT';
    symbol.dispatchEvent(new Event('change', { bubbles: true }));
    timeframe.value = '1h';
    timeframe.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();

    expect(view.get('[data-testid="coverage-empty"]').textContent).toContain(
      '当前筛选下没有固定演示样本',
    );
    expect(view.element.querySelector('[data-testid="coverage-table"]')).toBeNull();

    view.get<HTMLButtonElement>('[data-action="reset-coverage-filters"]').click();
    await nextTick();
    expect(view.get('[data-testid="coverage-table"] tbody').textContent).toContain(
      'BTCUSDT',
    );
    expect(view.element.querySelector('[data-testid="coverage-empty"]')).toBeNull();
  });

  it('周期筛选保留交易对级固定摘要，不派生周期覆盖率', async () => {
    const view = open('data-quality');
    const table = view.get('[data-testid="coverage-table"]');

    expect(table.querySelectorAll('tbody tr')).toHaveLength(3);
    expect(table.textContent).toContain('BTCUSDT');
    expect(table.textContent).toContain('ETHUSDT');
    expect(table.textContent).toContain('TRXUSDT');
    expect(table.textContent).toContain('96%');
    expect(table.textContent).toContain('88%');
    expect(table.textContent).toContain('71%');
    expect(table.textContent).toContain('12 分钟前');
    expect(table.textContent).toContain('18 分钟前');
    expect(table.textContent).toContain('未提供');
    expect(table.textContent).toContain('5m / 15m / 1h');
    expect(table.textContent).toContain('摘要覆盖率');

    const timeframe = view.get<HTMLSelectElement>(
      'select[aria-label="周期筛选"]',
    );
    timeframe.value = '1h';
    timeframe.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();

    expect(view.get('[data-testid="coverage-table"]').textContent).toContain(
      '96%',
    );
    expect(view.get('[data-testid="coverage-scope"]').textContent).toContain(
      '交易对级摘要',
    );
  });

  it('重复加入同一交易对时不重复写入已排队事件', async () => {
    const view = open('data-quality');
    const queue = view.get<HTMLButtonElement>('[data-action="queue-collection"]');

    queue.click();
    await nextTick();
    queue.click();
    await nextTick();

    expect(view.get('[data-testid="collection-queue"]').textContent).toBe('BTCUSDT');
    expect(view.get('[role="status"]').textContent).toContain('已在本地队列');
  });

  it('数据中心明确区分固定样本新鲜、过期和未知原因', () => {
    const view = open('data-quality');
    const table = view.get('[data-testid="coverage-table"]');

    expect(table.querySelector('[data-symbol="BTCUSDT"] [data-freshness="fresh"]')?.textContent).toContain('新鲜');
    expect(table.querySelector('[data-symbol="BTCUSDT"]')?.textContent).toContain('观测时间在演示有效窗口内');
    expect(table.querySelector('[data-symbol="ETHUSDT"] [data-freshness="stale"]')?.textContent).toContain('过期');
    expect(table.querySelector('[data-symbol="ETHUSDT"]')?.textContent).toContain('仅供回看');
    expect(table.querySelector('[data-symbol="TRXUSDT"] [data-freshness="unknown"]')?.textContent).toContain('未知');
    expect(table.querySelector('[data-symbol="TRXUSDT"]')?.textContent).toContain('不能判断新鲜度');
    expect(table.querySelector('[data-symbol="TRXUSDT"]')?.textContent).toContain('未提供');
    expect(table.querySelector('[data-symbol="TRXUSDT"]')?.textContent).not.toContain('分钟前');
  });

  it('数据中心新鲜度筛选与交易对和周期组合，无匹配时不保留旧详情', async () => {
    const view = open('data-quality');
    const symbol = view.get<HTMLSelectElement>('select[aria-label="交易对筛选"]');
    const timeframe = view.get<HTMLSelectElement>('select[aria-label="周期筛选"]');
    const freshness = view.get<HTMLSelectElement>('select[aria-label="新鲜度筛选"]');

    symbol.value = 'ETHUSDT';
    symbol.dispatchEvent(new Event('change', { bubbles: true }));
    timeframe.value = '1h';
    timeframe.dispatchEvent(new Event('change', { bubbles: true }));
    freshness.value = 'stale';
    freshness.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();

    expect(view.get('[data-testid="coverage-table"] tbody').querySelectorAll('tr')).toHaveLength(1);
    expect(view.get('[data-testid="coverage-table"]').textContent).toContain('ETHUSDT');
    expect(view.get('[data-testid="coverage-table"]').textContent).toContain('仅供回看');

    freshness.value = 'fresh';
    freshness.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();

    expect(view.get('[data-testid="coverage-empty"]').textContent).toContain('当前筛选下没有固定演示样本');
    expect(view.element.querySelector('[data-testid="coverage-table"]')).toBeNull();

    view.get<HTMLButtonElement>('[data-action="reset-coverage-filters"]').click();
    await nextTick();
    expect(view.get('[data-testid="coverage-table"] tbody').querySelectorAll('tr')).toHaveLength(3);
  });

  it('运维日志级别筛选与详情保留选中事件身份', async () => {
    const view = open('operations');
    expect(view.get('[data-testid="log-list"]').textContent).toContain('结果 · 演示样本已记录');
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
    expect(view.get('[data-testid="log-detail"]').textContent).toContain('保留最后成功样本');
    expect(view.get('[data-testid="log-detail"]').textContent).toContain('固定原因码');
    expect(view.get('[data-testid="log-detail"]').textContent).toContain('funding_fee_observation_delayed');
    expect(view.get('[data-testid="log-detail"]').textContent).toContain('0 毫秒');
  });

  it('筛选隐藏当前事件时将详情切换到首个可见事件', async () => {
    const view = open('operations');
    const filter = view.get<HTMLSelectElement>('select[aria-label="日志级别"]');

    filter.value = 'warning';
    filter.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();

    expect(view.get('[data-testid="log-detail"]').textContent).toContain('evt-1042');
    expect(view.get('[data-log="evt-1042"]').classList.contains('active')).toBe(true);
  });

  it('日志搜索无匹配时清除旧详情并在清空后恢复固定事件', async () => {
    const view = open('operations');
    const search = view.get<HTMLInputElement>('input[aria-label="日志搜索"]');

    search.value = '没有对应事件';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    await nextTick();

    expect(view.get('[data-testid="log-empty"]').textContent).toContain(
      '没有符合搜索条件的固定演示日志',
    );
    expect(view.get('[data-testid="log-list"]').querySelectorAll('[data-log]')).toHaveLength(0);
    expect(view.element.querySelector('[data-testid="log-detail"]')).toBeNull();

    search.value = '';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    await nextTick();

    expect(view.get('[data-testid="log-list"]').textContent).toContain('evt-1041');
    expect(view.get('[data-testid="log-detail"]').textContent).toContain('evt-1041');
    expect(view.element.querySelector('[data-testid="log-empty"]')).toBeNull();
  });

  it('通过固定演示事件 ID 定位历史日志并生成可复制的深链接', () => {
    const view = open('operations', 'evt-1042');

    expect(view.get('[data-testid="log-detail"]').textContent).toContain('evt-1042');
    expect(view.get('[data-testid="history-locator"]').getAttribute('href')).toBe(
      '/#/quant/operations?event=evt-1042',
    );
  });

  it('合法但不存在的历史事件显示缺失状态，不跳转到最新日志', () => {
    const view = open('operations', 'evt-9999');

    expect(view.get('[data-testid="history-missing"]').textContent).toContain('evt-9999');
    expect(view.element.querySelector('[data-testid="log-detail"]')).toBeNull();
  });

  it('格式错误的历史事件 ID 被拒绝并保留安全提示', () => {
    const view = open('operations', '../../evt-1041');

    expect(view.get('[data-testid="history-invalid"]').textContent).toContain('无效');
    expect(view.element.querySelector('[data-testid="log-detail"]')).toBeNull();
  });

  it('重复的历史事件 query 参数按无效输入处理', () => {
    const view = open('operations', ['evt-1041', 'evt-1042']);

    expect(view.get('[data-testid="history-invalid"]').textContent).toContain('无效');
    expect(view.element.querySelector('[data-testid="log-detail"]')).toBeNull();
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

  it('系统页展示固定服务状态样本并明确未探测真实运行状态', () => {
    const view = open('settings');

    expect(view.get('[data-testid="system-status-table"]').textContent).toContain(
      '组件状态',
    );
    expect(view.get('[data-testid="system-status-scope"]').textContent).toContain(
      '固定演示状态',
    );
    expect(view.get('[data-testid="system-status-table"]').textContent).toContain(
      '数据库',
    );
    expect(view.get('[data-testid="system-status-table"]').textContent).toContain(
      '未连接',
    );
    expect(view.get('[data-testid="system-status-scope"]').textContent).toContain(
      '不探测真实运行状态',
    );
    expect(view.get('[data-testid="system-status-table"]').textContent).toContain(
      '过期 · 仅供回看',
    );
    expect(view.get('[data-testid="system-status-table"]').textContent).toContain(
      '演示读取失败',
    );
    expect(view.get('[data-testid="system-status-table"]').textContent).toContain(
      '固定预置故障场景',
    );
    expect(
      view.get('[data-state="演示读取失败"]').textContent,
    ).toContain('演示读取失败');
    expect(view.get('[data-testid="system-status-scope"]').textContent).toContain(
      '不代表实时健康结论',
    );
  });
});

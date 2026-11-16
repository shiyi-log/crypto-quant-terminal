// @vitest-environment happy-dom
import type { App } from 'vue';

import { createApp, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('ant-design-vue', async () => {
  const { defineComponent, h } = await import('vue');
  const component = (tag: string) =>
    defineComponent({
      inheritAttrs: false,
      props: {
        value: { type: [String, Number, Array], default: undefined },
        checked: Boolean,
        options: Array,
        mode: String,
        open: Boolean,
        title: String,
        okText: String,
        cancelText: String,
        okButtonProps: Object,
        rows: Number,
        loading: Boolean,
        disabled: Boolean,
        type: String,
        danger: Boolean,
      },
      emits: ['update:value', 'update:checked', 'ok', 'cancel'],
      setup(props, { attrs, slots, emit }) {
        if (tag === 'modal')
          return () =>
            props.open
              ? h('div', { role: 'alertdialog' }, [
                  h('h2', props.title),
                  slots.default?.(),
                  h(
                    'button',
                    {
                      'data-confirm-command': '',
                      disabled: props.okButtonProps?.disabled,
                      onClick: () => emit('ok'),
                    },
                    props.okText || '确定',
                  ),
                  h(
                    'button',
                    { onClick: () => emit('cancel') },
                    props.cancelText || '取消',
                  ),
                ])
              : null;
        if (tag === 'select')
          return () =>
            h(
              'select',
              {
                ...attrs,
                multiple: props.mode === 'multiple',
                value: props.value,
                onChange: (event: Event) => {
                  const el = event.target as HTMLSelectElement;
                  emit(
                    'update:value',
                    props.mode === 'multiple'
                      ? [...el.selectedOptions].map((option) => option.value)
                      : el.value,
                  );
                },
              },
              (props.options || []).map((item: any) =>
                h('option', { value: item.value }, item.label),
              ),
            );
        if (tag === 'switch')
          return () =>
            h('input', {
              ...attrs,
              type: 'checkbox',
              checked: props.checked,
              onChange: (event: Event) =>
                emit(
                  'update:checked',
                  (event.target as HTMLInputElement).checked,
                ),
            });
        if (tag === 'input' || tag === 'input-number')
          return () =>
            h('input', {
              ...attrs,
              type: 'text',
              value: props.value,
              onInput: (event: Event) => {
                const value = (event.target as HTMLInputElement).value;
                emit(
                  'update:value',
                  tag === 'input-number' ? Number(value) : value,
                );
              },
            });
        if (tag === 'textarea')
          return () =>
            h('textarea', {
              ...attrs,
              rows: props.rows,
              value: props.value,
              onInput: (event: Event) =>
                emit(
                  'update:value',
                  (event.target as HTMLTextAreaElement).value,
                ),
            });
        if (tag === 'button')
          return () =>
            h(
              'button',
              {
                ...attrs,
                disabled: props.disabled || props.loading,
                type: props.type === 'submit' ? 'submit' : 'button',
              },
              slots.default?.(),
            );
        return () =>
          h('div', attrs, [
            slots.message?.(),
            slots.description?.(),
            slots.default?.(),
          ]);
      },
    });
  return {
    Alert: component('alert'),
    Button: component('button'),
    Input: component('input'),
    InputNumber: component('input-number'),
    Modal: component('modal'),
    Select: component('select'),
    Switch: component('switch'),
    Textarea: component('textarea'),
  };
});

const api = vi.hoisted(() => ({
  getBacktestStatus: vi.fn(),
  getBacktestHistory: vi.fn(),
  getWebStrategies: vi.fn(),
  getWebAvailablePairs: vi.fn(),
  getWhitelist: vi.fn(),
  postBacktest: vi.fn(),
  abortBacktest: vi.fn(),
  resetBacktest: vi.fn(),
  postDownloadData: vi.fn(),
  postLookaheadAnalysis: vi.fn(),
  postRecursiveAnalysis: vi.fn(),
  getBackgroundJob: vi.fn(),
  getLookaheadResult: vi.fn(),
  getRecursiveResult: vi.fn(),
  setBacktestNotes: vi.fn(),
  deleteBacktestHistory: vi.fn(),
}));
vi.mock('#/api/freqtrade', () => ({ ...api, errText: () => '读取失败' }));
const wrappers: Array<{ app: App; element: HTMLDivElement }> = [];
afterEach(() => {
  wrappers.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  });
  vi.clearAllMocks();
});
const row = {
  filename: 'result-a.json',
  strategy: 'StrategyA',
  run_id: 'run-a',
  backtest_start_time: 1,
  notes: '原始备注',
};
beforeEach(() => {
  api.getBacktestStatus.mockResolvedValue({
    running: false,
    status: 'ended',
    status_msg: '',
    step: '',
    progress: 0,
    backtest_result: {
      strategy: {
        StrategyA: {
          profit_total_abs: 12.34,
          profit_total: 0.1234,
          total_trades: 7,
        },
      },
    },
  });
  api.getBacktestHistory.mockResolvedValue([row]);
  api.getWebStrategies.mockResolvedValue({
    strategies: ['StrategyA', 'StrategyB'],
  });
  api.getWebAvailablePairs.mockResolvedValue({ pairs: ['BTC/USDT:USDT'] });
  api.getWhitelist.mockResolvedValue({ whitelist: ['BTC/USDT:USDT'] });
  api.postBacktest.mockResolvedValue({
    running: true,
    status: 'running',
    status_msg: '',
    step: '',
    progress: 0,
  });
  api.postDownloadData.mockResolvedValue({
    job_id: 'job-a',
    status: 'started',
  });
  api.setBacktestNotes.mockResolvedValue([{ ...row, notes: '新备注' }]);
});
const settle = async () => {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await nextTick();
};
async function page() {
  const { default: Component } = await import('./index.vue');
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:pathMatch(.*)*', component: Component }],
  });
  await router.push('/quant/tasks');
  await router.isReady();
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp(Component);
  app.use(router).mount(element);
  wrappers.push({ app, element });
  await settle();
  const get = <T extends HTMLElement = HTMLElement>(selector: string) => {
    const item = element.querySelector<T>(selector);
    if (!item) throw new Error('Missing rendered element: ' + selector);
    return item;
  };
  const click = async (selector: string) => {
    get(selector).click();
    await settle();
  };
  const input = async (selector: string, value: string) => {
    const item = get<HTMLInputElement>(selector);
    item.value = value;
    item.dispatchEvent(new Event('input', { bubbles: true }));
    item.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();
  };
  return { element, get, click, input };
}
describe('历史研究任务工作区', () => {
  it('进入只读，历史执行与研究/成本结论分开且无删除证据命令', async () => {
    const p = await page();
    expect(p.get('h1').textContent).toBe('研究任务队列');
    expect(p.element.textContent).toContain('成本完整度未知');
    expect(p.element.textContent).toContain('不构成研究通过');
    expect(p.get('[data-history-row]').textContent).toContain('run-a');
    expect(p.element.textContent).toContain('历史文件删除已退出正式研究入口');
    expect(p.element.textContent).toContain(
      '当前历史接口没有提供该 run_id 到回测明细的可验证映射',
    );
    expect(p.element.querySelector('.artifact-inspector a')).toBeNull();
    expect(api.postBacktest).not.toHaveBeenCalled();
    expect(api.abortBacktest).not.toHaveBeenCalled();
    expect(api.deleteBacktestHistory).not.toHaveBeenCalled();
  });
  it('回测保留完整字段且只在用户提交后发命令', async () => {
    const p = await page();
    await p.input('[name=bt-strategy]', 'StrategyB');
    await p.input('[name=bt-range]', '20260101-20260301');
    await p.input('[name=bt-detail]', '5m');
    await p.input('[name=bt-wallet]', '1000');
    await p.click('[data-submit-backtest]');
    expect(api.postBacktest).toHaveBeenCalledWith(
      expect.objectContaining({
        strategy: 'StrategyB',
        timerange: '20260101-20260301',
        timeframe_detail: '5m',
        dry_run_wallet: 1000,
        enable_protections: false,
      }),
    );
    expect(p.get('[data-command-outcome]').textContent).toContain('提交已响应');
  });
  it('erase下载先冻结用户确认参数，表单后改动不会扩展确认范围', async () => {
    const p = await page();
    await p.click('[data-tool=download]');
    await p.input('[name=dl-mode]', 'manual');
    await p.input('[name=dl-manual]', 'BTC/USDT:USDT');
    await p.click('[name=dl-erase]');
    await p.click('[data-submit-download]');
    expect(api.postDownloadData).not.toHaveBeenCalled();
    expect(p.get('[role=alertdialog]').textContent).toContain('BTC/USDT:USDT');
    await p.input('[name=dl-manual]', 'ETH/USDT:USDT');
    await p.click('[role=alertdialog] [data-confirm-command]');
    expect(api.postDownloadData).toHaveBeenCalledWith(
      expect.objectContaining({ pairs: ['BTC/USDT:USDT'], erase: true }),
    );
  });
  it('备注保存保留文件和策略身份，刷新失败保留旧历史', async () => {
    const p = await page();
    await p.click('[data-history-row]');
    await p.input('[name=history-notes]', '新备注');
    await p.click('[data-save-notes]');
    expect(api.setBacktestNotes).toHaveBeenCalledWith(
      'result-a.json',
      'StrategyA',
      '新备注',
    );
    api.getBacktestHistory.mockRejectedValueOnce(
      new Error('postgresql://u:secret@private/db'),
    );
    await p.click('[data-testid=refresh-tasks]');
    expect(p.get('[data-history-row]').textContent).toContain('run-a');
    expect(p.element.textContent).not.toContain('secret');
    expect(p.element.textContent).toContain('保留');
  });
  it('未知回执跨刷新保留锁，核对并接受重复风险后仍需另行点击提交', async () => {
    const p = await page();
    api.postBacktest.mockRejectedValueOnce(new Error('response lost'));
    await p.input('[name=bt-strategy]', 'StrategyA');
    await p.click('[data-submit-backtest]');
    expect(p.element.textContent).toContain('命令结果未知');
    expect(p.get('[data-submit-backtest]').hasAttribute('disabled')).toBe(true);
    await p.click('[data-unknown-kind=backtest]');
    expect(p.get('[role=alertdialog]').textContent).toContain('后台核对');
    p.get('[data-recovery-reviewed]').click();
    await settle();
    await p.input('[name=recovery-phrase]', '接受重复执行风险');
    await p.click('[role=alertdialog] [data-confirm-command]');
    expect(p.get('[data-submit-backtest]').hasAttribute('disabled')).toBe(
      false,
    );
    expect(api.postBacktest).toHaveBeenCalledTimes(1);
  });
});

import type { App } from 'vue';

import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApp, defineComponent, h, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

const api = vi.hoisted(() => ({
  status: vi.fn(),
  rows: vi.fn(),
  evidence: vi.fn(),
  save: vi.fn(),
  check: vi.fn(),
  command: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
}));
vi.mock('#/api/binance-demo', () => ({
  getBinanceDemoStatus: api.status,
  saveBinanceDemoCredentials: api.save,
  checkBinanceDemoConnection: api.check,
  adoptBinanceDemoSession: api.command,
  attachBinanceDemoStrategy: api.command,
  startBinanceDemo: api.command,
  startBinanceDemoSession: api.command,
  stopBinanceDemo: api.command,
  stopBinanceDemoSession: api.command,
  switchBinanceDemo: api.command,
}));
vi.mock('../settingsApi', () => ({
  readDemoAccount: api.rows,
  readDemoComparisons: api.rows,
  readDemoEvidence: api.evidence,
  readDemoFills: api.rows,
  readDemoOrders: api.rows,
  readDemoPositions: api.rows,
  readDemoSessions: api.rows,
  readDemoStrategies: api.rows,
}));
vi.mock('ant-design-vue', () => {
  const box = defineComponent({
    setup:
      (_, { slots }) =>
      () =>
        h('div', slots.default?.()),
  });
  const field = defineComponent({
    props: ['value', 'disabled', 'placeholder'],
    emits: ['update:value'],
    setup:
      (props, { emit }) =>
      () =>
        h('input', {
          value: props.value,
          disabled: props.disabled,
          placeholder: props.placeholder,
          onInput: (event: Event) =>
            emit('update:value', (event.target as HTMLInputElement).value),
        }),
  });
  const form = Object.assign(
    defineComponent({
      emits: ['finish'],
      setup:
        (_, { slots, emit }) =>
        () =>
          h(
            'form',
            {
              onSubmit: (event: Event) => {
                event.preventDefault();
                emit('finish');
              },
            },
            slots.default?.(),
          ),
    }),
    { Item: box },
  );
  const descriptions = Object.assign(box, { Item: box });
  const tabs = Object.assign(box, { TabPane: box });
  return {
    Alert: defineComponent({
      props: ['message', 'description'],
      setup: (props) => () =>
        h('div', { role: 'alert' }, [
          String(props.message ?? ''),
          String(props.description ?? ''),
        ]),
    }),
    Card: box,
    Button: defineComponent({
      props: ['disabled', 'htmlType'],
      setup:
        (props, { slots }) =>
        () =>
          h(
            'button',
            { disabled: props.disabled, type: props.htmlType ?? 'button' },
            slots.default?.(),
          ),
    }),
    Descriptions: descriptions,
    Empty: box,
    Form: form,
    Input: { Password: field },
    message: { success: api.success, error: api.error },
    Popconfirm: box,
    Select: box,
    Table: defineComponent({
      props: ['dataSource'],
      setup: (props) => () =>
        h('div', { 'data-table': '' }, JSON.stringify(props.dataSource)),
    }),
    Tabs: tabs,
    Tag: box,
  };
});
const mounted: { app: App; element: HTMLDivElement }[] = [];
const status = () => ({
  environment: 'binance_demo',
  endpoint: 'demo-fapi.binance.com',
  credentials: { configured: true, fingerprint: 'key-a' },
  runner: {
    state: 'paused',
    virtualAccountKey: 'session-a',
    selectedAccountId: 'strategy-a',
  },
  accounts: [
    {
      accountId: 'strategy-a',
      strategyId: 'strategy-a',
      enabled: true,
      eligible: false,
      ruleHash: 'r',
      executionHash: 'x',
    },
  ],
  metrics: {},
  limitations: [],
  switchSupported: true,
});
const evidence = () => ({
  environment: 'binance_demo',
  endpoint: 'demo-fapi.binance.com',
  scope: 'selected_candle_strategy_execution_only',
  questions: {
    fees: { state: 'no_actual_fills', rates: [], chargedByAsset: [] },
    secondsProfit: {},
    execution: {},
  },
});
async function settle() {
  for (let i = 0; i < 15; i++) await Promise.resolve();
  await nextTick();
}
async function open() {
  const { default: Component } = await import('../index.vue');
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:pathMatch(.*)*', component: Component }],
  });
  await router.push('/quant/binance-demo');
  await router.isReady();
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp(Component);
  app.use(router).mount(element);
  mounted.push({ app, element });
  await settle();
  return {
    element,
    refresh: async () => {
      [...element.querySelectorAll('button')]
        .find((button) => button.textContent?.includes('刷新记录'))!
        .click();
      await settle();
    },
  };
}
afterEach(() => {
  mounted.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  });
  vi.resetAllMocks();
});
function setup() {
  api.status.mockImplementation(async () => status());
  api.rows.mockResolvedValue({
    items: [{ orderId: 'known-order', price: '123.000000000000000001' }],
  });
  api.evidence.mockImplementation(async () => evidence());
}

describe('Demo 设置会话工作区', () => {
  it('凭据指纹改变后失败的会话读取不能保留旧账户订单', async () => {
    setup();
    const { element, refresh } = await open();
    expect(element.textContent).toContain('known-order');
    api.status.mockImplementation(async () => ({
      ...status(),
      credentials: { configured: true, fingerprint: 'key-b' },
    }));
    api.rows.mockRejectedValue(new Error('offline'));
    api.evidence.mockRejectedValue(new Error('offline'));
    await refresh();
    expect(element.textContent).toContain('key-b');
    expect(element.textContent).not.toContain('known-order');
  });
  it('风险状态与拒绝原因不回显未知上游秘密字符串', async () => {
    setup();
    api.status.mockImplementation(async () => ({
      ...status(),
      runner: {
        ...status().runner,
        risk_alert: 'secret=private-risk',
        ownership_conflicts: ['apiKey=private-conflict'],
      },
      accounts: [
        { ...status().accounts[0], blockedReason: 'secret=private-blocked' },
      ],
    }));
    const { element } = await open();
    expect(element.textContent).not.toContain('private-risk');
    expect(element.textContent).not.toContain('private-conflict');
    expect(element.textContent).not.toContain('private-blocked');
    expect(element.textContent).toContain('风险保护待核对');
  });
  it('记录或证据读取失败后不能利用旧资格开始新增风险', async () => {
    setup();
    api.status.mockImplementation(async () => ({
      ...status(),
      accounts: [{ ...status().accounts[0], eligible: true }],
      runner: { ...status().runner, state: 'configured' },
    }));
    const { element, refresh } = await open();
    const startButton = () =>
      [...element.querySelectorAll<HTMLButtonElement>('button')].find(
        (button) => button.textContent?.includes('手动开始 Demo'),
      )!;
    expect(startButton().disabled).toBe(false);
    api.rows.mockRejectedValue(new Error('offline'));
    await refresh();
    expect(startButton().disabled).toBe(true);
  });
  it('进入页面只读，读取失败保留原会话事实并标示失败', async () => {
    setup();
    const { element, refresh } = await open();
    expect(element.textContent).toContain('known-order');
    expect(api.command).not.toHaveBeenCalled();
    expect(api.save).not.toHaveBeenCalled();
    expect(api.check).not.toHaveBeenCalled();
    api.rows.mockRejectedValue(new Error('offline'));
    await refresh();
    expect(element.textContent).toContain('known-order');
    expect(element.textContent).toContain('读取失败');
  });
  it('整组读取后身份变化时不覆盖旧会话，也不把另一会话记录贴到当前身份', async () => {
    setup();
    const { element, refresh } = await open();
    api.rows.mockResolvedValue({ items: [{ orderId: 'wrong-new-order' }] });
    let calls = 0;
    api.status.mockImplementation(async () => {
      calls += 1;
      return calls === 1
        ? status()
        : {
            ...status(),
            runner: { ...status().runner, virtualAccountKey: 'session-b' },
          };
    });
    await refresh();
    expect(element.textContent).toContain('known-order');
    expect(element.textContent).not.toContain('wrong-new-order');
    expect(element.textContent).toContain('状态尚未确认');
  });
  it('重复保存仅发一次凭据命令，成功后输入清空，页面没有自动启用交易', async () => {
    setup();
    const { element } = await open();
    let resolve!: () => void;
    api.save.mockImplementation(
      () =>
        new Promise<void>((done) => {
          resolve = done;
        }),
    );
    const inputs = [...element.querySelectorAll<HTMLInputElement>('input')];
    for (const [i, input] of inputs.entries()) {
      input.value = i === 0 ? 'private-key' : 'private-secret';
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }
    await nextTick();
    const form = element.querySelector('form')!;
    form.dispatchEvent(new Event('submit'));
    form.dispatchEvent(new Event('submit'));
    await settle();
    expect(api.save).toHaveBeenCalledTimes(1);
    resolve();
    await settle();
    expect(inputs.every((input) => input.value === '')).toBe(true);
    expect(api.command).not.toHaveBeenCalled();
  });
});

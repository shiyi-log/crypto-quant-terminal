import { describe, expect, it } from 'vitest';

import {
  createReadState,
  parseEvents,
  parseHealth,
  parseServices,
  refreshReadState,
} from '../operationsEvidence';

const component = {
  key: 'collection',
  label: '行情采集',
  state: 'ok',
  updated_at: '2026-11-16T01:00:00Z',
  last_success_at: '2026-11-16T01:00:00Z',
  age_seconds: 2,
  error_codes: [],
  metrics: { persisted: 0 },
};
const health = () => ({
  generated_at: '2026-11-16T01:00:02Z',
  ok: true,
  components: [component],
});
const services = () => ({
  generated_at: '2026-11-16 09:00:02',
  running: 1,
  total: 1,
  services: [
    {
      key: 'collector',
      name: '行情采集',
      running: true,
      pid: '123',
      port: null,
    },
  ],
});
const events = () => ({
  total: 1,
  events: [
    {
      event_id: 'event-1',
      timestamp: '2026-11-16T01:00:00Z',
      event_type: 'research_started',
      research_id: 'run-1',
      payload: { sha256: 'a'.repeat(64), reason: '等待成本证据' },
    },
  ],
});

describe('运维事实合同', () => {
  it('保留零指标和真实停止进程，不把服务正常当作账户授权', () => {
    expect(parseHealth(health()).components[0]?.metrics.persisted).toBe(0);
    expect(parseHealth(health())).not.toHaveProperty('entries_allowed');
    const value = services();
    value.running = 0;
    value.services[0]!.running = false;
    expect(parseServices(value).services[0]?.running).toBe(false);
  });
  it.each([
    null,
    {},
    { ...health(), components: null },
    { ...health(), components: [component, component] },
    { ...health(), components: [{ ...component, age_seconds: Infinity }] },
  ])('拒绝损坏健康响应 %j', (value) => {
    expect(() => parseHealth(value)).toThrow();
  });
  it('汇总状态不能掩盖子组件故障', () => {
    expect(
      parseHealth({
        ...health(),
        components: [{ ...component, state: 'failed' }],
      }).ok,
    ).toBe(false);
  });
  it('保留后端负年龄的时钟偏差事实，拒绝数组状态', () => {
    expect(
      parseHealth({
        ...health(),
        components: [
          {
            ...component,
            state: 'failed',
            age_seconds: -31,
            error_codes: ['status_clock_skew'],
          },
        ],
      }).components[0]?.age_seconds,
    ).toBe(-31);
    expect(() =>
      parseHealth({
        ...health(),
        components: [{ ...component, state: ['ok'] }],
      }),
    ).toThrow();
  });
  it.each([
    null,
    {},
    { ...services(), running: 2 },
    { ...services(), services: [{ key: 'x', name: 'x', running: 'yes' }] },
    { ...services(), total: 0 },
  ])('拒绝损坏服务统计 %j', (value) => {
    expect(() => parseServices(value)).toThrow();
  });
  it('明确成功的空事件与缺失内容不同', () => {
    expect(parseEvents({ events: [], total: 0 })).toEqual({
      events: [],
      total: 0,
    });
    expect(() => parseEvents({ total: 0 })).toThrow();
    expect(() => parseEvents({ events: [], total: -1 })).toThrow();
  });
  it('只投影安全事件字段，未知hash不伪造，通过事件不自动授予交易资格', () => {
    const source = events();
    source.events[0]!.payload = {
      secret: 'must-not-render',
      sha256: 'a'.repeat(64),
      reason: '等待成本证据',
    } as never;
    const parsed = parseEvents(source);
    expect(parsed.events[0]).toMatchObject({
      hash: 'a'.repeat(64),
      reason: '等待成本证据',
    });
    expect(parsed.events[0]).not.toHaveProperty('payload');
    expect(JSON.stringify(parsed)).not.toContain('must-not-render');
    expect(
      parseEvents({
        events: [{ ...events().events[0], payload: {} }],
        total: 1,
      }).events[0]?.hash,
    ).toBeNull();
  });
  it('重复事件身份和坏日期不能伪装成新证据', () => {
    expect(() =>
      parseEvents({
        ...events(),
        total: 2,
        events: [events().events[0], events().events[0]],
      }),
    ).toThrow();
    expect(() =>
      parseEvents({
        ...events(),
        events: [{ ...events().events[0], timestamp: 'bad' }],
      }),
    ).toThrow();
  });
  it('拒绝不存在的日期、数字日期和空研究身份', () => {
    for (const timestamp of ['2026-02-30T00:00:00Z', '0'])
      expect(() =>
        parseEvents({
          ...events(),
          events: [{ ...events().events[0], timestamp }],
        }),
      ).toThrow();
    expect(() =>
      parseEvents({
        ...events(),
        events: [{ ...events().events[0], research_id: '' }],
      }),
    ).toThrow();
  });
});

describe('只读刷新与选择一致性', () => {
  it('读取失败和损坏响应保留上次成功值并标为旧数据', async () => {
    const state = createReadState<ReturnType<typeof parseEvents>>();
    await refreshReadState(state, 'run-1', async () => events(), parseEvents);
    await refreshReadState(
      state,
      'run-1',
      async () => {
        throw new Error('network secret');
      },
      parseEvents,
    );
    expect(state.value?.events[0]?.eventId).toBe('event-1');
    expect(state.error).toBe('读取失败，保留上次成功事实');
    expect(state.error).not.toContain('secret');
    await refreshReadState(state, 'run-1', async () => ({}), parseEvents);
    expect(state.value?.total).toBe(1);
    expect(state.status).toBe('error');
  });
  it('迟到的旧筛选不能覆盖新选择，也不能结束新请求加载状态', async () => {
    const state = createReadState<ReturnType<typeof parseEvents>>();
    let release!: (value: unknown) => void;
    const old = refreshReadState(
      state,
      'old',
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
      parseEvents,
    );
    await refreshReadState(
      state,
      'new',
      async () => ({ events: [], total: 0 }),
      parseEvents,
    );
    release(events());
    await old;
    expect(state.value).toEqual({ events: [], total: 0 });
    expect(state.valueKey).toBe('new');
    expect(state.status).toBe('ready');
  });
  it('新选择在失败时仍明确标记旧值所属筛选，取消信号已发送', async () => {
    const state = createReadState<ReturnType<typeof parseEvents>>();
    let signal!: AbortSignal;
    await refreshReadState(
      state,
      'run-1',
      async (s) => {
        signal = s;
        return events();
      },
      parseEvents,
    );
    await refreshReadState(
      state,
      'run-2',
      async () => {
        throw new Error('offline');
      },
      parseEvents,
    );
    expect(signal.aborted).toBe(true);
    expect(state.requestKey).toBe('run-2');
    expect(state.valueKey).toBe('run-1');
  });
});

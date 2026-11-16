import { describe, expect, it } from 'vitest';

import {
  assertSameDemoIdentity,
  createDemoCommandGate,
  parseDemoSettingsRows,
  parseDemoSettingsStatus,
  retainedDemoSlice,
  demoSettingsError,
} from '../settingsContract';

function status() {
  return {
    environment: 'binance_demo',
    endpoint: 'demo-fapi.binance.com',
    credentials: { configured: true, fingerprint: 'key-a' },
    runner: {
      state: 'paused',
      selectedAccountId: 'strategy-a',
      virtualAccountKey: 'session-a',
    },
    accounts: [
      {
        accountId: 'strategy-a',
        strategyId: 'strategy-a',
        enabled: true,
        eligible: false,
        ruleHash: 'rules',
        executionHash: 'exit',
      },
    ],
    metrics: {},
    limitations: [],
  };
}

describe('Demo 设置事实读取', () => {
  it('缺失或损坏列表不能显示成空账本，环境也不能跨越', () => {
    expect(() =>
      parseDemoSettingsRows({ environment: 'binance_demo' }),
    ).toThrow();
    expect(() =>
      parseDemoSettingsRows({ environment: 'binance_demo', items: [null] }),
    ).toThrow();
    expect(() =>
      parseDemoSettingsRows({ environment: 'live', items: [] }),
    ).toThrow();
    expect(
      parseDemoSettingsRows({
        environment: 'binance_demo',
        items: [],
        limit: 100,
      }).items,
    ).toEqual([]);
    expect(
      parseDemoSettingsRows({
        environment: 'binance_demo',
        items: [{ price: '1000.000000000000000001' }],
      }).items[0]!.price,
    ).toBe('1000.000000000000000001');
  });

  it('未知凭据和策略开关不强制转换为可操作状态', () => {
    const raw = status();
    expect(parseDemoSettingsStatus(raw).accounts[0]!.eligible).toBe(false);
    expect(() =>
      parseDemoSettingsStatus({ ...raw, credentials: { configured: 'false' } }),
    ).toThrow();
    expect(() => parseDemoSettingsStatus({ ...raw, runner: null })).toThrow();
    expect(() =>
      parseDemoSettingsStatus({
        ...raw,
        accounts: [raw.accounts[0], raw.accounts[0]],
      }),
    ).toThrow();
    expect(() =>
      parseDemoSettingsStatus({ ...raw, endpoint: 'fapi.binance.com' }),
    ).toThrow();
  });

  it.each(['virtualAccountKey', 'selectedAccountId'])(
    '读取前后 %s 改变时拒绝整组迟到事实',
    (key) => {
      const before = parseDemoSettingsStatus(status());
      const after = parseDemoSettingsStatus({
        ...status(),
        runner: { ...status().runner, [key]: 'different' },
      });
      expect(() => assertSameDemoIdentity(before, after)).toThrow();
      expect(() =>
        assertSameDemoIdentity(
          before,
          parseDemoSettingsStatus({
            ...status(),
            credentials: { configured: true, fingerprint: 'key-b' },
          }),
        ),
      ).toThrow();
    },
  );

  it('同一范围失败保留旧值，切换范围失败必须变为未知以免串账', () => {
    const old = [{ orderId: 'old-session-order' }];
    expect(
      retainedDemoSlice(
        old,
        { status: 'rejected', reason: new Error('offline') },
        'a',
        'a',
        [],
      ),
    ).toEqual(old);
    expect(
      retainedDemoSlice(
        old,
        { status: 'rejected', reason: new Error('offline') },
        'a',
        'b',
        [],
      ),
    ).toEqual([]);
    expect(
      retainedDemoSlice(old, { status: 'fulfilled', value: [] }, 'a', 'a', []),
    ).toEqual([]);
  });
});

describe('Demo 设置命令单次用户动作', () => {
  it('并发确认仅执行一次；失败不自动重发', async () => {
    const gate = createDemoCommandGate();
    let calls = 0;
    let resolve!: () => void;
    const request = gate.run(() => {
      calls += 1;
      return new Promise<void>((done) => {
        resolve = done;
      });
    });
    expect(
      await gate.run(async () => {
        calls += 1;
      }),
    ).toBe(false);
    resolve();
    expect(await request).toBe(true);
    expect(calls).toBe(1);
    await expect(
      gate.run(async () => {
        calls += 1;
        throw new Error('unknown');
      }),
    ).rejects.toThrow('unknown');
    expect(calls).toBe(2);
    expect(gate.busy()).toBe(false);
  });
  it('销毁后拒绝后续命令，已在途结果不会授予新的动作', async () => {
    const gate = createDemoCommandGate();
    gate.dispose();
    let calls = 0;
    expect(
      await gate.run(async () => {
        calls += 1;
      }),
    ).toBe(false);
    expect(calls).toBe(0);
  });
});

describe('设置页错误脱敏', () => {
  it('未知服务器字符串不回显可能包含凭据的请求体，保留已知拒绝原因', () => {
    expect(
      demoSettingsError(new Error('apiKey=private-key&secret=private-secret')),
    ).not.toContain('private');
    expect(
      demoSettingsError({
        response: { data: { detail: 'demo_account_positions_not_flat' } },
      }),
    ).toContain('不会自动平仓');
    expect(
      demoSettingsError({
        response: { data: { detail: 'demo_future_secret_echo_private' } },
      }),
    ).not.toContain('private');
  });
});

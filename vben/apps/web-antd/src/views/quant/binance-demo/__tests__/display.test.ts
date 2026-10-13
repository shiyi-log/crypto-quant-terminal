import type { DemoStatus, DemoStrategySession } from '../../../../api/binance-demo';

import { describe, expect, it, vi } from 'vitest';

import { canAdoptDemoSession, canAttachDemo, canStartDemo, canStartDemoSession, canSwitchDemo, comparisonObservation, comparisonPriceText, decimalText, demoError, demoOwnedPositionRows, demoReadSession, demoSessionLabel, demoState, demoStrategySessionState, exchangeTime, feeText, fillSide, isComparisonActivity, makerText, orderRecordStatus, orderStatus, pinDemoSessions, quotedRateText, selectedDemoAccount, submitCredentials, unfilledRatio } from '../display';

function status(): DemoStatus {
  return {
    environment: 'binance_demo', endpoint: 'demo-fapi.binance.com',
    credentials: { configured: true }, runner: { state: 'configured', tradingEnabled: false },
    accounts: [{ accountId: 's03_R50_k1.5', strategyId: 's03', enabled: true, eligible: true, ruleHash: 'rule', executionHash: 'execution' }],
    metrics: {}, limitations: [],
  };
}

function parallelStatus(): DemoStatus {
  const value = status();
  value.parallelSupported = true;
  value.runner = { state: 'running', selectedAccountId: 'old-identity', virtualAccountKey: 'demo_primary', tradingEnabled: true, workerRunning: true, accountLocked: true };
  value.accounts.push({ accountId: 'old-identity', strategyId: 'old', enabled: true, eligible: true, ruleHash: 'old-rule', executionHash: 'old-execution' });
  return value;
}

function strategySession(overrides: Partial<DemoStrategySession> = {}): DemoStrategySession {
  return {
    virtualAccountKey: 'demo_parallel', strategyId: 's03_R50_k1.5',
    primary: false, member: true, entriesEnabled: false, running: true,
    status: 'paused_protecting', ownedPositions: [], metrics: {}, roundTrips: [],
    ...overrides,
  };
}

describe('Demo exchange report presentation', () => {
  it('preserves decimal string precision and never converts an absent fee to zero', () => {
    expect(decimalText('0.00000000')).toBe('0.00000000');
    expect(decimalText('12345678901234567890.12345678')).toBe('12345678901234567890.12345678');
    expect(feeText({ commission: '0.00000000', commissionAsset: 'USDT' })).toBe('0.00000000 USDT');
    expect(feeText({ commission: null, commissionAsset: 'USDT' })).toBe('未知（成交回报未提供）');
    expect(decimalText(0)).toBe('未知');
    expect(makerText(null)).toBe('未知');
    expect(makerText(false)).toBe('Taker');
    expect(orderStatus('PARTIALLY_FILLED')).toBe('部分成交 · PARTIALLY_FILLED');
    expect(orderRecordStatus({ status: 'NEW', demoAudit: { status: 'unknown' } })).toContain('勿重复下单');
    expect(orderRecordStatus({ status: null, demoAudit: { status: 'intent' } })).toBe('本机准备提交');
    expect(orderRecordStatus({ status: 'NEW', demoAudit: { status: 'open' } })).toBe('挂单中 · NEW');
    expect(fillSide({ buyer: false })).toBe('SELL');
    expect(fillSide({ buyer: true })).toBe('BUY');
    expect(exchangeTime('2026-10-12T02:00:00+08:00')).not.toBe('—');
  });

  it('requires a configured, eligible exact account and does not pretend an unconfigured service is connected', () => {
    const value = status();
    expect(canStartDemo(value, 's03_R50_k1.5')).toBe(true);
    expect(canStartDemo(value, 'other-identity')).toBe(false);
    value.accounts[0]!.eligible = false;
    expect(canStartDemo(value, 's03_R50_k1.5')).toBe(false);
    value.credentials.configured = false;
    value.runner.state = 'connected';
    expect(demoState(value).label).toBe('待配置');
    expect(canStartDemo(value, 's03_R50_k1.5')).toBe(false);
  });

  it('shows exact quoted fee percentages and keeps missing quotations unknown', () => {
    expect(quotedRateText('0.00020000')).toBe('0.02%（原值 0.00020000）');
    expect(quotedRateText('0.00050000')).toBe('0.05%（原值 0.00050000）');
    expect(quotedRateText('0.00000000')).toBe('0%（原值 0.00000000）');
    expect(quotedRateText('-0.00000001')).toBe('-0.000001%（原值 -0.00000001）');
    expect(quotedRateText('0E-8')).toBe('0%（原值 0E-8）');
    expect(quotedRateText('1E-10')).toBe('0.00000001%（原值 1E-10）');
    expect(quotedRateText('-1E-6')).toBe('-0.0001%（原值 -1E-6）');
    expect(quotedRateText(null)).toBe('未知');
    expect(quotedRateText(0.0002)).toBe('未知');
  });

  it('keeps a protected session locked to its selected identity and blocks unavailable decryption', () => {
    const value = status();
    value.runner = { state: 'paused_protecting', workerRunning: true, accountLocked: true, selectedAccountId: 'other-identity', tradingEnabled: false };
    expect(canStartDemo(value, 's03_R50_k1.5')).toBe(false);
    expect(demoState(value).label).toBe('已暂停新开仓');
    value.runner.accountLocked = false;
    value.credentials.decryptionAvailable = false;
    expect(canStartDemo(value, 's03_R50_k1.5')).toBe(false);
    expect(demoState(value).label).toBe('配置暂不可读取');
  });

  it('keeps start bound to the current identity even after the worker yields the account lock', () => {
    const value = status();
    value.runner = { state: 'yielded_flat', selectedAccountId: 'old-identity', accountLocked: false, tradingEnabled: false };
    expect(canStartDemo(value, 's03_R50_k1.5')).toBe(false);
    value.runner.selectedAccountId = 's03_R50_k1.5';
    expect(canStartDemo(value, 's03_R50_k1.5')).toBe(true);
    value.runner.switchPersistenceUnresolved = true;
    expect(canStartDemo(value, 's03_R50_k1.5')).toBe(false);
    expect(demoState(value).label).toBe('切换状态待核对');
  });

  it('allows requesting a different eligible strategy while running but requires the backend switch capability', () => {
    const value = status();
    value.runner = { state: 'running', selectedAccountId: 'old-identity', tradingEnabled: true, workerRunning: true, accountLocked: true };
    expect(canSwitchDemo(value, 's03_R50_k1.5')).toBe(false);
    value.switchSupported = true;
    expect(canSwitchDemo(value, 's03_R50_k1.5')).toBe(true);
    expect(canStartDemo(value, 's03_R50_k1.5')).toBe(false);
    expect(canSwitchDemo(value, 'old-identity')).toBe(false);
    expect(canSwitchDemo(value, 'unknown-identity')).toBe(false);
    value.accounts[0]!.eligible = false;
    expect(canSwitchDemo(value, 's03_R50_k1.5')).toBe(false);
    value.accounts[0]!.eligible = true;
    value.runner.handoffInProgress = true;
    expect(canSwitchDemo(value, 's03_R50_k1.5')).toBe(false);
    value.runner.handoffInProgress = false;
    value.runner.switchPersistenceUnresolved = true;
    expect(canSwitchDemo(value, 's03_R50_k1.5')).toBe(false);
    value.runner.switchPersistenceUnresolved = false;
    value.credentials.decryptionAvailable = false;
    expect(canSwitchDemo(value, 's03_R50_k1.5')).toBe(false);
  });

  it('preserves a switch candidate across worker polls and labels historical records without exposing ledger keys', () => {
    const value = status();
    value.runner = { state: 'running', selectedAccountId: 'old-identity', workerRunning: true, accountLocked: true };
    expect(selectedDemoAccount(value, 's03_R50_k1.5')).toBe('s03_R50_k1.5');
    expect(selectedDemoAccount(value, '')).toBe('old-identity');
    expect(selectedDemoAccount(value, 'removed-identity')).toBe('old-identity');
    value.accounts[0]!.description = '均值回归';
    const label = demoSessionLabel({ virtualAccountKey: 'demo_strategy_private-key', strategyId: 's03_R50_k1.5', forwardBoundaryMs: 1791774000000, active: false }, value.accounts);
    expect(label).toContain('均值回归');
    expect(label).toContain(exchangeTime(1791774000000));
    expect(label).not.toContain('private-key');
  });

  it('explains switch blockers without claiming a position was closed or a switch succeeded', () => {
    expect(demoError({ response: { data: { detail: 'demo_account_positions_not_flat' } } })).toContain('不会自动平仓');
    expect(demoError({ response: { data: { detail: 'demo_owned_fills_unresolved' } } })).toContain('成交数量尚未完全对账');
    expect(demoError({ response: { data: { detail: 'demo_account_open_orders_unresolved' } } })).toContain('挂单');
    expect(demoError({ response: { data: { detail: 'demo_switch_persistence_failed' } } })).toContain('旧会话保持暂停并继续保护');
  });

  it('pins a refresh to its captured active ledger even if the session list observes a later switch', () => {
    const captured = status();
    captured.runner.virtualAccountKey = 'demo_default';
    const laterSessions = [
      { virtualAccountKey: 'demo_default', strategyId: 'old-strategy', active: false },
      { virtualAccountKey: 'demo_strategy_new', strategyId: 'new-strategy', active: true },
    ];
    expect(demoReadSession(captured, '')).toBe('demo_default');
    expect(demoReadSession(captured, 'demo_strategy_history')).toBe('demo_strategy_history');
    expect(pinDemoSessions(captured, laterSessions).map((session) => session.active)).toEqual([true, false]);
    expect(laterSessions.map((session) => session.active)).toEqual([false, true]);
    delete captured.runner.virtualAccountKey;
    expect(demoReadSession(captured, '')).toBeUndefined();
    expect(demoReadSession(captured, 'demo_strategy_history')).toBe('demo_strategy_history');
    expect(pinDemoSessions(captured, laterSessions)).toEqual(laterSessions);
  });

  it('does not hide reconciliation or protection failures behind an active trading flag', () => {
    const value = status();
    value.runner = { state: 'running', tradingEnabled: true, lastError: 'ExchangeError' };
    expect(demoState(value).label).toBe('运行异常');
    value.runner.lastError = null;
    value.runner.risk_alert = 'actual_demo_entry_price_unavailable';
    expect(demoState(value).label).toBe('风险保护待恢复');
    delete value.runner.risk_alert;
    value.runner.ownership_conflicts = ['unowned_open_order'];
    expect(demoState(value).label).toBe('仓位归属待核对');
  });

  it('does not pretend a signal intent has filled or zero samples have a zero unfilled rate', () => {
    expect(unfilledRatio(null)).toBe('未知');
    expect(unfilledRatio(0)).toBe('0.0%');
    const row = comparisonObservation({ signal_id: 'event', stage: 'paper_event', observed_ms: 1000, payload: { paper_event_type: 'order_submitted', paper_event: { pair: 'BTC/USDT:USDT', price: 100 } } });
    expect(row.internalPrice).toBeNull();
    expect(row.pair).toBe('BTC/USDT:USDT');
    expect(comparisonObservation({ payload: { paper_event_type: 'order_filled', paper_event: { fill_price: 100 } } }).internalPrice).toBe('100');
  });

  it('keeps routine logs available for audit while preserving execution failures and unfamiliar stages in the activity view', () => {
    const rows = ['binding', 'runner_checkpoint', 'actual_execution_config', 'paper_event', 'entry_denied', 'entry_expiry', 'submission_error', 'snapshot_error', 'future_execution_stage'].map((stage) => ({ stage }));
    const activity = rows.filter(isComparisonActivity);
    expect(activity.map((row) => row.stage)).toEqual(['paper_event', 'entry_denied', 'entry_expiry', 'submission_error', 'snapshot_error', 'future_execution_stage']);
    expect(rows).toHaveLength(9);
  });

  it('shows non-fill prices as inapplicable rather than unknown and does not turn submitted prices into fills', () => {
    for (const row of [
      { stage: 'runner_checkpoint', payload: {} },
      { stage: 'actual_execution_config', payload: {} },
      { stage: 'entry_denied', payload: { pair: 'BTC/USDT:USDT' } },
      { stage: 'paper_event', payload: { paper_event_type: 'order_submitted', paper_event: { price: 100, fill_price: 101 } } },
      { stage: 'paper_event', payload: { paper_event_type: 'order_cancelled', paper_event: { price: 100 } } },
    ]) {
      const observation = comparisonObservation(row);
      expect(observation.internalPrice).toBeNull();
      expect(comparisonPriceText(observation)).toBe('—');
    }
    expect(comparisonObservation({ stage: 'runner_checkpoint' }).stageLabel).toBe('运行心跳');
  });

  it('distinguishes missing fill prices from recorded fill and closing prices without borrowing order prices', () => {
    const fill = comparisonObservation({ payload: { paper_event_type: 'order_filled', paper_event: { fill_price: '1234567890.12345678' } } });
    expect(comparisonPriceText(fill)).toBe('1234567890.12345678');
    const zeroFill = comparisonObservation({ payload: { paper_event_type: 'order_filled', paper_event: { fill_price: 0 } } });
    expect(comparisonPriceText(zeroFill)).toBe('0');
    const missing = comparisonObservation({ payload: { paper_event_type: 'order_filled', paper_event: { price: 100 } } });
    expect(comparisonPriceText(missing)).toBe('未记录');
    const close = comparisonObservation({ payload: { paper_event_type: 'trade_closed', paper_event: { entry_price: 100, exit_price: '105.10', exit_reason: 'stop_loss' } } });
    expect(comparisonPriceText(close)).toBe('105.10（平仓价）');
    expect(close.reason).toBe('stop_loss');
    const missingClose = comparisonObservation({ payload: { paper_event_type: 'trade_closed', paper_event: { entry_price: 100 } } });
    expect(comparisonPriceText(missingClose)).toBe('未记录');
    expect(comparisonObservation({ stage: 'snapshot_error', payload: { error_type: 'TimeoutError' } }).reason).toBe('TimeoutError');
    expect(comparisonObservation({ payload: { paper_event_type: 'order_cancelled', paper_event: { reason: 'position_already_closed' } } }).reason).toBe('position_already_closed');
  });

  it.each([false, true])('discards both secrets after a submission (rejected=%s)', async (rejected) => {
    const form = { apiKey: ' demo-key ', secret: ' demo-secret ' };
    const save = vi.fn(async () => {
      if (rejected) throw new Error('保存失败');
    });
    const pending = submitCredentials(form, save);
    if (rejected) await expect(pending).rejects.toThrow('保存失败');
    else await pending;
    expect(save).toHaveBeenCalledWith('demo-key', 'demo-secret');
    expect(form).toEqual({ apiKey: '', secret: '' });
  });

  it('never serializes an error request body containing secrets', () => {
    expect(demoError({ response: { data: { detail: { apiKey: 'private-key', secret: 'private-secret' } } }, config: { data: 'private-secret' } })).toBe('Demo 请求失败，请检查服务状态');
    expect(demoError({ response: { data: { detail: '连接失败' } } })).toBe('连接失败');
  });
});

describe('independent Demo strategy sessions', () => {
  it('shows a direct strategy entry intent without inventing a paper or exchange fill', () => {
    const row = comparisonObservation({ observation_id: 901, stage: 'strategy_signal', payload: {
      source_type: 'price_burst_continuation', paper_mirror: false, strategy_event_type: 'order_submitted',
      strategy_event: { pair: 'BTC/USDT:USDT', kind: 'entry', price: '101.123', reason: 'burst_continuation' },
    } });
    expect(row.stageLabel).toBe('策略信号');
    expect(row.eventLabel).toBe('开仓意图');
    expect(row.pair).toBe('BTC/USDT:USDT');
    expect(row.reason).toBe('burst_continuation');
    expect(row.paperEventType).toBeUndefined();
    expect(row.paperEventLabel).toBeUndefined();
    expect(row.strategyEventType).toBe('order_submitted');
    expect(row.paperMirror).toBe(false);
    expect(row.sourceType).toBe('price_burst_continuation');
    expect(row.internalPrice).toBeNull();
    expect(comparisonPriceText(row)).toBe('—');
  });

  it('does not promote a direct strategy price into a paper fill and preserves paper fill formatting', () => {
    const direct = comparisonObservation({ stage: 'strategy_signal', payload: { paper_mirror: false,
      strategy_event_type: 'order_filled', strategy_event: { fill_price: '120' },
      paper_event_type: 'order_filled', paper_event: { fill_price: '999' },
    } });
    expect(direct.internalPrice).toBeNull();
    expect(comparisonPriceText(direct)).toBe('—');
    const exit = comparisonObservation({ stage: 'strategy_signal', payload: { strategy_event_type: 'order_submitted',
      strategy_event: { kind: 'exit' } } });
    expect(exit.eventLabel).toBe('平仓意图');
    const paper = comparisonObservation({ stage: 'paper_event', payload: { paper_event_type: 'order_filled', paper_event: { fill_price: '100.050' } } });
    expect(paper.eventLabel).toBe('已成交');
    expect(paper.paperEventLabel).toBe('已成交');
    expect(comparisonPriceText(paper)).toBe('100.050');
  });

  it('can attach an eligible strategy while the primary is running without changing primary start rules', () => {
    const value = parallelStatus();
    expect(canAttachDemo(value, 's03_R50_k1.5', [])).toBe(true);
    expect(canStartDemo(value, 's03_R50_k1.5')).toBe(false);
    expect(canAttachDemo(value, 'old-identity', [])).toBe(false);
    expect(canAttachDemo(value, 'unknown', [])).toBe(false);
  });

  it('reserves strategy identity for active or paused members but ignores historical namespaces', () => {
    const value = parallelStatus();
    const member = strategySession();
    expect(canAttachDemo(value, member.strategyId, [member])).toBe(false);
    expect(canAttachDemo(value, member.strategyId, [strategySession({ entriesEnabled: true })])).toBe(false);
    expect(canAttachDemo(value, member.strategyId, [strategySession({ member: false, running: false })])).toBe(true);
    expect(canAttachDemo(value, member.strategyId, [strategySession({ primary: true, member: false })])).toBe(false);
  });

  it('starts a paused member independently of global trading and keeps the entry gate explicit', () => {
    const value = parallelStatus();
    expect(canStartDemoSession(value, strategySession())).toBe(true);
    expect(canStartDemoSession(value, strategySession({ entriesEnabled: true }))).toBe(false);
    expect(canStartDemoSession(value, strategySession({ entriesEnabled: undefined }))).toBe(false);
    expect(canStartDemoSession(value, strategySession({ member: false, status: 'observing' }))).toBe(false);
    expect(canStartDemoSession(value, strategySession({ virtualAccountKey: 'demo_primary', strategyId: 'old-identity', member: false, primary: true }))).toBe(true);
  });

  it('recognizes captured runner membership even when a session list has older membership flags', () => {
    const value = parallelStatus();
    const session = strategySession({ member: false });
    value.runner.parallelSessions = [{ virtualAccountKey: session.virtualAccountKey, strategyId: session.strategyId }];
    expect(canStartDemoSession(value, session)).toBe(true);
    expect(canAttachDemo(value, session.strategyId, [])).toBe(false);
    expect(canAdoptDemoSession(value, session, [])).toBe(false);
    value.runner.parallelSessions = [session.virtualAccountKey];
    expect(canAttachDemo(value, session.strategyId, [session])).toBe(false);
    expect(canStartDemoSession(value, session)).toBe(true);
  });

  it('offers adoption of eligible history while rejecting duplicate current strategies', () => {
    const value = parallelStatus();
    const history = strategySession({ member: false, running: false });
    expect(canAdoptDemoSession(value, history, [history])).toBe(true);
    expect(canStartDemoSession(value, history)).toBe(false);
    expect(canAdoptDemoSession(value, history, [history, strategySession({ virtualAccountKey: 'another_member' })])).toBe(false);
    expect(canAdoptDemoSession(value, strategySession({ member: false, strategyId: 'old-identity' }), [])).toBe(false);
    expect(canAdoptDemoSession(value, strategySession(), [])).toBe(false);
  });

  it('requires backend capability, readable credentials, and eligible enabled accounts for every session action', () => {
    const value = parallelStatus();
    const history = strategySession({ member: false });
    const assertDisabled = () => {
      expect(canAttachDemo(value, history.strategyId, [history])).toBe(false);
      expect(canStartDemoSession(value, strategySession())).toBe(false);
      expect(canAdoptDemoSession(value, history, [history])).toBe(false);
    };
    value.parallelSupported = false;
    assertDisabled();
    value.parallelSupported = true;
    value.credentials.configured = false;
    assertDisabled();
    value.credentials.configured = true;
    value.credentials.decryptionAvailable = false;
    assertDisabled();
    value.credentials.decryptionAvailable = true;
    value.accounts[0]!.eligible = false;
    assertDisabled();
    value.accounts[0]!.eligible = true;
    value.accounts[0]!.enabled = false;
    assertDisabled();
    expect(canAttachDemo(null, history.strategyId, [])).toBe(false);
    expect(canStartDemoSession(null, strategySession())).toBe(false);
    expect(canAdoptDemoSession(null, history, [])).toBe(false);
  });

  it.each(['handoffInProgress', 'switchPersistenceUnresolved', 'sharedRegistryPersistenceUnresolved', 'sharedRecoveryIncomplete'])('blocks mutations while %s is unresolved', (flag) => {
    const value = parallelStatus();
    value.runner[flag] = true;
    const history = strategySession({ member: false });
    expect(canAttachDemo(value, history.strategyId, [])).toBe(false);
    expect(canStartDemoSession(value, strategySession())).toBe(false);
    expect(canAdoptDemoSession(value, history, [])).toBe(false);
  });

  it.each([
    ['sharedRegistryPersistenceUnresolved', '并行会话保存状态待核对', 'demo_shared_persistence_unresolved'],
    ['sharedRecoveryIncomplete', '并行会话恢复未完成', 'demo_shared_recovery_incomplete'],
  ] as const)('shows the actual controller blocker %s and preserves server rejection explanations', (flag, label, code) => {
    const value = parallelStatus();
    value.switchSupported = true;
    value.runner.tradingEnabled = false;
    value.runner.state = 'paused_flat';
    expect(canStartDemo(value, 'old-identity')).toBe(true);
    expect(canSwitchDemo(value, 's03_R50_k1.5')).toBe(true);
    value.runner[flag] = true;
    expect(canStartDemo(value, 'old-identity')).toBe(false);
    expect(canSwitchDemo(value, 's03_R50_k1.5')).toBe(false);
    expect(demoState(value)).toEqual({ label, color: 'error' });
    expect(demoError({ response: { data: { detail: code } } })).toContain('暂不能开启新开仓');
    value.runner[flag] = false;
    expect(canAttachDemo(value, 's03_R50_k1.5', [])).toBe(true);
    expect(canStartDemoSession(value, strategySession())).toBe(true);
    expect(canAdoptDemoSession(value, strategySession({ member: false }), [])).toBe(true);
  });

  it('labels primary, running and paused companions, and history without revealing ledger keys', () => {
    const value = parallelStatus();
    const primary = demoSessionLabel(strategySession({ primary: true }), value.accounts);
    const running = demoSessionLabel(strategySession({ entriesEnabled: true }), value.accounts);
    const paused = demoSessionLabel(strategySession(), value.accounts);
    const history = demoSessionLabel(strategySession({ member: false }), value.accounts);
    expect(primary).toContain('当前主会话');
    expect(running).toContain('并行运行');
    expect(paused).toContain('并行暂停');
    expect(history).toContain('历史会话');
    for (const label of [primary, running, paused, history]) expect(label).not.toContain('demo_parallel');
    expect(demoSessionLabel({ virtualAccountKey: 'demo_default', strategyId: 'old', active: true }, [])).toContain('当前主会话');
  });

  it('treats a protecting worker as paused and never labels historical entries as currently running', () => {
    expect(demoStrategySessionState(strategySession()).label).toBe('已暂停新开仓');
    expect(demoStrategySessionState(strategySession({ status: { state: 'observing', running: true }, entriesEnabled: true })).label).toBe('正在 Demo 运行');
    expect(demoStrategySessionState(strategySession({ entriesEnabled: true, running: false })).label).toBe('已开闸，等待运行');
    expect(demoStrategySessionState(strategySession({ member: false, entriesEnabled: true })).label).toBe('历史会话');
    expect(demoStrategySessionState(strategySession({ primary: true, member: false, entriesEnabled: true })).label).toBe('正在 Demo 运行');
    expect(demoStrategySessionState(strategySession({ entriesEnabled: undefined })).label).toBe('状态未读取');
    expect(demoSessionLabel(strategySession({ entriesEnabled: undefined }), []).includes('并行暂停')).toBe(false);
  });

  it('maps signed ledger positions without adding unobserved exchange prices or times', () => {
    const rows = demoOwnedPositionRows(strategySession({ ownedPositions: {
      'BTC/USDT:USDT': '-0.00100000', 'ETH/USDT:USDT': '12345678901234567890.12345678',
      'TRX/USDT:USDT': '-0.0000000000000000000000000000000001',
    } }));
    expect(rows).toEqual([
      { pair: 'BTC/USDT:USDT', signedQuantity: '-0.00100000', side: 'short' },
      { pair: 'ETH/USDT:USDT', signedQuantity: '12345678901234567890.12345678', side: 'long' },
      { pair: 'TRX/USDT:USDT', signedQuantity: '-0.0000000000000000000000000000000001', side: 'short' },
    ]);
    for (const row of rows) {
      expect(row).not.toHaveProperty('entryPrice');
      expect(row).not.toHaveProperty('openedAt');
    }
  });

  it('filters only decimal zero balances and leaves unavailable position quantities unknown', () => {
    const rows = demoOwnedPositionRows(strategySession({ ownedPositions: {
      zero: '0', negativeZero: '-0.0000', exponentZero: '0E-8',
      tiny: '-1E-1000', missing: null, malformed: '-unknown', numericZero: 0,
    } }));
    expect(rows).toEqual([
      { pair: 'tiny', signedQuantity: '-1E-1000', side: 'short' },
      { pair: 'missing', signedQuantity: null, side: 'unknown' },
      { pair: 'malformed', signedQuantity: '-unknown', side: 'unknown' },
      { pair: 'numericZero', signedQuantity: 0, side: 'unknown' },
    ]);
    const legacy = [{ pair: 'BTC/USDT:USDT', signedQuantity: '0.1', side: 'long' }];
    expect(demoOwnedPositionRows(strategySession({ ownedPositions: legacy }))).toEqual(legacy);
    expect(legacy).toHaveLength(1);
  });

  it('shows session reconciliation and protection failures before a running entry gate', () => {
    expect(demoStrategySessionState(strategySession({ status: 'reconciliation_error', entriesEnabled: true })).label).toBe('运行异常');
    expect(demoStrategySessionState(strategySession({ status: { lastError: 'TimeoutError' }, entriesEnabled: true })).label).toBe('运行异常');
    expect(demoStrategySessionState(strategySession({ initializationError: 'RestoreError', entriesEnabled: true })).label).toBe('运行异常');
    expect(demoStrategySessionState(strategySession({ status: { risk_alert: 'missing_entry_price' }, entriesEnabled: true })).label).toBe('风险保护待恢复');
    expect(demoStrategySessionState(strategySession({ status: { ownership_conflicts: ['unowned_open_order'] }, entriesEnabled: true })).label).toBe('仓位归属待核对');
    expect(demoStrategySessionState(strategySession({ status: 'observing', state: 'reconciliation_error', entriesEnabled: true })).label).toBe('运行异常');
    expect(demoStrategySessionState(strategySession({ riskAlert: 'missing_entry_price', entriesEnabled: true })).label).toBe('风险保护待恢复');
    expect(demoStrategySessionState(strategySession({ ownershipConflicts: ['unowned_open_order'], entriesEnabled: true })).label).toBe('仓位归属待核对');
  });

  it('explains shared-account skips and recovery failures without fabricating fills or successful handoffs', () => {
    expect(demoError(new Error('shared_pair_busy'))).toContain('本次信号跳过');
    expect(demoError(new Error('demo_shared_recovery_incomplete'))).toContain('暂不能开启新开仓');
    expect(demoError(new Error('demo_parallel_entries_enabled'))).toContain('暂停所有并行策略');
    expect(demoError(new Error('demo_shared_persistence_unresolved'))).toContain('保存状态待核对');
    expect(demoError(new Error('demo_shared_session_limit'))).toContain('最多同时管理三个策略会话');
    expect(demoError(new Error('demo_shared_account_unreconciled'))).toContain('归属对账');
  });
});

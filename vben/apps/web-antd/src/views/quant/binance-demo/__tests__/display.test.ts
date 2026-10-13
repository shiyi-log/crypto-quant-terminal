import type { DemoStatus } from '../../../../api/binance-demo';

import { describe, expect, it, vi } from 'vitest';

import { canStartDemo, canSwitchDemo, comparisonObservation, comparisonPriceText, decimalText, demoError, demoReadSession, demoSessionLabel, demoState, exchangeTime, feeText, fillSide, isComparisonActivity, makerText, orderRecordStatus, orderStatus, pinDemoSessions, quotedRateText, selectedDemoAccount, submitCredentials, unfilledRatio } from '../display';

function status(): DemoStatus {
  return {
    environment: 'binance_demo', endpoint: 'demo-fapi.binance.com',
    credentials: { configured: true }, runner: { state: 'configured', tradingEnabled: false },
    accounts: [{ accountId: 's03_R50_k1.5', strategyId: 's03', enabled: true, eligible: true, ruleHash: 'rule', executionHash: 'execution' }],
    metrics: {}, limitations: [],
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

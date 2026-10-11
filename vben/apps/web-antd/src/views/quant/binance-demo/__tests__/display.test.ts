import type { DemoStatus } from '../../../../api/binance-demo';

import { describe, expect, it, vi } from 'vitest';

import { canStartDemo, comparisonObservation, decimalText, demoError, demoState, exchangeTime, feeText, fillSide, makerText, orderRecordStatus, orderStatus, submitCredentials, unfilledRatio } from '../display';

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
    expect(comparisonObservation({ payload: { paper_event: { fill_price: 100 } } }).internalPrice).toBe('100');
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

import { describe, expect, it } from 'vitest';

import { environmentLabel, fillDirection, guardState, numberText, orderState, positionDirection, riskFacts, snapshotState } from './tradeDeskDisplay';

describe('交易台展示逻辑', () => {
  it('只把明确的 Demo 环境显示为 Demo', () => {
    expect(environmentLabel('binance_demo', 'demo-fapi.binance.com')).toBe('Binance Demo');
    expect(environmentLabel('binance_demo', 'fapi.binance.com')).toBe('环境待核对');
    expect(environmentLabel(undefined, undefined)).toBe('环境未知');
  });

  it('风险保护状态优先于运行状态', () => {
    expect(guardState({ state: 'running', risk_alert: 'stale_mark' })).toEqual({ label: '保护待恢复', color: 'error' });
    expect(guardState({ state: 'running', ownership_conflicts: ['foreign_order'] })).toEqual({ label: '归属待核对', color: 'error' });
    expect(guardState({ state: 'running' })).toEqual({ label: '允许观察', color: 'success' });
    expect(guardState(null)).toEqual({ label: '状态未知', color: 'default' });
  });

  it('缺失金额显示未知，不把缺失数据当零', () => {
    expect(numberText('0.00000000')).toBe('0.00000000');
    expect(numberText(null)).toBe('未知');
    expect(numberText(undefined)).toBe('未知');
  });

  it('单向仓位按实际数量区分多空并保留零仓事实', () => {
    expect(positionDirection({ positionSide: 'BOTH', positionAmt: '-0.010' })).toBe('空头');
    expect(positionDirection({ positionSide: 'BOTH', positionAmt: '0.010' })).toBe('多头');
    expect(positionDirection({ positionSide: 'BOTH', positionAmt: '0.000' })).toBe('空仓');
    expect(positionDirection({ positionSide: 'BOTH', positionAmt: null })).toBe('未知');
    expect(positionDirection({ positionAmt: 'NaN' })).toBe('未知');
  });

  it('成交没有 side 时仍从交易所 buyer 事实显示买卖', () => {
    expect(fillDirection({ buyer: true })).toBe('买入');
    expect(fillDirection({ buyer: false })).toBe('卖出');
    expect(fillDirection({ side: 'SELL', buyer: true })).toBe('卖出');
    expect(fillDirection({})).toBe('未知');
  });

  it('空的交易所状态仍显示未知提交和部分成交事实', () => {
    expect(orderState({ status: null, demoAudit: { status: 'unknown' } })).toEqual({ label: '提交结果待确认', color: 'warning' });
    expect(orderState({ status: 'PARTIALLY_FILLED' })).toEqual({ label: '部分成交', color: 'processing' });
    expect(orderState({ status: null })).toEqual({ label: '未知', color: 'default' });
  });

  it('请求刚完成不能覆盖服务事实陈旧或未知', () => {
    expect(snapshotState({ last_cycle_ms: 1_000 }, 62_000)).toEqual({ label: '服务事实已过期', color: 'warning' });
    expect(snapshotState({ status_snapshot_stale: true, last_cycle_ms: 1_000 }, 2_000)).toEqual({ label: '正在读取上次快照', color: 'warning' });
    expect(snapshotState({}, 2_000)).toEqual({ label: '事实时间未知', color: 'default' });
    expect(snapshotState({ last_cycle_ms: 1_000 }, 2_000)).toEqual({ label: '服务事实新鲜', color: 'success' });
  });

  it('硬失败和风险证据缺口优先于运行标志', () => {
    expect(guardState({ state: 'running', risk_policy: { entries_mode: 'terminated' } })).toEqual({ label: '本轮风险失败', color: 'error' });
    expect(guardState({ state: 'running', risk_policy: { entries_mode: 'cooldown' } })).toEqual({ label: '冷却中', color: 'warning' });
    expect(guardState({ state: 'running', risk_policy: { entries_mode: 'blocked_evidence_incomplete' } })).toEqual({ label: '风险证据不足', color: 'warning' });
  });

  it('缺少完整账户证据时不编造权益和日收益', () => {
    expect(riskFacts({ risk_policy: { evidence_complete: false, daily_pnl_usdt: '0' } }).dailyPnl).toBe('未知');
    expect(riskFacts({ risk_policy: { evidence_complete: true, daily_pnl_usdt: '0', stage_drawdown_usdt: '2' } })).toMatchObject({ dailyPnl: '0', drawdown: '2' });
  });
});

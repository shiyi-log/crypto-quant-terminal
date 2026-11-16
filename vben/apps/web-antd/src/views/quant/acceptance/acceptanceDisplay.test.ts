import { describe, expect, it } from 'vitest';

import { acceptanceDecision, evidenceMetric, evidenceState } from './acceptanceDisplay';

describe('Demo acceptance presentation', () => {
  it('keeps an unavailable 30-day verdict separate from observed execution evidence', () => {
    expect(acceptanceDecision(null)).toEqual({ label: '尚未形成 30 天裁定', color: 'default' });
    expect(acceptanceDecision({ status: 'insufficient_evidence' })).toEqual({
      label: '证据不足',
      color: 'warning',
    });
    expect(acceptanceDecision({ status: 'failed', reason: 'daily_loss_limit' })).toEqual({
      label: '本轮失败',
      color: 'error',
    });
    expect(acceptanceDecision({ status: 'passed' })).toEqual({ label: '通过', color: 'success' });
    expect(acceptanceDecision({ verdict: 'failed' })).toEqual({ label: '本轮失败', color: 'error' });
  });

  it('does not turn missing fee or funding evidence into a zero value', () => {
    expect(evidenceMetric('actual_fees_observed', '0.00000000 USDT')).toBe('0.00000000 USDT');
    expect(evidenceMetric('actual_fees_partially_known', null)).toBe('部分已知');
    expect(evidenceMetric('no_actual_fills', undefined)).toBe('尚无实际成交');
    expect(evidenceState('unknown')).toEqual({ label: '未知', color: 'default' });
    expect(evidenceState('demo_execution_observed')).toEqual({ label: '已观察到 Demo 成交', color: 'success' });
  });
});

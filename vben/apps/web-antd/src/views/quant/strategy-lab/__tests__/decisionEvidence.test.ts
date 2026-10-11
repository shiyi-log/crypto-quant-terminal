import { describe, expect, it } from 'vitest';

import { labBookDecisionEvidence, labRecordPayload } from '../decisionEvidence';
import { labFamily, labReason } from '../displayLabels';

describe('recorded orderbook decision evidence', () => {
  it('explains a rejected filter without converting missing features into zeros', () => {
    const rows = labBookDecisionEvidence({
      event_type: 'decision',
      payload: { signals: [{ pair: 'BTC/USDT:USDT', evidence: { orderbook: {
        group: 'filter', gate_pass: false, gate_reason: 'orderbook_feature_missing',
        paired_subsample_eligible: false, quality: { eligible: false }, features: {},
      } } }] },
    });
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      pair: 'BTC/USDT:USDT', groupLabel: '订单簿确认过滤',
      gateLabel: '入场确认未通过：订单簿特征缺失',
      pairedSampleLabel: '不纳入配对过滤贡献样本',
      featuresLabel: '五档失衡未知 · 相对价差未知 · 滞后方向一致性未知',
    });
    expect(rows[0]?.qualityLabel).toContain('覆盖率未知');
  });

  it('lets the unfiltered control retain baseline behavior but excludes missing-book paired evidence', () => {
    const [row] = labBookDecisionEvidence({ signals: {
      'ETH/USDT:USDT': { evidence: { orderbook: { group: 'control', gate_pass: false,
        gate_reason: 'orderbook_feature_missing', paired_subsample_eligible: false } } },
    } });
    expect(row?.gateLabel).toBe('按原 K 线信号运行，不按订单簿拦截');
    expect(row?.pairedSampleLabel).toBe('不纳入配对过滤贡献样本');
  });

  it('shows actual feature values and paired eligibility, including known zeros', () => {
    const [row] = labBookDecisionEvidence({ pair: 'TRX/USDT:USDT', entry_evidence: { orderbook: {
      group: 'filter', gate_pass: true, paired_subsample_eligible: true,
      quality: { eligible: true, coverage_ratio: 0.95, valid_snapshots: 700 },
      features: { obi_5_time_mean: 0, relative_spread_time_mean: 0.0002, lagged_obi_mid_directional_consistency: 0.7 },
    } } });
    expect(row?.gateLabel).toBe('入场确认通过');
    expect(row?.pairedSampleLabel).toBe('可用于配对评估过滤贡献');
    expect(row?.qualityLabel).toBe('特征质量合格 · 覆盖率 95.0% · 有效快照 700');
    expect(row?.featuresLabel).toBe('五档失衡 0.0000 · 相对价差 0.0200% · 滞后方向一致性 0.7000');
  });

  it('does not label an exit or held bar as a failed new-entry confirmation', () => {
    for (const target of [0, null]) {
      const [row] = labBookDecisionEvidence({ evidence: { orderbook: {
        group: 'filter', baseline_target_side: target, gate_pass: false,
        feature_validity: true, gate_reason: 'orderbook_confirmation_not_applicable',
        paired_subsample_eligible: true,
      } } });
      expect(row?.gateLabel).toBe('本根无新入场，订单簿确认不适用');
      expect(row?.featureValidityLabel).toBe('本根前向特征有效');
    }
  });

  it('retains generic records and distinguishes absent evidence from an unknown gate result', () => {
    const payload = { reason: 'position_held' };
    expect(labRecordPayload({ payload })).toBe(payload);
    expect(labBookDecisionEvidence({ payload })).toEqual([]);
    const [row] = labBookDecisionEvidence({ evidence: { orderbook: { group: 'filter' } } });
    expect(row?.gateLabel).toContain('确认结果未知');
    expect(row?.pairedSampleLabel).toBe('配对样本资格未知');
  });

  it('translates new-family metadata and feed/confirmation reasons', () => {
    expect(labFamily('基差/期限结构')).toBe('基差 · 期限结构');
    expect(labFamily('跨资产/价差')).toBe('跨资产 · 协整价差');
    expect(labReason('spot_data_stale;basis_timestamps_not_exactly_synchronized')).toBe('现货行情已过期；现货与永续 K 线未精确同步');
    expect(labReason('orderbook_confirmation_not_met')).toBe('订单簿入场确认未通过');
  });
});

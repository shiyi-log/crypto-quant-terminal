type EvidenceStatus = 'no_actual_fills' | 'actual_fees_partially_known' | 'actual_fees_observed'
  | 'no_exchange_orders' | 'demo_execution_observed' | string;

export function acceptanceDecision(value: unknown) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return { label: '尚未形成 30 天裁定', color: 'default' };
  }
  const status = (value as Record<string, unknown>).verdict ?? (value as Record<string, unknown>).status;
  if (status === 'passed') return { label: '通过', color: 'success' };
  if (status === 'failed') return { label: '本轮失败', color: 'error' };
  if (status === 'insufficient_evidence') return { label: '证据不足', color: 'warning' };
  return { label: '尚未形成 30 天裁定', color: 'default' };
}

export function evidenceState(status: EvidenceStatus) {
  if (status === 'actual_fees_observed') return { label: '手续费已观察', color: 'success' };
  if (status === 'actual_fees_partially_known') return { label: '手续费部分已知', color: 'warning' };
  if (status === 'no_actual_fills') return { label: '尚无实际成交费用', color: 'default' };
  if (status === 'demo_execution_observed') return { label: '已观察到 Demo 成交', color: 'success' };
  if (status === 'no_exchange_orders') return { label: '尚无交易所订单', color: 'default' };
  return { label: '未知', color: 'default' };
}

export function evidenceMetric(status: EvidenceStatus, value: unknown): string {
  if (value !== null && value !== undefined && value !== '') return String(value);
  if (status === 'no_actual_fills') return '尚无实际成交';
  if (status === 'actual_fees_partially_known') return '部分已知';
  return '未知';
}

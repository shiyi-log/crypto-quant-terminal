import type { DemoRecord, DemoStatus, DemoStrategyAccount } from '#/api/binance-demo';

/** Preserve exchange decimal strings, including their precision and known zero. */
export function decimalText(value: unknown): string {
  return typeof value === 'string' && value !== '' ? value : '未知';
}

export function valueText(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  return typeof value === 'object' ? '—' : String(value);
}

export function exchangeTime(value: unknown): string {
  const timestamp = typeof value === 'string' && !/^\d+$/.test(value)
    ? Date.parse(value) : typeof value === 'number' ? value : Number(value);
  if (value === null || value === undefined || value === '' || !Number.isFinite(timestamp) || timestamp <= 0) return '—';
  return new Date(timestamp).toLocaleString('zh-CN', { hour12: false });
}

export function feeText(row: DemoRecord): string {
  const amount = decimalText(row.commission);
  if (amount === '未知') return '未知（成交回报未提供）';
  return `${amount} ${valueText(row.commissionAsset)}`;
}

export function makerText(value: unknown): string {
  return value === true ? 'Maker' : value === false ? 'Taker' : '未知';
}

const orderStates: Record<string, string> = {
  NEW: '挂单中', PARTIALLY_FILLED: '部分成交', FILLED: '已成交',
  CANCELED: '已撤销', REJECTED: '已拒绝', EXPIRED: '已过期',
  EXPIRED_IN_MATCH: '撮合中过期', SUBMISSION_UNKNOWN: '提交结果待确认',
};

export function orderStatus(value: unknown): string {
  const key = typeof value === 'string' ? value.toUpperCase() : '';
  return orderStates[key] ? `${orderStates[key]} · ${key}` : valueText(value);
}

export function orderRecordStatus(row: DemoRecord): string {
  const audit = row.demoAudit as DemoRecord | undefined;
  if (isSubmissionUnknown(row)) return '提交结果待确认（勿重复下单）';
  if (row.status) return orderStatus(row.status);
  const internal: Record<string, string> = {
    intent: '本机准备提交', draft: '本机准备提交', submitting: '本机提交中',
    unknown: '提交结果待确认（勿重复下单）',
    submission_unknown: '提交结果待确认（勿重复下单）',
  };
  return typeof audit?.status === 'string' ? internal[audit.status] ?? '币安状态尚未回报' : '币安状态尚未回报';
}

export function isSubmissionUnknown(row: DemoRecord): boolean {
  const audit = row.demoAudit as DemoRecord | undefined;
  return audit?.status === 'unknown' || audit?.status === 'submission_unknown';
}

export function fillSide(row: DemoRecord): string {
  return typeof row.side === 'string' ? row.side.toUpperCase()
    : row.buyer === true ? 'BUY' : row.buyer === false ? 'SELL' : '未知';
}

export function demoState(status?: DemoStatus | null) {
  if (!status) return { label: '状态未读取', color: 'default' };
  if (!status.credentials?.configured) return { label: '待配置', color: 'default' };
  if (status.credentials.decryptionAvailable === false) return { label: '配置暂不可读取', color: 'error' };
  const state = status.runner?.state?.toLowerCase();
  if (status.runner.lastError || ['error', 'failed', 'reconciliation_error', 'blocked_unknown_exposure'].includes(state ?? '')) return { label: '运行异常', color: 'error' };
  if (status.runner.risk_alert) return { label: '风险保护待恢复', color: 'error' };
  if (Array.isArray(status.runner.ownership_conflicts) && status.runner.ownership_conflicts.length) return { label: '仓位归属待核对', color: 'error' };
  if (status.runner?.tradingEnabled || state === 'running') return { label: '正在 Demo 运行', color: 'processing' };
  if (['connected', 'checked', 'readonly_connected', 'ready'].includes(state ?? '')) {
    return { label: '只读连通', color: 'success' };
  }
  if (['stopped', 'paused', 'paused_protecting', 'paused_flat'].includes(state ?? '')) return { label: '已暂停新开仓', color: 'warning' };
  if (status.lastCheck?.connected === false) return { label: '只读连接失败', color: 'error' };
  if (status.lastCheck?.connected === true) return { label: '只读连通', color: 'success' };
  return { label: '已配置', color: 'blue' };
}

export function accountLabel(account: DemoStrategyAccount): string {
  const label = account.description || account.strategyId;
  return `${label} · ${account.timeframe || '周期未知'} · ${account.accountId}`;
}

export function canStartDemo(status: DemoStatus | null, accountId: string): boolean {
  return Boolean(status?.credentials?.configured && status.credentials.decryptionAvailable !== false && !status?.runner?.tradingEnabled && status?.runner?.state !== 'running' &&
    (!status.runner.accountLocked || status.runner.selectedAccountId === accountId) &&
    status?.accounts?.some((account) => account.accountId === accountId && account.enabled && account.eligible));
}

export function unfilledRatio(value: unknown): string {
  const ratio = typeof value === 'number' ? value : typeof value === 'string' && value !== '' ? Number(value) : null;
  return ratio !== null && Number.isFinite(ratio) && ratio >= 0 && ratio <= 1 ? `${(ratio * 100).toFixed(1)}%` : '未知';
}

function record(value: unknown): DemoRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as DemoRecord : {};
}

/** A comparison observation is an audit event, never a fabricated paired fill. */
export function comparisonObservation(row: DemoRecord): DemoRecord {
  const payload = record(row.payload);
  const paper = record(payload.paper_event);
  const price = paper.fill_price;
  return {
    signalId: row.signal_id ?? payload.signal_id,
    observedAt: row.observed_ms,
    pair: payload.pair ?? paper.pair,
    stage: row.stage,
    paperEventType: payload.paper_event_type,
    internalPrice: typeof price === 'string' ? price : typeof price === 'number' ? String(price) : null,
    reason: payload.reason ?? payload.cancellation_reason ?? payload.error_kind,
    ruleHash: row.rule_hash ?? payload.rule_hash,
    inputFingerprint: row.input_fingerprint ?? payload.input_fingerprint,
  };
}

/** Credential form contents are discarded after both accepted and rejected submissions. */
export async function submitCredentials(
  form: { apiKey: string; secret: string },
  save: (apiKey: string, secret: string) => Promise<unknown>,
): Promise<void> {
  try {
    await save(form.apiKey.trim(), form.secret.trim());
  } finally {
    form.apiKey = '';
    form.secret = '';
  }
}

/** Never stringify request configuration or unknown error objects containing POST bodies. */
export function demoError(error: unknown): string {
  const value = error as { message?: unknown; response?: { data?: { detail?: unknown; error?: unknown; message?: unknown } } };
  const detail = value?.response?.data;
  const message = detail?.detail ?? detail?.error ?? detail?.message ?? value?.message;
  return typeof message === 'string' ? message : 'Demo 请求失败，请检查服务状态';
}

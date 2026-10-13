import type { DemoRecord, DemoSession, DemoStatus, DemoStrategyAccount, DemoStrategySession } from '#/api/binance-demo';

/** Preserve exchange decimal strings, including their precision and known zero. */
export function decimalText(value: unknown): string {
  return typeof value === 'string' && value !== '' ? value : '未知';
}

/** Move a decimal string two places without rounding the quoted exchange rate. */
export function quotedRateText(value: unknown): string {
  if (typeof value !== 'string') return '未知';
  const match = /^(-?)(\d+)(?:\.(\d+))?(?:[Ee]([+-]?\d+))?$/.exec(value);
  if (!match) return '未知';
  // Only the exponent becomes a number; the coefficient is kept as digits.
  const exponent = Number(match[4] ?? '0');
  if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 1000) return '未知';
  const digits = `${match[2]}${match[3] ?? ''}`;
  const point = match[2]!.length + exponent;
  const integer = point <= 0 ? '0' : point >= digits.length ? digits.padEnd(point, '0') : digits.slice(0, point);
  const fraction = point <= 0 ? `${'0'.repeat(-point)}${digits}` : point >= digits.length ? '' : digits.slice(point);
  const whole = `${integer}${fraction.slice(0, 2).padEnd(2, '0')}`.replace(/^0+(?=\d)/, '');
  const tail = fraction.slice(2).replace(/0+$/, '');
  return `${match[1]}${whole}${tail ? `.${tail}` : ''}%（原值 ${value}）`;
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
  if (status.runner.switchPersistenceUnresolved) return { label: '切换状态待核对', color: 'error' };
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
  return Boolean(status?.credentials?.configured && status.credentials.decryptionAvailable !== false && !status?.runner?.tradingEnabled && status?.runner?.state !== 'running' && !status.runner.switchPersistenceUnresolved &&
    (!status.runner.selectedAccountId || status.runner.selectedAccountId === accountId) &&
    (!status.runner.accountLocked || status.runner.selectedAccountId === accountId) &&
    status?.accounts?.some((account) => account.accountId === accountId && account.enabled && account.eligible));
}

export function canSwitchDemo(status: DemoStatus | null, accountId: string): boolean {
  return Boolean(status?.switchSupported && status.credentials?.configured && status.credentials.decryptionAvailable !== false &&
    status.runner.selectedAccountId && status.runner.selectedAccountId !== accountId && !status.runner.handoffInProgress && !status.runner.switchPersistenceUnresolved &&
    status.accounts?.some((account) => account.accountId === accountId && account.enabled && account.eligible));
}

function parallelActionsReady(status: DemoStatus | null): status is DemoStatus {
  return Boolean(status?.parallelSupported && status.credentials?.configured &&
    status.credentials.decryptionAvailable !== false && !status.runner.handoffInProgress &&
    !status.runner.switchPersistenceUnresolved && !status.runner.sharedPersistenceUnresolved);
}

function eligibleDemoAccount(status: DemoStatus, accountId: string): boolean {
  return status.accounts.some((account) => account.accountId === accountId && account.enabled && account.eligible);
}

function parallelMembers(status: DemoStatus): DemoRecord[] {
  const sessions = status.runner.parallelSessions;
  return Array.isArray(sessions) ? sessions.map((session) =>
    typeof session === 'string' ? { virtualAccountKey: session } : record(session)) : [];
}

function managedDemoSession(status: DemoStatus, session: DemoSession | DemoStrategySession): boolean {
  return session.primary === true || session.member === true ||
    session.virtualAccountKey === status.runner.virtualAccountKey ||
    parallelMembers(status).some((member) => member.virtualAccountKey === session.virtualAccountKey);
}

/** A paused member retains its strategy identity; history alone does not reserve it. */
function strategyAlreadyManaged(status: DemoStatus, accountId: string, sessions: DemoStrategySession[]): boolean {
  return status.runner.selectedAccountId === accountId ||
    parallelMembers(status).some((member) => (member.strategyId ?? member.selectedAccountId) === accountId) ||
    sessions.some((session) => session.strategyId === accountId && managedDemoSession(status, session));
}

export function canAttachDemo(status: DemoStatus | null, accountId: string, sessions: DemoStrategySession[] = []): boolean {
  return parallelActionsReady(status) && eligibleDemoAccount(status, accountId) &&
    !strategyAlreadyManaged(status, accountId, sessions);
}

/** Each member has its own entry gate, even while another member is running. */
export function canStartDemoSession(status: DemoStatus | null, session: DemoStrategySession): boolean {
  return parallelActionsReady(status) && managedDemoSession(status, session) &&
    session.entriesEnabled === false && eligibleDemoAccount(status, session.strategyId);
}

/** Resume an immutable historical namespace instead of changing its binding. */
export function canAdoptDemoSession(status: DemoStatus | null, session: DemoStrategySession, sessions: DemoStrategySession[] = []): boolean {
  return parallelActionsReady(status) && !managedDemoSession(status, session) &&
    eligibleDemoAccount(status, session.strategyId) && !strategyAlreadyManaged(status, session.strategyId, sessions);
}

export function demoStrategySessionState(session: DemoStrategySession) {
  const detail = record(session.status);
  const state = String(session.state ?? (typeof session.status === 'string' ? session.status : detail.state ?? detail.status ?? '')).toLowerCase();
  if (session.lastError || detail.lastError || detail.last_error || detail.initializationError || session.initializationError ||
    ['error', 'failed', 'reconciliation_error', 'blocked_unknown_exposure'].includes(state)) {
    return { label: '运行异常', color: 'error' };
  }
  if (session.riskAlert || detail.risk_alert) return { label: '风险保护待恢复', color: 'error' };
  const conflicts = session.ownershipConflicts ?? detail.ownership_conflicts;
  if (Array.isArray(conflicts) && conflicts.length) {
    return { label: '仓位归属待核对', color: 'error' };
  }
  if (session.primary !== true && session.member !== true) return { label: '历史会话', color: 'default' };
  if (session.entriesEnabled === true) {
    if ((session.running ?? detail.running) === false) return { label: '已开闸，等待运行', color: 'warning' };
    return { label: '正在 Demo 运行', color: 'processing' };
  }
  return session.entriesEnabled === false
    ? { label: '已暂停新开仓', color: 'warning' }
    : { label: '状态未读取', color: 'default' };
}

/** Ledger quantities identify ownership; they do not provide exchange entry prices. */
export function demoOwnedPositionRows(session: DemoStrategySession): DemoRecord[] {
  if (Array.isArray(session.ownedPositions)) {
    return session.ownedPositions.filter((position) => position && typeof position === 'object' && !Array.isArray(position));
  }
  return Object.entries(record(session.ownedPositions)).flatMap(([pair, signedQuantity]) => {
    const decimal = typeof signedQuantity === 'string' && /^[+-]?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?$/.test(signedQuantity);
    if (decimal && /^[+-]?0+(?:\.0+)?(?:[Ee][+-]?\d+)?$/.test(signedQuantity)) return [];
    return [{ pair, signedQuantity, side: decimal ? signedQuantity.startsWith('-') ? 'short' : 'long' : 'unknown' }];
  });
}

/** Polling updates the runner without replacing the user's pending choice. */
export function selectedDemoAccount(status: DemoStatus, candidate: string): string {
  return candidate && status.accounts.some((account) => account.accountId === candidate)
    ? candidate : status.runner.selectedAccountId ?? '';
}

/** Bind a refresh to its captured active ledger, including after an external switch. */
export function demoReadSession(status: DemoStatus, candidate: string): string | undefined {
  return candidate || status.runner.virtualAccountKey || undefined;
}

export function pinDemoSessions(status: DemoStatus, sessions: DemoSession[]): DemoSession[] {
  const activeKey = status.runner.virtualAccountKey;
  return activeKey ? sessions.map((session) => ({ ...session, active: session.virtualAccountKey === activeKey })) : sessions;
}

export function demoSessionLabel(session: DemoSession | DemoStrategySession, accounts: DemoStrategyAccount[]): string {
  const account = accounts.find((item) => item.accountId === session.strategyId);
  const primary = session.active === true || (session.active === undefined && session.primary === true);
  const role = primary ? '当前主会话' : session.member === true
    ? session.entriesEnabled === true ? '并行运行' : session.entriesEnabled === false ? '并行暂停' : '并行状态待核对' : '历史会话';
  return `${account?.description || session.strategyId} · ${exchangeTime(session.forwardBoundaryMs)} · ${role}`;
}

export function unfilledRatio(value: unknown): string {
  const ratio = typeof value === 'number' ? value : typeof value === 'string' && value !== '' ? Number(value) : null;
  return ratio !== null && Number.isFinite(ratio) && ratio >= 0 && ratio <= 1 ? `${(ratio * 100).toFixed(1)}%` : '未知';
}

function record(value: unknown): DemoRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as DemoRecord : {};
}

const routineComparisonStages = new Set(['binding', 'runner_checkpoint', 'actual_execution_config']);

/** Hide routine telemetry in the trading view without discarding any audit rows. */
export function isComparisonActivity(row: DemoRecord): boolean {
  return !routineComparisonStages.has(String(row.stage ?? ''));
}

const comparisonStages: Record<string, string> = {
  paper_event: '内部模拟事件', entry_denied: 'Demo 未开仓', entry_expiry: 'Demo 挂单到期',
  submission_error: 'Demo 提交异常', snapshot_error: '账户检查异常',
  binding: '会话绑定', runner_checkpoint: '运行心跳', actual_execution_config: '执行配置检查',
};
const paperEventTypes: Record<string, string> = {
  order_submitted: '已委托', order_filled: '已成交', order_cancelled: '已撤单',
  order_rejected: '已拒绝', trade_closed: '已平仓',
};

/** A missing price is relevant only when the event claims a recorded paper fill. */
export function comparisonPriceText(row: DemoRecord): string {
  if (typeof row.internalPrice === 'string' && row.internalPrice !== '') {
    return row.paperEventType === 'trade_closed' ? `${row.internalPrice}（平仓价）` : row.internalPrice;
  }
  return row.paperEventType === 'order_filled' || row.paperEventType === 'trade_closed' ? '未记录' : '—';
}

/** A comparison observation is an audit event, never a fabricated paired fill. */
export function comparisonObservation(row: DemoRecord): DemoRecord {
  const payload = record(row.payload);
  const paper = record(payload.paper_event);
  const price = payload.paper_event_type === 'order_filled' ? paper.fill_price
    : payload.paper_event_type === 'trade_closed' ? paper.exit_price : null;
  return {
    observationId: row.observation_id,
    signalId: row.signal_id ?? payload.signal_id,
    observedAt: row.observed_ms,
    pair: payload.pair ?? paper.pair,
    stage: row.stage,
    stageLabel: comparisonStages[String(row.stage ?? '')] ?? valueText(row.stage),
    paperEventType: payload.paper_event_type,
    paperEventLabel: paperEventTypes[String(payload.paper_event_type ?? '')] ?? valueText(payload.paper_event_type),
    internalPrice: typeof price === 'string' && price !== '' ? price : typeof price === 'number' && Number.isFinite(price) ? String(price) : null,
    reason: payload.reason ?? payload.cancellation_reason ?? payload.error_kind ?? payload.error_type ?? paper.reason ?? paper.exit_reason,
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
  const labels: Record<string, string> = {
    demo_owned_exposure_unresolved: '旧策略仍有仓位或仓位归属待核对，暂不能切换；已有仓位继续保护。',
    demo_owned_orders_unresolved: '旧策略仍有挂单或提交结果未确认，暂不能切换；请稍后重试。',
    demo_owned_fills_unresolved: '旧策略成交数量尚未完全对账，暂不能切换；请稍后重试。',
    demo_worker_busy: 'Demo 正在处理订单或成交，请稍后重试切换。',
    demo_handoff_in_progress: 'Demo 正在交接，请稍后重试。',
    demo_account_positions_not_flat: 'Demo 账户仍有持仓，暂不能切换；切换不会自动平仓。',
    demo_account_open_orders_unresolved: 'Demo 账户仍有挂单或订单状态待确认，暂不能切换。',
    demo_account_reconciliation_unresolved: 'Demo 账户尚未完成对账，暂不能切换。',
    demo_switch_exchange_check_failed: '未能确认交易所账户空仓且无挂单，暂不能切换；请稍后重试。',
    demo_switch_persistence_failed: '新策略会话未能保存，旧会话保持暂停并继续保护；请刷新后重试。',
    demo_switch_persistence_unresolved: '策略切换状态尚未确认，请刷新核对当前策略后再操作。',
    demo_credential_identity_mismatch: 'Demo 凭据与原会话不一致，暂不能切换策略。',
    demo_switch_namespace_collision: '新策略会话无法建立，旧会话保持暂停；请刷新后重试。',
    demo_session_not_found: '所选历史会话不存在，请刷新后重新选择。',
    demo_shared_persistence_failed: '并行策略会话未能保存，请刷新核对后重试。',
    demo_shared_persistence_unresolved: '并行策略会话保存状态待核对，暂不能开启新开仓。',
    demo_shared_recovery_incomplete: '部分策略会话尚未恢复，暂不能开启新开仓；已有仓位继续保护。',
    demo_shared_session_initialization_failed: '策略会话初始化尚未完成，暂不能开启新开仓；请刷新核对。',
    demo_primary_session_required: '请先启用主策略会话，再接入并行策略。',
    demo_shared_session_limit: '一个 Demo 账户最多同时管理三个策略会话；历史记录继续保留。',
    demo_shared_account_unreconciled: '共享 Demo 账户仓位或挂单尚未完成归属对账，暂不能接入策略。',
    demo_session_binding_missing: '该历史会话缺少策略绑定，暂不能恢复；请刷新核对。',
    paper_account_not_demo_eligible: '该策略账户不符合 Demo 执行条件，请重新选择。',
    demo_parallel_entries_enabled: '请先暂停所有并行策略的新开仓，再切换主策略或交接账户。',
    shared_pair_busy: '该币种已有其他策略的持仓或挂单，本次信号跳过，避免净额混账。',
  };
  return typeof message === 'string' ? labels[message] ?? message : 'Demo 请求失败，请检查服务状态';
}

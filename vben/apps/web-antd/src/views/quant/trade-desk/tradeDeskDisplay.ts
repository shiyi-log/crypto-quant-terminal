export function environmentLabel(environment: unknown, endpoint: unknown): string {
  return environment === 'binance_demo' && endpoint === 'demo-fapi.binance.com'
    ? 'Binance Demo' : environment || endpoint ? '环境待核对' : '环境未知';
}

export function guardState(runner: Record<string, unknown> | null | undefined) {
  if (!runner) return { label: '状态未知', color: 'default' };
  const risk = record(runner.risk_policy);
  const mode = risk.entries_mode;
  if (risk.hard_failure || mode === 'terminated') return { label: '本轮风险失败', color: 'error' };
  if (mode === 'lock_until_reconciled') return { label: '撤单平仓锁定', color: 'error' };
  if (mode === 'stop_until_next_day') return { label: '当日停止开仓', color: 'warning' };
  if (mode === 'cooldown') return { label: '冷却中', color: 'warning' };
  if (mode === 'blocked_evidence_incomplete') return { label: '风险证据不足', color: 'warning' };
  if (runner.risk_alert) return { label: '保护待恢复', color: 'error' };
  if (Array.isArray(runner.ownership_conflicts) && runner.ownership_conflicts.length) {
    return { label: '归属待核对', color: 'error' };
  }
  if (runner.lastError) return { label: '运行异常', color: 'error' };
  return { label: runner.state === 'running' ? '允许观察' : '已暂停新开仓', color: runner.state === 'running' ? 'success' : 'warning' };
}

export function numberText(value: unknown): string {
  return typeof value === 'string' && /^[-+]?(?:\d+\.?\d*|\.\d+)$/.test(value) ? value : '未知';
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

export function positionDirection(value: Record<string, unknown>): string {
  const amount = numberText(value.positionAmt);
  if (amount === '未知') return '未知';
  if (!/[1-9]/.test(amount)) return '空仓';
  return amount.startsWith('-') ? '空头' : '多头';
}

export function fillDirection(value: Record<string, unknown>): string {
  const side = typeof value.side === 'string' ? value.side.toLowerCase() : '';
  if (side === 'buy') return '买入';
  if (side === 'sell') return '卖出';
  return value.buyer === true ? '买入' : value.buyer === false ? '卖出' : '未知';
}

export function orderState(value: Record<string, unknown>) {
  const audit = record(value.demoAudit);
  const status = String(value.status || audit.status || '').toLowerCase();
  if (status === 'unknown' || status === 'submitting') return { label: '提交结果待确认', color: 'warning' };
  if (status === 'partially_filled') return { label: '部分成交', color: 'processing' };
  if (status === 'filled') return { label: '已成交', color: 'success' };
  if (status === 'new' || status === 'open') return { label: '挂单中', color: 'processing' };
  if (status === 'intent') return { label: '意图已登记', color: 'default' };
  if (status === 'rejected') return { label: '已拒绝', color: 'error' };
  if (status === 'canceled' || status === 'cancelled') return { label: '已撤销', color: 'default' };
  if (status === 'expired' || status === 'abandoned_after_absent_lookup') return { label: '已失效', color: 'default' };
  return { label: '未知', color: 'default' };
}

export function snapshotState(runner: Record<string, unknown> | null | undefined, now: number) {
  if (runner?.status_snapshot_stale === true) return { label: '正在读取上次快照', color: 'warning' };
  const stamp = runner?.last_cycle_ms;
  if (typeof stamp !== 'number' || !Number.isFinite(stamp)) return { label: '事实时间未知', color: 'default' };
  if (stamp > now || now - stamp > 60_000) return { label: '服务事实已过期', color: 'warning' };
  return { label: '服务事实新鲜', color: 'success' };
}

export function beijingTime(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '未知';
  return new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
}

export function riskFacts(runner: Record<string, unknown> | null | undefined) {
  const risk = record(runner?.risk_policy);
  const configs = Array.isArray(runner?.actual_symbol_config) ? runner.actual_symbol_config.map(record) : [];
  const values = (key: string) => configs.length && configs.every((item) => item[key] !== null && item[key] !== undefined)
    ? [...new Set(configs.map((item) => String(item[key])))].join(' / ') : '未知';
  return {
    netEquity: risk.evidence_complete === true ? numberText(risk.net_equity_usdt) : '未知',
    dailyPnl: risk.evidence_complete === true ? numberText(risk.daily_pnl_usdt) : '未知',
    drawdown: numberText(risk.stage_drawdown_usdt),
    cooldownUntil: beijingTime(risk.cooldown_until_ms),
    leverage: values('leverage'),
    marginMode: values('margin_type'),
  };
}

/** 独立策略纸面账户；不通过交易所下单。 */
import { ftClient, unwrap } from '#/api/freqtrade/client';
import { requireSession as requireAuthenticatedSession } from '#/api/session';

export type LabValue = null | number | string;

export interface StrategyDefinition {
  name?: string;
  family?: string;
  timeframe?: string;
  mechanism?: string;
  entry_formula?: string;
  exit_formula?: string;
  distinct_from?: string;
  hypothesis_status?: string;
  params_frozen?: Record<string, unknown>;
  universe?: string[];
  min_warmup_bars?: number;
  /** Immutable experiment-arm identity (for example B/C/D/E). */
  arm_id?: string;
  variant_of?: string;
  variant_label?: string;
  data_status?: string;
  position_budget?: {
    target_exposure?: number;
    max_positions?: number;
    max_per_coin?: number;
    leverage?: number;
    sizing?: string;
  };
  unknown_limits?: string[];
  [key: string]: unknown;
}

export interface StrategyLabRow {
  strategy_id: string;
  [key: string]: unknown;
}

export interface StrategyLabAccount {
  strategy_id: string;
  label?: string;
  name?: string;
  family?: string;
  timeframe?: string;
  initial_capital: LabValue;
  cash: LabValue;
  equity: LabValue;
  realized_pnl: LabValue;
  unrealized_pnl: LabValue;
  fees_paid?: LabValue;
  fee_paid?: LabValue;
  open_entry_fees?: LabValue;
  closed_trade_count?: LabValue;
  trade_count?: LabValue;
  open_position_count?: LabValue;
  pending_order_count?: LabValue;
  win_rate?: LabValue;
  return_pct?: LabValue;
  total_return_ratio?: LabValue;
  max_drawdown_pct?: LabValue;
  max_drawdown_ratio?: LabValue;
  status?: string;
  lifecycle?: string;
  /** Execution arm and data-gating metadata projected by the lab service. */
  arm_id?: string;
  variant_of?: string;
  variant_label?: string;
  data_status?: string;
  performance_scope?: string;
  funding?: string;
  slippage?: string;
  rule_hash?: string;
  execution_hash?: string;
  definition?: StrategyDefinition;
  warmup?: Record<string, unknown>;
  open_positions: StrategyLabRow[];
  last_candle_ms?: null | number;
  last_observed_at_ms?: null | number;
  [key: string]: unknown;
}

export interface StrategyLabExecution {
  fill_model?: string;
  fee_rate_per_side?: LabValue;
  maker_fee_per_side?: LabValue;
  taker_fee_per_side?: LabValue;
  fee_basis?: string;
  non_fill_risk?: string;
  slippage?: string;
  funding?: string;
  leverage?: LabValue;
  timeframe?: string;
  arm_id?: string;
  active_variant_suffix?: string;
  [key: string]: unknown;
}

export interface StrategyLabBlockedStrategy {
  strategy_id: string;
  label?: string;
  arm_id?: string;
  timeframe?: string;
  status?: string;
  reason?: string;
  required_data?: string[];
}

export interface StrategyLabRejectedStrategy extends StrategyLabBlockedStrategy {
  status: 'rejected';
}

export interface StrategyLabOverview {
  accounts: StrategyLabAccount[];
  /** Research hypotheses waiting for data or execution support; excludes rejected candidates. */
  blocked_strategies?: StrategyLabBlockedStrategy[];
  /** Rejected research candidates retained for audit; never counted as active accounts. */
  rejected_strategies?: StrategyLabRejectedStrategy[];
  service?: {
    running?: boolean;
    status?: string;
    last_cycle_at?: null | number;
    last_error?: null | string;
  };
  execution?: StrategyLabExecution;
  execution_model?: StrategyLabExecution;
  limitations?: string[];
  scope?: string;
}

export interface StrategyLabDetail {
  account: StrategyLabAccount;
  orders: StrategyLabRow[];
  trades: StrategyLabRow[];
  equity_curve: StrategyLabRow[];
  events: StrategyLabRow[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/**
 * The local API normally returns a bare JSON object.  Some deployments still
 * pass it through the common `{ data: ... }` response envelope, so accept both
 * shapes before applying the strategy-lab contract.
 */
function responseBody(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) return {};
  if (isRecord(value.data) && !('accounts' in value) && !('account' in value)) {
    return value.data;
  }
  return value;
}

function scopedRows(value: unknown, strategyId: string): StrategyLabRow[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((row): row is StrategyLabRow => isRecord(row))
    .map((row) => ({
      ...row,
      // Older event payloads were stored without the redundant strategy id.
      // The endpoint is already scoped by strategy_id; annotate only missing
      // ids while still rejecting rows belonging to another account below.
      strategy_id: row.strategy_id ?? strategyId,
    }));
}

function requireSession() {
  requireAuthenticatedSession('请先登录后查看独立策略账户');
}

export async function getStrategyLab(
  signal?: AbortSignal,
): Promise<StrategyLabOverview> {
  requireSession();
  const response = responseBody(unwrap<unknown>(
    await ftClient.get('/locals/strategy-lab', { signal }),
  ));
  const accounts = Array.isArray(response.accounts)
    ? response.accounts
    : Array.isArray(response.items)
      ? response.items
      : [];
  return {
    ...response,
    accounts: accounts.filter((row): row is StrategyLabAccount => isRecord(row)),
  };
}

export async function getStrategyLabDetail(
  strategyId: string,
  signal?: AbortSignal,
): Promise<StrategyLabDetail> {
  requireSession();
  const response = responseBody(unwrap<unknown>(
    await ftClient.get('/locals/strategy-lab', {
      params: { strategy_id: strategyId, limit: 100 },
      signal,
    }),
  ));
  const account = isRecord(response.account)
    ? ({ ...response.account } as StrategyLabAccount)
    : null;
  if (account?.strategy_id !== strategyId) {
    throw new Error('策略账户不匹配，已停止展示明细');
  }
  const positions = scopedRows(account.open_positions, strategyId);
  if (positions.some((row) => row.strategy_id !== strategyId)) {
    throw new Error('持仓包含其他策略或缺少策略标识，已停止展示');
  }
  account.open_positions = positions;
  const result = { ...response, account } as StrategyLabDetail;
  for (const key of ['orders', 'trades', 'equity_curve', 'events'] as const) {
    const rows = scopedRows(response[key], strategyId);
    if (rows.some((row) => row.strategy_id !== strategyId)) {
      throw new Error('明细包含其他策略或缺少策略标识，已停止展示');
    }
    result[key] = rows;
  }
  return result;
}

import type {
  StrategyLabAccount,
  StrategyLabDetail,
  StrategyLabOverview,
  StrategyLabRow,
} from '#/api/strategy-lab';

type Fact = Record<string, unknown>;
function record(value: unknown): Fact {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw Error('invalid_paper_object');
  return value as Fact;
}
function identity(value: unknown): string {
  if (typeof value !== 'string' || !value.trim())
    throw Error('invalid_paper_identity');
  return value;
}
function body(value: unknown): Fact {
  const source = record(value);
  return source.data && !('accounts' in source) && !('account' in source)
    ? record(source.data)
    : source;
}
function amount(value: unknown) {
  if (value === null) return;
  if (
    (typeof value !== 'number' && typeof value !== 'string') ||
    String(value).trim() === '' ||
    (typeof value === 'string' &&
      !/^[+-]?(?:[0-9]+(?:[.][0-9]*)?|[.][0-9]+)(?:e[+-]?[0-9]+)?$/i.test(
        value.trim(),
      )) ||
    !Number.isFinite(Number(value))
  )
    throw Error('invalid_paper_amount');
}
function optionalTexts(source: Fact, fields: string[]) {
  for (const field of fields)
    if (
      source[field] !== undefined &&
      source[field] !== null &&
      typeof source[field] !== 'string'
    )
      throw Error('invalid_paper_text');
}
function optionalBooleans(source: Fact, fields: string[]) {
  for (const field of fields)
    if (
      source[field] !== undefined &&
      source[field] !== null &&
      typeof source[field] !== 'boolean'
    )
      throw Error('invalid_paper_boolean');
}
function validateNestedIdentity(value: unknown, strategyId: string): void {
  if (Array.isArray(value)) {
    value.forEach((item) => validateNestedIdentity(item, strategyId));
    return;
  }
  if (!value || typeof value !== 'object') return;
  const source = record(value);
  optionalBooleans(source, [
    'ready',
    'eligible_for_factorial_evaluation',
    'eligible_for_population_evaluation',
    'pre_cutoff_eligible',
  ]);
  if (
    source.signals !== undefined &&
    (!Array.isArray(source.signals) ||
      !source.signals.every(
        (item) =>
          Boolean(item) && typeof item === 'object' && !Array.isArray(item),
      ))
  )
    throw Error('invalid_paper_signals');
  if (
    source.strategy_id !== undefined &&
    source.strategy_id !== null &&
    source.strategy_id !== strategyId
  )
    throw Error('cross_account_paper_payload');
  Object.values(source).forEach((item) =>
    validateNestedIdentity(item, strategyId),
  );
}
function warmup(value: unknown): void {
  const source = record(value);
  optionalBooleans(source, ['ready']);
  for (const field of ['valid_bars', 'available_bars', 'required_bars'])
    if (source[field] !== undefined && source[field] !== null) {
      amount(source[field]);
      if (
        !Number.isSafeInteger(Number(source[field])) ||
        Number(source[field]) < 0
      )
        throw Error('invalid_paper_warmup_count');
    }
  if (
    source.ready === true &&
    source.valid_bars !== undefined &&
    source.required_bars !== undefined &&
    Number(source.valid_bars) < Number(source.required_bars)
  )
    throw Error('inconsistent_paper_warmup');
  for (const [key, item] of Object.entries(source))
    if (item && typeof item === 'object' && !Array.isArray(item)) {
      if (key === 'by_pair') Object.values(record(item)).forEach(warmup);
      else warmup(item);
    }
}
function optionalRecord(source: Fact, field: string) {
  if (source[field] !== undefined && source[field] !== null)
    record(source[field]);
}
function optionalStrings(source: Fact, field: string) {
  const value = source[field];
  if (
    value !== undefined &&
    value !== null &&
    (!Array.isArray(value) || !value.every((item) => typeof item === 'string'))
  )
    throw Error('invalid_paper_strings');
}
function rows(value: unknown, strategyId: string): StrategyLabRow[] {
  if (!Array.isArray(value)) throw Error('invalid_paper_rows');
  return value.map((item) => {
    const row = record(item);
    validateNestedIdentity(row, strategyId);
    const id =
      row.strategy_id === undefined || row.strategy_id === null
        ? strategyId
        : identity(row.strategy_id);
    if (id !== strategyId) throw Error('cross_account_paper_row');
    return { ...row, strategy_id: id };
  });
}
function validateExecution(value: unknown): void {
  const execution = record(value);
  optionalBooleans(execution, ['execution_enabled']);
  optionalTexts(execution, [
    'fill_model',
    'fee_basis',
    'non_fill_risk',
    'slippage',
    'funding',
    'blocked_execution_reason',
  ]);
  for (const key of ['execution_universe', 'research_universe'])
    optionalStrings(execution, key);
  for (const key of [
    'fee_rate_per_side',
    'maker_fee_per_side',
    'taker_fee_per_side',
    'leverage',
    'max_loss_per_trade_usdt',
    'risk_stop_atr_mult',
    'risk_stop_pct',
    'risk_round_trip_fee',
  ])
    if (execution[key] !== undefined) amount(execution[key]);
}
function account(value: unknown): StrategyLabAccount {
  const source = record(value);
  const id = identity(source.strategy_id);
  optionalTexts(source, [
    'label',
    'name',
    'family',
    'timeframe',
    'status',
    'lifecycle',
    'arm_id',
    'variant_of',
    'variant_label',
    'data_status',
    'sample_population',
    'performance_scope',
    'funding',
    'slippage',
    'rule_hash',
    'execution_hash',
  ]);
  for (const key of [
    'initial_capital',
    'cash',
    'equity',
    'realized_pnl',
    'unrealized_pnl',
  ])
    amount(source[key]);
  for (const key of [
    'fees_paid',
    'fee_paid',
    'open_entry_fees',
    'closed_trade_count',
    'trade_count',
    'open_position_count',
    'pending_order_count',
    'win_rate',
    'return_pct',
    'total_return_ratio',
    'max_drawdown_pct',
    'max_drawdown_ratio',
  ])
    if (source[key] !== undefined) amount(source[key]);
  for (const key of ['definition', 'execution', 'warmup'])
    optionalRecord(source, key);
  if (source.definition) {
    const definition = record(source.definition);
    optionalTexts(definition, [
      'name',
      'family',
      'timeframe',
      'mechanism',
      'entry_formula',
      'exit_formula',
      'distinct_from',
      'arm_id',
      'variant_of',
      'canonical_strategy_id',
      'variant_label',
      'data_status',
      'sample_population',
      'blocked_execution_reason',
    ]);
    optionalBooleans(definition, ['execution_enabled']);
    for (const key of ['factorial_factors', 'fnew_factors'])
      if (definition[key]) {
        const factors = record(definition[key]);
        optionalTexts(factors, ['strategy', 'risk_budget', 'book_group']);
        if (factors.stop_width_k !== undefined) amount(factors.stop_width_k);
      }
    for (const key of [
      'params_frozen',
      'params',
      'position_budget',
      'factorial_factors',
      'fnew_factors',
    ])
      optionalRecord(definition, key);
    for (const key of [
      'universe',
      'research_universe',
      'execution_universe',
      'execution_blockers',
      'unknown_limits',
    ])
      optionalStrings(definition, key);
  }
  if (source.execution) validateExecution(source.execution);
  if (source.warmup) warmup(source.warmup);
  return {
    ...source,
    strategy_id: id,
    open_positions: rows(source.open_positions, id),
  } as StrategyLabAccount;
}
export function parsePaperOverview(value: unknown): StrategyLabOverview {
  const source = body(value);
  const raw = Object.hasOwn(source, 'accounts')
    ? source.accounts
    : source.items;
  if (!Array.isArray(raw)) throw Error('invalid_paper_accounts');
  const accounts = raw.map(account);
  if (
    new Set(accounts.map((item) => item.strategy_id)).size !== accounts.length
  )
    throw Error('duplicate_paper_account');
  for (const key of ['execution', 'execution_model', 'service'])
    optionalRecord(source, key);
  for (const key of ['execution', 'execution_model'])
    if (source[key]) validateExecution(source[key]);
  optionalStrings(source, 'limitations');
  for (const key of ['blocked_strategies', 'rejected_strategies']) {
    if (
      source[key] !== undefined &&
      (!Array.isArray(source[key]) ||
        !(source[key] as unknown[]).every((item) => {
          const candidate = record(item);
          identity(candidate.strategy_id);
          optionalTexts(candidate, [
            'label',
            'reason',
            'status',
            'timeframe',
            'arm_id',
          ]);
          return true;
        }))
    )
      throw Error('invalid_paper_candidates');
  }
  if (source.service) {
    const service = record(source.service);
    if (service.running !== undefined && typeof service.running !== 'boolean')
      throw Error('invalid_paper_service');
  }
  return { ...source, accounts } as StrategyLabOverview;
}
export function parsePaperDetail(
  value: unknown,
  strategyId: string,
): StrategyLabDetail {
  const source = body(value);
  const selected = account(source.account);
  if (selected.strategy_id !== strategyId)
    throw Error('paper_detail_identity_mismatch');
  return {
    ...source,
    account: selected,
    orders: rows(source.orders, strategyId),
    trades: rows(source.trades, strategyId),
    equity_curve: rows(source.equity_curve, strategyId),
    events: rows(source.events, strategyId),
  } as StrategyLabDetail;
}

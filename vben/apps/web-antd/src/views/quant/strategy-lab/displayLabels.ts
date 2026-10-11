const reasonLabels: Readonly<Record<string, string>> = Object.freeze({
  account_budget_exhausted: '账户仓位预算已用尽',
  account_peer_data_unready: '另一币种数据尚未就绪',
  execution_disabled: '执行已暂停，保留账户与数据',
  paired_atr_stop_policy_unimplemented: '配对级 ATR 止损规则待实现，双腿执行暂停',
  lead_lag_atr_stop_policy_unimplemented: '领先滞后 ATR 止损规则待实现',
  breadth_atr_stop_policy_unimplemented: '市场宽度 ATR 止损规则待实现',
  cross_sectional_atr_stop_policy_unimplemented: '横截面 ATR 止损规则待实现',
  dual_asset_atr_stop_policy_unimplemented: '双资产 ATR 止损规则待实现',
  paused_research_context_required: '依赖已暂停的 20 币研究上下文，执行暂停',
  research_context_unavailable: '研究上下文数据尚未齐备',
  cross_sectional_portfolio_execution_unimplemented: '横截面组合执行能力待实现',
  atr_risk_execution_pending: 'ATR 风险执行规则待接入',
  research_context_paused: '所需多币研究上下文已暂停',
  paired_execution_pending: '配对执行规则待接入',
  forward_data_required: '等待新方向前向数据',
  fnew_closed_history_stale: '新方向所需已收盘行情已过期',
  futures_warmup_incomplete: '永续行情预热尚未完成',
  futures_data_stale: '永续行情已过期',
  spot_warmup_incomplete: '现货行情预热尚未完成',
  spot_data_stale: '现货行情已过期',
  basis_spot_warmup_incomplete: '基差信号所需现货预热尚未完成',
  basis_timestamps_not_aligned: '现货与永续 K 线时间未对齐',
  basis_timestamps_not_exactly_synchronized: '现货与永续 K 线未精确同步',
  basis_delta_reversion: '基差变化偏离触发回归信号',
  basis_delta_reverted: '基差变化已回归',
  basis_delta_sigma_zero: '基差变化的历史标准差为零',
  no_basis_delta_extreme: '基差变化尚未达到偏离阈值',
  invalid_quote_volume: '报价币成交额缺失或无效',
  vwap_session_start_missing: '缺少当日 VWAP 起点数据',
  vwap_session_volume_zero: '当日 VWAP 成交量为零',
  frozen_vwap_anchor_day_unavailable: '冻结的 VWAP 锚定日期缺失',
  vwap_anchor_reversion: '价格偏离 VWAP 锚点触发回归信号',
  no_vwap_deviation: '价格尚未达到 VWAP 偏离阈值',
  vwap_anchor_reached: '价格已回到 VWAP 锚点',
  vwap_anchor_session_elapsed: 'VWAP 锚定交易日已结束',
  vwap_session_end_no_entry: 'VWAP 交易日结束，不再开仓',
  cascade_continuation_proxy: '连续同向波动触发瀑布延续假设',
  no_cascade_continuation: '尚未触发瀑布延续信号',
  orderbook_feature_pending: '等待订单簿特征',
  orderbook_feature_missing: '订单簿特征缺失',
  orderbook_feature_unavailable: '订单簿特征不可用',
  orderbook_feature_expired: '订单簿特征已过期',
  orderbook_confirmation_not_met: '订单簿入场确认未通过',
  orderbook_confirmation_passed: '订单簿入场确认已通过',
  orderbook_observation_time_unavailable: '订单簿观察时点缺失',
  orderbook_feature_identity_mismatch: '订单簿特征与币种或周期不匹配',
  orderbook_feature_not_available_as_of: '订单簿特征在决策时点尚未可用',
  orderbook_feature_quality_unavailable: '订单簿特征质量不满足要求',
  orderbook_feature_values_unavailable: '订单簿特征值缺失或无效',
  orderbook_confirmation_not_applicable: '本根没有新入场，订单簿确认不适用',
  waiting_published_orderbook_feature: '等待订单簿特征发布',
  breadth_context_unavailable: '市场宽度数据尚未齐备',
  breadth_direction_reversed: '市场宽度方向已反转',
  breadth_neutral: '市场宽度回到中性区',
  breadth_regime: '市场宽度区制信号',
  breadth_reverted: '市场宽度已回归中性',
  atomic_leg_missing: '配对成交缺少一条腿，整组取消',
  atomic_position_inconsistent: '配对持仓不完整，整组退出',
  awaiting_data: '等待行情数据',
  btc_lead_lag_trigger: '比特币涨跌触发以太坊同向信号',
  candle_range_or_atr_zero: 'K 线振幅或平均真实波幅为零',
  closed_candle_stale: '已收盘 K 线数据已过期',
  daily_first_entry_already_consumed: '当日首次突破机会已使用',
  duplicate_closed_candle_timestamp: '已收盘 K 线时间戳重复',
  entry_data_missing: '开仓所需行情数据不完整',
  entry_price_recovered: '价格已回到入场价',
  fill_bar_missing: '缺少预定成交 K 线，订单取消',
  first_session_breakout: '当日首次开盘区间突破',
  frozen_entry_atr_unavailable: '入场时冻结的平均真实波幅缺失',
  gap_cancel: '行情 K 线断档，待成交订单取消',
  holding_period_elapsed: '已达到持仓期限',
  invalid_candle_container: '行情数据格式无效',
  invalid_candle_timestamp: 'K 线时间戳无效',
  invalid_closed_candle_ohlcv: '已收盘 K 线价格或成交量无效',
  invalid_position: '持仓数据无效',
  mean_reversion_complete: '价格已回归均值',
  missed_fill_bar: '错过预定成交 K 线，订单取消',
  momentum_reversed_without_volatility: '动量转向且波动率为零，退出持仓',
  momentum_target: '时序动量目标仓位信号',
  no_lead_lag_trigger: '尚未触发领先滞后信号',
  no_orderflow_extreme: '主动买卖失衡尚未达到阈值',
  no_range_breakout: '尚未突破波幅轨道',
  no_relative_strength_trigger: '相对强弱差尚未达到阈值',
  no_session_breakout: '尚未突破开盘区间',
  no_signal: '暂无交易信号',
  no_statistical_deviation: '价格偏离尚未达到阈值',
  no_trend_pullback: '尚未触发趋势回踩信号',
  no_volume_climax: '尚未触发量能高潮信号',
  no_wick_rejection: '尚未触发影线拒绝信号',
  multi_asset_context_unavailable: '多资产上下文尚未齐备',
  dual_asset_confirmation: 'BTC 与 ETH 方向一致确认',
  dual_asset_confirmation_not_met: 'BTC 与 ETH 方向尚未一致',
  dual_asset_direction_reversed: '双资产方向已反转',
  dual_asset_position_inconsistent: '双资产持仓不完整',
  cross_sectional_selection: '横截面筛选入选',
  outside_cross_sectional_selection: '未进入横截面筛选',
  cross_sectional_rebalance: '横截面调仓退出',
  breadth_observation_only: '仅用于市场宽度观察，不交易该币种',
  funding_rate_unavailable: '资金费率数据尚未接入',
  native_timeframe_adapter_active: '原生周期数据已接入，等待收盘 K 线',
  cross_sectional_execution_pending: '横截面执行能力待接入',
  awaiting_native_timeframe_adapter: '原生周期数据适配器尚未就绪',
  opposite_range_breakout: '反向突破波幅轨道',
  opposite_session_breakout: '反向突破开盘区间',
  orderflow_continuation: '主动买卖失衡延续信号',
  orderflow_direction_reversed: '主动买卖失衡方向已反转',
  orderflow_volume_filter_not_met: '成交量未达到订单流过滤要求',
  pair_timestamps_not_aligned: '比特币与以太坊 K 线时间未对齐',
  paired_position_held: '继续持有配对仓位',
  position_already_closed: '持仓已平，取消重复平仓订单',
  position_held: '继续持有现有仓位',
  price_standard_deviation_zero: '价格标准差为零',
  range_breakout: '波幅轨道突破信号',
  ready: '数据已就绪',
  realized_volatility_zero: '已实现波动率为零',
  relative_strength_converged: '相对强弱差已收敛',
  relative_strength_pair: '相对强弱配对信号',
  restart_cancel: '执行器重启，旧待成交订单取消',
  session_day_end: '当日结束，退出持仓',
  session_day_end_no_entry: '当日结束，不再开仓',
  session_range_not_ready: '当日开盘区间尚未形成',
  signal: '策略交易信号',
  signal_source_only: '仅作为信号来源，不交易该币种',
  slow_trend_broken: '价格已逆向越过长期趋势均线',
  stale_closed_bar: '成交 K 线到达过晚，订单取消',
  statistical_deviation: '价格统计偏离信号',
  stop_loss_close_confirmed: '收盘价确认达到止损条件',
  take_profit_close_confirmed: '收盘价确认达到止盈条件',
  taker_volume_unavailable: '主动买入成交量缺失或无效',
  trend_pullback: '趋势回踩入场信号',
  trend_slope_zero: '长期趋势均线斜率为零',
  unknown: '未知',
  volume_climax_reversal: '量能高潮反转信号',
  warmup_incomplete: '指标预热尚未完成',
  warmup_or_stale_or_missing_data: '指标未就绪或行情过期、缺失，拒绝开仓',
  wick_rejection: '影线拒绝信号',
  zero_sizing: '计算仓位为零，拒绝开仓',
});

const orderStatusLabels: Readonly<Record<string, string>> = Object.freeze({
  cancelled: '已取消',
  canceled: '已取消',
  filled: '已成交',
  pending: '待成交',
  rejected: '已拒绝',
  unknown: '未知',
});

const eventTypeLabels: Readonly<Record<string, string>> = Object.freeze({
  account_created: '账户创建',
  cancel: '订单取消',
  cancelled: '订单取消',
  created: '记录创建',
  decision: '策略决策',
  entry_denied: '开仓被拒绝',
  equity_snapshot: '账户权益快照',
  fill: '订单成交',
  filled: '订单成交',
  order_cancelled: '订单取消',
  order_filled: '订单成交',
  order_rejected: '订单拒绝',
  order_submitted: '订单提交',
  position_opened: '持仓建立',
  rejected: '订单拒绝',
  submitted: '订单提交',
  trade_closed: '交易平仓',
  unknown: '未知',
});

const familyLabels: Readonly<Record<string, string>> = Object.freeze({
  'breakout/range-scaled': '突破 · 波幅缩放',
  'breakout/session-range': '突破 · 开盘区间',
  'candle-structure/rejection': 'K 线形态 · 影线拒绝',
  'cross-asset/diffusion': '跨币种 · 信息传导',
  'cross-asset/information-diffusion': '跨币种 · 信息传导',
  'cross-sectional/pairs': '相对强弱 · 双腿配对',
  'cross-sectional/pairs (both legs)': '相对强弱 · 双腿配对',
  'mean-reversion/statistical': '均值回归 · 统计偏离',
  'mean-reversion/volume': '均值回归 · 量能极端',
  'mean-reversion/volume-extreme': '均值回归 · 量能极端',
  'momentum/time-series': '动量 · 时序动量',
  'order-flow/continuation': '订单流 · 失衡延续',
  'trend-following/pullback-entry': '趋势 · 回踩入场',
  'trend/pullback-entry': '趋势 · 回踩入场',
  'trend/confirmation': '趋势 · 多信号确认',
  'trend/volatility-regime': '趋势 · 波动区制',
  'trend/surge-gated': '趋势 · 量能门控',
  'trend/trailing': '趋势 · 峰值回撤止盈',
  'cross-asset/dual-confirmation': '跨资产 · 双向确认',
  'cross-sectional/low-volatility': '横截面 · 低波动筛选',
  'mean-reversion/long-horizon': '均值回归 · 长周期',
  'breakout/long-horizon': '突破 · 长周期通道',
  'carry/delta-neutral': '资金费 · Delta 中性 Carry',
  'carry/delta-neutral (two-leg)': '资金费 · 双腿 Delta 中性 Carry',
  'hybrid/trend-filtered mean-reversion': '组合 · 趋势过滤与均值回归',
  'volatility-regime/breakout': '波动区制 · 扩张突破',
  'trend/multi-horizon': '趋势 · 多窗口',
  'volatility/squeeze': '波动 · 压缩突破',
  'calendar/seasonality': '日历 · 周内季节性',
  'mean-reversion/range-boundary': '均值回归 · 区间边界',
  'path-dependent/drawdown': '路径依赖 · 回撤恢复',
  'breadth/market-regime': '市场宽度 · 区制信号',
  'intraday-position/momentum': '日内区间位置 · 动量',
  'mean-reversion/exhaustion': '均值回归 · 力竭反转',
  'confirmation/trend': '确认 · 趋势',
  'confirmation/breakout': '确认 · 突破',
  'cross-sectional/relative-value': '横截面 · 相对价值',
  'volatility-conditional/momentum': '波动条件 · 动量',
  'volatility-conditional/trend': '波动条件 · 趋势',
  'trend/weekly': '趋势 · 周线突破',
  'trend + exit-rule': '趋势 · 离场规则',
  'cross-asset/confirmation': '跨资产 · 方向确认',
  '跨资产/价差': '跨资产 · 协整价差',
  '基差/期限结构': '基差 · 期限结构',
  '均值回归': '均值回归',
  '清算/结构': '清算 · 连续波动结构',
  '跨资产/相关性': '跨资产 · 相关性区制',
  'orderbook/confirmation-filter': '订单簿 · 入场确认过滤',
  'microstructure/orderbook-confirmation': '订单簿 · 入场确认过滤',
  unknown: '未知',
});

function keyOf(value: unknown): string {
  return typeof value === 'string' ? value.trim().toLowerCase() : '';
}

/** Lists show translated labels; callers can expose the original code in details. */
export function labReason(value: unknown): string {
  const key = keyOf(value);
  if (!key) return '原因未知';
  return (
    key
      .split(/[;；]/u)
      .map((reason) => reason.trim())
      .filter(Boolean)
      .map((reason) => reasonLabels[reason] ?? '未识别原因')
      .join('；') || '原因未知'
  );
}

/** Research metadata can contain explanatory Chinese prose instead of event codes. */
export function labResearchReason(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) return '原因未知';
  const text = value.trim();
  return reasonLabels[keyOf(text)] ?? (/\p{Script=Han}/u.test(text) ? text : '未识别研究原因');
}

export function labOrderStatus(value: unknown): string {
  return orderStatusLabels[keyOf(value)] ?? '未知状态';
}

export function labEventType(value: unknown): string {
  return eventTypeLabels[keyOf(value)] ?? '未识别事件';
}

export function labFamily(value: unknown): string {
  const key = keyOf(value).replace(/\s*\/\s*/gu, '/');
  return familyLabels[key] ?? (typeof value === 'string' && /\p{Script=Han}/u.test(value) ? value.trim() : '未知机制');
}

/** A missing fill value is distinct from a known numeric zero. */
export function labFillValue(
  row: Record<string, unknown>,
  value: unknown,
  digits = 2,
): string {
  const parsed =
    (typeof value === 'number' || typeof value === 'string') &&
    String(value).trim() !== ''
      ? Number(value)
      : Number.NaN;
  if (Number.isFinite(parsed)) {
    return parsed.toLocaleString('zh-CN', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }
  const status = keyOf(row.status);
  return ['pending', 'cancelled', 'canceled', 'rejected'].includes(status)
    ? '尚未成交'
    : '未知';
}

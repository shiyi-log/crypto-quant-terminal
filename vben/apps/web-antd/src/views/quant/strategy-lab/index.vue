<script lang="ts" setup>
import type {
  StrategyLabBlockedStrategy,
  StrategyLabAccount,
  StrategyLabDetail,
  StrategyLabOverview,
  StrategyLabRejectedStrategy,
  StrategyLabRow,
} from '#/api/strategy-lab';
import type { LabPopulation, LabRiskFilter, LabStopFilter } from './accountFilters';

import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';

import {
  Alert,
  Button,
  Card,
  Descriptions,
  DescriptionsItem,
  Drawer,
  Empty,
  Select,
  Spin,
  Table,
  TabPane,
  Tabs,
  Tag,
} from 'ant-design-vue';

import { errText } from '#/api/freqtrade/client';
import { getStrategyLab, getStrategyLabDetail } from '#/api/strategy-lab';
import { fullOrderTime } from '../utils/orderDisplayId';
import AccountEquityChart from './AccountEquityChart.vue';
import {
  canonicalLabStrategy,
  filterLabAccounts,
  labAccountFactors,
  labAccountPopulation,
  labWarmupCandleTime,
  selectLabAccount,
  selectedLabDetail,
  selectedLabExecution,
} from './accountFilters';
import { labEventType, labFamily, labOrderStatus, labReason, labResearchReason } from './displayLabels';

const overview = ref<StrategyLabOverview | null>(null);
const detail = ref<StrategyLabDetail | null>(null);
const selectedId = ref('');
const population = ref<LabPopulation>('factorial');
const riskFilter = ref<LabRiskFilter>('R50');
const stopFilter = ref<LabStopFilter>('1.5');
const route = useRoute();
const section = ref('positions');
const loading = ref(false);
const detailLoading = ref(false);
const error = ref('');
const detailError = ref('');
const recordDetail = ref<Record<string, unknown> | null>(null);
const recordDetailOpen = ref(false);
const refreshedAt = ref<number | null>(null);
let overviewController: AbortController | undefined;
let detailController: AbortController | undefined;
let overviewSequence = 0;
let detailSequence = 0;
let timer: ReturnType<typeof setInterval> | undefined;

const focusOnly = computed(() => route.name === 'QuantStrategyFocus');
const scopedAccounts = computed(() => {
  const all = overview.value?.accounts ?? [];
  if (!focusOnly.value) return all;
  return all.filter((item) => {
    const baseId = canonicalLabStrategy(item);
    return baseId === 's03_tsmom_vol_target' || baseId === 's05_bollinger_fade';
  });
});
const accounts = computed(() => filterLabAccounts(
  scopedAccounts.value, population.value, riskFilter.value, stopFilter.value,
));
const populationLabels: Record<LabPopulation, string> = {
  factorial: 'ATR 因子账户',
  controls: '旧规则对照账户',
  archived: '已归档账户',
};
const populationOptions = computed(() =>
  (['factorial', 'controls', 'archived'] as const).map((value) => ({
    value,
    label: `${populationLabels[value]}（${scopedAccounts.value.filter((item) => labAccountPopulation(item) === value).length}）`,
  })),
);
const riskOptions = [
  { value: 'R50', label: 'R50 · 50 USDT' },
  { value: 'R200', label: 'R200 · 200 USDT' },
  { value: 'R1000', label: 'R1000 · 1,000 USDT' },
  { value: 'all', label: '全部风险档' },
];
const stopOptions = [
  { value: '1', label: 'k = 1.0' },
  { value: '1.5', label: 'k = 1.5' },
  { value: '2.5', label: 'k = 2.5' },
  { value: 'all', label: '全部止损宽度' },
];
const filterScope = computed(() => {
  const count = scopedAccounts.value.filter((item) => labAccountPopulation(item) === population.value).length;
  const factors = population.value === 'factorial'
    ? [riskFilter.value === 'all' ? '全部 R 档' : riskFilter.value,
        stopFilter.value === 'all' ? '全部 k 档' : `k${stopFilter.value}`].join(' · ')
    : '保留原冻结规则与样本';
  return `${focusOnly.value ? '重点策略范围 · ' : ''}${populationLabels[population.value]} · ${factors} · 当前显示 ${accounts.value.length} / ${count} 个账户`;
});
const blockedStrategies = computed<StrategyLabBlockedStrategy[]>(() =>
  focusOnly.value ? [] : overview.value?.blocked_strategies ?? [],
);
const blockedStrategySummary = computed(() =>
  blockedStrategies.value
    .map((item) => {
      const label = item.label ?? item.strategy_id;
      const reason = item.reason ?? 'awaiting_data';
      return `${label}：${labResearchReason(reason)}`;
    })
    .join('；'),
);
const rejectedStrategies = computed<StrategyLabRejectedStrategy[]>(() =>
  focusOnly.value ? [] : overview.value?.rejected_strategies ?? [],
);
const rejectedStrategySummary = computed(() =>
  rejectedStrategies.value
    .map((item) => `${item.label ?? item.strategy_id}：${labResearchReason(item.reason)}`)
    .join('；'),
);
const currentDetail = computed(() => selectedLabDetail(detail.value, selectedId.value));
const account = computed(() => currentDetail.value?.account ??
  accounts.value.find((item) => item.strategy_id === selectedId.value));
const definition = computed(() => account.value?.definition ?? {});
const execution = computed(
  () => selectedLabExecution(account.value,
    overview.value?.execution ?? overview.value?.execution_model ?? {}),
);
const orders = computed(() => currentDetail.value?.orders ?? []);
const trades = computed(() => currentDetail.value?.trades ?? []);
const events = computed(() => currentDetail.value?.events ?? []);
const positions = computed(() => account.value?.open_positions ?? []);
const equityPoints = computed(() => currentDetail.value?.equity_curve ?? []);
const factorialAccount = computed(() => account.value &&
  labAccountPopulation(account.value) === 'factorial');
const accountFactors = computed(() => labAccountFactors(account.value));
const atrStopMult = computed(() => numeric(execution.value.risk_stop_atr_mult ??
  definition.value.params_frozen?.risk_stop_atr_mult ??
  definition.value.params?.risk_stop_atr_mult));
const riskBudget = computed(() => execution.value.max_loss_per_trade_usdt ??
  definition.value.max_loss_per_trade_usdt);
const executionBlockReason = computed(() => {
  const blockers = definition.value.execution_blockers;
  return blockers?.length ? blockers.map(labResearchReason).join('；')
    : labResearchReason(execution.value.blocked_execution_reason ??
      definition.value.blocked_execution_reason);
});
const executionBlocked = computed(() => execution.value.execution_enabled === false ||
  definition.value.execution_enabled === false);
const executionUniverse = computed(() => execution.value.execution_universe ??
  definition.value.execution_universe ?? definition.value.universe);
const feeRate = computed(
  () => execution.value.fee_rate_per_side ?? execution.value.taker_fee_per_side,
);
const closedCount = computed(
  () => account.value?.closed_trade_count ?? account.value?.trade_count,
);
const feesPaid = computed(
  () => account.value?.fees_paid ?? account.value?.fee_paid,
);

function numeric(value: unknown): number | null {
  if (
    value === null ||
    value === undefined ||
    value === '' ||
    typeof value === 'boolean'
  )
    return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function fixed(value: unknown, digits = 2) {
  const parsed = numeric(value);
  return parsed === null
    ? '未知'
    : parsed.toLocaleString('zh-CN', {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      });
}

function percent(value: unknown) {
  const parsed = numeric(value);
  return parsed === null ? '未知' : `${(parsed * 100).toFixed(2)}%`;
}

function pnlClass(value: unknown) {
  const parsed = numeric(value);
  return parsed !== null && parsed > 0
    ? 'profit-up'
    : parsed !== null && parsed < 0
      ? 'profit-down'
      : '';
}

function total(key: keyof StrategyLabAccount) {
  if (!accounts.value.length) return null;
  const values = accounts.value.map((item) => numeric(item[key]));
  return values.every((value) => value !== null)
    ? (values as number[]).reduce((sum, value) => sum + value, 0)
    : null;
}

function valueOf(row: Record<string, unknown>, ...keys: string[]): unknown {
  for (const key of keys)
    if (row[key] !== null && row[key] !== undefined) return row[key];
  return null;
}

function textOf(row: Record<string, unknown>, ...keys: string[]) {
  const value = valueOf(row, ...keys);
  return value === null || value === '' ? '未知' : String(value);
}

function sideText(value: unknown) {
  if (value === 'long' || value === 1 || value === 'buy') return '做多';
  if (value === 'short' || value === -1 || value === 'sell') return '做空';
  return value === null || value === undefined ? '未知' : String(value);
}

function strategyLeverage() {
  const accountExecution = account.value?.execution;
  const accountLeverage =
    accountExecution && typeof accountExecution === 'object'
      ? numeric((accountExecution as Record<string, unknown>).leverage)
      : null;
  return (
    accountLeverage ??
    numeric(definition.value.position_budget?.leverage) ??
    numeric(execution.value.leverage) ??
    null
  );
}

function positionLiquidationPrice(row: Record<string, unknown>) {
  return numeric(valueOf(row, 'liquidation_price', 'liq_price'));
}

function positionDirection(row: Record<string, unknown>) {
  const value = valueOf(row, 'direction', 'side');
  if (value === 'short' || value === 'sell' || value === -1) return -1;
  if (value === 'long' || value === 'buy' || value === 1) return 1;
  return null;
}

function positionRiskDistance(row: Record<string, unknown>) {
  const liquidation = positionLiquidationPrice(row);
  const mark = numeric(valueOf(row, 'mark_price', 'current_price', 'price'));
  const direction = positionDirection(row);
  if (liquidation === null || mark === null || mark === 0 || direction === null)
    return null;
  return direction * (mark - liquidation) / mark;
}

function riskDistanceText(row: Record<string, unknown>) {
  const distance = positionRiskDistance(row);
  if (distance === null) return '未知';
  if (distance <= 0) return `已触发 ${percent(distance)}`;
  return percent(distance);
}

function riskDistanceClass(row: Record<string, unknown>) {
  const distance = positionRiskDistance(row);
  if (distance === null) return '';
  if (distance <= 0) return 'risk-critical';
  if (distance <= 0.0025) return 'risk-high';
  if (distance <= 0.01) return 'risk-watch';
  return '';
}

function protectionType(row: Record<string, unknown>) {
  const liquidation = positionLiquidationPrice(row);
  const leverage = numeric(valueOf(row, 'leverage')) ?? strategyLeverage();
  const params = definition.value.params_frozen ?? {};
  const hasAtrProtection =
    numeric(params.stop_atr_mult) !== null && numeric(params.tp_atr_mult) !== null;
  const hasRiskStop = atrStopMult.value !== null;
  if (liquidation !== null && leverage !== null && leverage > 1) {
    return `${fixed(leverage, 0)} 倍强平 · ${hasRiskStop ? 'ATR 风险止损' : hasAtrProtection ? 'ATR 止损/止盈' : '策略离场'}`;
  }
  if (hasRiskStop) return 'ATR 风险止损';
  if (hasAtrProtection) return 'ATR 止损/止盈';
  return leverage === 1 ? '未启用统一强平' : '风险保护未知';
}

function liquidationRuleText() {
  const leverage = strategyLeverage();
  if (leverage === null || leverage <= 1)
    return '当前账户未启用统一杠杆强平模型；请以策略离场规则为准。';
  return `杠杆 ${fixed(leverage, 0)} 倍：多头价格跌至入场价 × (1 − 1/${fixed(leverage, 0)})，空头价格涨至入场价 × (1 + 1/${fixed(leverage, 0)}) 时，逐根使用已收盘 K 线的最低价/最高价触发强平，保证金按规则全额损失。`;
}

function orderAction(value: unknown) {
  return value === 'entry'
    ? '开仓'
    : value === 'exit'
      ? '平仓'
      : value === null
        ? '未知'
        : String(value);
}

function paperNumber(row: Record<string, unknown>) {
  const time = fullOrderTime(
    valueOf(row, 'signal_candle_ms', 'opened_candle_ms', 'entry_timestamp', 'created_at', 'timestamp'),
  );
  const stamp =
    time === '—'
      ? '时间未知'
      : time.replaceAll('-', '').replace(' ', '-').replaceAll(':', '');
  const coin =
    typeof row.pair === 'string' ? row.pair.split('/')[0] : '币种未知';
  return `${stamp}-${coin}`;
}

function showRecord(row: Record<string, unknown>) {
  recordDetail.value = row;
  recordDetailOpen.value = true;
}

function fillValue(row: Record<string, unknown>, value: unknown, digits = 4) {
  if (numeric(value) !== null) return fixed(value, digits);
  return row.status === 'pending' ? '尚未成交'
    : row.status === 'cancelled' || row.status === 'rejected' ? '未成交' : '未知';
}

function recordStatus(row: Record<string, unknown>) {
  return row.trade_id && row.closed_candle_ms != null ? '已平仓' : labOrderStatus(row.status);
}

function statusOf(value: unknown) {
  const key = typeof value === 'string' ? value : 'unknown';
  const labels: Record<string, { color: string; label: string }> = {
    running: { color: 'blue', label: '观测运行中' },
    ready: { color: 'blue', label: '等待交易信号' },
    active: { color: 'blue', label: '前向模拟中' },
    initialized: { color: 'default', label: '账户已初始化' },
    warmup: { color: 'orange', label: '数据预热中' },
    warming_up: { color: 'orange', label: '数据预热中' },
    warming: { color: 'orange', label: '数据预热中' },
    blocked: { color: 'orange', label: '数据受阻' },
    execution_blocked: { color: 'orange', label: '执行暂停，保留研究' },
    research_paused: { color: 'orange', label: '研究依赖暂停' },
    error: { color: 'red', label: '运行错误' },
    stopped: { color: 'default', label: '已停止' },
    starting: { color: 'orange', label: '执行器启动中' },
    observing: { color: 'blue', label: '前向模拟观测中' },
    waiting_new_closed_candle: { color: 'blue', label: '等待新收盘 K 线' },
    awaiting_new_closed_bar: { color: 'blue', label: '等待新收盘 K 线' },
    waiting_aligned_data: { color: 'orange', label: '等待两币数据对齐' },
    data_stale: { color: 'orange', label: '行情数据已过期' },
    archived: { color: 'default', label: '已归档' },
    rejected: { color: 'default', label: '研究已淘汰，不进入模拟' },
    awaiting_data: { color: 'orange', label: '等待数据' },
    funding_data_pending: { color: 'orange', label: '资金费数据待接入' },
    data_blocked: { color: 'orange', label: '数据阻塞，暂不运行' },
    cross_sectional_execution_pending: { color: 'orange', label: '等待多腿执行能力' },
    awaiting_native_timeframe_adapter: { color: 'orange', label: '等待周期数据适配' },
    funding_data_unavailable: { color: 'orange', label: '资金费率数据缺失' },
    stopped_or_stale: { color: 'orange', label: '执行器已停或状态过期' },
    not_started: { color: 'default', label: '执行器尚未启动' },
    startup_failed: { color: 'red', label: '执行器启动失败' },
    unknown: { color: 'default', label: '状态未知' },
  };
  return labels[key] ?? { color: 'default', label: key };
}

function accountStatus(item: StrategyLabAccount) {
  if (labAccountPopulation(item) === 'archived') return statusOf('archived');
  if (item.execution?.execution_enabled === false || item.definition?.execution_enabled === false)
    return statusOf('execution_blocked');
  return statusOf(item.status);
}

function factorLabel(item: StrategyLabAccount) {
  const factors = labAccountFactors(item);
  return factors.riskBudget
    ? `${factors.riskBudget} · k${fixed(factors.stopWidth, 1)}` : '';
}

function atrRiskText() {
  return `R = ${fixed(riskBudget.value, 0)} USDT 是按入场冻结 ATR 止损距离与双边手续费反解仓位的计划预算；k = ${fixed(atrStopMult.value, 1)}。止损由收盘价确认，按下一根开盘价代理成交；跳空、滑点与 ${fixed(strategyLeverage(), 0)} 倍强平可能使实际亏损超过 R。`;
}

function warmupText(item: StrategyLabAccount) {
  const state = item.warmup;
  if (!state || !Object.keys(state).length) return '预热进度未知';
  if ('valid_bars' in state || 'required_bars' in state) {
    return `${fixed(state.valid_bars, 0)} / ${fixed(state.required_bars, 0)} 根${state.ready ? ' · 就绪' : ''}`;
  }
  const rows = Object.entries(state).filter(
    ([, row]) => row && typeof row === 'object',
  );
  if (rows.length)
    return rows
      .map(([pair, row]) => {
        const data = row as Record<string, unknown>;
        return `${pair.split('/')[0]} ${fixed(data.valid_bars ?? data.available_bars, 0)} / ${fixed(data.required_bars, 0)}${data.ready ? ' ✓' : ''}`;
      })
      .join(' · ');
  return state.ready === true
    ? '数据就绪'
    : labReason(state.reason ?? 'awaiting_data');
}

function accountName(item?: StrategyLabAccount) {
  const fallbackLabels: Record<string, string> = {
    s03_tsmom_vol_target: '时序动量（波动率目标）',
    s05_bollinger_fade: '布林带偏离回归',
    s11_tsmom_bollinger_pullback: '时序动量 × 布林回归',
    s31_delta_neutral_carry: '资金费率 Delta 中性 Carry',
  };
  const fallback = item?.strategy_id
    ? fallbackLabels[canonicalLabStrategy(item)]
    : undefined;
  return item?.label ?? item?.name ?? item?.definition?.name ?? fallback ?? item?.strategy_id ?? '未知';
}

function armId(item?: StrategyLabAccount) {
  return String(item?.arm_id ?? item?.definition?.arm_id ?? '').trim();
}

function armLabel(item?: StrategyLabAccount) {
  const value = armId(item);
  const labels: Record<string, string> = {
    A_5m_100x_taker: 'A 臂 · 5m · 已归档',
    B_15m_maker: 'B 臂 · 15m · maker',
    C_gen2_multitf: 'C 臂 · 第二代 · 多周期',
    D_gen3_multitf: 'D 臂 · 第三代 · 日线',
    E_carry_delta_neutral: 'E 臂 · 资金费 Carry',
  };
  return labels[value] ?? (value || '实验臂未知');
}

function dataStatusLabel(item?: StrategyLabAccount) {
  const value = String(item?.data_status ?? item?.definition?.data_status ?? '').trim();
  const labels: Record<string, string> = {
    native_timeframe_adapter_active: '原生周期数据已接入',
    cross_sectional_execution_pending: '横截面执行待接入',
    funding_data_pending: '资金费数据待接入',
    funding_series_loaded_rejected: '资金费数据已入库 · 研究已淘汰',
    research_context_paused: '所需多币研究上下文已暂停',
    atr_risk_execution_pending: 'ATR 风险执行规则待接入',
  };
  return labels[value] ?? (value ? labReason(value) : '数据状态未知');
}

function hasDataStatus(item?: StrategyLabAccount) {
  return Boolean(String(item?.data_status ?? item?.definition?.data_status ?? '').trim());
}

type ObservationBadge = {
  color: string;
  label: string;
  note: string;
};

function observationBadge(strategyOrAccount: unknown): ObservationBadge | null {
  const account = strategyOrAccount && typeof strategyOrAccount === 'object'
    ? (strategyOrAccount as StrategyLabAccount)
    : undefined;
  const strategyId = account?.strategy_id ?? strategyOrAccount;
  const baseId = account ? canonicalLabStrategy(account)
    : canonicalLabStrategy(typeof strategyId === 'string' ? strategyId : '');
  const searchText = `${String(strategyId ?? '')} ${String(account?.label ?? '')} ${String(account?.name ?? '')}`.toLowerCase();
  if (
    baseId === 's11_tsmom_bollinger_pullback' ||
    /s03.*s05|s05.*s03|tsmom.*bollinger|bollinger.*tsmom|combo|hybrid/.test(searchText)
  ) {
    return {
      color: 'purple',
      label: '组合观察',
      note: '时序动量方向过滤 × 布林带偏离回归，100 倍账户独立观察。',
    };
  }
  if (baseId === 's03_tsmom_vol_target') {
    return {
      color: 'gold',
      label: '重点观察',
      note: '重点观察时序动量的方向持续性、波动率定尺与强平风险。',
    };
  }
  if (baseId === 's05_bollinger_fade') {
    return {
      color: 'cyan',
      label: '重点观察',
      note: '重点观察布林带偏离回归的反转质量、持仓周期与强平风险。',
    };
  }
  return null;
}

function isFocusStrategy(strategyOrAccount: unknown) {
  return observationBadge(strategyOrAccount) !== null;
}

function observationLabel(strategyOrAccount: unknown) {
  return observationBadge(strategyOrAccount)?.label ?? '';
}

function observationColor(strategyOrAccount: unknown) {
  return observationBadge(strategyOrAccount)?.color ?? 'default';
}

function observationNote(strategyOrAccount: unknown) {
  return observationBadge(strategyOrAccount)?.note ?? '';
}

function accountDataStatus(item?: StrategyLabAccount) {
  const status = item?.data_status ?? item?.definition?.data_status;
  if (status === 'cross_sectional_execution_pending') {
    return '横截面信号需要多币种、多腿同时执行；当前账户仅保留研究上下文，未将 20 币误当作下单腿。';
  }
  if (status === 'funding_data_unavailable' || status === 'funding_data_pending') {
    return '该账户所需的资金费率事件数据尚未就绪；该策略暂不生成模拟成交。';
  }
  if (status === 'funding_series_loaded_rejected') {
    return '资金费率事件数据已入库；该研究经成本与近期表现复核后已淘汰，不进入前向模拟。';
  }
  if (status === 'native_timeframe_adapter_active') {
    return '该账户按策略原生周期读取已收盘 K 线；预热完成前不会生成模拟成交。';
  }
  if (status === 'research_context_paused') {
    return '本策略依赖完整的多币市场宽度或排名；其他币线已暂停，因此保留原机制并暂停执行，不能用三币替代原来的 20 币上下文。';
  }
  if (status === 'atr_risk_execution_pending') {
    return '所需 ATR 风险执行规则尚未实现，账户已登记并保留研究，当前不生成模拟订单。';
  }
  return '';
}

const accountEmptyDescription = computed(() =>
  `当前筛选没有账户（${populationLabels[population.value]}）；可调整账户分组、风险档或止损宽度。`,
);

function eventSummary(row: Record<string, unknown>) {
  const payload =
    row.payload && typeof row.payload === 'object'
      ? (row.payload as Record<string, unknown>)
      : row;
  const reason = valueOf(payload, 'reason', 'message');
  if (reason !== null) return labReason(reason);
  if (Array.isArray(payload.signals)) {
    return (
      payload.signals
        .map((value) => {
          const signal = value as Record<string, unknown>;
          const target = signal.target_side == null ? '维持现状'
            : signal.target_side === 0 ? '空仓' : sideText(signal.target_side);
          return `${textOf(signal, 'pair')} · ${target} · ${labReason(signal.reason)}`;
        })
        .join('；') || (payload.ready ? '数据就绪，等待信号' : '等待完整数据')
    );
  }
  if (row.event_type === 'account_created')
    return '独立模拟账户初始化，不补造历史交易';
  if (row.event_type === 'equity_snapshot')
    return `净值 ${fixed(payload.equity)} USDT；已实现 ${fixed(payload.realized_pnl)}；浮盈 ${fixed(payload.unrealized_pnl)}`;
  return '未知';
}

function rowKey(row: StrategyLabRow) {
  return textOf(
    row,
    'order_id',
    'paper_order_id',
    'trade_id',
    'position_id',
    'event_id',
    'timestamp',
  );
}

const positionColumns = [
  { title: '币种 / 配对组', key: 'pair', width: 200 },
  { title: '方向', key: 'side', width: 80 },
  { title: '入场时间（北京）', key: 'opened', width: 180 },
  { title: '数量', key: 'quantity', align: 'right' as const },
  { title: '投入 USDT', key: 'stake', align: 'right' as const },
  { title: '入场 / 标记价格', key: 'prices', align: 'right' as const },
  { title: '杠杆', key: 'leverage', width: 72, align: 'right' as const },
  { title: '强平价', key: 'liquidation', width: 110, align: 'right' as const },
  { title: '距强平', key: 'distance', width: 105, align: 'right' as const },
  { title: '保护类型', key: 'protection', width: 190 },
  { title: '浮盈 USDT', key: 'pnl', align: 'right' as const },
];

const orderColumns = [
  { title: '模拟订单编号', key: 'id', width: 190 },
  { title: '币种 / 配对组', key: 'pair', width: 180 },
  { title: '动作 / 方向', key: 'side', width: 140 },
  { title: '状态', key: 'status', width: 110 },
  { title: '信号 / 成交时间（北京）', key: 'time', width: 180 },
  { title: '成交价格', key: 'price', align: 'right' as const },
  { title: '手续费 USDT', key: 'fee', align: 'right' as const },
  { title: '原因', key: 'reason', width: 200 },
];

const tradeColumns = [
  { title: '成交记录 / 币种', key: 'id', width: 260 },
  { title: '方向', key: 'side', width: 80 },
  { title: '入场 / 离场时间（北京）', key: 'time', width: 180 },
  { title: '毛收益 USDT', key: 'gross', align: 'right' as const },
  { title: '手续费 USDT', key: 'fee', align: 'right' as const },
  { title: '扣手续费收益 USDT', key: 'net', align: 'right' as const },
  { title: '离场原因', key: 'reason', width: 160 },
];

const eventColumns = [
  { title: '记录时间（北京）', key: 'time', width: 180 },
  { title: '事件', key: 'kind', width: 160 },
  { title: '原因 / 说明', key: 'reason' },
];

async function refreshDetail() {
  detailController?.abort();
  const current = ++detailSequence;
  const strategyId = selectedId.value;
  detailError.value = '';
  if (!strategyId) {
    detail.value = null;
    detailLoading.value = false;
    return;
  }
  const controller = new AbortController();
  detailController = controller;
  detailLoading.value = true;
  try {
    const response = await getStrategyLabDetail(
      strategyId,
      controller.signal,
    );
    if (current !== detailSequence || strategyId !== selectedId.value) return;
    detail.value = response;
  } catch (reason) {
    if (current === detailSequence && strategyId === selectedId.value && !controller.signal.aborted) {
      detail.value = null;
      detailError.value = errText(reason);
    }
  } finally {
    if (current === detailSequence && strategyId === selectedId.value) detailLoading.value = false;
  }
}

async function refresh() {
  overviewController?.abort();
  overviewController = new AbortController();
  const current = ++overviewSequence;
  loading.value = true;
  error.value = '';
  try {
    const response = await getStrategyLab(overviewController.signal);
    if (current !== overviewSequence) return;
    const previousId = selectedId.value;
    overview.value = response;
    refreshedAt.value = Date.now();
    if (previousId && selectedId.value === previousId) await refreshDetail();
  } catch (reason) {
    if (current === overviewSequence) error.value = errText(reason);
  } finally {
    if (current === overviewSequence) loading.value = false;
  }
}

watch(accounts, (items) => {
  selectedId.value = selectLabAccount(items, selectedId.value, focusOnly.value);
}, { flush: 'sync' });
watch(selectedId, () => {
  recordDetailOpen.value = false;
  recordDetail.value = null;
  detail.value = null;
  void refreshDetail();
}, { flush: 'sync' });
watch(
  () => route.name,
  (name, previous) => {
    if (name === previous) return;
    void refresh();
  },
);
onMounted(() => {
  void refresh();
  timer = setInterval(() => void refresh(), 30_000);
});
onUnmounted(() => {
  overviewSequence++;
  detailSequence++;
  overviewController?.abort();
  detailController?.abort();
  if (timer) clearInterval(timer);
});
</script>

<template>
  <div class="p-4 strategy-lab">
    <Card
      :bordered="false"
      class="shadow-sm"
      :title="focusOnly ? '重点策略观察' : '多策略模拟实验室'"
    >
      <template #extra>
        <Button :loading="loading || detailLoading" @click="refresh"
          >刷新账户</Button
        >
      </template>
      <div class="flex flex-wrap items-center gap-2">
        <Tag color="blue">前向研究假设</Tag>
        <Tag :color="overview?.service?.running ? 'blue' : 'default'">
          {{
            overview?.service?.running === true
              ? '模拟执行器运行中'
              : overview?.service?.running === false
                ? '模拟执行器未运行'
                : '运行状态未知'
          }}
        </Tag>
        <Tag
          v-if="overview?.service?.status"
          :color="statusOf(overview.service.status).color"
        >
          {{ statusOf(overview.service.status).label }}
        </Tag>
        <span class="text-xs text-muted-foreground"
          >独立账户 · 独立订单 · 只使用已收盘数据</span
        >
      </div>
      <div class="mt-4 grid grid-cols-1 gap-3 md:grid-cols-4">
        <div class="metric-cell">
          <div class="metric-label">当前筛选账户</div>
          <div class="metric-value">
            {{ overview ? accounts.length : '未知' }}
          </div>
          <div class="metric-sub">
            {{ populationLabels[population] }} · {{ population === 'factorial' ? '每个 R/k 组合有独立账户' : '保留原规则与历史数据' }}
          </div>
        </div>
        <div class="metric-cell">
          <div class="metric-label">初始模拟资金合计</div>
          <div class="metric-value">{{ fixed(total('initial_capital')) }}</div>
          <div class="metric-sub">USDT · 仅当前筛选，账户不共享资金</div>
        </div>
        <div class="metric-cell">
          <div class="metric-label">账户净值合计</div>
          <div class="metric-value">{{ fixed(total('equity')) }}</div>
          <div class="metric-sub">USDT · 仅当前筛选，不代表可调拨资金</div>
        </div>
        <div class="metric-cell">
          <div class="metric-label">最近执行扫描（北京）</div>
          <div class="metric-value metric-time">
            {{ fullOrderTime(overview?.service?.last_cycle_at) }}
          </div>
          <div class="metric-sub">
            页面更新 {{ fullOrderTime(refreshedAt) }}
          </div>
        </div>
      </div>
      <div class="mt-4 flex flex-wrap items-center gap-3">
        <label class="lab-filter">
          <span>账户分组</span>
          <Select v-model:value="population" :options="populationOptions" class="population-select" aria-label="账户分组" />
        </label>
        <label class="lab-filter">
          <span>计划风险预算</span>
          <Select v-model:value="riskFilter" :options="riskOptions" :disabled="population !== 'factorial'" class="risk-select" aria-label="计划风险预算" />
        </label>
        <label class="lab-filter">
          <span>止损宽度</span>
          <Select v-model:value="stopFilter" :options="stopOptions" :disabled="population !== 'factorial'" class="stop-select" aria-label="止损宽度" />
        </label>
      </div>
      <p class="mt-3 text-xs text-muted-foreground">{{ filterScope }}。资金与净值合计只对应当前筛选；旧对照与新因子样本分开统计。</p>
    </Card>

    <Alert v-if="error" class="mt-3" type="error" show-icon :message="error" />
    <Alert
      v-if="overview?.service?.last_error"
      class="mt-3"
      type="error"
      show-icon
      message="模拟执行器最近发生错误"
      :description="overview.service.last_error"
    />
    <Alert
      class="mt-3"
      type="info"
      show-icon
      :message="focusOnly ? '重点观察：时序动量与布林带偏离回归' : '这些策略是待检验的研究假设；每项前向模拟独立记录，已有 TrendFollowing 模拟盘继续独立运行。'"
      :description="`成交按下一根开盘价代理；所选账户手续费 ${percent(feeRate)}/边。限价单未成交风险、滑点与资金费未知，扣手续费收益不能视为完整净收益。只记录上线后的真实信号，不补造历史模拟成交。`"
    />
    <Alert
      v-if="blockedStrategies.length"
      class="mt-3"
      type="warning"
      show-icon
      message="部分研究策略尚未进入模拟执行"
      :description="`${blockedStrategySummary}。这些策略不会伪造成交，也不会计入已登记账户。`"
    />
    <Alert
      v-if="rejectedStrategies.length"
      class="mt-3"
      type="info"
      show-icon
      message="已淘汰的研究候选"
      :description="`${rejectedStrategySummary}。仅保留审计记录，不启用账户，不计入活动模拟。`"
    />

    <Spin :spinning="loading && !overview">
      <Card :bordered="false" class="mt-3 shadow-sm">
        <Empty
          v-if="overview && !accounts.length"
          :description="accountEmptyDescription"
        />
        <Empty
          v-else-if="!overview && !loading"
          description="策略账户数据暂不可用，请点击“刷新账户”重试"
        />
        <Tabs
          v-else-if="overview && accounts.length"
          v-model:active-key="selectedId"
          class="strategy-tabs"
          size="small"
        >
          <TabPane v-for="item in accounts" :key="item.strategy_id">
            <template #tab>
              <div class="strategy-tab">
                <span class="flex items-center gap-1">
                  <span>{{ accountName(item) }}</span>
                  <Tag
                    v-if="isFocusStrategy(item)"
                    :color="observationColor(item)"
                    >{{ observationLabel(item) }}</Tag
                  >
                </span>
                <span class="strategy-tab-sub"
                  >{{ item.strategy_id.split('_')[0]?.toUpperCase() }} ·
                  {{ accountStatus(item).label }} · {{ factorLabel(item) || armLabel(item) }}</span
                >
              </div>
            </template>
          </TabPane>
        </Tabs>

        <Alert
          v-if="detailError"
          class="mb-3"
          type="error"
          show-icon
          :message="detailError"
        />
        <Spin :spinning="detailLoading">
          <Empty
            v-if="!account && !detailLoading"
            description="当前策略明细暂不可用，请重新选择或刷新账户"
          />
          <template v-else-if="account">
            <div class="mb-4 flex flex-wrap items-center gap-2">
              <span class="text-lg font-semibold">{{
                accountName(account)
              }}</span>
              <Tag
                v-if="isFocusStrategy(account)"
                :color="observationColor(account)"
                >{{ observationLabel(account) }}</Tag
              >
              <Tag color="blue">前向研究假设</Tag>
              <Tag :color="accountStatus(account).color">{{
                accountStatus(account).label
              }}</Tag>
              <Tag v-if="factorialAccount" color="purple">{{ accountFactors.riskBudget }} · k{{ fixed(accountFactors.stopWidth, 1) }}</Tag>
              <Tag v-else>{{ populationLabels[labAccountPopulation(account)] }}</Tag>
              <Tag v-if="armId(account)" color="geekblue">{{ armLabel(account) }}</Tag>
              <Tag v-if="hasDataStatus(account)" color="orange">{{ dataStatusLabel(account) }}</Tag>
              <span class="text-xs text-muted-foreground"
                >{{ labFamily(account.family ?? definition.family) }} ·
                {{ account.timeframe ?? definition.timeframe ?? '5m' }} ·
                {{ armLabel(account) }}</span
              >
            </div>
            <div class="mb-4 rounded border p-3 text-xs text-muted-foreground">
              {{ warmupText(account) }} · 最新收盘 K 线起点（北京）
              {{ fullOrderTime(labWarmupCandleTime(account)) }}
              · 最近执行 K 线起点
              {{ account.last_candle_ms == null ? '尚无执行记录' : fullOrderTime(account.last_candle_ms) }}
              <span v-if="account.warmup?.reason && account.warmup.reason !== 'ready'">
                · {{ labReason(account.warmup.reason) }}</span
              >
              <span v-if="hasDataStatus(account)">
                · {{ dataStatusLabel(account) }}</span
              >
            </div>
            <Alert
              v-if="executionBlocked"
              class="mb-4"
              type="warning"
              show-icon
              message="执行暂停，保留研究账户与全部数据"
              :description="executionBlockReason"
            />
            <Alert
              v-if="factorialAccount"
              class="mb-4"
              type="info"
              show-icon
              :message="`本账户 ATR 风险档 ${accountFactors.riskBudget} · k${fixed(atrStopMult, 1)}`"
              :description="atrRiskText()"
            />
            <Alert
              v-else
              class="mb-4"
              type="info"
              show-icon
              message="保留原冻结规则与样本"
              description="旧账户作为独立对照保留，规则与交易记录不合并进 ATR 因子实验。"
            />
            <Alert
              v-if="isFocusStrategy(account)"
              class="mb-4"
              type="info"
              show-icon
              :message="`${observationLabel(account)}：${accountName(account)}`"
              :description="observationNote(account)"
            />
            <Alert
              v-if="accountDataStatus(account)"
              class="mb-4"
              type="warning"
              show-icon
              message="当前账户存在执行边界"
              :description="accountDataStatus(account)"
            />
            <div class="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
              <div class="metric-cell">
                <div class="metric-label">初始资金 USDT</div>
                <div class="metric-value">
                  {{ fixed(account.initial_capital) }}
                </div>
              </div>
              <div class="metric-cell">
                <div class="metric-label">可用现金 USDT</div>
                <div class="metric-value">{{ fixed(account.cash) }}</div>
              </div>
              <div class="metric-cell">
                <div class="metric-label">账户净值 USDT</div>
                <div class="metric-value">{{ fixed(account.equity) }}</div>
              </div>
              <div class="metric-cell">
                <div class="metric-label">已实现收益 USDT</div>
                <div
                  class="metric-value"
                  :class="pnlClass(account.realized_pnl)"
                >
                  {{ fixed(account.realized_pnl) }}
                </div>
              </div>
              <div class="metric-cell">
                <div class="metric-label">持仓浮盈 USDT</div>
                <div
                  class="metric-value"
                  :class="pnlClass(account.unrealized_pnl)"
                >
                  {{ fixed(account.unrealized_pnl) }}
                </div>
              </div>
              <div class="metric-cell">
                <div class="metric-label">已支付手续费 USDT</div>
                <div class="metric-value">{{ fixed(feesPaid, 4) }}</div>
              </div>
              <div class="metric-cell">
                <div class="metric-label">持仓 / 已平仓笔数</div>
                <div class="metric-value">
                  {{ fixed(account.open_position_count, 0) }} /
                  {{ fixed(closedCount, 0) }}
                </div>
              </div>
            </div>

            <div
              class="mt-3 flex flex-wrap items-center gap-4 text-xs text-muted-foreground"
            >
              <span
                >扣手续费回报
                {{
                  percent(account.total_return_ratio ?? account.return_pct)
                }}</span
              >
              <span>已平仓胜率 {{ numeric(closedCount) === 0 ? '暂无平仓样本' : percent(account.win_rate) }}</span>
              <span
                >观测最大回撤
                {{
                  percent(
                    account.max_drawdown_ratio ?? account.max_drawdown_pct,
                  )
                }}</span
              >
              <span
                >待成交订单 {{ fixed(account.pending_order_count, 0) }}</span
              >
            </div>
            <Alert
              v-if="numeric(closedCount) === 0"
              class="mt-3"
              type="info"
              show-icon
              message="已平仓 0 笔，尚无已实现策略表现样本。持仓浮盈与已平仓结果分开统计。"
            />

            <Tabs v-model:active-key="section" class="mt-5" size="small">
              <TabPane
                key="positions"
                :tab="`持仓（${fixed(account.open_position_count, 0)}）`"
              >
                <Alert
                  class="mb-3"
                  type="warning"
                  show-icon
                  message="持仓风险保护"
                  :description="`${liquidationRuleText()} ${atrStopMult != null ? atrRiskText() : definition.params_frozen?.stop_atr_mult != null ? '本策略同时按冻结的 ATR 止损/止盈规则，在收盘确认后按下一根开盘价代理成交。' : '本策略按冻结的收盘离场与持仓期限规则退出。'}`"
                />
                <Table
                  :columns="positionColumns"
                  :data-source="positions"
                  :row-key="rowKey"
                  :pagination="false"
                  size="small"
                  :scroll="{ x: 1430 }"
                >
                  <template #bodyCell="{ column, record }">
                    <template v-if="column.key === 'pair'"
                      >{{ textOf(record, 'pair') }}
                      <div v-if="record.group_id" class="row-sub">
                        双腿配对
                      </div></template
                    >
                    <template v-else-if="column.key === 'side'">{{
                      sideText(valueOf(record, 'side', 'direction'))
                    }}</template>
                    <template v-else-if="column.key === 'opened'">{{
                      fullOrderTime(
                        valueOf(
                          record,
                          'opened_candle_ms',
                          'opened_at',
                          'opened_at_ms',
                          'entry_timestamp',
                          'timestamp',
                        ),
                      )
                    }}</template>
                    <template v-else-if="column.key === 'quantity'">{{
                      fixed(valueOf(record, 'quantity', 'amount'), 6)
                    }}</template>
                    <template v-else-if="column.key === 'stake'">{{
                      fixed(
                        valueOf(
                          record,
                          'margin',
                          'stake',
                          'stake_amount',
                          'notional',
                        ),
                      )
                    }}</template>
                    <template v-else-if="column.key === 'prices'"
                      >{{ fixed(record.entry_price, 4) }} /
                      {{
                        fixed(valueOf(record, 'mark_price', 'current_price'), 4)
                      }}</template
                    >
                    <template v-else-if="column.key === 'leverage'">
                      {{ fixed(valueOf(record, 'leverage') ?? strategyLeverage(), 0) }} 倍
                    </template>
                    <template v-else-if="column.key === 'liquidation'">
                      {{ fixed(positionLiquidationPrice(record), 4) }}
                    </template>
                    <template v-else-if="column.key === 'distance'">
                      <span :class="riskDistanceClass(record)">
                        {{ riskDistanceText(record) }}
                      </span>
                    </template>
                    <template v-else-if="column.key === 'protection'">
                      {{ protectionType(record) }}
                    </template>
                    <template v-else-if="column.key === 'pnl'"
                      ><span :class="pnlClass(record.unrealized_pnl)">{{
                        fixed(record.unrealized_pnl)
                      }}</span></template
                    >
                  </template>
                  <template #emptyText
                    ><Empty description="当前策略暂无模拟持仓"
                  /></template>
                </Table>
              </TabPane>

              <TabPane key="orders" tab="模拟订单">
                <div class="mb-3 text-xs text-muted-foreground">
                  列表编号为时间戳加币种，点击查看完整模拟编号；纸面订单没有交易所原始订单号。每行归属当前策略
                  {{ selectedId }}。
                </div>
                <Table
                  :columns="orderColumns"
                  :data-source="orders"
                  :row-key="rowKey"
                  :pagination="{ pageSize: 10 }"
                  size="small"
                  :scroll="{ x: 1250 }"
                >
                  <template #bodyCell="{ column, record }">
                    <template v-if="column.key === 'id'">
                      <Button type="link" class="!p-0 font-mono text-xs" @click="showRecord(record)">{{ paperNumber(record) }}</Button>
                    </template>
                    <template v-else-if="column.key === 'pair'"
                      >{{ textOf(record, 'pair') }}
                      <div v-if="record.group_id" class="row-sub">
                        双腿配对
                      </div></template
                    >
                    <template v-else-if="column.key === 'side'"
                      >{{
                        orderAction(
                          valueOf(record, 'kind', 'action', 'order_type'),
                        )
                      }}
                      ·
                      {{
                        sideText(valueOf(record, 'side', 'direction'))
                      }}</template
                    >
                    <template v-else-if="column.key === 'status'"
                      ><Tag
                        :color="
                          record.status === 'filled'
                            ? 'blue'
                            : record.status === 'pending'
                              ? 'orange'
                              : 'default'
                        "
                        >{{ labOrderStatus(record.status) }}</Tag
                      ></template
                    >
                    <template v-else-if="column.key === 'time'"
                      >{{
                        fullOrderTime(
                          valueOf(
                            record,
                            'signal_candle_ms',
                            'signal_timestamp',
                            'signal_at_ms',
                            'created_at',
                            'timestamp',
                          ),
                        )
                      }}
                      <div class="row-sub">
                        成交
                        {{
                          fullOrderTime(
                            valueOf(
                              record,
                              'fill_candle_ms',
                              'filled_candle_ms',
                              'filled_at',
                              'filled_at_ms',
                              'fill_timestamp',
                            ),
                          )
                        }}
                      </div></template
                    >
                    <template v-else-if="column.key === 'price'">{{
                      fillValue(record, valueOf(record, 'fill_price', 'price'))
                    }}</template>
                    <template v-else-if="column.key === 'fee'">{{
                      fillValue(record, valueOf(record, 'fee', 'fee_paid'))
                    }}</template>
                    <template v-else-if="column.key === 'reason'">{{
                      labReason(valueOf(record, 'reason', 'signal_reason'))
                    }}</template>
                  </template>
                  <template #emptyText
                    ><Empty description="暂无模拟订单；等待数据就绪及实际信号"
                  /></template>
                </Table>
              </TabPane>

              <TabPane key="trades" tab="已平仓交易">
                <Table
                  :columns="tradeColumns"
                  :data-source="trades"
                  :row-key="rowKey"
                  :pagination="{ pageSize: 10 }"
                  size="small"
                  :scroll="{ x: 1100 }"
                >
                  <template #bodyCell="{ column, record }">
                    <template v-if="column.key === 'id'"
                      ><Button type="link" class="!p-0 font-mono text-xs" @click="showRecord(record)">{{ paperNumber(record) }}</Button>
                      <div class="row-sub">
                        {{ textOf(record, 'pair')
                        }}<span v-if="record.group_id">
                          · 双腿配对</span
                        >
                      </div></template
                    >
                    <template v-else-if="column.key === 'side'">{{
                      sideText(valueOf(record, 'side', 'direction'))
                    }}</template>
                    <template v-else-if="column.key === 'time'"
                      >{{
                        fullOrderTime(
                          valueOf(
                            record,
                            'opened_candle_ms',
                            'opened_at',
                            'entry_timestamp',
                            'opened_at_ms',
                          ),
                        )
                      }}
                      <div class="row-sub">
                        至
                        {{
                          fullOrderTime(
                            valueOf(
                              record,
                              'closed_candle_ms',
                              'closed_at',
                              'exit_timestamp',
                              'closed_at_ms',
                            ),
                          )
                        }}
                      </div></template
                    >
                    <template v-else-if="column.key === 'gross'"
                      ><span :class="pnlClass(record.gross_pnl)">{{
                        fixed(record.gross_pnl)
                      }}</span></template
                    >
                    <template v-else-if="column.key === 'fee'">{{
                      fixed(
                        valueOf(record, 'fees_paid', 'fee_paid', 'fees', 'fee'),
                        4,
                      )
                    }}</template>
                    <template v-else-if="column.key === 'net'"
                      ><span
                        :class="
                          pnlClass(
                            valueOf(record, 'net_pnl', 'realized_pnl', 'pnl'),
                          )
                        "
                        >{{
                          fixed(
                            valueOf(record, 'net_pnl', 'realized_pnl', 'pnl'),
                          )
                        }}</span
                      ></template
                    >
                    <template v-else-if="column.key === 'reason'">{{
                      labReason(valueOf(record, 'exit_reason', 'reason'))
                    }}</template>
                  </template>
                  <template #emptyText
                    ><Empty description="暂无已平仓记录；等待成熟交易结果"
                  /></template>
                </Table>
              </TabPane>

              <TabPane key="equity" tab="账户净值曲线">
                <div class="mb-2 text-xs text-muted-foreground">
                  仅绘制已落库的真实净值记录；初始资金不是历史交易收益，缺失点不补造。
                </div>
                <AccountEquityChart
                  v-if="section === 'equity' && currentDetail"
                  :key="selectedId"
                  :points="equityPoints"
                />
                <Empty
                  v-if="!equityPoints.length"
                  description="尚无净值记录，等待执行扫描"
                />
                <div
                  v-else-if="equityPoints.length === 1"
                  class="py-2 text-center text-xs text-muted-foreground"
                >
                  当前仅有一个实际净值记录，尚未形成收益曲线。
                </div>
              </TabPane>

              <TabPane key="rules" tab="冻结规则">
                <div class="rule-grid">
                  <div>
                    <div class="rule-label">机制</div>
                    <p>{{ definition.mechanism ?? '未知' }}</p>
                  </div>
                  <div>
                    <div class="rule-label">入场判据</div>
                    <p>{{ definition.entry_formula ?? '未知' }}</p>
                  </div>
                  <div>
                    <div class="rule-label">离场判据</div>
                    <p>{{ definition.exit_formula ?? '未知' }}</p>
                  </div>
                  <div>
                    <div class="rule-label">与其他策略的区别</div>
                    <p>{{ definition.distinct_from ?? '未知' }}</p>
                  </div>
                  <div>
                    <div class="rule-label">账户风险预算</div>
                    <p v-if="factorialAccount">
                      {{ accountFactors.riskBudget }} · 计划止损与手续费预算
                      {{ fixed(riskBudget, 0) }} USDT · k = {{ fixed(atrStopMult, 1) }}
                    </p>
                    <p>
                      目标投入
                      {{ percent(definition.position_budget?.target_exposure) }}
                      · 最多
                      {{ fixed(definition.position_budget?.max_positions, 0) }}
                      仓 · 每币
                      {{ fixed(definition.position_budget?.max_per_coin, 0) }}
                      仓 · 杠杆
                      {{ fixed(strategyLeverage(), 0) }} 倍
                    </p>
                  </div>
                  <div class="risk-rule-block">
                    <div class="rule-label">风险保护与止损</div>
                    <p>{{ liquidationRuleText() }}</p>
                    <p v-if="atrStopMult != null">{{ atrRiskText() }}</p>
                    <p v-else-if="definition.params_frozen?.stop_atr_mult != null">
                      s08 影线拒绝策略另有冻结 ATR 止损/止盈：止损倍数
                      {{ fixed(definition.params_frozen.stop_atr_mult, 2) }}，止盈倍数
                      {{ fixed(definition.params_frozen.tp_atr_mult, 2) }}；ATR 在入场时冻结，触发只认收盘价，下一根开盘价代理成交。
                    </p>
                    <p v-else>当前策略没有统一固定百分比止损，主要依靠策略离场判据、持仓期限与杠杆强平保护。</p>
                  </div>
                  <div>
                    <div class="rule-label">实际交易币池 / 成交模型</div>
                    <p>
                      {{ executionUniverse?.join(' · ') ?? '未知'
                      }}<br />连续下一根开盘价代理（下一根收盘时记录）
                    </p>
                    <p v-if="definition.research_universe?.length">
                      信号研究币池：{{ definition.research_universe.join(' · ') }}
                    </p>
                    <p v-if="executionBlocked">执行暂停：{{ executionBlockReason }}</p>
                  </div>
                </div>
                <div class="rule-label mt-4">冻结参数</div>
                <pre class="params-box">{{
                  definition.params_frozen
                    ? JSON.stringify(definition.params_frozen, null, 2)
                    : '未知'
                }}</pre>
                <div class="mt-4 break-all text-xs text-muted-foreground">
                  策略 ID：{{ account.strategy_id }}<br />规则哈希：{{
                    account.rule_hash ?? '未知'
                  }}<br />执行模型哈希：{{
                    account.execution_hash ?? '未知'
                  }}
                </div>
              </TabPane>

              <TabPane key="events" tab="决策记录">
                <Table
                  :columns="eventColumns"
                  :data-source="events"
                  :row-key="rowKey"
                  :pagination="{ pageSize: 10 }"
                  size="small"
                  :scroll="{ x: 680 }"
                >
                  <template #bodyCell="{ column, record }">
                    <template v-if="column.key === 'time'">{{
                      fullOrderTime(
                        valueOf(
                          record,
                          'observed_at_ms',
                          'timestamp',
                          'created_at',
                        ),
                      )
                    }}</template>
                    <template v-else-if="column.key === 'kind'">{{
                      labEventType(valueOf(record, 'event_type', 'kind', 'type'))
                    }}</template>
                    <template v-else-if="column.key === 'reason'">{{
                      eventSummary(record)
                    }}</template>
                  </template>
                  <template #emptyText
                    ><Empty description="暂无当前策略的决策事件"
                  /></template>
                </Table>
              </TabPane>
            </Tabs>
          </template>
        </Spin>
      </Card>
    </Spin>
    <Drawer v-model:open="recordDetailOpen" title="模拟记录详情" :width="560">
      <Descriptions v-if="recordDetail" bordered :column="1" size="small">
        <DescriptionsItem label="展示编号">{{ paperNumber(recordDetail) }}</DescriptionsItem>
        <DescriptionsItem label="策略">{{ accountName(accounts.find(item => item.strategy_id === recordDetail?.strategy_id)) }}</DescriptionsItem>
        <DescriptionsItem label="币种">{{ textOf(recordDetail, 'pair') }}</DescriptionsItem>
        <DescriptionsItem label="状态">{{ recordStatus(recordDetail) }}</DescriptionsItem>
        <DescriptionsItem label="原因">{{ labReason(valueOf(recordDetail, 'reason', 'signal_reason', 'exit_reason')) }}</DescriptionsItem>
        <DescriptionsItem label="完整模拟订单号"><span class="break-all">{{ textOf(recordDetail, 'order_id', 'paper_order_id', 'entry_order_id') }}</span></DescriptionsItem>
        <DescriptionsItem v-if="recordDetail.trade_id" label="完整成交记录号"><span class="break-all">{{ recordDetail.trade_id }}</span></DescriptionsItem>
        <DescriptionsItem v-if="recordDetail.group_id" label="完整配对组编号"><span class="break-all">{{ recordDetail.group_id }}</span></DescriptionsItem>
        <DescriptionsItem label="交易所原始订单号">{{ recordDetail.exchange_order_id ?? '无（模拟订单）' }}</DescriptionsItem>
        <DescriptionsItem label="原始原因代码"><span class="break-all">{{ textOf(recordDetail, 'reason', 'signal_reason', 'exit_reason') }}</span></DescriptionsItem>
        <DescriptionsItem label="成交价格">{{ fillValue(recordDetail, valueOf(recordDetail, 'fill_price', 'price', 'entry_price')) }}</DescriptionsItem>
        <DescriptionsItem label="手续费 USDT">{{ fillValue(recordDetail, valueOf(recordDetail, 'fee', 'fee_paid', 'fees')) }}</DescriptionsItem>
      </Descriptions>
    </Drawer>
    <div class="mt-3 text-xs text-muted-foreground">
      前向模拟结果仅扣除已记录手续费；滑点、资金费与真实订单成交差异尚未建模。
      <span v-if="overview?.limitations?.length">{{
        overview.limitations.join('；').replace(/\bunknown\b/gi, '未知')
      }}</span>
    </div>
  </div>
</template>

<style scoped>
.lab-filter {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.population-select {
  min-width: 215px;
}
.risk-select {
  min-width: 180px;
}
.stop-select {
  min-width: 135px;
}
.metric-cell {
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
  padding: 12px;
  min-width: 0;
}
.metric-label {
  font-size: 12px;
  color: hsl(var(--muted-foreground));
}
.metric-value {
  margin-top: 8px;
  font-size: 20px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.metric-sub {
  margin-top: 6px;
  font-size: 11px;
  color: hsl(var(--muted-foreground));
}
.metric-time {
  font-size: 14px;
}
.strategy-tab {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 3px 0;
}
.strategy-tab-sub {
  font-size: 10px;
  color: hsl(var(--muted-foreground));
}
.row-sub {
  margin-top: 4px;
  font-size: 11px;
  color: hsl(var(--muted-foreground));
}
.profit-up {
  color: #c2413a;
}
.profit-down {
  color: #157f78;
}
.risk-critical {
  color: #b42318;
  font-weight: 600;
}
.risk-high {
  color: #c2410c;
  font-weight: 600;
}
.risk-watch {
  color: #a16207;
}
.risk-rule-block {
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
  padding: 12px;
}
.rule-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 20px;
}
.rule-grid p {
  margin-top: 8px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.rule-label {
  font-size: 12px;
  font-weight: 600;
  color: hsl(var(--muted-foreground));
}
.params-box {
  margin-top: 8px;
  border-radius: 8px;
  background: hsl(var(--muted));
  padding: 12px;
  font-size: 12px;
  overflow-x: auto;
}
@media (max-width: 767px) {
  .rule-grid {
    grid-template-columns: 1fr;
  }
  .metric-value {
    font-size: 18px;
  }
}
</style>

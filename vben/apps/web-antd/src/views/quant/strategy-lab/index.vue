<script lang="ts" setup>
import type {
  StrategyLabBlockedStrategy,
  StrategyLabAccount,
  StrategyLabDetail,
  StrategyLabOverview,
  StrategyLabRejectedStrategy,
  StrategyLabRow,
} from '#/api/strategy-lab';
import type {
  LabBookGroupFilter,
  LabPopulation,
  LabRiskFilter,
  LabStopFilter,
} from './accountFilters';

import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';

import {
  Alert,
  Button,
  Descriptions,
  DescriptionsItem,
  Drawer,
  Empty,
  Spin,
  Table,
  TabPane,
  Tabs,
  Tag,
} from 'ant-design-vue';

import { ftClient, unwrap } from '#/api/freqtrade/client';
import { requireSession } from '#/api/session';
import { parsePaperDetail, parsePaperOverview } from './paperEvidence';
import { fullOrderTime } from '../utils/orderDisplayId';
import AccountEquityChart from './AccountEquityChart.vue';
import {
  canonicalLabStrategy,
  filterLabAccounts,
  labAccountFactors,
  labAccountPopulation,
  labBookGroup,
  labPopulationHasRiskFactors,
  labWarmupCandleTime,
  selectLabAccount,
  selectedLabDetail,
  selectedLabExecution,
} from './accountFilters';
import {
  labEventType,
  labFamily,
  labOrderStatus,
  labReason,
  labResearchReason,
} from './displayLabels';
import { labBookDecisionEvidence, labRecordPayload } from './decisionEvidence';

const overview = ref<StrategyLabOverview | null>(null);
const detail = ref<StrategyLabDetail | null>(null);
const selectedId = ref('');
const inspectorSection = ref('definition');
const inspectorTabs = [
  { key: 'definition', label: '定义' },
  { key: 'time', label: '时间评估' },
  { key: 'cost', label: '成本' },
  { key: 'execution', label: '执行' },
  { key: 'archive', label: '档案' },
];
const population = ref<LabPopulation>('factorial');
const riskFilter = ref<LabRiskFilter>('all');
const stopFilter = ref<LabStopFilter>('all');
const bookGroupFilter = ref<LabBookGroupFilter>('all');
const route = useRoute();
const section = ref('positions');
const loading = ref(false);
const detailLoading = ref(false);
const error = ref('');
const detailError = ref('');
const recordDetail = ref<Record<string, unknown> | null>(null);
const recordDetailOpen = ref(false);
const recordBookEvidence = computed(() =>
  recordDetail.value ? labBookDecisionEvidence(recordDetail.value) : [],
);
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
const accounts = computed(() =>
  filterLabAccounts(
    scopedAccounts.value,
    population.value,
    riskFilter.value,
    stopFilter.value,
    bookGroupFilter.value,
  ),
);
const strategyGroups = computed(() => {
  const groups = new Map<
    string,
    { id: string; code: string; label: string; items: StrategyLabAccount[] }
  >();
  for (const item of accounts.value) {
    const id = canonicalLabStrategy(item);
    let group = groups.get(id);
    if (!group) {
      group = {
        id,
        code: item.strategy_id.split('_')[0]?.toUpperCase() ?? '',
        label:
          labAccountPopulation(item) === 'microstructure'
            ? '波动压缩 · 订单簿对照实验'
            : (accountName(item).split(' · ')[0] ?? accountName(item)),
        items: [],
      };
      groups.set(id, group);
    }
    group.items.push(item);
  }
  return [...groups.values()];
});

function accountVariantName(item: StrategyLabAccount) {
  const variant =
    accountName(item).split(' · ').slice(1).join(' · ') ||
    factorLabel(item) ||
    armLabel(item);
  return labAccountPopulation(item) === 'microstructure'
    ? `${labBookGroup(item) === 'filter' ? '订单簿确认过滤' : '无订单簿过滤对照'} · ${variant}`
    : variant;
}
const populationLabels: Record<LabPopulation, string> = {
  factorial: 'ATR 因子账户',
  fnew: 'Fnew 新方向候选',
  microstructure: '订单簿对照实验',
  controls: '旧规则对照账户',
  archived: '已归档账户',
};
const populationOptions = computed(() =>
  (
    ['factorial', 'fnew', 'microstructure', 'controls', 'archived'] as const
  ).map((value) => ({
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
const bookGroupOptions = [
  { value: 'all', label: '对照与过滤并列' },
  { value: 'control', label: '无订单簿过滤对照' },
  { value: 'filter', label: '订单簿确认过滤' },
];
const riskFactorsVisible = computed(() =>
  labPopulationHasRiskFactors(population.value),
);
const filterScope = computed(() => {
  const count = scopedAccounts.value.filter(
    (item) => labAccountPopulation(item) === population.value,
  ).length;
  const factors = riskFactorsVisible.value
    ? [
        riskFilter.value === 'all' ? '全部 R 档' : riskFilter.value,
        stopFilter.value === 'all' ? '全部 k 档' : `k${stopFilter.value}`,
      ].join(' · ')
    : '保留原冻结规则与样本';
  const bookGroup =
    population.value === 'microstructure'
      ? ` · ${bookGroupOptions.find((item) => item.value === bookGroupFilter.value)?.label}`
      : '';
  return `${focusOnly.value ? '重点策略范围 · ' : ''}${populationLabels[population.value]} · ${factors}${bookGroup} · 当前显示 ${accounts.value.length} / ${count} 个账户`;
});
const blockedStrategies = computed<StrategyLabBlockedStrategy[]>(() =>
  focusOnly.value ? [] : (overview.value?.blocked_strategies ?? []),
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
  focusOnly.value ? [] : (overview.value?.rejected_strategies ?? []),
);
const rejectedStrategySummary = computed(() =>
  rejectedStrategies.value
    .map(
      (item) =>
        `${item.label ?? item.strategy_id}：${labResearchReason(item.reason)}`,
    )
    .join('；'),
);
const currentDetail = computed(() =>
  selectedLabDetail(detail.value, selectedId.value),
);
const account = computed(
  () =>
    currentDetail.value?.account ??
    accounts.value.find((item) => item.strategy_id === selectedId.value),
);
const definition = computed(() => account.value?.definition ?? {});
const execution = computed(() =>
  selectedLabExecution(
    account.value,
    overview.value?.execution ?? overview.value?.execution_model ?? {},
  ),
);
const orders = computed(() => currentDetail.value?.orders ?? []);
const trades = computed(() => currentDetail.value?.trades ?? []);
const events = computed(() => currentDetail.value?.events ?? []);
const positions = computed(() => account.value?.open_positions ?? []);
const equityPoints = computed(() => currentDetail.value?.equity_curve ?? []);
const atrRiskAccount = computed(
  () =>
    account.value &&
    labPopulationHasRiskFactors(labAccountPopulation(account.value)),
);
const selectedPopulation = computed(() =>
  account.value ? labAccountPopulation(account.value) : population.value,
);
const accountFamilyLabel = computed(() =>
  selectedPopulation.value === 'microstructure'
    ? '订单簿 · 波动压缩入场确认'
    : labFamily(account.value?.family ?? definition.value.family),
);
const mechanismText = computed(() =>
  selectedPopulation.value === 'microstructure'
    ? '共同基线为 1h 波动压缩突破；对照保留原 K 线信号，过滤组使用有效订单簿特征确认新入场，两组使用相同的策略离场规则。'
    : (definition.value.mechanism ?? '未知'),
);
const accountFactors = computed(() => labAccountFactors(account.value));
const atrStopMult = computed(() =>
  numeric(
    execution.value.risk_stop_atr_mult ??
      definition.value.params_frozen?.risk_stop_atr_mult ??
      definition.value.params?.risk_stop_atr_mult,
  ),
);
const riskBudget = computed(
  () =>
    execution.value.max_loss_per_trade_usdt ??
    definition.value.max_loss_per_trade_usdt,
);
const executionBlockReason = computed(() => {
  const blockers = definition.value.execution_blockers;
  return blockers?.length
    ? blockers.map(labResearchReason).join('；')
    : labResearchReason(
        execution.value.blocked_execution_reason ??
          definition.value.blocked_execution_reason,
      );
});
const executionBlocked = computed(
  () =>
    execution.value.execution_enabled === false ||
    definition.value.execution_enabled === false,
);
const executionUniverse = computed(
  () =>
    execution.value.execution_universe ??
    definition.value.execution_universe ??
    definition.value.universe,
);
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
  return (direction * (mark - liquidation)) / mark;
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
    numeric(params.stop_atr_mult) !== null &&
    numeric(params.tp_atr_mult) !== null;
  const hasRiskStop = atrStopMult.value !== null;
  if (liquidation !== null && leverage !== null && leverage > 1) {
    return `${fixed(leverage, 0)} 倍强平 · ${hasRiskStop ? 'ATR 风险止损' : hasAtrProtection ? 'ATR 止损/止盈' : '策略离场'}`;
  }
  if (hasRiskStop) return 'ATR 风险止损';
  if (hasAtrProtection) return 'ATR 止损/止盈';
  return leverage === 1 ? '未启用统一强平' : '风险保护未知';
}

function stopRuleText() {
  const mode = execution.value.stop_execution_mode;
  const policy =
    typeof execution.value.stop_policy === 'string'
      ? execution.value.stop_policy
      : '';
  if (mode === 'intrabar' || policy.includes('intrabar'))
    return '按盘中高低价触发，止损价或跳空开盘价代理成交；同根 OHLC 路径未知，止损优先。';
  if (
    mode === 'close_confirmed' ||
    policy.includes('close_confirmed') ||
    policy.includes('next_open') ||
    execution.value.atomic_atr_stop_policy ===
      'paired_close_confirmed_aggregate_return'
  )
    return '止损由收盘价确认，按下一根开盘价代理成交。';
  return '止损触发方式未在所选冻结执行字段中明确提供，请核对原规则。';
}
function fillModelText() {
  return typeof execution.value.fill_model === 'string'
    ? execution.value.fill_model
    : '成交模型未提供，无法确认成交代理';
}
function liquidationRuleText() {
  const leverage = strategyLeverage();
  const nested = execution.value.liquidation;
  const liquidation =
    nested && typeof nested === 'object' && !Array.isArray(nested)
      ? (nested as Record<string, unknown>)
      : {};
  const model = execution.value.liquidation_model ?? liquidation.model;
  if (model === 'binance_tiered')
    return (
      '分层维持保证金模型：按所选杠杆档位与维持保证金条件，用 K 线高低价检查穿越，按穿越价格代理结算，未覆盖额外不利 tick 滑点。档位快照：' +
      (execution.value.tier_snapshot_hash ?? '未提供，档位身份未知') +
      '。'
    );
  if (model === 'bankruptcy_price')
    return '冻结破产价模型：按入场价与所选杠杆的破产边界检查触发，保证金按规则全额损失；此为纸面代理。';
  if (leverage === 1) return '所选账户杠杆为 1；统一强平模型未明确提供。';
  return '冻结强平模型未明确提供，请核对所选账户原执行规则；无法从杠杆单独确认结算方式。';
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
    valueOf(
      row,
      'signal_candle_ms',
      'opened_candle_ms',
      'entry_timestamp',
      'created_at',
      'timestamp',
      'candle_ms',
      'observed_at_ms',
    ),
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
  return row.status === 'pending'
    ? '尚未成交'
    : row.status === 'cancelled' || row.status === 'rejected'
      ? '未成交'
      : '未知';
}

function recordStatus(row: Record<string, unknown>) {
  if (row.event_type) return labEventType(row.event_type);
  return row.trade_id && row.closed_candle_ms != null
    ? '已平仓'
    : labOrderStatus(row.status);
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
    cross_sectional_execution_pending: {
      color: 'orange',
      label: '等待多腿执行能力',
    },
    awaiting_native_timeframe_adapter: {
      color: 'orange',
      label: '等待周期数据适配',
    },
    funding_data_unavailable: { color: 'orange', label: '资金费率数据缺失' },
    stopped_or_stale: { color: 'orange', label: '执行器已停或状态过期' },
    not_started: { color: 'default', label: '执行器尚未启动' },
    startup_failed: { color: 'red', label: '执行器启动失败' },
    forward_research_hypothesis: { color: 'blue', label: '新方向前向假设' },
    paired_execution_pending: { color: 'orange', label: '配对执行待接入' },
    forward_data_required: { color: 'orange', label: '等待前向输入数据' },
    orderbook_feature_pending: { color: 'orange', label: '等待订单簿特征' },
    waiting_published_orderbook_feature: {
      color: 'orange',
      label: '等待订单簿特征发布',
    },
    unknown: { color: 'default', label: '状态未知' },
  };
  return Object.hasOwn(labels, key)
    ? labels[key]!
    : { color: 'default', label: key };
}

function accountStatus(item: StrategyLabAccount) {
  if (labAccountPopulation(item) === 'archived') return statusOf('archived');
  if (
    item.execution?.execution_enabled === false ||
    item.definition?.execution_enabled === false
  )
    return statusOf('execution_blocked');
  return statusOf(item.status);
}

function factorLabel(item: StrategyLabAccount) {
  const factors = labAccountFactors(item);
  return factors.riskBudget
    ? `${factors.riskBudget} · k${fixed(factors.stopWidth, 1)}${labAccountPopulation(item) === 'microstructure' ? ` · ${labBookGroup(item) === 'filter' ? '过滤组' : '对照组'}` : ''}`
    : '';
}

function atrRiskText() {
  const scope =
    (execution.value.risk_budget_scope ??
      definition.value.risk_budget_scope) === 'atomic_pair_portfolio'
      ? '整组配对双腿'
      : '入场冻结 ATR 距离反解仓位';
  return (
    'R = ' +
    fixed(riskBudget.value, 0) +
    ' USDT 是' +
    scope +
    '的计划止损与手续费预算；k = ' +
    fixed(atrStopMult.value, 1) +
    '。' +
    stopRuleText() +
    '跳空、滑点与强平模型可能使实际亏损超过 R。'
  );
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
  return (
    item?.label ??
    item?.name ??
    item?.definition?.name ??
    fallback ??
    item?.strategy_id ??
    '未知'
  );
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
    Fnew_20261012: 'Fnew · 新方向候选',
    F_microstructure_forward: 'S37 · 订单簿对照',
  };
  return labels[value] ?? (value || '实验臂未知');
}

function dataStatusLabel(item?: StrategyLabAccount) {
  const value = String(
    item?.data_status ?? item?.definition?.data_status ?? '',
  ).trim();
  const labels: Record<string, string> = {
    native_timeframe_adapter_active: '原生周期数据已接入',
    cross_sectional_execution_pending: '横截面执行待接入',
    funding_data_pending: '资金费数据待接入',
    funding_series_loaded_rejected: '资金费数据已入库 · 研究已淘汰',
    research_context_paused: '所需多币研究上下文已暂停',
    atr_risk_execution_pending: 'ATR 风险执行规则待接入',
    forward_data_required: '依赖新方向前向数据',
    paired_execution_pending: '配对执行待接入',
    orderbook_feature_pending: '等待订单簿特征',
  };
  return labels[value] ?? (value ? labReason(value) : '数据状态未知');
}

function hasDataStatus(item?: StrategyLabAccount) {
  return Boolean(
    String(item?.data_status ?? item?.definition?.data_status ?? '').trim(),
  );
}

type ObservationBadge = {
  color: string;
  label: string;
  note: string;
};

function observationBadge(strategyOrAccount: unknown): ObservationBadge | null {
  const account =
    strategyOrAccount && typeof strategyOrAccount === 'object'
      ? (strategyOrAccount as StrategyLabAccount)
      : undefined;
  const strategyId = account?.strategy_id ?? strategyOrAccount;
  const baseId = account
    ? canonicalLabStrategy(account)
    : canonicalLabStrategy(typeof strategyId === 'string' ? strategyId : '');
  const searchText =
    `${String(strategyId ?? '')} ${String(account?.label ?? '')} ${String(account?.name ?? '')}`.toLowerCase();
  if (
    baseId === 's11_tsmom_bollinger_pullback' ||
    /s03.*s05|s05.*s03|tsmom.*bollinger|bollinger.*tsmom|combo|hybrid/.test(
      searchText,
    )
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
  if (
    status === 'funding_data_unavailable' ||
    status === 'funding_data_pending'
  ) {
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
  if (status === 'paired_execution_pending') {
    return '新候选需要原子配对执行与组合风险保护；执行规则验收前保留研究账户，暂停生成模拟订单。';
  }
  return '';
}

const accountEmptyDescription = computed(
  () =>
    `当前接口返回的筛选中没有账户（${populationLabels[population.value]}）；可调整筛选。未枚举账户不代表历史数据库没有档案。`,
);

function eventSummary(row: Record<string, unknown>) {
  const payload = labRecordPayload(row);
  const reason = valueOf(payload, 'reason', 'message');
  if (reason !== null) return labReason(reason);
  if (Array.isArray(payload.signals)) {
    return (
      payload.signals
        .map((value) => {
          const signal = value as Record<string, unknown>;
          const target =
            signal.target_side == null
              ? '维持现状'
              : signal.target_side === 0
                ? '空仓'
                : sideText(signal.target_side);
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
    requireSession('请先登录后查看纸面策略档案');
    const response = parsePaperDetail(
      unwrap<unknown>(
        await ftClient.get('/locals/strategy-lab', {
          params: { strategy_id: strategyId, limit: 100 },
          signal: controller.signal,
        }),
      ),
      strategyId,
    );
    if (current !== detailSequence || strategyId !== selectedId.value) return;
    detail.value = response;
  } catch {
    if (
      current === detailSequence &&
      strategyId === selectedId.value &&
      !controller.signal.aborted
    ) {
      detailError.value = currentDetail.value
        ? '明细读取失败，保留上次成功事实'
        : '明细读取失败，尚无可用事实';
    }
  } finally {
    if (current === detailSequence && strategyId === selectedId.value)
      detailLoading.value = false;
  }
}

async function refresh() {
  overviewController?.abort();
  overviewController = new AbortController();
  const current = ++overviewSequence;
  loading.value = true;
  error.value = '';
  try {
    requireSession('请先登录后查看纸面策略档案');
    const response = parsePaperOverview(
      unwrap<unknown>(
        await ftClient.get('/locals/strategy-lab', {
          signal: overviewController.signal,
        }),
      ),
    );
    if (current !== overviewSequence) return;
    const previousId = selectedId.value;
    overview.value = response;
    refreshedAt.value = Date.now();
    if (previousId && selectedId.value === previousId) await refreshDetail();
  } catch {
    if (current === overviewSequence && !overviewController.signal.aborted)
      error.value = overview.value
        ? '概览读取失败，保留上次成功事实'
        : '概览读取失败，尚无可用事实';
  } finally {
    if (current === overviewSequence) loading.value = false;
  }
}

watch(
  accounts,
  (items) => {
    selectedId.value = selectLabAccount(
      items,
      selectedId.value,
      focusOnly.value,
    );
  },
  { flush: 'sync' },
);
watch(
  selectedId,
  () => {
    recordDetailOpen.value = false;
    recordDetail.value = null;
    detail.value = null;
    void refreshDetail();
  },
  { flush: 'sync' },
);
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
  <main class="strategy-lab paper-workspace" aria-labelledby="paper-title">
    <header class="paper-heading">
      <div>
        <h1 id="paper-title">
          {{ focusOnly ? '重点策略纸面档案' : '纸面策略档案' }}
        </h1>
        <p>独立账户与实验臂，按冻结规则追溯定义、成本和执行记录。</p>
      </div>
      <Button
        data-refresh-paper
        :loading="loading || detailLoading"
        @click="refresh"
        >刷新账户</Button
      >
    </header>
    <div class="paper-scope">
      <strong>历史前向纸面</strong><span>账户资格未核验</span
      ><span>滑点与资金费未知</span
      ><Tag>{{
        overview?.service?.running === true
          ? '纸面执行器运行中'
          : overview?.service?.running === false
            ? '纸面执行器未运行'
            : '执行器状态未知'
      }}</Tag
      ><span v-if="overview?.service?.status">{{
        statusOf(overview.service.status).label
      }}</span>
    </div>
    <section class="paper-policy" aria-label="账户筛选与观测摘要">
      <div class="paper-filters">
        <label class="lab-filter"
          ><span>账户分组</span
          ><select
            v-model="population"
            class="population-select"
            aria-label="账户分组"
          >
            <option
              v-for="option in populationOptions"
              :key="option.value"
              :value="option.value"
            >
              {{ option.label }}
            </option>
          </select></label
        ><label class="lab-filter"
          ><span>计划风险预算</span
          ><select
            v-model="riskFilter"
            :disabled="!riskFactorsVisible"
            class="risk-select"
            aria-label="计划风险预算"
          >
            <option
              v-for="option in riskOptions"
              :key="option.value"
              :value="option.value"
            >
              {{ option.label }}
            </option>
          </select></label
        ><label class="lab-filter"
          ><span>止损宽度</span
          ><select
            v-model="stopFilter"
            :disabled="!riskFactorsVisible"
            class="stop-select"
            aria-label="止损宽度"
          >
            <option
              v-for="option in stopOptions"
              :key="option.value"
              :value="option.value"
            >
              {{ option.label }}
            </option>
          </select></label
        ><label v-if="population === 'microstructure'" class="lab-filter"
          ><span>对照方式</span
          ><select
            v-model="bookGroupFilter"
            class="risk-select"
            aria-label="订单簿对照方式"
          >
            <option
              v-for="option in bookGroupOptions"
              :key="option.value"
              :value="option.value"
            >
              {{ option.label }}
            </option>
          </select></label
        >
      </div>
      <p class="paper-note">{{ filterScope }}。不同账户的资金与样本独立。</p>
      <dl class="policy-totals">
        <div>
          <dt>筛选账户</dt>
          <dd>{{ overview ? accounts.length : '未知' }}</dd>
        </div>
        <div>
          <dt>初始纸面资金合计</dt>
          <dd>{{ fixed(total('initial_capital')) }} USDT</dd>
        </div>
        <div>
          <dt>纸面净值合计</dt>
          <dd>{{ fixed(total('equity')) }} USDT</dd>
        </div>
        <div>
          <dt>最近扫描（北京）</dt>
          <dd>{{ fullOrderTime(overview?.service?.last_cycle_at) }}</dd>
        </div>
        <div>
          <dt>页面读取时间（北京）</dt>
          <dd>{{ fullOrderTime(refreshedAt) }}</dd>
        </div>
      </dl>
    </section>
    <p v-if="error" class="paper-error" role="status">{{ error }}</p>
    <p v-if="overview?.service?.last_error" class="paper-error">
      模拟执行器报告错误；请在受控日志中核对。
    </p>
    <details
      v-if="blockedStrategies.length || rejectedStrategies.length"
      class="candidate-boundaries"
    >
      <summary>待接入 / 淘汰研究候选</summary>
      <p v-if="blockedStrategies.length">
        未进入纸面：{{ blockedStrategySummary }}
      </p>
      <p v-if="rejectedStrategies.length">
        已淘汰：{{ rejectedStrategySummary }}
      </p>
    </details>
    <Spin :spinning="loading && !overview"
      ><div class="paper-layout">
        <section
          class="account-ledger"
          data-account-ledger
          aria-label="纸面候选账户台账"
        >
          <div class="ledger-heading">
            <h2>候选账户台账</h2>
            <span
              >{{ strategyGroups.length }} 项 /
              {{ accounts.length }} 个账户</span
            >
          </div>
          <div
            class="ledger-scroll"
            role="region"
            aria-label="纸面账户表，可横向滚动"
            tabindex="0"
          >
            <table>
              <thead>
                <tr>
                  <th>账户 / 实验臂</th>
                  <th>周期 / 风险</th>
                  <th>纸面状态</th>
                  <th>费用后收益</th>
                </tr>
              </thead>
              <tbody>
                <template v-for="group in strategyGroups" :key="group.id"
                  ><tr class="ledger-group">
                    <th colspan="4">{{ group.code }} {{ group.label }}</th>
                  </tr>
                  <tr
                    v-for="item in group.items"
                    :key="item.strategy_id"
                    :class="{ 'selected-row': selectedId === item.strategy_id }"
                  >
                    <th scope="row">
                      <button
                        type="button"
                        :data-account-id="item.strategy_id"
                        :aria-pressed="selectedId === item.strategy_id"
                        @click="selectedId = item.strategy_id"
                      >
                        <strong>{{ accountName(item) }}</strong
                        ><small
                          >{{ accountVariantName(item) }} ·
                          {{ armLabel(item) }}</small
                        ><code>{{ item.strategy_id }}</code>
                      </button>
                    </th>
                    <td>
                      {{ item.timeframe ?? item.definition?.timeframe ?? '未知'
                      }}<small>{{ factorLabel(item) || '原冻结规则' }}</small>
                    </td>
                    <td>
                      {{ accountStatus(item).label
                      }}<small>{{
                        isFocusStrategy(item)
                          ? observationLabel(item)
                          : populationLabels[labAccountPopulation(item)]
                      }}</small>
                    </td>
                    <td :class="pnlClass(item.realized_pnl)">
                      {{ fixed(item.realized_pnl) }}<small>其它成本未齐</small>
                    </td>
                  </tr></template
                >
              </tbody>
            </table>
          </div>
          <p v-if="overview && !accounts.length" class="paper-empty">
            {{ accountEmptyDescription }}
          </p>
          <p v-else-if="!overview && !loading" class="paper-empty">
            尚无可用账户事实，刷新可重试。
          </p>
        </section>
        <section class="strategy-detail" aria-label="所选纸面账户详情">
          <p v-if="detailError" class="paper-error" role="status">
            {{ detailError }}
          </p>
          <Spin :spinning="detailLoading"
            ><Empty
              v-if="!account && !detailLoading"
              description="选择账户查看独立档案" /><template
              v-else-if="account"
            >
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
                <Tag v-if="atrRiskAccount" color="purple"
                  >{{ accountFactors.riskBudget }} · k{{
                    fixed(accountFactors.stopWidth, 1)
                  }}</Tag
                >
                <Tag v-else>{{
                  populationLabels[labAccountPopulation(account)]
                }}</Tag>
                <Tag
                  v-if="
                    selectedPopulation === 'fnew' ||
                    selectedPopulation === 'microstructure'
                  "
                  color="geekblue"
                  >{{ populationLabels[selectedPopulation] }}</Tag
                >
                <Tag
                  v-if="selectedPopulation === 'microstructure'"
                  color="cyan"
                  >{{
                    labBookGroup(account) === 'filter'
                      ? '订单簿确认过滤'
                      : '无订单簿过滤对照'
                  }}</Tag
                >
                <Tag v-if="armId(account)" color="geekblue">{{
                  armLabel(account)
                }}</Tag>
                <Tag v-if="hasDataStatus(account)" color="orange">{{
                  dataStatusLabel(account)
                }}</Tag>
                <span class="text-xs text-muted-foreground"
                  >{{ accountFamilyLabel }} ·
                  {{ account.timeframe ?? definition.timeframe ?? '未知' }} ·
                  {{ armLabel(account) }}</span
                >
              </div>
              <div class="selected-identity" data-selected-paper>
                <span>纸面账户</span><code>{{ selectedId }}</code
                ><span>账户资格未核验</span>
              </div>
              <nav class="inspector-tabs" aria-label="所选账户证据分区">
                <button
                  v-for="tab in inspectorTabs"
                  :key="tab.key"
                  type="button"
                  :data-inspector-tab="tab.key"
                  :aria-pressed="inspectorSection === tab.key"
                  @click="inspectorSection = tab.key"
                >
                  {{ tab.label }}
                </button>
              </nav>
              <section
                v-show="inspectorSection === 'definition'"
                class="inspector-section"
                aria-label="冻结定义"
              >
                <div class="rule-grid">
                  <div>
                    <div class="rule-label">机制</div>
                    <p>{{ mechanismText }}</p>
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
                    <p v-if="atrRiskAccount">
                      {{ accountFactors.riskBudget }} · 计划止损与手续费预算
                      {{ fixed(riskBudget, 0) }} USDT · k =
                      {{ fixed(atrStopMult, 1) }}
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
                    <p
                      v-else-if="
                        definition.params_frozen?.stop_atr_mult != null
                      "
                    >
                      s08 影线拒绝策略另有冻结 ATR 止损/止盈：止损倍数
                      {{
                        fixed(definition.params_frozen.stop_atr_mult, 2)
                      }}，止盈倍数
                      {{ fixed(definition.params_frozen.tp_atr_mult, 2) }}；ATR
                      在入场时冻结，{{ stopRuleText() }}
                    </p>
                    <p v-else>
                      当前策略没有统一固定百分比止损，主要依靠策略离场判据、持仓期限与杠杆强平保护。
                    </p>
                  </div>
                  <div>
                    <div class="rule-label">实际交易币池 / 成交模型</div>
                    <p>
                      {{ executionUniverse?.join(' · ') ?? '未知' }}<br />{{
                        fillModelText()
                      }}
                    </p>
                    <p v-if="definition.research_universe?.length">
                      信号研究币池：{{
                        definition.research_universe.join(' · ')
                      }}
                    </p>
                    <p v-if="executionBlocked">
                      执行暂停：{{ executionBlockReason }}
                    </p>
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
                  }}<br />执行模型哈希：{{ account.execution_hash ?? '未知' }}
                </div>
              </section>
              <section
                v-show="inspectorSection === 'time'"
                class="inspector-section"
                aria-label="时间评估"
              >
                <div
                  class="mb-4 rounded border p-3 text-xs text-muted-foreground"
                >
                  {{ warmupText(account) }} · 最新收盘 K 线起点（北京）
                  {{ fullOrderTime(labWarmupCandleTime(account)) }}
                  · 最近执行 K 线起点
                  {{
                    account.last_candle_ms == null
                      ? '尚无执行记录'
                      : fullOrderTime(account.last_candle_ms)
                  }}
                  <span
                    v-if="
                      account.warmup?.reason &&
                      account.warmup.reason !== 'ready'
                    "
                  >
                    · {{ labReason(account.warmup.reason) }}</span
                  >
                  <span v-if="hasDataStatus(account)">
                    · {{ dataStatusLabel(account) }}</span
                  >
                </div>
                <div class="mb-2 text-xs text-muted-foreground">
                  仅绘制已落库的真实净值记录；初始资金不是历史交易收益，缺失点不补造。
                </div>
                <AccountEquityChart
                  v-if="currentDetail && inspectorSection === 'time'"
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
                <p class="paper-note">
                  当前归档不提供新计划要求的逐阶段时间验收与封闭覆盖证明，时间资格仍未知。
                </p>
              </section>
              <section
                v-show="inspectorSection === 'cost'"
                class="inspector-section"
                aria-label="成本证据"
              >
                <h3>费用口径与完整度</h3>
                <p class="cost-boundary">
                  滑点与资金费未知，扣手续费收益不能视为完整净收益。
                </p>
                <dl class="paper-facts">
                  <div>
                    <dt>手续费 / 边</dt>
                    <dd>{{ percent(feeRate) }}</dd>
                  </div>
                  <div>
                    <dt>已支付手续费 USDT</dt>
                    <dd>{{ fixed(feesPaid, 4) }}</dd>
                  </div>
                  <div>
                    <dt>成交代理</dt>
                    <dd>
                      {{
                        execution.fill_model ??
                        '下一根开盘价代理，实际委托成交差异未建模'
                      }}
                    </dd>
                  </div>
                  <div>
                    <dt>资金费</dt>
                    <dd>
                      {{ execution.funding ?? account.funding ?? '未知' }}
                    </dd>
                  </div>
                  <div>
                    <dt>滑点</dt>
                    <dd>
                      {{ execution.slippage ?? account.slippage ?? '未知' }}
                    </dd>
                  </div>
                  <div>
                    <dt>限价单未成交风险</dt>
                    <dd>{{ execution.non_fill_risk ?? '未知' }}</dd>
                  </div>
                  <div>
                    <dt>冻结费用依据</dt>
                    <dd>{{ execution.fee_basis ?? '来源未提供' }}</dd>
                  </div>
                </dl>
                <p class="paper-note">
                  费用后收益仅对应所选纸面账户，未与 Binance Demo
                  或真实账户合并。
                </p>
              </section>
              <section
                v-show="inspectorSection === 'execution'"
                class="inspector-section"
                aria-label="纸面执行观测"
              >
                <Alert
                  v-if="executionBlocked"
                  class="mb-4"
                  type="warning"
                  show-icon
                  message="执行暂停，保留研究账户与全部数据"
                  :description="executionBlockReason"
                />
                <Alert
                  v-if="atrRiskAccount"
                  class="mb-4"
                  type="info"
                  show-icon
                  :message="`本账户 ATR 风险档 ${accountFactors.riskBudget} · k${fixed(atrStopMult, 1)}`"
                  :description="atrRiskText()"
                />
                <Alert
                  v-if="
                    selectedPopulation === 'fnew' &&
                    canonicalLabStrategy(account) === 's33_basis_momentum_1h'
                  "
                  class="mb-4"
                  type="info"
                  show-icon
                  message="基差信号 · 单腿永续"
                  description="现货价格仅用于计算信号，账户只交易永续且没有现货对冲；收益仍包含永续的方向风险，不能视为纯基差收益。手续费按本账户实际 taker 口径记录。"
                />
                <Alert
                  v-if="selectedPopulation === 'microstructure'"
                  class="mb-4"
                  type="info"
                  show-icon
                  message="订单簿只用于入场确认"
                  description="无仓时两组共同等待本根特征发布，最长 90 秒；超时后，对照按原 K 线信号运行，过滤组要求有效特征并通过确认。已有持仓退出不等待订单簿。只有双方具备有效特征的配对样本用于评估过滤贡献。"
                />
                <Alert
                  v-if="!atrRiskAccount"
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
                <div
                  class="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7"
                >
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
                  <span
                    >已平仓胜率
                    {{
                      numeric(closedCount) === 0
                        ? '暂无平仓样本'
                        : percent(account.win_rate)
                    }}</span
                  >
                  <span
                    >观测最大回撤
                    {{
                      percent(
                        account.max_drawdown_ratio ?? account.max_drawdown_pct,
                      )
                    }}</span
                  >
                  <span
                    >待成交订单
                    {{ fixed(account.pending_order_count, 0) }}</span
                  >
                </div>
                <Alert
                  v-if="numeric(closedCount) === 0"
                  class="mt-3"
                  type="info"
                  show-icon
                  message="已平仓 0 笔，尚无已实现策略表现样本。持仓浮盈与已平仓结果分开统计。"
                />

                <p class="paper-note">
                  当前纸面执行观测不授予新增风险、Demo 或实盘资格。
                </p>
              </section>
              <section
                v-show="inspectorSection === 'archive'"
                class="inspector-section"
                aria-label="纸面账户档案"
              >
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
                      :description="`${liquidationRuleText()} ${atrStopMult != null ? atrRiskText() : definition.params_frozen?.stop_atr_mult != null ? stopRuleText() : '策略离场与持仓期限以所选冻结规则为准。'}`"
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
                            fixed(
                              valueOf(record, 'mark_price', 'current_price'),
                              4,
                            )
                          }}</template
                        >
                        <template v-else-if="column.key === 'leverage'">
                          {{
                            fixed(
                              valueOf(record, 'leverage') ?? strategyLeverage(),
                              0,
                            )
                          }}
                          倍
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
                          <Button
                            type="link"
                            class="!p-0 font-mono text-xs"
                            @click="showRecord(record)"
                            >{{ paperNumber(record) }}</Button
                          >
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
                          fillValue(
                            record,
                            valueOf(record, 'fill_price', 'price'),
                          )
                        }}</template>
                        <template v-else-if="column.key === 'fee'">{{
                          fillValue(record, valueOf(record, 'fee', 'fee_paid'))
                        }}</template>
                        <template v-else-if="column.key === 'reason'">{{
                          labReason(valueOf(record, 'reason', 'signal_reason'))
                        }}</template>
                      </template>
                      <template #emptyText
                        ><Empty
                          description="暂无模拟订单；等待数据就绪及实际信号"
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
                          ><Button
                            type="link"
                            class="!p-0 font-mono text-xs"
                            @click="showRecord(record)"
                            >{{ paperNumber(record) }}</Button
                          >
                          <div class="row-sub">
                            {{ textOf(record, 'pair')
                            }}<span v-if="record.group_id"> · 双腿配对</span>
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
                            valueOf(
                              record,
                              'fees_paid',
                              'fee_paid',
                              'fees',
                              'fee',
                            ),
                            4,
                          )
                        }}</template>
                        <template v-else-if="column.key === 'net'"
                          ><span
                            :class="
                              pnlClass(
                                valueOf(
                                  record,
                                  'net_pnl',
                                  'realized_pnl',
                                  'pnl',
                                ),
                              )
                            "
                            >{{
                              fixed(
                                valueOf(
                                  record,
                                  'net_pnl',
                                  'realized_pnl',
                                  'pnl',
                                ),
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
                        <p>{{ mechanismText }}</p>
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
                        <p v-if="atrRiskAccount">
                          {{ accountFactors.riskBudget }} · 计划止损与手续费预算
                          {{ fixed(riskBudget, 0) }} USDT · k =
                          {{ fixed(atrStopMult, 1) }}
                        </p>
                        <p>
                          目标投入
                          {{
                            percent(definition.position_budget?.target_exposure)
                          }}
                          · 最多
                          {{
                            fixed(definition.position_budget?.max_positions, 0)
                          }}
                          仓 · 每币
                          {{
                            fixed(definition.position_budget?.max_per_coin, 0)
                          }}
                          仓 · 杠杆
                          {{ fixed(strategyLeverage(), 0) }} 倍
                        </p>
                      </div>
                      <div class="risk-rule-block">
                        <div class="rule-label">风险保护与止损</div>
                        <p>{{ liquidationRuleText() }}</p>
                        <p v-if="atrStopMult != null">{{ atrRiskText() }}</p>
                        <p
                          v-else-if="
                            definition.params_frozen?.stop_atr_mult != null
                          "
                        >
                          s08 影线拒绝策略另有冻结 ATR 止损/止盈：止损倍数
                          {{
                            fixed(definition.params_frozen.stop_atr_mult, 2)
                          }}，止盈倍数
                          {{
                            fixed(definition.params_frozen.tp_atr_mult, 2)
                          }}；ATR 在入场时冻结，{{ stopRuleText() }}
                        </p>
                        <p v-else>
                          当前策略没有统一固定百分比止损，主要依靠策略离场判据、持仓期限与杠杆强平保护。
                        </p>
                      </div>
                      <div>
                        <div class="rule-label">实际交易币池 / 成交模型</div>
                        <p>
                          {{ executionUniverse?.join(' · ') ?? '未知' }}<br />{{
                            fillModelText()
                          }}
                        </p>
                        <p v-if="definition.research_universe?.length">
                          信号研究币池：{{
                            definition.research_universe.join(' · ')
                          }}
                        </p>
                        <p v-if="executionBlocked">
                          执行暂停：{{ executionBlockReason }}
                        </p>
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
                          labEventType(
                            valueOf(record, 'event_type', 'kind', 'type'),
                          )
                        }}</template>
                        <template v-else-if="column.key === 'reason'">
                          <Button
                            type="link"
                            class="!h-auto !p-0 text-left !whitespace-normal"
                            @click="showRecord(record)"
                            >{{ eventSummary(record) }}</Button
                          >
                        </template>
                      </template>
                      <template #emptyText
                        ><Empty description="暂无当前策略的决策事件"
                      /></template>
                    </Table>
                  </TabPane>
                </Tabs></section></template
          ></Spin>
        </section></div
    ></Spin>
    <Drawer v-model:open="recordDetailOpen" title="模拟记录详情" :width="560">
      <Descriptions v-if="recordDetail" bordered :column="1" size="small">
        <DescriptionsItem label="展示编号">{{
          paperNumber(recordDetail)
        }}</DescriptionsItem>
        <DescriptionsItem label="策略">{{
          accountName(
            accounts.find(
              (item) => item.strategy_id === recordDetail?.strategy_id,
            ),
          )
        }}</DescriptionsItem>
        <DescriptionsItem label="币种">{{
          textOf(recordDetail, 'pair')
        }}</DescriptionsItem>
        <DescriptionsItem label="状态">{{
          recordStatus(recordDetail)
        }}</DescriptionsItem>
        <DescriptionsItem label="原因">{{
          recordDetail.event_type
            ? eventSummary(recordDetail)
            : labReason(
                valueOf(recordDetail, 'reason', 'signal_reason', 'exit_reason'),
              )
        }}</DescriptionsItem>
        <DescriptionsItem v-if="!recordDetail.event_type" label="完整模拟订单号"
          ><span class="break-all">{{
            textOf(recordDetail, 'order_id', 'paper_order_id', 'entry_order_id')
          }}</span></DescriptionsItem
        >
        <DescriptionsItem v-if="recordDetail.trade_id" label="完整成交记录号"
          ><span class="break-all">{{
            recordDetail.trade_id
          }}</span></DescriptionsItem
        >
        <DescriptionsItem v-if="recordDetail.group_id" label="完整配对组编号"
          ><span class="break-all">{{
            recordDetail.group_id
          }}</span></DescriptionsItem
        >
        <DescriptionsItem
          v-if="!recordDetail.event_type"
          label="交易所原始订单号"
          >{{
            recordDetail.exchange_order_id ?? '无（模拟订单）'
          }}</DescriptionsItem
        >
        <DescriptionsItem v-if="!recordDetail.event_type" label="原始原因代码"
          ><span class="break-all">{{
            textOf(recordDetail, 'reason', 'signal_reason', 'exit_reason')
          }}</span></DescriptionsItem
        >
        <DescriptionsItem v-if="!recordDetail.event_type" label="成交价格">{{
          fillValue(
            recordDetail,
            valueOf(recordDetail, 'fill_price', 'price', 'entry_price'),
          )
        }}</DescriptionsItem>
        <DescriptionsItem v-if="!recordDetail.event_type" label="手续费 USDT">{{
          fillValue(
            recordDetail,
            valueOf(recordDetail, 'fee', 'fee_paid', 'fees'),
          )
        }}</DescriptionsItem>
        <DescriptionsItem
          v-if="recordDetail.event_type"
          label="记录时间（北京）"
          >{{ fullOrderTime(recordDetail.observed_at_ms) }}</DescriptionsItem
        >
        <DescriptionsItem
          v-for="(book, index) in recordBookEvidence"
          :key="`${book.pair}-${index}`"
          :label="`${book.pair} · 订单簿证据`"
        >
          <p>{{ book.groupLabel }} · {{ book.gateLabel }}</p>
          <p>{{ book.qualityLabel }}</p>
          <p>{{ book.featureValidityLabel }}</p>
          <p>{{ book.featuresLabel }}</p>
          <p>{{ book.pairedSampleLabel }}</p>
        </DescriptionsItem>
      </Descriptions>
      <details
        v-if="
          recordDetail && (recordDetail.event_type || recordBookEvidence.length)
        "
        class="mt-4"
      >
        <summary class="cursor-pointer text-xs">原始决策证据</summary>
        <pre class="params-box mt-2">{{
          JSON.stringify(recordDetail, null, 2)
        }}</pre>
      </details>
    </Drawer>
    <p class="paper-footer">
      前向纸面结果仅扣已记录手续费；持仓浮盈与已平仓收益分开。归档记录不等同批准或执行资格。
    </p>
  </main>
</template>
<style scoped>
.paper-workspace {
  --paper-bg: #0c1b2a;
  --paper-panel: #132b3e;
  --paper-border: #29485c;
  --paper-text: #e5edf4;
  --paper-muted: #a2b8c9;
  --paper-accent: #69b4e4;
  min-width: 0;
  max-width: 100%;
  padding: 24px;
  background: var(--paper-bg);
  color: var(--paper-text);
  font:
    13px/1.55 'IBM Plex Sans',
    'Source Han Sans SC',
    'Noto Sans SC',
    system-ui,
    sans-serif;
  color-scheme: dark;
}
.paper-workspace * {
  box-sizing: border-box;
}
.paper-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 18px;
  margin-bottom: 18px;
}
.paper-heading h1 {
  margin: 0 0 4px;
  font-size: 25px;
  font-weight: 600;
}
.paper-heading p {
  margin: 0;
  color: var(--paper-muted);
}
.paper-scope {
  display: flex;
  align-items: center;
  gap: 8px 18px;
  flex-wrap: wrap;
  padding: 10px 14px;
  border: 1px solid var(--paper-border);
  border-left: 3px solid var(--paper-accent);
  background: #102437;
  color: var(--paper-muted);
}
.paper-scope strong {
  color: var(--paper-text);
}
.paper-policy {
  margin: 16px 0;
  border: 1px solid var(--paper-border);
  background: var(--paper-panel);
  padding: 16px;
}
.paper-filters {
  display: flex;
  gap: 12px 20px;
  align-items: center;
  flex-wrap: wrap;
}
.lab-filter {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--paper-muted);
}
.population-select {
  min-width: 210px;
}
.risk-select {
  min-width: 160px;
}
.stop-select {
  min-width: 130px;
}
.paper-note {
  margin: 10px 0;
  color: var(--paper-muted);
  font-size: 12px;
}
.policy-totals {
  display: flex;
  align-items: start;
  gap: 12px 26px;
  flex-wrap: wrap;
  margin: 14px 0 0;
  padding-top: 12px;
  border-top: 1px solid var(--paper-border);
}
.policy-totals dt {
  color: var(--paper-muted);
  font-size: 11px;
}
.policy-totals dd {
  margin: 2px 0 0;
  font-size: 14px;
  font-variant-numeric: tabular-nums;
}
.paper-layout {
  display: grid;
  grid-template-columns: minmax(360px, 42%) minmax(0, 1fr);
  gap: 18px;
  align-items: start;
}
.account-ledger,
.strategy-detail {
  min-width: 0;
  border: 1px solid var(--paper-border);
  background: var(--paper-panel);
}
.ledger-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 13px 14px;
  border-bottom: 1px solid var(--paper-border);
}
.ledger-heading h2 {
  font-size: 14px;
  margin: 0;
}
.ledger-heading span {
  font-size: 11px;
  color: var(--paper-muted);
}
.ledger-scroll {
  overflow-x: auto;
  max-width: 100%;
}
.ledger-scroll table {
  width: 100%;
  min-width: 540px;
  border-collapse: collapse;
  text-align: left;
}
.ledger-scroll th,
.ledger-scroll td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--paper-border);
  vertical-align: top;
  font-size: 12px;
  font-weight: 400;
}
.ledger-scroll thead th {
  color: var(--paper-muted);
  font-size: 11px;
  white-space: nowrap;
}
.ledger-group th {
  padding: 8px 12px;
  background: #102437;
  color: var(--paper-accent);
  font-weight: 500;
}
.ledger-scroll small {
  display: block;
  margin-top: 4px;
  color: var(--paper-muted);
  font-size: 10px;
}
.ledger-scroll code {
  display: block;
  margin-top: 5px;
  color: var(--paper-muted);
  font-size: 10px;
  overflow-wrap: anywhere;
}
.ledger-scroll button {
  display: block;
  width: 100%;
  padding: 0;
  text-align: left;
  color: var(--paper-text);
  background: none;
  border: 0;
  cursor: pointer;
}
.ledger-scroll button strong {
  font-size: 12px;
  font-weight: 500;
}
.selected-row {
  background: #1c3c53;
}
.selected-row th {
  box-shadow: inset 3px 0 var(--paper-accent);
}
.ledger-scroll button:hover strong {
  color: var(--paper-accent);
}
.strategy-detail {
  padding: 16px;
  overflow: hidden;
}
.selected-identity {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 12px 0 16px;
  padding: 10px 12px;
  background: #102437;
  border: 1px solid var(--paper-border);
  font-size: 11px;
  color: var(--paper-muted);
}
.selected-identity code {
  color: var(--paper-accent);
  overflow-wrap: anywhere;
}
.selected-identity span:last-child {
  color: #f1bf86;
}
.inspector-tabs {
  display: flex;
  gap: 3px;
  margin: 0 -16px;
  padding: 0 12px;
  border-bottom: 1px solid var(--paper-border);
  overflow-x: auto;
}
.inspector-tabs button {
  flex: 1;
  min-width: 64px;
  padding: 10px 6px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: none;
  color: var(--paper-muted);
  cursor: pointer;
  white-space: nowrap;
}
.inspector-tabs button[aria-pressed='true'] {
  border-bottom-color: var(--paper-accent);
  color: var(--paper-text);
  background: #1b394e;
}
.inspector-section {
  padding: 16px 0 0;
  min-width: 0;
}
.inspector-section h3 {
  font-size: 15px;
  margin: 0 0 12px;
}
.rule-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
}
.rule-grid p {
  margin: 6px 0 0;
  overflow-wrap: anywhere;
}
.rule-label {
  color: var(--paper-muted);
  font-size: 11px;
}
.risk-rule-block {
  padding: 10px;
  border: 1px solid var(--paper-border);
  border-radius: 3px;
}
.params-box {
  max-width: 100%;
  padding: 12px;
  border: 1px solid var(--paper-border);
  border-radius: 3px;
  background: #0c1b2a;
  font-size: 11px;
  overflow-x: auto;
  color: var(--paper-text);
}
.metric-cell {
  padding: 10px;
  border: 1px solid var(--paper-border);
  border-radius: 3px;
  min-width: 0;
}
.metric-label {
  font-size: 11px;
  color: var(--paper-muted);
}
.metric-value {
  margin-top: 4px;
  font-size: 17px;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.metric-sub {
  font-size: 10px;
  color: var(--paper-muted);
}
.metric-time {
  font-size: 13px;
}
.paper-facts {
  margin: 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.paper-facts dt {
  font-size: 11px;
  color: var(--paper-muted);
}
.paper-facts dd {
  margin: 4px 0 0;
  font-size: 13px;
  overflow-wrap: anywhere;
}
.cost-boundary {
  padding: 10px 12px;
  border: 1px solid #795e3f;
  background: #3b3329;
  color: #f1bf86;
}
.paper-error {
  margin: 12px 0;
  padding: 10px 12px;
  border: 1px solid #805348;
  background: #413032;
  color: #f0b4aa;
  font-size: 12px;
}
.paper-empty {
  padding: 24px 14px;
  color: var(--paper-muted);
  text-align: center;
}
.candidate-boundaries {
  margin: 12px 0;
  padding: 10px 14px;
  border: 1px solid var(--paper-border);
  color: var(--paper-muted);
  font-size: 12px;
}
.candidate-boundaries summary {
  cursor: pointer;
  color: var(--paper-text);
}
.paper-footer {
  margin: 14px 0 0;
  color: var(--paper-muted);
  font-size: 11px;
}
.row-sub {
  font-size: 10px;
  margin-top: 4px;
  color: var(--paper-muted);
}
.profit-up {
  color: #f0a49b;
}
.profit-down {
  color: #91d0bd;
}
.risk-critical,
.risk-high {
  color: #f0a49b;
}
.risk-watch {
  color: #f1bf86;
}
.paper-workspace :deep(.text-muted-foreground) {
  color: var(--paper-muted);
}
.paper-workspace :deep(.ant-table),
.paper-workspace :deep(.ant-table-cell),
.paper-workspace :deep(.ant-tabs-content-holder),
.paper-workspace :deep(.ant-select-selector) {
  color: var(--paper-text) !important;
  background: var(--paper-panel) !important;
  border-color: var(--paper-border) !important;
}
.paper-workspace :deep(.ant-table-thead > tr > th) {
  color: var(--paper-muted) !important;
  background: #102437 !important;
  font-size: 11px;
}
.paper-workspace :deep(.ant-table-container) {
  border-color: var(--paper-border) !important;
}
.paper-workspace :deep(.ant-table-row:hover > td) {
  background: #1b394e !important;
}
.paper-workspace :deep(.ant-tabs-tab) {
  color: var(--paper-muted);
}
.paper-workspace :deep(.ant-tabs-tab-active .ant-tabs-tab-btn) {
  color: var(--paper-accent) !important;
}
.paper-workspace :deep(.ant-tabs-nav::before) {
  border-color: var(--paper-border) !important;
}
.paper-workspace :deep(.ant-alert) {
  background: #163247;
  border: 1px solid var(--paper-border);
  color: var(--paper-text);
  font-size: 12px;
}
.paper-workspace :deep(.ant-alert-message),
.paper-workspace :deep(.ant-alert-description),
.paper-workspace :deep(.ant-empty-description) {
  color: var(--paper-muted);
}
.paper-workspace :deep(.ant-btn) {
  border-color: #45677d;
  background: #1b394e;
  color: var(--paper-text);
}
.paper-workspace :deep(.ant-btn-link) {
  background: transparent;
  color: var(--paper-accent);
  border-color: transparent;
}
.paper-workspace :deep(.ant-table-wrapper) {
  min-width: 0;
  max-width: 100%;
}
.paper-workspace button:focus-visible,
.paper-workspace a:focus-visible,
.paper-workspace [tabindex]:focus-visible {
  outline: 2px solid var(--paper-accent);
  outline-offset: 3px;
}
.paper-workspace :deep(.ant-select-focused .ant-select-selector) {
  outline: 2px solid var(--paper-accent);
  outline-offset: 2px;
}
@media (min-width: 1700px) {
  .paper-workspace {
    padding: 28px 32px;
  }
  .paper-layout {
    grid-template-columns: minmax(600px, 43%) minmax(0, 1fr);
  }
}
@media (max-width: 1150px) {
  .paper-layout {
    grid-template-columns: minmax(0, 1fr);
  }
  .ledger-scroll table {
    min-width: 700px;
  }
  .rule-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 600px) {
  .paper-workspace {
    padding: 14px 12px;
  }
  .paper-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 12px;
  }
  .paper-heading h1 {
    font-size: 23px;
  }
  .paper-heading > .ant-btn {
    width: 100%;
  }
  .paper-policy {
    padding: 12px;
  }
  .paper-filters {
    align-items: stretch;
    flex-direction: column;
    gap: 10px;
  }
  .lab-filter {
    justify-content: space-between;
  }
  .lab-filter select {
    min-width: 0;
    width: 67%;
  }
  .policy-totals {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }
  .policy-totals dd {
    font-size: 12px;
  }
  .strategy-detail {
    padding: 12px;
  }
  .inspector-tabs {
    margin: 0 -12px;
    padding: 0 6px;
  }
  .inspector-tabs button {
    min-width: 58px;
    font-size: 12px;
  }
  .rule-grid,
  .paper-facts {
    grid-template-columns: minmax(0, 1fr);
  }
  .paper-scope {
    gap: 6px 12px;
    font-size: 11px;
  }
  .metric-value {
    font-size: 16px;
  }
}

.lab-filter select {
  min-height: 32px;
  padding: 6px 9px;
  border: 1px solid #45677d;
  border-radius: 3px;
  background: #0c1b2a;
  color: var(--paper-text);
  font: inherit;
  max-width: 100%;
}
.lab-filter select:focus-visible {
  outline: 2px solid var(--paper-accent);
  outline-offset: 2px;
}
.lab-filter select:disabled {
  opacity: 0.6;
}
@media (max-width: 600px) {
  .lab-filter select {
    min-width: 0;
    width: 67%;
  }
}
</style>

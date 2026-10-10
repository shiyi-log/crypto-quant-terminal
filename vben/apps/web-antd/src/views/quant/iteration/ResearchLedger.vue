<script lang="ts" setup>
import { computed, onMounted, ref } from 'vue';

import { Alert, Button, Card, Empty, Select, Table, Tag } from 'ant-design-vue';

import {
  getForwardPaper,
  getResearchLedger,
  type ForwardPaperExitRule,
  type ForwardPaperEvent,
  type ForwardPaperResponse,
  type ForwardPaperVariant,
  type ResearchLedger,
  type ResearchLedgerVersion,
} from '#/api/freqtrade';

const ledger = ref<ResearchLedger | null>(null);
const loading = ref(false);
const error = ref('');
const forwardPaper = ref<ForwardPaperResponse | null>(null);
const forwardLoading = ref(false);
const forwardError = ref('');
const selectedForwardVariant = ref('all');

const versionColumns = [
  { dataIndex: 'id', title: '版本 / 轮次', width: 190 },
  { dataIndex: 'direction', title: '迭代方向', width: 150 },
  { key: 'change', title: '相对基线的变化' },
  { key: 'reported', title: '原报指标', width: 170 },
  { key: 'deployment', title: '部署状态', width: 130 },
  { key: 'evaluation', title: '评估有效性', width: 150 },
  { key: 'effect', title: '账户订单 / 平仓', width: 150 },
  { key: 'availability', title: '可用性依据' },
];
const tradeColumns = [
  { dataIndex: 'trade_id', title: '交易', width: 80 },
  { dataIndex: 'pair', title: '币对' },
  { dataIndex: 'side', title: '方向' },
  { dataIndex: 'leverage', title: '杠杆' },
  { dataIndex: 'open_date', title: '开仓' },
  { dataIndex: 'close_date', title: '平仓' },
  { key: 'profit', title: '账户已实现收益', width: 140 },
  { key: 'exit', title: '离场原因 / 归类', width: 180 },
  { key: 'strategy', title: '策略收益归因', width: 130 },
  { dataIndex: 'model_id', title: '模型归属' },
  { dataIndex: 'attribution_status', title: '归因状态' },
];
const orderColumns = [
  { dataIndex: 'order_id', title: '订单', width: 90 },
  { dataIndex: 'trade_id', title: '交易' },
  { dataIndex: 'pair', title: '币对' },
  { dataIndex: 'side', title: '方向' },
  { dataIndex: 'status', title: '状态' },
  { key: 'fill', title: '数量 / 成交' },
  { key: 'price', title: '价格' },
  { dataIndex: 'order_date', title: '时间' },
  { dataIndex: 'model_id', title: '模型归属' },
];
const operationColumns = [
  { dataIndex: 'requested_at_utc', title: '请求时间（UTC）', width: 190 },
  { key: 'actor', title: '操作者 / 来源', width: 150 },
  { key: 'action', title: '操作 / 请求', width: 230 },
  { key: 'target', title: '目标', width: 180 },
  { key: 'outcome', title: '请求结果', width: 150 },
  { key: 'result', title: '响应证据', width: 230 },
  { dataIndex: 'request_id', title: '请求编号', width: 180 },
];
const forwardVariantColumns = [
  { dataIndex: 'variant_id', title: '变体', width: 170 },
  { key: 'rules', title: '冻结规则', width: 300 },
  { key: 'rule_hash', title: '规则哈希', width: 150 },
  { key: 'current_status', title: '当前数据状态', width: 130 },
  { key: 'replay_status', title: '最近回放状态', width: 140 },
  { dataIndex: 'closed_trade_count', title: '已平仓', width: 90 },
  { key: 'realized', title: '已实现收益', width: 130 },
  { key: 'floating', title: '浮盈亏', width: 120 },
  { key: 'equity', title: '期末估值', width: 120 },
  { dataIndex: 'open_position_count', title: '未平仓', width: 90 },
];
const forwardDecisionColumns = [
  { dataIndex: 'variant_id', title: '变体', width: 150 },
  { dataIndex: 'candle_utc', title: '信号 K 线', width: 175 },
  { dataIndex: 'event_time', title: '执行时点', width: 175 },
  { dataIndex: 'coin', title: '币种 / pair', width: 120 },
  { dataIndex: 'action_side', title: '决策 / 方向', width: 145 },
  { key: 'filter', title: '过滤指标 / 就绪状态', width: 220 },
  { key: 'trigger', title: '退出触发证据', width: 260 },
  { dataIndex: 'fee', title: '预期手续费', width: 110 },
  { dataIndex: 'reason', title: '放行 / 拒绝原因', width: 210 },
];
const forwardFillColumns = [
  { dataIndex: 'variant_id', title: '变体', width: 150 },
  { dataIndex: 'candle_utc', title: '信号 K 线', width: 175 },
  { dataIndex: 'event_time', title: '纸面成交时间', width: 175 },
  { dataIndex: 'coin', title: '币种 / pair', width: 120 },
  { dataIndex: 'action_side', title: '动作 / 方向', width: 145 },
  { dataIndex: 'price', title: '纸面开盘填价', width: 120 },
  { dataIndex: 'quantity', title: '数量', width: 100 },
  { dataIndex: 'fee', title: '手续费', width: 100 },
  { dataIndex: 'profit_abs', title: '扣手续费收益', width: 130 },
  { key: 'trigger', title: '触发 / 风险价位', width: 260 },
  { dataIndex: 'reason', title: '原因', width: 210 },
];

function value(value: unknown, suffix = '') {
  return value === null || value === undefined || value === ''
    ? '暂无'
    : `${value}${suffix}`;
}
function number(value: unknown, digits = 4) {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toFixed(digits)
    : '暂无';
}
function config(value: unknown) {
  if (value === null || value === undefined || value === '') return '暂无';
  return typeof value === 'string' ? value : JSON.stringify(value);
}
function statusColor(status: string) {
  if (status === 'verified') return 'green';
  if (status === 'invalidated') return 'red';
  if (status === 'not_deployed') return 'orange';
  return 'blue';
}
function statusText(status: string) {
  const names: Record<string, string> = {
    verified: '已验证',
    unverified: '未验证',
    not_deployed: '未部署',
    invalidated: '已作废',
    forward_observation: '事前观察中',
    planned: '计划中',
  };
  return names[status] || status || '暂无';
}
function modelAttribution(modelId: string | null) {
  return modelId || '未知（未归因）';
}
function exitCategoryText(category?: string) {
  const names: Record<string, string> = {
    strategy_signal: '策略信号',
    strategy_roi: '策略止盈',
    strategy_risk: '策略风控',
    strategy_adjustment: '策略减仓',
    execution_emergency: '执行故障应急退出',
    liquidation: '强平风险结果',
    external_intervention: '外部干预',
    external_exchange_execution: '交易所侧离场',
    unknown: '原因未知',
    not_closed: '尚未平仓',
  };
  return names[category || ''] || '归类未知';
}
function exitCategoryColor(category?: string) {
  if (category === 'external_intervention' || category === 'external_exchange_execution') return 'orange';
  if (category === 'execution_emergency') return 'red';
  if (category === 'liquidation') return 'red';
  if (category?.startsWith('strategy_')) return 'blue';
  return 'default';
}
function strategyProfitText(trade: {
  exit_category?: string;
  is_open?: boolean | null;
  strategy_eligible?: boolean;
}) {
  if (trade.is_open === true || trade.exit_category === 'not_closed') return '未成熟';
  if (trade.strategy_eligible === true) return '计入策略';
  if (trade.exit_category === 'external_intervention') return '排除：外部干预';
  if (trade.exit_category === 'external_exchange_execution') return '排除：交易所侧离场';
  return '归因未知';
}
function operationActionText(action: string) {
  const names: Record<string, string> = {
    forceexit: '强制平仓',
    force_exit: '强制平仓',
    forceenter: '强制开仓',
    force_enter: '强制开仓',
    delete_trade: '删除交易',
    cancel_open_order: '取消挂单',
    reload_trade: '重载交易',
    blacklist_add: '加入黑名单',
    blacklist_remove: '移出黑名单',
    lock_add: '锁定交易对',
    lock_remove: '解除交易对锁定',
    other_write: '其他写操作',
    reload_config: '重载配置',
    start: '启动交易',
    stop: '停止交易',
    stopbuy: '停止开仓',
    pause: '暂停交易',
    pause_entries: '暂停开仓',
  };
  return names[action] || action || '操作未记录';
}
function operationOutcomeText(outcome: string) {
  const names: Record<string, string> = {
    accepted: 'API 已接受',
    rejected: '请求被拒绝',
    unknown: '结果未知',
    not_forwarded: '未转发',
  };
  return names[outcome] || '结果未知';
}
function operationOutcomeColor(outcome: string) {
  if (outcome === 'accepted') return 'blue';
  if (outcome === 'rejected') return 'red';
  return 'orange';
}

const realizedGroups = computed(() => {
  const summary = ledger.value?.summary;
  if (!summary) return [];
  return [
    {
      label: '账户已实现',
      count: summary.closed_trade_count,
      profit: summary.realized_profit_abs,
      known: summary.realized_profit_known_abs,
      missing: summary.realized_profit_missing_count,
    },
    {
      label: '策略归因已实现',
      count: summary.strategy_closed_trade_count,
      profit: summary.strategy_realized_profit_abs,
      known: summary.strategy_realized_profit_known_abs,
      missing: summary.strategy_realized_profit_missing_count,
    },
    {
      label: '外部干预已实现',
      count: summary.external_exit_count,
      profit: summary.external_realized_profit_abs,
      known: summary.external_realized_profit_known_abs,
      missing: summary.external_realized_profit_missing_count,
    },
    {
      label: '离场原因未知已实现',
      count: summary.unknown_exit_count,
      profit: summary.unknown_realized_profit_abs,
      known: summary.unknown_realized_profit_known_abs,
      missing: summary.unknown_realized_profit_missing_count,
    },
  ];
});
function join(items?: string[]) {
  return items?.length ? items.join('；') : '暂无记录';
}
function changeHistory(items?: ResearchLedgerVersion['changes']) {
  if (!items?.length) return '暂无记录';
  return items
    .map((item) => {
      if (typeof item === 'string') return item;
      return [item.t, item.event, item.note].filter(Boolean).join('：');
    })
    .filter(Boolean)
    .join('；');
}
function configChanges(
  items?: ResearchLedger['comparison'] extends infer Comparison
    ? Comparison extends { config_changes?: infer Changes }
      ? Changes
      : never
    : never,
) {
  if (!items) return '暂无记录';
  if (!items.length) return '配置一致（无参数差异）';
  return items
    .map((item) => {
      if (typeof item === 'string') return item;
      const before = config(item.before);
      const after = config(item.after);
      return `${item.key}: ${before} → ${after}`;
    })
    .join('；');
}
function repeatRule(value: string | boolean | null | undefined) {
  if (value === true) return '避免再次使用未经验证口径';
  if (value === false || value == null || value === '') return '暂无记录';
  return value;
}
function versionRow(item: ResearchLedgerVersion) {
  const metrics = item.reported_metrics || {};
  return {
    ...item,
    key: item.id,
    roundText:
      item.round == null ? item.id : `${item.id} · 第 ${item.round} 轮`,
    changeText: `${changeHistory(item.changes)}${item.config ? ` | 配置: ${config(item.config)}` : ''}`,
    reportedText: `IC ${number(metrics.ic)} · t ${number(metrics.t, 2)} · q ${number(metrics.q)}`,
    effectText:
      item.effect?.actual_orders == null && item.effect?.closed_trades == null
        ? '未归因'
        : `${value(item.effect?.actual_orders)} / ${value(item.effect?.closed_trades)} · 账户收益 ${value(item.effect?.realized_profit_abs)}`,
    availabilityText: `证据：${join(item.evidence)}；限制：${join(item.limitations)}`,
  };
}

function eventText(value: unknown, fallback = '—') {
  if (value === null || value === undefined || value === '') return fallback;
  if (typeof value === 'number') return Number.isFinite(value) ? value.toFixed(6) : fallback;
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function eventRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function eventList(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.map(eventRecord) : [];
}

function ruleNumber(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toFixed(6).replace(/\.?0+$/, '')
    : '未知';
}

function filterKind(value: unknown) {
  const names: Record<string, string> = {
    adx: 'ADX',
    channel_width_pct: '通道宽度',
    realized_vol_pct: '实现波动率',
    none: '无过滤',
  };
  return names[String(value)] || eventText(value, '未记录');
}

function filterComparison(filter: Record<string, unknown>) {
  const op = filter.op === 'gt' ? '>' : filter.op === 'lt' ? '<' : '未知比较符';
  const unit = ['channel_width_pct', 'realized_vol_pct'].includes(String(filter.kind))
    ? '%'
    : '';
  return `${op} ${ruleNumber(filter.threshold)}${unit}`;
}

function exitRuleText(rule: ForwardPaperExitRule | undefined, label: string) {
  if (!rule) return `${label}未记录`;
  if (rule.kind === 'none') return `${label}无`;
  if (rule.kind === 'atr_multiple') {
    return `${label} ${ruleNumber(rule.value)} × ATR(${value(rule.atr_window)})`;
  }
  const mode = rule.kind === 'trailing_pct' ? '跟踪退出 ' : '';
  return `${label} ${mode}${ruleNumber(rule.value)}%`;
}

function variantRuleText(variant: ForwardPaperVariant) {
  const filter = variant.range_filter;
  const filterText = !filter
    ? '横盘过滤未记录'
    : filter.kind === 'none'
      ? '横盘过滤：无'
      : `${filterKind(filter.kind)}(${filter.window}) ${filterComparison(eventRecord(filter))} · 预热 ${filter.warmup_bars} 根`;
  const execution = variant.execution;
  const executionText = !execution
    ? '执行细则未记录'
    : [
        execution.dual_touch === 'stop_loss_first' ? '双触达止损优先' : '双触达规则未知',
        execution.exit_timing === 'next_open' && execution.gap_fill === 'execution_open'
          ? '次根开盘价退出（含跳空）'
          : '退出成交规则未知',
        execution.slot_release === 'after_exit_fill_next_open'
          ? '退出释放的槽位下一开盘复用'
          : '槽位规则未知',
      ].join('；');
  return {
    channel: `入 / 退通道 ${value(variant.chan_entry)} / ${value(variant.chan_exit)}`,
    filter: filterText,
    exits: `${exitRuleText(variant.take_profit, '止盈')}；${exitRuleText(variant.stop_loss, '止损')}`,
    execution: executionText,
  };
}

function paperReason(value: unknown, fallback = '—') {
  const names: Record<string, string> = {
    allow: '放行',
    deny: '拒绝',
    entry: '开仓',
    exit: '平仓',
    close: '平仓',
    long: '多',
    short: '空',
    outside_top_n: '排名不在前 N',
    already_held: '已有持仓',
    max_open: '持仓槽位已满',
    missing_open_price: '缺开盘价，保留槽位',
    insufficient_cash: '可用现金不足',
    top_n_and_slot_available: '排名与槽位允许',
    range_filter_failed: '横盘过滤未通过',
    range_filter_rejected: '横盘过滤未通过',
    range_filter_not_ready: '横盘过滤指标未就绪',
    risk_indicators_not_ready: '风险指标未就绪',
    stop_loss: '止损',
    take_profit: '止盈',
    trailing_take_profit: '跟踪退出',
    trend_end: '趋势结束',
    trend_end_pending_missing_open: '趋势结束，缺价等待成交',
    pending_exit_filled: '等待退出已成交',
    no_rankable_candidates: '无可排名候选',
    no_signal_candidates: '无信号候选',
    engine_warmup: '信号预热',
  };
  return names[String(value)] || eventText(value, fallback);
}

function filterEvidence(value: unknown) {
  const filter = eventRecord(value);
  if (!Object.keys(filter).length) return { text: '未记录', status: '', ready: null };
  if (filter.kind === 'none') return { text: '无横盘过滤', status: '', ready: true };
  const unit = ['channel_width_pct', 'realized_vol_pct'].includes(String(filter.kind)) ? '%' : '';
  const status = filter.ready === false
    ? '指标未就绪'
    : filter.ready === true
      ? filter.passed === true ? '就绪 · 通过' : filter.passed === false ? '就绪 · 未通过' : '就绪 · 结果未知'
      : '就绪状态未记录';
  return {
    text: `${filterKind(filter.kind)}(${eventText(filter.window)}) ${ruleNumber(filter.raw_value)}${unit} ${filterComparison(filter)}`,
    status,
    ready: typeof filter.ready === 'boolean' ? filter.ready : null,
  };
}

function triggerEvidence(value: unknown, riskValue?: unknown) {
  const trigger = eventRecord(value);
  const risk = eventRecord(riskValue);
  if (Object.keys(trigger).length) {
    const level = typeof trigger.trigger_level === 'number'
      ? `触发线 ${ruleNumber(trigger.trigger_level)}`
      : '';
    const observed = typeof trigger.observed_price === 'number'
      ? `观察价 ${ruleNumber(trigger.observed_price)}`
      : '';
    return {
      time: eventText(trigger.candle_utc, ''),
      text: [paperReason(trigger.reason), level, observed].filter(Boolean).join(' · '),
      detail: trigger.dual_touch === true
        ? trigger.dual_touch_policy === 'stop_loss_first' ? '同根双触达，按冻结规则止损优先' : '同根双触达，处理规则未记录'
        : '',
    };
  }
  const levels = [
    typeof risk.take_profit_level === 'number' ? `止盈线 ${ruleNumber(risk.take_profit_level)}` : '',
    typeof risk.stop_loss_level === 'number' ? `止损线 ${ruleNumber(risk.stop_loss_level)}` : '',
    typeof risk.trailing_level === 'number' ? `跟踪线 ${ruleNumber(risk.trailing_level)}` : '',
  ].filter(Boolean);
  return { time: '', text: levels.join(' · ') || '—', detail: '' };
}

function matchesSelectedVariant(variantId: unknown) {
  return selectedForwardVariant.value === 'all' ||
    variantId === selectedForwardVariant.value;
}

const forwardVariantOptions = computed(() => [
  { label: '全部变体', value: 'all' },
  ...(forwardPaper.value?.manifest.variants || []).map((variant) => ({
    label: variant.variant_id,
    value: variant.variant_id,
  })),
]);

const forwardCurrentReady = computed(() => {
  const response = forwardPaper.value;
  if (!response) return false;
  return response.latest_snapshot?.data_ready ?? response.summary.data_ready;
});

type ForwardDatabaseLedger = {
  enabled?: boolean;
  synced_event_count?: number;
  sync_error?: string | null;
};

const forwardDatabaseLedger = computed<ForwardDatabaseLedger>(() =>
  forwardPaper.value?.summary.database_ledger ||
  forwardPaper.value?.checkpoint?.database_ledger ||
  { enabled: false, sync_error: null },
);

const forwardVariantRows = computed(() => {
  const response = forwardPaper.value;
  if (!response) return [];
  const summaries = response.variants || response.checkpoint?.variant_summaries || {};
  const replaySummaries = response.last_successful_replay?.variants ||
    response.checkpoint?.variant_summaries || {};
  const currentReady = forwardCurrentReady.value;
  return (response.manifest.variants || []).map((variant) => {
    const summary = summaries[variant.variant_id] || {};
    const replaySummary = replaySummaries[variant.variant_id] || {};
    return {
      ...summary,
      key: variant.variant_id,
      variant_id: variant.variant_id,
      rule_hash: variant.rule_hash,
      hypothesis: variant.hypothesis,
      rules: variantRuleText(variant),
      current_status: currentReady ? '数据就绪' : '当前未就绪',
      replay_status: replaySummary.strategy_usable,
    };
  });
});

const forwardDecisionRows = computed(() => {
  const events = forwardPaper.value?.decisions || [];
  return events
    .filter((event) => matchesSelectedVariant(event.variant_id))
    .flatMap((event, eventIndex) => {
      const candidates = eventList(event.candidates);
      const exits = eventList(event.exits);
      const baseKey = event.event_id || `${event.variant_id}-${event.candle_utc}-${eventIndex}`;
      const makeRow = (
        detail: Record<string, unknown>,
        detailIndex: number,
        kind: 'candidate' | 'exit',
      ) => ({
        key: `${baseKey}-${kind}-${detailIndex}`,
        variant_id: event.variant_id,
        candle_utc: eventText(event.candle_utc),
        event_time: eventText(event.execution_at_utc),
        coin: eventText(detail.pair ?? detail.coin),
        action_side: [
          paperReason(detail.decision ?? detail.status ?? (kind === 'exit' ? '退出' : '候选')),
          eventText(detail.side, ''),
        ].filter(Boolean).join(' / '),
        filter: filterEvidence(detail.range_filter),
        trigger: triggerEvidence(detail.trigger, detail.risk_state),
        fee: eventText(detail.expected_fee),
        reason: paperReason(detail.reason ?? event.decision_reason),
      });
      const rows = [
        ...candidates.map((candidate, index) => makeRow(candidate, index, 'candidate')),
        ...exits.map((exit, index) => makeRow(exit, candidates.length + index, 'exit')),
      ];
      if (rows.length) return rows;
      return [{
        key: `${baseKey}-summary`,
        variant_id: event.variant_id,
        candle_utc: eventText(event.candle_utc),
        event_time: eventText(event.execution_at_utc),
        coin: '—',
        action_side: '决策事件',
        filter: { text: '—', status: '', ready: null },
        trigger: { text: '—', detail: '', time: '' },
        fee: '—',
        reason: paperReason(event.decision_reason),
      }];
    })
    .reverse();
});

function decisionReasonForFill(fill: ForwardPaperEvent) {
  const decision = (forwardPaper.value?.decisions || []).find((event) =>
    event.variant_id === fill.variant_id &&
    event.candle_utc === fill.candle_utc,
  );
  if (!decision) return '成交事件未保存原因';
  const details = fill.action === 'entry'
    ? eventList(decision.candidates)
    : eventList(decision.exits);
  const match = details.find((detail) => detail.coin === fill.coin);
  return eventText(match?.reason ?? decision.decision_reason, '成交事件未保存原因');
}

const forwardFillRows = computed(() =>
  (forwardPaper.value?.fills || [])
    .filter((event) => matchesSelectedVariant(event.variant_id))
    .slice(-30)
    .reverse()
    .map((event, index) => ({
      key: event.event_id || `${event.variant_id}-${event.filled_at_utc}-${index}`,
      variant_id: event.variant_id,
      candle_utc: eventText(event.candle_utc),
      event_time: eventText(event.filled_at_utc),
      coin: eventText(event.coin),
      action_side: [eventText(event.action), eventText(event.side, '')].filter(Boolean).join(' / '),
      price: eventText(event.price),
      quantity: eventText(event.quantity),
      fee: eventText(event.fee),
      profit_abs: eventText(event.profit_abs),
      reason: paperReason(event.reason ?? event.exit_reason ?? decisionReasonForFill(event)),
      trigger: triggerEvidence(event.trigger, event.risk_state),
    })),
);

const forwardCloseRows = computed(() =>
  (forwardPaper.value?.closes || [])
    .filter((event) => matchesSelectedVariant(event.variant_id))
    .slice(-30)
    .reverse()
    .map((event, index) => ({
      key: event.event_id || `${event.variant_id}-${event.filled_at_utc}-${index}`,
      variant_id: event.variant_id,
      candle_utc: eventText(event.candle_utc),
      event_time: eventText(event.filled_at_utc ?? event.close_date),
      coin: eventText(event.coin ?? event.pair),
      action_side: ['平仓', eventText(event.side, '')].filter(Boolean).join(' / '),
      price: eventText(event.price ?? event.close_rate),
      quantity: eventText(event.quantity),
      fee: eventText(event.fee),
      profit_abs: eventText(event.profit_abs ?? event.pnl),
      reason: paperReason(event.reason ?? event.exit_reason ?? decisionReasonForFill(event)),
      trigger: triggerEvidence(event.trigger, event.risk_state),
    })),
);

function forwardNumber(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toFixed(4)
    : '未知';
}

function replayStatus(value: unknown) {
  if (value === true) return '可用';
  if (value === false) return '不可用';
  return '未知';
}

async function load() {
  loading.value = true;
  error.value = '';
  forwardLoading.value = true;
  forwardError.value = '';
  try {
    const [ledgerResult, forwardResult] = await Promise.allSettled([
      getResearchLedger(),
      getForwardPaper(undefined, 30),
    ]);
    if (ledgerResult.status === 'fulfilled') {
      ledger.value = ledgerResult.value;
    } else {
      ledger.value = null;
      error.value = ledgerResult.reason?.message || '证据账读取失败';
    }
    if (forwardResult.status === 'fulfilled') {
      forwardPaper.value = forwardResult.value;
    } else {
      forwardPaper.value = null;
      forwardError.value =
        forwardResult.reason?.message || '前向纸面运行读取失败';
    }
  } finally {
    loading.value = false;
    forwardLoading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <Card :bordered="false" class="mb-4 shadow-sm" title="模型与交易证据账">
    <template #extra>
      <Button :loading="loading" size="small" @click="load">手动刷新</Button>
    </template>

    <Alert
      v-if="error"
      class="mb-3"
      type="error"
      show-icon
      :message="error"
      description="请检查认证服务和账本接口后重试。"
    >
      <template #action
        ><Button size="small" @click="load">重试</Button></template
      >
    </Alert>

    <Empty v-if="!ledger && !loading && !error" description="暂无证据账数据" />
    <template v-if="ledger">
      <Tag v-if="ledger.source_id?.includes('dryrun')" color="red" class="mb-2">
        模拟盘 dry-run · 模拟订单与收益
      </Tag>
      <div class="mb-3 text-xs text-muted-foreground">
        来源 {{ value(ledger.source_id) }} · 接口快照
        {{ value(ledger.generated_at) }} · 研究账本事件同步
        {{ value(ledger.last_synced_at) }}
      </div>

      <div class="grid grid-cols-2 gap-3 lg:grid-cols-6">
        <div
          v-for="item in [
            ['登记版本', ledger.summary.version_count],
            ['实际交易', ledger.summary.actual_trade_count],
            ['实际订单', ledger.summary.actual_order_count],
            ['已平仓', ledger.summary.closed_trade_count],
            ['已部署 ML', ledger.summary.deployed_ml_count],
            ['未归因交易', ledger.summary.unattributed_trade_count],
          ]"
          :key="item[0]"
          class="rounded-lg border border-gray-100 p-3 dark:border-gray-800"
        >
          <div class="text-xs text-muted-foreground">{{ item[0] }}</div>
          <div class="mt-1 font-mono text-base font-semibold">
            {{ value(item[1]) }}
          </div>
        </div>
      </div>
      <div class="mt-3 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <div
          v-for="group in realizedGroups"
          :key="group.label"
          class="rounded-lg border border-gray-100 p-3 dark:border-gray-800"
        >
          <div class="text-xs text-muted-foreground">{{ group.label }}</div>
          <div class="mt-1 font-mono text-base font-semibold">
            {{ number(group.profit) }}
          </div>
          <div class="mt-1 text-xs text-muted-foreground">
            已平仓 {{ value(group.count) }} 笔
          </div>
          <div v-if="group.missing && group.missing > 0" class="mt-1 text-xs text-orange-600">
            收益缺失 {{ group.missing }} 笔；已知小计 {{ number(group.known) }}，非完整收益。
          </div>
        </div>
      </div>
      <div class="mt-2 text-xs text-muted-foreground">
        账户收益包含全部已平仓交易。策略归因计入信号、止盈、风控、强平和执行故障结果；force_exit / force_sell 属于外部干预，单独记录，不计入策略表现。未平仓浮盈亏不计入已实现收益。原因未知不等于人工干预。
      </div>
      <div v-if="ledger.exit_classification_limitations?.length" class="mt-2 text-xs text-muted-foreground">
        归因限制：{{ join(ledger.exit_classification_limitations) }}
      </div>
      <div v-if="ledger.summary.unknown_trade_state_count" class="mt-2 text-xs text-orange-600">
        {{ ledger.summary.unknown_trade_state_count }} 笔交易的开闭仓状态未知，不作已平仓收益推断。
      </div>
      <div v-if="ledger.summary.externally_intervened_trade_count" class="mt-2 text-xs text-orange-600">
        {{ ledger.summary.externally_intervened_trade_count }} 笔交易存在外部干预成交证据（包含尚未平仓的外部减仓）。有外部干预的已平仓交易整笔归外部，不拆分策略损益。
      </div>
      <div v-if="ledger.summary.partial_exit_audit_unknown_trade_count" class="mt-2 text-xs text-muted-foreground">
        {{ ledger.summary.partial_exit_audit_unknown_trade_count }} 笔交易缺少完整的订单标签审计，不能仅凭最终离场原因排除外部部分平仓。
      </div>
      <div
        v-if="ledger.summary.closed_trade_count === 0"
        class="mt-3 rounded-lg bg-orange-50 p-3 text-xs text-orange-700 dark:bg-orange-950/20"
      >
        暂无成熟收益：当前没有已平仓交易，收益、胜率和期望值不作推断。
      </div>
      <div
        v-else-if="ledger.summary.strategy_closed_trade_count === 0"
        class="mt-3 rounded-lg bg-orange-50 p-3 text-xs text-orange-700 dark:bg-orange-950/20"
      >
        暂无策略归因的成熟收益：账户已有平仓，但不能据此推断策略胜率或期望值。
      </div>
      <div
        v-if="ledger.summary.deployed_ml_count === 0"
        class="mt-2 rounded-lg bg-blue-50 p-3 text-xs text-blue-700 dark:bg-blue-950/20"
      >
        ML 未部署：研究版本没有参与实际订单，登记 champion 不代表运行中的模型。
      </div>

      <div class="mt-5 mb-2 text-sm font-medium">每代模型与策略版本</div>
      <Table
        :columns="versionColumns"
        :data-source="ledger.versions.map(versionRow)"
        :pagination="{ pageSize: 8, hideOnSinglePage: true }"
        :scroll="{ x: 1100 }"
        size="small"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.dataIndex === 'id'"
            ><span class="font-mono text-xs">{{
              record.roundText
            }}</span></template
          >
          <template v-else-if="column.key === 'change'"
            ><span class="text-xs">{{ record.changeText }}</span></template
          >
          <template v-else-if="column.key === 'reported'"
            ><span class="font-mono text-xs">{{
              record.reportedText
            }}</span></template
          >
          <template v-else-if="column.key === 'deployment'"
            ><Tag :color="statusColor(record.deployment_status)">{{
              statusText(record.deployment_status)
            }}</Tag></template
          >
          <template v-else-if="column.key === 'evaluation'"
            ><Tag :color="statusColor(record.evaluation_status)">{{
              statusText(record.evaluation_status)
            }}</Tag></template
          >
          <template v-else-if="column.key === 'effect'"
            ><span class="font-mono text-xs">{{
              record.effectText
            }}</span></template
          >
          <template v-else-if="column.key === 'availability'"
            ><span class="text-xs">{{
              record.availabilityText
            }}</span></template
          >
        </template>
      </Table>
      <div
        v-if="ledger.versions.length"
        class="mt-2 text-xs text-muted-foreground"
      >
        版本指标仅作原始记录；已作废或未验证的历史结果不会被当作收益证据。
      </div>

      <div
        v-if="ledger.comparison"
        class="mt-5 rounded-lg border border-gray-100 p-3 text-xs dark:border-gray-800"
      >
        <div class="mb-1 font-medium">基线与候选对比</div>
        <div>
          基线 {{ value(ledger.comparison.baseline_id) }} → 候选
          {{ value(ledger.comparison.candidate_id) }}
        </div>
        <div class="mt-1">
          参数变化：{{ configChanges(ledger.comparison.config_changes) }}
        </div>
        <div class="mt-1 text-muted-foreground">
          {{ value(ledger.comparison.note) }}
        </div>
      </div>

      <div class="mt-5 mb-2 text-sm font-medium">交易操作审计</div>
      <Alert
        class="mb-3"
        type="info"
        show-icon
        message="API 请求记录与订单成交分开核验"
        description="API 已接受仅表示接口接受请求，不代表成交成功。过去未记录的操作，以及绕过认证代理直连机器人的请求，无法据此追溯调用者；不会猜测历史 force_exit 的操作者。"
      />
      <div v-if="ledger.operation_audit?.limitations?.length" class="mb-2 text-xs text-muted-foreground">
        覆盖限制：{{ join(ledger.operation_audit.limitations) }}
      </div>
      <Table
        v-if="ledger.operation_audit?.items?.length"
        :columns="operationColumns"
        :data-source="ledger.operation_audit.items"
        row-key="request_id"
        :pagination="{ pageSize: 6, hideOnSinglePage: true }"
        :scroll="{ x: 1300 }"
        size="small"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'actor'">
            <div class="text-xs">{{ value(record.actor) }}</div>
            <div class="text-xs text-muted-foreground">{{ value(record.source_ip) }}</div>
          </template>
          <template v-else-if="column.key === 'action'">
            <div class="text-xs">{{ operationActionText(record.action) }}</div>
            <div class="font-mono text-xs text-muted-foreground">{{ record.method }} {{ record.path }}</div>
          </template>
          <template v-else-if="column.key === 'target'">
            <span class="break-all font-mono text-xs">{{ config(record.target) }}</span>
          </template>
          <template v-else-if="column.key === 'outcome'">
            <Tag :color="operationOutcomeColor(record.outcome)">{{ operationOutcomeText(record.outcome) }}</Tag>
            <div v-if="record.upstream_status != null" class="text-xs">HTTP {{ record.upstream_status }}</div>
          </template>
          <template v-else-if="column.key === 'result'">
            <div class="text-xs">{{ record.result_event_id ? `响应时间 ${value(record.finished_at_utc)}` : '没有响应记录，结果未知' }}</div>
            <div v-if="record.error_code" class="font-mono text-xs text-orange-600">{{ record.error_code }}</div>
            <div v-if="record.result_event_id" class="break-all font-mono text-xs text-muted-foreground">{{ record.result_event_id }}</div>
          </template>
          <template v-else-if="column.dataIndex === 'request_id'">
            <span class="break-all font-mono text-xs">{{ record.request_id }}</span>
          </template>
        </template>
      </Table>
      <Empty
        v-else
        :image="Empty.PRESENTED_IMAGE_SIMPLE"
        :description="ledger.operation_audit ? '暂无已记录的操作请求，不表示历史上无人干预' : '操作审计记录暂不可用'"
      />

      <div class="mt-5 grid gap-4 lg:grid-cols-2">
        <div>
          <div class="mb-2 text-sm font-medium">实际交易（与订单分开）</div>
          <Table
            :columns="tradeColumns"
            :data-source="ledger.actual_trades"
            :pagination="{ pageSize: 6, hideOnSinglePage: true }"
            :scroll="{ x: 1400 }"
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'profit'"
                ><span class="font-mono text-xs">{{
                  value(record.realized_profit_abs)
                }}</span></template
              >
              <template v-else-if="column.dataIndex === 'model_id'"
                ><span class="text-xs">{{
                  modelAttribution(record.model_id)
                }}</span></template
              >
              <template v-else-if="column.dataIndex === 'close_date'"
                ><span class="text-xs">{{
                  value(record.close_date)
                }}</span></template
              >
              <template v-else-if="column.dataIndex === 'attribution_status'"
                ><Tag color="blue">{{
                  value(record.attribution_status)
                }}</Tag></template
              >
              <template v-else-if="column.key === 'exit'">
                <div class="text-xs">
                  <div class="font-mono">{{ value(record.exit_reason) }}</div>
                  <Tag :color="exitCategoryColor(record.exit_category)">
                    {{ exitCategoryText(record.exit_category) }}
                  </Tag>
                  <Tag v-if="record.externally_intervened" color="orange">外部干预成交</Tag>
                  <div v-if="record.exit_classification_basis === 'filled_external_exit_order'" class="text-muted-foreground">
                    依据：已成交外部退出订单 {{ value(record.external_exit_order_count) }} 笔
                  </div>
                  <div v-if="record.order_tag_audit_unknown !== false" class="text-muted-foreground">
                    部分退出审计不完整
                  </div>
                </div>
              </template>
              <template v-else-if="column.key === 'strategy'">
                <span class="text-xs" :class="record.exit_category === 'external_intervention' ? 'text-orange-600' : ''">
                  {{ strategyProfitText(record) }}
                </span>
              </template>
            </template>
          </Table>
        </div>
        <div>
          <div class="mb-2 text-sm font-medium">实际订单</div>
          <Table
            :columns="orderColumns"
            :data-source="ledger.actual_orders"
            :pagination="{ pageSize: 6, hideOnSinglePage: true }"
            :scroll="{ x: 950 }"
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'fill'"
                ><span class="font-mono text-xs"
                  >{{ value(record.amount) }} / {{ value(record.filled) }}</span
                ></template
              >
              <template v-else-if="column.key === 'price'"
                ><span class="font-mono text-xs">{{
                  value(record.price)
                }}</span></template
              >
              <template v-else-if="column.dataIndex === 'model_id'"
                ><span class="text-xs">{{
                  modelAttribution(record.model_id)
                }}</span></template
              >
            </template>
          </Table>
        </div>
      </div>

      <div class="mt-5 grid gap-4 lg:grid-cols-2">
        <div>
          <div class="mb-2 text-sm font-medium">已确认的教训</div>
          <div
            v-for="lesson in ledger.lessons"
            :key="lesson.id"
            class="mb-2 rounded-lg border border-gray-100 p-3 text-xs dark:border-gray-800"
          >
            <div class="flex items-center gap-2">
              <b>{{ lesson.title }}</b
              ><Tag :color="statusColor(lesson.status)">{{
                statusText(lesson.status)
              }}</Tag>
            </div>
            <div class="mt-1">原因：{{ lesson.reason }}</div>
            <div class="mt-1 text-muted-foreground">
              证据：{{ join(lesson.evidence) }}
            </div>
            <div class="mt-1 text-orange-600">
              避免重犯：{{ repeatRule(lesson.do_not_repeat) }}
            </div>
          </div>
          <Empty
            v-if="!ledger.lessons.length"
            :image="Empty.PRESENTED_IMAGE_SIMPLE"
            description="暂无教训记录"
          />
        </div>
        <div>
          <div class="mb-2 text-sm font-medium">研究方向</div>
          <div
            v-for="direction in ledger.directions"
            :key="direction.id"
            class="mb-2 rounded-lg border border-gray-100 p-3 text-xs dark:border-gray-800"
          >
            <div class="flex items-center gap-2">
              <b>{{ direction.title }}</b
              ><Tag :color="statusColor(direction.status)">{{
                statusText(direction.status)
              }}</Tag>
            </div>
            <div class="mt-1">假设：{{ direction.hypothesis }}</div>
            <div class="mt-1 text-muted-foreground">
              下一检查点：{{ direction.next_check }}
            </div>
          </div>
          <Empty
            v-if="!ledger.directions.length"
            :image="Empty.PRESENTED_IMAGE_SIMPLE"
            description="暂无研究方向"
          />
        </div>
      </div>
      <Alert
        v-if="ledger.errors.length"
        class="mt-4"
        type="warning"
        show-icon
        message="账本存在数据错误"
        :description="ledger.errors.join('；')"
      />
    </template>
  </Card>

  <Card :bordered="false" class="mb-4 shadow-sm" title="前向纸面运行">
    <template #extra>
      <Button :loading="forwardLoading" size="small" @click="load">
        刷新
      </Button>
    </template>

    <div v-if="forwardLoading" class="text-xs text-muted-foreground">
      正在读取前向纸面记录…
    </div>
    <Alert
      v-else-if="forwardError"
      class="mb-3"
      type="warning"
      show-icon
      message="前向纸面记录暂不可用"
      :description="forwardError"
    />
    <Empty
      v-else-if="!forwardPaper"
      description="暂无前向纸面运行记录"
    />
    <template v-else>
      <div class="mb-3 text-xs text-muted-foreground">
        运行 {{ forwardPaper.run_id }} · 最近 K 线
        {{ value(forwardPaper.summary.candle_through_utc) }} · 数据指纹
        <span class="font-mono">{{ value(forwardPaper.summary.data_fingerprint) }}</span>
      </div>
      <Alert
        class="mb-3"
        :type="forwardDatabaseLedger.sync_error ? 'warning' : 'info'"
        show-icon
        :message="forwardDatabaseLedger.enabled
          ? (forwardDatabaseLedger.sync_error ? '数据库账本同步待重试' : '数据库账本已接通')
          : '数据库账本未接通，当前使用本地 JSONL outbox'"
        :description="forwardDatabaseLedger.sync_error || '纸面回放不会因数据库暂时不可用而中断；后续更新或重启会重试同步。'"
      />
      <div class="grid grid-cols-2 gap-3 lg:grid-cols-7">
        <div
          v-for="item in [
            ['数据状态', forwardPaper.summary.data_ready ? '就绪' : '未就绪'],
            ['决策', forwardPaper.summary.decision_count],
            ['成交', forwardPaper.summary.fill_count],
            ['变体数', forwardVariantRows.length],
            ['数据库同步事件', forwardDatabaseLedger.synced_event_count ?? '未知'],
          ]"
          :key="item[0]"
          class="rounded-lg border border-gray-100 p-3 dark:border-gray-800"
        >
          <div class="text-xs text-muted-foreground">{{ item[0] }}</div>
          <div class="mt-1 font-mono text-base font-semibold">
            {{ value(item[1]) }}
          </div>
        </div>
      </div>
      <Alert
        class="mt-3"
        type="info"
        show-icon
        message="收益口径"
        description="手续费已由纸面引擎建模；滑点和资金费仍未知，净收益不会被伪装成完整结果。"
      />
      <div class="mt-5 mb-2 text-sm font-medium">并行变体效果</div>
      <div class="mb-2 text-xs text-muted-foreground">
        当前数据状态与最近一次成功回放状态分开展示；最近回放指标按变体独立，不跨变体合计。
      </div>
      <Table
        :columns="forwardVariantColumns"
        :data-source="forwardVariantRows"
        :pagination="{ pageSize: 10, hideOnSinglePage: true }"
        :scroll="{ x: 850 }"
        size="small"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'rule_hash'">
            <span class="font-mono text-xs">{{ value(record.rule_hash) }}</span>
          </template>
          <template v-else-if="column.key === 'rules'">
            <div class="text-xs leading-5">
              <div>{{ record.rules.channel }} · {{ record.rules.filter }}</div>
              <div>{{ record.rules.exits }}</div>
              <div class="text-muted-foreground">{{ record.rules.execution }}</div>
            </div>
          </template>
          <template v-else-if="column.key === 'current_status'">
            <Tag :color="forwardCurrentReady ? 'green' : 'red'">
              {{ forwardCurrentReady ? '就绪' : '当前未就绪' }}
            </Tag>
          </template>
          <template v-else-if="column.key === 'replay_status'">
            <Tag :color="record.replay_status === true ? 'green' : record.replay_status === false ? 'red' : 'default'">
              {{ replayStatus(record.replay_status) }}
            </Tag>
          </template>
          <template v-else-if="column.key === 'realized'">
            <span class="font-mono text-xs">{{
              forwardNumber(record.realized_profit_after_fee_before_unknown_costs)
            }}</span>
          </template>
          <template v-else-if="column.key === 'floating'">
            <span class="font-mono text-xs">{{
              forwardNumber(record.unrealized_pnl_before_unknown_costs)
            }}</span>
          </template>
          <template v-else-if="column.key === 'equity'">
            <span class="font-mono text-xs">{{
              forwardNumber(record.ending_equity_marked)
            }}</span>
          </template>
        </template>
      </Table>
      <div class="mt-2 text-xs text-muted-foreground">
        变体只在纸面运行。已平仓收益、浮盈亏和未平仓数量按变体分别记录。
      </div>

      <div class="mt-5 flex flex-wrap items-center justify-between gap-2">
        <div class="text-sm font-medium">最近事件记录</div>
        <Select
          v-model:value="selectedForwardVariant"
          :options="forwardVariantOptions"
          class="!w-56"
          aria-label="按变体筛选前向纸面记录"
        />
      </div>
      <div class="mt-1 mb-3 text-xs text-muted-foreground">
        接口按事件返回最近记录；决策候选按币种展开。时间均为 UTC，信号 K 线时间与执行 / 成交时间分开显示。纸面成交不代表实盘成交；滑点和资金费未知。
      </div>

      <div class="mb-4">
        <div class="mb-2 text-sm font-medium">最近决策</div>
        <Table
          :columns="forwardDecisionColumns"
          :data-source="forwardDecisionRows"
          :pagination="{ pageSize: 10, hideOnSinglePage: true }"
          :scroll="{ x: 1200 }"
          size="small"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'filter'">
              <div class="text-xs">
                <div>{{ record.filter.text }}</div>
                <Tag v-if="record.filter.status" :color="record.filter.ready === false ? 'orange' : record.filter.ready === true ? 'green' : 'default'">
                  {{ record.filter.status }}
                </Tag>
              </div>
            </template>
            <template v-else-if="column.key === 'trigger'">
              <div class="text-xs">
                <div>{{ record.trigger.text }}</div>
                <div v-if="record.trigger.time" class="text-muted-foreground">触发 K 线 {{ record.trigger.time }}</div>
                <div v-if="record.trigger.detail" class="text-muted-foreground">{{ record.trigger.detail }}</div>
              </div>
            </template>
            <template v-else-if="column.dataIndex === 'reason'">
              <span class="text-xs">{{ record.reason }}</span>
            </template>
          </template>
        </Table>
        <Empty
          v-if="!forwardDecisionRows.length"
          :image="Empty.PRESENTED_IMAGE_SIMPLE"
          description="暂无匹配的决策记录"
        />
      </div>
      <div class="mb-4">
        <div class="mb-2 text-sm font-medium">最近纸面成交</div>
        <Table
          :columns="forwardFillColumns"
          :data-source="forwardFillRows"
          :pagination="{ pageSize: 10, hideOnSinglePage: true }"
          :scroll="{ x: 1200 }"
          size="small"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'trigger'">
              <div class="text-xs">
                <div>{{ record.trigger.text }}</div>
                <div v-if="record.trigger.time" class="text-muted-foreground">触发 K 线 {{ record.trigger.time }}</div>
                <div v-if="record.trigger.detail" class="text-muted-foreground">{{ record.trigger.detail }}</div>
              </div>
            </template>
            <template v-else-if="column.dataIndex === 'reason'">
              <span class="text-xs">{{ record.reason }}</span>
            </template>
          </template>
        </Table>
        <Empty
          v-if="!forwardFillRows.length"
          :image="Empty.PRESENTED_IMAGE_SIMPLE"
          description="暂无匹配的成交记录"
        />
      </div>
      <div>
        <div class="mb-2 text-sm font-medium">最近平仓明细</div>
        <Table
          :columns="forwardFillColumns"
          :data-source="forwardCloseRows"
          :pagination="{ pageSize: 10, hideOnSinglePage: true }"
          :scroll="{ x: 1200 }"
          size="small"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'trigger'">
              <div class="text-xs">
                <div>{{ record.trigger.text }}</div>
                <div v-if="record.trigger.time" class="text-muted-foreground">触发 K 线 {{ record.trigger.time }}</div>
                <div v-if="record.trigger.detail" class="text-muted-foreground">{{ record.trigger.detail }}</div>
              </div>
            </template>
            <template v-else-if="column.dataIndex === 'reason'">
              <span class="text-xs">{{ record.reason }}</span>
            </template>
          </template>
        </Table>
        <Empty
          v-if="!forwardCloseRows.length"
          :image="Empty.PRESENTED_IMAGE_SIMPLE"
          description="暂无匹配的平仓记录"
        />
      </div>
    </template>
  </Card>
</template>

<script lang="ts" setup>
import { computed, onMounted, ref } from 'vue';

import { Alert, Button, Card, Empty, Select, Table, Tag } from 'ant-design-vue';

import {
  getForwardPaper,
  getResearchLedger,
  type ForwardPaperEvent,
  type ForwardPaperResponse,
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
  { key: 'effect', title: '实际订单 / 平仓', width: 150 },
  { key: 'availability', title: '可用性依据' },
];
const tradeColumns = [
  { dataIndex: 'trade_id', title: '交易', width: 80 },
  { dataIndex: 'pair', title: '币对' },
  { dataIndex: 'side', title: '方向' },
  { dataIndex: 'leverage', title: '杠杆' },
  { dataIndex: 'open_date', title: '开仓' },
  { dataIndex: 'close_date', title: '平仓' },
  { key: 'profit', title: '已实现收益' },
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
const forwardVariantColumns = [
  { dataIndex: 'variant_id', title: '变体', width: 170 },
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
  { dataIndex: 'price', title: '价格', width: 100 },
  { dataIndex: 'quantity', title: '数量', width: 100 },
  { dataIndex: 'fee', title: '预期手续费', width: 110 },
  { dataIndex: 'profit_abs', title: 'profit_abs', width: 110 },
  { dataIndex: 'reason', title: '原因', width: 210 },
];
const forwardFillColumns = [
  { dataIndex: 'variant_id', title: '变体', width: 150 },
  { dataIndex: 'candle_utc', title: '信号 K 线', width: 175 },
  { dataIndex: 'event_time', title: '成交时间', width: 175 },
  { dataIndex: 'coin', title: '币种 / pair', width: 120 },
  { dataIndex: 'action_side', title: '动作 / 方向', width: 145 },
  { dataIndex: 'price', title: '价格', width: 100 },
  { dataIndex: 'quantity', title: '数量', width: 100 },
  { dataIndex: 'fee', title: '手续费', width: 100 },
  { dataIndex: 'profit_abs', title: 'profit_abs', width: 110 },
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
        : `${value(item.effect?.actual_orders)} / ${value(item.effect?.closed_trades)} · 收益 ${value(item.effect?.realized_profit_abs)}`,
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
          eventText(detail.decision ?? detail.status ?? (kind === 'exit' ? '退出' : '候选')),
          eventText(detail.side, ''),
        ].filter(Boolean).join(' / '),
        price: eventText(eventRecord(detail.actual_fill).price),
        quantity: '—',
        fee: eventText(detail.expected_fee),
        profit_abs: '—',
        reason: eventText(detail.reason ?? event.decision_reason),
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
        price: '—',
        quantity: '—',
        fee: '—',
        profit_abs: '—',
        reason: eventText(event.decision_reason),
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
      reason: eventText(event.reason, decisionReasonForFill(event)),
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
      reason: eventText(event.reason ?? event.exit_reason, decisionReasonForFill(event)),
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

      <div class="grid grid-cols-2 gap-3 lg:grid-cols-7">
        <div
          v-for="item in [
            ['登记版本', ledger.summary.version_count],
            ['实际交易', ledger.summary.actual_trade_count],
            ['实际订单', ledger.summary.actual_order_count],
            ['已平仓', ledger.summary.closed_trade_count],
            ['已部署 ML', ledger.summary.deployed_ml_count],
            ['未归因交易', ledger.summary.unattributed_trade_count],
            ['已实现收益', ledger.summary.realized_profit_abs],
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
      <div
        v-if="ledger.summary.closed_trade_count === 0"
        class="mt-3 rounded-lg bg-orange-50 p-3 text-xs text-orange-700 dark:bg-orange-950/20"
      >
        暂无成熟收益：当前没有已平仓交易，收益、胜率和期望值不作推断。
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

      <div class="mt-5 grid gap-4 lg:grid-cols-2">
        <div>
          <div class="mb-2 text-sm font-medium">实际交易（与订单分开）</div>
          <Table
            :columns="tradeColumns"
            :data-source="ledger.actual_trades"
            :pagination="{ pageSize: 6, hideOnSinglePage: true }"
            :scroll="{ x: 1000 }"
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
          <template v-else-if="column.key === 'current_status'">
            <Tag :color="forwardPaper.summary.data_ready ? 'green' : 'red'">
              {{ forwardPaper.summary.data_ready ? '就绪' : '当前未就绪' }}
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
        接口按事件返回最近记录；决策候选按币种展开。时间均为 UTC，信号 K 线时间与执行 / 成交时间分开显示。滑点和资金费未知。
      </div>

      <div class="mb-4">
        <div class="mb-2 text-sm font-medium">最近决策</div>
        <Table
          :columns="forwardDecisionColumns"
          :data-source="forwardDecisionRows"
          :pagination="{ pageSize: 10, hideOnSinglePage: true }"
          :scroll="{ x: 1200 }"
          size="small"
        />
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
        />
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
        />
        <Empty
          v-if="!forwardCloseRows.length"
          :image="Empty.PRESENTED_IMAGE_SIMPLE"
          description="暂无匹配的平仓记录"
        />
      </div>
    </template>
  </Card>
</template>

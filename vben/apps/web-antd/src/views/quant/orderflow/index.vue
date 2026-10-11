<script lang="ts" setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';

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
  Tag,
} from 'ant-design-vue';

import {
  getOrderflowSurge,
  getOrderflowSurgeStats,
  getOrderflowSurges,
} from '#/api/database';
import type {
  OrderflowSurgeDetail,
  OrderflowSurgeObservation,
  OrderflowSurgeRevision,
  OrderflowSurgeStats,
  OrderflowSurgeStatsBucket,
} from '#/api/database';
import { errText } from '#/api/freqtrade/client';

const rows = ref<OrderflowSurgeObservation[]>([]);
const stats = ref<OrderflowSurgeStats | null>(null);
const selectedPair = ref('all');
const selectedStatus = ref('all');
const loading = ref(false);
const error = ref('');
const refreshedAt = ref<DatabaseTimestamp | null>(null);
const detailOpen = ref(false);
const detailLoading = ref(false);
const detailError = ref('');
const detail = ref<OrderflowSurgeDetail | null>(null);
let refreshController: AbortController | undefined;
let detailController: AbortController | undefined;
let refreshSequence = 0;
let detailSequence = 0;

type DatabaseTimestamp = null | number | string;

const pairOptions = [
  { label: '全部交易对', value: 'all' },
  { label: 'BTC/USDT:USDT', value: 'BTC/USDT:USDT' },
  { label: 'ETH/USDT:USDT', value: 'ETH/USDT:USDT' },
];
const statusOptions = [
  { label: '全部状态', value: 'all' },
  { label: '未成熟（unknown）', value: 'unknown' },
  { label: '已成熟', value: 'matured' },
  { label: '无效', value: 'invalid' },
];

const columns = [
  { key: 'timestamp', title: '观测时间', width: 170 },
  { key: 'threshold', title: '阈值档', width: 90 },
  { key: 'ratio', title: '量比', width: 90, align: 'right' as const },
  { key: 'quote', title: '成交额 / 基线', width: 170, align: 'right' as const },
  { key: 'trades', title: '成交笔数', width: 100, align: 'right' as const },
  {
    key: 'imbalance',
    title: '主动买卖失衡',
    width: 130,
    align: 'right' as const,
  },
  { key: 'status', title: '状态', width: 105 },
  { key: 'action', title: '详情', width: 80 },
];

type LooseRecord = Record<string, unknown>;

function valueOf(row: LooseRecord, key: string): unknown {
  const nested = row.observation;
  if (nested && typeof nested === 'object') {
    return (nested as LooseRecord)[key] ?? row[key];
  }
  return row[key];
}

function resultOf(row: LooseRecord): LooseRecord {
  const result = row.result;
  return result && typeof result === 'object' && !Array.isArray(result)
    ? (result as LooseRecord)
    : {};
}

function asNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function formatNumber(value: unknown, digits = 2) {
  const number = asNumber(value);
  if (number === null) return '—';
  return number.toLocaleString('zh-CN', { maximumFractionDigits: digits });
}

function formatPercent(value: unknown, digits = 3) {
  const number = asNumber(value);
  if (number === null) return '—';
  return `${(number * 100).toFixed(digits)}%`;
}

function timestampValue(value: unknown) {
  if (value === null || value === undefined || value === '') return Number.NaN;
  const text = String(value);
  if (/^\d+(?:\.\d+)?$/.test(text)) return Number(text);
  const iso = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text)
    ? text
    : `${text.replace(' ', 'T')}Z`;
  return new Date(iso).getTime();
}

function localTime(value: unknown) {
  const timestamp = timestampValue(value);
  return Number.isFinite(timestamp)
    ? new Date(timestamp).toLocaleString('zh-CN', { hour12: false })
    : '—';
}

function pairOf(row: LooseRecord) {
  return String(valueOf(row, 'pair') ?? row.pair ?? '').toUpperCase();
}

function statusOf(row: LooseRecord) {
  return String(
    row.status ?? valueOf(row, 'status') ?? 'unknown',
  ).toLowerCase();
}

function statusColor(status: string) {
  return status === 'matured'
    ? 'success'
    : status === 'invalid'
      ? 'error'
      : 'warning';
}

function statusLabel(status: string) {
  return status === 'matured'
    ? '已成熟'
    : status === 'invalid'
      ? '无效'
      : '未成熟';
}

function maturityProgress(row: LooseRecord) {
  const completed = row.completed_horizons;
  const required = row.required_horizons;
  return Array.isArray(completed) && Array.isArray(required) && required.length
    ? `${completed.length}/${required.length} 窗口`
    : '';
}

function rowKey(row: LooseRecord) {
  return String(row.event_id ?? `${pairOf(row)}:${valueOf(row, 'timestamp')}`);
}

const visibleRows = computed(() =>
  rows.value.filter((row) => {
    const pairOk =
      selectedPair.value === 'all' || pairOf(row) === selectedPair.value;
    const statusOk =
      selectedStatus.value === 'all' || statusOf(row) === selectedStatus.value;
    return pairOk && statusOk;
  }),
);
const btcRows = computed(() =>
  visibleRows.value.filter((row) => pairOf(row).startsWith('BTC/')),
);
const ethRows = computed(() =>
  visibleRows.value.filter((row) => pairOf(row).startsWith('ETH/')),
);

const maturityRows = computed(() => {
  const results = new Map<string, LooseRecord>();
  const events: LooseRecord[] = [...(detail.value?.events ?? [])].sort(
    (left, right) =>
      (asNumber(left.revision_no ?? left.sequence_id) ?? 0) -
      (asNumber(right.revision_no ?? right.sequence_id) ?? 0),
  );
  if (detail.value) events.unshift(detail.value.observation);
  for (const event of events) {
    const payload = event.payload;
    const payloadRecord =
      payload && typeof payload === 'object' && !Array.isArray(payload)
        ? (payload as LooseRecord)
        : {};
    const directResult = resultOf(event);
    const result = Object.keys(directResult).length
      ? directResult
      : Object.keys(resultOf(payloadRecord)).length
        ? resultOf(payloadRecord)
        : payloadRecord;
    const horizon = result.horizon;
    const status = String(result.status ?? event.status);
    if (
      typeof horizon === 'string' &&
      ['matured', 'invalid'].includes(status)
    ) {
      results.set(horizon, { ...result, status });
    }
  }
  return ['5m', '30m', '1h', '4h'].map((horizon) => ({
    ...(results.get(horizon)?.status === 'invalid' ? {} : results.get(horizon)),
    horizon,
    status: results.has(horizon)
      ? String(results.get(horizon)?.status ?? 'matured')
      : 'unknown',
  }));
});

function bucketFor(pair: string): OrderflowSurgeStatsBucket {
  const value = stats.value;
  if (!value) return {};
  const pairs = value.pairs;
  if (Array.isArray(pairs)) {
    return (
      pairs.find((item) => String(item.pair ?? '').toUpperCase() === pair) ?? {}
    );
  }
  if (pairs && typeof pairs === 'object' && !Array.isArray(pairs)) {
    return pairs[pair] ?? {};
  }
  const byPair = value.by_pair;
  if (Array.isArray(byPair)) {
    return (
      byPair.find((item) => String(item.pair ?? '').toUpperCase() === pair) ??
      {}
    );
  }
  if (byPair && typeof byPair === 'object') return byPair[pair] ?? {};
  return {};
}

function horizonStats(pair: string) {
  const bucket = bucketFor(pair);
  return ['5m', '30m', '1h', '4h'].map((horizon) => ({
    ...bucket.horizons?.[horizon],
    horizon,
  }));
}

function warmupFor(pair: string) {
  return stats.value?.warmup?.[pair];
}

function warmupDelayed(pair: string) {
  const timestamp = timestampValue(warmupFor(pair)?.last_timestamp);
  const now = timestampValue(refreshedAt.value);
  // Candle timestamps name the opening time. The last close remains current
  // until the following 5m candle closes, with 90s for its delivery.
  return Number.isFinite(timestamp) && Number.isFinite(now)
    && now > timestamp + 2 * 300_000 + 90_000;
}

function warmupEstimate(pair: string) {
  const warmup = warmupFor(pair);
  if (!warmup || warmup.ready !== false) return '';
  const valid = asNumber(warmup.valid_bars);
  const required = asNumber(warmup.required_bars);
  if (valid === null || required === null) return '';
  const remaining = Math.max(0, required - valid);
  if (warmupDelayed(pair)) {
    return `还需 ${remaining} 根；收盘数据延迟，恢复连续采集后才能估算完成时间`;
  }
  const timestamp = timestampValue(warmup.last_timestamp);
  return Number.isFinite(timestamp)
    ? `还需 ${remaining} 根；连续采集无缺口时预计 ${localTime(timestamp + (remaining + 1) * 300_000)} 完成`
    : `还需 ${remaining} 根连续收盘数据`;
}

function emptyReason(pair: string) {
  if (loading.value && !stats.value) return '正在读取观测数据';
  if (error.value && !stats.value) return '数据读取失败，请重试';
  const warmup = warmupFor(pair);
  if (warmupDelayed(pair)) return '收盘数据延迟，等待采集恢复';
  if (warmup?.ready === false)
    return `实时基线预热中（${formatNumber(warmup.valid_bars, 0)} / ${formatNumber(warmup.required_bars, 0)} 根），就绪后开始记录突增`;
  return '当前筛选下暂无突增事件，继续实时观测';
}

function statValue(bucket: OrderflowSurgeStatsBucket, ...keys: string[]) {
  for (const key of keys) {
    if (bucket[key] !== undefined && bucket[key] !== null) return bucket[key];
  }
  return null;
}

function tableRows(value: OrderflowSurgeObservation[]) {
  return value.map((row) => ({ ...row, _key: rowKey(row) }));
}

function openDetail(row: LooseRecord) {
  detailController?.abort();
  detailController = new AbortController();
  const sequence = ++detailSequence;
  detailOpen.value = true;
  detailLoading.value = true;
  detailError.value = '';
  detail.value = null;
  void getOrderflowSurge(rowKey(row), detailController.signal)
    .then((result) => {
      if (sequence !== detailSequence) return;
      detail.value = result;
    })
    .catch((reason) => {
      if (sequence !== detailSequence) return;
      detailError.value = errText(reason);
    })
    .finally(() => {
      if (sequence !== detailSequence) return;
      detailLoading.value = false;
    });
}

async function refresh() {
  refreshController?.abort();
  refreshController = new AbortController();
  const sequence = ++refreshSequence;
  loading.value = true;
  error.value = '';
  try {
    const query = {
      ...(selectedPair.value === 'all' ? {} : { pair: selectedPair.value }),
      ...(selectedStatus.value === 'all'
        ? {}
        : { status: selectedStatus.value }),
      limit: 100,
    };
    const [listResult, statsResult] = await Promise.allSettled([
      getOrderflowSurges(query, refreshController.signal),
      getOrderflowSurgeStats({}, refreshController.signal),
    ]);
    if (sequence !== refreshSequence) return;
    if (listResult.status === 'rejected') throw listResult.reason;
    rows.value = listResult.value.items;
    if (statsResult.status === 'fulfilled') {
      stats.value = statsResult.value;
    } else {
      stats.value = null;
      error.value = `观测已加载，统计暂不可用：${errText(statsResult.reason)}`;
    }
    refreshedAt.value = Date.now();
  } catch (reason) {
    if (sequence !== refreshSequence) return;
    error.value = errText(reason);
  } finally {
    if (sequence !== refreshSequence) return;
    loading.value = false;
  }
}

let timer: ReturnType<typeof setInterval> | undefined;
onMounted(() => {
  void refresh();
  timer = setInterval(() => {
    if (!loading.value) void refresh();
  }, 30_000);
});
onUnmounted(() => {
  if (timer) clearInterval(timer);
  ++refreshSequence;
  ++detailSequence;
  refreshController?.abort();
  detailController?.abort();
});
</script>

<template>
  <div class="space-y-4 p-4">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <div class="text-lg font-semibold">订单流突增</div>
        <div class="mt-1 text-xs text-muted-foreground">
          BTC 与 ETH 的 5 分钟成交额突增长期观测 · 每 30 秒刷新
        </div>
      </div>
      <div class="flex items-center gap-3">
        <span class="text-xs text-muted-foreground"
          >更新 {{ localTime(refreshedAt) }}</span
        >
        <Button :loading="loading" @click="refresh">刷新</Button>
      </div>
    </div>

    <Alert
      v-if="error"
      :description="error"
      message="订单流突增数据读取失败"
      show-icon
      type="error"
    />

    <Card :bordered="false" title="统计概览">
      <div class="grid gap-3 md:grid-cols-2">
        <Card
          v-for="pair in ['BTC/USDT:USDT', 'ETH/USDT:USDT']"
          :key="pair"
          size="small"
        >
          <template #title>{{
            pair.startsWith('BTC') ? 'BTC' : 'ETH'
          }}</template>
          <div v-if="warmupFor(pair)" class="mb-3 text-xs text-muted-foreground">
            实时基线 {{ formatNumber(warmupFor(pair)?.valid_bars, 0) }} /
            {{ formatNumber(warmupFor(pair)?.required_bars, 0) }} 根 ·
            {{ warmupFor(pair)?.ready ? '就绪，持续观测' : '预热中，暂不生成事件' }}
            <div class="mt-1">最近收盘根 {{ localTime(warmupFor(pair)?.last_timestamp) }}</div>
            <div v-if="warmupEstimate(pair)" class="mt-1">{{ warmupEstimate(pair) }}</div>
            <div class="mt-1">基线需连续 24 小时；就绪后仅记录成交额达到前 288 根中位数 2 倍的新事件。</div>
          </div>
          <div class="grid grid-cols-2 gap-3 text-sm xl:grid-cols-5">
            <div>
              <div class="text-xs text-muted-foreground">
                {{ bucketFor(pair).statistics_horizon ?? '1h' }} 毛收益均值
              </div>
              <div class="mt-1 font-semibold">
                {{
                  formatPercent(
                    statValue(
                      bucketFor(pair),
                      'gross_return_mean',
                      'gross_mean',
                      'mean_return',
                    ),
                  )
                }}
              </div>
              <div class="mt-1 text-xs text-muted-foreground">
                成熟样本 {{ formatNumber(bucketFor(pair).sample_count, 0) }}
              </div>
            </div>
            <div>
              <div class="text-xs text-muted-foreground">所需费率</div>
              <div class="mt-1 font-semibold">
                {{
                  formatPercent(
                    statValue(
                      bucketFor(pair),
                      'required_fee_rate',
                      'required_fee',
                    ),
                  )
                }}
              </div>
            </div>
            <div>
              <div class="text-xs text-muted-foreground">实际费率</div>
              <div class="mt-1 font-semibold">
                {{
                  formatPercent(
                    statValue(bucketFor(pair), 'actual_fee_rate', 'fee_rate'),
                  )
                }}
              </div>
            </div>
            <div>
              <div class="text-xs text-muted-foreground">样本 / 成熟</div>
              <div class="mt-1 font-semibold">
                {{ formatNumber(statValue(bucketFor(pair), 'total')) }} /
                {{
                  formatNumber(
                    statValue(
                      bucketFor(pair),
                      'matured',
                      'mature',
                      'mature_count',
                    ),
                  )
                }}
              </div>
            </div>
            <div>
              <div class="text-xs text-muted-foreground">未成熟</div>
              <div class="mt-1 font-semibold">
                {{ formatNumber(statValue(bucketFor(pair), 'unknown')) }}
              </div>
            </div>
          </div>
          <Table
            class="mt-4"
            :columns="[
              { key: 'horizon', title: '窗口', width: 65 },
              { key: 'sample_count', title: '成熟样本', width: 85 },
              { key: 'gross_return_mean', title: '毛收益均值', width: 110 },
              { key: 'win_rate', title: '正收益比例', width: 110 },
              { key: 'mfe_mean', title: 'MFE 均值', width: 100 },
              { key: 'mae_mean', title: 'MAE 均值', width: 100 },
            ]"
            :data-source="horizonStats(pair)"
            :pagination="false"
            row-key="horizon"
            :scroll="{ x: 570 }"
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'horizon'">{{
                record.horizon
              }}</template>
              <template v-else-if="column.key === 'sample_count'">{{
                formatNumber(record.sample_count, 0)
              }}</template>
              <template v-else>{{
                formatPercent(record[String(column.key)])
              }}</template>
            </template>
          </Table>
        </Card>
      </div>
      <Alert
        class="mt-3"
        description="观测在全部窗口完成前保持 unknown，各窗口仅统计其已成熟结果，unknown 与无效结果均不计入表现统计。— 表示尚未提供该指标。主动买卖失衡来自已成交订单；盘口仅为 depth20 部分快照，不包含完整挂单流。毛收益未扣除滑点、资金费率或盘口重建成本；实际交易费率、限价单成交率与逆向选择仍未知。"
        message="数据限制"
        show-icon
        type="warning"
      />
    </Card>

    <Card :bordered="false" title="突增观测">
      <template #extra>
        <div class="flex flex-wrap gap-2">
          <Select
            v-model:value="selectedPair"
            :options="pairOptions"
            class="!w-44"
            @change="refresh"
          />
          <Select
            v-model:value="selectedStatus"
            :options="statusOptions"
            class="!w-44"
            @change="refresh"
          />
        </div>
      </template>
      <Spin :spinning="loading">
        <div class="space-y-5">
          <section
            v-for="section in [
              { label: 'BTC', items: btcRows },
              { label: 'ETH', items: ethRows },
            ]"
            :key="section.label"
          >
            <div class="mb-2 text-sm font-medium">
              {{ section.label }}
              <span class="text-xs text-muted-foreground"
                >（{{ section.items.length }} 条）</span
              >
            </div>
            <Table
              :columns="columns"
              :data-source="tableRows(section.items)"
              :pagination="{ pageSize: 10, showSizeChanger: true }"
              :row-key="
                (record: OrderflowSurgeObservation & { _key: string }) =>
                  record._key
              "
              :scroll="{ x: 1040 }"
              size="small"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'timestamp'">{{
                  localTime(valueOf(record, 'timestamp'))
                }}</template>
                <template v-else-if="column.key === 'threshold'">{{
                  valueOf(record, 'threshold_level') ??
                  valueOf(record, 'threshold_band') ??
                  (asNumber(
                    valueOf(record, 'threshold_multiple') ??
                      valueOf(record, 'threshold'),
                  ) === null
                    ? '—'
                    : `${formatNumber(valueOf(record, 'threshold_multiple') ?? valueOf(record, 'threshold'))}x`)
                }}</template>
                <template v-else-if="column.key === 'ratio'"
                  >{{
                    formatNumber(valueOf(record, 'volume_ratio'), 2)
                  }}x</template
                >
                <template v-else-if="column.key === 'quote'"
                  >{{ formatNumber(valueOf(record, 'quote_volume')) }} /
                  {{
                    formatNumber(valueOf(record, 'baseline_quote_volume'))
                  }}</template
                >
                <template v-else-if="column.key === 'trades'">{{
                  formatNumber(valueOf(record, 'num_trades'), 0)
                }}</template>
                <template v-else-if="column.key === 'imbalance'">{{
                  formatNumber(valueOf(record, 'flow_imbal'), 4)
                }}</template>
                <template v-else-if="column.key === 'status'"
                  ><Tag :color="statusColor(statusOf(record))">{{
                    statusLabel(statusOf(record))
                  }}</Tag>
                  <div class="mt-1 text-xs text-muted-foreground">
                    {{ maturityProgress(record) }}
                  </div></template
                >
                <template v-else-if="column.key === 'action'"
                  ><Button type="link" size="small" @click="openDetail(record)"
                    >详情</Button
                  ></template
                >
              </template>
              <template #emptyText
                ><Empty :description="emptyReason(`${section.label}/USDT:USDT`)"
              /></template>
            </Table>
          </section>
        </div>
      </Spin>
    </Card>

    <Drawer v-model:open="detailOpen" title="订单流突增详情" :width="560">
      <Spin :spinning="detailLoading">
        <Alert
          v-if="detailError"
          :description="detailError"
          message="详情读取失败"
          show-icon
          type="error"
        />
        <Empty v-else-if="!detail" description="正在读取详情" />
        <template v-else>
          <Descriptions bordered :column="1" size="small">
            <DescriptionsItem label="事件 ID">{{
              detail.observation.event_id
            }}</DescriptionsItem>
            <DescriptionsItem label="交易对">{{
              pairOf(detail.observation)
            }}</DescriptionsItem>
            <DescriptionsItem label="观测时间">{{
              localTime(valueOf(detail.observation, 'timestamp'))
            }}</DescriptionsItem>
            <DescriptionsItem label="量比"
              >{{
                formatNumber(valueOf(detail.observation, 'volume_ratio'), 2)
              }}x</DescriptionsItem
            >
            <DescriptionsItem label="状态"
              ><Tag :color="statusColor(statusOf(detail.observation))">{{
                statusLabel(statusOf(detail.observation))
              }}</Tag>
              {{ maturityProgress(detail.observation) }}</DescriptionsItem
            >
            <DescriptionsItem label="方法版本">{{
              detail.observation.method_version ?? '—'
            }}</DescriptionsItem>
            <DescriptionsItem label="数据指纹">{{
              detail.observation.data_fingerprint ?? '—'
            }}</DescriptionsItem>
          </Descriptions>
          <div class="mb-2 mt-5 text-sm font-medium">成熟结果</div>
          <Table
            :columns="[
              { key: 'horizon', title: '窗口', width: 70 },
              { key: 'status', title: '状态', width: 80 },
              { key: 'return', title: '毛收益', width: 90 },
              { key: 'mfe', title: 'MFE', width: 90 },
              { key: 'mae', title: 'MAE', width: 90 },
              { key: 'maturity_timestamp', title: '成熟时间' },
            ]"
            :data-source="maturityRows"
            :pagination="false"
            row-key="horizon"
            :scroll="{ x: 590 }"
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'status'"
                ><Tag :color="statusColor(record.status)">{{
                  statusLabel(record.status)
                }}</Tag></template
              >
              <template
                v-else-if="
                  ['return', 'mfe', 'mae'].includes(String(column.key))
                "
                >{{ formatPercent(record[String(column.key)]) }}</template
              >
              <template v-else-if="column.key === 'maturity_timestamp'">{{
                localTime(record.maturity_timestamp)
              }}</template>
              <template v-else>{{
                record[String(column.key)] ?? '—'
              }}</template>
            </template>
            <template #emptyText
              >暂无成熟结果（unknown 不计入表现统计）</template
            >
          </Table>
          <div class="mb-2 mt-5 text-sm font-medium">事件修订记录</div>
          <Table
            :columns="[
              { key: 'created_at', title: '时间', width: 170 },
              { key: 'event_type', title: '事件' },
              { key: 'sequence_id', title: '序号', width: 70 },
            ]"
            :data-source="detail.events ?? []"
            :pagination="false"
            :row-key="
              (record: OrderflowSurgeRevision, index?: number) =>
                String(
                  record.revision_id ??
                    `${record.event_id ?? 'revision'}:${record.revision_no ?? index ?? 0}`,
                )
            "
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'created_at'">{{
                localTime(record.created_at)
              }}</template>
              <template v-else-if="column.key === 'event_type'">{{
                record.event_type ?? '—'
              }}</template>
              <template v-else>{{
                record.revision_no ?? record.sequence_id ?? '—'
              }}</template>
            </template>
            <template #emptyText>暂无修订记录</template>
          </Table>
        </template>
      </Spin>
    </Drawer>
  </div>
</template>

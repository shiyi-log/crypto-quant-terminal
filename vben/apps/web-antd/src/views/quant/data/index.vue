<script lang="ts" setup>
import type {
  DatabaseDataset,
  DatabaseMarketQuery,
  DatabaseOrderbook,
  DatabaseSource,
  DatabaseStatus,
  DatabaseTick,
} from '#/api/database';

import { computed, onMounted, onUnmounted, ref } from 'vue';

import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Select,
  Spin,
  Table,
  Tag,
} from 'ant-design-vue';

import {
  getDatabaseOrderbooks,
  getDatabaseStatus,
  getDatabaseTicks,
} from '#/api/database';
import { errText } from '#/api/freqtrade/client';

/** 数据中心只读取已经入库的数据，采集与同步由后端管理。 */
const status = ref<DatabaseStatus | null>(null);
const ticks = ref<DatabaseTick[]>([]);
const orderbooks = ref<DatabaseOrderbook[]>([]);
const loading = ref(false);
const statusError = ref('');
const ticksError = ref('');
const orderbooksError = ref('');
const refreshedAt = ref<number | null>(null);
const marketRefreshedAt = ref<number | null>(null);
const sourceSearch = ref('');
const datasetSearch = ref('');
const sourceErrorsOnly = ref(false);
const exchange = ref('binance');
const market = ref('futures');
const pair = ref('BTC/USDT:USDT');
const displayedQuery = ref<DatabaseMarketQuery | null>(null);

const metricDefinitions = [
  { key: 'candles', label: 'K 线', description: '历史行情与实时行情' },
  { key: 'ticks', label: '逐笔成交', description: '交易所成交事件' },
  { key: 'orderbooks', label: '盘口快照', description: '买卖档位与数量' },
  {
    key: 'series_points',
    label: '时序指标',
    description: '资金费率、持仓量等',
  },
  { key: 'documents', label: '业务快照', description: '研究与运维状态' },
  { key: 'events', label: '事件记录', description: '运行日志与历史事件' },
  {
    key: 'trade_records',
    label: '交易记录',
    description: '持仓、订单与钱包记录',
  },
  { key: 'users', label: '用户', description: '认证账号' },
  { key: 'artifacts', label: '数据资产', description: '模型与回测产物索引' },
] as const;

const metrics = computed(() =>
  metricDefinitions.map((definition) => ({
    ...definition,
    value: status.value?.counts?.[definition.key],
  })),
);
const sources = computed(() => {
  const keyword = sourceSearch.value.trim().toLowerCase();
  return (status.value?.sources ?? []).filter((source) => {
    const matches = [source.key, source.path, source.kind]
      .join(' ')
      .toLowerCase()
      .includes(keyword);
    return matches && (!sourceErrorsOnly.value || Boolean(source.error));
  });
});
const datasets = computed(() => {
  const keyword = datasetSearch.value.trim().toLowerCase();
  return (status.value?.datasets ?? []).filter((dataset) =>
    [
      dataset.exchange,
      dataset.market,
      dataset.pair,
      dataset.timeframe,
      dataset.candle_type,
    ]
      .join(' ')
      .toLowerCase()
      .includes(keyword),
  );
});
const datasetRows = computed(() =>
  datasets.value.reduce((sum, dataset) => sum + (Number(dataset.rows) || 0), 0),
);
const sourceErrorCount = computed(
  () => (status.value?.sources ?? []).filter((source) => source.error).length,
);
const syncErrors = computed(() => {
  const errors: unknown = status.value?.sync?.errors;
  if (!errors) return [];
  return (Array.isArray(errors) ? errors : [errors]).map((error) => {
    if (typeof error === 'string') return error;
    if (error && typeof error === 'object') {
      const value = error as Record<string, unknown>;
      const detail = value.error ?? value.message ?? JSON.stringify(error);
      return [value.key ?? value.path, detail].filter(Boolean).join('：');
    }
    return String(error);
  });
});
const initialImport = computed(() => {
  if (!status.value) return false;
  const keys = [
    'candles',
    'ticks',
    'orderbooks',
    'series_points',
    'documents',
    'events',
    'trade_records',
  ] as const;
  return keys.every((key) => Number(status.value?.counts?.[key] ?? 0) === 0);
});
const collector = computed<Record<string, unknown> | null>(() => {
  const value: unknown = status.value?.collector;
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
});
const collectorError = computed(() => {
  const value = collector.value?.error ?? collector.value?.last_error;
  if (!value) return '';
  return typeof value === 'string' ? value : JSON.stringify(value);
});
const latestOrderbook = computed(() => {
  // 同毫秒的多个快照再按更新编号比较，编号使用 BigInt 保留完整精度。
  return [...orderbooks.value].sort((left, right) => {
    const byTime =
      timestampValue(right.timestamp) - timestampValue(left.timestamp);
    if (byTime) return byTime;
    const leftId = String(left.last_update_id ?? left.update_id ?? '');
    const rightId = String(right.last_update_id ?? right.update_id ?? '');
    if (/^\d+$/.test(leftId) && /^\d+$/.test(rightId)) {
      const leftNumber = BigInt(leftId);
      const rightNumber = BigInt(rightId);
      return rightNumber > leftNumber ? 1 : rightNumber < leftNumber ? -1 : 0;
    }
    return rightId.localeCompare(leftId);
  })[0];
});
const bidLevels = computed(() =>
  [...(latestOrderbook.value?.bids ?? [])]
    .sort((left, right) => Number(right[0]) - Number(left[0]))
    .slice(0, 10)
    .map((level, index) => ({
      level: index + 1,
      price: level[0],
      quantity: level[1],
    })),
);
const askLevels = computed(() =>
  [...(latestOrderbook.value?.asks ?? [])]
    .sort((left, right) => Number(left[0]) - Number(right[0]))
    .slice(0, 10)
    .map((level, index) => ({
      level: index + 1,
      price: level[0],
      quantity: level[1],
    })),
);
const displayedMarket = computed(() => {
  const query = displayedQuery.value;
  return query
    ? `${query.exchange} · ${query.market} · ${query.pair}`
    : '尚未查询';
});

const sourceColumns = [
  { title: '同步源', key: 'source', width: 360 },
  { title: '类型', dataIndex: 'kind', width: 120 },
  { title: '记录数', key: 'rows', width: 110 },
  { title: '更新时间', key: 'updated_at', width: 190 },
  { title: '同步结果', key: 'result', width: 300 },
];
const datasetColumns = [
  { title: '交易所', dataIndex: 'exchange', width: 100 },
  { title: '市场', dataIndex: 'market', width: 100 },
  { title: '交易对', dataIndex: 'pair', width: 180 },
  { title: '周期', dataIndex: 'timeframe', width: 90 },
  { title: '行情类型', dataIndex: 'candle_type', width: 120 },
  { title: '记录数', key: 'rows', width: 110 },
  { title: '首条时间', key: 'first_timestamp', width: 190 },
  { title: '末条时间', key: 'last_timestamp', width: 190 },
];
const tickColumns = [
  { title: '时间', key: 'timestamp', width: 175 },
  { title: '成交 ID', dataIndex: 'trade_id', width: 130 },
  { title: '价格', key: 'price', width: 110 },
  { title: '数量', key: 'quantity', width: 110 },
  { title: '主动方向', key: 'side', width: 100 },
];
const orderbookColumns = [
  { title: '档位', dataIndex: 'level', width: 70 },
  { title: '价格', key: 'price' },
  { title: '数量', key: 'quantity' },
];
const pagination = {
  pageSize: 10,
  showSizeChanger: true,
  pageSizeOptions: ['10', '25', '50'],
};

function formatCount(value: number | undefined) {
  return value === undefined ? '—' : Number(value).toLocaleString('zh-CN');
}

function formatBytes(value: number | undefined) {
  if (value === undefined || !Number.isFinite(value)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const level = Math.min(
    Math.floor(Math.log(Math.max(value, 1)) / Math.log(1024)),
    units.length - 1,
  );
  return `${(value / 1024 ** level).toFixed(level === 0 ? 0 : 2)} ${units[level]}`;
}

function formatNumber(value: number | string | undefined) {
  const numeric = Number(value);
  if (value === undefined || !Number.isFinite(numeric)) return '—';
  // 价量可能以精确小数字符串返回，展示时不转成浮点数或截断小数位。
  const text = String(value);
  if (!/^-?\d+(?:\.\d+)?$/.test(text)) return text;
  const [integer = '', fraction] = text.split('.');
  return `${integer.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}${fraction === undefined ? '' : `.${fraction}`}`;
}

/** 后端没有时区后缀的时间按 UTC 处理，再交给浏览器转为本地时间。 */
function timestampValue(value: unknown): number {
  if (value === null || value === undefined || value === '') return Number.NaN;
  if (typeof value === 'number' || /^\d+(?:\.\d+)?$/.test(String(value))) {
    // 数值时间戳严格遵循数据库的毫秒约定，包括 Unix 纪元附近的测试数据。
    return Number(value);
  }
  const text = String(value).trim();
  const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text);
  return new Date(zoned ? text : `${text.replace(' ', 'T')}Z`).getTime();
}

function localTime(value: unknown) {
  const timestamp = timestampValue(value);
  return Number.isFinite(timestamp)
    ? new Date(timestamp).toLocaleString('zh-CN', { hour12: false })
    : '—';
}

const sourceKey = (source: DatabaseSource) =>
  `${source.key}:${source.path ?? ''}`;
const datasetKey = (dataset: DatabaseDataset) =>
  [
    dataset.exchange,
    dataset.market,
    dataset.pair,
    dataset.timeframe,
    dataset.candle_type,
  ].join(':');
const tickKey = (tick: DatabaseTick) => `${tick.trade_id}:${tick.timestamp}`;
const buyerMaker = (maker?: boolean, legacyMaker?: boolean) =>
  maker ?? legacyMaker;

let active = false;
let timer: ReturnType<typeof setTimeout> | undefined;
let controller: AbortController | undefined;

async function refresh() {
  // 所有请求共用一次刷新生命周期，慢请求不会与下一轮轮询重叠。
  if (!active || loading.value) return;
  if (timer !== undefined) clearTimeout(timer);
  loading.value = true;
  const requestController = new AbortController();
  controller = requestController;
  const query: DatabaseMarketQuery = {
    exchange: exchange.value,
    market: market.value,
    pair: pair.value.trim(),
    limit: 50,
  };
  try {
    const responses = await Promise.allSettled([
      getDatabaseStatus(requestController.signal),
      query.pair
        ? getDatabaseTicks(query, requestController.signal)
        : Promise.reject(new Error('请输入交易对')),
      query.pair
        ? getDatabaseOrderbooks(query, requestController.signal)
        : Promise.reject(new Error('请输入交易对')),
    ]);
    // 卸载时取消请求，同时阻止已经完成的请求继续修改页面状态。
    if (!active || requestController.signal.aborted) return;
    const [statusResponse, ticksResponse, orderbooksResponse] = responses;
    if (statusResponse.status === 'fulfilled') {
      status.value = statusResponse.value;
      statusError.value = '';
      refreshedAt.value = Date.now();
    } else {
      statusError.value = errText(statusResponse.reason);
    }
    const changedMarket =
      displayedQuery.value?.exchange !== query.exchange ||
      displayedQuery.value?.market !== query.market ||
      displayedQuery.value?.pair !== query.pair;
    if (changedMarket) {
      ticks.value = [];
      orderbooks.value = [];
      marketRefreshedAt.value = null;
    }
    displayedQuery.value = query;
    if (ticksResponse.status === 'fulfilled') {
      ticks.value = ticksResponse.value.items;
      ticksError.value = '';
    } else {
      ticksError.value = errText(ticksResponse.reason);
    }
    if (orderbooksResponse.status === 'fulfilled') {
      orderbooks.value = orderbooksResponse.value.items;
      orderbooksError.value = '';
    } else {
      orderbooksError.value = errText(orderbooksResponse.reason);
    }
    if (
      ticksResponse.status === 'fulfilled' ||
      orderbooksResponse.status === 'fulfilled'
    ) {
      marketRefreshedAt.value = Date.now();
    }
  } finally {
    if (controller === requestController) controller = undefined;
    loading.value = false;
    if (active) timer = setTimeout(refresh, 15_000);
  }
}

onMounted(() => {
  active = true;
  void refresh();
});
onUnmounted(() => {
  active = false;
  if (timer !== undefined) clearTimeout(timer);
  controller?.abort();
});
</script>

<template>
  <div class="space-y-4 p-4">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <div class="text-lg font-semibold">数据中心</div>
        <div class="mt-1 text-xs text-muted-foreground">
          查看入库覆盖、同步结果和最新市场数据 · 每 15 秒自动刷新
        </div>
      </div>
      <div class="flex items-center gap-3">
        <span class="text-xs text-muted-foreground">
          页面更新 {{ localTime(refreshedAt) }}
        </span>
        <Button :loading="loading" @click="refresh">刷新</Button>
      </div>
    </div>

    <Alert
      v-if="statusError"
      :description="statusError"
      :message="
        status
          ? '数据库状态刷新失败，以下保留上次成功数据'
          : '数据库状态读取失败'
      "
      show-icon
      type="error"
    />
    <Spin :spinning="loading && !status">
      <Card v-if="!status" :bordered="false">
        <Empty
          :description="
            loading
              ? '正在读取数据库状态…'
              : '暂时无法读取数据库状态，请检查服务后刷新'
          "
        />
      </Card>
      <template v-else>
        <Card :bordered="false" title="数据库概览">
          <template #extra>
            <Tag color="blue">{{ status.engine ?? '数据库' }}</Tag>
            <Tag v-if="status.sync?.running" color="processing">同步中</Tag>
            <Tag
              v-else-if="sourceErrorCount || syncErrors.length"
              color="warning"
              >同步需关注</Tag
            >
            <Tag v-else-if="status.sync?.last_success_at" color="success"
              >同步正常</Tag
            >
            <Tag v-else>等待首次同步</Tag>
          </template>
          <div class="grid gap-3 text-sm sm:grid-cols-2 xl:grid-cols-4">
            <div>
              <div class="text-xs text-muted-foreground">数据库</div>
              <div class="mt-1 break-all font-mono">
                {{ status.database?.name ?? status.database?.path ?? '—' }}
              </div>
            </div>
            <div>
              <div class="text-xs text-muted-foreground">
                占用空间 / 结构版本
              </div>
              <div class="mt-1">
                {{ formatBytes(status.database?.size_bytes) }} /
                {{ status.database?.schema_version ?? '—' }}
              </div>
            </div>
            <div>
              <div class="text-xs text-muted-foreground">最近同步尝试</div>
              <div class="mt-1">{{ localTime(status.sync?.last_at) }}</div>
            </div>
            <div>
              <div class="text-xs text-muted-foreground">最近同步成功</div>
              <div class="mt-1">
                {{ localTime(status.sync?.last_success_at) }}
              </div>
            </div>
          </div>
          <Alert
            v-if="initialImport"
            class="mt-4"
            description="数据库已连接，目前还没有业务数据。首轮导入或实时采集完成后，这里的统计和表格会自动更新。"
            message="等待初始数据入库"
            show-icon
            type="info"
          />
          <Alert
            v-if="syncErrors.length"
            class="mt-4"
            message="同步错误"
            show-icon
            type="error"
          >
            <template #description>
              <ul class="list-inside list-disc space-y-1 break-words">
                <li v-for="(error, index) in syncErrors" :key="index">
                  {{ error }}
                </li>
              </ul>
            </template>
          </Alert>
        </Card>

        <div class="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
          <Card
            v-for="metric in metrics"
            :key="metric.key"
            :bordered="false"
            size="small"
          >
            <div class="text-xs text-muted-foreground">{{ metric.label }}</div>
            <div class="mt-2 text-2xl font-semibold tabular-nums">
              {{ formatCount(metric.value) }}
            </div>
            <div class="mt-1 text-xs text-muted-foreground">
              {{ metric.description }}
            </div>
          </Card>
        </div>

        <Card v-if="collector" :bordered="false" title="实时采集状态">
          <div class="flex flex-wrap items-center gap-3 text-sm">
            <Tag :color="collector.state === 'running' ? 'processing' : 'default'">
              {{
                collector.state === 'running'
                  ? '采集中'
                  : (collector.state ?? '暂无运行状态')
              }}
            </Tag>
            <span v-if="collector.exchange"
              >{{ collector.exchange }} · {{ collector.market ?? '—' }}</span
            >
            <span class="text-xs text-muted-foreground">
              状态时间
              {{ localTime(collector.updated_at ?? collector.last_at) }}
            </span>
          </div>
          <Alert
            v-if="collectorError"
            class="mt-3"
            :description="collectorError"
            message="采集错误"
            show-icon
            type="error"
          />
        </Card>

        <Card :bordered="false" title="同步来源">
          <template #extra>
            <span class="text-xs text-muted-foreground"
              >{{ status.sources?.length ?? 0 }} 个来源 ·
              {{ sourceErrorCount }} 个错误</span
            >
          </template>
          <div class="mb-3 flex flex-wrap gap-2">
            <Input
              v-model:value="sourceSearch"
              allow-clear
              class="!max-w-sm"
              placeholder="搜索来源、路径或类型"
            />
            <Button
              :type="sourceErrorsOnly ? 'primary' : 'default'"
              @click="sourceErrorsOnly = !sourceErrorsOnly"
            >
              {{ sourceErrorsOnly ? '正在筛选错误' : '只看错误' }}
            </Button>
          </div>
          <Table
            :columns="sourceColumns"
            :data-source="sources"
            :pagination="pagination"
            :row-key="sourceKey"
            :scroll="{ x: 1080 }"
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'source'">
                <div class="font-medium">{{ record.key }}</div>
                <div
                  class="mt-1 break-all font-mono text-xs text-muted-foreground"
                >
                  {{ record.path ?? '—' }}
                </div>
              </template>
              <template v-else-if="column.key === 'rows'">{{
                formatCount(record.row_count)
              }}</template>
              <template v-else-if="column.key === 'updated_at'">{{
                localTime(record.updated_at)
              }}</template>
              <template v-else-if="column.key === 'result'">
                <div v-if="record.error" class="break-words text-red-500">
                  {{ record.error }}
                </div>
                <Tag v-else-if="record.updated_at" color="success">已同步</Tag>
                <Tag v-else>等待同步</Tag>
              </template>
            </template>
            <template #emptyText>
              <Empty
                :description="
                  sourceSearch || sourceErrorsOnly
                    ? '没有符合筛选条件的来源'
                    : '暂无已登记的同步来源'
                "
              />
            </template>
          </Table>
        </Card>

        <Card :bordered="false" title="行情数据集">
          <template #extra>
            <span class="text-xs text-muted-foreground"
              >筛选结果 {{ datasets.length }} 组 ·
              {{ formatCount(datasetRows) }} 条 K 线</span
            >
          </template>
          <Input
            v-model:value="datasetSearch"
            allow-clear
            class="mb-3 !max-w-sm"
            placeholder="搜索交易所、市场、交易对或周期"
          />
          <Table
            :columns="datasetColumns"
            :data-source="datasets"
            :pagination="pagination"
            :row-key="datasetKey"
            :scroll="{ x: 1180 }"
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'rows'">{{
                formatCount(record.rows)
              }}</template>
              <template v-else-if="column.key === 'first_timestamp'">{{
                localTime(record.first_timestamp)
              }}</template>
              <template v-else-if="column.key === 'last_timestamp'">{{
                localTime(record.last_timestamp)
              }}</template>
            </template>
            <template #emptyText>
              <Empty
                :description="
                  datasetSearch ? '没有符合筛选条件的数据集' : '行情尚未入库'
                "
              />
            </template>
          </Table>
        </Card>
      </template>
    </Spin>

    <Card :bordered="false" title="最新逐笔与盘口">
      <template #extra>
        <span class="text-xs text-muted-foreground"
          >查询更新 {{ localTime(marketRefreshedAt) }}</span
        >
      </template>
      <div class="mb-3 flex flex-wrap gap-2">
        <Select
          v-model:value="exchange"
          :options="[
            { label: 'Binance', value: 'binance' },
            { label: 'OKX', value: 'okx' },
          ]"
          class="!w-32"
          aria-label="交易所"
        />
        <Select
          v-model:value="market"
          :options="[
            { label: '永续合约', value: 'futures' },
            { label: '现货', value: 'spot' },
          ]"
          class="!w-32"
          aria-label="市场"
        />
        <Input
          v-model:value="pair"
          allow-clear
          class="!max-w-xs"
          placeholder="例如 BTC/USDT:USDT"
          aria-label="交易对"
          @press-enter="refresh"
        />
        <Button :disabled="!pair.trim()" :loading="loading" @click="refresh"
          >查询</Button
        >
      </div>
      <div class="mb-4 text-xs text-muted-foreground">
        {{ displayedMarket }}
      </div>
      <div class="grid gap-4 xl:grid-cols-2">
        <div class="min-w-0">
          <div class="mb-2 text-sm font-medium">最近 50 笔成交</div>
          <Alert
            v-if="ticksError"
            class="mb-3"
            :description="ticksError"
            :message="
              ticks.length
                ? '逐笔数据刷新失败，保留上次成功数据'
                : '逐笔数据读取失败'
            "
            show-icon
            type="error"
          />
          <Table
            :columns="tickColumns"
            :data-source="ticks"
            :pagination="{ pageSize: 10, showSizeChanger: false }"
            :row-key="tickKey"
            :scroll="{ x: 625 }"
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'timestamp'">{{
                localTime(record.timestamp)
              }}</template>
              <template v-else-if="column.key === 'price'">{{
                formatNumber(record.price)
              }}</template>
              <template v-else-if="column.key === 'quantity'">{{
                formatNumber(record.quantity)
              }}</template>
              <template v-else-if="column.key === 'side'">
                <Tag
                  v-if="
                    buyerMaker(record.buyer_maker, record.is_buyer_maker) ===
                    true
                  "
                  color="red"
                  >主动卖出</Tag
                >
                <Tag
                  v-else-if="
                    buyerMaker(record.buyer_maker, record.is_buyer_maker) ===
                    false
                  "
                  color="green"
                  >主动买入</Tag
                >
                <span v-else>—</span>
              </template>
            </template>
            <template #emptyText
              ><Empty description="所选市场暂无已入库的逐笔成交"
            /></template>
          </Table>
        </div>
        <div class="min-w-0">
          <div
            class="mb-2 flex flex-wrap items-center justify-between gap-2 text-sm"
          >
            <span class="font-medium">最新盘口 · 前 10 档</span>
            <span class="text-xs text-muted-foreground"
              >{{ localTime(latestOrderbook?.timestamp) }} · 更新 ID
              {{
                latestOrderbook?.last_update_id ??
                latestOrderbook?.update_id ??
                '—'
              }}</span
            >
          </div>
          <Alert
            v-if="orderbooksError"
            class="mb-3"
            :description="orderbooksError"
            :message="
              orderbooks.length
                ? '盘口数据刷新失败，保留上次成功数据'
                : '盘口数据读取失败'
            "
            show-icon
            type="error"
          />
          <Empty
            v-if="!latestOrderbook"
            class="py-8"
            description="所选市场暂无已入库的盘口快照"
          />
          <div v-else class="grid grid-cols-2 gap-3">
            <div class="min-w-0">
              <div class="mb-2 text-xs font-medium text-emerald-500">买盘</div>
              <Table
                :columns="orderbookColumns"
                :data-source="bidLevels"
                :pagination="false"
                :scroll="{ x: 250 }"
                row-key="level"
                size="small"
              >
                <template #bodyCell="{ column, record }">
                  <span
                    v-if="column.key === 'price'"
                    class="text-emerald-500"
                    >{{ formatNumber(record.price) }}</span
                  >
                  <template v-else-if="column.key === 'quantity'">{{
                    formatNumber(record.quantity)
                  }}</template>
                </template>
                <template #emptyText>暂无买盘档位</template>
              </Table>
            </div>
            <div class="min-w-0">
              <div class="mb-2 text-xs font-medium text-red-500">卖盘</div>
              <Table
                :columns="orderbookColumns"
                :data-source="askLevels"
                :pagination="false"
                :scroll="{ x: 250 }"
                row-key="level"
                size="small"
              >
                <template #bodyCell="{ column, record }">
                  <span v-if="column.key === 'price'" class="text-red-500">{{
                    formatNumber(record.price)
                  }}</span>
                  <template v-else-if="column.key === 'quantity'">{{
                    formatNumber(record.quantity)
                  }}</template>
                </template>
                <template #emptyText>暂无卖盘档位</template>
              </Table>
            </div>
          </div>
        </div>
      </div>
    </Card>
  </div>
</template>

<script lang="ts" setup>
import type { EchartsUIType } from '@vben/plugins/echarts';

import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';

import { EchartsUI, useEcharts } from '@vben/plugins/echarts';
import { Alert, Button, Card, Empty, Select, Spin, Table, Tag } from 'ant-design-vue';

import { getOrderflowPriceSpikes } from '#/api/database';
import type { PriceSpikeCandidate, PriceSpikeResponse, PriceSpikeStats } from '#/api/database';
import { errText } from '#/api/freqtrade/client';

const pairs = [
  { label: 'BTC/USDT:USDT', value: 'BTC/USDT:USDT' },
  { label: 'ETH/USDT:USDT', value: 'ETH/USDT:USDT' },
];
const directions = [
  { label: '全部方向', value: 'any' },
  { label: '上涨爆发', value: 'up' },
  { label: '下跌爆发', value: 'down' },
];
const bands = [
  { key: 'lt1', label: '<1x' },
  { key: '1to2', label: '1-2x' },
  { key: '2to4', label: '2-4x' },
  { key: '4to8', label: '4-8x' },
  { key: '8to20', label: '8-20x' },
  { key: 'ge20', label: '≥20x' },
];

const pair = ref(pairs[0]!.value);
const direction = ref<'any' | 'up' | 'down'>('any');
const items = ref<PriceSpikeCandidate[]>([]);
const stats = ref<PriceSpikeStats | null>(null);
const warmup = ref<PriceSpikeResponse['warmup']>({});
const loading = ref(false);
const error = ref('');
const updatedAt = ref<number | null>(null);
const chartRef = ref<EchartsUIType>();
const { renderEcharts } = useEcharts(chartRef);
let controller: AbortController | undefined;
let sequence = 0;
let timer: ReturnType<typeof setInterval> | undefined;

const pairStats = computed<Record<string, any>>(
  () => (stats.value?.by_pair?.[pair.value] as Record<string, any>) ?? {},
);
const profile = computed<Record<string, any>>(
  () => (stats.value?.profile_by_pair?.[pair.value] as Record<string, any>) ?? {},
);
const pairWarmup = computed(() => warmup.value[pair.value] ?? {});
const hasProfile = computed(() => ['up', 'down'].some((direction) => {
  const points = profile.value[direction];
  return Array.isArray(points) && points.some((point) =>
    Number(point.sample_count) > 0 && number(point.mean_volume_ratio) !== null);
}));
const probabilityRows = computed(() =>
  bands.map(({ key, label }) => ({
    key,
    band: label,
    ...(pairStats.value.bands?.[key] ?? {}),
  })),
);
const spikeRows = computed(() =>
  items.value.map((row) => ({ ...row, _key: row.event_id })),
);

function number(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function fixed(value: unknown, digits = 2) {
  const parsed = number(value);
  return parsed === null ? '—' : parsed.toLocaleString('zh-CN', { maximumFractionDigits: digits });
}

function percent(value: unknown) {
  const parsed = number(value);
  return parsed === null ? '—' : `${(parsed * 100).toFixed(2)}%`;
}

function localTime(value: unknown) {
  const parsed = number(value);
  if (parsed === null) return '—';
  return new Date(parsed).toLocaleString('zh-CN', { hour12: false });
}

function candidateValue(row: Record<string, unknown>, key: string): unknown {
  const nested = row.observation;
  const value = nested && typeof nested === 'object'
    ? (nested as Record<string, unknown>)[key]
    : undefined;
  return value ?? row[key];
}

const probabilityColumns = [
  { title: '量比档', dataIndex: 'band', key: 'band', width: 100 },
  { title: '有效样本', dataIndex: 'matured_count', key: 'n', align: 'right' as const },
  { title: 'P(涨)', dataIndex: 'p_up', key: 'up', align: 'right' as const },
  { title: 'P(跌)', dataIndex: 'p_down', key: 'down', align: 'right' as const },
  { title: 'P(任一)', dataIndex: 'p_any', key: 'any', align: 'right' as const },
  { title: '无条件提升', dataIndex: 'lift', key: 'lift', align: 'right' as const },
];

const eventColumns = [
  { title: '候选根时间', key: 'timestamp', width: 180 },
  { title: '结果', key: 'direction', width: 140 },
  { title: '15 分钟收益', key: 'return', align: 'right' as const },
  { title: '量比档', key: 'band', width: 90 },
  { title: '量比', key: 'ratio', align: 'right' as const },
  { title: '记录 / 成熟时间', key: 'detected', width: 180 },
  { title: '剖面', key: 'profile', width: 90 },
];

function drawProfile() {
  const offsets = Array.from({ length: 25 }, (_, index) => index - 12);
  const up = Array.isArray(profile.value.up) ? profile.value.up : [];
  const down = Array.isArray(profile.value.down) ? profile.value.down : [];
  const upName = `上涨爆发 n=${fixed(up[0]?.sample_count, 0)}`;
  const downName = `下跌爆发 n=${fixed(down[0]?.sample_count, 0)}`;
  const values = (rows: any[]) => offsets.map((offset) => {
    const point = rows.find((item) => Number(item.offset) === offset);
    return number(point?.mean_volume_ratio);
  });
  renderEcharts({
    animation: false,
    color: ['#c2413a', '#157f78'],
    grid: { top: 28, right: 24, bottom: 32, left: 56 },
    legend: { data: [upName, downName], top: 0 },
    tooltip: { trigger: 'axis' },
    xAxis: {
      type: 'category',
      data: offsets.map((offset) => (offset > 0 ? `+${offset}` : `${offset}`)),
      name: '相对候选根（5m）',
      nameLocation: 'middle',
      nameGap: 24,
    },
    yAxis: {
      type: 'value',
      name: '量比',
      min: 0,
      splitLine: { lineStyle: { color: '#e5e7eb' } },
    },
    series: [
      {
        name: upName, type: 'line', connectNulls: false, showSymbol: false,
        data: values(up),
        markLine: { symbol: 'none', data: [{ yAxis: 1, name: '1x' }], label: { formatter: '1x' } },
      },
      { name: downName, type: 'line', connectNulls: false, showSymbol: false, data: values(down) },
    ],
  });
}

async function refresh() {
  controller?.abort();
  controller = new AbortController();
  const current = ++sequence;
  loading.value = true;
  error.value = '';
  try {
    const response = await getOrderflowPriceSpikes(
      { pair: pair.value, direction: direction.value, limit: 100 },
      controller.signal,
    );
    if (current !== sequence) return;
    items.value = response.items;
    stats.value = response.stats;
    warmup.value = response.warmup ?? {};
    updatedAt.value = Date.now();
    await nextTick();
    drawProfile();
  } catch (reason) {
    if (current === sequence) error.value = errText(reason);
  } finally {
    if (current === sequence) loading.value = false;
  }
}

watch([pair, direction], () => void refresh());
onMounted(() => {
  void refresh();
  timer = setInterval(() => void refresh(), 60_000);
});
onUnmounted(() => {
  controller?.abort();
  if (timer) clearInterval(timer);
});
</script>

<template>
  <div class="p-4">
    <Alert
      class="mb-3"
      type="warning"
      show-icon
      message="量比对应波动概率，不提供涨跌方向，也不是单独入场信号。"
      description="在线样本只从本次运行期间、基线就绪后的已收盘 K 线开始；候选根收盘至第 3 根后续 K 线收盘计算 15 分钟收益。"
    />
    <Alert v-if="error" class="mb-3" type="error" show-icon :message="error" />

    <Card :bordered="false" class="shadow-sm" title="价格爆发与量比">
      <template #extra>
        <div class="flex items-center gap-2">
          <Select v-model:value="pair" :options="pairs" style="width: 180px" />
          <Select v-model:value="direction" :options="directions" style="width: 128px" />
          <Button :loading="loading" @click="refresh">刷新</Button>
        </div>
      </template>

      <Spin :spinning="loading">
        <div class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
          <div class="metric-cell">
            <div class="metric-label">在线有效试验</div>
            <div class="metric-value">{{ fixed(pairStats.matured_count, 0) }}</div>
            <div class="metric-sub">候选 {{ fixed(pairStats.candidate_count, 0) }} · 无效 {{ fixed(pairStats.invalid_count, 0) }}</div>
          </div>
          <div class="metric-cell">
            <div class="metric-label">在线无条件 P(任一)</div>
            <div class="metric-value">{{ percent(pairStats.base_p_any) }}</div>
            <div class="metric-sub">仅使用已成熟且窗口连续的样本</div>
          </div>
          <div class="metric-cell">
            <div class="metric-label">在线采样区间</div>
            <div class="metric-value metric-period">
              {{ localTime(pairStats.first_timestamp) }}
            </div>
            <div class="metric-sub">至 {{ localTime(pairStats.last_timestamp) }} · 更新 {{ localTime(updatedAt) }}</div>
          </div>
          <div class="metric-cell">
            <div class="metric-label">基线预热</div>
            <div class="metric-value">
              {{ fixed(pairWarmup.valid_bars, 0) }} / {{ fixed(pairWarmup.required_bars, 0) }}
            </div>
            <div class="metric-sub">
              {{ pairWarmup.ready ? '基线就绪' : '预热中 · 仅使用本机实时收盘 K 线' }}
            </div>
          </div>
        </div>

        <div class="mt-6">
          <div class="section-title">条件概率与无条件提升</div>
          <Table
            :columns="probabilityColumns"
            :data-source="probabilityRows"
            :pagination="false"
            :row-key="(row: any) => row.key"
            size="small"
            :scroll="{ x: 680 }"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'n'">{{ fixed(record.matured_count, 0) }}</template>
              <template v-else-if="['up', 'down', 'any'].includes(String(column.key))">
                {{ percent(record[String(column.key) === 'up' ? 'p_up' : String(column.key) === 'down' ? 'p_down' : 'p_any']) }}
              </template>
              <template v-else-if="column.key === 'lift'">{{ record.lift == null ? '—' : `${fixed(record.lift, 2)}×` }}</template>
            </template>
          </Table>
        </div>

        <div class="mt-6">
          <div class="section-title">上涨与下跌事件的量比剖面</div>
          <div class="mb-2 text-xs text-muted-foreground">仅显示已获得完整 t−12 至 t+12 在线数据的事件；曲线按方向分组，缺失窗口不补值。</div>
          <EchartsUI v-show="hasProfile" ref="chartRef" height="280px" />
          <div v-if="!hasProfile" class="py-2 text-center text-xs text-muted-foreground">
            当前币种尚无完整剖面样本
          </div>
        </div>

        <div class="mt-6">
          <div class="section-title">最近爆发与待判定候选</div>
          <Table
            :columns="eventColumns"
            :data-source="spikeRows"
            :pagination="false"
            :row-key="(row: any) => row._key"
            size="small"
            :scroll="{ x: 900, y: 420 }"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'timestamp'">{{ localTime(record.timestamp) }}</template>
              <template v-else-if="column.key === 'direction'">
                <Tag v-if="record.status === 'unknown'" color="warning">待成熟 · unknown</Tag>
                <Tag v-else-if="record.status === 'invalid'" color="default">无效 · 数据缺口</Tag>
                <Tag v-else :color="record.outcome?.direction === 'up' ? 'red' : 'green'">
                  {{ record.outcome?.direction === 'up' ? '上涨' : '下跌' }}
                </Tag>
              </template>
              <template v-else-if="column.key === 'return'">{{ percent(record.outcome?.return_pct) }}</template>
              <template v-else-if="column.key === 'band'">{{ candidateValue(record, 'ratio_band') }}</template>
              <template v-else-if="column.key === 'ratio'">{{ fixed(candidateValue(record, 'volume_ratio'), 2) }}×</template>
              <template v-else-if="column.key === 'detected'">
                {{ localTime(record.outcome?.matured_at_utc ?? record.observed_at) }}
              </template>
              <template v-else-if="column.key === 'profile'">
                {{ record.profile_status === 'matured' ? '完整' : record.profile_status === 'invalid' ? '有缺口' : '待成熟' }}
              </template>
            </template>
            <template #emptyText>
              <Empty description="暂无在线候选记录" />
            </template>
          </Table>
        </div>

        <div class="mt-6 border-t border-gray-200 pt-4 dark:border-gray-700">
          <div class="section-title">第三轮回顾性研究参考</div>
          <div class="grid grid-cols-1 gap-2 text-sm md:grid-cols-2">
            <div class="reference-row">BTC ≥20x：P(任一) 4.28% · 涨 2.44% · 跌 1.83%</div>
            <div class="reference-row">ETH ≥20x：P(任一) 7.93% · 涨 4.21% · 跌 3.72%</div>
          </div>
          <div class="mt-2 text-xs leading-5 text-muted-foreground">
            历史值来自 2.5 年回顾样本，不并入上方在线统计；原研究没有注明收益锚点，不能当作在线方法的复算结果。上涨与下跌概率接近；按表内 P(任一) 计算，≥20x 后仍有约 95.7% 的 BTC 与 92.1% 的 ETH 窗口未达到 1.5%。滑点、资金费和成交可行性不在本观测口径内。
          </div>
        </div>
      </Spin>
    </Card>
  </div>
</template>

<style scoped>
.metric-cell {
  min-width: 0;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  padding: 12px;
}

.metric-label,
.metric-sub {
  color: #6b7280;
  font-size: 12px;
}

.metric-value {
  margin-top: 4px;
  overflow-wrap: anywhere;
  font-size: 20px;
  font-weight: 600;
  line-height: 1.35;
}

.metric-period {
  font-size: 14px;
}

.metric-sub {
  margin-top: 4px;
}

.section-title {
  margin-bottom: 10px;
  font-size: 14px;
  font-weight: 600;
}

.reference-row {
  border-left: 2px solid #157f78;
  padding: 6px 10px;
}
</style>

<script lang="ts" setup>
import { sortCoins } from '../utils/coinOrder';
/**
 * 回测明细 —— 回答三个问题：
 *   1. 这轮赚了多少？（资金曲线 + 盈亏指标）
 *   2. 怎样下单的？（每笔交易的开平仓价、时间、投入、平仓原因）
 *   3. 怎样决策的？（每笔交易当时的模型预测值与阈值比较）
 */
import type { EchartsUIType } from '@vben/plugins/echarts';

import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';

import { EchartsUI, useEcharts } from '@vben/plugins/echarts';

import {
  Alert,
  Card,
  Col,
  Radio,
  Row,
  Select,
  Spin,
  Table,
  Tag,
  Tooltip,
} from 'ant-design-vue';

import { EXIT_LABELS, getBacktestDetail, getBacktestList, modelLabel } from '#/api/freqtrade';

const list = ref<any[]>([]);
const ident = ref('');
const detail = ref<any>(null);
const loading = ref(false);
const errMsg = ref<string | null>(null);
const filter = ref<'all' | 'loss' | 'win'>('all');
const pairFilter = ref<string>('');

const chartRef = ref<EchartsUIType>();
const { renderEcharts } = useEcharts(chartRef);
const scatterRef = ref<EchartsUIType>();
const { renderEcharts: renderScatter } = useEcharts(scatterRef);

const fmt = (v: any, n = 2) =>
  v === null || v === undefined || Number.isNaN(Number(v))
    ? '—'
    : Number(v).toFixed(n);
const cls = (v: any) =>
  Number(v) > 0 ? 'text-red-500' : Number(v) < 0 ? 'text-emerald-500' : '';
const localTime = (v?: string) => {
  if (!v) return '—';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return String(v);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};

const summary = computed(() => detail.value?.summary ?? {});

const kpis = computed(() => {
  const s = summary.value;
  if (!s.trades) return [];
  return [
    { k: '净利润', v: `${s.profit_abs > 0 ? '+' : ''}${s.profit_abs}`, u: 'USDT', c: cls(s.profit_abs) },
    { k: '收益率', v: `${s.profit_pct > 0 ? '+' : ''}${s.profit_pct}`, u: '%', c: cls(s.profit_pct) },
    { k: '期末余额', v: `${s.final_balance}`, u: '', sub: `起始 ${s.starting_balance}`, c: '' },
    { k: '交易笔数', v: `${s.trades}`, u: '笔', sub: `多 ${s.trades_long} / 空 ${s.trades_short}`, c: '' },
    { k: '胜率', v: `${s.winrate}`, u: '%', sub: `${s.wins} 胜 / ${s.losses} 负`, c: '' },
    { k: '平均盈利', v: `${s.avg_win ?? '—'}`, u: '%', c: 'text-red-500' },
    { k: '平均亏损', v: `${s.avg_loss ?? '—'}`, u: '%', c: 'text-emerald-500' },
    { k: '最大回撤', v: `${s.max_drawdown_pct}`, u: '%', c: 'text-emerald-500' },
    { k: 'Sharpe', v: `${s.sharpe}`, u: '', c: cls(s.sharpe) },
    { k: '盈亏比', v: `${s.profit_factor}`, u: '', c: s.profit_factor > 1 ? 'text-red-500' : 'text-emerald-500' },
    {
      k: '预测-实际 IC',
      v: `${s.pred_vs_actual_ic ?? '—'}`,
      u: '',
      sub: '模型判断力的直接度量',
      c: cls(s.pred_vs_actual_ic),
    },
    {
      k: '方向命中率',
      v: `${s.sign_hit_rate ?? '—'}`,
      u: '%',
      sub: '50% 为随机水平',
      c: s.sign_hit_rate >= 50 ? 'text-red-500' : 'text-emerald-500',
    },
  ];
});

const trades = computed(() => {
  let t: any[] = detail.value?.trades ?? [];
  if (filter.value === 'win') t = t.filter((x) => x.profit_abs > 0);
  if (filter.value === 'loss') t = t.filter((x) => x.profit_abs <= 0);
  if (pairFilter.value) t = t.filter((x) => x.pair === pairFilter.value);
  return t;
});

/** 下拉选项：中文名给用户看，identifier 仍作为取值 */
const identOptions = computed(() =>
  list.value.map((x: any) => ({ label: modelLabel(x.identifier), value: x.identifier })),
);

const pairOptions = computed(() => [
  { label: '全部币对', value: '' },
  ...sortCoins(detail.value?.by_pair ?? [], (row: any) => row.pair).map((p: any) => ({ label: p.pair, value: p.pair })),
]);

const columns = [
  { dataIndex: 'pair', key: 'pair', title: '币对', width: 130, fixed: 'left' as const },
  { key: 'side', title: '方向', width: 60 },
  { dataIndex: 'open_date', key: 'open_date', title: '开仓时间', width: 130 },
  { dataIndex: 'open_rate', key: 'open_rate', title: '开仓价', align: 'right' as const },
  { dataIndex: 'close_rate', key: 'close_rate', title: '平仓价', align: 'right' as const },
  { dataIndex: 'stake_amount', key: 'stake_amount', title: '投入', align: 'right' as const },
  { key: 'profit_pct', title: '收益率', align: 'right' as const, sorter: (a: any, b: any) => a.profit_pct - b.profit_pct },
  { key: 'profit_abs', title: '盈亏', align: 'right' as const, sorter: (a: any, b: any) => a.profit_abs - b.profit_abs },
  { key: 'exit_reason', title: '平仓原因', width: 100 },
  { key: 'model_pred_pct', title: '模型预测', align: 'right' as const, sorter: (a: any, b: any) => (a.model_pred_pct ?? 0) - (b.model_pred_pct ?? 0) },
  { key: 'decision', title: '决策依据', width: 240 },
  { key: 'duration_h', title: '持仓(h)', align: 'right' as const },
];

function drawEquity() {
  const eq = detail.value?.equity ?? [];
  if (eq.length < 2) return;
  renderEcharts({
    grid: { bottom: 40, containLabel: true, left: 60, right: 20, top: 30 },
    series: [
      {
        areaStyle: { color: 'rgba(59,130,246,0.15)' },
        data: eq.map((d: any) => d[1]),
        itemStyle: { color: '#3b82f6' },
        name: '账户余额',
        showSymbol: false,
        smooth: true,
        type: 'line',
      },
    ],
    tooltip: { trigger: 'axis' },
    xAxis: { axisLabel: { fontSize: 10 }, boundaryGap: false, data: eq.map((d: any) => d[0]), type: 'category' },
    yAxis: { name: 'USDT', scale: true, type: 'value' },
  });
}

function drawScatter() {
  const t = detail.value?.trades ?? [];
  const pts = t
    .filter((x: any) => x.model_pred_pct !== null)
    .map((x: any) => ({
      itemStyle: { color: x.profit_pct >= 0 ? '#12b886' : '#f0424f', opacity: 0.8 },
      value: [x.model_pred_pct, x.profit_pct, x.pair],
    }));
  if (!pts.length) return;
  renderScatter({
    grid: { bottom: 48, containLabel: true, left: 24, right: 24, top: 20 },
    series: [
      {
        data: pts,
        name: '每笔交易',
        symbolSize: 9,
        type: 'scatter',
      },
    ],
    tooltip: {
      formatter: (p: any) =>
        `${p.value[2]}<br/>模型预测 ${p.value[0]}%<br/>实际结果 ${p.value[1]}%`,
    },
    xAxis: {
      axisLine: { onZero: true },
      name: '模型预测 (%)',
      nameLocation: 'middle',
      nameGap: 30,
      splitLine: { show: true },
      type: 'value',
    },
    yAxis: {
      axisLine: { onZero: true },
      // nameLocation: middle + 足够的 nameGap，避免与刻度标签堆叠
      name: '实际收益 (%)',
      nameGap: 46,
      nameLocation: 'middle',
      nameRotate: 90,
      splitLine: { show: true },
      type: 'value',
    },
  });
}

async function load() {
  loading.value = true;
  try {
    const l = await getBacktestList();
    list.value = l.items ?? [];
    if (!ident.value && list.value.length) {
      // 默认选中 BTC 相关的那次回测；没有则退回列表第一个
      // （列表按收益率排序，BTC 不在第一个）
      const btc = list.value.find((x: any) =>
        String(x.identifier).toLowerCase().includes('btc'),
      );
      ident.value = (btc ?? list.value[0]!).identifier;
    }
    if (ident.value) {
      detail.value = await getBacktestDetail(ident.value);
      // 图表位于 v-else 分支：必须等 Vue 把 DOM 渲染出来再初始化 ECharts
      await nextTick();
      await nextTick();
      drawEquity();
      drawScatter();
    }
  } catch (e: any) {
      // 没有回测结果 / 接口不可用时，不要抛成未捕获的 Promise 拒绝
      detail.value = null;
      errMsg.value = e?.message ?? '加载回测明细失败';
  } finally {
    loading.value = false;
  }
}

watch(ident, () => {
  pairFilter.value = '';
  load();
});
let timer: any = null;
onMounted(() => {
  load();
  // 每次回测产出新的明细目录，列表与图表都要自动跟上
  timer = setInterval(load, 60_000);
});
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <div class="p-4">
    <Alert
      v-if="errMsg"
      class="mb-3"
      type="warning"
      show-icon
      :message="errMsg"
    />

    <Card :bordered="false" class="shadow-sm" title="回测明细">
      <template #extra>
        <span class="text-xs text-muted-foreground">
          展示每笔交易的下单明细与当时的模型判断
        </span>
        <!-- 下拉显示中文名，值仍是 identifier（取数要用）；悬停可看原始标识 -->
        <span :title="ident" class="ml-3 inline-block">
          <Select v-model:value="ident" :options="identOptions" style="width: 340px" />
        </span>
      </template>

      <Spin :spinning="loading">
        <div v-if="!list.length" class="py-16 text-center text-muted-foreground">
          暂无回测明细 —— 运行
          <code class="mx-1">python extract_detail.py --auto</code>
          生成
        </div>

        <template v-else>
          <!-- 指标 -->
          <Row :gutter="[12, 12]">
            <Col v-for="x in kpis" :key="x.k" :lg="2" :md="6" :sm="8" :xs="12" :xxl="2">
              <div class="rounded-lg border border-gray-100 p-3 dark:border-gray-800">
                <div class="text-xs text-muted-foreground">{{ x.k }}</div>
                <div class="mt-1 text-lg font-semibold" :class="x.c">
                  {{ x.v }}
                  <span class="text-xs font-normal text-muted-foreground">{{ x.u }}</span>
                </div>
                <div v-if="x.sub" class="text-[11px] text-muted-foreground">{{ x.sub }}</div>
              </div>
            </Col>
          </Row>

          <!-- 图 -->
          <Row :gutter="[12, 12]" class="mt-4">
            <Col :lg="13" :xs="24">
              <div class="mb-2 text-sm font-medium">资金曲线</div>
              <EchartsUI ref="chartRef" height="260px" />
            </Col>
            <Col :lg="11" :xs="24">
              <div class="mb-2 text-sm font-medium">
                模型预测 vs 实际收益
                <Tooltip title="每个点是一笔交易：横轴是开仓时模型的预测涨跌幅，纵轴是最终实际收益。若模型有效，点应沿右上方向分布。">
                  <span class="ml-1 cursor-help text-xs text-blue-500">说明</span>
                </Tooltip>
              </div>
              <EchartsUI ref="scatterRef" height="260px" />
            </Col>
          </Row>
        </template>
      </Spin>
    </Card>

    <!-- 交易明细 -->
    <Card v-if="detail" :bordered="false" class="mt-4 shadow-sm" title="交易明细">
      <template #extra>
        <Radio.Group v-model:value="filter" button-style="solid" size="small">
          <Radio.Button value="all">全部</Radio.Button>
          <Radio.Button value="win">只看盈利</Radio.Button>
          <Radio.Button value="loss">只看亏损</Radio.Button>
        </Radio.Group>
        <Select
          v-model:value="pairFilter"
          :options="pairOptions"
          size="small"
          style="width: 160px; margin-left: 10px"
        />
        <span class="ml-3 text-xs text-muted-foreground">{{ trades.length }} 笔</span>
      </template>

      <Table
        :columns="columns"
        :data-source="sortCoins(trades, (row) => row.pair)"
        :pagination="{ pageSize: 20, showSizeChanger: false }"
        :scroll="{ x: 1500 }"
        row-key="open_date"
        size="small"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'pair'">
            <span class="font-medium">{{ record.pair }}</span>
          </template>
          <template v-else-if="column.key === 'side'">
            <Tag :color="record.is_short ? 'green' : 'red'">{{ record.side }}</Tag>
          </template>
          <template v-else-if="column.key === 'open_date'">
            <span class="text-xs">{{ localTime(record.open_date) }}</span>
          </template>
          <template v-else-if="column.key === 'profit_pct'">
            <span :class="cls(record.profit_pct)" class="font-semibold">
              {{ record.profit_pct > 0 ? '+' : '' }}{{ record.profit_pct }}%
            </span>
          </template>
          <template v-else-if="column.key === 'profit_abs'">
            <span :class="cls(record.profit_abs)">
              {{ record.profit_abs > 0 ? '+' : '' }}{{ record.profit_abs }}
            </span>
          </template>
          <template v-else-if="column.key === 'exit_reason'">
            <Tag :color="record.exit_reason === 'stop_loss' ? 'red' : record.exit_reason === 'trailing_stop_loss' ? 'orange' : 'default'">
              {{ EXIT_LABELS[record.exit_reason] || record.exit_reason }}
            </Tag>
          </template>
          <template v-else-if="column.key === 'model_pred_pct'">
            <span :class="cls(record.model_pred_pct)" class="font-mono">
              {{ record.model_pred_pct === null ? '—' : (record.model_pred_pct > 0 ? '+' : '') + record.model_pred_pct + '%' }}
            </span>
          </template>
          <template v-else-if="column.key === 'decision'">
            <span class="text-xs text-muted-foreground">{{ record.decision }}</span>
          </template>
          <template v-else-if="column.key === 'open_rate' || column.key === 'close_rate'">
            <span class="font-mono text-xs">{{ fmt(record[column.key], 6) }}</span>
          </template>
          <template v-else-if="column.key === 'stake_amount'">
            <span class="font-mono text-xs">{{ fmt(record.stake_amount) }}</span>
          </template>
          <template v-else-if="column.key === 'duration_h'">
            <span class="font-mono text-xs">{{ record.duration_h }}</span>
          </template>
        </template>
      </Table>
    </Card>
  </div>
</template>

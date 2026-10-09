<script lang="ts" setup>
import { compareCoins } from '../utils/coinOrder';
/**
 * 实盘统计 —— 真实实盘口径的账户与交易统计
 *
 * 为什么单独做一页：
 *   终端里原有的资金曲线（总览 / 回测明细）全部来自【回测产物】，
 *   与实盘无关。本页只使用 Freqtrade 的实盘统计接口，口径与回测页不同：
 *
 *   /v1/profit            盈亏总览（已平仓 / 含持仓）
 *   /v1/balance           账户余额与起始资金
 *   /v1/historic_balance  实盘钱包净值历史（真实净值曲线来源）
 *   /v1/daily|weekly|monthly  按日/周/月盈亏分解
 *   /v1/stats             出场原因统计与持仓时长
 *   /v1/performance|entries|exits|mix_tags  四类归因
 *
 * 说明：
 *   - timescale 的单位随粒度变化（日=天 / 周=周 / 月=月），见 timescaleUnit。
 *   - 本页只读，不提供任何写操作。
 *   - 每 30 秒轮询一次；单个接口失败只降级该板块，且保留上一次成功的数据，
 *     不会让整页崩掉。
 */
import type { EchartsUIType } from '@vben/plugins/echarts';

import type {
  FtBalance,
  FtDaily,
  FtProfit,
  FtStatRow,
  FtStats,
  FtWalletHistory,
} from '#/api/freqtrade';

import { computed, onMounted, onUnmounted, ref, watch } from 'vue';

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
} from 'ant-design-vue';

import {
  errText,
  getBalance,
  getDaily,
  getEntryStats,
  getExitStats,
  getHistoricBalance,
  getMixTagStats,
  getMonthly,
  getPerformance,
  getProfit,
  getStats,
  getWeekly,
} from '#/api/freqtrade';

type Period = 'daily' | 'monthly' | 'weekly';

/** 三种粒度的接口；timescale 单位不同，见 timescaleUnit */
const PERIOD_API: Record<Period, (timescale?: number) => Promise<FtDaily>> = {
  daily: getDaily,
  monthly: getMonthly,
  weekly: getWeekly,
};

const TIMESCALES = [7, 30, 90, 365];

/* ══════════ 状态 ══════════ */
const profit = ref<FtProfit | null>(null);
const balance = ref<FtBalance | null>(null);
const hist = ref<FtWalletHistory | null>(null);
const stats = ref<FtStats | null>(null);
const perf = ref<FtStatRow[] | null>(null);
const entryStats = ref<FtStatRow[] | null>(null);
const exitStats = ref<FtStatRow[] | null>(null);
const mixStats = ref<FtStatRow[] | null>(null);

const series = ref<FtDaily | null>(null);
const period = ref<Period>('daily');
const timescale = ref(30);
const seriesLoading = ref(false);
const seriesErr = ref('');
const errs = ref<string[]>([]);
const updatedAt = ref('');

const equityRef = ref<EchartsUIType>();
const seriesRef = ref<EchartsUIType>();
const { renderEcharts: renderEquity } = useEcharts(equityRef);
const { renderEcharts: renderSeries } = useEcharts(seriesRef);

/* ══════════ 通用格式化 ══════════ */
const isNum = (v: any) =>
  v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
const fmt = (v: any, n = 2) => (isNum(v) ? Number(v).toFixed(n) : '—');
const signed = (v: any, n = 2) =>
  isNum(v) ? `${Number(v) > 0 ? '+' : ''}${Number(v).toFixed(n)}` : '—';
const signedPct = (v: any, n = 2) =>
  isNum(v) ? `${Number(v) > 0 ? '+' : ''}${Number(v).toFixed(n)}%` : '—';
/** 项目约定：红色 = 盈利，绿色 = 亏损 */
const cls = (v: any) =>
  Number(v) > 0 ? 'text-red-500' : Number(v) < 0 ? 'text-emerald-500' : '';

/** 秒 → 可读时长 */
function humanDuration(v: any): string {
  if (!isNum(v)) return '—';
  const s = Number(v);
  if (s < 60) return `${s.toFixed(1)} 秒`;
  const m = s / 60;
  if (m < 60) return `${m.toFixed(1)} 分钟`;
  const h = m / 60;
  if (h < 24) return `${h.toFixed(1)} 小时`;
  return `${(h / 24).toFixed(1)} 天`;
}

/**
 * Freqtrade 的时间戳是 UTC；字符串形式不带时区，这里显式按 UTC 解析，
 * 否则浏览器会把 UTC 当本地时间，净值曲线时间整体偏移。
 */
function toDate(v: any): Date | null {
  if (v === null || v === undefined || v === '') return null;
  if (typeof v === 'number' || /^-?\d+(?:\.\d+)?$/.test(String(v))) {
    const n = Number(v);
    if (!Number.isFinite(n)) return null;
    // 秒 / 毫秒自适应
    return new Date(Math.abs(n) < 1e11 ? n * 1000 : n);
  }
  const raw = String(v).trim();
  const iso = /(?:z|[+-]\d{2}:?\d{2})$/i.test(raw)
    ? raw
    : `${raw.replace(' ', 'T')}Z`;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

function fmtHistTime(v: any): string {
  const d = toDate(v);
  if (!d) return v === null || v === undefined || v === '' ? '—' : String(v);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(
    d.getHours(),
  )}:${p(d.getMinutes())}`;
}

/* ══════════ ① 账户概览 KPI ══════════ */
const modeText = computed(() => {
  const note = String(balance.value?.note ?? '');
  if (/simulat/i.test(note)) return 'dry-run 模拟盘';
  return note || '实盘账户';
});

const kpis = computed(() => {
  const p = profit.value;
  const b = balance.value;
  const sym = b?.symbol ?? b?.stake ?? '';
  return [
    {
      label: '总收益（已平仓）',
      value: signed(p?.profit_closed_coin),
      suffix: sym,
      sub: `已平仓收益率 ${signedPct(p?.profit_closed_percent)} · 含持仓浮盈 ${signed(
        p?.profit_all_coin,
      )}`,
      color: cls(p?.profit_closed_coin),
    },
    {
      label: '胜率',
      value: isNum(p?.winrate) ? (Number(p?.winrate) * 100).toFixed(1) : '—',
      suffix: '%',
      sub: `${p?.winning_trades ?? '—'} 胜 / ${p?.losing_trades ?? '—'} 负`,
      color: '',
    },
    {
      label: '已平仓笔数',
      value: p?.closed_trade_count ?? '—',
      suffix: '笔',
      sub: `累计成交 ${p?.trade_count ?? '—'} 笔`,
      color: '',
    },
    {
      label: '盈利 / 亏损',
      value: `${p?.winning_trades ?? '—'} / ${p?.losing_trades ?? '—'}`,
      suffix: '笔',
      sub: `按已平仓 ${p?.closed_trade_count ?? '—'} 笔统计`,
      color: '',
    },
    {
      label: '起始资金',
      value: fmt(b?.starting_capital),
      suffix: sym,
      sub: `相对起始 ${signedPct(b?.starting_capital_pct)}`,
      color: cls(b?.starting_capital_pct),
    },
    {
      label: '当前总资产',
      value: fmt(b?.total),
      suffix: sym,
      sub: `USDT 口径 ${fmt(b?.value)} · ${modeText.value}`,
      color: '',
    },
  ];
});

/* ══════════ ② 实盘净值曲线 ══════════ */
const histColumns = computed(() => (hist.value?.columns ?? []).map(String));

/** 按列名猜列：先精确匹配，再退化为包含关键字 */
function pickColumn(
  cols: string[],
  exact: string[],
  keywords: string[],
  banned: string[] = [],
): number {
  const low = cols.map((c) => c.toLowerCase());
  for (const e of exact) {
    const i = low.indexOf(e);
    if (i >= 0) return i;
  }
  for (const k of keywords) {
    const i = low.findIndex(
      (c) => c.includes(k) && !banned.some((b) => c.includes(b)),
    );
    if (i >= 0) return i;
  }
  return -1;
}

/**
 * 时间列与余额列的下标。
 * 实际接口返回的是 ["date", "__date_ts", "total_quote"]：
 * 余额列名叫 total_quote，不含 balance/value，所以关键字里必须带上 quote/total。
 */
const histLayout = computed(() => {
  const cols = histColumns.value;
  if (!cols.length) return null;
  const banned = ['pct', 'ratio', 'profit', 'rate', 'coin'];
  const d = pickColumn(
    cols,
    ['date', 'datetime', 'timestamp', 'time'],
    ['timestamp', 'date', 'time'],
    [],
  );
  const v = pickColumn(
    cols,
    ['total_quote', 'total_value', 'total', 'balance', 'value', 'equity'],
    ['balance', 'equity', 'net_value', 'quote', 'total', 'value'],
    [...banned, '_ts'],
  );
  if (d < 0 || v < 0 || d === v) return null;
  return { d, v };
});

const equityPoints = computed(() => {
  const layout = histLayout.value;
  const rows = hist.value?.data ?? [];
  if (!layout || !rows.length) return [];
  return rows
    .map((r) => ({
      t: fmtHistTime(r?.[layout.d]),
      v: Number(r?.[layout.v]),
    }))
    .filter((p) => Number.isFinite(p.v))
    .sort((a, b) => a.t.localeCompare(b.t));
});

const captureStart = computed(() => {
  const ts = hist.value?.capture_start_ts;
  return isNum(ts) && Number(ts) > 0 ? fmtHistTime(ts) : '';
});

function drawEquity() {
  const pts = equityPoints.value;
  if (pts.length < 2) return;
  renderEquity({
    grid: { bottom: 24, containLabel: true, left: 60, right: 20, top: 30 },
    series: [
      {
        areaStyle: { color: 'rgba(59,130,246,0.18)' },
        data: pts.map((p) => p.v),
        itemStyle: { color: '#3b82f6' },
        name: '账户净值',
        showSymbol: false,
        smooth: true,
        type: 'line',
      },
    ],
    tooltip: { trigger: 'axis' },
    xAxis: {
      axisLabel: { fontSize: 10, formatter: (v: string) => String(v).slice(5) },
      boundaryGap: false,
      data: pts.map((p) => p.t),
      type: 'category',
    },
    yAxis: { scale: true, type: 'value' },
  });
}

/* ══════════ ③ 日 / 周 / 月盈亏 ══════════ */
const timescaleUnit = computed(() =>
  period.value === 'daily' ? '天' : period.value === 'weekly' ? '周' : '月',
);
const timescaleOptions = computed(() =>
  TIMESCALES.map((v) => ({ label: `近 ${v} ${timescaleUnit.value}`, value: v })),
);

const seriesRows = computed(() => series.value?.data ?? []);
/** 图表按时间正序（接口返回的是倒序，最新在前） */
const chartRows = computed(() => [...seriesRows.value].reverse());

const seriesSummary = computed(() => {
  const rows = seriesRows.value;
  const total = rows.reduce((a, r) => a + (Number(r?.abs_profit) || 0), 0);
  const trades = rows.reduce((a, r) => a + (Number(r?.trade_count) || 0), 0);
  return { total, trades, count: rows.length };
});

function drawSeries() {
  const rows = chartRows.value;
  if (!rows.length) return;
  renderSeries({
    grid: { bottom: 24, containLabel: true, left: 60, right: 20, top: 30 },
    series: [
      {
        data: rows.map((r) => ({
          // 与页面文字一致：盈利红、亏损绿
          itemStyle: {
            color: Number(r?.abs_profit) >= 0 ? '#f0424f' : '#12b886',
          },
          value: Number(r?.abs_profit) || 0,
        })),
        name: '绝对盈亏',
        type: 'bar',
      },
    ],
    tooltip: {
      formatter: (params: any) => {
        const first = Array.isArray(params) ? params[0] : params;
        const row = rows[first?.dataIndex ?? 0];
        if (!row) return '';
        return `${row.date}<br/>绝对盈亏 ${signed(row.abs_profit)}<br/>相对盈亏 ${signedPct(
          Number(row.rel_profit) * 100,
        )}<br/>成交 ${row.trade_count ?? '—'} 笔`;
      },
      trigger: 'axis',
    },
    xAxis: {
      axisLabel: { fontSize: 10 },
      data: rows.map((r) => String(r?.date ?? '')),
      type: 'category',
    },
    yAxis: { scale: true, type: 'value' },
  });
}

const seriesColumns = [
  { dataIndex: 'date', key: 'date', title: '日期', width: 110 },
  {
    align: 'right' as const,
    dataIndex: 'abs_profit',
    key: 'abs_profit',
    title: '绝对盈亏',
  },
  {
    align: 'right' as const,
    dataIndex: 'rel_profit',
    key: 'rel_profit',
    title: '相对盈亏%',
  },
  {
    align: 'right' as const,
    dataIndex: 'starting_balance',
    key: 'starting_balance',
    title: '起始余额',
  },
  {
    align: 'right' as const,
    dataIndex: 'trade_count',
    key: 'trade_count',
    title: '成交数',
    width: 90,
  },
];

const seriesUnitText = computed(() => {
  const s = series.value;
  return s?.stake_currency
    ? `${s.stake_currency}${s.fiat_display_currency ? ` / ${s.fiat_display_currency}` : ''}`
    : '';
});

async function loadSeries() {
  seriesLoading.value = true;
  try {
    const res = await PERIOD_API[period.value](timescale.value);
    series.value = res ?? null;
    seriesErr.value = '';
  } catch (error) {
    seriesErr.value = errText(error);
  } finally {
    seriesLoading.value = false;
  }
  drawSeries();
}

/* ══════════ ④ 出场原因统计 ══════════ */
const exitRows = computed(() =>
  Object.entries(stats.value?.exit_reasons ?? {})
    .map(([reason, v]: [string, any]) => {
      const wins = Number(v?.wins ?? 0);
      const losses = Number(v?.losses ?? 0);
      const draws = Number(v?.draws ?? 0);
      const total = wins + losses + draws;
      return {
        draws,
        losses,
        reason,
        total,
        winrate: total ? `${((wins / total) * 100).toFixed(1)}%` : '—',
        wins,
      };
    })
    .sort((a, b) => b.total - a.total),
);

const DURATION_LABEL: Record<string, string> = {
  avg: '平均持仓',
  draws: '持平单平均持仓',
  losses: '亏损单平均持仓',
  max: '最长持仓',
  min: '最短持仓',
  total: '累计持仓',
  wins: '盈利单平均持仓',
};

const durationRows = computed(() =>
  Object.entries(stats.value?.durations ?? {}).map(([k, v]: [string, any]) => ({
    human: humanDuration(v),
    key: DURATION_LABEL[k] ?? k,
    raw: isNum(v) ? `${Number(v).toFixed(0)} 秒` : '—',
  })),
);

const exitColumns = [
  { dataIndex: 'reason', key: 'reason', title: '出场原因' },
  { align: 'right' as const, dataIndex: 'wins', key: 'wins', title: '盈利' },
  { align: 'right' as const, dataIndex: 'losses', key: 'losses', title: '亏损' },
  { align: 'right' as const, dataIndex: 'draws', key: 'draws', title: '持平' },
  { align: 'right' as const, dataIndex: 'total', key: 'total', title: '合计' },
  { align: 'right' as const, dataIndex: 'winrate', key: 'winrate', title: '胜率' },
];

/* ══════════ ⑤ 四张归因表 ══════════ */
interface AttrRow {
  count: number | string;
  name: string;
  profitAbs: any;
  profitPct: any;
}

/** 统一成「名称 / 笔数 / 盈亏 / 收益率%」，并按 profit_abs 降序 */
function normRows(
  rows: FtStatRow[] | null,
  field: 'enter_tag' | 'exit_reason' | 'mix_tag' | 'pair',
): AttrRow[] {
  return (rows ?? [])
    .map((r) => ({
      count: r?.count ?? '—',
      name: String((r as any)?.[field] ?? '—'),
      profitAbs: r?.profit_abs,
      profitPct: r?.profit_pct,
    }))
    .sort((a, b) => field === 'pair'
      ? compareCoins(a.name, b.name)
      : (Number(b.profitAbs) || 0) - (Number(a.profitAbs) || 0));
}

const attrTables = computed(() => [
  { field: 'pair', rows: normRows(perf.value, 'pair'), title: '分币对绩效' },
  {
    field: 'enter_tag',
    rows: normRows(entryStats.value, 'enter_tag'),
    title: '入场标签归因',
  },
  {
    field: 'exit_reason',
    rows: normRows(exitStats.value, 'exit_reason'),
    title: '出场原因归因',
  },
  {
    field: 'mix_tag',
    rows: normRows(mixStats.value, 'mix_tag'),
    title: '组合标签归因（入场 × 出场）',
  },
]);

const attrColumns = [
  { dataIndex: 'name', key: 'name', title: '名称' },
  {
    align: 'right' as const,
    dataIndex: 'count',
    key: 'count',
    title: '笔数',
    width: 80,
  },
  {
    align: 'right' as const,
    dataIndex: 'profitAbs',
    key: 'profitAbs',
    title: '盈亏',
    width: 120,
  },
  {
    align: 'right' as const,
    dataIndex: 'profitPct',
    key: 'profitPct',
    title: '收益率%',
    width: 110,
  },
];

/* ══════════ 轮询 ══════════ */
async function load() {
  const fails: string[] = [];
  /** 单个接口失败只记录并返回 null，不影响其它板块 */
  const safe = async <T,>(
    label: string,
    fn: () => Promise<T>,
  ): Promise<null | T> => {
    try {
      return await fn();
    } catch (error) {
      fails.push(`${label}：${errText(error)}`);
      return null;
    }
  };

  const [p, b, h, s, pf, en, ex, mx] = await Promise.all([
    safe('盈亏总览 /v1/profit', () => getProfit()),
    safe('账户余额 /v1/balance', () => getBalance()),
    safe('净值历史 /v1/historic_balance', () => getHistoricBalance()),
    safe('出场统计 /v1/stats', () => getStats()),
    safe('分币对绩效 /v1/performance', () => getPerformance()),
    safe('入场标签 /v1/entries', () => getEntryStats()),
    safe('出场原因 /v1/exits', () => getExitStats()),
    safe('组合标签 /v1/mix_tags', () => getMixTagStats()),
  ]);

  // 失败时保留上一次成功的数据，避免轮询抖动把页面闪成空态；
  // 首次加载就失败时各板块保持 null，模板里显示「—」或空态文案。
  if (p) profit.value = p;
  if (b) balance.value = b;
  if (h) hist.value = h;
  if (s) stats.value = s;
  if (pf) perf.value = pf;
  if (en) entryStats.value = en;
  if (ex) exitStats.value = ex;
  if (mx) mixStats.value = mx;
  if (h) drawEquity();

  errs.value = fails;
  updatedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  await loadSeries();
}

watch([period, timescale], () => {
  loadSeries();
});

let timer: any = null;
onMounted(() => {
  load();
  timer = setInterval(() => {
    load();
  }, 30_000);
});
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <div class="p-4">
    <!-- ① 口径提示 -->
    <Alert type="info" show-icon>
      <template #message>
        <span class="font-medium">本页为实盘口径，与「回测明细」页的回测口径不同</span>
        <Tag class="ml-2" color="blue">{{ modeText }}</Tag>
        <span class="ml-2 text-xs font-normal text-gray-400">
          每 30 秒自动刷新<template v-if="updatedAt"> · 最后更新 {{ updatedAt }}</template>
        </span>
      </template>
      <template #description>
        <span class="text-xs">
          数据全部来自 Freqtrade 实盘统计接口（/v1/profit、/v1/balance、/v1/historic_balance、
          /v1/daily、/v1/stats、/v1/performance 等）。「回测明细」页的曲线是策略在历史数据上的
          模拟产物，与机器人真实运行产生的成交、余额变动无关。
        </span>
      </template>
    </Alert>

    <!-- 接口异常提示：任一接口失败只降级对应板块 -->
    <Alert v-if="errs.length" class="mt-2" show-icon type="warning">
      <template #message>
        部分实盘接口调用失败，相关板块已降级（其余板块不受影响）
      </template>
      <template #description>
        <div v-for="e in errs" :key="e" class="text-xs">{{ e }}</div>
      </template>
    </Alert>

    <!-- ② 账户概览 -->
    <Row :gutter="[12, 12]" class="mt-3">
      <Col v-for="c in kpis" :key="c.label" :lg="4" :md="8" :sm="12" :xs="12">
        <Card :bordered="false" class="h-full shadow-sm">
          <div class="text-xs text-gray-500">{{ c.label }}</div>
          <div class="mt-1 text-2xl font-semibold">
            <span :class="c.color">{{ c.value }}</span>
            <span class="ml-1 text-xs font-normal text-gray-400">{{ c.suffix }}</span>
          </div>
          <div class="mt-1 text-xs text-gray-400">{{ c.sub }}</div>
        </Card>
      </Col>
    </Row>

    <!-- ③ 实盘净值曲线 -->
    <Card :bordered="false" class="mt-3 shadow-sm" title="实盘净值曲线">
      <template #extra>
        <span class="text-xs text-gray-400">
          /v1/historic_balance · {{ equityPoints.length }} 个采样点<template
            v-if="captureStart"
          >
            · 数据采集自 {{ captureStart }}</template>
        </span>
      </template>
      <EchartsUI v-if="equityPoints.length >= 2" ref="equityRef" height="300px" />
      <div v-else class="py-10 text-center text-sm text-gray-400">
        <div>暂无净值历史 —— 机器人按周期记录钱包余额，采集到数据后此处显示实盘净值曲线</div>
        <div v-if="histColumns.length" class="mt-2 text-xs">
          接口返回的列名：
          <span class="font-mono">{{ histColumns.join(' / ') }}</span>
          <template v-if="!histLayout"> （未能从中识别出时间列与余额列，已跳过绘图）</template>
        </div>
        <div v-else class="mt-2 text-xs">
          接口未返回任何列（可能未登录或接口不可用，请查看上方告警）
        </div>
      </div>
    </Card>

    <!-- ④ 日 / 周 / 月盈亏 -->
    <Card :bordered="false" class="mt-3 shadow-sm" title="盈亏分解（日 / 周 / 月）">
      <template #extra>
        <span class="mr-2 text-xs text-gray-400">
          合计 {{ signed(seriesSummary.total) }}<template v-if="seriesUnitText">
            {{ seriesUnitText }}</template>
          · {{ seriesSummary.trades }} 笔 · {{ seriesSummary.count }} 个周期
        </span>
        <Select
          v-model:value="timescale"
          :options="timescaleOptions"
          size="small"
          style="width: 130px"
        />
        <Radio.Group
          v-model:value="period"
          class="ml-2"
          button-style="solid"
          size="small"
        >
          <Radio.Button value="daily">日</Radio.Button>
          <Radio.Button value="weekly">周</Radio.Button>
          <Radio.Button value="monthly">月</Radio.Button>
        </Radio.Group>
      </template>

      <div v-if="seriesErr" class="mb-2 text-xs text-orange-500">
        盈亏分解接口调用失败：{{ seriesErr }}
      </div>
      <Spin :spinning="seriesLoading">
        <EchartsUI v-if="chartRows.length" ref="seriesRef" height="280px" />
        <div v-else class="py-10 text-center text-sm text-gray-400">
          暂无{{ period === 'daily' ? '日' : period === 'weekly' ? '周' : '月' }}度盈亏数据
          —— 该区间内没有已平仓交易
        </div>
        <Table
          v-if="seriesRows.length"
          class="mt-3"
          :columns="seriesColumns"
          :data-source="seriesRows"
          :pagination="false"
          row-key="date"
          size="small"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'abs_profit'">
              <span class="font-mono text-xs" :class="cls(record.abs_profit)">
                {{ signed(record.abs_profit) }}
              </span>
            </template>
            <template v-else-if="column.key === 'rel_profit'">
              <span class="font-mono text-xs" :class="cls(record.rel_profit)">
                {{ signedPct(Number(record.rel_profit) * 100) }}
              </span>
            </template>
            <template v-else-if="column.key === 'starting_balance'">
              <span class="font-mono text-xs">{{ fmt(record.starting_balance) }}</span>
            </template>
            <template v-else-if="column.key === 'trade_count'">
              <span class="text-xs">{{ record.trade_count ?? '—' }}</span>
            </template>
          </template>
        </Table>
      </Spin>
    </Card>

    <!-- ⑤ 出场原因统计 + 持仓时长 -->
    <Card :bordered="false" class="mt-3 shadow-sm" title="出场原因统计">
      <template #extra>
        <span class="text-xs text-gray-400">
          /v1/stats · {{ exitRows.length }} 类出场原因（按已平仓交易统计）
        </span>
      </template>
      <Row :gutter="[16, 16]">
        <Col :lg="15" :xs="24">
          <Table
            v-if="exitRows.length"
            :columns="exitColumns"
            :data-source="exitRows"
            :pagination="false"
            row-key="reason"
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'reason'">
                <Tag>{{ record.reason }}</Tag>
              </template>
              <template v-else-if="column.key === 'wins'">
                <span class="font-mono text-xs text-red-500">{{ record.wins }}</span>
              </template>
              <template v-else-if="column.key === 'losses'">
                <span class="font-mono text-xs text-emerald-500">
                  {{ record.losses }}
                </span>
              </template>
              <template v-else-if="column.key === 'draws'">
                <span class="font-mono text-xs text-gray-500">{{ record.draws }}</span>
              </template>
              <template v-else-if="column.key === 'total'">
                <span class="font-mono text-xs font-semibold">{{ record.total }}</span>
              </template>
            </template>
          </Table>
          <div v-else class="py-10 text-center text-sm text-gray-400">
            暂无出场原因统计 —— 当前没有已平仓交易
          </div>
        </Col>
        <Col :lg="9" :xs="24">
          <div class="mb-2 text-xs text-gray-500">
            持仓时长（durations，秒 → 可读时长）
          </div>
          <Table
            v-if="durationRows.length"
            :columns="[
              { dataIndex: 'key', key: 'key', title: '口径' },
              { dataIndex: 'raw', key: 'raw', title: '秒', align: 'right' },
              { dataIndex: 'human', key: 'human', title: '可读', align: 'right' },
            ]"
            :data-source="durationRows"
            :pagination="false"
            row-key="key"
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'raw'">
                <span class="font-mono text-xs text-gray-400">{{ record.raw }}</span>
              </template>
              <template v-else-if="column.key === 'human'">
                <span class="font-mono text-xs">{{ record.human }}</span>
              </template>
            </template>
          </Table>
          <div v-else class="py-10 text-center text-sm text-gray-400">
            暂无持仓时长数据
          </div>
        </Col>
      </Row>
    </Card>

    <!-- ⑥ 四张归因表 -->
    <Row :gutter="[12, 12]" class="mt-3">
      <Col v-for="t in attrTables" :key="t.field" :lg="12" :xs="24">
        <Card :bordered="false" class="h-full shadow-sm" :title="t.title">
          <template #extra>
            <span class="text-xs text-gray-400">{{ t.rows.length }} 行 · 按盈亏降序</span>
          </template>
          <Table
            v-if="t.rows.length"
            :columns="attrColumns"
            :data-source="t.rows"
            :pagination="false"
            :scroll="{ y: 260 }"
            row-key="name"
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'name'">
                <span class="font-mono text-xs">{{ record.name }}</span>
              </template>
              <template v-else-if="column.key === 'profitAbs'">
                <span class="font-mono text-xs" :class="cls(record.profitAbs)">
                  {{ signed(record.profitAbs) }}
                </span>
              </template>
              <template v-else-if="column.key === 'profitPct'">
                <span class="font-mono text-xs" :class="cls(record.profitPct)">
                  {{ signedPct(record.profitPct) }}
                </span>
              </template>
            </template>
          </Table>
          <div v-else class="py-10 text-center text-sm text-gray-400">
            暂无数据 —— 该维度需要已平仓交易
          </div>
        </Card>
      </Col>
    </Row>

    <div class="mt-3 text-center text-xs text-gray-400">
      本页为只读统计，数据每 30 秒自动刷新<template v-if="updatedAt">
        · 最后更新 {{ updatedAt }}
</template>
    </div>
  </div>
</template>

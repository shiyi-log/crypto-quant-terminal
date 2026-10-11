<script lang="ts" setup>
import { sortCoins } from '../utils/coinOrder';
import { shortOrderTime, tradeDisplayId } from '../utils/orderDisplayId';
/**
 * 总览 —— 一屏看全：账户 / 实盘策略 / 持仓 / 研究结论 / 回测曲线
 *
 * 数据源（全部走认证代理 :8890）：
 *   /api/v1/show_config  策略配置        /api/v1/profit      盈亏
 *   /api/v1/balance      余额            /api/v1/status      持仓
 *   /api/v1/trades       成交            /run_progress.json  回测进度
 *   /api/locals/ops      运维（波动率/因子健康度）
 *   /api/locals/iteration 模型迭代（走查结果）
 *   /api/locals/research 研究方向结论
 */
import { computed, onMounted, onUnmounted, ref } from 'vue';


import { Alert, Card, Col, Progress, Row, Table, Tag } from 'ant-design-vue';

import {
  errText,
  getBackgroundJobs,
  getBalance,
  getConfig,
  getIteration,
  getOps,
  getOpenTrades,
  getProfit,
  getResearch,
  getResearchProgress,
  getServices,
  getRunProgress,
  getTrades,
  getWhitelist,
} from '#/api/freqtrade';

import TaskProgress from '#/views/quant/components/TaskProgress.vue';

const cfg = ref<Record<string, any>>({});
const profit = ref<Record<string, any>>({});
const bal = ref<Record<string, any>>({});
const progress = ref<any>(null);
const researchProg = ref<any>(null);
const jobs = ref<any[]>([]);
const services = ref<any>(null);
const positions = ref<any[]>([]);
const trades = ref<any[]>([]);
const ops = ref<any>(null);
const it = ref<any>(null);
const rs = ref<any>(null);
const whitelist = ref<string[]>([]);
const loading = ref(false);
const loaded = ref<Record<string, boolean>>({});
const loadErrors = ref<string[]>([]);


const up = (v: any) => Number(v) > 0;
const cls = (v: any) =>
  Number(v) > 0 ? 'text-red-500' : Number(v) < 0 ? 'text-emerald-500' : '';
const fmt = (v: any, n = 2) =>
  v === null || v === undefined || Number.isNaN(Number(v))
    ? '—'
    : Number(v).toFixed(n);

/* ══════════ 第一行：账户 KPI ══════════ */
const kpis = computed(() => [
  {
    label: '总资产',
    value: fmt(bal.value.total),
    suffix: bal.value.symbol ?? 'USDT',
    sub: `起始 ${fmt(bal.value.starting_capital)}`,
    color: '',
  },
  {
    label: '总盈亏',
    value: (up(profit.value.profit_all_coin) ? '+' : '') + fmt(profit.value.profit_all_coin),
    suffix: bal.value.symbol ?? 'USDT',
    sub: `${up(profit.value.profit_all_percent) ? '+' : ''}${fmt(profit.value.profit_all_percent)}%`,
    color: cls(profit.value.profit_all_coin),
  },
  {
    label: '已平仓盈亏',
    value: (up(profit.value.profit_closed_coin) ? '+' : '') + fmt(profit.value.profit_closed_coin),
    suffix: bal.value.symbol ?? 'USDT',
    sub: `${profit.value.winning_trades ?? '—'} 胜 / ${profit.value.losing_trades ?? '—'} 负`,
    color: cls(profit.value.profit_closed_coin),
  },
  {
    label: '当前持仓',
    value: loaded.value.positions ? String(positions.value.length) : '—',
    suffix: `笔 / 上限 ${Number(cfg.value.max_open_trades) === -1 ? '不限' : cfg.value.max_open_trades ?? '—'}`,
    sub: `已用 ${fmt(openStake.value)} USDT`,
    color: '',
  },
  {
    label: '胜率',
    value: Number(profit.value.closed_trade_count) > 0
      ? fmt(Number(profit.value.winrate) * 100, 1)
      : loaded.value.profit ? '暂无平仓样本' : '未知',
    suffix: Number(profit.value.closed_trade_count) > 0 ? '%' : '',
    sub: `共 ${profit.value.closed_trade_count ?? '—'} 笔已平仓`,
    color: '',
  },
  {
    label: '运行状态',
    value: cfg.value.state === 'running' ? '运行中' : (cfg.value.state ?? '—'),
    suffix: '',
    sub: loaded.value.config ? (cfg.value.dry_run ? '干跑模拟' : '实盘') : '等待交易状态',
    color: cfg.value.state === 'running' ? 'text-emerald-500' : 'text-gray-400',
  },
]);

const openStake = computed(() => loaded.value.positions
  ? positions.value.reduce((a, t) => a + (t.stake_amount ?? 0), 0)
  : null);


/** 直接汇总交易引擎的浮动盈亏金额；缺失数据不按零处理。 */
const openProfit = computed(() => {
  if (!loaded.value.positions) return null;
  if (positions.value.some((trade) => trade.profit_abs == null || !Number.isFinite(Number(trade.profit_abs)))) {
    return null;
  }
  return positions.value.reduce((sum, trade) => sum + Number(trade.profit_abs), 0);
});

/* ══════════ 第二行：策略与风控 ══════════ */
const strategy = computed(() => {
  const s = cfg.value;
  const v = ops.value?.vol_regime;
  const live = it.value?.live;
  return [
    { k: '策略', v: s.strategy ?? '—', sub: s.timeframe ? `周期 ${s.timeframe}` : '' },
    {
      // ⚠️ show_config 不返回白名单，改用 /v1/whitelist
      k: '交易对',
      v: whitelist.value.length ? `${whitelist.value.length} 个` : '—',
      sub: s.trading_mode ? `${s.trading_mode} / ${s.margin_mode ?? ''}` : '',
    },
    {
      k: '选币规则',
      v: live?.params?.top_n ? `强度前 ${live.params.top_n}` : '—',
      sub: live?.params?.target_exposure ? `目标敞口 ${(live.params.target_exposure * 100).toFixed(0)}%` : '',
    },
    {
      k: '波动率中枢',
      v: v ? `${v.vol_90d_pct}%` : '—',
      sub: v ? `历史分位 ${v.vol_90d_percentile}% · ${v.regime}` : '',
      color: v?.regime === '高波动' ? 'text-red-500' : 'text-emerald-500',
    },
    {
      k: '因子告警',
      v: ops.value ? String(ops.value.factors.filter((f: any) => f.alert).length) : '—',
      sub: ops.value ? `共 ${ops.value.factors.length} 个因子` : '',
      color:
        ops.value && ops.value.factors.some((f: any) => f.alert)
          ? 'text-orange-500'
          : 'text-emerald-500',
    },
    {
      k: '模型重估',
      v: ops.value ? (ops.value.retrain.need_retrain ? '⚠ 需要' : '✅ 不需要') : '—',
      sub: '基于因子漂移自动判定',
      color: ops.value?.retrain?.need_retrain ? 'text-orange-500' : 'text-emerald-500',
    },
  ];
});

/* ══════════ 第三行：研究结论 ══════════ */

/** 离场通道周期（来自实盘参数，不写死） */
const exitPeriod = computed(() => it.value?.live?.params?.exit_period ?? null);

/**
 * 「弱信号」判定：优先后端结构化标志 weak，老快照没有该字段时退回文案匹配。
 * 之前直接写 `status !== '弱信号(基线IC<0.03)'`，后端文案一改就静默失效。
 */
const isWeakFactor = (f: any) => f.weak ?? /弱信号/.test(String(f.status ?? ''));
const strongFactors = computed(() =>
  (ops.value?.factors ?? []).filter((f: any) => !isWeakFactor(f)),
);

/**
 * 最近成交的行。
 *
 * Freqtrade 的 /v1/trades 只返回【已平仓】成交 —— 一笔都没平时表格全空，
 * 但持仓里明明有开仓记录。这里把持仓中的开仓并进来，标记 is_open，
 * 前端显示为「持仓中」+ 浮动盈亏。
 */
const recentRows = computed(() => [
  ...positions.value.map((t: any) => ({ ...t, is_open: true })),
  ...trades.value,
]);

/** 持仓时长统计：只用真实已平仓交易算，无样本时不显示 */
const holdStats = computed(() => {
  const days = trades.value
    .filter((t: any) => t.open_date && t.close_date)
    .map(
      (t: any) =>
        (new Date(t.close_date).getTime() - new Date(t.open_date).getTime()) /
        86_400_000,
    )
    .filter((d: number) => Number.isFinite(d) && d >= 0)
    .sort((a, b) => a - b);
  if (!days.length) return null;
  return {
    mean: Math.round(days.reduce((a, b) => a + b, 0) / days.length),
    median: Math.round(days[Math.floor(days.length / 2)]!),
    n: days.length,
  };
});

const posColumns = [
  { key: 'display_id', title: '开仓订单编号', width: 190 },
  { dataIndex: 'pair', key: 'pair', title: '币对', width: 150 },
  { key: 'side', title: '方向', width: 70 },
  { key: 'open_rate', title: '开仓价', align: 'right' as const },
  { dataIndex: 'current_rate', key: 'current_rate', title: '现价', align: 'right' as const },
  { key: 'stake_amount', title: '投入', align: 'right' as const },
  { key: 'profit_pct', title: '浮动盈亏 (%)', align: 'right' as const },
  { key: 'profit_abs', title: '浮动盈亏 (USDT)', align: 'right' as const },
  { key: 'open_date', title: '开仓时间', width: 110 },
];

const tradeColumns = [
  { key: 'display_id', title: '开仓订单编号', width: 190 },
  { dataIndex: 'pair', key: 'pair', title: '币对', width: 140 },
  { key: 'side', title: '方向', width: 60 },
  { key: 'profit_pct', title: '收益率', align: 'right' as const },
  { key: 'profit_abs', title: '盈亏', align: 'right' as const },
  { key: 'exit_reason', title: '平仓原因', width: 100 },
  { key: 'close_date', title: '平仓时间', width: 110 },
];

/* ══════════ 资金曲线 ══════════ */

async function load() {
  if (loading.value) return;
  loading.value = true;
  const failures: string[] = [];
  const refresh = async <T,>(key: string, label: string, request: () => Promise<T>, apply: (value: T) => void) => {
    try {
      apply(await request());
      loaded.value = { ...loaded.value, [key]: true };
    } catch (error) {
      failures.push(`${label}：${errText(error)}`);
    }
  };
  try {
    await Promise.all([
      refresh('config', '交易状态', getConfig, (value) => { cfg.value = value; }),
      refresh('profit', '盈亏统计', getProfit, (value) => { profit.value = value; }),
      refresh('balance', '账户余额', getBalance, (value) => { bal.value = value; }),
      refresh('progress', '回测进度', getRunProgress, (value) => { progress.value = value; }),
      refresh('positions', '当前持仓', getOpenTrades, (value) => { positions.value = value; }),
      refresh('trades', '最近成交', () => getTrades(20), (value) => { trades.value = value.trades ?? []; }),
      refresh('ops', '运维快照', getOps, (value) => { ops.value = value; }),
      refresh('iteration', '模型迭代', getIteration, (value) => { it.value = value; }),
      refresh('research', '研究汇总', getResearch, (value) => { rs.value = value; }),
      refresh('researchProgress', '研究进度', getResearchProgress, (value) => { researchProg.value = value; }),
      refresh('jobs', '后台任务', getBackgroundJobs, (value) => { jobs.value = value; }),
      refresh('services', '程序运行状况', getServices, (value) => { services.value = value; }),
      refresh('whitelist', '交易对白名单', getWhitelist, (value) => { whitelist.value = value.whitelist ?? []; }),
    ]);
    loadErrors.value = failures;
  } finally {
    loading.value = false;
  }
}

/** 每个服务的一句状态说明（让它一眼看出「跑到哪」） */
function serviceNote(sv: any): string {
  const st = sv?.state;
  if (!st) return '';
  if (st.round !== undefined) return `第 ${st.round} 轮 · ${taskPercent(st.percent) === null ? '进度未知' : `${taskPercent(st.percent)}%`}`;
  if (st.rounds_done !== undefined) return `已完成 ${st.rounds_done} 轮`;
  if (st.summary_generated_at) return `数据更新 ${st.summary_generated_at}`;
  if (st.ok !== undefined) return st.ok ? '上次刷新成功' : '上次刷新失败';
  return '';
}

function taskPercent(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const percent = Number(value);
  return Number.isFinite(percent) ? Math.min(100, Math.max(0, percent)) : null;
}

function taskStatus(status: unknown): string {
  const labels: Record<string, string> = {
    running: '运行中',
    done: '已完成',
    failed: '失败',
    stopped: '已停止',
    idle: '待命',
    pending: '等待中',
    queued: '排队中',
    cancelled: '已取消',
    canceled: '已取消',
  };
  return typeof status === 'string' ? labels[status] ?? status : '状态未知';
}

/** 全部任务：回测/训练、模型迭代、后台任务 */
const allTasks = computed(() => {
  const out: any[] = [];
  if (progress.value) {
    out.push({
      key: 'run',
      name: '回测 / 训练',
      icon: '🧪',
      percent: taskPercent(progress.value.percent),
      status: progress.value.status,
      note: [progress.value.phase ?? progress.value.model,
             progress.value.current_pair].filter(Boolean).join(' · '),
    });
  }
  const r = researchProg.value?.progress;
  if (r) {
    out.push({
      key: 'research',
      name: '模型自动迭代',
      icon: '🔬',
      percent: taskPercent(r.percent),
      status: r.status,
      note: `第 ${r.round ?? '—'} 轮 · trial ${r.trial ?? '—'}/${r.total ?? '—'}` +
            (r.phase ? ` · ${r.phase}` : ''),
    });
  }
  for (const j of jobs.value || []) {
    out.push({
      key: `job-${j.job_id ?? j.id}`,
      name: j.kind ?? j.name ?? '后台任务',
      icon: '⚙️',
      percent: taskPercent(j.percent),
      status: j.status,
      note: j.message ?? j.phase ?? '',
    });
  }
  return out;
});

let timer: any = null;
onMounted(() => {
  load();
  timer = setInterval(load, 5000);
});
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <div class="p-4">
    <TaskProgress />
    <Alert v-if="loadErrors.length" class="mb-3" type="warning" show-icon
      message="部分数据刷新失败，保留上次成功数据；首次未读取的指标显示未知。"
      :description="loadErrors.join('；')" />

    <!-- ① 账户 KPI -->
    <Row :gutter="[12, 12]">
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

    <!-- ② 策略与风控 -->
    <Card :bordered="false" class="mt-3 shadow-sm" title="实盘策略与风控">
      <template #extra>
        <span class="text-xs text-gray-400">
          数据每 5 秒刷新 · 运维快照 {{ ops?.generated_at ?? '未生成' }}
        </span>
      </template>
      <Row :gutter="[12, 12]">
        <Col v-for="s in strategy" :key="s.k" :lg="4" :md="8" :sm="12" :xs="12">
          <div class="rounded-lg border border-gray-100 p-3 dark:border-gray-800">
            <div class="text-xs text-gray-500">{{ s.k }}</div>
            <div class="mt-1 text-base font-semibold" :class="s.color">{{ s.v }}</div>
            <div class="mt-0.5 text-[11px] text-gray-400">{{ s.sub }}</div>
          </div>
        </Col>
      </Row>
    </Card>

    <!-- ══════════ 整个程序的运行状况 ══════════ -->
    <Card
      v-if="services"
      :bordered="false"
      class="mt-3 shadow-sm"
      :title="`🖥 程序运行状况`"
    >
      <template #extra>
        <Tag :color="services.running === services.total ? 'green' : 'orange'">
          {{ services.running }}/{{ services.total }} 运行中
        </Tag>
        <span class="ml-2 text-xs text-gray-400">{{ services.generated_at }}</span>
      </template>
      <div class="grid grid-cols-2 gap-2 md:grid-cols-3 lg:grid-cols-5">
        <div
          v-for="sv in services.services"
          :key="sv.key"
          class="rounded-lg border p-2.5"
          :class="sv.running
            ? 'border-emerald-200 bg-emerald-50/40 dark:border-emerald-900 dark:bg-emerald-950/20'
            : 'border-red-200 bg-red-50/40 dark:border-red-900 dark:bg-red-950/20'"
        >
          <div class="flex items-center gap-1.5">
            <span>{{ sv.running ? '🟢' : '🔴' }}</span>
            <span class="text-xs font-medium">{{ sv.name }}</span>
          </div>
          <div class="mt-1 truncate font-mono text-[11px] text-gray-500">
            <template v-if="sv.port">{{ sv.port }} · </template>PID {{ sv.pid || '—' }}
          </div>
          <div
            v-if="serviceNote(sv)"
            class="mt-0.5 truncate text-[11px] text-blue-500"
            :title="serviceNote(sv)"
          >
            {{ serviceNote(sv) }}
          </div>
        </div>
      </div>
    </Card>

    <!-- ③ 当前持仓 -->
    <Card :bordered="false" class="mt-3 shadow-sm" title="当前持仓">
      <template #extra>
        <span class="text-xs text-gray-400">
          {{ loaded.positions ? positions.length : '—' }} 笔 · 占用 {{ fmt(openStake) }} USDT · 浮动盈亏
          <span :class="cls(openProfit)">
            {{ up(openProfit) ? '+' : '' }}{{ fmt(openProfit) }} USDT
          </span><template
            v-if="exitPeriod"
          >
            · 离场：反向突破 {{ exitPeriod }} 日通道
          </template>
        </span>
      </template>
      <Table
        v-if="positions.length"
        :columns="posColumns"
        :data-source="sortCoins(positions, (row) => row.pair)"
        :pagination="false"
        :scroll="{ x: 'max-content' }"
        row-key="trade_id"
        size="small"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'display_id'">
            <span class="font-mono text-xs">{{ tradeDisplayId(record) }}</span>
          </template>
          <template v-else-if="column.key === 'pair'">
            <span class="font-medium">{{ record.pair }}</span>
          </template>
          <template v-else-if="column.key === 'side'">
            <Tag :color="record.is_short ? 'green' : 'red'">
              {{ record.is_short ? '空' : '多' }}
            </Tag>
          </template>
          <template v-else-if="column.key === 'open_rate'">
            <span class="font-mono text-xs">{{ fmt(record.open_rate, 6) }}</span>
          </template>
          <template v-else-if="column.key === 'current_rate'">
            <span class="font-mono text-xs">{{ fmt(record.current_rate, 6) }}</span>
          </template>
          <template v-else-if="column.key === 'stake_amount'">
            <span class="font-mono text-xs">{{ fmt(record.stake_amount) }}</span>
          </template>
          <template v-else-if="column.key === 'profit_pct'">
            <span class="font-mono text-xs font-semibold"
                  :class="cls(record.profit_pct)">
              {{ up(record.profit_pct) ? '+' : '' }}{{ fmt(record.profit_pct) }}%
            </span>
          </template>
          <template v-else-if="column.key === 'profit_abs'">
            <span class="font-mono text-xs font-semibold" :class="cls(record.profit_abs)">
              {{ up(record.profit_abs) ? '+' : '' }}{{ fmt(record.profit_abs) }}
            </span>
          </template>
          <template v-else-if="column.key === 'open_date'">
            <span class="text-xs">{{ shortOrderTime(record.open_timestamp, record.open_date) }}</span>
          </template>
        </template>
      </Table>
      <div v-else class="py-8 text-center text-sm text-gray-400">
        {{ loaded.positions ? '当前无持仓 —— 趋势策略等突破信号' : '持仓数据尚未读取，请查看刷新状态' }}
      </div>
    </Card>

    <Row :gutter="[12, 12]" class="mt-3">
      <Col :lg="12" :xs="24">
        <Card :bordered="false" class="h-full shadow-sm" title="因子健康度">
          <template #extra>
            <span class="text-xs text-gray-400">
              {{ ops ? `${ops.factors.length} 个因子` : '' }}
            </span>
          </template>
          <div v-if="ops" class="space-y-1.5">
            <div
              v-for="f in strongFactors.slice(0, 5)"
              :key="f.factor"
              class="grid grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)_minmax(0,1fr)_max-content] items-center gap-3 text-xs"
            >
              <!-- 固定列宽、统一小数位，名称长短不会推挤后面的数值。 -->
              <span class="truncate font-mono" :title="f.factor">{{ f.factor }}</span>
              <span class="text-right tabular-nums text-gray-400">基线 {{ fmt(f.ic_base, 4) }}</span>
              <span class="text-right tabular-nums text-gray-400">近期 {{ fmt(f.ic_recent, 4) }}</span>
              <Tag class="m-0" :color="f.status === '正常' ? 'green' : 'orange'">{{ f.status }}</Tag>
            </div>
            <div v-if="!strongFactors.length"
                 class="py-4 text-center text-gray-400">
              全部因子基线 IC &lt; 0.03
            </div>
          </div>
          <div v-else class="py-8 text-center text-sm text-gray-400">暂无运维快照</div>
        </Card>
      </Col>

      <Col :lg="12" :xs="24">
        <Card :bordered="false" class="h-full shadow-sm" title="任务进度">
          <template #extra>
            <span class="text-xs text-gray-400">{{ allTasks.length }} 个任务</span>
          </template>
          <div v-if="allTasks.length" class="space-y-3">
            <div v-for="t in allTasks" :key="t.key">
              <div class="flex items-center gap-2 text-sm">
                <span>{{ t.icon }}</span>
                <span class="font-medium">{{ t.name }}</span>
                <Tag
                  :color="t.status === 'running' ? 'blue' : t.status === 'done' ? 'green' : t.status === 'failed' ? 'red' : 'default'"
                >
                  {{ taskStatus(t.status) }}
                </Tag>
                <span class="ml-auto font-mono text-xs">{{ t.percent === null ? '进度未知' : `${t.percent.toFixed(0)}%` }}</span>
              </div>
              <Progress
                v-if="t.percent !== null"
                :percent="t.percent"
                :status="t.status === 'failed' ? 'exception' : t.status === 'running' ? 'active' : 'normal'"
                size="small"
                class="mt-1"
              />
              <div v-if="t.note" class="mt-0.5 truncate text-xs text-gray-500" :title="t.note">{{ t.note }}</div>
            </div>
          </div>
          <div v-else class="py-8 text-center text-sm text-gray-400">当前无运行中的任务</div>
        </Card>
      </Col>
    </Row>

    <!-- ⑥ 最近成交 -->
    <Card :bordered="false" class="mt-3 shadow-sm" title="最近成交">
      <template #extra>
        <span class="text-xs text-gray-400">
          持仓 {{ positions.length }} 笔 · 已平仓 {{ trades.length }} 笔<template
            v-if="holdStats"
          >
            · 平均持有 {{ holdStats.mean }} 天（中位 {{ holdStats.median }} 天）
          </template>
        </span>
      </template>
      <Table
        v-if="recentRows.length"
        :columns="tradeColumns"
        :data-source="sortCoins(recentRows, (row) => row.pair)"
        :pagination="false"
        :scroll="{ x: 'max-content' }"
        row-key="trade_id"
        size="small"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'display_id'">
            <span class="font-mono text-xs">{{ tradeDisplayId(record) }}</span>
          </template>
          <template v-else-if="column.key === 'pair'">
            <span class="font-medium">{{ record.pair }}</span>
          </template>
          <template v-else-if="column.key === 'side'">
            <Tag :color="record.is_short ? 'green' : 'red'">
              {{ record.is_short ? '空' : '多' }}
            </Tag>
          </template>
          <template v-else-if="column.key === 'profit_pct'">
            <span class="font-mono text-xs font-semibold" :class="cls(record.profit_pct)">
              {{ up(record.profit_pct) ? '+' : '' }}{{ fmt(record.profit_pct) }}%
            </span>
          </template>
          <template v-else-if="column.key === 'profit_abs'">
            <span class="font-mono text-xs" :class="cls(record.profit_abs)">
              {{ up(record.profit_abs) ? '+' : '' }}{{ fmt(record.profit_abs) }}
            </span>
          </template>
          <template v-else-if="column.key === 'exit_reason'">
            <Tag v-if="record.is_open" color="warning">持仓中</Tag>
            <Tag v-else>{{ record.exit_reason }}</Tag>
          </template>
          <template v-else-if="column.key === 'close_date'">
            <span class="text-xs">
              {{ record.is_open ? '—' : shortOrderTime(record.close_timestamp, record.close_date) }}
            </span>
          </template>
        </template>
      </Table>
      <div v-else class="py-8 text-center text-sm text-gray-400">
        {{ loaded.positions && loaded.trades ? '暂无持仓与成交 —— 等待突破信号' : '成交数据尚未读取，请查看刷新状态' }}
      </div>
    </Card>
  </div>
</template>

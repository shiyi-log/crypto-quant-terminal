<script lang="ts" setup>
/**
 * 策略研究 —— 把全部研究结论汇总到面板
 *
 * 三块内容：
 *   1. 路线对比：每条研究方向的证据与结论（哪些已证伪、哪个推荐主攻）
 *   2. 模型对比：各 FreqAI 模型的表现 + 决策质量指标
 *   3. Carry 回测：参数扫描、强平风险、保证金缓冲情景
 */
import { computed, onMounted, onUnmounted, ref } from 'vue';

import { Card, Col, Row, Spin, Table, Tag, Tooltip } from 'ant-design-vue';

import { getResearch, getResearchStatus } from '#/api/freqtrade';

const data = ref<any>(null);
const rstatus = ref<any>(null);
const loading = ref(false);

/* ══════════ 板块清单：跳转标签与卡片标题的唯一来源 ══════════
 * 之前标签写死 7 个、卡片标题各自写死，跳转还靠
 * document.querySelectorAll('.ant-card-head-title') 做中文子串匹配 ——
 * 标题一改标签就静默失效。现在两边同源，滚动改用模板 ref。
 */
const SECTIONS = [
  { key: 'routes', label: '研究方向', title: '研究方向对比' },
  { key: 'models', label: '模型对比', title: 'FreqAI 模型对比（预测路线证伪证据）' },
  { key: 'carry', label: 'Carry', title: 'Carry 回测 · 收益拆解' },
  { key: 'liquidation', label: '强平风险', title: '⛔ 强平风险（决定成败）' },
  { key: 'sweep', label: '参数扫描', title: '参数扫描' },
  { key: 'buffers', label: '保证金缓冲', title: '加入真实保证金缓冲后的年化' },
  { key: 'schemes', label: '保证金方案', title: '💥 保证金方案对比 —— 逐小时模拟，含真实强平' },
];
const titleOf = (key: string) => SECTIONS.find((s) => s.key === key)?.title ?? '';

const sectionEls = ref<Record<string, any>>({});
const bindSection = (key: string) => (el: any) => {
  if (el) sectionEls.value[key] = el;
};
function jump(key: string) {
  const el = sectionEls.value[key];
  (el?.$el ?? el)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

const fmt = (v: any, n = 2) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(n);
const cls = (v: any) =>
  Number(v) > 0 ? 'text-red-500' : Number(v) < 0 ? 'text-emerald-500' : '';

/* ══════════ 结论：全部由数据推导，不在模板里写死 ══════════ */
const carry = computed<any>(() => data.value?.carry ?? null);
const cash = computed<any>(() => carry.value?.cash ?? null);

/** 主攻方向取 level==='good' 的路线，与总览页同源 */
const goodRoutes = computed(() =>
  (data.value?.routes ?? []).filter((r: any) => r.level === 'good'),
);

/** 最大单期涨幅（= 最坏情况缓冲所需的保证金比例） */
const maxBufferPct = computed(() => {
  const vals = (carry.value?.buffers ?? [])
    .map((b: any) => b.buffer_pct)
    .filter((v: any) => typeof v === 'number');
  return vals.length ? Math.max(...vals) : null;
});

/** 保证金方案对比 */
const schemes = computed<any[]>(() => carry.value?.schemes ?? []);
const unifiedSchemes = computed(() =>
  schemes.value.filter((s: any) => s.mode === 'unified'),
);
const isolatedSchemes = computed(() =>
  schemes.value.filter((s: any) => s.mode === 'isolated'),
);
const reserveSchemes = computed(() =>
  schemes.value.filter((s: any) => s.mode === 'reserve'),
);

const schemeTotals = computed(() => {
  const vals = schemes.value
    .map((s: any) => s.total_pct)
    .filter((v: any) => typeof v === 'number');
  return vals.length ? { min: Math.min(...vals), max: Math.max(...vals) } : null;
});

const unifiedAnnual = computed(() => {
  const vals = unifiedSchemes.value
    .map((s: any) => s.annual_pct)
    .filter((v: any) => typeof v === 'number');
  return vals.length ? { min: Math.min(...vals), max: Math.max(...vals) } : null;
});

/** 逐仓里【最低】的那档杠杆也会被强平吗 */
const isolatedLowestLiquidated = computed(() => {
  const rows = isolatedSchemes.value.filter((s: any) => (s.liquidations ?? 0) > 0);
  if (!rows.length) return null;
  return rows.reduce((a: any, b: any) => (b.leverage < a.leverage ? b : a));
});

/** 加备用金 vs 同杠杆逐仓 */
const reserveVsIsolated = computed(() => {
  const r = reserveSchemes.value[0];
  if (!r) return null;
  const base = isolatedSchemes.value.find((s: any) => s.leverage === r.leverage);
  return base ? { base, reserve: r } : null;
});

const unifiedNoLiquidation = computed(
  () =>
    unifiedSchemes.value.length > 0 &&
    unifiedSchemes.value.every((s: any) => !s.liquidations),
);

/**
 * 数据新鲜度。
 *
 * 这一页曾经「很久没更新」：build_research.py 是纯手工脚本，没有任何调度。
 * 现在由 bot/refresh_research.py 每 6 小时重建，页面按生成时间显式提示新旧，
 * 超过 12 小时标红，避免再次出现「看不出数据是旧的」。
 */
const staleness = computed(() => {
  const g = data.value?.generated_at;
  if (!g) return null;
  const t = new Date(String(g).replace(' ', 'T')).getTime();
  if (Number.isNaN(t)) return null;
  const hours = (Date.now() - t) / 3_600_000;
  const label =
    hours < 1 ? `${Math.max(1, Math.round(hours * 60))} 分钟前` : `${hours.toFixed(1)} 小时前`;
  return { hours, label, stale: hours > 12 };
});

const LEVEL: Record<string, { color: string; label: string }> = {
  bad: { color: 'red', label: '已证伪' },
  warn: { color: 'orange', label: '辅助' },
  good: { color: 'green', label: '推荐' },
};

const routeCols = [
  { dataIndex: 'name', key: 'name', title: '研究方向', width: 210 },
  { dataIndex: 'evidence', key: 'evidence', title: '实测证据' },
  { dataIndex: 'annual', key: 'annual', title: '年化', width: 120, align: 'center' as const },
  { key: 'verdict', title: '结论', width: 110, align: 'center' as const },
];

const modelCols = [
  { dataIndex: 'label', key: 'label', title: '模型', width: 170 },
  { dataIndex: 'trades', key: 'trades', title: '交易', width: 70, align: 'right' as const },
  { key: 'profit_pct', title: '收益率', width: 100, align: 'right' as const },
  { key: 'winrate', title: '胜率', width: 90, align: 'right' as const },
  { key: 'max_drawdown_pct', title: '最大回撤', width: 100, align: 'right' as const },
  { key: 'pred_ic', title: '预测-实际 IC', width: 120, align: 'right' as const },
  { key: 'sign_hit_rate', title: '方向命中率', width: 110, align: 'right' as const },
  { key: 'verdict', title: '结论', width: 90, align: 'center' as const },
];

const sweepCols = [
  { key: 'cfg', title: '配置', width: 200 },
  { key: 'annual_pct', title: '年化', align: 'right' as const },
  { key: 'total_pct', title: '累计', align: 'right' as const },
  { key: 'max_drawdown_pct', title: '回撤', align: 'right' as const },
  { key: 'win_rate', title: '按期胜率', align: 'right' as const },
];

const liqCols = computed(() => [
  { key: 'leverage', title: '杠杆', width: 80 },
  { key: 'threshold_pct', title: '强平阈值（币价涨幅）', align: 'right' as const },
  {
    key: 'liquidated_periods',
    title: `被强平期数 / ${cash.value?.periods ?? '—'}`,
    align: 'right' as const,
  },
  { key: 'violation_rate', title: '触发比例', align: 'right' as const },
]);

async function load() {
  if (!data.value) loading.value = true;
  try {
    const [d, s] = await Promise.all([
      getResearch(),
      getResearchStatus().catch(() => null),
    ]);
    data.value = d;
    rstatus.value = s;
  } finally {
    loading.value = false;
  }
}

let timer: any = null;
onMounted(() => {
  load();
  // 研究结论由 build_research.py 重新生成，页面必须自动跟上
  timer = setInterval(load, 60_000);
});
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <div class="p-4">
    <Spin :spinning="loading">
      <div v-if="!data" class="py-16 text-center text-muted-foreground">
        暂无研究汇总 —— 运行 <code class="mx-1">python build_research.py</code> 生成
      </div>

      <template v-else>
        <!-- 结论横幅 -->
        <Card :bordered="false" class="shadow-sm">
          <div class="flex flex-wrap items-center gap-3">
            <Tag color="green" class="!px-3 !py-1 text-sm">主攻方向</Tag>
            <span class="text-base font-semibold">
              {{ goodRoutes.map((r: any) => r.name).join(' · ') || '暂无结论' }}
            </span>
            <span v-if="goodRoutes.length" class="text-sm text-muted-foreground">
              {{ goodRoutes.map((r: any) => r.evidence).join('；') }} · 实测年化
              {{ goodRoutes.map((r: any) => r.annual).join(' · ') }}
            </span>
            <span class="ml-auto flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <Tag v-if="staleness" :color="staleness.stale ? 'orange' : 'default'">
                数据更新于 {{ staleness.label }}
              </Tag>
              <Tag v-if="rstatus?.ok === false" color="red">最近刷新失败</Tag>
              <Tag v-else-if="rstatus?.refresh_running" color="blue">刷新守护运行中</Tag>
              <span>生成于 {{ data.generated_at }}</span>
            </span>
          </div>
        </Card>

        <!-- 板块导航：标签与下方卡片标题同源（SECTIONS），不会各改各的 -->
        <div class="mt-3 flex flex-wrap items-center gap-2">
          <span class="text-xs text-muted-foreground">快速跳转：</span>
          <Tag
            v-for="sec in SECTIONS"
            :key="sec.key"
            class="cursor-pointer"
            @click="jump(sec.key)"
          >
            {{ sec.label }}
          </Tag>
        </div>

        <!-- 路线对比 -->
        <Card
          :ref="bindSection('routes')"
          :bordered="false"
          class="mt-4 shadow-sm"
          :title="titleOf('routes')"
        >
          <template #extra>
            <span class="text-xs text-muted-foreground">
              每条方向都经过了实测检验，不是文献引用
            </span>
          </template>
          <Table
            :columns="routeCols"
            :data-source="data.routes"
            :pagination="false"
            row-key="name"
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'name'">
                <span class="font-medium">{{ record.name }}</span>
              </template>
              <template v-else-if="column.key === 'annual'">
                <span class="font-mono text-xs">{{ record.annual }}</span>
              </template>
              <template v-else-if="column.key === 'verdict'">
                <Tag :color="LEVEL[record.level]?.color">
                  {{ LEVEL[record.level]?.label }}
                </Tag>
              </template>
            </template>
          </Table>
        </Card>

        <!-- 模型对比 -->
        <Card
          :ref="bindSection('models')"
          :bordered="false"
          class="mt-4 shadow-sm"
          :title="titleOf('models')"
        >
          <template #extra>
            <Tooltip title="IC = 模型预测值与实际收益的秩相关。为正说明预测方向正确；为负说明系统性判断反了。方向命中率 50% 才是随机水平。">
              <span class="cursor-help text-xs text-blue-500">指标说明</span>
            </Tooltip>
          </template>
          <Table
            :columns="modelCols"
            :data-source="data.models"
            :pagination="false"
            row-key="identifier"
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'label'">
                <span class="font-medium">{{ record.label }}</span>
              </template>
              <template v-else-if="column.key === 'profit_pct'">
                <span :class="cls(record.profit_pct)" class="font-semibold">
                  {{ record.profit_pct > 0 ? '+' : '' }}{{ fmt(record.profit_pct) }}%
                </span>
              </template>
              <template v-else-if="column.key === 'winrate'">
                {{ fmt(record.winrate) }}%
              </template>
              <template v-else-if="column.key === 'max_drawdown_pct'">
                <span class="text-emerald-500">{{ fmt(record.max_drawdown_pct) }}%</span>
              </template>
              <template v-else-if="column.key === 'pred_ic'">
                <span :class="cls(record.pred_ic)" class="font-mono font-semibold">
                  {{ fmt(record.pred_ic, 4) }}
                </span>
              </template>
              <template v-else-if="column.key === 'sign_hit_rate'">
                <span :class="record.sign_hit_rate >= 50 ? 'text-red-500' : 'text-emerald-500'"
                      class="font-semibold">
                  {{ fmt(record.sign_hit_rate) }}%
                </span>
              </template>
              <template v-else-if="column.key === 'verdict'">
                <Tag color="red">{{ record.verdict }}</Tag>
              </template>
            </template>
          </Table>
        </Card>

        <!-- Carry 回测 -->
        <Row :gutter="[12, 12]" class="mt-4">
          <Col :lg="12" :xs="24">
            <Card
              :ref="bindSection('carry')"
              :bordered="false"
              class="h-full shadow-sm"
              :title="titleOf('carry')"
            >
              <div v-if="cash && cash.funding_pct !== undefined" class="space-y-3">
                <div class="grid grid-cols-2 gap-3">
                  <div class="rounded-lg border border-gray-100 p-3 dark:border-gray-800">
                    <div class="text-xs text-muted-foreground">资金费收入（每期）</div>
                    <div class="text-lg font-semibold text-red-500">
                      +{{ cash.funding_pct }}%
                    </div>
                  </div>
                  <div class="rounded-lg border border-gray-100 p-3 dark:border-gray-800">
                    <div class="text-xs text-muted-foreground">基差盈亏（每期）</div>
                    <div class="text-lg font-semibold">
                      {{ cash.basis_pct }}%
                    </div>
                    <div class="text-[11px] text-muted-foreground">≈0，基差不是问题</div>
                  </div>
                  <div class="rounded-lg border border-gray-100 p-3 dark:border-gray-800">
                    <div class="text-xs text-muted-foreground">手续费滑点（每期）</div>
                    <div class="text-lg font-semibold text-emerald-500">
                      {{ cash.cost_pct }}%
                    </div>
                  </div>
                  <div class="rounded-lg border border-gray-100 p-3 dark:border-gray-800">
                    <div class="text-xs text-muted-foreground">净收益（每期）</div>
                    <div class="text-lg font-semibold text-red-500">
                      +{{ cash.net_pct }}%
                    </div>
                  </div>
                </div>
                <div>
                  <div class="mb-1 text-xs text-muted-foreground">
                    基差统计（永续/现货−1）：均值 {{ cash.basis_stats?.mean }}% ·
                    标准差 {{ cash.basis_stats?.std }}% ·
                    95% 区间 [{{ cash.basis_stats?.p95_low }}%,
                    {{ cash.basis_stats?.p95_high }}%]
                  </div>
                </div>
                <div>
                  <div class="mb-2 text-xs text-muted-foreground">
                    边际衰减趋势（折算年化口径）
                  </div>
                  <div class="flex gap-2">
                    <div
                      v-for="y in cash.yearly"
                      :key="y.year"
                      class="flex-1 rounded-lg border border-gray-100 p-2 text-center dark:border-gray-800"
                    >
                      <div class="text-xs text-muted-foreground">{{ y.year }}</div>
                      <div class="font-semibold" :class="cls(y.net_pct)">
                        {{ y.net_pct > 0 ? '+' : '' }}{{ y.net_pct }}%
                      </div>
                      <div class="text-[11px] text-muted-foreground">
                        年化 {{ y.annualized_pct }}%
                      </div>
                    </div>
                  </div>
                </div>
                <div class="rounded-lg bg-orange-50/60 p-3 text-xs dark:bg-orange-950/20">
                  <div class="font-medium text-orange-600">⚠️ 前瞻预期修正</div>
                  <div class="mt-1 text-muted-foreground">
                    回测均值 <b>{{ cash.backtest_avg_1x }}%</b>（1x）是被最近一年拉高的。
                    按当前实际运行速率，前瞻应取
                    <b class="text-orange-600">{{ cash.forward_annual_1x }}%</b>（1x）/
                    <b class="text-orange-600">{{ cash.forward_annual_3x }}%</b>（3x）。
                    边际正在被套利掉。
                  </div>
                </div>
              </div>
              <div v-else class="py-8 text-center text-sm text-muted-foreground">
                暂无 Carry 回测数据
              </div>
            </Card>
          </Col>

          <Col :lg="12" :xs="24">
            <Card
              :ref="bindSection('liquidation')"
              :bordered="false"
              class="h-full shadow-sm"
              :title="titleOf('liquidation')"
            >
              <div class="mb-2 text-xs text-muted-foreground">
                永续空头是独立保证金账户，币价涨到阈值即被强平 ——
                <span class="text-orange-500">现货腿的盈利救不了它</span>。
                <template v-if="maxBufferPct !== null">
                  回测区间内最大单期涨幅为
                  <span class="font-semibold">+{{ maxBufferPct }}%</span>（最坏情况缓冲即按此设定）。
                </template>
              </div>
              <Table
                :columns="liqCols"
                :data-source="carry?.liquidation ?? []"
                :pagination="false"
                row-key="leverage"
                size="small"
              >
                <template #bodyCell="{ column, record }">
                  <template v-if="column.key === 'leverage'">
                    <span class="font-semibold">{{ record.leverage }}x</span>
                  </template>
                  <template v-else-if="column.key === 'threshold_pct'">
                    涨 {{ record.threshold_pct }}%
                  </template>
                  <template v-else-if="column.key === 'liquidated_periods'">
                    <span :class="record.violation_rate > 30 ? 'text-red-500' : 'text-orange-500'"
                          class="font-semibold">
                      {{ record.liquidated_periods }} 期
                    </span>
                  </template>
                  <template v-else-if="column.key === 'violation_rate'">
                    <span :class="record.violation_rate > 30 ? 'text-red-500' : 'text-orange-500'">
                      {{ record.violation_rate }}%
                    </span>
                  </template>
                </template>
              </Table>
            </Card>
          </Col>
        </Row>

        <Row :gutter="[12, 12]" class="mt-4">
          <Col :lg="12" :xs="24">
            <Card
              :ref="bindSection('sweep')"
              :bordered="false"
              class="h-full shadow-sm"
              :title="titleOf('sweep')"
            >
              <Table
                :columns="sweepCols"
                :data-source="carry?.sweep ?? []"
                :pagination="false"
                row-key="cfg"
                size="small"
              >
                <template #bodyCell="{ column, record }">
                  <template v-if="column.key === 'cfg'">
                    调仓 {{ record.rebalance_days }}d · {{ record.topn }} 币 ·
                    {{ record.leverage }}x
                  </template>
                  <template v-else-if="column.key === 'annual_pct'">
                    <span :class="cls(record.annual_pct)" class="font-semibold">
                      +{{ fmt(record.annual_pct) }}%
                    </span>
                  </template>
                  <template v-else-if="column.key === 'total_pct'">
                    +{{ fmt(record.total_pct) }}%
                  </template>
                  <template v-else-if="column.key === 'max_drawdown_pct'">
                    <span class="text-emerald-500">{{ fmt(record.max_drawdown_pct) }}%</span>
                  </template>
                  <template v-else-if="column.key === 'win_rate'">
                    {{ fmt(record.win_rate, 1) }}%
                  </template>
                </template>
              </Table>
              <div class="mt-2 text-xs text-orange-500">
                ⚠ 表中的年化未计入强平 —— 高杠杆配置在现实中会被强平，见右上表
              </div>
            </Card>
          </Col>

          <Col :lg="12" :xs="24">
            <Card
              :ref="bindSection('buffers')"
              :bordered="false"
              class="h-full shadow-sm"
              :title="titleOf('buffers')"
            >
              <Table
                :columns="[
                  { key: 'label', title: '缓冲策略' },
                  { key: 'buffer_pct', title: '保证金', align: 'right' },
                  { key: 'capital_multiple', title: '资金占用', align: 'right' },
                  { key: 'annual_pct', title: '年化', align: 'right' },
                ]"
                :data-source="carry?.buffers ?? []"
                :pagination="false"
                row-key="label"
                size="small"
              >
                <template #bodyCell="{ column, record }">
                  <template v-if="column.key === 'label'">
                    <span :class="record.buffer_pct === null ? 'text-muted-foreground line-through' : ''">
                      {{ record.label }}
                    </span>
                  </template>
                  <template v-else-if="column.key === 'buffer_pct'">
                    {{ record.buffer_pct === null ? '—' : record.buffer_pct + '%' }}
                  </template>
                  <template v-else-if="column.key === 'capital_multiple'">
                    {{ record.capital_multiple }}N
                  </template>
                  <template v-else-if="column.key === 'annual_pct'">
                    <span class="font-semibold text-red-500">+{{ fmt(record.annual_pct) }}%</span>
                  </template>
                </template>
              </Table>
              <div class="mt-2 text-xs text-muted-foreground">
                <span class="font-medium">唯一能救回收益的前提：</span>
                现货可作期货抵押品（统一账户/组合保证金）→ 强平看合并净值
                <template v-if="unifiedAnnual">
                  → 年化回到 {{ unifiedAnnual.min.toFixed(1) }}% ~
                  {{ unifiedAnnual.max.toFixed(1) }}%
                </template>。
                <span class="text-orange-500">币安 USDT-M 默认不支持，必须实盘前确认。</span>
              </div>
            </Card>
          </Col>
        </Row>
        <!-- 保证金方案（逐小时模拟，含真实强平） -->
        <Card
          :ref="bindSection('schemes')"
          :bordered="false"
          class="mt-4 shadow-sm"
          :title="titleOf('schemes')"
        >
          <template #extra>
            <span v-if="schemeTotals" class="text-xs text-muted-foreground">
              同一策略、仅保证金账户结构不同：累计收益 {{ schemeTotals.min }}% ~
              {{ schemeTotals.max }}%（相差
              {{ (schemeTotals.max - schemeTotals.min).toFixed(1) }} 个百分点）
            </span>
          </template>
          <Table
            :columns="[
              { key: 'label', title: '保证金方案' },
              { key: 'leverage', title: '杠杆', align: 'right', width: 80 },
              { key: 'liquidations', title: '强平次数', align: 'right', width: 110 },
              { key: 'total_pct', title: '累计收益', align: 'right', width: 120 },
              { key: 'annual_pct', title: '年化', align: 'right', width: 110 },
              { key: 'max_drawdown_pct', title: '最大回撤', align: 'right', width: 120 },
            ]"
            :data-source="schemes"
            :pagination="false"
            :row-key="(r: any) => `${r.label}-${r.leverage}`"
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'label'">
                <Tag :color="record.mode === 'unified' ? 'green' : 'red'">
                  {{ record.mode === 'unified' ? '统一账户' : '逐仓' }}
                </Tag>
                <span class="ml-1">{{ record.label }}</span>
              </template>
              <template v-else-if="column.key === 'leverage'">
                {{ record.leverage }}x
              </template>
              <template v-else-if="column.key === 'liquidations'">
                <span :class="record.liquidations > 0 ? 'font-semibold text-red-500' : 'text-emerald-500'">
                  {{ record.liquidations }}
                </span>
              </template>
              <template v-else-if="column.key === 'total_pct'">
                <span :class="cls(record.total_pct)">{{ fmt(record.total_pct) }}%</span>
              </template>
              <template v-else-if="column.key === 'annual_pct'">
                <span :class="cls(record.annual_pct)" class="font-semibold">
                  {{ record.annual_pct === null ? '—' : fmt(record.annual_pct) + '%' }}
                </span>
              </template>
              <template v-else-if="column.key === 'max_drawdown_pct'">
                <span class="text-emerald-500">{{ fmt(record.max_drawdown_pct) }}%</span>
              </template>
            </template>
          </Table>
          <div class="mt-3 rounded-lg bg-orange-50/60 p-3 text-xs dark:bg-orange-950/20">
            <div class="font-medium text-orange-600">关键结论</div>
            <ul class="mt-1 list-inside list-disc space-y-0.5 text-muted-foreground">
              <li v-if="isolatedLowestLiquidated">
                逐仓模式<b>最低 {{ isolatedLowestLiquidated.leverage }}x 也会被强平</b>
                —— 该档在回测区间被强平 {{ isolatedLowestLiquidated.liquidations }} 次
              </li>
              <li v-if="reserveVsIsolated">
                <b>加备用金反而更差</b>：同为 {{ reserveVsIsolated.reserve.leverage }}x，
                逐仓累计 {{ reserveVsIsolated.base.total_pct }}%，
                加备用金后 {{ reserveVsIsolated.reserve.total_pct }}%，
                强平次数都是 {{ reserveVsIsolated.reserve.liquidations }} 次
              </li>
              <li v-if="unifiedNoLiquidation">
                只有<b>统一账户/组合保证金</b>能从根上解决 ——
                该模式下强平次数为 0，Delta 中性头寸的合并净值几乎不动
              </li>
              <li>所以 Carry 能否落地，<b>完全取决于保证金基础设施</b>，而非策略本身</li>
            </ul>
          </div>
        </Card>
      </template>
    </Spin>
  </div>
</template>

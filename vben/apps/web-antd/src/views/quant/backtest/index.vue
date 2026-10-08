<script lang="ts" setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';

import { Card, Col, Progress, Row, Table, Tag } from 'ant-design-vue';

import { EXIT_LABELS, getResearchProgress, getRunProgress } from '#/api/freqtrade';


import { Tabs, TabPane } from 'ant-design-vue';

// 合并进来：研究任务 / 模型迭代（原为独立菜单，2026-10-09 合并同日）
import IterationPage from '#/views/quant/iteration/index.vue';
import TasksPage from '#/views/quant/tasks/index.vue';

/** 合并后的分页（2026-10-09）：回测 / 研究任务 / 模型迭代 */
const tab = ref('bt');

const progress = ref<any>(null);
/* 模型自动迭代任务（bot/auto_research.py）—— 与回测任务并列展示 */
const rp = ref<any>(null);
const rprog = computed(() => rp.value?.progress ?? null);

const rpStatus = computed(() => {
  const s = rprog.value?.status;
  if (s === 'done') return 'success';
  if (s === 'failed') return 'error';
  if (s === 'stopped') return 'warning';
  return 'processing';
});
const rpStatusText = computed(() => {
  const s = rprog.value?.status;
  return s === 'done'
    ? '已完成'
    : s === 'failed'
      ? '失败'
      : s === 'stopped'
        ? '已停止（预算用尽）'
        : '运行中';
});

function cfgLabel(c: any) {
  if (!c) return '—';
  return `${c.kind} L=${c.seq_len} h=${c.hidden} ly=${c.layers} dp=${c.dropout}`;
}
function fmtEta(s: any) {
  if (s === null || s === undefined) return '—';
  const m = Math.round(Number(s) / 60);
  return m >= 60 ? `${(m / 60).toFixed(1)} 小时` : `${m} 分钟`;
}
const trialCols = [
  { dataIndex: 'label', key: 'label', title: '配置' },
  { key: 't', title: '逐窗口 t', align: 'right' as const, width: 110 },
  { key: 'ic', title: 'IC', align: 'right' as const, width: 100 },
  { key: 'pos', title: '正窗口', align: 'right' as const, width: 100 },
  { key: 'phase', title: '阶段', width: 200 },
];
const rpTrialRows = computed(() =>
  (rp.value?.trials?.recent ?? []).map((r: any) => ({
    ...r,
    label: cfgLabel(r.config),
    pos: `${r.pos_windows}/${r.n_windows}`,
  })),
);

const result = computed(() => progress.value?.result ?? null);

const kpis = computed(() => {
  const r = result.value;
  if (!r || r.error) return [];
  return [
    { label: '净利润', value: `${r.profit_abs > 0 ? '+' : ''}${r.profit_abs}`, unit: 'USDT', cls: r.profit_abs > 0 ? 'text-red-500' : 'text-emerald-500' },
    { label: '收益率', value: `${r.profit_pct > 0 ? '+' : ''}${r.profit_pct}`, unit: '%', cls: r.profit_pct > 0 ? 'text-red-500' : 'text-emerald-500' },
    { label: '期末余额', value: `${r.final_balance}`, unit: '', sub: `起始 ${r.starting_balance}`, cls: '' },
    { label: '胜率', value: `${r.winrate}`, unit: '%', sub: `${r.wins} 胜 / ${r.losses} 负`, cls: '' },
    { label: '交易笔数', value: `${r.trades}`, unit: '', sub: `多 ${r.trades_long} / 空 ${r.trades_short}`, cls: '' },
    { label: '最大回撤', value: `${r.max_drawdown_pct}`, unit: '%', cls: 'text-emerald-500' },
    { label: 'Sharpe', value: `${r.sharpe}`, unit: '', cls: r.sharpe > 0 ? 'text-red-500' : 'text-emerald-500' },
    { label: '盈亏比', value: `${r.profit_factor}`, unit: '', cls: r.profit_factor > 1 ? 'text-red-500' : 'text-emerald-500' },
    { label: 'CAGR', value: `${r.cagr}`, unit: '%', cls: r.cagr > 0 ? 'text-red-500' : 'text-emerald-500' },
    { label: '日均交易', value: `${r.trades_per_day}`, unit: '笔', cls: '' },
  ];
});

const dropCls = (v: any) => (Number(v) > 0 ? 'text-red-500' : 'text-emerald-500');

const pairCols = [
  { dataIndex: 'pair', key: 'pair', title: '币对' },
  { dataIndex: 'trades', key: 'trades', title: '交易', align: 'right' as const, width: 90 },
  { dataIndex: 'profit_abs', key: 'profit_abs', title: '盈亏', align: 'right' as const },
  { dataIndex: 'profit_pct', key: 'profit_pct', title: '收益率', align: 'right' as const },
];

const exitCols = [
  { dataIndex: 'reason', key: 'reason', title: '平仓原因' },
  { dataIndex: 'trades', key: 'trades', title: '笔数', align: 'right' as const, width: 90 },
  { dataIndex: 'profit_abs', key: 'profit_abs', title: '盈亏贡献', align: 'right' as const },
  { key: 'bar', title: '', width: 160 },
];

const exits = computed(() =>
  (result.value?.exits ?? [])
    .filter((e: any) => e.reason !== 'TOTAL')
    .slice()
    .sort((a: any, b: any) => a.profit_abs - b.profit_abs),
);
const maxAbs = computed(() =>
  Math.max(...exits.value.map((e: any) => Math.abs(e.profit_abs)), 1),
);

async function load() {
  const [p, r] = await Promise.all([
    getRunProgress().catch(() => null),
    getResearchProgress().catch(() => null),
  ]);
  if (p) progress.value = p;
  if (r) rp.value = r;
}

let timer: any = null;
onMounted(() => {
  load();
  timer = setInterval(load, 3000);
});
onUnmounted(() => clearInterval(timer));

</script>

<template>
  <div class="p-4">
    <Tabs v-model:activeKey="tab" size="small">
      <TabPane key="bt" tab="回测">
    <!-- 进度 -->
    <Card :bordered="false" class="shadow-sm" title="回测 / 训练任务">
      <template #extra>
        <Tag
          v-if="progress"
          :color="
            progress.status === 'done'
              ? 'success'
              : progress.status === 'failed'
                ? 'error'
                : 'processing'
          "
        >
          {{
            progress.status === 'done'
              ? '已完成'
              : progress.status === 'failed'
                ? '失败'
                : '运行中'
          }}
        </Tag>
      </template>
      <template v-if="progress">
        <Progress
          :percent="Number((progress.percent ?? 0).toFixed(1))"
          :status="progress.status === 'failed' ? 'exception' : 'active'"
        />
        <Row :gutter="[12, 12]" class="mt-4">
          <Col :lg="3" :sm="8" :xs="12">
            <div class="text-xs text-gray-400">阶段</div>
            <div class="text-sm font-semibold">{{ progress.phase }}</div>
          </Col>
          <Col :lg="3" :sm="8" :xs="12">
            <div class="text-xs text-gray-400">当前币对</div>
            <div class="text-sm font-semibold">{{ progress.current_pair || '—' }}</div>
          </Col>
          <Col :lg="3" :sm="8" :xs="12">
            <div class="text-xs text-gray-400">已训练轮次</div>
            <div class="text-sm font-semibold">{{ progress.trained }} / {{ progress.total }}</div>
          </Col>
          <Col :lg="3" :sm="8" :xs="12">
            <div class="text-xs text-gray-400">速率</div>
            <div class="text-sm font-semibold">{{ progress.rate_per_min || '—' }} 轮/分</div>
          </Col>
          <Col :lg="3" :sm="8" :xs="12">
            <div class="text-xs text-gray-400">已用时</div>
            <div class="text-sm font-semibold">{{ progress.elapsed_seconds }}s</div>
          </Col>
          <Col :lg="3" :sm="8" :xs="12">
            <div class="text-xs text-gray-400">模型 / 区间</div>
            <div class="text-sm font-semibold">{{ progress.model }}</div>
            <div class="text-xs text-gray-400">{{ progress.timerange }}</div>
          </Col>
        </Row>
      </template>
      <div v-else class="py-12 text-center text-sm text-gray-400">
        暂无任务 —— 用 <code>python run_backtest_task.py</code> 启动回测后此处显示进度
      </div>
    </Card>

    <!-- 模型自动迭代任务 -->
    <Card :bordered="false" class="mt-3 shadow-sm" title="模型自动迭代任务">
      <template #extra>
        <Tag v-if="rprog" :color="rpStatus">{{ rpStatusText }}</Tag>
        <span v-if="rprog?.run_id" class="ml-2 text-xs text-gray-400">
          {{ rprog.run_id }} · 第 {{ rprog.round }} 轮
        </span>
      </template>

      <template v-if="rprog">
        <Progress
          :percent="Number((rprog.percent ?? 0).toFixed(1))"
          :status="rprog.status === 'failed' ? 'exception' : 'active'"
        />
        <Row :gutter="[12, 12]" class="mt-4">
          <Col :lg="3" :sm="8" :xs="12">
            <div class="text-xs text-gray-400">阶段</div>
            <div class="text-sm font-semibold">{{ rprog.phase }}</div>
          </Col>
          <Col :lg="4" :sm="8" :xs="12">
            <div class="text-xs text-gray-400">当前配置</div>
            <div class="font-mono text-sm font-semibold">{{ cfgLabel(rprog.current) }}</div>
          </Col>
          <Col :lg="3" :sm="8" :xs="12">
            <div class="text-xs text-gray-400">已完成 trial</div>
            <div class="text-sm font-semibold">{{ rprog.trial }} / {{ rprog.total }}</div>
          </Col>
          <Col :lg="2" :sm="8" :xs="12">
            <div class="text-xs text-gray-400">速率</div>
            <div class="text-sm font-semibold">
              {{ rprog.rate_per_min || '—' }} <span class="text-xs text-gray-400">个/分</span>
            </div>
          </Col>
          <Col :lg="2" :sm="8" :xs="12">
            <div class="text-xs text-gray-400">已用时</div>
            <div class="text-sm font-semibold">{{ Math.round((rprog.elapsed_seconds || 0) / 60) }} 分</div>
          </Col>
          <Col :lg="3" :sm="8" :xs="12">
            <div class="text-xs text-gray-400">预计剩余</div>
            <div class="text-sm font-semibold">{{ fmtEta(rprog.eta_seconds) }}</div>
          </Col>
          <Col :lg="4" :sm="8" :xs="12">
            <div class="text-xs text-gray-400">本轮历史最优</div>
            <div class="font-mono text-sm font-semibold text-blue-500">
              t={{ rprog.best ? Number(rprog.best.t_period).toFixed(2) : '—' }}
            </div>
            <div class="font-mono text-xs text-gray-400">
              {{ rprog.best ? rprog.best.label : '' }}
            </div>
          </Col>
        </Row>

        <div v-if="rprog.result" class="mt-3 rounded bg-gray-50 p-2 text-xs dark:bg-gray-800">
          <span class="font-medium">本轮结果：</span>
          {{ rprog.result.n_trials }} 个 trial ·
          最优 t={{ Number(rprog.result.best_t).toFixed(2) }} ·
          {{ rprog.result.passed ? '✅ 达标' : '❌ 未达标' }} ·
          候选版本 <span class="font-mono">{{ rprog.result.version_id || '—' }}</span> ·
          报告 {{ rprog.result.report }}
        </div>

        <div class="mt-4">
          <div class="mb-1 text-xs font-medium">
            最近 trial（含失败 —— 自动迭代的全部尝试都可追溯）
          </div>
          <Table
            :columns="trialCols"
            :data-source="rpTrialRows"
            :pagination="false"
            row-key="label"
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'label'">
                <span class="font-mono text-xs">{{ record.label }}</span>
              </template>
              <template v-else-if="column.key === 't'">
                <span
                  class="font-mono text-xs"
                  :class="Number(record.t_period) > 2 ? 'font-semibold text-blue-500' : ''"
                >
                  {{ record.t_period === null || record.t_period === undefined
                    ? '—'
                    : Number(record.t_period).toFixed(2) }}
                </span>
              </template>
              <template v-else-if="column.key === 'ic'">
                <span class="font-mono text-xs">
                  {{ record.ic_period === null || record.ic_period === undefined
                    ? '—'
                    : Number(record.ic_period).toFixed(4) }}
                </span>
              </template>
              <template v-else-if="column.key === 'pos'">
                <span class="font-mono text-xs">{{ record.pos }}</span>
              </template>
              <template v-else-if="column.key === 'phase'">
                <span class="text-xs text-gray-400">{{ record.phase }}</span>
              </template>
            </template>
          </Table>
          <div class="mt-1 text-xs text-gray-400">
            账本累计 {{ rp?.trials?.total || 0 }} 条 trial · {{ rp?.trials?.runs || 0 }} 轮 ·
            原始记录 <code>bot/user_data/research_trials.jsonl</code>
          </div>
        </div>
      </template>

      <div v-else class="py-12 text-center text-sm text-gray-400">
        暂无迭代任务 —— 运行 <code>python auto_research.py</code>（在 bot/ 下）后此处显示进度
      </div>
    </Card>

    <!-- 结果 -->
    <template v-if="result && !result.error">
      <Card :bordered="false" class="mt-3 shadow-sm">
        <template #title>
          回测绩效
          <span class="ml-2 text-xs font-normal text-gray-400">
            {{ result.strategy }} · {{ result.start }} → {{ result.end }}（{{ result.days }} 天）
          </span>
        </template>
        <Row :gutter="[12, 12]">
          <Col v-for="k in kpis" :key="k.label" :lg="2.4" :md="8" :sm="12" :xs="12" :xl="2">
            <div>
              <div class="text-xs text-gray-400">{{ k.label }}</div>
              <div class="mt-1 text-lg font-semibold" :class="k.cls">
                {{ k.value }}
                <span class="text-xs font-normal text-gray-400">{{ k.unit }}</span>
              </div>
              <div v-if="k.sub" class="text-xs text-gray-400">{{ k.sub }}</div>
            </div>
          </Col>
        </Row>
      </Card>

      <Row :gutter="[12, 12]" class="mt-3">
        <Col :lg="14" :xs="24">
          <Card :bordered="false" class="shadow-sm" title="平仓原因贡献（按亏损排序）">
            <Table
              :columns="exitCols"
              :data-source="exits"
              :pagination="false"
              row-key="reason"
              size="small"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'reason'">
                  <span class="font-medium">
                    {{ EXIT_LABELS[record.reason] || record.reason }}
                  </span>
                </template>
                <template v-else-if="column.key === 'profit_abs'">
                  <span :class="dropCls(record.profit_abs)" class="font-semibold">
                    {{ record.profit_abs > 0 ? '+' : '' }}{{ record.profit_abs }}
                  </span>
                </template>
                <template v-else-if="column.key === 'bar'">
                  <div class="h-1.5 w-full overflow-hidden rounded bg-gray-100 dark:bg-gray-700">
                    <div
                      class="h-full rounded"
                      :style="{
                        background: record.profit_abs > 0 ? '#12b886' : '#f0424f',
                        marginLeft: record.profit_abs > 0
                          ? `${100 - (Math.abs(record.profit_abs) / maxAbs) * 100}%`
                          : '0',
                        width: `${(Math.abs(record.profit_abs) / maxAbs) * 100}%`,
                      }"
                    ></div>
                  </div>
                </template>
              </template>
            </Table>
          </Card>
        </Col>
        <Col :lg="10" :xs="24">
          <Card :bordered="false" class="shadow-sm" title="分币对表现">
            <Table
              :columns="pairCols"
              :data-source="result.pairs ?? []"
              :pagination="false"
              row-key="pair"
              size="small"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'pair'">
                  <span class="font-medium">{{ record.pair }}</span>
                </template>
                <template v-else-if="column.key === 'profit_abs'">
                  <span :class="dropCls(record.profit_abs)">
                    {{ record.profit_abs > 0 ? '+' : '' }}{{ record.profit_abs }}
                  </span>
                </template>
                <template v-else-if="column.key === 'profit_pct'">
                  <span :class="dropCls(record.profit_pct)">
                    {{ record.profit_pct > 0 ? '+' : '' }}{{ record.profit_pct }}%
                  </span>
                </template>
              </template>
            </Table>
          </Card>
        </Col>
      </Row>
    </template>
      </TabPane>

      <TabPane key="tasks" tab="研究任务">
        <TasksPage v-if="tab === 'tasks'" />
      </TabPane>

      <TabPane key="iter" tab="模型迭代">
        <IterationPage v-if="tab === 'iter'" />
      </TabPane>
    </Tabs>
  </div>
</template>

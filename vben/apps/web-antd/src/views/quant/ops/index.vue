<script lang="ts" setup>
/**
 * 实盘运维 —— 策略信号 / 波动率中枢 / 因子健康度 / 重估信号
 *
 * 数据来源: bot/monitor.py --daemon（常驻，每 30 分钟刷新）
 *           → user_data/ops_status.json
 *
 * 为什么单独一页：
 *   策略研究页已积累 9 个板块，运维信息在最底部，很难找到。
 *   运维是需要高频查看的内容，独立成页更合适。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue';

import { Card, Col, Row, Spin, Table, Tag } from 'ant-design-vue';

import { getAutoIterate, getOps } from '#/api/freqtrade';


import { Tabs, TabPane } from 'ant-design-vue';

// 合并进来：实盘统计（原为独立菜单，2026-10-09 合并）
import LivePage from '#/views/quant/live/index.vue';
/** 合并后的分页（2026-10-09）：运维 / 实盘统计 */
const tab = ref('ops');

const ops = ref<any>(null);
const ai = ref<any>(null);
const loading = ref(false);

/** C2 巡检项：当前参数、走查窗口都由后端给，不在模板里写死 */
const c2 = computed<any>(() => ai.value?.checks?.['C2 参数稳定性'] ?? null);

/** 「弱信号」用结构化标志判断，不依赖 status 的中文文案 */
const isWeakFactor = (f: any) => f.weak ?? /弱信号/.test(String(f.status ?? ''));
const strongFactors = computed(() =>
  (ops.value?.factors ?? []).filter((f: any) => !isWeakFactor(f)),
);

const inDryRun = computed(() => ops.value?.strategy?.dry_run === true);

async function load() {
  if (!ops.value) loading.value = true;
  try {
    ops.value = await getOps();
    ai.value = await getAutoIterate().catch(() => null);
  } finally {
    loading.value = false;
  }
}
let timer: any = null;
onMounted(() => {
  load();
  // 运维快照每 30 分钟刷新、巡检每几小时一轮，页面必须自动跟上
  timer = setInterval(load, 60_000);
});
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <div class="p-4">
    <Tabs v-model:activeKey="tab" size="small">
      <TabPane key="ops" tab="运维">
    <Spin :spinning="loading">
      <div v-if="!ops" class="py-16 text-center text-muted-foreground">
        暂无运维快照 —— 确认 monitor.py 常驻运行
        （<code class="mx-1">./start.sh status</code> 查看）
      </div>
      <template v-else>
        <!-- 自动迭代巡检 -->
        <Card v-if="ai" :bordered="false" class="shadow-sm" title="🔄 自动迭代巡检">
          <template #extra>
            <span class="text-xs text-muted-foreground">
              {{ ai.t }}<template v-if="ai.interval_hours"> · 每 {{ ai.interval_hours }} 小时一轮</template>
            </span>
          </template>

          <div class="mb-3 flex flex-wrap items-center gap-2">
            <Tag :color="ai.all_ok ? 'green' : 'orange'" class="!px-3 !py-1">
              {{ ai.all_ok ? '✅ 全部通过' : '⚠ 需关注' }}
            </Tag>
            <span v-if="!ai.all_ok" class="text-xs text-orange-500">
              {{ ai.failed.join(' · ') }}
            </span>
            <span class="ml-auto text-[11px] text-muted-foreground">
              只诊断报警，不自动改策略 —— 避免自动套用参数导致过拟合
            </span>
          </div>

          <div class="grid gap-2">
            <div
              v-for="(v, k) in ai.checks"
              :key="k"
              class="flex items-start gap-3 rounded-lg border p-2.5"
              :class="v.ok
                ? 'border-gray-100 dark:border-gray-800'
                : 'border-orange-200 bg-orange-50/50 dark:border-orange-900 dark:bg-orange-950/20'"
            >
              <span class="mt-0.5">{{ v.ok ? '✅' : '❌' }}</span>
              <div class="min-w-0 flex-1">
                <div class="text-xs font-medium">{{ k }}</div>
                <div class="break-words text-[11px] text-muted-foreground">
                  {{ v.detail || v.error }}
                </div>
              </div>
            </div>
          </div>

          <div v-if="c2?.ranking" class="mt-3">
            <div class="mb-1 text-xs font-medium">
              近 {{ ((c2.window_days ?? 365) / 30).toFixed(0) }} 个月滚动走查
              <template v-if="c2.current_params">（当前 {{ c2.current_params }} 标绿）</template>
            </div>
            <div class="flex flex-wrap gap-2">
              <Tag
                v-for="r in c2.ranking"
                :key="r.params"
                :color="r.params === c2.current_params ? 'green' : 'default'"
              >
                {{ r.params }} · Calmar {{ Number(r.calmar).toFixed(2) }}
              </Tag>
            </div>
          </div>

          <div v-if="ai.history?.length" class="mt-3">
            <div class="mb-1 text-xs font-medium">
              巡检历史（最近 {{ ai.history.length }} 轮）
            </div>
            <div class="flex flex-wrap gap-1">
              <span
                v-for="(h, i) in ai.history"
                :key="i"
                class="inline-block h-4 w-4 rounded-sm"
                :class="h.all_ok ? 'bg-emerald-400' : 'bg-orange-400'"
                :title="`${h.t} ${h.all_ok ? '通过' : h.failed.join('/')}`"
              ></span>
            </div>
          </div>
        </Card>

        <Card v-if="ops" :bordered="false" class="mt-4 shadow-sm" title="🔧 实盘运维">
          <template #extra>
            <span class="text-xs text-muted-foreground">
        快照 {{ ops.generated_at }}
            </span>
          </template>

          <Row :gutter="[12, 12]">
            <Col :lg="6" :xs="12">
        <div class="rounded-lg border border-gray-100 p-3 dark:border-gray-800">
          <div class="text-xs text-muted-foreground">运行策略</div>
          <div class="mt-1 font-semibold">{{ ops.strategy.name }}</div>
          <div class="text-[11px] text-muted-foreground">
            {{ ops.strategy.timeframe }} · {{ ops.strategy.pairs }} 币 ·
            {{ inDryRun ? '干跑' : '实盘' }}
          </div>
        </div>
            </Col>
            <Col :lg="6" :xs="12">
        <div class="rounded-lg border border-gray-100 p-3 dark:border-gray-800">
          <div class="text-xs text-muted-foreground">波动率中枢</div>
          <div class="mt-1 font-semibold">{{ ops.vol_regime.vol_90d_pct }}%</div>
          <div class="text-[11px] text-muted-foreground">
            历史分位 {{ ops.vol_regime.vol_90d_percentile }}% ·
            <span :class="ops.vol_regime.regime === '高波动' ? 'text-red-500' : 'text-emerald-500'">
              {{ ops.vol_regime.regime }}
            </span>
          </div>
        </div>
            </Col>
            <Col :lg="6" :xs="12">
        <div class="rounded-lg border border-gray-100 p-3 dark:border-gray-800">
          <div class="text-xs text-muted-foreground">因子告警</div>
          <div class="mt-1 font-semibold"
               :class="ops.factors.filter((f: any) => f.alert).length ? 'text-orange-500' : 'text-emerald-500'">
            {{ ops.factors.filter((f: any) => f.alert).length }}
          </div>
          <div class="text-[11px] text-muted-foreground">共 {{ ops.factors.length }} 个因子</div>
        </div>
            </Col>
            <Col :lg="6" :xs="12">
        <div class="rounded-lg border border-gray-100 p-3 dark:border-gray-800">
          <div class="text-xs text-muted-foreground">模型重估</div>
          <div class="mt-1 font-semibold"
               :class="ops.retrain.need_retrain ? 'text-orange-500' : 'text-emerald-500'">
            {{ ops.retrain.need_retrain ? '⚠ 需要' : '✅ 不需要' }}
          </div>
          <div class="text-[11px] text-muted-foreground">基于因子漂移自动判定</div>
        </div>
            </Col>
          </Row>

          <div v-if="ops.retrain.reasons.length" class="mt-3 rounded-lg bg-orange-50/60 p-3 text-xs dark:bg-orange-950/20">
            <div class="font-medium text-orange-600">重估原因</div>
            <ul class="mt-1 list-inside list-disc text-muted-foreground">
        <li v-for="(r, i) in ops.retrain.reasons" :key="i">{{ r }}</li>
            </ul>
          </div>

          <div class="mt-4 grid gap-4 lg:grid-cols-2">
            <div>
        <div class="mb-2 text-sm font-medium">
          接近突破的币（{{ ops.strategy.near_breakout.length }}）
        </div>
        <Table
          :columns="[
            { dataIndex: 'pair', title: '币对', width: 150 },
            { key: 'pos', title: '通道位置', align: 'right' },
            { key: 'need', title: '距突破', align: 'right' },
            { key: 'vol', title: '波动率', align: 'right' },
          ]"
          :data-source="ops.strategy.near_breakout"
          :pagination="false"
          row-key="pair"
          size="small"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'pos'">
              <Tag v-if="record.signal" color="blue">已突破 {{ record.signal }}</Tag>
              <span v-else class="font-mono text-xs">{{ record.pos }}%</span>
            </template>
            <template v-else-if="column.key === 'need'">
              <span class="font-mono text-xs">
                {{ record.signal ? '—' : '上 ' + record.need_up + '% / 下 ' + record.need_dn + '%' }}
              </span>
            </template>
            <template v-else-if="column.key === 'vol'">
              <span class="font-mono text-xs">{{ record.vol30_pct }}%</span>
            </template>
          </template>
        </Table>
            </div>
            <div>
        <div class="mb-2 text-sm font-medium">因子健康度（滚动 IC vs 基线）</div>
        <Table
          :columns="[
            { dataIndex: 'factor', title: '因子', width: 130 },
            { key: 'ic_base', title: '基线IC', align: 'right' },
            { key: 'ic_recent', title: '近期IC', align: 'right' },
            { key: 't_recent', title: 't值', align: 'right' },
            { key: 'status', title: '状态' },
          ]"
          :data-source="strongFactors.slice(0, 8)"
          :pagination="false"
          row-key="factor"
          size="small"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'ic_base'">
              <span class="font-mono text-xs">{{ record.ic_base }}</span>
            </template>
            <template v-else-if="column.key === 'ic_recent'">
              <span class="font-mono text-xs">{{ record.ic_recent }}</span>
            </template>
            <template v-else-if="column.key === 't_recent'">
              <span class="font-mono text-xs" :class="Math.abs(record.t_recent) > 2 ? 'text-blue-500' : ''">
                {{ record.t_recent }}
              </span>
            </template>
            <template v-else-if="column.key === 'status'">
              <Tag :color="record.status === '正常' ? 'green' : 'orange'">{{ record.status }}</Tag>
            </template>
          </template>
        </Table>
            </div>
          </div>
        </Card>
            </template>
          </Spin>
            </TabPane>

      <TabPane key="live" tab="实盘统计">
        <LivePage v-if="tab === 'live'" />
      </TabPane>
    </Tabs>
  </div>
</template>

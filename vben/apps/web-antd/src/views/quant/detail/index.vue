<script lang="ts" setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { EchartsUI, useEcharts } from '@vben/plugins/echarts';
import type { EchartsUIType } from '@vben/plugins/echarts';
import {
  getBacktestDetail,
  getBacktestList,
  getBackgroundJob,
  getIterationRun,
  getLookaheadResult,
  getRecursiveResult,
} from '#/api/freqtrade';

import {
  checkedDetail,
  checkedList,
  identity,
  label,
  metric,
  object,
  percent,
  publicJson,
  signed,
  targetFromQuery,
} from './detailEvidence';

const route = useRoute();
const router = useRouter();
const list = ref<Record<string, unknown>[]>([]);
const selected = ref<ReturnType<typeof targetFromQuery> | null>(null);
const evidence = ref<ReturnType<typeof checkedDetail> | null>(null);
const runEvidence = ref<Record<string, any> | null>(null);
const jobEvidence = ref<Record<string, any> | null>(null);
const snapshotTarget = ref('');
const stale = ref(false);
const listError = ref('');
const detailError = ref('');
const listLoading = ref(false);
const detailLoading = ref(false);
const refreshing = ref(false);
const filter = ref<'all' | 'win' | 'loss'>('all');
const pairFilter = ref('');
let listRequest = 0;
let detailRequest = 0;
let disposed = false;
let timer: ReturnType<typeof setInterval> | undefined;
let listController: AbortController | undefined;
let detailController: AbortController | undefined;

const target = computed(() =>
  targetFromQuery(route.query as Record<string, unknown>),
);
const selectedIdentifier = computed(() =>
  target.value.kind === 'identifier' ? target.value.id : '',
);
const trades = computed(() => {
  const rows = evidence.value?.trades ?? [];
  return rows.filter(
    (row) =>
      (!pairFilter.value || row.pair === pairFilter.value) &&
      (filter.value === 'all' ||
        (filter.value === 'win'
          ? Number(row.profit_abs) > 0
          : Number(row.profit_abs) <= 0)),
  );
});
const pairs = computed(() =>
  [
    ...new Set((evidence.value?.by_pair ?? []).map((row) => String(row.pair))),
  ].sort(),
);
const metrics = computed(() => {
  const summary = evidence.value?.summary;
  if (!summary) return [];
  return [
    { label: '净利润 USDT', value: signed(summary.profit_abs) },
    { label: '收益率', value: percent(summary.profit_pct) },
    { label: '结束余额 USDT', value: metric(summary.final_balance) },
    { label: '交易', value: metric(summary.trades, 0) },
    { label: '胜率', value: percent(summary.winrate) },
    { label: '平均盈利', value: percent(summary.avg_win) },
    { label: '平均亏损', value: percent(summary.avg_loss) },
    { label: '最大回撤', value: percent(summary.max_drawdown_pct) },
    { label: '盈亏比', value: metric(summary.profit_factor) },
    { label: 'Sharpe', value: metric(summary.sharpe) },
    { label: '预测-实际 IC', value: metric(summary.pred_vs_actual_ic, 4) },
    { label: '方向命中率', value: percent(summary.sign_hit_rate) },
  ];
});
const jobResult = computed(() => jobEvidence.value?.result);

const equityChart = ref<EchartsUIType>();
const predictionChart = ref<EchartsUIType>();
const { renderEcharts: drawEquity } = useEcharts(equityChart);
const { renderEcharts: drawPredictions } = useEcharts(predictionChart);
function renderCharts() {
  const rows = evidence.value?.equity ?? [];
  if (rows.length)
    drawEquity({
      animation: false,
      color: ['#69b4e4'],
      grid: { bottom: 28, containLabel: true, left: 55, right: 16, top: 16 },
      series: [
        {
          type: 'line',
          data: rows.map((row) => row[1]),
          showSymbol: false,
          lineStyle: { width: 2 },
          areaStyle: { opacity: 0.14 },
        },
      ],
      tooltip: { trigger: 'axis' },
      xAxis: {
        type: 'category',
        data: rows.map((row) => row[0]),
        boundaryGap: false,
      },
      yAxis: { type: 'value', scale: true, name: 'USDT' },
    });
  const points = (evidence.value?.trades ?? []).filter(
    (row) =>
      typeof row.model_pred_pct === 'number' &&
      Number.isFinite(row.model_pred_pct) &&
      typeof row.profit_pct === 'number' &&
      Number.isFinite(row.profit_pct),
  );
  if (points.length)
    drawPredictions({
      animation: false,
      color: ['#69b4e4'],
      grid: { bottom: 35, containLabel: true, left: 52, right: 16, top: 16 },
      series: [
        {
          type: 'scatter',
          data: points.map((row) => [
            Number(row.model_pred_pct),
            Number(row.profit_pct),
          ]),
          symbolSize: 7,
        },
      ],
      tooltip: { trigger: 'item' },
      xAxis: { type: 'value', name: '模型预测 %' },
      yAxis: { type: 'value', name: '实际结果 %' },
    });
}

async function loadList() {
  listController?.abort();
  const request = ++listRequest;
  const controller = new AbortController();
  listController = controller;
  listLoading.value = true;
  try {
    const rows = checkedList(await getBacktestList());
    if (disposed || controller.signal.aborted || request !== listRequest)
      return;
    list.value = rows;
    listError.value = '';
  } catch {
    if (!disposed && !controller.signal.aborted && request === listRequest)
      listError.value =
        '产物目录读取失败' +
        (list.value.length ? '，保留上次成功快照' : '，目录尚未取得') +
        '；请重试';
  } finally {
    if (!disposed && request === listRequest) listLoading.value = false;
  }
}
async function loadSelected() {
  detailController?.abort();
  const request = ++detailRequest;
  const current = target.value;
  const currentKey = current.kind + ':' + current.id;
  const hadSnapshot =
    snapshotTarget.value === currentKey &&
    Boolean(evidence.value || runEvidence.value || jobEvidence.value);
  selected.value = current;
  detailError.value = '';
  stale.value = hadSnapshot;
  if (!hadSnapshot) {
    runEvidence.value = null;
    jobEvidence.value = null;
    evidence.value = null;
  }
  if (snapshotTarget.value !== currentKey) {
    runEvidence.value = null;
    jobEvidence.value = null;
    evidence.value = null;
  }
  snapshotTarget.value = currentKey;
  if (current.kind === 'none' || current.kind === 'invalid') {
    detailLoading.value = false;
    return;
  }
  const controller = new AbortController();
  detailController = controller;
  detailLoading.value = true;
  let nextEvidence: ReturnType<typeof checkedDetail> | null = null;
  let nextRun: Record<string, any> | null = null;
  let nextJob: Record<string, any> | null = null;
  try {
    if (current.kind === 'identifier') {
      const response = checkedDetail(
        await getBacktestDetail(current.id),
        current.id,
      );
      if (
        disposed ||
        controller.signal.aborted ||
        request !== detailRequest ||
        target.value.id !== current.id ||
        target.value.kind !== current.kind
      )
        return;
      nextEvidence = response;
    } else if (current.kind === 'run_id') {
      const response = await getIterationRun(current.id);
      if (!object(response) || response.run_id !== current.id)
        throw new Error('run_identity_mismatch');
      if (
        disposed ||
        controller.signal.aborted ||
        request !== detailRequest ||
        target.value.id !== current.id
      )
        return;
      nextRun = response;
      const artifactIdentifier =
        typeof response.identifier === 'string' ? response.identifier : '';
      if (identity(artifactIdentifier)) {
        const artifact = checkedDetail(
          await getBacktestDetail(artifactIdentifier),
          artifactIdentifier,
        );
        if (
          disposed ||
          controller.signal.aborted ||
          request !== detailRequest ||
          target.value.id !== current.id
        )
          return;
        nextEvidence = artifact;
      }
    } else {
      const status = await getBackgroundJob(current.id);
      if (
        !object(status) ||
        status.job_id !== current.id ||
        typeof status.running !== 'boolean' ||
        typeof status.status !== 'string'
      )
        throw new Error('job_identity_mismatch');
      if (
        disposed ||
        controller.signal.aborted ||
        request !== detailRequest ||
        target.value.id !== current.id
      )
        return;
      nextJob = status;
      if (
        status.running === false &&
        status.status === 'success' &&
        !status.error &&
        (status.job_category === 'lookahead_analysis' ||
          status.job_category === 'recursive_analysis')
      ) {
        const result =
          status.job_category === 'lookahead_analysis'
            ? await getLookaheadResult(current.id)
            : await getRecursiveResult(current.id);
        if (
          disposed ||
          controller.signal.aborted ||
          request !== detailRequest ||
          target.value.id !== current.id
        )
          return;
        if (
          !object(result) ||
          (result.job_id !== undefined && result.job_id !== current.id)
        )
          throw new Error('job_result_identity_mismatch');
        nextJob = { ...status, result };
      } else if (
        status.running === false &&
        status.status === 'success' &&
        status.job_category !== 'lookahead_analysis' &&
        status.job_category !== 'recursive_analysis'
      ) {
        throw new Error('后台任务类别不支持精确结果读取');
      }
    }
    if (
      disposed ||
      controller.signal.aborted ||
      request !== detailRequest ||
      target.value.id !== current.id ||
      target.value.kind !== current.kind
    )
      return;
    evidence.value = nextEvidence;
    runEvidence.value = nextRun;
    jobEvidence.value = nextJob;
    stale.value = false;
    detailError.value = '';
    await nextTick();
    if (request === detailRequest && !disposed) renderCharts();
  } catch (error) {
    if (!disposed && !controller.signal.aborted && request === detailRequest) {
      stale.value = Boolean(
        evidence.value || runEvidence.value || jobEvidence.value,
      );
      const reason =
        error instanceof Error && error.message === '返回产物身份不匹配'
          ? '返回产物身份不匹配'
          : error instanceof Error && error.message.includes('合同')
            ? '返回产物合同不完整'
            : current.kind === 'identifier'
              ? '指定产物无法读取'
              : '指定运行或后台任务无法读取';
      detailError.value =
        reason +
        (stale.value ? '，保留该目标上次成功快照' : '，未取得该目标证据') +
        '；请重试';
    }
  } finally {
    if (!disposed && request === detailRequest) detailLoading.value = false;
  }
}
async function refresh() {
  refreshing.value = true;
  try {
    await Promise.all([loadList(), loadSelected()]);
  } finally {
    refreshing.value = false;
  }
}
function selectArtifact(id: string) {
  if (identity(id))
    void router.push({ path: '/quant/detail', query: { identifier: id } });
}
const json = publicJson;
function executionLabel(value: unknown) {
  const labels: Record<string, string> = {
    succeeded: '执行成功',
    failed: '执行失败',
    cancelled: '已取消',
    running: '运行中',
    queued: '排队中',
    completed: '已完成',
  };
  return Object.hasOwn(labels, String(value))
    ? labels[String(value)]!
    : '执行状态未知';
}
function verdictLabel(value: unknown) {
  const labels: Record<string, string> = {
    passed: '研究通过（历史裁定）',
    rejected: '研究拒绝',
    not_estimable: '不可评估',
  };
  return Object.hasOwn(labels, String(value))
    ? labels[String(value)]!
    : '尚无研究裁定';
}
watch(
  () => route.fullPath,
  () => {
    pairFilter.value = '';
    void loadSelected();
  },
);
onMounted(() => {
  void loadList();
  void loadSelected();
  timer = setInterval(() => void refresh(), 60_000);
});
onUnmounted(() => {
  disposed = true;
  listRequest += 1;
  detailRequest += 1;
  listController?.abort();
  detailController?.abort();
  if (timer) clearInterval(timer);
});
</script>

<template>
  <main class="artifact-workspace" aria-label="回测历史产物">
    <header class="artifact-heading">
      <div>
        <h1>回测产物</h1>
        <p>历史 Freqtrade 结果；不属于 Binance Demo 或 30 日验收。</p>
      </div>
      <button
        data-testid="refresh-detail"
        type="button"
        :disabled="refreshing"
        @click="refresh"
      >
        刷新记录
      </button>
    </header>
    <p v-if="listError" class="detail-alert" role="status">{{ listError }}</p>
    <p v-if="detailError" class="detail-alert" role="alert">
      {{ detailError }}
    </p>
    <div class="artifact-grid">
      <aside class="artifact-directory" aria-label="历史产物目录">
        <div class="directory-heading">
          <h2>已归档产物</h2>
          <span>{{ list.length }}</span>
        </div>
        <p v-if="listLoading && !list.length" class="quiet">正在读取目录</p>
        <p v-else-if="!list.length" class="quiet">
          {{ listError ? '目录尚未取得' : '当前目录为空' }}
        </p>
        <button
          v-for="item in list"
          :key="String(item.identifier)"
          type="button"
          class="artifact-row"
          :class="{ active: selectedIdentifier === item.identifier }"
          :aria-current="
            selectedIdentifier === item.identifier ? 'true' : undefined
          "
          @click="selectArtifact(String(item.identifier))"
        >
          <strong>{{ item.identifier }}</strong
          ><span>{{ item.strategy || '策略未知' }}</span
          ><span
            >历史净利润
            {{
              metric(object(item.summary) ? item.summary.profit_abs : undefined)
            }}
            USDT</span
          >
        </button>
      </aside>
      <section class="detail-content" aria-label="所选产物证据">
        <div
          v-if="target.kind === 'none' || target.kind === 'invalid'"
          class="detail-empty"
          data-testid="target-state"
        >
          <strong>{{
            target.kind === 'invalid' ? '目标参数无效' : '选择一份历史产物'
          }}</strong>
          <p>{{ target.reason }}</p>
          <p v-if="target.kind === 'none'">
            也可以从实验运行或后台任务记录进入精确详情。
          </p>
        </div>
        <template v-else>
          <div class="selected-heading" data-testid="selected-identity">
            <div>
              <span>{{
                selected?.kind === 'identifier'
                  ? '指定产物'
                  : selected?.kind === 'run_id'
                    ? '指定迭代运行'
                    : '指定后台任务'
              }}</span>
              <h2>{{ selected?.id }}</h2>
            </div>
            <span v-if="stale" class="stale-marker">旧快照</span>
          </div>
          <p v-if="detailLoading" class="quiet" aria-live="polite">
            正在读取这份产物
          </p>
          <div v-if="evidence" class="evidence-stack">
            <section class="summary-strip" aria-label="回测摘要">
              <div v-for="item in metrics" :key="item.label">
                <span>{{ item.label }}</span
                ><strong>{{ item.value }}</strong>
              </div>
              <p>
                其它成本未齐，摘要数值按历史来源保留，不能视为完整净收益或验收。
              </p>
            </section>
            <section class="chart-grid">
              <div class="detail-panel chart-panel">
                <h3>历史权益</h3>
                <EchartsUI
                  v-if="evidence.equity.length"
                  ref="equityChart"
                  data-testid="equity-chart"
                  height="230px"
                />
                <p v-else class="quiet">该产物未记录资金曲线</p>
              </div>
              <div class="detail-panel chart-panel">
                <h3>预测与实际结果</h3>
                <EchartsUI
                  v-if="
                    evidence.trades.some(
                      (row) =>
                        typeof row.model_pred_pct === 'number' &&
                        Number.isFinite(row.model_pred_pct),
                    )
                  "
                  ref="predictionChart"
                  data-testid="prediction-chart"
                  height="230px"
                />
                <p v-else class="quiet">没有同时具备模型预测和实际收益的交易</p>
              </div>
            </section>
            <section class="detail-panel">
              <div class="trade-heading">
                <h3>交易检查器</h3>
                <div>
                  <select v-model="pairFilter" aria-label="币对">
                    <option value="">全部币对</option>
                    <option v-for="pair in pairs" :key="pair" :value="pair">
                      {{ pair }}
                    </option></select
                  ><select v-model="filter" aria-label="盈亏筛选">
                    <option value="all">全部盈亏</option>
                    <option value="win">只看盈利</option>
                    <option value="loss">只看亏损</option>
                  </select>
                </div>
              </div>
              <div class="table-scroll" data-testid="trade-table" tabindex="0">
                <table>
                  <thead>
                    <tr>
                      <th>币对与方向</th>
                      <th>开仓 / 平仓时间</th>
                      <th>开仓价</th>
                      <th>平仓价</th>
                      <th>投入</th>
                      <th>净收益率</th>
                      <th>净盈亏 USDT</th>
                      <th>平仓原因</th>
                      <th>模型预测</th>
                      <th>决策依据</th>
                      <th>持仓时长 h</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="(trade, index) in trades"
                      :key="String(trade.open_date) + ':' + index"
                    >
                      <td>
                        {{ label(trade.pair) }}<br />{{ label(trade.side) }}
                      </td>
                      <td>
                        {{ label(trade.open_date) }}<br />{{
                          label(trade.close_date)
                        }}
                      </td>
                      <td>{{ metric(trade.open_rate, 6) }}</td>
                      <td>{{ metric(trade.close_rate, 6) }}</td>
                      <td>{{ metric(trade.stake_amount) }}</td>
                      <td>{{ percent(trade.profit_pct) }}</td>
                      <td>{{ signed(trade.profit_abs) }}</td>
                      <td>{{ label(trade.exit_reason) }}</td>
                      <td>{{ percent(trade.model_pred_pct) }}</td>
                      <td>{{ label(trade.decision) }}</td>
                      <td>{{ metric(trade.duration_h) }}</td>
                    </tr>
                    <tr v-if="!trades.length">
                      <td colspan="11">该筛选下没有可显示的交易</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <p class="quiet">
                显示 {{ trades.length }} /
                {{ evidence.trades.length }}
                笔。历史交易不代表新账户订单、成交或费用后完整结果。
              </p>
            </section>
            <section class="inspector-grid">
              <div class="detail-panel">
                <h3>按币对统计</h3>
                <table class="compact-table">
                  <thead>
                    <tr>
                      <th>币对</th>
                      <th>笔数</th>
                      <th>胜数</th>
                      <th>历史盈亏 USDT</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="row in evidence.by_pair" :key="String(row.pair)">
                      <td>{{ label(row.pair) }}</td>
                      <td>{{ metric(row.trades, 0) }}</td>
                      <td>{{ metric(row.wins, 0) }}</td>
                      <td>{{ signed(row.profit_abs) }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div class="detail-panel">
                <h3>按退出原因统计</h3>
                <table class="compact-table">
                  <thead>
                    <tr>
                      <th>原因</th>
                      <th>笔数</th>
                      <th>历史盈亏 USDT</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="row in evidence.by_exit"
                      :key="String(row.reason)"
                    >
                      <td>{{ label(row.reason) }}</td>
                      <td>{{ metric(row.trades, 0) }}</td>
                      <td>{{ signed(row.profit_abs) }}</td>
                    </tr>
                  </tbody>
                </table>
                <h3>回测阈值</h3>
                <pre>{{ json(evidence.thresholds) }}</pre>
              </div>
            </section>
          </div>
          <div
            v-else-if="
              selected?.kind === 'identifier' && !detailLoading && !detailError
            "
            class="detail-empty"
          >
            <strong>指定产物尚无证据</strong>
            <p>该标识没有可以展示的交易明细。</p>
          </div>
        </template>
        <section
          v-if="runEvidence || jobEvidence"
          class="detail-panel detail-run"
          aria-label="运行或任务证据"
        >
          <h2>运行事实</h2>
          <dl v-if="jobEvidence">
            <dt>后台任务</dt>
            <dd>{{ jobEvidence.job_id }}</dd>
            <dt>执行状态</dt>
            <dd>{{ executionLabel(jobEvidence.status) }}</dd>
            <dt>类别</dt>
            <dd>{{ label(jobEvidence.job_category) }}</dd>
            <dt>研究裁定</dt>
            <dd>没有因任务完成而授予通过</dd>
          </dl>
          <dl v-if="runEvidence">
            <dt>运行身份</dt>
            <dd>{{ runEvidence.run_id }}</dd>
            <dt>执行状态</dt>
            <dd>
              {{
                executionLabel(
                  runEvidence.execution_status ?? runEvidence.status,
                )
              }}
            </dd>
            <dt>研究裁定</dt>
            <dd>{{ verdictLabel(runEvidence.research_verdict) }}</dd>
            <dt>精确产物映射</dt>
            <dd>
              {{
                identity(runEvidence.identifier)
                  ? runEvidence.identifier
                  : '没有可核验的回测产物映射'
              }}
            </dd>
            <dt>运行证据</dt>
            <dd>
              {{
                json({
                  metrics: runEvidence.metrics,
                  evidence: runEvidence.evidence,
                  gate: runEvidence.gate,
                })
              }}
            </dd>
          </dl>
          <pre v-if="jobResult">{{ json(jobResult) }}</pre>
          <p v-if="jobEvidence?.running === true">
            后台任务仍在运行；此页面只查询，不会自动取消任务。
          </p>
        </section>
      </section>
    </div>
  </main>
</template>

<style scoped>
.artifact-workspace {
  --navy: #0c1b2a;
  --surface: #132b3e;
  --line: #29485c;
  --text: #e5edf4;
  --muted: #a2b8c9;
  --accent: #69b4e4;
  min-width: 0;
  padding: 18px 24px 32px;
  color: var(--text);
  font-family:
    'IBM Plex Sans', 'Source Han Sans SC', 'Noto Sans SC', sans-serif;
}
.artifact-heading,
.directory-heading,
.selected-heading,
.trade-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
h1,
h2,
h3,
p {
  margin: 0;
}
h1 {
  font-size: 22px;
}
h2 {
  font-size: 16px;
}
h3 {
  margin: 0 0 12px;
  font-size: 14px;
}
.artifact-heading {
  margin-bottom: 18px;
}
.artifact-heading p,
.quiet,
.directory-heading span,
.artifact-row span,
.summary-strip span {
  color: var(--muted);
  font-size: 12px;
}
button,
select {
  color: var(--text);
  background: var(--surface);
  border: 1px solid var(--line);
  padding: 7px 10px;
}
button {
  cursor: pointer;
}
button:disabled {
  cursor: wait;
  opacity: 0.6;
}
button:focus-visible,
select:focus-visible,
.table-scroll:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.artifact-grid {
  display: grid;
  grid-template-columns: minmax(220px, 270px) minmax(0, 1fr);
  align-items: start;
  gap: 18px;
}
.artifact-directory,
.detail-content {
  min-width: 0;
}
.artifact-directory {
  border-right: 1px solid var(--line);
  padding-right: 14px;
}
.directory-heading {
  padding: 0 0 10px;
  border-bottom: 1px solid var(--line);
}
.artifact-row {
  display: grid;
  gap: 5px;
  width: 100%;
  margin: 4px 0;
  padding: 10px;
  text-align: left;
  overflow-wrap: anywhere;
}
.artifact-row strong {
  font-size: 13px;
}
.artifact-row.active {
  border-color: var(--accent);
  background: #183851;
}
.detail-content {
  display: grid;
  gap: 14px;
}
.selected-heading {
  border-bottom: 1px solid var(--line);
  padding-bottom: 12px;
}
.selected-heading > div > span {
  color: var(--muted);
  font-size: 11px;
}
.selected-heading h2 {
  overflow-wrap: anywhere;
}
.stale-marker {
  color: #efc982;
  font-size: 12px;
}
.detail-empty,
.detail-panel,
.summary-strip {
  min-width: 0;
  padding: 14px;
  background: var(--surface);
  border: 1px solid var(--line);
}
.detail-empty p {
  margin-top: 7px;
  color: var(--muted);
}
.detail-alert {
  padding: 9px 12px;
  color: #f4ce82;
  background: #342d20;
  border-left: 3px solid #dfb566;
  overflow-wrap: anywhere;
}
.evidence-stack {
  display: grid;
  gap: 12px;
}
.summary-strip {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 10px;
}
.summary-strip div {
  display: grid;
  gap: 4px;
  min-width: 0;
}
.summary-strip strong {
  overflow-wrap: anywhere;
  font-size: 14px;
}
.summary-strip p {
  grid-column: 1/-1;
  color: var(--muted);
  font-size: 12px;
}
.chart-grid,
.inspector-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.chart-panel {
  min-height: 290px;
}
.trade-heading {
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.trade-heading > div {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.table-scroll {
  min-width: 0;
  overflow-x: auto;
  border: 1px solid var(--line);
}
table {
  width: 100%;
  min-width: 1080px;
  border-collapse: collapse;
  font-size: 12px;
}
th,
td {
  padding: 8px 10px;
  text-align: left;
  vertical-align: top;
  border-bottom: 1px solid var(--line);
}
th {
  color: var(--muted);
  background: var(--navy);
  font-weight: 500;
}
td {
  max-width: 240px;
  overflow-wrap: anywhere;
}
.compact-table {
  min-width: 100%;
}
pre {
  max-width: 100%;
  max-height: 340px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--muted);
  font-size: 11px;
}
.detail-run {
  display: grid;
  gap: 10px;
}
.detail-run dl {
  display: grid;
  grid-template-columns: 140px minmax(0, 1fr);
  gap: 6px 10px;
  margin: 0;
}
.detail-run dt {
  color: var(--muted);
}
.detail-run dd {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
@media (max-width: 900px) {
  .artifact-workspace {
    padding: 14px;
  }
  .artifact-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .artifact-directory {
    border-right: 0;
    border-bottom: 1px solid var(--line);
    padding: 0 0 12px;
  }
  .artifact-row {
    display: inline-grid;
    width: min(100%, 300px);
    vertical-align: top;
  }
  .summary-strip {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
@media (max-width: 560px) {
  .artifact-workspace {
    padding: 12px;
  }
  .artifact-heading {
    align-items: flex-start;
  }
  .chart-grid,
  .inspector-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .summary-strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .detail-run dl {
    grid-template-columns: 100px minmax(0, 1fr);
  }
}
</style>

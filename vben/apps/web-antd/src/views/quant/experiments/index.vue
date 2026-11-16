<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';

import { getIterationRun, getIterationRuns } from '#/api/freqtrade';

import {
  cancelExperiments,
  createExperimentState,
  executionLabel,
  identityFacts,
  metricText,
  refreshRuns,
  selectRun,
  verdictLabel,
} from './experimentState';

const route = useRoute();
const router = useRouter();
const state = reactive(createExperimentState());
const execution = ref('all');
const search = ref('');
const rows = computed(
  () =>
    state.snapshot?.items.filter(
      (run) =>
        (execution.value === 'all' ||
          run.execution_status === execution.value) &&
        (run.run_id + ' ' + (run.strategy ?? '') + ' ' + (run.model ?? ''))
          .toLowerCase()
          .includes(search.value.trim().toLowerCase()),
    ) ?? [],
);
const selected = computed(() => state.detail);
const facts = computed(() =>
  selected.value ? identityFacts(selected.value) : [],
);
const metrics = computed(() => ({
  ...selected.value?.entry?.metrics,
  ...selected.value?.metrics,
  ...selected.value?.gate?.values,
}));
const metricRows = computed(() => [
  { label: '源收益指标 · %', value: metricText(metrics.value.profit_pct) },
  {
    label: '源交易数',
    value: metricText(
      Object.hasOwn(metrics.value, 'trades')
        ? metrics.value.trades
        : metrics.value.trade_count,
    ),
  },
  { label: '源利润因子', value: metricText(metrics.value.profit_factor) },
  {
    label: '源回撤指标 · %',
    value: metricText(metrics.value.max_drawdown_pct),
  },
]);

function queryRun(): null | string {
  const value = route.query.run_id;
  if (value === undefined) return '';
  return typeof value === 'string' && value.length > 0 ? value : null;
}
function clearSelection(invalid = false) {
  state.detailController?.abort();
  state.detailRequest += 1;
  state.selectedRunId = '';
  state.detail = null;
  state.detailLoading = false;
  state.detailError = invalid
    ? '运行查询身份无法核对；请选择一条明确的运行记录。'
    : '';
}
function readSelected(id: string) {
  // 现有兼容 API 没有信号参数，请求代数和取消状态负责阻止迟到发布。
  return selectRun(state, id, (runId) => getIterationRun(runId));
}
async function choose(id: string) {
  if (id === queryRun()) {
    await readSelected(id);
    return;
  }
  await router.replace({ query: { ...route.query, run_id: id } });
}
async function refresh(includeDetail = true) {
  await refreshRuns(state, () => getIterationRuns(100));
  if (state.disposed) return;
  const id = queryRun();
  if (id === null) return;
  if (!id && state.snapshot?.items[0])
    await choose(state.snapshot.items[0].run_id);
  else if (id && includeDetail) await readSelected(id);
}
watch(
  () => route.query.run_id,
  () => {
    const id = queryRun();
    if (id === null) clearSelection(true);
    else if (id) void readSelected(id);
    else if (state.snapshot?.items[0])
      void choose(state.snapshot.items[0].run_id);
    else clearSelection();
  },
  { immediate: true },
);
onMounted(() => {
  void refresh(false);
});
onUnmounted(() => cancelExperiments(state));
</script>

<template>
  <main class="experiments-workspace" aria-labelledby="experiments-title">
    <header class="workspace-heading">
      <div>
        <p class="eyebrow">研究 / 运行与产物</p>
        <h1 id="experiments-title">实验任务</h1>
        <p class="intro">
          逐轮查看执行过程、研究裁定与已有证据。执行成功不构成上线批准。
        </p>
      </div>
      <button
        type="button"
        data-testid="refresh-runs"
        :disabled="state.listLoading"
        @click="refresh()"
      >
        {{ state.listLoading ? '正在读取' : '刷新运行记录' }}
      </button>
    </header>
    <div v-if="state.listError" class="read-error" role="alert">
      {{ state.listError }}
    </div>
    <section class="snapshot-band" aria-label="运行来源">
      <div><span>来源</span><strong>历史自动迭代 · 只读</strong></div>
      <div>
        <span>当前载入 / 源总数</span
        ><strong>{{
          state.snapshot
            ? state.snapshot.items.length + ' / ' + state.snapshot.total
            : '未知'
        }}</strong>
      </div>
      <div>
        <span>源生成时间</span
        ><strong>{{ state.snapshot?.generated_at ?? '未知' }}</strong>
      </div>
      <div><span>新研究准入</span><strong class="caution">未接通</strong></div>
    </section>
    <div class="experiment-grid">
      <section class="run-ledger" aria-labelledby="run-list-title">
        <header class="ledger-heading">
          <div>
            <h2 id="run-list-title">运行账</h2>
            <p>所有返回轮次均保留；失败与不可评估仍是研究记录。</p>
          </div>
          <span class="record-count">显示 {{ rows.length }} 条</span>
        </header>
        <form class="filters" @submit.prevent>
          <label
            >查找运行<input
              v-model="search"
              type="search"
              aria-label="查找运行"
              placeholder="运行 ID / 策略 / 模型"
          /></label>
          <label
            >执行状态<select v-model="execution" aria-label="执行状态筛选">
              <option value="all">全部状态</option>
              <option value="running">运行中</option>
              <option value="succeeded">执行成功</option>
              <option value="failed">执行失败</option>
              <option value="disabled">自动迭代已停用</option>
              <option value="cancelled">已取消</option>
            </select></label
          >
        </form>
        <p v-if="!state.snapshot" class="empty-state">
          {{
            state.listLoading
              ? '正在读取运行记录…'
              : state.listError
                ? '运行账尚未取得可用快照。'
                : '尚未读取运行记录。'
          }}
        </p>
        <p v-else-if="state.snapshot.items.length === 0" class="empty-state">
          尚无运行记录。本次读取成功，但没有返回轮次。
        </p>
        <div
          v-else
          class="table-viewport"
          tabindex="0"
          aria-label="运行账，可横向滚动"
        >
          <table>
            <thead>
              <tr>
                <th scope="col">运行 / 策略</th>
                <th scope="col">执行状态</th>
                <th scope="col">研究裁定</th>
                <th scope="col">时间 / 范围</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="run in rows"
                :key="run.run_id"
                :class="{ selected: state.selectedRunId === run.run_id }"
              >
                <th scope="row">
                  <button
                    type="button"
                    :data-run-id="run.run_id"
                    :aria-pressed="state.selectedRunId === run.run_id"
                    @click="choose(run.run_id)"
                  >
                    {{ run.run_id }}</button
                  ><small
                    >{{ run.strategy || '策略未知' }} ·
                    {{ run.model || '模型未知' }}</small
                  >
                </th>
                <td>
                  <span
                    class="status"
                    :class="{ caution: run.execution_status === 'failed' }"
                    >{{ executionLabel(run.execution_status) }}</span
                  >
                </td>
                <td>{{ verdictLabel(run.research_verdict) }}</td>
                <td class="time-cell">
                  {{ run.finished_at || run.started_at || '时间未知'
                  }}<small>{{ run.timerange || '范围未知' }}</small>
                </td>
              </tr>
            </tbody>
          </table>
          <p v-if="rows.length === 0" class="empty-state">
            当前筛选没有运行记录。
          </p>
        </div>
      </section>
      <aside class="run-inspector" aria-labelledby="run-detail-title">
        <header class="inspector-heading">
          <p class="eyebrow">选中轮次</p>
          <h2 id="run-detail-title" data-testid="selected-run">
            {{
              queryRun() === null
                ? '无效运行查询'
                : state.selectedRunId || '尚未选择运行'
            }}
          </h2>
          <p v-if="state.detailLoading" class="loading-text" role="status">
            正在核对该轮证据…
          </p>
        </header>
        <div v-if="state.detailError" class="detail-error" role="alert">
          {{ state.detailError }}
        </div>
        <template v-if="selected">
          <dl class="status-pair">
            <div>
              <dt>执行状态</dt>
              <dd>{{ executionLabel(selected.execution_status) }}</dd>
            </div>
            <div>
              <dt>研究裁定</dt>
              <dd>{{ verdictLabel(selected.research_verdict) }}</dd>
            </div>
          </dl>
          <p class="scope-note">
            旧研究判据与执行结果分别显示，不构成上线批准。
          </p>
          <dl class="detail-list">
            <div>
              <dt>策略 / 模型</dt>
              <dd>
                {{ selected.strategy || '未知' }} /
                {{ selected.model || '未知' }}
              </dd>
            </div>
            <div>
              <dt>模型版本</dt>
              <dd>{{ selected.version_id || '未知' }}</dd>
            </div>
            <div>
              <dt>研究范围</dt>
              <dd>{{ selected.timerange || '未知' }}</dd>
            </div>
            <div>
              <dt>进度 / 阶段</dt>
              <dd>
                {{
                  selected.progress?.percent === undefined ||
                  selected.progress?.percent === null
                    ? '未知'
                    : selected.progress.percent + '%'
                }}
                / {{ selected.progress?.phase || '未知' }}
              </dd>
            </div>
            <div>
              <dt>失败代码</dt>
              <dd>{{ selected.error_code ?? '未提供' }}</dd>
            </div>
          </dl>
          <h3>来源统计</h3>
          <dl class="metric-grid">
            <div v-for="metric in metricRows" :key="metric.label">
              <dt>{{ metric.label }}</dt>
              <dd>{{ metric.value }}</dd>
            </div>
          </dl>
          <p class="scope-note">
            源统计的完整费用、资金费、滑点及压力场景尚未核对；这里不称为完整成本后净收益。
          </p>
          <h3>已有身份指纹</h3>
          <dl class="detail-list identity-list">
            <div v-for="fact in facts" :key="fact.label">
              <dt>{{ fact.label }}</dt>
              <dd>{{ fact.value }}</dd>
            </div>
          </dl>
          <section v-if="selected.gate?.reasons?.length" class="gate-reasons">
            <h3>源裁定原因</h3>
            <ul>
              <li v-for="(reason, index) in selected.gate.reasons" :key="index">
                {{ reason }}
              </li>
            </ul>
          </section>
        </template>
        <p v-else class="empty-detail">
          {{
            state.selectedRunId
              ? '未取得该轮详情；不会显示其他轮次的产物。'
              : '从运行账选择一轮，查看独立执行状态、研究裁定与产物身份。'
          }}
        </p>
        <section class="evidence-gap">
          <h3>新实验协议仍需补证</h3>
          <ul>
            <li>数据库权威预注册与完整实验族清单</li>
            <li>120 配置上限、固定种子与比较方法</li>
            <li>时间块统计、全族校正与单次最终留出</li>
            <li>数据、代码、策略、成本、执行和风险身份一致</li>
          </ul>
          <p>未接通的证据保持未知，不从旧研究通过推导 Demo 资格。</p>
        </section>
        <nav class="history-links" aria-label="相关证据工作区">
          <RouterLink to="/quant/research">策略研究</RouterLink
          ><RouterLink to="/quant/operations">运维与证据</RouterLink>
        </nav>
        <p class="history-note">
          历史产物尚未建立按本轮身份精确定位的读取合同；当前仅展示已返回证据，历史研究结论不授予上线批准。
        </p>
      </aside>
    </div>
  </main>
</template>

<style scoped>
.experiments-workspace {
  --base: #0c1b2a;
  --work: #132b3e;
  --line: #29485c;
  --text: #e5edf4;
  --muted: #a2b8c9;
  --accent: #69b4e4;
  min-width: 0;
  min-height: 100%;
  padding: 28px;
  color: var(--text);
  background: var(--base);
  font-family:
    'IBM Plex Sans', 'Source Han Sans SC', 'Noto Sans SC', sans-serif;
}
.workspace-heading {
  display: flex;
  gap: 24px;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 24px;
}
.eyebrow {
  color: var(--accent);
  font-size: 11px;
  margin: 0 0 8px;
}
h1 {
  font-size: 28px;
  font-weight: 650;
  margin: 0;
  line-height: 1.3;
}
.intro {
  color: var(--muted);
  font-size: 13px;
  margin: 10px 0 0;
  line-height: 1.8;
}
button,
input,
select {
  color: var(--text);
  background: #19374d;
  border: 1px solid var(--line);
  border-radius: 5px;
  font: inherit;
}
button {
  cursor: pointer;
  padding: 9px 14px;
}
button:disabled {
  cursor: wait;
  opacity: 0.65;
}
button:focus-visible,
input:focus-visible,
select:focus-visible,
a:focus-visible,
.table-viewport:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}
.read-error,
.detail-error {
  color: #f3cd96;
  border-left: 3px solid #e1ab61;
  background: #352d22;
  padding: 12px 16px;
  line-height: 1.7;
  font-size: 12px;
  margin-bottom: 16px;
}
.snapshot-band {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px 20px;
  padding: 17px 0;
  margin-bottom: 24px;
  border-block: 1px solid var(--line);
}
.snapshot-band div {
  min-width: 0;
  border-right: 1px solid var(--line);
  padding-right: 12px;
}
.snapshot-band div:last-child {
  border: 0;
}
.snapshot-band span {
  display: block;
  color: var(--muted);
  font-size: 11px;
  margin-bottom: 7px;
}
.snapshot-band strong {
  display: block;
  font-size: 13px;
  font-weight: 500;
  overflow-wrap: anywhere;
}
.caution {
  color: #f2c27b;
}
.experiment-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 348px;
  gap: 24px;
  align-items: start;
}
.run-ledger,
.run-inspector {
  min-width: 0;
  background: var(--work);
  border-top: 2px solid var(--accent);
}
.ledger-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 20px 20px 15px;
}
h2 {
  font-size: 16px;
  font-weight: 600;
  margin: 0;
  overflow-wrap: anywhere;
}
.ledger-heading p {
  margin: 7px 0 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.7;
}
.record-count {
  white-space: nowrap;
  color: var(--muted);
  font-size: 11px;
}
.filters {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  padding: 0 20px 20px;
}
.filters label {
  display: flex;
  flex-direction: column;
  gap: 7px;
  font-size: 11px;
  color: var(--muted);
  min-width: 0;
}
.filters input {
  width: 220px;
  max-width: 100%;
  padding: 8px 10px;
}
.filters select {
  padding: 8px 10px;
  max-width: 100%;
}
.table-viewport {
  max-width: 100%;
  overflow: auto;
}
table {
  width: 100%;
  min-width: 700px;
  text-align: left;
  border-collapse: collapse;
  font-size: 12px;
}
thead {
  background: #102536;
  color: var(--muted);
}
th,
td {
  border-bottom: 1px solid var(--line);
  padding: 15px 16px;
  vertical-align: top;
}
thead th {
  font-weight: 500;
  white-space: nowrap;
}
tbody th {
  font-weight: 500;
}
tbody th button {
  background: none;
  border: 0;
  padding: 0;
  max-width: 220px;
  text-align: left;
  overflow-wrap: anywhere;
  font-family: ui-monospace, monospace;
}
small {
  display: block;
  color: var(--muted);
  font-weight: 400;
  line-height: 1.6;
  margin-top: 7px;
}
.time-cell {
  max-width: 200px;
  font-size: 11px;
  overflow-wrap: anywhere;
}
.status {
  white-space: nowrap;
}
.selected {
  background: #1a394e;
}
.empty-state {
  color: var(--muted);
  font-size: 13px;
  line-height: 1.8;
  padding: 24px 20px;
}
.run-inspector {
  padding: 20px;
}
.inspector-heading h2 {
  font-family: ui-monospace, monospace;
  font-size: 15px;
}
.loading-text {
  font-size: 11px;
  color: var(--accent);
}
.status-pair {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  border-block: 1px solid var(--line);
  padding: 15px 0;
  margin: 20px 0 10px;
}
dt {
  color: var(--muted);
  font-size: 11px;
}
dd {
  margin: 6px 0 0;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.scope-note,
.history-note {
  font-size: 11px;
  line-height: 1.8;
  color: var(--muted);
  margin: 8px 0;
}
.detail-list {
  margin: 18px 0;
}
.detail-list div {
  display: grid;
  grid-template-columns: 95px minmax(0, 1fr);
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--line);
}
.detail-list dd {
  text-align: right;
  margin: 0;
}
.identity-list dd {
  font-family: ui-monospace, monospace;
  font-size: 11px;
}
h3 {
  font-size: 12px;
  font-weight: 500;
  margin: 22px 0 12px;
}
.metric-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px 12px;
  padding: 14px;
  background: #102536;
  margin: 0;
}
.metric-grid dd {
  font-variant-numeric: tabular-nums;
  line-height: 1.6;
}
.gate-reasons ul,
.evidence-gap ul {
  padding-left: 17px;
  margin: 0;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.9;
  overflow-wrap: anywhere;
}
.evidence-gap {
  padding: 1px 14px 14px;
  border-left: 2px solid #e1ab61;
  background: #102536;
  margin-top: 24px;
}
.evidence-gap p {
  color: #f2c27b;
  font-size: 11px;
  line-height: 1.8;
  margin: 12px 0 0;
}
.empty-detail {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.8;
  margin: 22px 0;
}
.history-links {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 22px;
  padding-top: 18px;
  border-top: 1px solid var(--line);
}
.history-links a {
  font-size: 11px;
  color: var(--accent);
  text-decoration: underline;
  text-underline-offset: 3px;
}
@media (min-width: 1800px) {
  .experiments-workspace {
    padding: 36px;
  }
  .experiment-grid {
    grid-template-columns: minmax(0, 1fr) 390px;
  }
}
@media (max-width: 1200px) {
  .experiment-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
@media (max-width: 767px) {
  .experiments-workspace {
    padding: 18px 12px;
  }
  .workspace-heading {
    flex-direction: column;
    align-items: flex-start;
    gap: 16px;
  }
  h1 {
    font-size: 24px;
  }
  .snapshot-band {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 18px 12px;
  }
  .snapshot-band div:nth-child(2) {
    border: 0;
  }
  .ledger-heading {
    padding: 16px;
    align-items: flex-start;
  }
  .filters {
    padding: 0 16px 16px;
  }
  .run-inspector {
    padding: 18px;
  }
  .experiment-grid {
    gap: 18px;
  }
}
</style>

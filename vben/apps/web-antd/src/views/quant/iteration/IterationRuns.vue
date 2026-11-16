<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import { getIterationRun, getIterationRuns } from '#/api/freqtrade';
import {
  createRunDetail,
  createRunSnapshot,
  executionView,
  researchView,
  formatRunMetric,
  formatRunTime,
  refreshRunDetail,
  refreshRunSnapshot,
  runMetrics,
  validRunId,
} from './runDisplay';

const route = useRoute();
const router = useRouter();
const snapshot = reactive(createRunSnapshot());
const detail = reactive(createRunDetail());
const search = ref('');
const status = ref('all');
const page = ref(1);
let active = true;
let timer: ReturnType<typeof setInterval> | undefined;
const target = computed(() => {
  const value = route.query.run_id;
  if (value === undefined) return { id: '', invalid: false };
  return validRunId(value)
    ? { id: value, invalid: false }
    : { id: '', invalid: true };
});
const requestedTarget = computed(() =>
  Array.isArray(route.query.run_id)
    ? route.query.run_id.join(' / ')
    : String(route.query.run_id ?? ''),
);
const filtered = computed(() =>
  snapshot.items.filter(
    (item) =>
      (status.value === 'all' || item.execution_status === status.value) &&
      [item.run_id, item.strategy, item.model].some(
        (value) =>
          typeof value === 'string' &&
          value.toLowerCase().includes(search.value.trim().toLowerCase()),
      ),
  ),
);
const pages = computed(() =>
  Math.max(1, Math.ceil(filtered.value.length / 10)),
);
const rows = computed(() =>
  filtered.value.slice((page.value - 1) * 10, page.value * 10),
);
watch([search, status], () => {
  page.value = 1;
});
watch(pages, (count) => {
  if (page.value > count) page.value = count;
});
const values = computed(() => runMetrics(detail.item));
const reasons = computed(() => detail.item?.gate?.reasons ?? []);
const progress = computed(() => detail.item?.progress?.percent);
const evidence = computed(
  () => detail.item?.evidence ?? detail.item?.entry?.evidence,
);
const evidenceText = computed(() =>
  evidence.value
    ? JSON.stringify(evidence.value, null, 2)
    : '尚无产物引用或指纹，不能补写证据。',
);
function time(item: Record<string, unknown>) {
  return formatRunTime(
    item.updated_at ??
      item.finished_at ??
      item.completed_at ??
      item.timestamp ??
      item.started_at,
  );
}
async function select(id: string) {
  await router.replace({ query: { ...route.query, run_id: id } });
}
async function loadSelected() {
  if (target.value.invalid || !target.value.id) {
    detail.request += 1;
    detail.item = null;
    detail.loading = false;
    detail.selectedRunId = '';
    detail.error = '';
    return;
  }
  await refreshRunDetail(detail, target.value.id, getIterationRun);
}
async function refresh() {
  await Promise.all([
    refreshRunSnapshot(snapshot, getIterationRuns),
    loadSelected(),
  ]);
}
watch(
  () => route.query.run_id,
  () => {
    if (active) void loadSelected();
  },
  { immediate: true },
);
onMounted(() => {
  void refreshRunSnapshot(snapshot, getIterationRuns);
  timer = setInterval(() => {
    if (document.visibilityState === 'visible' && !detail.loading)
      void refresh();
  }, 15_000);
});
onUnmounted(() => {
  active = false;
  clearInterval(timer);
  snapshot.request += 1;
  detail.request += 1;
});
</script>

<template>
  <section class="run-workbench" aria-label="历史运行队列">
    <header class="run-toolbar">
      <div>
        <h2>运行队列</h2>
        <p>执行完成与研究结论分别记录；没有成本与冻结证据时保持未知。</p>
      </div>
      <button
        data-testid="refresh-runs"
        type="button"
        :disabled="snapshot.loading || detail.loading"
        @click="refresh"
      >
        {{ snapshot.loading ? '正在读取' : '刷新运行证据' }}
      </button>
    </header>
    <p v-if="snapshot.error" class="run-warning" role="alert">
      {{ snapshot.error }}。{{
        snapshot.hasSnapshot ? '保留上次成功读取的运行列表。' : '尚无成功快照。'
      }}
    </p>
    <p v-if="target.invalid" class="run-warning" role="alert">
      运行参数无效，保留原目标：{{ requestedTarget }}。请从运行列表重新选择。
    </p>
    <div class="run-layout">
      <div class="run-queue">
        <div class="run-filters">
          <label
            >查找运行<input
              v-model="search"
              type="search"
              placeholder="运行编号、策略或模型"
          /></label>
          <label
            >执行状态<select v-model="status">
              <option value="all">全部状态</option>
              <option value="running">运行中</option>
              <option value="succeeded">执行成功</option>
              <option value="failed">执行失败</option>
              <option value="disabled">已停用</option>
            </select></label
          >
        </div>
        <div
          class="run-table-scroll"
          tabindex="0"
          aria-label="历史运行表，可横向滚动"
        >
          <table>
            <thead>
              <tr>
                <th>运行 / 策略</th>
                <th>执行状态</th>
                <th>研究裁定</th>
                <th>更新时间（北京时间）</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="item in rows"
                :key="item.run_id"
                :class="{ selected: target.id === item.run_id }"
              >
                <td>
                  <button
                    type="button"
                    :data-run-id="item.run_id"
                    :aria-pressed="target.id === item.run_id"
                    @click="select(item.run_id)"
                  >
                    {{ item.run_id }}</button
                  ><small
                    >{{ item.strategy || '策略未记录' }} /
                    {{
                      item.model ||
                      item.identifier ||
                      item.version_id ||
                      '模型未记录'
                    }}</small
                  >
                </td>
                <td>{{ executionView(item.execution_status).label }}</td>
                <td>{{ researchView(item.research_verdict).label }}</td>
                <td>{{ time(item) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="!rows.length" class="run-empty">
          {{
            snapshot.hasSnapshot
              ? '本次读取范围没有匹配运行。'
              : snapshot.loading
                ? '正在读取运行列表…'
                : '运行列表尚未读取。'
          }}
        </p>
        <div class="run-pagination">
          <span
            >本次读取 {{ snapshot.items.length }} /
            {{ snapshot.total }} 轮；筛选 {{ filtered.length }} 轮</span
          ><button :disabled="page <= 1" @click="page -= 1">上一页</button
          ><span>{{ page }} / {{ pages }}</span
          ><button :disabled="page >= pages" @click="page += 1">下一页</button>
        </div>
      </div>
      <aside
        class="run-inspector"
        data-testid="run-inspector"
        aria-label="所选运行证据"
      >
        <h3>所选运行</h3>
        <p v-if="!target.id">选择一轮历史运行，按其运行编号读取证据。</p>
        <template v-else>
          <p class="run-id">{{ target.id }}</p>
          <p v-if="detail.error" class="run-warning" role="alert">
            指定运行证据无法读取。{{
              detail.item
                ? '保留该轮上次成功快照。'
                : '未展示其他运行代替该目标。'
            }}
          </p>
          <p v-if="detail.loading">正在读取这一轮证据…</p>
          <template v-if="detail.item">
            <dl class="run-facts">
              <div>
                <dt>执行状态</dt>
                <dd>{{ executionView(detail.item.execution_status).label }}</dd>
              </div>
              <div>
                <dt>研究裁定</dt>
                <dd>{{ researchView(detail.item.research_verdict).label }}</dd>
              </div>
              <div>
                <dt>策略</dt>
                <dd>{{ detail.item.strategy || '未记录' }}</dd>
              </div>
              <div>
                <dt>模型 / 版本</dt>
                <dd>
                  {{
                    detail.item.model ||
                    detail.item.identifier ||
                    detail.item.version_id ||
                    '未记录'
                  }}
                </dd>
              </div>
              <div>
                <dt>数据区间</dt>
                <dd>{{ detail.item.timerange || '未记录' }}</dd>
              </div>
              <div>
                <dt>更新时间（北京时间）</dt>
                <dd>{{ time(detail.item) }}</dd>
              </div>
              <div>
                <dt>结果来源</dt>
                <dd>
                  {{
                    detail.item.result_source || evidence?.source || '未记录'
                  }}
                </dd>
              </div>
            </dl>
            <p v-if="detail.item.error_code" class="run-warning">
              任务记录了失败原因，请在下方原始证据核对。
            </p>
            <label v-if="typeof progress === 'number'" class="run-progress"
              >当前执行进度 {{ progress }}%<progress
                max="100"
                :value="progress"
            /></label>
            <p v-if="detail.item.progress?.phase">
              {{ detail.item.progress.phase }}
            </p>
            <dl class="run-metrics">
              <div>
                <dt>平仓交易</dt>
                <dd>{{ formatRunMetric(values.trades, '', 0) }}</dd>
              </div>
              <div>
                <dt>历史收益率</dt>
                <dd>{{ formatRunMetric(values.profit_pct, '%') }}</dd>
              </div>
              <div>
                <dt>最大回撤</dt>
                <dd>{{ formatRunMetric(values.max_drawdown_pct, '%') }}</dd>
              </div>
              <div>
                <dt>Sharpe</dt>
                <dd>{{ formatRunMetric(values.sharpe) }}</dd>
              </div>
            </dl>
            <p class="run-warning">
              费用证据未齐。历史指标不授予 Demo 准入或实际交易批准。
            </p>
            <ul v-if="reasons.length">
              <li v-for="(reason, index) in reasons" :key="index">
                {{ reason }}
              </li>
            </ul>
            <details>
              <summary>产物引用与冻结指纹</summary>
              <pre>{{ evidenceText }}</pre>
            </details>
            <RouterLink
              :to="{ path: '/quant/detail', query: { run_id: target.id } }"
              >按该运行编号打开历史明细</RouterLink
            >
          </template>
        </template>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.run-workbench {
  color: #e5edf4;
  min-width: 0;
}
.run-toolbar {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
  margin-bottom: 18px;
}
.run-toolbar h2 {
  font-size: 20px;
  font-weight: 600;
  margin: 0 0 6px;
}
.run-toolbar p,
.run-empty {
  color: #a2b8c9;
  font-size: 13px;
  margin: 0;
}
.run-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.5fr) minmax(280px, 1fr);
  border: 1px solid #29485c;
  background: #132b3e;
}
.run-queue {
  min-width: 0;
}
.run-filters {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  padding: 14px;
  border-bottom: 1px solid #29485c;
}
.run-filters label {
  display: grid;
  gap: 6px;
  font-size: 12px;
  color: #a2b8c9;
}
.run-filters input,
.run-filters select {
  background: #0c1b2a;
  border: 1px solid #426278;
  color: #e5edf4;
  padding: 8px;
  max-width: 100%;
  border-radius: 3px;
}
.run-table-scroll {
  overflow-x: auto;
  max-width: 100%;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  min-width: 650px;
}
th,
td {
  text-align: left;
  vertical-align: top;
  padding: 12px;
  border-bottom: 1px solid #29485c;
}
th {
  font-weight: 500;
  color: #a2b8c9;
}
td small {
  display: block;
  color: #a2b8c9;
  margin-top: 6px;
  max-width: 240px;
  overflow-wrap: anywhere;
}
tr.selected {
  background: #193b50;
}
button {
  color: #e5edf4;
  border: 1px solid #426278;
  background: #17364b;
  border-radius: 3px;
  padding: 7px 10px;
  cursor: pointer;
  font-size: 13px;
}
td button {
  border: 0;
  background: transparent;
  padding: 0;
  color: #8bc8ed;
  text-align: left;
  overflow-wrap: anywhere;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
button:focus-visible,
a:focus-visible,
input:focus-visible,
select:focus-visible,
.run-table-scroll:focus-visible {
  outline: 2px solid #69b4e4;
  outline-offset: 3px;
}
.run-pagination {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  padding: 14px;
  color: #a2b8c9;
  font-size: 12px;
}
.run-pagination span:first-child {
  margin-right: auto;
}
.run-inspector {
  border-left: 1px solid #29485c;
  padding: 18px;
  min-width: 0;
  font-size: 13px;
}
.run-inspector h3 {
  font-size: 17px;
  margin: 0 0 10px;
  font-weight: 600;
}
.run-id {
  font-family: monospace;
  overflow-wrap: anywhere;
  color: #8bc8ed;
}
.run-facts,
.run-metrics {
  margin: 16px 0;
  display: grid;
  gap: 12px;
}
.run-facts div {
  display: grid;
  grid-template-columns: 110px minmax(0, 1fr);
  gap: 10px;
}
.run-facts dt,
.run-metrics dt {
  color: #a2b8c9;
}
.run-facts dd,
.run-metrics dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.run-metrics {
  grid-template-columns: 1fr 1fr;
  border-top: 1px solid #29485c;
  padding-top: 14px;
}
.run-metrics dd {
  font-size: 20px;
  font-variant-numeric: tabular-nums;
  margin-top: 4px;
}
.run-warning {
  color: #f0c17b;
  border-left: 2px solid #b38445;
  padding: 8px 12px;
  background: #273340;
  overflow-wrap: anywhere;
  font-size: 13px;
}
.run-empty {
  padding: 24px;
}
.run-progress {
  display: grid;
  gap: 5px;
}
progress {
  width: 100%;
  accent-color: #69b4e4;
}
details {
  margin: 16px 0;
}
summary {
  cursor: pointer;
  color: #8bc8ed;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
  max-height: 360px;
  overflow: auto;
}
a {
  color: #8bc8ed;
}
ul {
  padding-left: 20px;
}
@media (max-width: 1000px) {
  .run-layout {
    grid-template-columns: minmax(0, 1fr);
  }
  .run-inspector {
    border-left: 0;
    border-top: 1px solid #29485c;
  }
}
@media (max-width: 500px) {
  .run-toolbar {
    flex-direction: column;
  }
  .run-filters {
    flex-direction: column;
  }
  .run-filters input,
  .run-filters select {
    width: 100%;
  }
  .run-inspector {
    padding: 14px;
  }
  .run-facts div {
    grid-template-columns: 95px minmax(0, 1fr);
  }
}
</style>

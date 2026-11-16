<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { RouterLink } from 'vue-router';

import { createDataInventoryReader, selectDatasets } from './dataQuality';

const props = defineProps<{
  loadInventory: (signal: AbortSignal) => Promise<unknown>;
}>();
const reader = createDataInventoryReader((signal) =>
  props.loadInventory(signal),
);
const state = reader.state;
const pair = ref('');
const timeframe = ref('');
const selectedId = ref('');
const pairs = computed(() =>
  [...new Set(state.snapshot?.datasets.map((row) => row.pair) ?? [])].sort(),
);
const timeframes = computed(() =>
  [
    ...new Set(state.snapshot?.datasets.map((row) => row.timeframe) ?? []),
  ].sort(),
);
const rows = computed(() =>
  selectDatasets(state.snapshot?.datasets ?? [], pair.value, timeframe.value),
);
const selected = computed(
  () => rows.value.find((row) => row.id === selectedId.value) ?? rows.value[0],
);
const counters = [
  { key: 'candles', label: 'K 线记录' },
  { key: 'ticks', label: '逐笔记录' },
  { key: 'orderbooks', label: '盘口记录' },
  { key: 'series_points', label: '序列数据点' },
];
const gates = [
  { title: '365 天逐频率覆盖', requirement: '连续区间、缺口清单与冻结版本' },
  {
    title: '闭盘与可用时间',
    requirement: 'closed-only、available-at 与时区约束',
  },
  { title: '盘口序列连续性', requirement: '快照、增量序号与断档校验' },
  { title: '冻结数据指纹', requirement: '不可变 URI、SHA-256 与质量裁定' },
];
const statusText = computed(() =>
  state.loading
    ? '正在读取清单'
    : state.status === 'stale'
      ? '保留上次成功快照'
      : state.status === 'ready'
        ? '已取得清单事实'
        : '清单尚未取得',
);
watch(
  rows,
  (current) => {
    if (!current.some((row) => row.id === selectedId.value))
      selectedId.value = current[0]?.id ?? '';
  },
  { immediate: true },
);
function count(value: null | number | undefined) {
  return value == null ? '未提供' : value.toLocaleString('en-US');
}
function time(value: null | number | string | undefined) {
  if (value == null) return '未提供';
  if (typeof value === 'string' && !/(?:Z|[+-]\d{2}:?\d{2})$/.test(value))
    return `${value}（时区未提供）`;
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? date.toISOString().replace('T', ' ').replace('.000Z', ' UTC')
    : '未提供';
}
function sourceState(value: string | undefined) {
  if (value === 'running') return '运行中';
  if (value === 'stopped') return '已停止';
  return value && value !== 'unknown' ? value : '未提供';
}
onMounted(() => {
  void reader.refresh();
});
onUnmounted(reader.dispose);
</script>

<template>
  <section class="data-workspace" aria-labelledby="data-quality-title">
    <header class="page-heading">
      <div>
        <p class="eyebrow">DATA / EVIDENCE INVENTORY</p>
        <h1 id="data-quality-title">数据与质量</h1>
        <p class="intro">核对数据存量、频率与来源，逐项查看研究需要的证据。</p>
      </div>
      <div class="heading-actions">
        <button
          data-refresh
          :disabled="state.loading"
          type="button"
          @click="reader.refresh"
        >
          {{ state.loading ? '读取中…' : '刷新清单' }}
        </button>
      </div>
    </header>

    <div
      class="read-status"
      role="status"
      aria-live="polite"
      :data-state="state.status"
    >
      <span class="status-dot" aria-hidden="true"></span
      ><strong>{{ statusText }}</strong>
      <span>上次成功读取：{{ time(state.fetchedAt) }}</span>
      <span v-if="state.error" class="error-text">{{ state.error }}</span>
    </div>

    <dl class="inventory-counts" aria-label="数据存量">
      <div
        v-for="counter in counters"
        :key="counter.key"
        :data-count="counter.key"
      >
        <dt>{{ counter.label }}</dt>
        <dd>{{ count(state.snapshot?.counts[counter.key]) }}</dd>
      </div>
      <div>
        <dt>引擎 / 数据库</dt>
        <dd class="database-name">
          {{ state.snapshot?.engine ?? '未提供'
          }}<small>{{
            state.snapshot?.databaseName ?? '数据库名未提供'
          }}</small>
        </dd>
      </div>
    </dl>

    <section
      class="quality-strip"
      data-quality-gates
      aria-labelledby="quality-gates-title"
    >
      <div class="section-title">
        <div>
          <p class="eyebrow">RESEARCH EVIDENCE</p>
          <h2 id="quality-gates-title">质量裁定待补齐</h2>
        </div>
        <p>首末时间与记录数无法证明连续覆盖</p>
      </div>
      <div
        class="table-scroll"
        tabindex="0"
        role="region"
        aria-label="研究质量证据表，可横向滚动"
      >
        <table class="quality-table">
          <thead>
            <tr>
              <th scope="col">核对项</th>
              <th scope="col">裁定证据</th>
              <th scope="col">需要补齐</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="gate in gates" :key="gate.title">
              <th scope="row">{{ gate.title }}</th>
              <td><span class="unavailable">未取得证据</span></td>
              <td>{{ gate.requirement }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="evidence-note">
        当前接口提供数据清单，尚未提供研究准入所需的逐项质量证据。
      </p>
    </section>

    <div class="inventory-grid">
      <section class="inventory-panel" aria-labelledby="inventory-title">
        <div class="section-title">
          <div>
            <p class="eyebrow">PAIR × FREQUENCY</p>
            <h2 id="inventory-title">数据清单</h2>
          </div>
          <span
            >{{ rows.length }} /
            {{ state.snapshot?.datasets.length ?? '—' }} 组</span
          >
        </div>
        <div class="filter-row">
          <label
            >交易对<select v-model="pair" name="pair">
              <option value="">全部交易对</option>
              <option v-for="item in pairs" :key="item" :value="item">
                {{ item }}
              </option>
            </select></label
          >
          <label
            >频率<select v-model="timeframe" name="timeframe">
              <option value="">全部频率</option>
              <option v-for="item in timeframes" :key="item" :value="item">
                {{ item }}
              </option>
            </select></label
          >
        </div>
        <div
          class="table-scroll"
          tabindex="0"
          role="region"
          aria-label="数据清单表，可横向滚动"
        >
          <table data-inventory-table class="dataset-table">
            <thead>
              <tr>
                <th scope="col">交易对 / 市场</th>
                <th scope="col">频率</th>
                <th scope="col" class="numeric">记录数</th>
                <th scope="col">首条 UTC</th>
                <th scope="col">末条 UTC</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in rows"
                :key="row.id"
                :data-selected="selected?.id === row.id"
              >
                <th scope="row">
                  <button
                    type="button"
                    :aria-pressed="selected?.id === row.id"
                    @click="selectedId = row.id"
                  >
                    {{ row.pair
                    }}<small>{{ row.exchange }} · {{ row.market }}</small>
                  </button>
                </th>
                <td>{{ row.timeframe }}</td>
                <td class="numeric mono">{{ count(row.rows) }}</td>
                <td class="timestamp">{{ time(row.firstTimestamp) }}</td>
                <td class="timestamp">{{ time(row.lastTimestamp) }}</td>
              </tr>
              <tr v-if="!rows.length">
                <td colspan="5" class="empty-state">
                  {{
                    !state.snapshot
                      ? '清单尚未取得'
                      : state.snapshot.datasets.length
                        ? '当前筛选没有清单记录'
                        : '数据源尚无清单记录'
                  }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <aside
        data-dataset-inspector
        class="inspector"
        aria-labelledby="dataset-inspector-title"
      >
        <p class="eyebrow">FACT INSPECTOR</p>
        <h2 id="dataset-inspector-title">所选数据事实</h2>
        <template v-if="selected">
          <h3>{{ selected.pair }}</h3>
          <p class="selected-meta">
            {{ selected.timeframe }} · {{ selected.candleType }}
          </p>
          <dl class="fact-list">
            <div>
              <dt>来源交易所</dt>
              <dd>{{ selected.exchange }}</dd>
            </div>
            <div>
              <dt>市场</dt>
              <dd>{{ selected.market }}</dd>
            </div>
            <div>
              <dt>记录数</dt>
              <dd class="mono">{{ count(selected.rows) }}</dd>
            </div>
            <div>
              <dt>首条记录 UTC</dt>
              <dd class="mono">{{ time(selected.firstTimestamp) }}</dd>
            </div>
            <div>
              <dt>末条记录 UTC</dt>
              <dd class="mono">{{ time(selected.lastTimestamp) }}</dd>
            </div>
            <div>
              <dt>连续覆盖 / 闭盘校验</dt>
              <dd class="unavailable">未取得证据</dd>
            </div>
            <div>
              <dt>指纹与不可变 URI</dt>
              <dd class="unavailable">未取得证据</dd>
            </div>
          </dl>
          <p class="evidence-note">
            记录时间仅描述端点。缺口、重复与可用时间需要独立核验。
          </p>
        </template>
        <p v-else class="empty-state">
          选择一组清单查看事实；没有匹配数据时，证据保持未知。
        </p>
      </aside>
    </div>

    <section class="source-panel" aria-labelledby="source-title">
      <div class="section-title">
        <div>
          <p class="eyebrow">COLLECTION / SOURCES</p>
          <h2 id="source-title">采集状态与来源</h2>
        </div>
        <RouterLink to="/quant/operations"
          >运维与证据 <span aria-hidden="true">↗</span></RouterLink
        >
      </div>
      <dl class="collection-facts">
        <div>
          <dt>采集进程</dt>
          <dd>{{ sourceState(state.snapshot?.collectorState) }}</dd>
        </div>
        <div>
          <dt>同步进程</dt>
          <dd>{{ sourceState(state.snapshot?.syncState) }}</dd>
        </div>
        <div>
          <dt>最近成功同步</dt>
          <dd>{{ time(state.snapshot?.syncLastSuccess) }}</dd>
        </div>
        <div>
          <dt>磁盘状态</dt>
          <dd>{{ sourceState(state.snapshot?.collectorDiskState) }}</dd>
        </div>
        <div>
          <dt>数据库版本</dt>
          <dd>{{ state.snapshot?.schemaVersion ?? '未提供' }}</dd>
        </div>
      </dl>
      <p v-if="state.snapshot?.collectorError" class="source-error">
        采集错误：{{ state.snapshot.collectorError }}
      </p>
      <p
        v-for="error in state.snapshot?.syncErrors ?? []"
        :key="error"
        class="source-error"
      >
        {{ error }}
      </p>
      <div
        class="table-scroll"
        tabindex="0"
        role="region"
        aria-label="数据来源表，可横向滚动"
      >
        <table class="sources-table">
          <thead>
            <tr>
              <th scope="col">来源标识</th>
              <th scope="col">格式</th>
              <th scope="col" class="numeric">来源记录数</th>
              <th scope="col">来源更新时间 UTC</th>
              <th scope="col">错误事实</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="source in state.snapshot?.sources ?? []"
              :key="source.key"
            >
              <th scope="row" class="source-key">{{ source.key }}</th>
              <td>{{ source.kind }}</td>
              <td class="numeric mono">{{ count(source.rowCount) }}</td>
              <td class="timestamp">{{ time(source.updatedAt) }}</td>
              <td>{{ source.error ?? '未报告错误' }}</td>
            </tr>
            <tr v-if="!state.snapshot?.sources.length">
              <td colspan="5" class="empty-state">
                {{
                  state.snapshot ? '数据源返回空来源清单' : '来源清单尚未取得'
                }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="evidence-note">
        采集运行状态仅反映服务报告；研究质量与交易资格需各自的权威证据。
      </p>
    </section>
  </section>
</template>

<style scoped>
.data-workspace {
  --base: #0c1b2a;
  --surface: #132b3e;
  --line: #29485c;
  --text: #e5edf4;
  --secondary: #a2b8c9;
  --accent: #69b4e4;
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  padding: clamp(16px, 2vw, 32px);
  background: var(--base);
  color: var(--text);
  font-family:
    'IBM Plex Sans', 'Noto Sans SC', 'Source Han Sans SC', 'PingFang SC',
    sans-serif;
}
.data-workspace *,
.data-workspace *::before,
.data-workspace *::after {
  box-sizing: border-box;
}
.page-heading,
.section-title,
.heading-actions,
.read-status,
.filter-row {
  display: flex;
  align-items: center;
  gap: 16px;
}
.page-heading,
.section-title {
  justify-content: space-between;
}
.page-heading {
  margin-bottom: 24px;
  align-items: flex-start;
}
h1,
h2,
h3,
p,
dl {
  margin: 0;
}
h1 {
  margin-top: 5px;
  font-size: clamp(26px, 2.2vw, 34px);
  font-weight: 650;
  letter-spacing: -0.03em;
}
h2 {
  font-size: 18px;
  font-weight: 600;
}
h3 {
  margin-top: 22px;
  overflow-wrap: anywhere;
  font-size: 21px;
  font-weight: 600;
}
.eyebrow {
  color: var(--accent);
  font-size: 10px;
  font-weight: 650;
  letter-spacing: 0.17em;
  line-height: 1.8;
}
.intro,
.section-title > p,
.section-title > span,
.evidence-note,
.selected-meta {
  color: var(--secondary);
  font-size: 12px;
  line-height: 1.8;
}
.intro {
  margin-top: 9px;
  font-size: 13px;
}
a {
  color: var(--accent);
  font-size: 12px;
  text-decoration: none;
}
a:hover {
  text-decoration: underline;
}
button,
select {
  min-height: 36px;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--surface);
  color: var(--text);
  font: inherit;
  font-size: 12px;
}
button {
  padding: 8px 14px;
  cursor: pointer;
}
button:hover {
  border-color: var(--accent);
}
button:disabled {
  opacity: 0.6;
  cursor: wait;
}
button:focus-visible,
a:focus-visible,
select:focus-visible,
[tabindex]:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}
.read-status {
  flex-wrap: wrap;
  gap: 8px 16px;
  padding: 12px 0;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  font-size: 12px;
  color: var(--secondary);
}
.read-status strong {
  color: var(--text);
  font-weight: 500;
}
.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
}
.read-status[data-state='stale'] .status-dot,
.read-status[data-state='error'] .status-dot {
  background: #e6bb7b;
}
.error-text,
.source-error {
  color: #e6bb7b;
  overflow-wrap: anywhere;
}
.inventory-counts {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr)) minmax(150px, 1.4fr);
  margin: 22px 0 28px;
  gap: 16px;
}
.inventory-counts > div {
  padding-left: 16px;
  border-left: 1px solid var(--line);
  min-width: 0;
}
.inventory-counts > div:first-child {
  padding-left: 0;
  border-left: 0;
}
dt {
  color: var(--secondary);
  font-size: 11px;
  line-height: 1.8;
}
.inventory-counts dd {
  margin: 7px 0 0;
  font-size: clamp(19px, 2vw, 28px);
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
}
.inventory-counts dd.database-name {
  font-size: 14px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.database-name small {
  display: block;
  color: var(--secondary);
  font-size: 11px;
}
.quality-strip,
.source-panel {
  min-width: 0;
  border-top: 2px solid var(--line);
  padding-top: 18px;
}
.section-title {
  margin-bottom: 16px;
  flex-wrap: wrap;
  gap: 8px;
}
.table-scroll {
  min-width: 0;
  max-width: 100%;
  overflow-x: auto;
  overscroll-behavior-x: contain;
}
table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  font-size: 12px;
}
th,
td {
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
  line-height: 1.6;
}
thead th {
  background: #102536;
  color: var(--secondary);
  font-size: 10px;
  font-weight: 500;
  white-space: nowrap;
}
tbody th {
  font-weight: 500;
}
.quality-table {
  min-width: 600px;
}
.quality-table th:first-child {
  width: 27%;
}
.quality-table td:nth-child(2) {
  width: 23%;
}
.quality-table td:last-child {
  color: var(--secondary);
}
.unavailable {
  color: #e6bb7b;
  font-size: 12px;
}
.evidence-note {
  margin-top: 12px;
}
.inventory-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 304px;
  gap: 24px;
  margin: 28px 0;
}
.inventory-panel,
.inspector {
  min-width: 0;
}
.inventory-panel {
  padding-top: 18px;
  border-top: 2px solid var(--accent);
}
.filter-row {
  margin-bottom: 16px;
  gap: 14px;
  flex-wrap: wrap;
}
label {
  display: grid;
  gap: 6px;
  flex: 1;
  min-width: 120px;
  color: var(--secondary);
  font-size: 11px;
}
select {
  padding: 7px 10px;
  width: 100%;
}
.dataset-table {
  min-width: 740px;
}
.dataset-table tbody th {
  padding: 0;
}
.dataset-table button {
  display: block;
  width: 100%;
  text-align: left;
  border: 0;
  border-radius: 0;
  background: transparent;
  font-weight: 600;
  padding: 13px 14px;
}
.dataset-table button small {
  display: block;
  margin-top: 2px;
  color: var(--secondary);
  font-size: 10px;
  font-weight: 400;
}
.dataset-table tr[data-selected='true'] {
  background: var(--surface);
}
.dataset-table tr[data-selected='true'] th {
  box-shadow: inset 3px 0 var(--accent);
}
.numeric {
  text-align: right;
}
.timestamp {
  white-space: nowrap;
  font-size: 11px;
  color: var(--secondary);
  font-variant-numeric: tabular-nums;
}
.mono {
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-variant-numeric: tabular-nums;
}
.empty-state {
  padding: 28px 14px;
  color: var(--secondary);
  font-size: 12px;
  line-height: 1.8;
  text-align: center;
}
.inspector {
  border: 1px solid var(--line);
  padding: 20px;
  background: var(--surface);
}
.fact-list {
  margin-top: 22px;
}
.fact-list > div {
  padding: 10px 0;
  border-top: 1px solid var(--line);
}
.fact-list dd {
  margin: 5px 0 0;
  overflow-wrap: anywhere;
  font-size: 12px;
  line-height: 1.8;
}
.collection-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 18px 32px;
  margin: 18px 0;
}
.collection-facts dd {
  margin: 3px 0 0;
  font-size: 12px;
}
.sources-table {
  min-width: 740px;
}
.source-key {
  max-width: 340px;
  overflow-wrap: anywhere;
}
.source-error {
  margin-bottom: 12px;
  font-size: 12px;
}
@media (min-width: 1700px) {
  .inventory-grid {
    grid-template-columns: minmax(0, 1fr) 360px;
    gap: 32px;
  }
  .data-workspace {
    padding: 32px 40px;
  }
  th,
  td {
    padding-block: 14px;
  }
}
@media (max-width: 1100px) {
  .inventory-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .inspector {
    display: block;
  }
  .fact-list {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 0 20px;
  }
  .inventory-counts {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }
  .inventory-counts > div:last-child {
    grid-column: 1 / -1;
    padding-left: 0;
    border-left: 0;
  }
}
@media (max-width: 600px) {
  .page-heading {
    flex-direction: column;
    gap: 18px;
  }
  .heading-actions {
    width: 100%;
    justify-content: space-between;
    gap: 12px;
  }
  .inventory-counts {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 20px 12px;
  }
  .inventory-counts > div:nth-child(3) {
    padding-left: 0;
    border-left: 0;
  }
  .fact-list {
    grid-template-columns: minmax(0, 1fr);
  }
  .inspector {
    padding: 18px;
  }
  .read-status > span:last-child {
    max-width: 100%;
  }
  .section-title > p {
    width: 100%;
  }
}
</style>

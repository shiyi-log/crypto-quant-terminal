<script setup lang="ts">
import type { FactorSnapshot } from './factorSnapshot';

import { computed, ref } from 'vue';
import { RouterLink } from 'vue-router';

import { selectFactors, snapshotFreshness } from './factorSnapshot';

const props = defineProps<{
  snapshot: FactorSnapshot | null;
  loading: boolean;
  error: string;
  now: number;
}>();
defineEmits<{ refresh: [] }>();
const group = ref('全部');
const alertsOnly = ref(false);
const selectedName = ref('');
const groups = computed(() => [
  '全部',
  ...new Set(props.snapshot?.factors.map((row) => row.group) || []),
]);
const rows = computed(() =>
  selectFactors(props.snapshot, group.value, alertsOnly.value),
);
const selected = computed(
  () =>
    rows.value.find((row) => row.name === selectedName.value) || rows.value[0],
);
const freshness = computed(() =>
  snapshotFreshness(props.snapshot?.generatedMs ?? null, props.now),
);
const freshnessText = computed(
  () =>
    ({
      fresh: '一小时内历史快照',
      stale: '快照已过期',
      unknown: '快照时间未知',
    })[freshness.value],
);
const alertCount = computed(
  () =>
    props.snapshot?.factors.filter((row) => row.alert === true).length ?? null,
);
function display(value: null | string) {
  return value ?? '未知';
}
</script>

<template>
  <main class="factor-workspace" aria-labelledby="factor-title">
    <header class="workspace-heading">
      <div>
        <p class="source-label">研究 / 历史横截面监控</p>
        <h1 id="factor-title">因子实验室</h1>
        <p class="intro">
          比较历史基线与近期统计，逐项查看漂移和证据。这里不产生交易信号。
        </p>
      </div>
      <button
        data-testid="refresh-factors"
        type="button"
        :disabled="loading"
        @click="$emit('refresh')"
      >
        {{ loading ? '正在读取' : '刷新监控快照' }}
      </button>
    </header>
    <div v-if="error" role="alert" class="read-error">{{ error }}</div>
    <section class="provenance-band" aria-label="来源与时效">
      <div><span>监控范围</span><strong>旧研究 / 日线横截面</strong></div>
      <div>
        <span>源快照时间</span
        ><strong>{{ snapshot?.generatedAt ?? '未知' }}</strong>
      </div>
      <div>
        <span>读取状态</span
        ><strong
          :class="{ caution: freshness !== 'fresh' || Boolean(error) }"
          >{{
            error
              ? snapshot
                ? '保留旧快照 · ' + freshnessText
                : '尚无成功快照'
              : freshnessText
          }}</strong
        >
      </div>
      <div>
        <span>明确告警</span
        ><strong
          >{{ alertCount ?? '未知'
          }}<small v-if="snapshot">
            / {{ snapshot.factors.length }} 个因子</small
          ></strong
        >
      </div>
    </section>
    <div class="research-grid">
      <section class="factor-ledger" aria-labelledby="factor-ledger-title">
        <div class="ledger-heading">
          <div>
            <h2 id="factor-ledger-title">因子观察表</h2>
            <p>零值按零显示；缺字段保留未知。</p>
          </div>
          <div class="filters">
            <label
              >分组
              <select v-model="group" aria-label="因子分组">
                <option v-for="name in groups" :key="name" :value="name">
                  {{ name }}
                </option>
              </select></label
            >
            <label class="check"
              ><input v-model="alertsOnly" type="checkbox" />只看明确告警</label
            >
          </div>
        </div>
        <p v-if="!snapshot" class="empty-state">
          {{ loading ? '正在读取因子快照…' : error || '尚未读取因子快照' }}
        </p>
        <p v-else-if="snapshot.factors.length === 0" class="empty-state">
          本次成功快照没有因子记录。监控程序也可能处于未包含因子的快速模式。
        </p>
        <div
          v-else
          class="table-viewport"
          tabindex="0"
          aria-label="因子统计表，可横向滚动"
        >
          <table>
            <thead>
              <tr>
                <th scope="col">因子 / 分组</th>
                <th scope="col">基线 IC</th>
                <th scope="col">近期 IC</th>
                <th scope="col">近期 t 值</th>
                <th scope="col">告警事实</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in rows"
                :key="row.name"
                :class="{ selected: row.name === selected?.name }"
              >
                <th scope="row">
                  <button
                    type="button"
                    :data-factor="row.name"
                    :aria-pressed="row.name === selected?.name"
                    @click="selectedName = row.name"
                  >
                    {{ row.name }}</button
                  ><small>{{ row.group }}</small>
                </th>
                <td>{{ display(row.baseline) }}</td>
                <td>{{ display(row.recent) }}</td>
                <td>{{ display(row.t) }}</td>
                <td>
                  <span
                    class="fact-state"
                    :class="{ caution: row.alert === true }"
                    >{{
                      row.alert === null
                        ? '未知'
                        : row.alert
                          ? '有告警'
                          : '未报触发'
                    }}</span
                  >
                </td>
              </tr>
            </tbody>
          </table>
          <p v-if="rows.length === 0" class="empty-state">
            当前筛选没有匹配因子。
          </p>
        </div>
      </section>
      <aside
        class="evidence-inspector"
        data-testid="factor-inspector"
        aria-labelledby="factor-inspector-title"
      >
        <h2 id="factor-inspector-title">{{ selected?.name ?? '因子证据' }}</h2>
        <p class="inspector-intro">
          {{ selected?.group ?? '选择一项因子查看已有统计与缺失证据。' }}
        </p>
        <dl v-if="selected" class="detail-list">
          <div>
            <dt>源状态文本</dt>
            <dd>{{ selected.status ?? '未知' }}</dd>
          </div>
          <div>
            <dt>IC 差异</dt>
            <dd>{{ selected.drift ?? '未知' }}</dd>
          </div>
          <div>
            <dt>弱信号标记</dt>
            <dd>
              {{
                selected.weak === null ? '未知' : selected.weak ? '是' : '否'
              }}
            </dd>
          </div>
          <div>
            <dt>符号反转 / 衰减</dt>
            <dd>
              {{
                selected.flip === null
                  ? '未知'
                  : selected.flip
                    ? '反转'
                    : '未报反转'
              }}
              /
              {{
                selected.decay === null
                  ? '未知'
                  : selected.decay
                    ? '衰减'
                    : '未报衰减'
              }}
            </dd>
          </div>
        </dl>
        <div class="identity-gap">
          <strong>版本身份未知</strong>
          <p>
            此接口没有因子版本、输入数据指纹或完整统计产物，不能据统计摘要补造身份。
          </p>
        </div>
        <h3>进入新研究前仍需补证</h3>
        <ul class="evidence-list">
          <li><span>逐币 / 逐周期时序评估</span><em>未接通</em></li>
          <li><span>相关性、市场阶段与消融</span><em>未接通</em></li>
          <li><span>时间块统计与全族校正</span><em>未接通</em></li>
          <li><span>一次性最终留出</span><em>未接通</em></li>
        </ul>
        <p class="eligibility">不授予 Demo 准入</p>
        <p class="boundary">
          历史横截面 IC 不替代 BTC、ETH、TRX
          的单币时序研究；也不证明完整成本后收益。
        </p>
        <nav class="history-links" aria-label="历史研究入口">
          <RouterLink to="/quant/ops">旧监控详情</RouterLink
          ><RouterLink to="/quant/research">历史策略研究</RouterLink
          ><RouterLink to="/quant/research-ledger">研究证据账</RouterLink>
        </nav>
      </aside>
    </div>
  </main>
</template>

<style scoped>
.factor-workspace {
  --base: #0c1b2a;
  --work: #132b3e;
  --line: #29485c;
  --text: #e5edf4;
  --muted: #a2b8c9;
  --accent: #69b4e4;
  min-width: 0;
  min-height: 100%;
  padding: 28px;
  background: var(--base);
  color: var(--text);
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
.source-label {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--accent);
}
h1 {
  margin: 0;
  font-size: 28px;
  line-height: 1.3;
  font-weight: 650;
}
.intro {
  max-width: 70ch;
  margin: 10px 0 0;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.7;
}
button,
select {
  border: 1px solid var(--line);
  background: #19374d;
  color: var(--text);
  border-radius: 5px;
  font: inherit;
}
button {
  padding: 9px 14px;
  cursor: pointer;
}
button:disabled {
  opacity: 0.65;
  cursor: wait;
}
button:focus-visible,
select:focus-visible,
input:focus-visible,
a:focus-visible,
.table-viewport:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}
.read-error {
  padding: 12px 16px;
  margin-bottom: 16px;
  border-left: 3px solid #e1ab61;
  color: #f3cd96;
  background: #352d22;
  font-size: 13px;
}
.provenance-band {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  padding: 17px 0;
  margin-bottom: 24px;
  border-block: 1px solid var(--line);
  gap: 12px 20px;
}
.provenance-band div {
  min-width: 0;
  border-right: 1px solid var(--line);
  padding-right: 12px;
}
.provenance-band div:last-child {
  border: 0;
}
.provenance-band span {
  display: block;
  color: var(--muted);
  font-size: 11px;
  margin-bottom: 7px;
}
.provenance-band strong {
  display: block;
  font-size: 13px;
  font-weight: 500;
  overflow-wrap: anywhere;
}
.provenance-band small {
  font-size: 11px;
  color: var(--muted);
}
.caution {
  color: #f2c27b !important;
}
.research-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 306px;
  align-items: start;
  gap: 24px;
}
.factor-ledger,
.evidence-inspector {
  min-width: 0;
  background: var(--work);
  border-top: 2px solid var(--accent);
}
.ledger-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 16px;
  padding: 20px;
}
h2 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  overflow-wrap: anywhere;
}
.ledger-heading p,
.inspector-intro {
  color: var(--muted);
  margin: 7px 0 0;
  font-size: 12px;
  line-height: 1.6;
}
.filters {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  color: var(--muted);
  font-size: 12px;
}
.filters select {
  padding: 6px 8px;
  margin-left: 6px;
  max-width: 170px;
}
.check {
  display: flex;
  gap: 7px;
  align-items: center;
}
input {
  accent-color: var(--accent);
}
.table-viewport {
  max-width: 100%;
  overflow: auto;
}
table {
  width: 100%;
  min-width: 610px;
  border-collapse: collapse;
  text-align: left;
  font-size: 12px;
}
thead {
  background: #102536;
  color: var(--muted);
}
th,
td {
  padding: 14px 16px;
  border-bottom: 1px solid var(--line);
}
thead th {
  font-weight: 500;
  white-space: nowrap;
}
tbody th {
  font-weight: 500;
}
tbody th button {
  border: 0;
  background: transparent;
  padding: 0;
  text-align: left;
  color: var(--text);
  max-width: 200px;
  overflow-wrap: anywhere;
}
tbody th small {
  display: block;
  color: var(--muted);
  font-weight: 400;
  margin-top: 5px;
}
td {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.selected {
  background: #1a394e;
}
.fact-state {
  color: var(--muted);
}
.empty-state {
  padding: 28px 20px;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.8;
}
.evidence-inspector {
  padding: 20px;
}
.detail-list {
  margin: 22px 0;
}
.detail-list div {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--line);
  font-size: 12px;
}
dt {
  color: var(--muted);
}
dd {
  margin: 0;
  text-align: right;
  overflow-wrap: anywhere;
}
.identity-gap {
  padding: 14px;
  background: #102536;
  border-left: 2px solid #e1ab61;
  margin-top: 22px;
  font-size: 12px;
}
.identity-gap strong {
  color: #f2c27b;
  font-weight: 500;
}
.identity-gap p {
  color: var(--muted);
  line-height: 1.7;
  margin: 8px 0 0;
}
h3 {
  font-size: 12px;
  margin: 24px 0 10px;
  font-weight: 500;
}
.evidence-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.evidence-list li {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  padding: 11px 0;
  border-bottom: 1px solid var(--line);
  font-size: 11px;
}
.evidence-list em {
  color: var(--muted);
  font-style: normal;
  white-space: nowrap;
}
.eligibility {
  color: #f2c27b;
  font-size: 13px;
  margin: 22px 0 7px;
}
.boundary {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.8;
  margin: 0;
}
.history-links {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  border-top: 1px solid var(--line);
  padding-top: 18px;
  margin-top: 20px;
}
.history-links a {
  color: var(--accent);
  font-size: 11px;
  text-decoration: underline;
  text-underline-offset: 3px;
}
@media (min-width: 1800px) {
  .factor-workspace {
    padding: 36px;
  }
  .research-grid {
    grid-template-columns: minmax(0, 1fr) 360px;
  }
}
@media (max-width: 1100px) {
  .research-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .evidence-inspector {
    display: block;
  }
}
@media (max-width: 767px) {
  .factor-workspace {
    padding: 18px 12px;
  }
  .workspace-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 16px;
  }
  h1 {
    font-size: 24px;
  }
  .provenance-band {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 18px 12px;
  }
  .provenance-band div:nth-child(2) {
    border: 0;
  }
  .ledger-heading {
    padding: 16px;
  }
  .evidence-inspector {
    padding: 18px;
  }
  .research-grid {
    gap: 18px;
  }
}
</style>

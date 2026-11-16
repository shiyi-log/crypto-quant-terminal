<script setup lang="ts">
import type {
  EventSnapshot,
  HealthEvidence,
  HealthState,
  ServiceEvidence,
} from './operationsEvidence';

import {
  computed,
  onBeforeUnmount,
  onMounted,
  ref,
  shallowReactive,
} from 'vue';
import { RouterLink } from 'vue-router';

import {
  cancelReadState,
  createReadState,
  parseEvents,
  parseHealth,
  parseServices,
  refreshReadState,
} from './operationsEvidence';

const props = withDefaults(
  defineProps<{
    loadHealth: (signal: AbortSignal) => Promise<unknown>;
    loadServices: (signal: AbortSignal) => Promise<unknown>;
    loadEvents: (researchId: string, signal: AbortSignal) => Promise<unknown>;
    initialResearchId?: string;
  }>(),
  { initialResearchId: '' },
);

const health = shallowReactive(createReadState<HealthEvidence>());
const services = shallowReactive(createReadState<ServiceEvidence>());
const events = shallowReactive(createReadState<EventSnapshot>());
const researchInput = ref(props.initialResearchId);
const researchFilter = ref(props.initialResearchId);
const selectedEventId = ref('');
const selectedEvent = computed(
  () =>
    events.value?.events.find(
      (event) => event.eventId === selectedEventId.value,
    ) ??
    events.value?.events[0] ??
    null,
);
const refreshing = computed(() =>
  [health.status, services.status, events.status].includes('loading'),
);
const healthNames: Record<HealthState, string> = {
  ok: '正常',
  warning: '警告',
  failed: '故障',
  unknown: '未知',
};

function filterName(value: null | string) {
  return value || '全部研究';
}
function ageName(value: null | number) {
  if (value === null) return '年龄未知';
  if (value < 0) return '时钟超前 ' + Math.abs(value) + ' 秒';
  return value + ' 秒';
}
function refreshHealth() {
  return refreshReadState(
    health,
    'runtime-health',
    props.loadHealth,
    parseHealth,
  );
}
function refreshServices() {
  return refreshReadState(
    services,
    'services',
    props.loadServices,
    parseServices,
  );
}
function refreshEvents() {
  const filter = researchFilter.value;
  return refreshReadState(
    events,
    filter,
    (signal) => props.loadEvents(filter, signal),
    parseEvents,
  );
}
function refreshAll() {
  return Promise.allSettled([
    refreshHealth(),
    refreshServices(),
    refreshEvents(),
  ]);
}
function submitResearch() {
  researchFilter.value = researchInput.value.trim();
  void refreshEvents();
}

onMounted(() => void refreshAll());
onBeforeUnmount(() => {
  cancelReadState(health);
  cancelReadState(services);
  cancelReadState(events);
});
</script>

<template>
  <main class="operations-workspace" aria-labelledby="operations-title">
    <header class="workspace-heading">
      <div>
        <p class="eyebrow">OPERATIONS / EVIDENCE</p>
        <h1 id="operations-title">运维与证据</h1>
        <p class="description">
          核对进程事实，追溯研究事件。账户执行资格需独立证据。
        </p>
      </div>
      <button
        class="refresh-button"
        data-refresh-all
        type="button"
        :aria-busy="refreshing"
        @click="refreshAll"
      >
        {{ refreshing ? '正在读取…' : '刷新全部事实' }}
      </button>
    </header>

    <div class="scope-bar" role="note">
      <span class="scope-label">本地观测 · 只读</span>
      <span>服务正常仅表示观测状态；无法据此批准新增风险。</span>
    </div>

    <div class="workspace-grid">
      <div class="fact-column">
        <section
          class="fact-panel"
          aria-labelledby="services-title"
          :aria-busy="services.status === 'loading'"
        >
          <header class="panel-heading">
            <div>
              <p class="eyebrow">01 / PROCESS FACTS</p>
              <h2 id="services-title">服务事实</h2>
            </div>
            <button type="button" class="text-button" @click="refreshServices">
              刷新服务
            </button>
          </header>
          <div class="panel-meta">
            <span v-if="services.value"
              ><strong
                >{{ services.value.running }} /
                {{ services.value.total }}</strong
              >
              进程在运行</span
            >
            <span v-else>尚无可用服务事实</span>
            <time v-if="services.value"
              >采集时间 {{ services.value.generatedAt }}</time
            >
          </div>
          <p v-if="services.error" class="read-error" role="status">
            {{ services.error }}
          </p>
          <p
            v-else-if="services.status === 'loading'"
            class="read-note"
            role="status"
          >
            正在读取服务事实…
          </p>
          <div
            v-if="services.value"
            class="table-scroll"
            role="region"
            aria-label="服务事实表，可横向滚动"
            tabindex="0"
          >
            <table>
              <thead>
                <tr>
                  <th scope="col">服务 / 身份</th>
                  <th scope="col">进程状态</th>
                  <th scope="col">PID</th>
                  <th scope="col">端口</th>
                  <th scope="col">观测说明</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="service in services.value.services"
                  :key="service.key"
                >
                  <th scope="row">
                    <span>{{ service.name }}</span
                    ><small class="mono">{{ service.key }}</small>
                  </th>
                  <td>
                    <span
                      class="state-tag"
                      :class="service.running ? 'state-ok' : 'state-unknown'"
                      >{{ service.running ? '运行中' : '未运行' }}</span
                    >
                  </td>
                  <td class="mono">{{ service.pid ?? '未知' }}</td>
                  <td class="mono">{{ service.port ?? '未知' }}</td>
                  <td>
                    <span>{{ service.healthReason || '未提供说明' }}</span
                    ><small v-if="service.desiredState"
                      >期望 {{ service.desiredState }}</small
                    >
                  </td>
                </tr>
                <tr v-if="services.value.services.length === 0">
                  <td colspan="5" class="empty-state">
                    来源明确返回空服务清单
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section
          class="fact-panel"
          aria-labelledby="health-title"
          :aria-busy="health.status === 'loading'"
        >
          <header class="panel-heading">
            <div>
              <p class="eyebrow">02 / RUNTIME OBSERVATIONS</p>
              <h2 id="health-title">运行健康</h2>
            </div>
            <button type="button" class="text-button" @click="refreshHealth">
              刷新健康
            </button>
          </header>
          <div class="panel-meta">
            <span
              v-if="health.value"
              class="state-tag"
              :class="health.value.ok ? 'state-ok' : 'state-warning'"
              >{{
                health.value.ok ? '已返回组件均正常' : '存在非正常或未知组件'
              }}</span
            >
            <span v-else>健康状态未知</span>
            <time v-if="health.value"
              >来源时间 {{ health.value.generated_at }}</time
            >
          </div>
          <p v-if="health.error" class="read-error" role="status">
            {{ health.error }}
          </p>
          <p
            v-else-if="health.status === 'loading'"
            class="read-note"
            role="status"
          >
            正在读取健康事实…
          </p>
          <ul v-if="health.value" class="health-list">
            <li
              v-for="component in health.value.components"
              :key="component.key"
            >
              <div class="health-line">
                <strong>{{ component.label }}</strong
                ><span class="state-tag" :class="'state-' + component.state">{{
                  healthNames[component.state]
                }}</span
                ><span
                  class="age"
                  :class="{
                    'clock-skew':
                      component.age_seconds !== null &&
                      component.age_seconds < 0,
                  }"
                  >{{ ageName(component.age_seconds) }}</span
                >
              </div>
              <div class="observation-times">
                <span>最后成功 {{ component.last_success_at ?? '未知' }}</span
                ><span>更新时间 {{ component.updated_at ?? '未知' }}</span>
              </div>
              <p v-if="component.error_codes.length" class="error-codes mono">
                {{ component.error_codes.join(' · ') }}
              </p>
              <dl v-if="Object.keys(component.metrics).length" class="metrics">
                <div v-for="(value, key) in component.metrics" :key="key">
                  <dt>{{ key }}</dt>
                  <dd class="mono">{{ value === null ? '未知' : value }}</dd>
                </div>
              </dl>
            </li>
            <li v-if="health.value.components.length === 0" class="empty-state">
              来源明确返回空组件清单，健康仍未知
            </li>
          </ul>
        </section>

        <section
          class="fact-panel"
          aria-labelledby="events-title"
          :aria-busy="events.status === 'loading'"
        >
          <header class="panel-heading">
            <div>
              <p class="eyebrow">03 / RESEARCH EVIDENCE</p>
              <h2 id="events-title">研究事件时间线</h2>
            </div>
            <span class="source-label">研究账本 · 最近 50 条</span>
          </header>
          <form class="research-filter" @submit.prevent="submitResearch">
            <label for="operations-research-id">研究 ID</label>
            <input
              id="operations-research-id"
              v-model="researchInput"
              name="research_id"
              type="text"
              placeholder="全部研究"
              autocomplete="off"
              maxlength="200"
            />
            <button type="submit">读取筛选</button>
          </form>
          <p class="filter-context">
            当前请求筛选：{{ filterName(events.requestKey)
            }}<span
              v-if="events.value && events.valueKey !== events.requestKey"
              class="retained-context"
              >保留事实筛选：{{ filterName(events.valueKey) }}</span
            >
          </p>
          <p v-if="events.error" class="read-error" role="status">
            {{ events.error }}
          </p>
          <p
            v-else-if="events.status === 'loading'"
            class="read-note"
            role="status"
          >
            正在读取研究事件…
          </p>
          <div v-if="events.value" class="event-summary">
            <span
              >本次返回计数
              <strong class="mono">{{ events.value.total }}</strong> 条</span
            ><span>已返回 {{ events.value.events.length }} 条</span>
          </div>
          <ol
            v-if="events.value?.events.length"
            class="event-timeline"
            aria-label="研究事件"
          >
            <li v-for="event in events.value.events" :key="event.eventId">
              <button
                type="button"
                class="event-button"
                :class="{ selected: selectedEvent?.eventId === event.eventId }"
                :aria-pressed="selectedEvent?.eventId === event.eventId"
                @click="selectedEventId = event.eventId"
              >
                <time>{{ event.timestamp }}</time
                ><strong>{{ event.eventType }}</strong
                ><span class="event-id mono">{{ event.eventId }}</span
                ><span class="event-research"
                  >研究 {{ event.researchId ?? '未关联' }}</span
                >
              </button>
            </li>
          </ol>
          <p
            v-else-if="events.value && events.status === 'ready'"
            class="empty-state"
          >
            该筛选没有研究事件
          </p>
          <p
            v-else-if="!events.value && events.status !== 'loading'"
            class="empty-state"
          >
            尚无可用研究事件事实
          </p>
        </section>
      </div>

      <aside class="inspector-column" aria-label="证据核对侧栏">
        <section
          class="inspector-panel"
          data-account-gates
          aria-labelledby="account-gates-title"
        >
          <p class="eyebrow">ACCOUNT AUTHORITY</p>
          <h2 id="account-gates-title">账户执行资格</h2>
          <p class="authority-title">尚未取得账户执行资格证据</p>
          <p class="muted">
            当前来源没有账户与阶段绑定的权威门禁，服务健康不会改变此状态。
          </p>
          <dl class="authority-list">
            <div>
              <dt>唯一写者</dt>
              <dd>未知</dd>
            </div>
            <div>
              <dt>风险预算与预留</dt>
              <dd>未知</dd>
            </div>
            <div>
              <dt>备份与恢复演练</dt>
              <dd>未知</dd>
            </div>
            <div>
              <dt>阶段验收与人工批准</dt>
              <dd>未知</dd>
            </div>
          </dl>
        </section>

        <section
          class="inspector-panel"
          aria-labelledby="event-inspector-title"
        >
          <p class="eyebrow">EVENT INSPECTOR</p>
          <h2 id="event-inspector-title">所选事件</h2>
          <dl v-if="selectedEvent" class="event-facts">
            <div>
              <dt>事件 ID</dt>
              <dd class="mono">{{ selectedEvent.eventId }}</dd>
            </div>
            <div>
              <dt>来源时间</dt>
              <dd class="mono">{{ selectedEvent.timestamp }}</dd>
            </div>
            <div>
              <dt>研究 ID</dt>
              <dd class="mono">{{ selectedEvent.researchId ?? '未关联' }}</dd>
            </div>
            <div>
              <dt>内容哈希</dt>
              <dd class="mono hash">
                {{ selectedEvent.hash ?? '来源未提供可核验哈希' }}
              </dd>
            </div>
            <div>
              <dt>事件说明</dt>
              <dd>{{ selectedEvent.reason ?? '未提供' }}</dd>
            </div>
          </dl>
          <p v-else class="muted">选择时间线中的事件查看已提供的证据字段。</p>
          <p class="inspector-footnote">
            事件记录和内容哈希用于追溯；当前页面未核验批准者、签名或账户资格。
          </p>
        </section>

        <nav class="inspector-panel history-links" aria-label="历史运维入口">
          <p class="eyebrow">HISTORICAL REFERENCES</p>
          <h2>历史入口</h2>
          <RouterLink to="/quant/logs"
            >历史运行日志 <span aria-hidden="true">↗</span></RouterLink
          >
          <RouterLink to="/quant/ops"
            >历史运维监控 <span aria-hidden="true">↗</span></RouterLink
          >
          <RouterLink to="/quant/research-ledger"
            >研究账本原始视图 <span aria-hidden="true">↗</span></RouterLink
          >
        </nav>
      </aside>
    </div>
  </main>
</template>

<style scoped>
.operations-workspace {
  --ops-bg: #0c1b2a;
  --ops-panel: #132b3e;
  --ops-border: #29485c;
  --ops-text: #e5edf4;
  --ops-muted: #a2b8c9;
  --ops-accent: #69b4e4;
  min-width: 0;
  max-width: 100%;
  min-height: 100%;
  padding: 24px;
  background: var(--ops-bg);
  color: var(--ops-text);
  font-family:
    'IBM Plex Sans', 'Source Han Sans SC', 'Noto Sans SC', system-ui, sans-serif;
  font-size: 13px;
  line-height: 1.55;
  color-scheme: dark;
}
.operations-workspace * {
  box-sizing: border-box;
}
.workspace-heading,
.panel-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.workspace-heading {
  margin-bottom: 20px;
}
.eyebrow {
  margin: 0 0 5px;
  color: var(--ops-accent);
  font-size: 10px;
  letter-spacing: 0.14em;
  font-weight: 600;
}
h1,
h2,
p {
  margin-top: 0;
}
h1 {
  margin-bottom: 5px;
  font-size: 26px;
  font-weight: 600;
  letter-spacing: 0.025em;
}
h2 {
  margin-bottom: 0;
  font-size: 16px;
  font-weight: 600;
}
.description {
  margin-bottom: 0;
  color: var(--ops-muted);
}
button,
input,
a {
  font: inherit;
}
button {
  border: 1px solid #45677d;
  border-radius: 3px;
  background: #1c3a50;
  padding: 8px 12px;
  color: var(--ops-text);
  cursor: pointer;
}
button:hover {
  border-color: var(--ops-accent);
  background: #244b66;
}
button:focus-visible,
a:focus-visible,
input:focus-visible,
[tabindex]:focus-visible {
  outline: 2px solid var(--ops-accent);
  outline-offset: 3px;
}
.refresh-button {
  flex-shrink: 0;
  min-width: 112px;
}
.scope-bar {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  margin-bottom: 20px;
  padding: 10px 14px;
  border: 1px solid var(--ops-border);
  border-left: 3px solid var(--ops-accent);
  color: var(--ops-muted);
  background: #102437;
}
.scope-label {
  color: var(--ops-text);
  font-weight: 600;
}
.workspace-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 296px;
  align-items: start;
  gap: 20px;
}
.fact-column,
.inspector-column {
  display: grid;
  gap: 20px;
  min-width: 0;
}
.fact-panel,
.inspector-panel {
  min-width: 0;
  border: 1px solid var(--ops-border);
  border-radius: 4px;
  background: var(--ops-panel);
}
.panel-heading {
  padding: 16px 18px;
  border-bottom: 1px solid var(--ops-border);
}
.text-button {
  border: 0;
  background: transparent;
  padding: 6px 0;
  color: var(--ops-accent);
  white-space: nowrap;
}
.panel-meta,
.event-summary {
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  padding: 11px 18px;
  color: var(--ops-muted);
  font-size: 12px;
}
.panel-meta strong {
  color: var(--ops-text);
  font-size: 16px;
}
.panel-meta time {
  overflow-wrap: anywhere;
}
.read-error,
.read-note {
  margin: 0;
  padding: 9px 18px;
  font-size: 12px;
}
.read-error {
  color: #f1bf86;
  background: #3c3028;
  border-top: 1px solid #725337;
  border-bottom: 1px solid #725337;
}
.read-note {
  color: var(--ops-accent);
}
.table-scroll {
  overflow-x: auto;
  max-width: 100%;
}
table {
  width: 100%;
  min-width: 640px;
  border-collapse: collapse;
  text-align: left;
}
th,
td {
  padding: 11px 18px;
  border-top: 1px solid var(--ops-border);
  vertical-align: top;
}
thead th {
  color: var(--ops-muted);
  font-size: 11px;
  font-weight: 500;
  white-space: nowrap;
}
tbody th {
  font-weight: 500;
}
small {
  display: block;
  margin-top: 2px;
  color: var(--ops-muted);
  font-size: 11px;
}
.mono {
  font-family: 'IBM Plex Mono', ui-monospace, SFMono-Regular, monospace;
  font-variant-numeric: tabular-nums;
}
.state-tag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 1px solid #526474;
  border-radius: 2px;
  padding: 1px 6px;
  font-size: 11px;
  white-space: nowrap;
}
.state-ok {
  color: #94d2bd;
  border-color: #3e7567;
  background: #193d3a;
}
.state-warning {
  color: #f1bf86;
  border-color: #8a6945;
  background: #413628;
}
.state-failed {
  color: #f0a8a4;
  border-color: #935452;
  background: #472f35;
}
.state-unknown {
  color: #bfccda;
  background: #273d50;
}
.health-list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.health-list > li {
  padding: 12px 18px;
  border-top: 1px solid var(--ops-border);
}
.health-line {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.health-line strong {
  font-weight: 500;
}
.age {
  margin-left: auto;
  color: var(--ops-muted);
  font-size: 12px;
}
.clock-skew {
  color: #f0a8a4;
}
.observation-times {
  display: flex;
  gap: 6px 20px;
  flex-wrap: wrap;
  margin-top: 6px;
  color: var(--ops-muted);
  font-size: 11px;
  overflow-wrap: anywhere;
}
.error-codes {
  margin: 7px 0 0;
  color: #f1bf86;
  font-size: 11px;
  overflow-wrap: anywhere;
}
.metrics {
  display: flex;
  gap: 4px 16px;
  flex-wrap: wrap;
  margin: 8px 0 0;
  font-size: 11px;
}
.metrics > div {
  display: flex;
  gap: 8px;
}
.metrics dt {
  color: var(--ops-muted);
  overflow-wrap: anywhere;
}
.metrics dd {
  margin: 0;
}
.source-label {
  color: var(--ops-muted);
  font-size: 11px;
}
.research-filter {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 18px 8px;
  flex-wrap: wrap;
}
.research-filter label {
  font-size: 12px;
  color: var(--ops-muted);
}
input {
  width: 220px;
  max-width: 100%;
  min-width: 0;
  border: 1px solid #45677d;
  border-radius: 3px;
  padding: 7px 10px;
  background: #0c1b2a;
  color: var(--ops-text);
}
input::placeholder {
  color: #91a6b8;
}
.filter-context {
  display: flex;
  gap: 8px 18px;
  flex-wrap: wrap;
  padding: 0 18px;
  margin: 0 0 10px;
  font-size: 11px;
  color: var(--ops-muted);
  overflow-wrap: anywhere;
}
.retained-context {
  color: #f1bf86;
}
.event-summary {
  border-top: 1px solid var(--ops-border);
}
.event-timeline {
  margin: 0;
  padding: 0;
  list-style: none;
}
.event-timeline li {
  position: relative;
  margin: 0;
  padding: 0 12px 0 27px;
}
.event-timeline li::before {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 17px;
  width: 1px;
  background: var(--ops-border);
  content: '';
}
.event-timeline li::after {
  position: absolute;
  top: 19px;
  left: 14px;
  width: 7px;
  height: 7px;
  border: 1px solid var(--ops-accent);
  border-radius: 50%;
  background: var(--ops-panel);
  content: '';
}
.event-button {
  display: grid;
  grid-template-columns: 166px minmax(0, 1fr);
  gap: 3px 14px;
  width: 100%;
  margin: 0 0 8px;
  padding: 10px;
  border: 1px solid transparent;
  background: transparent;
  text-align: left;
}
.event-button:hover,
.event-button.selected {
  background: #1a384e;
  border-color: #40627b;
}
.event-button time {
  grid-row: span 3;
  font-size: 11px;
  color: var(--ops-muted);
  overflow-wrap: anywhere;
}
.event-button strong {
  font-size: 12px;
  font-weight: 500;
  overflow-wrap: anywhere;
}
.event-id,
.event-research {
  color: var(--ops-muted);
  font-size: 11px;
  overflow-wrap: anywhere;
}
.empty-state {
  margin: 0;
  padding: 24px 18px;
  text-align: center;
  color: var(--ops-muted);
  font-size: 12px;
}
.inspector-panel {
  padding: 16px;
}
.authority-title {
  margin: 18px 0 8px;
  color: #f1bf86;
  font-size: 15px;
  line-height: 1.5;
  font-weight: 500;
}
.muted {
  color: var(--ops-muted);
  font-size: 12px;
}
.authority-list {
  margin: 18px 0 0;
}
.authority-list > div {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  padding: 11px 0;
  border-top: 1px solid var(--ops-border);
}
.authority-list dt {
  color: var(--ops-muted);
  font-size: 12px;
}
.authority-list dd {
  margin: 0;
  color: #bfccda;
  font-size: 12px;
}
.event-facts {
  margin: 16px 0 0;
}
.event-facts > div {
  margin-bottom: 14px;
}
.event-facts dt {
  margin-bottom: 4px;
  color: var(--ops-muted);
  font-size: 11px;
}
.event-facts dd {
  margin: 0;
  overflow-wrap: anywhere;
  font-size: 12px;
}
.hash {
  color: var(--ops-accent);
  font-size: 11px !important;
}
.inspector-footnote {
  padding-top: 12px;
  margin: 0;
  border-top: 1px solid var(--ops-border);
  color: var(--ops-muted);
  font-size: 11px;
}
.history-links {
  display: flex;
  flex-direction: column;
}
.history-links a {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--ops-border);
  color: var(--ops-accent);
  text-decoration: none;
}
.history-links a:hover {
  text-decoration: underline;
}
@media (min-width: 1700px) {
  .workspace-grid {
    grid-template-columns: minmax(0, 1fr) 340px;
  }
  .operations-workspace {
    padding: 28px 32px;
  }
  .inspector-panel {
    padding: 20px;
  }
}
@media (max-width: 1050px) {
  .workspace-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .inspector-column {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .history-links {
    grid-column: 1/-1;
  }
  .event-button {
    grid-template-columns: 156px minmax(0, 1fr);
  }
}
@media (max-width: 600px) {
  .operations-workspace {
    padding: 14px 12px;
  }
  .workspace-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 12px;
  }
  .refresh-button {
    width: 100%;
  }
  h1 {
    font-size: 23px;
  }
  .scope-bar {
    gap: 5px;
    padding: 10px 12px;
  }
  .workspace-grid,
  .fact-column,
  .inspector-column {
    gap: 14px;
  }
  .inspector-column {
    grid-template-columns: minmax(0, 1fr);
  }
  .panel-heading {
    padding: 13px 12px;
    align-items: flex-start;
  }
  .panel-meta,
  .event-summary {
    padding: 10px 12px;
  }
  .panel-meta {
    flex-direction: column;
  }
  .panel-heading .source-label {
    max-width: 110px;
    text-align: right;
  }
  .health-list > li {
    padding: 12px;
  }
  .age {
    margin-left: 0;
  }
  .research-filter {
    padding: 12px;
  }
  .research-filter label {
    width: 100%;
  }
  .research-filter input {
    flex: 1;
    width: 120px;
  }
  .filter-context {
    padding: 0 12px;
  }
  .event-button {
    grid-template-columns: minmax(0, 1fr);
    gap: 3px;
  }
  .event-button time {
    grid-row: auto;
  }
  .read-error,
  .read-note {
    padding: 9px 12px;
  }
  .history-links {
    grid-column: auto;
  }
}
</style>

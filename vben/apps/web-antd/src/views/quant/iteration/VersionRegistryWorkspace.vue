<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue';
import { getModelVersions, promoteModelVersion } from '#/api/freqtrade';
import { createVersionRegistry, type RegistryVersion } from './versionRegistry';

const registry = createVersionRegistry({
  load: getModelVersions,
  promote: promoteModelVersion,
});
const state = reactive(registry.state);
const filter = ref('all');
const query = ref('');
const selected = computed(
  () => state.snapshot?.rows.find((row) => row.id === state.selectedId) ?? null,
);
const visibleRows = computed(() =>
  (state.snapshot?.rows ?? []).filter(
    (row) =>
      (filter.value === 'all' || row.layer === filter.value) &&
      (!query.value.trim() ||
        [row.id, row.label, row.layer, row.status].some(
          (item) =>
            typeof item === 'string' &&
            item.toLowerCase().includes(query.value.trim().toLowerCase()),
        )),
  ),
);
const layers = computed(() => Object.keys(state.snapshot?.layers ?? {}));
const target = computed(() => state.confirmTarget);

function layerName(layer: string) {
  return (
    (
      { live_strategy: '实盘策略', ml_model: '研究模型' } as Record<
        string,
        string
      >
    )[layer] ?? layer
  );
}
function statusName(status: string) {
  return (
    (
      {
        champion: '优选登记',
        challenger: '候选版本',
        archived: '历史归档',
      } as Record<string, string>
    )[status] ?? '未知状态'
  );
}
function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '未知';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}
function metricsText(version: RegistryVersion) {
  const metrics = version.metrics;
  const entries = Object.entries(metrics);
  if (version.metrics_validity === 'invalidated')
    return '研究口径已作废，历史指标仅留档';
  if (!entries.length) return '未记录';
  return entries.map(([key, value]) => key + ': ' + display(value)).join(' · ');
}
function gateText(version: RegistryVersion) {
  if (!version.gate) return '未记录';
  if (
    version.gate.status === 'invalidated' ||
    version.metrics_validity === 'invalidated'
  )
    return '口径已作废';
  return version.gate.passed
    ? '已通过' + (version.gate.criteria ? ' · ' + version.gate.criteria : '')
    : '未通过' +
        (version.gate.reasons.length
          ? ' · ' + version.gate.reasons.join('；')
          : '');
}
function isInvalidated(version: RegistryVersion) {
  return (
    version.metrics_validity === 'invalidated' ||
    version.gate?.status === 'invalidated'
  );
}
function openConfirmation(version: RegistryVersion) {
  registry.openConfirmation(version.id);
}
onMounted(() => {
  void registry.refresh();
});
onUnmounted(() => registry.dispose());
</script>

<template>
  <main class="registry-workspace" aria-labelledby="registry-title">
    <header class="registry-heading">
      <div>
        <p class="source-label">研究 / 版本登记</p>
        <h1 id="registry-title">版本登记</h1>
        <p class="intro">
          优选登记表示注册表中的人工选择，不代表实际部署或交易授权。
        </p>
      </div>
      <button
        data-testid="refresh-version-registry"
        type="button"
        :disabled="state.loading || state.submitting"
        @click="registry.refresh"
      >
        {{ state.loading ? '正在读取' : '刷新注册表' }}
      </button>
    </header>

    <div class="registry-provenance" aria-label="版本注册表范围">
      <div><span>数据来源</span><strong>版本注册表</strong></div>
      <div>
        <span>最近更新</span
        ><strong>{{ display(state.snapshot?.updated) }}</strong>
      </div>
      <div>
        <span>自动迭代</span
        ><strong>{{
          state.snapshot?.running
            ? '运行中 · PID ' + display(state.snapshot.running.pid)
            : state.snapshot
              ? '未报告运行中'
              : '未知'
        }}</strong>
      </div>
      <div>
        <span>历史 Trial</span
        ><strong
          >{{ display(state.snapshot?.trials.total) }} 条 /
          {{ display(state.snapshot?.trials.runs) }} 轮</strong
        >
      </div>
    </div>

    <p v-if="state.error" class="registry-warning" role="alert">
      {{ state.error }}
    </p>
    <p v-if="state.commandError" class="registry-error" role="alert">
      {{ state.commandError }}
    </p>
    <p v-if="state.outcome" class="registry-outcome" role="status">
      {{ state.outcome }}
    </p>

    <section
      class="registry-table-section"
      aria-labelledby="registry-table-title"
    >
      <div class="table-heading">
        <div>
          <h2 id="registry-table-title">版本清单</h2>
          <p>指标和判据显示注册表原值，缺少值保留为未知。</p>
        </div>
        <div class="filters">
          <label
            >分层<select v-model="filter" aria-label="版本分层">
              <option value="all">全部分层</option>
              <option v-for="layer in layers" :key="layer" :value="layer">
                {{ layerName(layer) }}
              </option>
            </select></label
          >
          <label
            >查找<input
              v-model="query"
              type="search"
              placeholder="版本编号或标签"
          /></label>
        </div>
      </div>
      <p v-if="!state.snapshot" class="empty-state">
        {{ state.loading ? '正在读取注册表…' : '尚无成功读取的注册表快照。' }}
      </p>
      <p v-else-if="visibleRows.length === 0" class="empty-state">
        {{
          state.snapshot.rows.length
            ? '当前筛选没有匹配版本。'
            : '注册表当前没有版本记录。'
        }}
      </p>
      <div
        v-else
        class="table-viewport"
        tabindex="0"
        aria-label="版本列表，可横向滚动"
      >
        <table>
          <thead>
            <tr>
              <th scope="col">版本</th>
              <th scope="col">分层 / 状态</th>
              <th scope="col">冻结判据</th>
              <th scope="col">历史指标</th>
              <th scope="col">部署记录</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="version in visibleRows"
              :key="version.id"
              :class="{ selected: version.id === state.selectedId }"
            >
              <th scope="row">
                <button
                  class="version-link"
                  type="button"
                  :aria-pressed="version.id === state.selectedId"
                  @click="registry.select(version.id)"
                >
                  {{ version.id }}</button
                ><small
                  >{{ display(version.label) }} · 第
                  {{ display(version.round) }} 轮</small
                >
              </th>
              <td>
                {{ layerName(version.layer)
                }}<small>{{ statusName(version.status) }}</small>
              </td>
              <td>{{ gateText(version) }}</td>
              <td>{{ metricsText(version) }}</td>
              <td>{{ display(version.deployed) }}</td>
              <td>
                <button
                  v-if="version.status === 'challenger'"
                  type="button"
                  :disabled="isInvalidated(version) || state.submitting"
                  @click="openConfirmation(version)"
                >
                  设为优选登记</button
                ><span v-else>只读</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section
      v-if="selected"
      class="registry-inspector"
      aria-labelledby="selected-version-title"
    >
      <h2 id="selected-version-title">所选版本</h2>
      <p class="version-id">{{ selected.id }}</p>
      <dl>
        <div>
          <dt>分层</dt>
          <dd>{{ layerName(selected.layer) }}</dd>
        </div>
        <div>
          <dt>当前状态</dt>
          <dd>{{ statusName(selected.status) }}</dd>
        </div>
        <div>
          <dt>判据状态</dt>
          <dd>{{ gateText(selected) }}</dd>
        </div>
        <div>
          <dt>判据理由</dt>
          <dd>{{ selected.gate?.reasons.join('；') || '未知' }}</dd>
        </div>
        <div>
          <dt>配置规格</dt>
          <dd>{{ display(selected.spec) }}</dd>
        </div>
        <div>
          <dt>部署信息</dt>
          <dd>{{ display(selected.deployed) }}</dd>
        </div>
        <div>
          <dt>产物引用</dt>
          <dd>{{ display(selected.artifacts) }}</dd>
        </div>
        <div>
          <dt>备注</dt>
          <dd>{{ display(selected.notes) }}</dd>
        </div>
      </dl>
    </section>

    <section
      v-if="target"
      class="confirmation"
      aria-labelledby="confirmation-title"
    >
      <div class="confirmation-heading">
        <div>
          <h2 id="confirmation-title">人工确认版本登记</h2>
          <p>目标已冻结；切换列表选择不会改变本次确认目标。</p>
        </div>
        <button
          type="button"
          :disabled="state.submitting"
          @click="state.confirmTarget = null"
        >
          取消
        </button>
      </div>
      <p class="confirm-id">
        {{ target.id }} · {{ layerName(target.layer)
        }}{{ target.label ? ' · ' + target.label : '' }}
      </p>
      <p
        :class="target.gate?.passed === true ? 'gate-pass' : 'registry-warning'"
      >
        {{
          target.gate?.passed === true
            ? '已通过冻结判据' +
              (target.gate.criteria ? '：' + target.gate.criteria : '')
            : (target.gate?.status === 'invalidated'
                ? '口径已作废'
                : '未通过冻结判据') +
              '：' +
              (target.gate?.reasons.join('；') || '原因未记录')
        }}
      </p>
      <label class="note-field"
        >登记理由（至少 4 个字，写入版本历史）<textarea
          v-model="state.note"
          rows="3"
          maxlength="1000"
          placeholder="说明人工复核依据"
        ></textarea>
      </label>
      <label v-if="target.gate?.passed !== true" class="confirm-unpassed"
        ><input
          v-model="state.confirmUnpassed"
          type="checkbox"
        />我已复核并明确接受该版本未通过冻结判据</label
      >
      <button
        class="submit-button"
        data-testid="confirm-version-promotion"
        type="button"
        :disabled="state.submitting"
        @click="registry.submit"
      >
        {{ state.submitting ? '正在提交' : '确认设为优选登记' }}
      </button>
      <p class="boundary-note">
        此操作仅更新版本注册表，不修改部署配置，也不创建或改变订单。
      </p>
    </section>
  </main>
</template>

<style scoped>
.registry-workspace {
  min-width: 0;
  color: #e5edf4;
}
.registry-heading,
.table-heading,
.confirmation-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}
.registry-heading {
  margin-bottom: 18px;
}
.source-label {
  margin: 0 0 5px;
  color: #8bc8ed;
  font-size: 12px;
}
.registry-heading h1 {
  margin: 0;
  font-size: 24px;
  font-weight: 600;
}
.intro,
.table-heading p,
.confirmation-heading p {
  margin: 6px 0 0;
  color: #a2b8c9;
  font-size: 13px;
}
.registry-workspace button,
.registry-workspace input,
.registry-workspace select,
.registry-workspace textarea {
  font: inherit;
}
.registry-workspace button {
  border: 1px solid #426278;
  border-radius: 3px;
  background: #17364b;
  color: #e5edf4;
  padding: 8px 11px;
  cursor: pointer;
  font-size: 13px;
}
.registry-workspace button:disabled {
  opacity: 0.5;
  cursor: default;
}
.registry-workspace button:focus-visible,
.registry-workspace input:focus-visible,
.registry-workspace select:focus-visible,
.registry-workspace textarea:focus-visible,
.table-viewport:focus-visible {
  outline: 2px solid #69b4e4;
  outline-offset: 3px;
}
.registry-provenance {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  border: 1px solid #29485c;
  background: #132b3e;
  margin-bottom: 18px;
}
.registry-provenance div {
  display: grid;
  gap: 5px;
  padding: 12px;
  border-right: 1px solid #29485c;
  min-width: 0;
}
.registry-provenance div:last-child {
  border-right: 0;
}
.registry-provenance span,
.registry-inspector dt {
  color: #a2b8c9;
  font-size: 12px;
}
.registry-provenance strong {
  font-size: 13px;
  font-weight: 500;
  overflow-wrap: anywhere;
}
.registry-warning,
.registry-error,
.registry-outcome {
  padding: 10px 12px;
  font-size: 13px;
  overflow-wrap: anywhere;
}
.registry-warning {
  color: #f0c17b;
  border-left: 2px solid #b38445;
  background: #273340;
}
.registry-error {
  color: #ffc1ba;
  border-left: 2px solid #d87569;
  background: #352c31;
}
.registry-outcome,
.gate-pass {
  color: #9cdbb2;
  border-left: 2px solid #58a87a;
  background: #1d352e;
  padding: 10px 12px;
}
.registry-table-section {
  border-top: 1px solid #29485c;
}
.table-heading {
  align-items: end;
  padding: 16px 0 12px;
}
.table-heading h2,
.registry-inspector h2,
.confirmation h2 {
  margin: 0;
  font-size: 17px;
  font-weight: 600;
}
.filters {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.filters label,
.note-field {
  display: grid;
  gap: 5px;
  color: #a2b8c9;
  font-size: 12px;
}
.filters input,
.filters select,
.note-field textarea {
  min-width: 170px;
  max-width: 100%;
  border: 1px solid #426278;
  border-radius: 3px;
  background: #0c1b2a;
  color: #e5edf4;
  padding: 8px;
}
.table-viewport {
  max-width: 100%;
  overflow: auto;
  border: 1px solid #29485c;
  background: #132b3e;
}
table {
  width: 100%;
  min-width: 1020px;
  border-collapse: collapse;
  font-size: 13px;
}
th,
td {
  text-align: left;
  vertical-align: top;
  padding: 11px;
  border-bottom: 1px solid #29485c;
  max-width: 320px;
  overflow-wrap: anywhere;
}
thead th {
  position: sticky;
  top: 0;
  background: #17364b;
  color: #a2b8c9;
  font-weight: 500;
}
tbody th {
  font-weight: 500;
}
.registry-workspace small {
  display: block;
  color: #a2b8c9;
  margin-top: 5px;
}
.version-link {
  border: 0 !important;
  background: transparent !important;
  padding: 0 !important;
  color: #8bc8ed !important;
  text-align: left !important;
  font-family: monospace;
}
.selected {
  background: #193b50;
}
.empty-state {
  padding: 24px;
  color: #a2b8c9;
  font-size: 13px;
}
.registry-inspector,
.confirmation {
  margin-top: 18px;
  padding: 16px 0;
  border-top: 1px solid #29485c;
}
.version-id,
.confirm-id {
  font-family: monospace;
  color: #8bc8ed;
  overflow-wrap: anywhere;
}
.registry-inspector dl {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px 24px;
}
.registry-inspector dl div {
  display: grid;
  grid-template-columns: 110px minmax(0, 1fr);
  gap: 10px;
}
.registry-inspector dd {
  margin: 0;
  overflow-wrap: anywhere;
  font-size: 13px;
}
.confirmation {
  max-width: 760px;
}
.confirmation-heading {
  align-items: center;
}
.confirmation .registry-warning,
.confirmation .gate-pass {
  margin: 12px 0;
}
.note-field textarea {
  width: 100%;
  resize: vertical;
}
.confirm-unpassed {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  margin: 12px 0;
  color: #f0c17b;
  font-size: 13px;
}
.confirm-unpassed input {
  margin-top: 3px;
  accent-color: #69b4e4;
}
.submit-button {
  margin-top: 14px;
}
.boundary-note {
  color: #a2b8c9;
  font-size: 12px;
  margin: 10px 0 0;
}
@media (max-width: 760px) {
  .registry-provenance {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .registry-provenance div:nth-child(2) {
    border-right: 0;
  }
  .registry-provenance div:nth-child(-n + 2) {
    border-bottom: 1px solid #29485c;
  }
  .table-heading,
  .registry-heading {
    align-items: stretch;
    flex-direction: column;
  }
  .filters {
    width: 100%;
  }
  .filters label {
    flex: 1;
    min-width: 130px;
  }
  .filters input,
  .filters select {
    width: 100%;
    min-width: 0;
  }
  .registry-inspector dl {
    grid-template-columns: minmax(0, 1fr);
  }
  .registry-inspector dl div {
    grid-template-columns: 95px minmax(0, 1fr);
  }
}
</style>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import type { LocationQueryRaw } from 'vue-router';
import {
  createResearchReader,
  createSurgeReader,
  finite,
  maturityWindows,
  pairStats,
  parseSurgeDetail,
  parseSurgeStats,
  timeMs,
  valueOf,
} from './surgeResearch';
import type { Fact, SurgeQuery } from './surgeResearch';
const props = defineProps<{
  loadList: (query: SurgeQuery, signal: AbortSignal) => Promise<unknown>;
  loadStats: (signal: AbortSignal) => Promise<unknown>;
  loadDetail: (id: string, signal: AbortSignal) => Promise<unknown>;
}>();
const route = useRoute(),
  router = useRouter(),
  pairs = ['BTC/USDT:USDT', 'ETH/USDT:USDT'];
const invalidScope = computed(() => {
  const p = route.query.pair,
    s = route.query.status,
    id = route.query.event_id;
  return (
    (p !== undefined &&
      (typeof p !== 'string' || !['all', ...pairs].includes(p))) ||
    (s !== undefined &&
      (typeof s !== 'string' ||
        !['all', 'unknown', 'matured', 'invalid'].includes(s))) ||
    (id !== undefined && (typeof id !== 'string' || !id || id.length > 512))
  );
});
// URL 是筛选范围的唯一来源，浏览器前进/后退与界面选择不会漂移。
function setFilter(key: 'pair' | 'status', value: string) {
  const next = { ...route.query };
  delete next.event_id;
  if (value === 'all') delete next[key];
  else next[key] = value;
  void router.replace({ query: next });
}
const pair = computed({
    get: () =>
      typeof route.query.pair === 'string' && pairs.includes(route.query.pair)
        ? route.query.pair
        : 'all',
    set: (value: string) => setFilter('pair', value),
  }),
  status = computed({
    get: () =>
      typeof route.query.status === 'string' &&
      ['unknown', 'matured', 'invalid'].includes(route.query.status)
        ? route.query.status
        : 'all',
    set: (value: string) => setFilter('status', value),
  }),
  page = ref(1),
  pageSize = ref(10);
const list = createSurgeReader((query, signal) => {
    if (invalidScope.value) return Promise.reject(new Error('invalid scope'));
    return props.loadList(query, signal);
  }),
  statistics = createResearchReader(
    (_query: Record<string, never>, signal) => props.loadStats(signal),
    parseSurgeStats,
  ),
  details = createResearchReader(
    (query: { eventId: string; pair?: string; status?: string }, signal) =>
      query.eventId && !invalidScope.value
        ? props.loadDetail(query.eventId, signal)
        : Promise.reject(new Error('no selection')),
    (raw, query) => {
      const detail = parseSurgeDetail(raw, query.eventId);
      if (
        (query.pair && detail.observation.pair !== query.pair) ||
        (query.status && detail.observation.status !== query.status)
      )
        throw new Error('detail scope mismatch');
      return detail;
    },
  );
const state = list.state,
  statsState = statistics.state,
  detailState = details.state,
  eventId = computed(() =>
    typeof route.query.event_id === 'string' ? route.query.event_id : '',
  );
const selected = computed(() =>
    !invalidScope.value &&
    eventId.value &&
    (pair.value === 'all' ||
      detailState.snapshot?.observation.pair === pair.value) &&
    (status.value === 'all' ||
      detailState.snapshot?.observation.status === status.value) &&
    detailState.snapshot?.observation.event_id === eventId.value
      ? detailState.snapshot
      : null,
  ),
  windows = computed(() =>
    selected.value ? maturityWindows(selected.value) : [],
  ),
  oldFilter = computed(
    () =>
      state.snapshotQuery &&
      (state.snapshotQuery.pair !== query().pair ||
        state.snapshotQuery.status !== query().status),
  ),
  rows = computed(() => state.snapshot ?? []),
  pageRows = computed(() =>
    rows.value.slice(
      (page.value - 1) * pageSize.value,
      page.value * pageSize.value,
    ),
  ),
  pages = computed(() =>
    Math.max(1, Math.ceil(rows.value.length / pageSize.value)),
  );
function query(): SurgeQuery {
  return {
    ...(pair.value === 'all' ? {} : { pair: pair.value }),
    ...(status.value === 'all' ? {} : { status: status.value }),
    timeframe: '5m',
  };
}
function refresh() {
  void list.refresh(query());
  void statistics.refresh({});
}
function select(id: string) {
  // 旧快照仍可检查，但深链必须携带该快照的范围而非当前失败的筛选。
  const next: LocationQueryRaw = { ...route.query, event_id: id };
  if (state.snapshotQuery?.pair) next.pair = state.snapshotQuery.pair;
  else delete next.pair;
  if (state.snapshotQuery?.status) next.status = state.snapshotQuery.status;
  else delete next.status;
  void router.replace({ query: next });
}
function fmt(v: unknown, d = 2) {
  const n = finite(v);
  return n === null
    ? '未知'
    : n.toLocaleString('zh-CN', { maximumFractionDigits: d });
}
function pct(v: unknown) {
  const n = finite(v);
  return n === null ? '未知' : (n > 0 ? '+' : '') + (n * 100).toFixed(3) + '%';
}
function time(v: unknown) {
  const n = timeMs(v);
  return n === null
    ? '未提供'
    : new Date(n).toLocaleString('zh-CN', { hour12: false });
}
function label(v: unknown) {
  return v === 'matured'
    ? '已成熟'
    : v === 'invalid'
      ? '无效 · 不计收益'
      : '待成熟 · unknown';
}
function bucket(s: string) {
  return pairStats(statsState.snapshot, s);
}
function metric(s: string, h: string, k: string) {
  const v = bucket(s)?.horizons;
  return v && typeof v === 'object' && !Array.isArray(v)
    ? (v as Record<string, Fact>)[h]?.[k]
    : null;
}
function warmup(s: string): Fact {
  const v = statsState.snapshot?.warmup;
  return v && typeof v === 'object' && !Array.isArray(v)
    ? ((v as Record<string, Fact>)[s] ?? {})
    : {};
}
function warmupNote(s: string) {
  const f = warmup(s);
  if (f.ready === true) return '基线就绪；只记录新观测';
  if (f.ready !== false) return '预热状态未知';
  const v = finite(f.valid_bars),
    r = finite(f.required_bars),
    last = timeMs(f.last_timestamp);
  if (v === null || r === null) return '预热中，剩余根数未知';
  const left = Math.max(0, r - v);
  if (last === null) return '还需 ' + left + ' 根连续收盘数据';
  if ((statsState.updatedAt ?? Date.now()) > last + 690000)
    return '还需 ' + left + ' 根；收盘数据延迟，恢复后再估算';
  return (
    '还需 ' +
    left +
    ' 根；连续无缺口时预计 ' +
    time(last + (left + 1) * 300000) +
    ' 完成'
  );
}
function progress(r: Fact) {
  return Array.isArray(r.completed_horizons) &&
    Array.isArray(r.required_horizons)
    ? r.completed_horizons.length + '/' + r.required_horizons.length + ' 窗口'
    : '成熟进度未提供';
}
function threshold(r: Fact) {
  return String(
    valueOf(r, 'threshold_level') ??
      valueOf(r, 'threshold_band') ??
      valueOf(r, 'threshold_multiple') ??
      valueOf(r, 'threshold') ??
      '未提供',
  );
}
watch([pair, status, invalidScope], () => {
  page.value = 1;
  refresh();
});
watch(pageSize, () => {
  page.value = 1;
});
watch(
  [eventId, pair, status, invalidScope],
  () => void details.refresh({ eventId: eventId.value, ...query() }),
  {
    immediate: true,
  },
);
let timer: ReturnType<typeof setInterval> | undefined;
onMounted(() => {
  refresh();
  timer = setInterval(() => {
    if (!state.loading) refresh();
  }, 30000);
});
onUnmounted(() => {
  if (timer) clearInterval(timer);
  list.dispose();
  statistics.dispose();
  details.dispose();
});
</script>
<template>
  <main class="surge-workspace">
    <p v-if="invalidScope" role="alert">
      筛选范围无效：不支持、重复或空的查询参数不能默认成另一范围。请选择有效交易对与状态。
    </p>
    <header class="heading">
      <div>
        <h1>订单流与价格研究</h1>
        <p>成交额突增观测账本 <span>Binance 历史研究 · futures 5m</span></p>
      </div>
      <RouterLink to="/quant/price-spikes">价格爆发与量比</RouterLink>
    </header>
    <section class="protocol">
      <strong>量比是观测条件，不是方向信号</strong>
      <p>
        当前仅 BTC / ETH。TRX 与其它周期尚未取得在线证据；depth20
        不能说明真实挂单总数、撤单量或队列位置。
      </p>
      <details>
        <summary>方法、窗口与成本边界</summary>
        <p>
          基线使用当前根之前 288 根已完成 5m K
          线的成交额中位数，当前根不进入基线；2x / 4x / 8x / 20x
          为预注册分档。以所选记录的方法版本为准。
        </p>
        <p>
          现有成熟窗口为 5m / 30m / 1h / 4h；计划中的未来 1 / 5 / 15 / 30 / 60
          分钟研究尚未接通。unknown
          与无效不计入表现统计。毛收益未扣费用、资金费和回测滑点；实际费率、限价成交率、逆向选择和统计校正尚未取得，不授予
          Demo 准入。
        </p>
      </details>
    </section>
    <div class="toolbar">
      <label
        >交易对<select v-model="pair" aria-label="观测交易对">
          <option value="all">全部交易对</option>
          <option v-for="s in pairs" :key="s" :value="s">{{ s }}</option>
        </select></label
      ><label
        >成熟状态<select v-model="status" aria-label="成熟状态">
          <option value="all">全部状态</option>
          <option value="unknown">待成熟</option>
          <option value="matured">已成熟</option>
          <option value="invalid">无效</option>
        </select></label
      ><span>成功读取 {{ time(state.updatedAt) }} · 每 30 秒刷新</span
      ><button :disabled="state.loading" @click="refresh">
        {{ state.loading ? '读取中' : '刷新观测' }}
      </button>
    </div>
    <p
      data-testid="surge-read-status"
      class="read-status"
      :role="state.error ? 'alert' : 'status'"
    >
      {{
        state.error ||
        (state.snapshot === null ? '尚未取得观测事实' : '已取得观测事实')
      }}<span v-if="oldFilter">
        · 旧筛选 {{ state.snapshotQuery?.pair ?? '全部交易对' }} /
        {{ state.snapshotQuery?.status ?? '全部状态' }}；不代表当前筛选</span
      >
    </p>
    <div class="body">
      <section class="ledger">
        <div class="section-heading">
          <h2>突增事件</h2>
          <span>本次响应 {{ rows.length }} 条</span>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>事件 / 时间</th>
                <th>交易对</th>
                <th>量比 / 档位</th>
                <th>成交额 / 基线</th>
                <th>笔数 / 失衡</th>
                <th>成熟状态</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in pageRows"
                :key="row.event_id"
                :data-selected="row.event_id === eventId"
              >
                <td>
                  <button
                    :data-event="row.event_id"
                    @click="select(row.event_id)"
                  >
                    {{ row.event_id }}</button
                  ><small>{{ time(valueOf(row, 'timestamp')) }}</small>
                </td>
                <td>
                  {{ row.pair }}<small>{{ row.timeframe }}</small>
                </td>
                <td>
                  {{ fmt(valueOf(row, 'volume_ratio')) }}×<small>{{
                    threshold(row)
                  }}</small>
                </td>
                <td>
                  {{ fmt(valueOf(row, 'quote_volume'))
                  }}<small
                    >基线
                    {{ fmt(valueOf(row, 'baseline_quote_volume')) }}</small
                  >
                </td>
                <td>
                  {{ fmt(valueOf(row, 'num_trades'), 0)
                  }}<small
                    >主动失衡 {{ fmt(valueOf(row, 'flow_imbal'), 4) }}</small
                  >
                </td>
                <td>
                  <span :data-status="row.status">{{ label(row.status) }}</span
                  ><small>{{ progress(row) }}</small>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="!rows.length" class="empty">
          {{
            state.snapshot === null
              ? '观测事实尚未取得；读取失败不能视作无事件'
              : '本次成功响应没有符合筛选的事件；预热或缺口状态见下方'
          }}
        </p>
        <footer class="pagination">
          <label
            >每页<select v-model.number="pageSize" aria-label="事件每页数量">
              <option :value="10">10</option>
              <option :value="25">25</option>
              <option :value="50">50</option>
            </select></label
          ><button :disabled="page <= 1" @click="page--">上一页</button
          ><span>{{ page }} / {{ pages }}</span
          ><button :disabled="page >= pages" @click="page++">下一页</button>
        </footer>
      </section>
      <aside class="inspector" data-testid="surge-inspector">
        <h2>所选事件</h2>
        <p v-if="!eventId" class="empty">
          选择事件，核对方法、输入指纹与各成熟窗口。
        </p>
        <p v-else-if="detailState.loading && !selected">
          正在读取 {{ eventId }}
        </p>
        <p v-else-if="!selected" role="alert">
          指定事件未取得：{{
            eventId
          }}。记录不存在、不可读取或合同不匹配；不会替换为其它事件。
        </p>
        <template v-if="selected"
          ><p v-if="detailState.error" role="alert">{{ detailState.error }}</p>
          <dl>
            <dt>事件 ID</dt>
            <dd>{{ selected.observation.event_id }}</dd>
            <dt>交易对 / 时间</dt>
            <dd>
              {{ selected.observation.pair
              }}<small>{{ time(selected.observation.timestamp) }}</small>
            </dd>
            <dt>方法版本</dt>
            <dd>{{ selected.observation.method_version ?? '未提供' }}</dd>
            <dt>数据指纹</dt>
            <dd class="hash">
              {{ selected.observation.data_fingerprint ?? '未提供' }}
            </dd>
            <dt>成熟状态</dt>
            <dd>
              {{ label(selected.observation.status) }} ·
              {{ progress(selected.observation) }}
            </dd>
          </dl>
          <h3>相对观测 t0 的成熟窗口</h3>
          <div class="table-scroll">
            <table class="maturity">
              <thead>
                <tr>
                  <th>窗口 / 状态</th>
                  <th>毛收益 / MFE / MAE</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="w in windows" :key="w.horizon">
                  <td>
                    {{ w.horizon }}<small>{{ label(w.status) }}</small>
                  </td>
                  <td>
                    {{ pct(w.return)
                    }}<small>{{ pct(w.mfe) }} / {{ pct(w.mae) }}</small
                    ><small>{{ time(w.maturity_timestamp) }}</small>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <h3>修订时间线</h3>
          <ol class="revisions">
            <li
              v-for="(r, i) in selected.events"
              :key="String(r.revision_id ?? i)"
            >
              <strong>{{
                r.event_type ?? r.revision_kind ?? '事件类型未提供'
              }}</strong
              ><span
                >序号 {{ r.revision_no ?? r.sequence_id ?? '未知' }} ·
                {{ time(r.created_at) }}</span
              >
            </li>
          </ol>
          <p v-if="!selected.events.length">本次成功详情无修订记录。</p>
          <details data-testid="surge-raw">
            <summary>原始观测与完整修订</summary>
            <pre>{{ JSON.stringify(selected, null, 2) }}</pre>
          </details></template
        >
      </aside>
    </div>
    <section class="statistics">
      <div class="section-heading">
        <h2>按币种与成熟窗口核对</h2>
        <span>{{
          statsState.error
            ? '历史统计 · ' + statsState.error
            : '统计读取 ' + time(statsState.updatedAt)
        }}</span>
      </div>
      <p v-if="statsState.snapshot === null" class="empty">
        统计证据尚未取得，不以零样本代替。
      </p>
      <div v-else class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>币种 / 实时基线</th>
              <th>样本 / 成熟 / 待成熟</th>
              <th>窗口</th>
              <th>成熟样本</th>
              <th>毛收益 / 正收益比例</th>
              <th>MFE / MAE 均值</th>
              <th>所需 / 实际费率</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="s in pairs" :key="s"
              ><tr v-for="(h, i) in ['5m', '30m', '1h', '4h']" :key="s + h">
                <td v-if="i === 0" rowspan="4">
                  <strong>{{ s }}</strong
                  ><small
                    >{{ fmt(warmup(s).valid_bars, 0) }} /
                    {{ fmt(warmup(s).required_bars, 0) }} 根</small
                  ><small>{{ warmupNote(s) }}</small
                  ><small>最近根 {{ time(warmup(s).last_timestamp) }}</small>
                </td>
                <td v-if="i === 0" rowspan="4">
                  {{ fmt(bucket(s)?.total, 0) }} /
                  {{ fmt(bucket(s)?.matured, 0) }} /
                  {{ fmt(bucket(s)?.unknown, 0)
                  }}<small
                    >概览窗口
                    {{ bucket(s)?.statistics_horizon ?? '未提供' }}</small
                  >
                </td>
                <td>{{ h }}</td>
                <td>{{ fmt(metric(s, h, 'sample_count'), 0) }}</td>
                <td>
                  {{ pct(metric(s, h, 'gross_return_mean'))
                  }}<small>{{ pct(metric(s, h, 'win_rate')) }}</small>
                </td>
                <td>
                  {{ pct(metric(s, h, 'mfe_mean'))
                  }}<small>{{ pct(metric(s, h, 'mae_mean')) }}</small>
                </td>
                <td>
                  {{ pct(bucket(s)?.required_fee_rate) }} /
                  {{ pct(bucket(s)?.actual_fee_rate) }}
                </td>
              </tr></template
            >
          </tbody>
        </table>
      </div>
    </section>
  </main>
</template>
<style scoped>
.surge-workspace {
  --line: #29485c;
  min-width: 0;
  padding: 24px;
  background: #0c1b2a;
  color: #e5edf4;
  font:
    14px/1.55 'IBM Plex Sans',
    'Source Han Sans SC',
    'Noto Sans CJK SC',
    sans-serif;
}
.heading,
.toolbar,
.section-heading,
.pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.heading h1 {
  margin: 0;
  font-size: 25px;
}
.heading p {
  margin: 5px 0 0;
  color: #a2b8c9;
}
.heading p span {
  margin-left: 16px;
  font-size: 12px;
}
.surge-workspace a,
.ledger td button {
  color: #69b4e4;
}
.protocol {
  border-left: 3px solid #69b4e4;
  margin: 22px 0;
  background: #132b3e;
  padding: 12px 16px;
}
.protocol p {
  margin: 5px 0;
  color: #a2b8c9;
  max-width: 100ch;
}
.protocol details {
  margin-top: 8px;
}
.surge-workspace summary {
  cursor: pointer;
  color: #69b4e4;
}
.toolbar {
  justify-content: flex-start;
  gap: 12px;
  border-bottom: 1px solid var(--line);
  padding-bottom: 16px;
}
.toolbar label,
.pagination label {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #a2b8c9;
  font-size: 12px;
}
.toolbar > span {
  margin-left: auto;
  color: #a2b8c9;
  font-size: 12px;
}
.surge-workspace button,
.surge-workspace select {
  border: 1px solid #47718c;
  background: #132b3e;
  color: #e5edf4;
  padding: 7px 11px;
  border-radius: 4px;
  font: inherit;
}
.surge-workspace button {
  cursor: pointer;
}
.surge-workspace button:disabled {
  opacity: 0.45;
  cursor: default;
}
.surge-workspace :is(a, button, select, summary):focus-visible {
  outline: 2px solid #69b4e4;
  outline-offset: 3px;
}
.read-status {
  margin: 12px 0;
  font-size: 12px;
  color: #a2b8c9;
}
.read-status[role='alert'],
.inspector [role='alert'] {
  color: #f0c17b;
}
.body {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(280px, 340px);
  gap: 20px;
  align-items: start;
}
.ledger,
.inspector,
.statistics {
  min-width: 0;
  background: #132b3e;
  border: 1px solid var(--line);
}
.section-heading {
  padding: 14px 16px;
  border-bottom: 1px solid var(--line);
}
.section-heading h2,
.inspector h2 {
  font-size: 16px;
  margin: 0;
}
.section-heading span {
  font-size: 12px;
  color: #a2b8c9;
}
.table-scroll {
  overflow-x: auto;
  max-width: 100%;
}
.surge-workspace table {
  width: 100%;
  border-collapse: collapse;
  font-variant-numeric: tabular-nums;
  text-align: left;
  white-space: nowrap;
}
.surge-workspace th {
  font-size: 12px;
  font-weight: 500;
  color: #a2b8c9;
  background: #102538;
  padding: 10px 13px;
}
.surge-workspace td {
  padding: 12px 13px;
  border-top: 1px solid var(--line);
  font-size: 12px;
  vertical-align: top;
}
.surge-workspace td small,
.surge-workspace dd small {
  display: block;
  color: #a2b8c9;
  margin-top: 4px;
}
.ledger td button {
  border: 0;
  padding: 0;
  background: transparent;
  text-align: left;
  max-width: 220px;
  overflow-wrap: anywhere;
  white-space: normal;
}
.ledger tr[data-selected='true'] {
  background: #1b3c53;
  box-shadow: inset 3px 0 #69b4e4;
}
.pagination {
  justify-content: flex-end;
  padding: 12px 14px;
  font-size: 12px;
  border-top: 1px solid var(--line);
}
.pagination label {
  margin-right: auto;
}
.inspector {
  padding: 16px;
}
.inspector h2 {
  padding-bottom: 10px;
  border-bottom: 1px solid var(--line);
}
.inspector h3 {
  font-size: 13px;
  margin: 22px 0 9px;
}
.inspector dt {
  font-size: 11px;
  color: #a2b8c9;
  margin-top: 12px;
}
.inspector dd {
  font-size: 12px;
  margin: 3px 0;
  overflow-wrap: anywhere;
}
.hash,
.revisions span {
  font-family: 'IBM Plex Mono', monospace;
}
.maturity td,
.maturity th {
  padding: 8px;
}
.revisions {
  list-style: none;
  padding: 0;
  border-left: 1px solid #47718c;
}
.revisions li {
  position: relative;
  padding: 0 0 14px 13px;
}
.revisions li::before {
  content: '';
  width: 5px;
  height: 5px;
  background: #69b4e4;
  position: absolute;
  left: -3px;
  top: 7px;
  border-radius: 50%;
}
.revisions strong,
.revisions span {
  display: block;
  font-size: 11px;
  font-weight: 400;
  overflow-wrap: anywhere;
}
.revisions span {
  color: #a2b8c9;
}
.surge-workspace pre {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 11px;
  max-height: 400px;
  overflow: auto;
  background: #0c1b2a;
  padding: 10px;
}
.empty {
  padding: 22px 16px;
  color: #a2b8c9;
  font-size: 12px;
}
.statistics {
  margin-top: 22px;
}
.statistics td:first-child small {
  max-width: 240px;
  white-space: normal;
}
[data-status='invalid'] {
  color: #f19a9a;
}
[data-status='unknown'] {
  color: #f0c17b;
}
[data-status='matured'] {
  color: #7acbaa;
}
@media (min-width: 1700px) {
  .body {
    grid-template-columns: minmax(0, 1fr) 380px;
  }
  .surge-workspace td {
    font-size: 13px;
  }
}
@media (max-width: 1100px) {
  .body {
    grid-template-columns: minmax(0, 1fr);
  }
  .inspector dl {
    display: grid;
    grid-template-columns: 140px minmax(0, 1fr);
    gap: 8px;
  }
  .inspector dt,
  .inspector dd {
    margin: 0;
  }
  .maturity {
    min-width: 430px;
  }
}
@media (max-width: 600px) {
  .surge-workspace {
    padding: 14px;
  }
  .heading h1 {
    font-size: 21px;
  }
  .heading p span {
    display: block;
    margin: 3px 0 0;
  }
  .toolbar > span {
    width: 100%;
    order: 3;
    margin: 0;
  }
  .toolbar label {
    width: 100%;
    justify-content: space-between;
  }
  .toolbar select {
    width: 68%;
    min-width: 0;
  }
  .toolbar > button {
    width: 100%;
  }
  .protocol {
    margin: 16px 0;
    font-size: 12px;
  }
  .inspector dl {
    grid-template-columns: minmax(0, 1fr);
  }
  .inspector dd {
    margin-bottom: 8px;
  }
  .pagination {
    gap: 8px;
  }
  .section-heading {
    gap: 6px;
    padding: 12px;
  }
  .section-heading span {
    width: 100%;
  }
}
</style>

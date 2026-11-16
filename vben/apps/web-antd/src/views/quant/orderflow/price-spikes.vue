<script lang="ts" setup>
import { computed, onMounted, onUnmounted, watch } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';

import { getOrderflowPriceSpikes } from '#/api/database';

import {
  createPriceReader,
  outcomeLabel,
  priceBands,
  priceNumber,
  profileSegments,
  selectPriceEvent,
} from './priceResearch';
import type { PriceEvent, PriceQuery, PriceRecord } from './priceResearch';

const route = useRoute();
const router = useRouter();
const supportedPairs = ['BTC/USDT:USDT', 'ETH/USDT:USDT'];
// URL 是唯一筛选身份；移除参数恢复默认，重复/非法参数阻止新读取。
const queryError = computed(() => {
  const values = route.query;
  if (
    values.pair !== undefined &&
    (typeof values.pair !== 'string' || !supportedPairs.includes(values.pair))
  )
    return '查询参数无效：请选择 BTC 或 ETH 的单一交易对';
  if (
    values.direction !== undefined &&
    (typeof values.direction !== 'string' ||
      !['any', 'up', 'down'].includes(values.direction))
  )
    return '查询参数无效：方向必须为 any、up 或 down';
  if (
    values.event_id !== undefined &&
    (typeof values.event_id !== 'string' ||
      !values.event_id ||
      values.event_id.length > 512)
  )
    return '查询参数无效：事件身份必须唯一且非空';
  return '';
});
const pair = computed({
  get: () =>
    typeof route.query.pair === 'string' &&
    supportedPairs.includes(route.query.pair)
      ? route.query.pair
      : supportedPairs[0]!,
  set: (value: string) => {
    void router.replace({
      query: { ...route.query, pair: value, event_id: undefined },
    });
  },
});
const direction = computed<PriceQuery['direction']>({
  get: () =>
    typeof route.query.direction === 'string' &&
    ['up', 'down'].includes(route.query.direction)
      ? (route.query.direction as 'up' | 'down')
      : 'any',
  set: (value) => {
    void router.replace({
      query: { ...route.query, direction: value, event_id: undefined },
    });
  },
});
const reader = createPriceReader();
const snapshot = computed(() => reader.state.snapshot);
const sourcePair = computed(() => snapshot.value?.query.pair ?? pair.value);
const eventId = computed(() =>
  typeof route.query.event_id === 'string' ? route.query.event_id : '',
);
const selection = computed(() =>
  selectPriceEvent(snapshot.value?.items ?? [], eventId.value),
);
const pairStats = computed(
  () => snapshot.value?.stats.by_pair[sourcePair.value] ?? {},
);
const warmup = computed(() => snapshot.value?.warmup[sourcePair.value] ?? {});
const profile = computed(
  () => snapshot.value?.stats.profile_by_pair[sourcePair.value] ?? {},
);
const stale = computed(
  () =>
    !!snapshot.value &&
    (!!queryError.value ||
      !!reader.state.error ||
      snapshot.value.query.pair !== pair.value ||
      snapshot.value.query.direction !== direction.value),
);
const bands = computed(() =>
  priceBands.map((band) => ({
    ...band,
    values: nested(pairStats.value.bands)[band.key] as PriceRecord | undefined,
  })),
);
const curves = computed(() =>
  ['up', 'down'].map((side) => ({
    side,
    segments: profileSegments(profile.value[side]),
  })),
);
const maxRatio = computed(() =>
  Math.max(
    1,
    ...curves.value.flatMap((curve) =>
      curve.segments.flatMap((segment) => segment.map((point) => point.value)),
    ),
  ),
);
const hasProfile = computed(() =>
  curves.value.some((curve) => curve.segments.length),
);
const raw = computed(() =>
  selection.value.selected
    ? JSON.stringify(selection.value.selected, null, 2)
    : '',
);

function nested(value: unknown): PriceRecord {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as PriceRecord)
    : {};
}
function fixed(value: unknown, digits = 2) {
  const n = priceNumber(value);
  return n === null
    ? '未知'
    : n.toLocaleString('zh-CN', { maximumFractionDigits: digits });
}
function percent(value: unknown) {
  const n = priceNumber(value);
  return n === null ? '未知' : `${n > 0 ? '+' : ''}${(n * 100).toFixed(2)}%`;
}
function localTime(value: unknown) {
  const n = priceNumber(value);
  if (n === null || n < 0 || n > 8640000000000000) return '未知';
  return new Date(n).toLocaleString('zh-CN', { hour12: false });
}
function value(row: PriceEvent, name: string) {
  return row.observation[name] ?? row[name];
}
function profileStatus(row: PriceEvent) {
  return row.profile_status === 'matured'
    ? '完整'
    : row.profile_status === 'invalid'
      ? '有缺口'
      : '待成熟';
}
function points(segment: { offset: number; value: number }[]) {
  return segment
    .map(
      (point) =>
        `${30 + (point.offset + 12) * 20},${180 - (point.value / maxRatio.value) * 150}`,
    )
    .join(' ');
}
function choose(row: PriceEvent) {
  void router.replace({
    query: {
      ...route.query,
      pair: sourcePair.value,
      direction: snapshot.value?.query.direction ?? direction.value,
      event_id: row.event_id,
    },
  });
}
async function refresh() {
  if (queryError.value) {
    reader.cancel();
    return;
  }
  const query: PriceQuery = { pair: pair.value, direction: direction.value };
  await reader.load(query, (signal) =>
    getOrderflowPriceSpikes({ ...query, limit: 100 }, signal),
  );
}
watch([pair, direction, queryError], () => void refresh());
let timer: ReturnType<typeof setInterval> | undefined;
onMounted(() => {
  void refresh();
  timer = setInterval(() => {
    if (!reader.state.loading) void refresh();
  }, 60_000);
});
onUnmounted(() => {
  reader.cancel();
  if (timer) clearInterval(timer);
});
</script>

<template>
  <main class="price-workspace" :aria-busy="reader.state.loading">
    <header class="price-header">
      <div>
        <h1>价格事件研究</h1>
        <p>在线候选、成熟结果与量比剖面</p>
      </div>
      <RouterLink to="/quant/orderflow">查看成交额突增</RouterLink>
    </header>
    <section class="price-method" aria-label="研究方法边界">
      <strong>量比预测波动，不提供独立方向信号</strong>
      <p>
        当前方法：候选根收盘至第 3 根后续 K 线收盘，15 分钟绝对收益达到
        1.5%。仅统计基线就绪后的在线已收盘样本。
      </p>
      <p>
        前 288 根已完成 5m K 线中位数作为基线，排除当前根。2x / 4x / 8x / 20x
        为分档；新计划的 1/5/15/30/60
        分钟评估尚未接通。成本与成交能力尚未核验，当前观测不授予 Demo 准入。
      </p>
      <p>
        此在线接口仅提供 BTC / ETH、5m 数据。TRX 与其它周期尚未接通，盘口历史
        不足时保持不可评估；20 档快照不能推算全市场挂单数、撤单量或排队位置。
      </p>
    </section>
    <form class="price-filters" @submit.prevent="refresh">
      <label
        >交易对<select v-model="pair" aria-label="价格研究交易对">
          <option v-for="item in supportedPairs" :key="item" :value="item">
            {{ item }}
          </option>
        </select></label
      >
      <label
        >事件方向<select v-model="direction" aria-label="价格研究方向">
          <option value="any">全部爆发与待判定</option>
          <option value="up">上涨爆发</option>
          <option value="down">下跌爆发</option>
        </select></label
      >
      <span>固定周期 5m</span
      ><button type="submit" :disabled="reader.state.loading">
        {{ reader.state.loading ? '读取中' : '刷新观测' }}
      </button>
    </form>
    <p v-if="queryError || reader.state.error" role="alert" class="price-error">
      {{ queryError || reader.state.error }}
    </p>
    <div
      v-if="snapshot"
      class="price-source"
      data-testid="price-snapshot-scope"
    >
      <strong>{{ stale ? '旧快照' : '成功快照' }} · {{ sourcePair }}</strong>
      <span
        >{{
          snapshot.query.direction === 'any'
            ? '全部爆发与待判定'
            : snapshot.query.direction === 'up'
              ? '上涨爆发'
              : '下跌爆发'
        }}
        · 接收 {{ localTime(snapshot.receivedAt) }}</span
      >
      <code>{{ snapshot.method }}</code>
    </div>
    <p v-else class="price-empty">
      {{
        reader.state.loading
          ? '正在读取在线价格研究'
          : '尚无成功快照；不能判断是否存在候选'
      }}
    </p>
    <template v-if="snapshot">
      <dl class="price-summary">
        <div>
          <dt>在线有效试验</dt>
          <dd>{{ fixed(pairStats.matured_count, 0) }}</dd>
          <small
            >候选 {{ fixed(pairStats.candidate_count, 0) }} / 无效
            {{ fixed(pairStats.invalid_count, 0) }}</small
          >
        </div>
        <div>
          <dt>无条件 P(任一)</dt>
          <dd>{{ percent(pairStats.base_p_any) }}</dd>
          <small>未成熟及无效窗口均排除</small>
        </div>
        <div>
          <dt>在线采样区间</dt>
          <dd class="price-period">
            {{ localTime(pairStats.first_timestamp) }}
          </dd>
          <small>至 {{ localTime(pairStats.last_timestamp) }}</small>
        </div>
        <div>
          <dt>基线预热</dt>
          <dd>
            {{ fixed(warmup.valid_bars, 0) }} /
            {{ fixed(warmup.required_bars, 0) }}
          </dd>
          <small
            >{{
              warmup.ready === true
                ? '基线就绪'
                : warmup.ready === false
                  ? '预热中'
                  : '就绪状态未知'
            }}
            · {{ warmup.source ?? '来源未知' }}</small
          >
        </div>
      </dl>
      <div class="price-event-grid">
        <section aria-labelledby="price-ledger-title" class="price-panel">
          <div class="price-section-heading">
            <h2 id="price-ledger-title">候选账本</h2>
            <span>最近 {{ snapshot.items.length }} 条</span>
          </div>
          <div
            class="price-table-scroll"
            tabindex="0"
            aria-label="价格候选表，可横向滚动"
          >
            <table data-testid="price-events">
              <thead>
                <tr>
                  <th>候选根 / 事件</th>
                  <th>成熟结果</th>
                  <th>15 分钟收益</th>
                  <th>量比 / 档位</th>
                  <th>成熟时间 / 剖面</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="row in snapshot.items"
                  :key="row.event_id"
                  :class="{ selected: row.event_id === eventId }"
                >
                  <td>
                    <button
                      type="button"
                      class="price-event-button"
                      :data-event="row.event_id"
                      :aria-pressed="row.event_id === eventId"
                      @click="choose(row)"
                    >
                      {{ localTime(row.timestamp)
                      }}<code>{{ row.event_id }}</code>
                    </button>
                  </td>
                  <td>{{ outcomeLabel(row) }}</td>
                  <td>{{ percent(row.outcome?.return_pct) }}</td>
                  <td>
                    {{ fixed(value(row, 'volume_ratio')) }}×<small>{{
                      value(row, 'ratio_band') ?? '档位未知'
                    }}</small>
                  </td>
                  <td>
                    {{
                      localTime(row.outcome?.matured_at_utc ?? row.observed_at)
                    }}<small>{{ profileStatus(row) }}</small>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-if="!snapshot.items.length" class="price-empty">
            本次成功查询没有符合条件的在线候选。
          </p>
        </section>
        <aside
          class="price-inspector"
          data-testid="price-inspector"
          aria-label="所选价格事件证据"
        >
          <h2>事件检查器</h2>
          <template v-if="selection.selected"
            ><code class="price-identity">{{
              selection.selected.event_id
            }}</code>
            <p>{{ outcomeLabel(selection.selected) }}</p>
            <dl>
              <div>
                <dt>候选收盘根</dt>
                <dd>{{ localTime(selection.selected.timestamp) }}</dd>
              </div>
              <div>
                <dt>真实结果</dt>
                <dd>{{ percent(selection.selected.outcome?.return_pct) }}</dd>
              </div>
              <div>
                <dt>剖面状态</dt>
                <dd>{{ profileStatus(selection.selected) }}</dd>
              </div>
              <div>
                <dt>方法版本</dt>
                <dd>
                  {{ selection.selected.method_version ?? snapshot.method }}
                </dd>
              </div>
              <div>
                <dt>输入指纹</dt>
                <dd class="price-identity">
                  {{ selection.selected.data_fingerprint ?? '未提供' }}
                </dd>
              </div>
            </dl>
            <details>
              <summary>原始观测与结果</summary>
              <pre>{{ raw }}</pre>
            </details>
          </template>
          <p v-else-if="selection.missing" class="price-notice">
            未找到事件 {{ eventId }}。它可能不在当前交易对、方向或最近 100
            条范围内；没有替换为其它记录。
          </p>
          <p v-else>选择一条候选查看其成熟事实和输入身份。</p>
        </aside>
      </div>
      <section class="price-panel" aria-labelledby="price-probability-title">
        <div class="price-section-heading">
          <h2 id="price-probability-title">条件概率与无条件提升</h2>
          <span>在线样本 · {{ sourcePair }}</span>
        </div>
        <div
          class="price-table-scroll"
          tabindex="0"
          aria-label="在线条件概率表，可横向滚动"
        >
          <table>
            <thead>
              <tr>
                <th>量比档</th>
                <th>有效样本</th>
                <th>P(涨)</th>
                <th>P(跌)</th>
                <th>P(任一)</th>
                <th>无条件提升</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="band in bands" :key="band.key">
                <th>{{ band.label }}</th>
                <td>{{ fixed(band.values?.matured_count, 0) }}</td>
                <td>{{ percent(band.values?.p_up) }}</td>
                <td>{{ percent(band.values?.p_down) }}</td>
                <td>{{ percent(band.values?.p_any) }}</td>
                <td>
                  {{
                    band.values?.lift == null
                      ? '未知'
                      : `${fixed(band.values.lift)}×`
                  }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
      <section class="price-panel" aria-labelledby="price-profile-title">
        <div class="price-section-heading">
          <h2 id="price-profile-title">上涨与下跌量比剖面</h2>
          <span>t−12 至 t+12 · 每格 5m</span>
        </div>
        <p>
          只显示完整在线事件的实际方向分组。缺失点断开，零样本不补零；相同方向各点样本量可能不同。
        </p>
        <div v-if="hasProfile" class="price-profile">
          <svg
            viewBox="0 0 540 215"
            role="img"
            :aria-label="`${sourcePair} 实际量比剖面，上涨蓝色实线，下跌黄色虚线`"
          >
            <line x1="30" y1="180" x2="510" y2="180" class="price-axis" />
            <line x1="270" y1="20" x2="270" y2="180" class="price-origin" />
            <g
              v-for="curve in curves"
              :key="curve.side"
              :class="`price-curve-${curve.side}`"
            >
              <polyline
                v-for="(segment, index) in curve.segments"
                :key="index"
                :points="points(segment)"
                fill="none"
              />
              <template
                v-for="(segment, index) in curve.segments"
                :key="`points-${index}`"
              >
                <circle
                  v-for="point in segment"
                  :key="point.offset"
                  :cx="30 + (point.offset + 12) * 20"
                  :cy="180 - (point.value / maxRatio) * 150"
                  r="2"
                >
                  <title>
                    {{ curve.side === 'up' ? '上涨 +' : '下跌 −' }}，t{{
                      point.offset > 0 ? '+' : ''
                    }}{{ point.offset }}：{{ point.value }}×
                  </title>
                </circle>
              </template>
            </g>
            <text x="30" y="202">−12</text>
            <text x="262" y="202">t0</text>
            <text x="485" y="202">+12</text>
            <text x="30" y="15">{{ fixed(maxRatio) }}×</text>
          </svg>
          <div class="price-legend">
            <span>上涨 + 蓝色实线</span><span>下跌 − 黄色虚线</span>
          </div>
        </div>
        <p v-else class="price-empty">当前币种尚无完整剖面样本。</p>
        <details>
          <summary>查看逐点样本量与均值</summary>
          <div class="price-table-scroll" tabindex="0">
            <table>
              <thead>
                <tr>
                  <th>方向</th>
                  <th>相对根</th>
                  <th>量比均值</th>
                  <th>样本量</th>
                </tr>
              </thead>
              <tbody>
                <template v-for="side in ['up', 'down']" :key="side"
                  ><tr
                    v-for="point in (profile[side] as PriceRecord[]) ?? []"
                    :key="`${side}-${point.offset}`"
                  >
                    <td>{{ side === 'up' ? '上涨 +' : '下跌 −' }}</td>
                    <td>{{ point.offset }}</td>
                    <td>{{ fixed(point.mean_volume_ratio) }}</td>
                    <td>{{ fixed(point.sample_count, 0) }}</td>
                  </tr></template
                >
              </tbody>
            </table>
          </div>
        </details>
      </section>
    </template>
    <section class="price-history">
      <h2>第三轮回顾性参考</h2>
      <p>
        BTC ≥20x：P(任一) 4.28% · 涨 2.44% · 跌 1.83%；ETH ≥20x：P(任一) 7.93% ·
        涨 4.21% · 跌 3.72%。
      </p>
      <p>
        来自 2.5
        年回顾样本，不并入在线统计。原研究未注明收益锚点，不能作为当前方法复算结果。按表内概率，约
        95.7% 的 BTC 与 92.1% 的 ETH 窗口未达到
        1.5%；未覆盖滑点、资金费与成交可行性。
      </p>
    </section>
  </main>
</template>

<style scoped>
.price-workspace {
  --price-bg: #0c1b2a;
  --price-surface: #132b3e;
  --price-line: #29485c;
  --price-text: #e5edf4;
  --price-muted: #a2b8c9;
  --price-blue: #69b4e4;
  background: var(--price-bg);
  color: var(--price-text);
  padding: 24px;
  min-width: 0;
  font-family:
    'IBM Plex Sans', 'Source Han Sans SC', 'Noto Sans CJK SC', sans-serif;
  font-size: 14px;
  line-height: 1.6;
}
.price-workspace * {
  box-sizing: border-box;
}
.price-header,
.price-section-heading,
.price-filters,
.price-source {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.price-header h1 {
  font-size: 26px;
  margin: 0;
  font-weight: 600;
}
.price-header p,
.price-panel > p {
  color: var(--price-muted);
  margin: 4px 0 12px;
}
.price-workspace a {
  color: var(--price-blue);
}
.price-method {
  border-left: 3px solid var(--price-blue);
  padding: 12px 16px;
  background: var(--price-surface);
  margin: 20px 0;
}
.price-method p,
.price-history p {
  max-width: 100ch;
  color: var(--price-muted);
  margin: 5px 0;
}
.price-filters {
  justify-content: flex-start;
  gap: 12px;
  padding: 12px 0;
  border-bottom: 1px solid var(--price-line);
}
.price-filters label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: var(--price-muted);
  font-size: 12px;
}
.price-workspace button,
.price-workspace select {
  border: 1px solid var(--price-line);
  background: var(--price-surface);
  color: var(--price-text);
  border-radius: 3px;
  padding: 7px 10px;
  font: inherit;
}
.price-workspace button {
  cursor: pointer;
}
.price-workspace button:disabled {
  opacity: 0.6;
  cursor: wait;
}
.price-workspace :is(button, select, a, summary, [tabindex]):focus-visible {
  outline: 2px solid var(--price-blue);
  outline-offset: 3px;
}
.price-error {
  border: 1px solid #915b5b;
  background: #3d252c;
  padding: 12px;
  color: #f19a9a;
}
.price-source {
  align-items: flex-start;
  justify-content: flex-start;
  font-size: 12px;
  padding: 12px 0;
  color: var(--price-muted);
}
.price-source code {
  margin-left: auto;
}
.price-summary {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  border-top: 1px solid var(--price-line);
  border-bottom: 1px solid var(--price-line);
  margin: 0 0 20px;
}
.price-summary > div {
  padding: 14px 12px;
  border-right: 1px solid var(--price-line);
  min-width: 0;
}
.price-summary > div:last-child {
  border-right: 0;
}
.price-summary dt,
small {
  color: var(--price-muted);
  font-size: 12px;
}
.price-summary dd {
  font-size: 24px;
  margin: 4px 0;
  font-variant-numeric: tabular-nums;
}
.price-summary dd.price-period {
  font-size: 14px;
  padding: 9px 0;
}
.price-event-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(260px, 320px);
  align-items: start;
  gap: 20px;
}
.price-panel,
.price-inspector {
  min-width: 0;
  border: 1px solid var(--price-line);
  background: var(--price-surface);
  margin-bottom: 20px;
}
.price-section-heading {
  padding: 12px 16px;
  border-bottom: 1px solid var(--price-line);
}
.price-workspace h2 {
  font-size: 16px;
  font-weight: 600;
  margin: 0;
}
.price-section-heading span {
  font-size: 12px;
  color: var(--price-muted);
}
.price-table-scroll {
  overflow: auto;
  max-width: 100%;
  min-width: 0;
}
.price-workspace table {
  width: 100%;
  border-collapse: collapse;
  white-space: nowrap;
  text-align: left;
  font-variant-numeric: tabular-nums;
}
.price-workspace td,
.price-workspace th {
  padding: 10px 14px;
  border-bottom: 1px solid var(--price-line);
  font-size: 12px;
}
.price-workspace th {
  color: var(--price-muted);
  font-weight: 500;
}
.price-workspace td small {
  display: block;
}
.price-workspace tr.selected {
  background: #1d3d54;
}
.price-workspace .price-event-button {
  padding: 0;
  border: 0;
  background: transparent;
  text-align: left;
  color: var(--price-blue);
}
.price-event-button code {
  display: block;
  max-width: 235px;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 11px;
}
.price-inspector {
  padding: 16px;
}
.price-inspector h2 {
  margin-bottom: 12px;
}
.price-identity {
  overflow-wrap: anywhere;
  font:
    12px/1.6 'IBM Plex Mono',
    monospace;
}
.price-inspector dl > div {
  padding: 10px 0;
  border-bottom: 1px solid var(--price-line);
}
.price-inspector dt {
  color: var(--price-muted);
  font-size: 12px;
}
.price-inspector dd {
  margin: 3px 0 0;
}
.price-inspector details {
  margin-top: 16px;
}
.price-workspace summary {
  cursor: pointer;
  color: var(--price-blue);
  padding: 8px 0;
}
.price-inspector pre {
  overflow: auto;
  max-height: 380px;
  background: var(--price-bg);
  padding: 10px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 11px;
}
.price-empty,
.price-notice {
  padding: 16px;
  color: var(--price-muted);
  margin: 0;
}
.price-panel > p,
.price-panel > details {
  padding: 0 16px;
}
.price-profile {
  max-width: 820px;
  margin: 0 auto;
  padding: 0 18px;
}
.price-profile svg {
  width: 100%;
  height: auto;
}
.price-profile text {
  fill: var(--price-muted);
  font-size: 11px;
}
.price-axis {
  stroke: var(--price-line);
}
.price-origin {
  stroke: var(--price-line);
  stroke-dasharray: 3 4;
}
.price-curve-up {
  stroke: #69b4e4;
  fill: #69b4e4;
  stroke-width: 2;
}
.price-curve-down {
  stroke: #f0c17b;
  fill: #f0c17b;
  stroke-width: 2;
  stroke-dasharray: 5 3;
}
.price-legend {
  display: flex;
  justify-content: center;
  gap: 18px;
  font-size: 12px;
  color: var(--price-muted);
  padding-bottom: 10px;
}
.price-history {
  border-top: 1px solid var(--price-line);
  padding-top: 16px;
  font-size: 12px;
}
.price-workspace code {
  font-family: 'IBM Plex Mono', monospace;
}
.price-workspace p {
  overflow-wrap: anywhere;
}
@media (max-width: 1100px) {
  .price-event-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .price-inspector dl {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0 16px;
  }
}
@media (max-width: 768px) {
  .price-workspace {
    padding: 16px;
  }
  .price-summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .price-summary > div:nth-child(2) {
    border-right: 0;
  }
  .price-summary > div:nth-child(-n + 2) {
    border-bottom: 1px solid var(--price-line);
  }
  .price-source code {
    margin-left: 0;
    overflow-wrap: anywhere;
  }
  .price-filters label {
    flex: 1;
    min-width: 145px;
  }
  .price-filters select {
    width: 100%;
  }
  .price-section-heading {
    padding: 10px 12px;
  }
}
@media (max-width: 420px) {
  .price-workspace {
    padding: 12px;
  }
  .price-header h1 {
    font-size: 23px;
  }
  .price-summary dd {
    font-size: 20px;
  }
  .price-inspector dl {
    grid-template-columns: minmax(0, 1fr);
  }
  .price-source {
    gap: 5px;
  }
  .price-filters > span {
    font-size: 12px;
  }
  .price-legend {
    flex-wrap: wrap;
    gap: 4px 12px;
  }
}
</style>

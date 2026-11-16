<script lang="ts" setup>
import type { ResearchRecord, ResearchSnapshot } from './researchState';
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import { getResearch, getResearchStatus } from '#/api/freqtrade';
import {
  metricText,
  parseResearchSnapshot,
  pickResearchCandidate,
  researchLevel,
  researchTime,
} from './researchState';

const route = useRoute();
const router = useRouter();
const data = ref<ResearchSnapshot | null>(null);
const status = ref<Record<string, unknown> | null>(null);
const loadError = ref('');
const statusError = ref('');
const refreshing = ref(false);
const now = ref(Date.now());
const sections = [
  { key: 'routes', label: '研究方向' },
  { key: 'models', label: '模型对比' },
  { key: 'carry', label: 'Carry 拆解' },
  { key: 'liquidation', label: '强平情景' },
  { key: 'sweep', label: '参数扫描' },
  { key: 'buffers', label: '保证金缓冲' },
  { key: 'schemes', label: '保证金方案' },
];
const section = ref('routes');
const invalidSection = computed(
  () =>
    Object.hasOwn(route.query, 'section') &&
    (typeof route.query.section !== 'string' ||
      !sections.some((item) => item.key === route.query.section)),
);
const candidate = computed(() =>
  typeof route.query.candidate === 'string' ? route.query.candidate : '',
);
const invalidCandidate = computed(
  () =>
    Object.hasOwn(route.query, 'candidate') &&
    (typeof route.query.candidate !== 'string' ||
      !route.query.candidate.trim()),
);
const selected = computed(() =>
  invalidCandidate.value
    ? null
    : pickResearchCandidate(data.value?.routes ?? [], candidate.value),
);
const carry = computed(() => data.value?.carry);
const cash = computed(() => carry.value?.cash);
const sourceTime = computed(() => researchTime(data.value?.generated_at));
const freshness = computed(() => {
  if (sourceTime.value === null) return '生成时间未知';
  const age = now.value - sourceTime.value;
  if (age < -60_000) return '生成时间晚于本机时钟，需核对';
  return age > 12 * 3_600_000
    ? '历史快照已超过 12 小时'
    : '历史快照在 12 小时内生成';
});
const matrixRows = computed<ResearchRecord[]>(() => {
  const key = section.value;
  return key === 'models'
    ? (data.value?.models ?? [])
    : key === 'carry'
      ? (cash.value?.yearly ?? [])
      : (carry.value?.[key] ?? []);
});
const columns: Record<
  string,
  {
    key: string;
    label: string;
    digits?: number;
    percent?: boolean;
    signed?: boolean;
  }[]
> = {
  models: [
    { key: 'label', label: '模型' },
    { key: 'trades', label: '交易数', digits: 0 },
    { key: 'profit_pct', label: '历史收益', percent: true, signed: true },
    { key: 'winrate', label: '胜率', percent: true },
    { key: 'max_drawdown_pct', label: '最大回撤', percent: true },
    { key: 'profit_factor', label: '利润因子' },
    { key: 'pred_ic', label: '预测 IC', digits: 4 },
    { key: 'sign_hit_rate', label: '方向命中率', percent: true },
    { key: 'verdict', label: '历史结论' },
  ],
  carry: [
    { key: 'year', label: '年份', digits: 0 },
    { key: 'periods', label: '期数', digits: 0 },
    { key: 'net_pct', label: '区间净收益', percent: true, signed: true },
    { key: 'funding_pct', label: '资金费', percent: true, signed: true },
    { key: 'annualized_pct', label: '折算年化', percent: true, signed: true },
  ],
  liquidation: [
    { key: 'leverage', label: '情景杠杆' },
    { key: 'threshold_pct', label: '强平阈值', percent: true },
    { key: 'liquidated_periods', label: '输入击穿期数', digits: 0 },
    { key: 'violation_rate', label: '情景触发比例', percent: true },
  ],
  sweep: [
    { key: 'rebalance_days', label: '调仓天数', digits: 0 },
    { key: 'topn', label: '币种数', digits: 0 },
    { key: 'leverage', label: '杠杆' },
    { key: 'annual_pct', label: '年化', percent: true, signed: true },
    { key: 'total_pct', label: '累计', percent: true, signed: true },
    { key: 'max_drawdown_pct', label: '回撤', percent: true },
    { key: 'win_rate', label: '按期胜率', percent: true },
  ],
  buffers: [
    { key: 'label', label: '缓冲情景' },
    { key: 'buffer_pct', label: '保证金比例', percent: true },
    { key: 'capital_multiple', label: '资金占用倍数' },
    { key: 'annual_pct', label: '假设年化', percent: true, signed: true },
  ],
  schemes: [
    { key: 'label', label: '保证金方案' },
    { key: 'mode', label: '账户模型' },
    { key: 'leverage', label: '杠杆' },
    { key: 'liquidations', label: '模拟强平次数', digits: 0 },
    { key: 'total_pct', label: '累计收益', percent: true, signed: true },
    { key: 'annual_pct', label: '年化', percent: true, signed: true },
    { key: 'max_drawdown_pct', label: '最大回撤', percent: true },
  ],
};
function cell(
  row: ResearchRecord,
  col: { key: string; digits?: number; percent?: boolean; signed?: boolean },
) {
  const value = row[col.key];
  if (['label', 'mode', 'verdict'].includes(col.key))
    return typeof value === 'string' && value ? value : '未知';
  const result = metricText(value, col.digits ?? 2, col.signed);
  return result === '未知' ? result : result + (col.percent ? '%' : '');
}
function selectCandidate(name: string) {
  void router.replace({ query: { ...route.query, candidate: name } });
}
watch(
  () => route.query.section,
  (value) => {
    if (
      typeof value === 'string' &&
      sections.some((item) => item.key === value)
    )
      section.value = value;
    else section.value = 'routes';
  },
  { immediate: true },
);
function selectSection(key: string) {
  section.value = key;
  void router.replace({ query: { ...route.query, section: key } });
}
let generation = 0;
let disposed = false;
let controller: AbortController | undefined;
async function load() {
  controller?.abort();
  const activeController = new AbortController();
  controller = activeController;
  const current = ++generation;
  refreshing.value = true;
  // 旧 API 不接收 AbortSignal；generation 仍阻止已卸载页面和迟到结果写入。
  const [researchResult, statusResult] = await Promise.allSettled([
    getResearch(),
    getResearchStatus(),
  ]);
  if (disposed || current !== generation || activeController.signal.aborted)
    return;
  if (researchResult.status === 'fulfilled') {
    try {
      data.value = parseResearchSnapshot(researchResult.value);
      loadError.value = '';
    } catch {
      loadError.value = '研究汇总合同不完整，刷新失败。';
    }
  } else loadError.value = '研究汇总读取失败，请重试。';
  if (
    statusResult.status === 'fulfilled' &&
    statusResult.value &&
    typeof statusResult.value === 'object' &&
    !Array.isArray(statusResult.value)
  ) {
    status.value = statusResult.value;
    statusError.value = '';
  } else statusError.value = '刷新任务状态读取失败。';
  refreshing.value = false;
  now.value = Date.now();
}
let timer: ReturnType<typeof setInterval> | undefined;
onMounted(() => {
  void load();
  timer = setInterval(load, 60_000);
});
onUnmounted(() => {
  disposed = true;
  generation++;
  controller?.abort();
  clearInterval(timer);
});
</script>

<template>
  <main class="strategy-research-workspace">
    <header class="research-heading">
      <div>
        <h1>策略研究</h1>
        <p>候选方向、历史模型与 Carry 证据按来源逐项核对。</p>
      </div>
      <button type="button" :aria-busy="refreshing" @click="load">
        {{ refreshing ? '读取中' : '刷新研究证据' }}
      </button>
    </header>
    <div class="research-boundary" role="status">
      <strong>历史研究</strong><span>尚无 Demo 准入裁定</span
      ><span>研究推荐不构成策略上线批准</span>
    </div>
    <p v-if="loadError" role="alert" class="research-error">
      {{ loadError }}
      {{ data ? '保留上次成功快照，请核对生成时间。' : '尚无成功快照。' }}
    </p>
    <p v-if="statusError" role="alert" class="research-error">
      {{ statusError }} 守护状态未知。
    </p>
    <p v-if="invalidSection" role="alert" class="research-error">
      板块定位无效，已显示研究方向；请使用单个有效板块。
    </p>
    <div class="research-source">
      <span>生成于 {{ data?.generated_at || '未知' }}</span
      ><span>{{ freshness }}</span
      ><span
        >最近重建：{{
          statusError
            ? '未知'
            : status?.ok === false
              ? '失败'
              : status?.ok === true
                ? '成功'
                : '未知'
        }}</span
      >
      <span
        >刷新守护：{{
          statusError
            ? '未知'
            : status?.refresh_running === true
              ? '运行中'
              : status?.refresh_running === false
                ? '未检测到'
                : '未知'
        }}</span
      >
      <span>最近尝试 {{ status?.attempt_at || '未知' }}</span>
      <span>任务记录生成于 {{ status?.summary_generated_at || '未知' }}</span>
    </div>
    <nav aria-label="研究板块" class="research-sections">
      <button
        v-for="item in sections"
        :key="item.key"
        type="button"
        :aria-pressed="section === item.key"
        @click="selectSection(item.key)"
      >
        {{ item.label }}
      </button>
    </nav>
    <div class="research-grid">
      <section class="research-ledger" aria-label="研究内容">
        <template v-if="section === 'routes'">
          <div class="section-title">
            <h2>候选方向账本</h2>
            <span>{{ data?.routes.length ?? '未知' }} 条历史方向</span>
          </div>
          <p v-if="!data && refreshing">正在读取研究汇总。</p>
          <p v-else-if="!data">无可读取的研究快照。</p>
          <p v-else-if="!data.routes.length">本次成功快照无研究方向记录。</p>
          <div
            v-else
            class="research-table-scroll"
            tabindex="0"
            aria-label="候选方向表格"
          >
            <table>
              <thead>
                <tr>
                  <th>研究方向</th>
                  <th>历史结论</th>
                  <th>历史收益口径</th>
                  <th>来源证据</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="item in data.routes"
                  :key="item.name"
                  :data-selected="selected?.name === item.name"
                >
                  <th>
                    <button
                      type="button"
                      :aria-pressed="selected?.name === item.name"
                      @click="selectCandidate(item.name)"
                    >
                      {{ item.name }}
                    </button>
                  </th>
                  <td>{{ researchLevel(item.level) }}</td>
                  <td>{{ item.annual ?? '未知' }}</td>
                  <td>{{ item.evidence ?? '未知' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
        <template v-else>
          <div class="section-title">
            <h2>{{ sections.find((item) => item.key === section)?.label }}</h2>
            <span>历史研究档案</span>
          </div>
          <template v-if="section === 'carry'">
            <p>
              以下为历史回测的资金费、基差与成本拆解，尚无实际账户成本核对。
            </p>
            <dl class="carry-breakdown">
              <div>
                <dt>每期资金费</dt>
                <dd>{{ metricText(cash?.funding_pct, 4, true) }}%</dd>
              </div>
              <div>
                <dt>每期基差</dt>
                <dd>{{ metricText(cash?.basis_pct, 4, true) }}%</dd>
              </div>
              <div>
                <dt>每期费用与滑点</dt>
                <dd>{{ metricText(cash?.cost_pct, 4, true) }}%</dd>
              </div>
              <div>
                <dt>每期净收益</dt>
                <dd>{{ metricText(cash?.net_pct, 4, true) }}%</dd>
              </div>
            </dl>
            <p>
              基差均值 {{ metricText(cash?.basis_stats?.mean, 4) }}%，标准差
              {{ metricText(cash?.basis_stats?.std, 4) }}%，95%区间 [{{
                metricText(cash?.basis_stats?.p95_low, 3)
              }}%, {{ metricText(cash?.basis_stats?.p95_high, 3) }}%]。
            </p>
            <p>
              历史平均年化：1x {{ metricText(cash?.backtest_avg_1x) }}%，3x
              {{ metricText(cash?.backtest_avg_3x) }}%；原研究前瞻输入：1x
              {{ metricText(cash?.forward_annual_1x) }}%，3x
              {{
                metricText(cash?.forward_annual_3x)
              }}%。这些输入不代表当前收益预期。
            </p>
          </template>
          <p v-if="section === 'models'">
            IC
            为预测与实际收益的秩相关；缺失值保留未知。历史模型评估不授予当前冻结版本资格。
          </p>
          <p v-if="section === 'liquidation'" class="research-caution">
            击穿期数是原研究情景输入，阈值来自维持保证金模型；它们不是交易所实际强平成交。
          </p>
          <p v-if="section === 'sweep'" class="research-caution">
            参数扫描收益未计入强平，不能用高杠杆年化作为当前逐仓、最高 10x
            政策的准入证据。
          </p>
          <p v-if="section === 'buffers'" class="research-caution">
            保证金缓冲及年化属于情景假设。统一账户与现货抵押模型不符合本轮逐仓协议，保留作历史比较。
          </p>
          <p v-if="section === 'schemes'" class="research-caution">
            逐小时模拟包含模型强平；模拟零强平不证明真实账户保护，也不改变本轮逐仓账户约束。
          </p>
          <p v-if="!data || (section !== 'models' && !carry)">
            {{
              data
                ? 'Carry 证据尚未提供，当前板块不可评估。'
                : '尚无成功研究快照，当前板块不可评估。'
            }}
          </p>
          <p v-else-if="!matrixRows.length">
            当前快照无此板块记录；缺少数据不视为零收益或通过。
          </p>
          <div
            v-else
            class="research-table-scroll"
            tabindex="0"
            aria-label="历史研究表格"
          >
            <table>
              <thead>
                <tr>
                  <th v-for="col in columns[section]" :key="col.key">
                    {{ col.label }}
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(row, index) in matrixRows"
                  :key="row.identifier || index"
                >
                  <td v-for="col in columns[section]" :key="col.key">
                    {{ cell(row, col) }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <details class="research-raw">
            <summary>查看本板块原始研究字段</summary>
            <pre>{{
              JSON.stringify(section === 'carry' ? cash : matrixRows, null, 2)
            }}</pre>
          </details>
        </template>
      </section>
      <aside class="research-inspector" aria-label="所选研究证据">
        <h2>所选方向证据</h2>
        <template v-if="selected"
          ><h3>{{ selected.name }}</h3>
          <p class="historical-verdict">{{ researchLevel(selected.level) }}</p>
          <p>{{ selected.evidence || '历史说明未知' }}</p>
          <dl>
            <div>
              <dt>历史收益口径</dt>
              <dd>{{ selected.annual ?? '未知' }}</dd>
            </div>
            <div>
              <dt>原研究裁定</dt>
              <dd>{{ selected.verdict ?? '未知' }}</dd>
            </div>
          </dl></template
        >
        <p v-else-if="invalidCandidate">
          研究身份无效。请使用单个非空的候选名称，原定位未自动替换。
        </p>
        <p v-else-if="candidate">
          未找到指定研究：{{ candidate }}。保留原定位，不替换为其它方向。
        </p>
        <p v-else>尚无所选研究。</p>
        <h3>准入所需证据</h3>
        <dl class="evidence-gaps">
          <div>
            <dt>冻结策略、代码与数据指纹</dt>
            <dd>未取得</dd>
          </div>
          <div>
            <dt>完整成本与压力场景</dt>
            <dd>未取得</dd>
          </div>
          <div>
            <dt>样本外下界与一次性留出</dt>
            <dd>未取得</dd>
          </div>
          <div>
            <dt>执行与原生保护预检</dt>
            <dd>未取得</dd>
          </div>
          <div>
            <dt>人工上线批准</dt>
            <dd>未取得</dd>
          </div>
        </dl>
        <nav aria-label="相关档案">
          <RouterLink to="/quant/strategy-lab">纸面账户档案</RouterLink
          ><RouterLink to="/quant/experiments">实验任务</RouterLink>
        </nav>
      </aside>
    </div>
  </main>
</template>

<style scoped>
.strategy-research-workspace {
  --research-bg: #0c1b2a;
  --research-panel: #132b3e;
  --research-line: #29485c;
  --research-text: #e5edf4;
  --research-muted: #a2b8c9;
  box-sizing: border-box;
  min-width: 0;
  min-height: 100%;
  padding: 24px;
  background: var(--research-bg);
  color: var(--research-text);
  font-family:
    'IBM Plex Sans', 'Source Han Sans SC', 'Noto Sans CJK SC', sans-serif;
  font-size: 14px;
  line-height: 1.6;
}
.strategy-research-workspace * {
  box-sizing: border-box;
}
.research-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 20px;
}
.research-heading h1 {
  font-size: 28px;
  line-height: 1.25;
  font-weight: 600;
  margin: 0 0 8px;
}
.research-heading p {
  margin: 0;
  color: var(--research-muted);
  max-width: 65ch;
}
button {
  cursor: pointer;
  background: transparent;
  color: inherit;
  border: 1px solid var(--research-line);
  padding: 8px 12px;
  border-radius: 4px;
  font: inherit;
}
button:hover {
  border-color: #69b4e4;
}
button:focus-visible,
a:focus-visible,
summary:focus-visible,
.research-table-scroll:focus-visible {
  outline: 2px solid #69b4e4;
  outline-offset: 3px;
}
.research-heading button {
  white-space: nowrap;
  background: #173b53;
  color: #bde4ff;
}
.research-boundary {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 24px;
  border-block: 1px solid var(--research-line);
  padding: 10px 0;
  color: #f0c17b;
}
.research-boundary strong {
  color: var(--research-text);
}
.research-source {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 24px;
  color: var(--research-muted);
  font-size: 12px;
  padding: 12px 0;
}
.research-error {
  border-left: 3px solid #f19a9a;
  padding: 10px 14px;
  background: #3d2734;
  color: #ffd4d4;
}
.research-sections {
  display: flex;
  flex-wrap: wrap;
  gap: 0;
  border-bottom: 1px solid var(--research-line);
  margin: 10px 0 20px;
}
.research-sections button {
  border: 0;
  border-bottom: 3px solid transparent;
  border-radius: 0;
  padding: 12px 16px;
  color: var(--research-muted);
}
.research-sections button[aria-pressed='true'] {
  border-color: #69b4e4;
  color: #e5edf4;
  background: #183548;
}
.research-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 300px;
  gap: 24px;
  align-items: start;
}
.research-ledger,
.research-inspector {
  min-width: 0;
}
.research-ledger {
  background: var(--research-panel);
  border: 1px solid var(--research-line);
  padding: 20px;
}
.section-title {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 18px;
}
.section-title h2,
.research-inspector h2 {
  margin: 0;
  font-size: 18px;
  font-weight: 500;
}
.section-title span {
  font-size: 12px;
  color: var(--research-muted);
}
.research-table-scroll {
  overflow-x: auto;
  max-width: 100%;
  border-top: 1px solid var(--research-line);
}
table {
  border-collapse: collapse;
  width: 100%;
  font-variant-numeric: tabular-nums;
}
th,
td {
  text-align: left;
  vertical-align: top;
  border-bottom: 1px solid var(--research-line);
  padding: 12px;
  min-width: 92px;
}
thead th {
  color: var(--research-muted);
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
}
tbody th {
  min-width: 150px;
}
tbody th button {
  padding: 0;
  border: 0;
  color: #bde4ff;
  text-align: left;
}
tr[data-selected='true'] {
  background: #1c3c53;
}
tbody td:last-child {
  max-width: 48ch;
  min-width: 160px;
}
.research-inspector {
  padding: 0 0 0 20px;
  border-left: 1px solid var(--research-line);
}
.research-inspector h3 {
  font-size: 15px;
  margin: 24px 0 8px;
}
.research-inspector p {
  overflow-wrap: anywhere;
  color: var(--research-muted);
}
.historical-verdict {
  color: #f0c17b !important;
}
dl {
  margin: 16px 0;
}
dl > div {
  padding: 10px 0;
  border-bottom: 1px solid var(--research-line);
}
dt {
  font-size: 12px;
  color: var(--research-muted);
}
dd {
  margin: 3px 0 0;
  overflow-wrap: anywhere;
}
.evidence-gaps dd {
  color: #f0c17b;
}
.research-inspector nav {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 24px;
}
.research-inspector a {
  color: #8ed0fa;
  text-decoration: underline;
  text-underline-offset: 3px;
}
.research-caution {
  border-left: 2px solid #f0c17b;
  padding-left: 12px;
  color: #f0c17b;
  max-width: 80ch;
}
.carry-breakdown {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}
.carry-breakdown dd {
  font-size: 18px;
}
.research-raw {
  margin-top: 20px;
  color: var(--research-muted);
}
summary {
  cursor: pointer;
}
pre {
  font:
    12px/1.6 'IBM Plex Mono',
    monospace;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 400px;
  overflow: auto;
  border-top: 1px solid var(--research-line);
  padding-top: 12px;
}
@media (max-width: 1100px) {
  .research-grid {
    grid-template-columns: minmax(0, 1fr) 260px;
    gap: 16px;
  }
  .research-ledger {
    padding: 16px;
  }
}
@media (max-width: 900px) {
  .research-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .research-inspector {
    border-left: 0;
    border-top: 1px solid var(--research-line);
    padding: 20px 0;
  }
  .evidence-gaps {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0 20px;
  }
  .research-inspector nav {
    flex-direction: row;
    flex-wrap: wrap;
  }
  .carry-breakdown {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 480px) {
  .strategy-research-workspace {
    padding: 16px;
  }
  .research-heading {
    display: block;
  }
  .research-heading h1 {
    font-size: 24px;
  }
  .research-heading button {
    margin-top: 14px;
  }
  .research-boundary {
    display: grid;
    gap: 4px;
  }
  .research-sections button {
    padding: 10px;
  }
  .research-ledger {
    padding: 12px;
  }
  .evidence-gaps {
    display: block;
  }
  .research-source {
    display: grid;
    gap: 3px;
  }
  .research-grid {
    gap: 20px;
  }
}
</style>

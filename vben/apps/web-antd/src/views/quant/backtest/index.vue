<script lang="ts" setup>
import {
  computed,
  defineAsyncComponent,
  onMounted,
  onUnmounted,
  ref,
} from 'vue';
import {
  EXIT_LABELS,
  FT_SERVER_ORIGIN,
  FT_TOKEN_KEY,
  getBacktestStatus,
  getResearchProgress,
} from '#/api/freqtrade';
const TasksPage = defineAsyncComponent(
  () => import('#/views/quant/tasks/index.vue'),
);
const IterationPage = defineAsyncComponent(
  () => import('#/views/quant/iteration/index.vue'),
);
type Row = Record<string, any>;
type SourceKey = 'file' | 'web' | 'research';
interface Source {
  data: Row | null;
  error: string;
  observed: string;
}
const tab = ref('bt');
const selected = ref<SourceKey>('file');
const file = ref<Source>({ data: null, error: '', observed: '' });
const web = ref<Source>({ data: null, error: '', observed: '' });
const research = ref<Source>({ data: null, error: '', observed: '' });
const fileMissing = ref(false);
const loading = ref(false);
const sources = computed(() => [
  { key: 'file' as const, label: '脚本回测 / 训练', state: file.value },
  ...(fileMissing.value || web.value.data
    ? [{ key: 'web' as const, label: '研究实例回测', state: web.value }]
    : []),
  { key: 'research' as const, label: '模型自动迭代', state: research.value },
]);
const current = computed(
  () =>
    ({ file: file.value, web: web.value, research: research.value })[
      selected.value
    ],
);
const currentData = computed<Row | null>(() =>
  selected.value === 'research'
    ? (current.value.data?.progress ?? null)
    : current.value.data,
);
const currentStatus = computed<Row | null>(() =>
  selected.value === 'research' && current.value.data && !currentData.value
    ? { status: 'idle' }
    : currentData.value,
);
const result = computed<Row | null>(() => currentData.value?.result ?? null);
const errors = computed(() => sources.value.filter((s) => s.state.error));
const labels: Record<string, string> = {
  done: '执行完成',
  failed: '执行失败',
  idle: '当前无运行任务',
  stopped: '已停止',
  running: '运行中',
  stopping: '等待停止确认',
  unknown: '状态未知',
};
const statusText = (data: Row | null) =>
  data
    ? Object.hasOwn(labels, data.status)
      ? labels[data.status]
      : '状态未知'
    : '尚未取得状态';
const redact = (value: string) =>
  value
    .replace(/[a-z]+:[/][/][^ "'<>]+/gi, '[敏感地址已隐藏]')
    .replace(/Bearer[ ]+[^ ]+/gi, '[凭据已隐藏]');
function sanitized(value: unknown, depth = 0): unknown {
  if (depth > 12) return '[内容过深]';
  if (typeof value === 'string') return redact(value);
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (Array.isArray(value))
    return value.map((item) => sanitized(item, depth + 1));
  if (value && typeof value === 'object')
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        redact(key),
        /password|secret|token|api[_-]?key|error|traceback/i.test(key)
          ? '[敏感或错误详情已隐藏]'
          : sanitized(item, depth + 1),
      ]),
    );
  return value;
}
const text = (value: unknown) =>
  value === null ||
  value === undefined ||
  value === '' ||
  (typeof value === 'number' && !Number.isFinite(value))
    ? '未知'
    : typeof value === 'object'
      ? JSON.stringify(sanitized(value))
      : redact(String(value));
const configLabel = (value: Row | null) =>
  value
    ? Object.entries(value)
        .map(([k, v]) => k + '=' + text(v))
        .join(' / ')
    : '未知';
const raw = computed(() =>
  JSON.stringify(sanitized(current.value.data), null, 2),
);
const metrics = [
  ['来源报告利润', 'profit_abs', 'USDT'],
  ['收益率', 'profit_pct', '%'],
  ['期末余额', 'final_balance', ''],
  ['起始余额', 'starting_balance', ''],
  ['胜率', 'winrate', '%'],
  ['盈利笔数', 'wins', ''],
  ['亏损笔数', 'losses', ''],
  ['交易笔数', 'trades', ''],
  ['多头交易', 'trades_long', ''],
  ['空头交易', 'trades_short', ''],
  ['最大回撤', 'max_drawdown_pct', '%'],
  ['Sharpe', 'sharpe', ''],
  ['盈亏比', 'profit_factor', ''],
  ['CAGR', 'cagr', '%'],
  ['日均交易', 'trades_per_day', ''],
] as const;
const pairRows = computed<Row[]>(() =>
  Array.isArray(result.value?.pairs) ? result.value!.pairs : [],
);
const exitRows = computed<Row[]>(() =>
  Array.isArray(result.value?.exits)
    ? result.value!.exits.filter((r: Row) => r.reason !== 'TOTAL')
    : [],
);
const trialRows = computed<Row[]>(() =>
  Array.isArray(research.value.data?.trials?.recent)
    ? research.value.data!.trials.recent
    : [],
);
const hasResearchHistory = computed(
  () =>
    !!research.value.data &&
    (trialRows.value.length > 0 ||
      research.value.data.trials.total > 0 ||
      research.value.data.trials.runs > 0),
);
let active = true;
let generation = 0;
let controller: AbortController | null = null;
let timer: ReturnType<typeof setInterval> | undefined;
let missingChecked = 0;
let sessionIdentity = localStorage.getItem(FT_TOKEN_KEY);
const record = (value: unknown): value is Row =>
  !!value && typeof value === 'object' && !Array.isArray(value);
function progressContract(value: unknown): Row {
  if (!record(value) || typeof value.status !== 'string')
    throw new Error('contract');
  if (
    value.result !== null &&
    value.result !== undefined &&
    !record(value.result)
  )
    throw new Error('contract');
  if (record(value.result))
    for (const key of ['pairs', 'exits']) {
      if (
        value.result[key] !== undefined &&
        (!Array.isArray(value.result[key]) ||
          value.result[key].some((r: unknown) => !record(r)))
      )
        throw new Error('contract');
    }
  return Object.hasOwn(labels, value.status)
    ? value
    : { ...value, status: 'unknown' };
}
function researchContract(value: unknown): Row {
  if (
    !record(value) ||
    !Object.hasOwn(value, 'progress') ||
    (value.progress !== null &&
      (!record(value.progress) || typeof value.progress.status !== 'string')) ||
    !record(value.trials) ||
    !Array.isArray(value.trials.recent) ||
    value.trials.recent.some((r: unknown) => !record(r)) ||
    !Number.isInteger(value.trials.total) ||
    value.trials.total < 0 ||
    !Number.isInteger(value.trials.runs) ||
    value.trials.runs < 0
  )
    throw new Error('contract');
  if (record(value.progress) && !Object.hasOwn(labels, value.progress.status))
    return {
      ...value,
      progress: { ...value.progress, status: 'unknown' },
    };
  return value;
}
function webContract(value: unknown): Row {
  if (
    !record(value) ||
    typeof value.running !== 'boolean' ||
    typeof value.status !== 'string'
  )
    throw new Error('contract');
  const strategyMap = value.backtest_result?.strategy;
  if (strategyMap !== undefined && !record(strategyMap))
    throw new Error('contract');
  const [strategy, m] = Object.entries(strategyMap ?? {})[0] ?? ['', null];
  if (m !== null && !record(m)) throw new Error('contract');
  const n = (v: unknown, factor = 1) =>
    typeof v === 'number' && Number.isFinite(v)
      ? Number((v * factor).toPrecision(14))
      : null;
  const rows = (v: unknown, field: string) => {
    if (v === undefined || v === null) return [];
    if (!Array.isArray(v) || v.some((r) => !record(r)))
      throw new Error('contract');
    return v
      .filter((r) => r.key !== 'TOTAL')
      .map((r) => ({
        [field]: r.key,
        trades: r.trades,
        profit_abs: n(r.profit_total_abs),
        profit_pct: n(r.profit_total, 100),
      }));
  };
  const mtr = m as Row | null;
  const status =
    value.status === 'error'
      ? 'failed'
      : value.status === 'stopping'
        ? 'stopping'
        : value.running
          ? 'running'
          : mtr && value.status === 'ended'
            ? 'done'
            : value.status === 'not_started'
              ? 'idle'
              : 'unknown';
  const phase =
    value.status === 'error'
      ? '研究实例报告执行错误；详细错误已隐藏'
      : value.status === 'stopping'
        ? '已请求停止，等待研究实例确认'
        : value.status_msg === 'Backtest ended'
          ? '研究实例回测已结束'
          : value.status_msg === 'Backtest started'
            ? '研究实例回测运行中'
            : '研究实例状态由服务确认';
  return {
    ...value,
    status,
    phase,
    percent: n(value.progress, 100),
    model: strategy || null,
    result: mtr
      ? {
          ...mtr,
          strategy,
          start: mtr.backtest_start,
          end: mtr.backtest_end,
          days: mtr.backtest_days,
          profit_abs: n(mtr.profit_total_abs),
          profit_pct: n(mtr.profit_total, 100),
          winrate: n(mtr.winrate, 100),
          trades: mtr.total_trades,
          trades_long: mtr.trade_count_long,
          trades_short: mtr.trade_count_short,
          max_drawdown_pct: n(mtr.max_drawdown_account, 100),
          cagr: n(mtr.cagr, 100),
          pairs: rows(mtr.results_per_pair, 'pair'),
          exits: rows(mtr.exit_reason_summary, 'reason'),
        }
      : null,
  };
}
function accepted(target: typeof file, value: Row) {
  target.value = {
    data: value,
    error: '',
    observed: new Date().toLocaleString('zh-CN', { hour12: false }),
  };
}
function failed(target: typeof file, label: string, reason: unknown) {
  target.value.error =
    label +
    (reason instanceof Error && reason.message === 'contract'
      ? '合同不完整'
      : '读取失败') +
    '；' +
    (target.value.data ? '保留上次成功快照' : '尚无成功快照，请刷新重试');
}
async function load(force = false) {
  const token = localStorage.getItem(FT_TOKEN_KEY);
  if (token !== sessionIdentity) {
    sessionIdentity = token;
    for (const target of [file, web, research])
      target.value = { data: null, error: '', observed: '' };
    fileMissing.value = false;
    missingChecked = 0;
    selected.value = 'file';
    force = true;
  }
  if (!force && loading.value) return;
  const id = ++generation;
  controller?.abort();
  controller = new AbortController();
  const signal = controller.signal;
  const valid = () =>
    active && id === generation && localStorage.getItem(FT_TOKEN_KEY) === token;
  loading.value = true;
  await Promise.allSettled([
    (async () => {
      try {
        if (
          !force &&
          fileMissing.value &&
          Date.now() - missingChecked < 30_000
        ) {
          try {
            const value = webContract(await getBacktestStatus());
            if (valid()) accepted(web, value);
          } catch (e) {
            if (valid()) failed(web, '研究实例状态', e);
          }
          return;
        }
        const response = await fetch(
          FT_SERVER_ORIGIN + '/run_progress.json?t=' + Date.now(),
          {
            cache: 'no-store',
            headers: token ? { Authorization: 'Bearer ' + token } : {},
            signal: AbortSignal.any([signal, AbortSignal.timeout(20_000)]),
          },
        );
        if (!valid()) return;
        if (response.status === 404) {
          fileMissing.value = true;
          missingChecked = Date.now();
          file.value.error = '';
          if (!file.value.data && selected.value === 'file')
            selected.value = 'web';
          try {
            const value = webContract(await getBacktestStatus());
            if (valid()) accepted(web, value);
          } catch (e) {
            if (valid()) failed(web, '研究实例状态', e);
          }
          return;
        }
        if (!response.ok) throw new Error('read');
        const value = progressContract(await response.json());
        if (valid()) {
          fileMissing.value = false;
          accepted(file, value);
        }
      } catch (e) {
        if (valid()) failed(file, '脚本进度', e);
      }
    })(),
    (async () => {
      try {
        const value = researchContract(await getResearchProgress());
        if (valid()) accepted(research, value);
      } catch (e) {
        if (valid()) failed(research, '模型迭代', e);
      }
    })(),
  ]);
  if (active && id === generation) loading.value = false;
}
onMounted(() => {
  void load();
  timer = setInterval(() => void load(), 3000);
});
onUnmounted(() => {
  active = false;
  generation++;
  controller?.abort();
  clearInterval(timer);
});
</script>

<template>
  <main class="backtest-workspace">
    <header class="workspace-heading">
      <h1>回测与历史任务</h1>
      <p>
        历史研究环境。执行完成不等于研究通过，成本完整性未知，不构成 Demo
        或实盘验收。
      </p>
    </header>
    <nav class="workspace-tabs" aria-label="历史实验分页">
      <button
        v-for="item in [
          { key: 'bt', label: '回测' },
          { key: 'tasks', label: '研究任务' },
          { key: 'iter', label: '模型迭代' },
        ]"
        :key="item.key"
        :data-tab="item.key"
        :aria-pressed="tab === item.key"
        @click="tab = item.key"
      >
        {{ item.label }}
      </button>
    </nav>
    <template v-if="tab === 'bt'">
      <div class="monitor-tools">
        <span>每 3 秒读取；脚本不存在时查看独立研究实例</span
        ><button data-refresh @click="load(true)">刷新状态</button>
      </div>
      <div
        v-for="item in errors"
        :key="item.key"
        class="monitor-error"
        role="alert"
      >
        {{ item.state.error }}
      </div>
      <div class="monitor-master-detail">
        <aside class="monitor-sources" aria-label="任务来源">
          <button
            v-for="item in sources"
            :key="item.key"
            :data-source="item.key"
            :aria-pressed="selected === item.key"
            @click="selected = item.key"
          >
            <strong>{{ item.label }}</strong
            ><span>{{
              statusText(
                item.key === 'research'
                  ? (item.state.data?.progress ??
                      (item.state.data ? { status: 'idle' } : null))
                  : item.state.data,
              )
            }}</span
            ><small>{{
              item.state.error
                ? '旧快照 / 读取失败'
                : item.state.observed
                  ? '读取于 ' + item.state.observed
                  : '尚未读取'
            }}</small>
          </button>
          <p v-if="fileMissing" class="monitor-boundary">
            已确认无脚本进度文件。研究实例与脚本是不同来源，历史快照分别保留。
          </p>
        </aside>
        <section
          class="monitor-inspector"
          data-inspector
          aria-label="所选任务事实"
        >
          <div class="inspector-title">
            <h2>{{ sources.find((s) => s.key === selected)?.label }}</h2>
            <span>{{ statusText(currentStatus) }}</span>
          </div>
          <p class="monitor-boundary">
            {{
              current.error
                ? '当前展示上次成功事实，读取失败未覆盖记录。'
                : current.observed
                  ? '本次读取时间 ' +
                    current.observed +
                    '；源时间 ' +
                    text(currentData?.updated_at)
                  : '正在读取，未知状态不会显示为待命。'
            }}
          </p>
          <template v-if="currentData">
            <dl class="progress-ledger">
              <div
                v-for="[label, value] in [
                  ['运行身份', currentData.run_id],
                  ['模型 / 策略', currentData.model],
                  ['阶段', currentData.phase],
                  ['进度（%）', currentData.percent],
                  ['当前币对', currentData.current_pair],
                  ['训练 / trial', currentData.trained ?? currentData.trial],
                  ['计划总量', currentData.total],
                  ['轮次', currentData.round],
                  ['速率（每分钟）', currentData.rate_per_min],
                  ['已用秒数', currentData.elapsed_seconds],
                  ['剩余秒数估计', currentData.eta_seconds],
                  ['时间区间', currentData.timerange],
                  [
                    '当前配置',
                    currentData.current
                      ? configLabel(currentData.current)
                      : null,
                  ],
                  [
                    '本轮最优',
                    currentData.best ? JSON.stringify(currentData.best) : null,
                  ],
                ]"
                :key="String(label)"
              >
                <dt>{{ label }}</dt>
                <dd>{{ text(value) }}</dd>
              </div>
            </dl>
            <template v-if="selected === 'research'">
              <p v-if="result" class="monitor-boundary">
                旧判据{{
                  result.passed === true
                    ? '达标'
                    : result.passed === false
                      ? '未达标'
                      : '未知'
                }}；{{ text(result.n_trials) }} 个 trial；版本
                {{ text(result.version_id) }}；报告
                {{ text(result.report) }}。旧判据不构成上线批准。
              </p>
            </template>
            <template v-else-if="result && !result.error">
              <h3>来源报告绩效</h3>
              <p class="monitor-boundary">
                {{ text(result.strategy) }} / {{ text(result.start) }} 至
                {{ text(result.end) }} /
                {{ text(result.days) }} 天；成本完整性未知。
              </p>
              <dl class="metric-ledger">
                <div v-for="[label, key, unit] in metrics" :key="key">
                  <dt>{{ label }}</dt>
                  <dd>
                    {{ text(result[key]) }}
                    {{
                      result[key] === null || result[key] === undefined
                        ? ''
                        : unit
                    }}
                  </dd>
                </div>
              </dl>
              <h3>分币对表现</h3>
              <div class="table-viewport">
                <table>
                  <thead>
                    <tr>
                      <th>交易对</th>
                      <th>交易</th>
                      <th>报告利润</th>
                      <th>收益率（%）</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(row, i) in pairRows" :key="i">
                      <td>{{ text(row.pair) }}</td>
                      <td>{{ text(row.trades) }}</td>
                      <td>{{ text(row.profit_abs) }}</td>
                      <td>{{ text(row.profit_pct) }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <p v-if="!pairRows.length">来源未提供分币对明细。</p>
              <h3>平仓原因贡献</h3>
              <div class="table-viewport">
                <table>
                  <thead>
                    <tr>
                      <th>原因</th>
                      <th>笔数</th>
                      <th>报告利润</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(row, i) in exitRows" :key="i">
                      <td>{{ EXIT_LABELS[row.reason] || text(row.reason) }}</td>
                      <td>{{ text(row.trades) }}</td>
                      <td>{{ text(row.profit_abs) }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </template>
            <p v-else-if="result?.error" role="alert">
              来源报告任务失败。请查看可信本机任务日志，错误详情不在页面回显。
            </p>
          </template>
          <template v-if="selected === 'research' && current.data">
            <p v-if="!currentData" class="monitor-boundary">
              {{
                hasResearchHistory
                  ? '暂无当前运行进度；历史 trial 记录仍在下方展示。'
                  : '当前没有已记录的模型迭代任务。'
              }}
            </p>
            <h3>最近 trial（包含失败）</h3>
            <div class="table-viewport">
              <table>
                <thead>
                  <tr>
                    <th>配置</th>
                    <th>逐窗口 t</th>
                    <th>IC</th>
                    <th>正窗口</th>
                    <th>阶段</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(row, i) in trialRows" :key="i">
                    <td>{{ configLabel(row.config) }}</td>
                    <td>{{ text(row.t_period) }}</td>
                    <td>{{ text(row.ic_period) }}</td>
                    <td>
                      {{ text(row.pos_windows) }} / {{ text(row.n_windows) }}
                    </td>
                    <td>{{ text(row.phase) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p v-if="!trialRows.length">尚无最近 trial。</p>
            <p class="monitor-boundary">
              账本累计 {{ text(research.data?.trials.total) }} 条 trial /
              {{ text(research.data?.trials.runs) }} 轮。
            </p>
          </template>
          <p v-if="!currentData && !(selected === 'research' && current.data)">
            {{
              current.error ? '该来源暂不可读取。' : '尚无该来源的任务事实。'
            }}
          </p>
          <details v-if="current.data">
            <summary>脱敏来源 JSON</summary>
            <pre>{{ raw }}</pre>
          </details>
        </section>
      </div>
    </template>
    <TasksPage v-else-if="tab === 'tasks'" />
    <IterationPage v-else-if="tab === 'iter'" />
  </main>
</template>

<style scoped>
.backtest-workspace {
  min-width: 0;
  max-width: 100%;
  padding: 24px;
  color: #e5edf4;
  background: #0c1b2a;
  font-family:
    'IBM Plex Sans', 'Source Han Sans SC', 'Noto Sans SC', sans-serif;
  font-size: 14px;
}
.workspace-heading h1 {
  margin: 0;
  font-size: 24px;
  font-weight: 600;
}
.workspace-heading p,
.monitor-boundary {
  max-width: 78ch;
  color: #a2b8c9;
  font-size: 12px;
  line-height: 1.6;
}
.workspace-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 20px 0;
  border-bottom: 1px solid #29485c;
}
.workspace-tabs button {
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  padding: 10px 16px;
  color: #a2b8c9;
}
.workspace-tabs button[aria-pressed='true'] {
  color: #e5edf4;
  border-bottom-color: #69b4e4;
}
.monitor-tools {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 12px;
  align-items: center;
  color: #a2b8c9;
  font-size: 12px;
  margin-bottom: 16px;
}
.monitor-tools button {
  border: 1px solid #29485c;
  background: #132b3e;
  padding: 8px 14px;
  color: #e5edf4;
}
.monitor-error {
  margin: 12px 0;
  padding: 12px;
  border-left: 3px solid #f0c17b;
  background: #132b3e;
  color: #f0c17b;
}
.monitor-master-detail {
  display: grid;
  grid-template-columns: minmax(200px, 280px) minmax(0, 1fr);
  border: 1px solid #29485c;
}
.monitor-sources {
  min-width: 0;
  border-right: 1px solid #29485c;
  padding: 12px;
}
.monitor-sources button {
  display: grid;
  gap: 8px;
  width: 100%;
  text-align: left;
  border: 1px solid transparent;
  border-left: 3px solid transparent;
  background: transparent;
  padding: 16px 12px;
  color: #e5edf4;
}
.monitor-sources button[aria-pressed='true'] {
  border-color: #29485c;
  border-left-color: #69b4e4;
  background: #132b3e;
}
.monitor-sources small,
.monitor-sources span {
  color: #a2b8c9;
  font-size: 12px;
}
.monitor-inspector {
  min-width: 0;
  padding: 20px;
  background: #132b3e;
}
.inspector-title {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.inspector-title h2 {
  margin: 0;
  font-size: 18px;
}
.inspector-title span {
  color: #69b4e4;
}
.progress-ledger,
.metric-ledger {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin: 16px 0;
  border-top: 1px solid #29485c;
}
.progress-ledger > div,
.metric-ledger > div {
  min-width: 0;
  padding: 12px 8px;
  border-bottom: 1px solid #29485c;
}
.progress-ledger dt,
.metric-ledger dt {
  font-size: 12px;
  color: #a2b8c9;
}
.progress-ledger dd,
.metric-ledger dd {
  margin: 6px 0 0;
  overflow-wrap: anywhere;
}
.monitor-inspector h3 {
  font-size: 14px;
  font-weight: 600;
  margin: 24px 0 12px;
}
.table-viewport {
  max-width: 100%;
  overflow-x: auto;
  border: 1px solid #29485c;
}
table {
  width: 100%;
  border-collapse: collapse;
  white-space: nowrap;
}
th,
td {
  text-align: left;
  padding: 10px 12px;
  border-bottom: 1px solid #29485c;
  font-size: 12px;
}
th {
  color: #a2b8c9;
  font-weight: 500;
}
details {
  margin-top: 24px;
}
summary {
  cursor: pointer;
  color: #69b4e4;
}
pre {
  max-height: 480px;
  overflow: auto;
  font-size: 12px;
  padding: 12px;
  border: 1px solid #29485c;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
button {
  cursor: pointer;
}
button:focus-visible,
summary:focus-visible {
  outline: 2px solid #69b4e4;
  outline-offset: 3px;
}
@media (max-width: 900px) {
  .backtest-workspace {
    padding: 16px;
  }
  .monitor-master-detail {
    grid-template-columns: minmax(0, 1fr);
  }
  .monitor-sources {
    display: flex;
    flex-wrap: wrap;
    border-right: 0;
    border-bottom: 1px solid #29485c;
  }
  .monitor-sources button {
    width: auto;
    flex: 1;
    min-width: 150px;
  }
  .monitor-sources p {
    width: 100%;
  }
  .progress-ledger,
  .metric-ledger {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .monitor-inspector {
    padding: 16px;
  }
}
@media (max-width: 420px) {
  .backtest-workspace {
    padding: 12px;
  }
  .workspace-heading h1 {
    font-size: 22px;
  }
  .progress-ledger,
  .metric-ledger {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>

<script lang="ts" setup>
import { sortCoins } from '../utils/coinOrder';
/**
 * 研究任务 —— 在浏览器里发起回测 / 下载数据 / 前瞻分析 / 递归分析，并查看进度
 *
 * 为什么单独做一页：
 *   Freqtrade 把这些端点挂在 `is_webserver_mode` 依赖下，实盘 bot（runmode=trade）
 *   访问会直接 503。项目因此另起了一个只做研究、不下单的 `freqtrade webserver`
 *   实例（8891），前端经认证服务 8890 的 `/api/web/` 前缀访问它 —— 也就是
 *   `#/api/freqtrade` 里 `/web/v1/*` 那一组封装，本页只调用它们，不碰实盘接口。
 *
 * 轮询约定（本页最容易出问题的地方，全部走下面的 timers 登记表）：
 *   - 回测     `getBacktestStatus()`       仅在 `running === true` 时每秒轮询，结束即停
 *   - 后台任务 `getBackgroundJob(job_id)`  每秒轮询，直到 `running === false`
 *   任何一个定时器在轮询结束 / 出错时立刻 clearInterval，onUnmounted 再兜底清一次，
 *   不会留下每秒打接口的空转定时器。
 */
import { computed, onMounted, onUnmounted, reactive, ref, type Ref } from 'vue';

import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  CollapsePanel,
  Input,
  InputNumber,
  message,
  Modal,
  Progress,
  RadioButton,
  RadioGroup,
  Row,
  Select,
  Switch,
  TabPane,
  Table,
  Tabs,
  Tag,
  Textarea,
} from 'ant-design-vue';

import {
  abortBacktest,
  deleteBacktestHistory,
  errText,
  getBacktestHistory,
  getBacktestStatus,
  getBackgroundJob,
  getLookaheadResult,
  getRecursiveResult,
  getWebAvailablePairs,
  getWebStrategies,
  getWhitelist,
  postBacktest,
  postDownloadData,
  postLookaheadAnalysis,
  postRecursiveAnalysis,
  resetBacktest,
  setBacktestNotes,
  type FtBacktestHistoryEntry,
  type FtBacktestStatus,
  type FtBackgroundJob,
} from '#/api/freqtrade';

/* ══════════════════ 通用工具 ══════════════════ */

/** 数字输入框清空后是 null，统一压成 undefined，让 payload 里直接省略该字段 */
function optNum(v: any): number | undefined {
  return typeof v === 'number' && Number.isFinite(v) ? v : undefined;
}

/**
 * 进度换算：Freqtrade 的 progress 是 0–1 的比率。
 * 防御性地兼容「已经乘过 100」的返回值（> 1 时按已是百分比处理），并夹到 0–100。
 */
function toPercent(v: any): number {
  if (typeof v !== 'number' || !Number.isFinite(v)) return 0;
  const pct = v > 1 ? v : v * 100;
  return Number(Math.min(100, Math.max(0, pct)).toFixed(1));
}

/** 表格 / 卡片里的空值文案 */
function valText(v: any): string {
  if (v === null || v === undefined || v === '') return '—';
  if (typeof v === 'boolean') return v ? '是' : '否';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/** 折叠区里的完整 JSON：序列化失败也不能把页面搞崩 */
function jsonText(v: any): string {
  if (v === null || v === undefined) return '';
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return '（结果无法序列化为 JSON）';
  }
}

/** 秒级时间戳 → 本地时间；`backtest_start_time` 是秒，必须 ×1000 */
function fmtSec(v: any): string {
  const n = typeof v === 'number' ? v : Number(v);
  if (!Number.isFinite(n) || n <= 0) return '—';
  return new Date(n * 1000).toLocaleString('zh-CN', { hour12: false });
}

/* ══════════════════ 定时器登记表 ══════════════════ */

/**
 * 所有轮询定时器都登记在这里，key 唯一。
 * stopTimer 是唯一的停止入口，保证「结束即清、不重复、不留空转」。
 */
const timers: Record<string, any> = {};

function stopTimer(key: string) {
  if (timers[key]) {
    clearInterval(timers[key]);
    timers[key] = null;
  }
}

onUnmounted(() => {
  Object.keys(timers).forEach((k) => stopTimer(k));
});

/* ══════════════════ ① 顶部说明条 / 可读错误 ══════════════════ */

const bannerError = ref('');

/** 首次加载失败（例如认证服务连不上 8891）时，把可读文案落到顶部说明条 */
function reportBannerError(e: any) {
  if (!bannerError.value) bannerError.value = errText(e);
}

/* ══════════════════ 公共下拉选项 ══════════════════ */

const TIMEFRAME_OPTIONS = ['1m', '5m', '15m', '30m', '1h', '4h', '1d'].map(
  (v) => ({ label: v, value: v }),
);

const CACHE_OPTIONS = [
  { label: '不使用缓存（none）', value: 'none' },
  { label: '按天缓存（day）', value: 'day' },
  { label: '按周缓存（week）', value: 'week' },
  { label: '按月缓存（month）', value: 'month' },
];

const TRADING_MODE_OPTIONS = [
  { label: '留空（不指定）', value: '' },
  { label: 'spot（现货）', value: 'spot' },
  { label: 'futures（合约）', value: 'futures' },
];

/** 研究实例（8891）上可用的策略，回测与两个分析共用 */
const strategies = ref<string[]>([]);
const strategyOptions = computed(() =>
  strategies.value.map((s) => ({ label: s, value: s })),
);

/* ══════════════════ ② 回测 ══════════════════ */

const btForm = reactive<any>({
  strategy: '',
  timeframe: '1d',
  timeframe_detail: '',
  timerange: '',
  max_open_trades: undefined,
  dry_run_wallet: undefined,
  enable_protections: false,
  backtest_cache: 'none',
});

const btStatus = ref<FtBacktestStatus | null>(null);
const btSubmitting = ref(false);
const btRunning = computed(() => btStatus.value?.running === true);

/** 只读的首次状态：如果 8891 上本来就有一个回测在跑，进页面就能接着看进度 */
async function loadBacktestStatus() {
  try {
    btStatus.value = await getBacktestStatus();
    if (btStatus.value?.running) startBacktestPoll();
  } catch (e) {
    reportBannerError(e);
  }
}

async function tickBacktest() {
  try {
    const st = await getBacktestStatus();
    btStatus.value = st;
    // 运行结束（成功或失败）立刻停表，绝不留每秒空转
    if (!st.running) stopTimer('backtest');
  } catch (e) {
    stopTimer('backtest');
    message.error(errText(e));
  }
}

function startBacktestPoll() {
  stopTimer('backtest');
  timers.backtest = setInterval(() => void tickBacktest(), 1000);
}

async function startBacktest() {
  if (!btForm.strategy) {
    message.warning('请先选择策略');
    return;
  }
  btSubmitting.value = true;
  try {
    const st = await postBacktest({
      strategy: btForm.strategy,
      timeframe: btForm.timeframe || undefined,
      timeframe_detail: btForm.timeframe_detail || undefined,
      timerange: btForm.timerange || undefined,
      max_open_trades: optNum(btForm.max_open_trades),
      dry_run_wallet: optNum(btForm.dry_run_wallet),
      enable_protections: btForm.enable_protections === true,
      backtest_cache: btForm.backtest_cache || undefined,
    });
    btStatus.value = st;
    message.success('回测已提交，运行期间进度每秒刷新');
    if (st?.running) startBacktestPoll();
    else stopTimer('backtest');
  } catch (e) {
    message.error(errText(e));
  } finally {
    btSubmitting.value = false;
  }
}

function confirmAbortBacktest() {
  Modal.confirm({
    title: '中止回测',
    content: '将中断正在运行的回测任务，当前这一轮已算出的结果不会保留。确定中止？',
    okText: '确定中止',
    cancelText: '取消',
    okButtonProps: { danger: true },
    onOk: async () => {
      try {
        btStatus.value = await abortBacktest();
        stopTimer('backtest');
        message.success('已请求中止回测');
      } catch (e) {
        message.error(errText(e));
      }
    },
  });
}

function confirmResetBacktest() {
  Modal.confirm({
    title: '清空回测结果',
    content:
      '将清空研究实例内存中保存的回测结果，页面上的 KPI 与 JSON 明细也会一起消失（磁盘上的历史文件不受影响）。确定清空？',
    okText: '确定清空',
    cancelText: '取消',
    okButtonProps: { danger: true },
    onOk: async () => {
      try {
        btStatus.value = await resetBacktest();
        stopTimer('backtest');
        message.success('已清空回测结果');
      } catch (e) {
        message.error(errText(e));
      }
    },
  });
}

/**
 * Freqtrade 的结果结构是 `backtest_result.strategy.<策略名>.<指标>`，
 * 策略名事先不知道，所以探测 `strategy` 下的第一个键；取不到就当作「无结果明细」。
 */
const btStrategyMetrics = computed<any>(() => {
  const raw: any = btStatus.value?.backtest_result;
  const strat = raw?.strategy;
  if (!strat || typeof strat !== 'object') return null;
  const key = Object.keys(strat)[0];
  if (!key) return null;
  const metrics = strat[key];
  return metrics && typeof metrics === 'object' ? metrics : null;
});

const NUM = (v: any, digits = 2): string =>
  typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : '—';

/**
 * 比率 → 百分比。Freqtrade 的 profit_total / winrate / max_drawdown_account
 * 都是 0–1 的比率（不是已经乘过 100 的百分数），这里统一 ×100。
 */
const RATIO = (v: any, digits = 2): string =>
  typeof v === 'number' && Number.isFinite(v) ? `${(v * 100).toFixed(digits)}%` : '—';

/** 涨红跌绿（与终端其它页面一致） */
function trendCls(v: any): string {
  if (typeof v !== 'number' || !Number.isFinite(v) || v === 0) return '';
  return v > 0 ? 'text-red-500' : 'text-emerald-500';
}

const btKpis = computed(() => {
  const m = btStrategyMetrics.value;
  if (!m) return [];
  return [
    { label: '总交易数', value: NUM(m.total_trades, 0), unit: '笔', cls: '' },
    {
      label: '净利润',
      value: NUM(m.profit_total_abs, 2),
      unit: 'USDT',
      cls: trendCls(m.profit_total_abs),
    },
    { label: '收益率', value: RATIO(m.profit_total), unit: '', cls: trendCls(m.profit_total) },
    { label: '胜率', value: RATIO(m.winrate), unit: '', cls: '' },
    {
      label: '最大回撤',
      value: RATIO(m.max_drawdown_account),
      unit: '',
      cls: 'text-emerald-500',
    },
    { label: 'Sharpe', value: NUM(m.sharpe, 2), unit: '', cls: trendCls(m.sharpe) },
    { label: 'Sortino', value: NUM(m.sortino, 2), unit: '', cls: trendCls(m.sortino) },
    { label: 'Calmar', value: NUM(m.calmar, 2), unit: '', cls: trendCls(m.calmar) },
    { label: '盈亏比', value: NUM(m.profit_factor, 2), unit: '', cls: '' },
  ];
});

const btStateText = computed(() => {
  const st = btStatus.value;
  if (!st) return '暂无状态';
  if (st.running) return '运行中';
  return st.status_msg || st.status || '已结束';
});

const btStateColor = computed(() => {
  const st = btStatus.value;
  if (!st) return 'default';
  if (st.running) return 'processing';
  return btStrategyMetrics.value ? 'success' : 'default';
});

const btProgressStatus = computed<'active' | 'exception' | 'normal' | 'success'>(() => {
  if (btRunning.value) return 'active';
  return btStrategyMetrics.value ? 'success' : 'normal';
});

const btRawJson = computed(() => jsonText(btStatus.value?.backtest_result));

/* ══════════════════ ③ 回测历史 ══════════════════ */

const history = ref<FtBacktestHistoryEntry[]>([]);
const historyLoading = ref(false);

const histColumns: any[] = [
  { dataIndex: 'filename', key: 'filename', title: '文件名', ellipsis: true },
  { dataIndex: 'strategy', key: 'strategy', title: '策略', width: 200 },
  { key: 'timeframe', title: '周期', width: 130 },
  { key: 'time', title: '运行时间', width: 180 },
  { dataIndex: 'notes', key: 'notes', title: '备注' },
  { key: 'action', title: '操作', width: 170 },
];

/** silent = true 时（进页面自动加载）失败只写顶部说明条，避免一进页面弹一堆 toast */
async function loadHistory(silent = false) {
  historyLoading.value = true;
  try {
    history.value = await getBacktestHistory();
  } catch (e) {
    if (silent) reportBannerError(e);
    else message.error(errText(e));
  } finally {
    historyLoading.value = false;
  }
}

const notesOpen = ref(false);
const notesSaving = ref(false);
const notesFilename = ref('');
/** 后端 PATCH 要求必须带 strategy（只发 notes 会 422），所以要把行上的策略一起存下来 */
const notesStrategy = ref('');
const notesValue = ref('');

function openNotes(rec: any) {
  notesFilename.value = String(rec?.filename ?? '');
  notesStrategy.value = String(rec?.strategy ?? '');
  notesValue.value = String(rec?.notes ?? '');
  notesOpen.value = true;
}

async function saveNotes() {
  if (!notesFilename.value) return;
  notesSaving.value = true;
  try {
    history.value = await setBacktestNotes(
      notesFilename.value,
      notesStrategy.value,
      notesValue.value,
    );
    notesOpen.value = false;
    message.success('备注已保存');
  } catch (e) {
    message.error(errText(e));
  } finally {
    notesSaving.value = false;
  }
}

function confirmDeleteHistory(rec: any) {
  const filename = String(rec?.filename ?? '');
  if (!filename) return;
  Modal.confirm({
    title: '删除历史回测结果',
    content: `将删除磁盘上的回测结果文件 ${filename}，文件里的全部交易明细与指标都无法恢复。确定删除？`,
    okText: '确定删除文件',
    cancelText: '取消',
    okButtonProps: { danger: true },
    onOk: async () => {
      try {
        history.value = await deleteBacktestHistory(filename);
        message.success('已删除该回测结果文件');
      } catch (e) {
        message.error(errText(e));
      }
    },
  });
}

/* ══════════════════ ④ 下载数据 ══════════════════ */

const pairOptions = ref<Array<{ label: string; value: string }>>([]);
const dlMode = ref('list');
const dlPairs = ref<string[]>([]);
const orderedDlPairs = computed({
  get: () => sortCoins(dlPairs.value),
  set: (pairs: string[]) => {
    dlPairs.value = sortCoins(pairs);
  },
});
const dlPairsText = ref('');
const dlTimeframes = ref<string[]>(['5m', '1h']);
const dlRangeMode = ref('days');
const dlDays = ref<number | undefined>(30);
const dlTimerange = ref('');
const dlErase = ref(false);
const dlPrepend = ref(false);
const dlTrades = ref(false);
const dlTradingMode = ref('');
const dlJob = ref<FtBackgroundJob | null>(null);
const dlSubmitting = ref(false);

async function loadPairs() {
  try {
    const r = await getWebAvailablePairs();
    pairOptions.value = sortCoins(r?.pairs ?? []).map((p) => ({ label: p, value: p }));
  } catch (e) {
    reportBannerError(e);
  }
}

/** 运行期白名单预填前 5 个当作初始币种，不要一次塞 20 个 */
async function loadWhitelist() {
  try {
    const r = await getWhitelist();
    const list = r?.whitelist ?? [];
    if (dlPairs.value.length === 0) dlPairs.value = sortCoins(list).slice(0, 5);
  } catch (e) {
    reportBannerError(e);
  }
}

/** 「最近 N 天」与「自定义 timerange」互斥：切换时清空另一个 */
function onRangeModeChange() {
  if (dlRangeMode.value === 'days') {
    dlTimerange.value = '';
  } else {
    dlDays.value = undefined;
  }
}

/** 列表选择 / 手工输入 二选一，最终都要变成 string[] */
function resolvedPairs(): string[] {
  if (dlMode.value === 'list') {
    return sortCoins(dlPairs.value.map((p) => p.trim()).filter(Boolean));
  }
  return sortCoins(dlPairsText.value
    .split(/[\s,;，、]+/)
    .map((p) => p.trim())
    .filter(Boolean));
}

function startDownload() {
  const pairs = resolvedPairs();
  if (pairs.length === 0) {
    message.warning('请至少选择一个或填写一个币种');
    return;
  }
  if (dlRangeMode.value === 'timerange' && !dlTimerange.value.trim()) {
    message.warning('自定义 timerange 不能为空，或改回「最近 N 天」');
    return;
  }
  if (dlErase.value) {
    const preview = `${pairs.slice(0, 5).join('、')}${pairs.length > 5 ? ` 等 ${pairs.length} 个币种` : ''}`;
    Modal.confirm({
      title: '抹掉已有数据（危险）',
      content: `erase 会先删除 ${preview} 在本地已下载的历史数据，再重新下载，耗时可能很久且期间无法用于回测。确定继续？`,
      okText: '确定抹掉并下载',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: () => doDownload(pairs),
    });
    return;
  }
  void doDownload(pairs);
}

async function doDownload(pairs: string[]) {
  dlSubmitting.value = true;
  try {
    const r = await postDownloadData({
      pairs,
      timeframes: dlTimeframes.value,
      days: dlRangeMode.value === 'days' ? optNum(dlDays.value) : undefined,
      timerange: dlRangeMode.value === 'timerange' ? dlTimerange.value.trim() : undefined,
      erase: dlErase.value === true,
      prepend_data: dlPrepend.value === true,
      download_trades: dlTrades.value === true,
      trading_mode: dlTradingMode.value || undefined,
    });
    dlJob.value = { job_id: r.job_id, running: true, status: r.status };
    message.success('下载任务已提交，进度每秒刷新');
    watchJob('download', r.job_id, dlJob, async () => {
      message.success('数据下载完成');
    });
  } catch (e) {
    message.error(errText(e));
  } finally {
    dlSubmitting.value = false;
  }
}

/* ══════════════════ ⑤ 分析（前瞻 / 递归） ══════════════════ */

/** 任务轮询：下载 / 前瞻 / 递归共用。tab 用字符串比较，避免和 Tabs 的 Key 类型纠缠 */
const anaTab = ref('lookahead');

function onAnaTabChange(k: any) {
  anaTab.value = String(k ?? 'lookahead');
}

/**
 * 轮询一个后台任务，直到 `running === false`。
 * - 结束 / 出错立刻 clearInterval，不留每秒空转的定时器
 * - onDone 只在任务成功结束时调用，用来拉取最终结果
 */
function watchJob(
  key: string,
  jobId: string,
  state: Ref<FtBackgroundJob | null>,
  onDone: (id: string) => Promise<void>,
) {
  stopTimer(key);
  const tick = async () => {
    try {
      const job = await getBackgroundJob(jobId);
      state.value = job;
      if (job.running) return;
      stopTimer(key);
      if (job.error) {
        message.error(`任务失败：${job.error}`);
        return;
      }
      await onDone(jobId);
    } catch (e) {
      stopTimer(key);
      message.error(errText(e));
    }
  };
  timers[key] = setInterval(() => void tick(), 1000);
  void tick();
}

const laForm = reactive<any>({
  strategy: '',
  timeframe: '1d',
  timerange: '',
  minimum_trade_amount: 10,
  targeted_trade_amount: 20,
  lookahead_allow_limit_orders: false,
});
const laJob = ref<FtBackgroundJob | null>(null);
const laResult = ref<any>(null);
const laSubmitting = ref(false);

const rcForm = reactive<any>({
  strategy: '',
  timeframe: '1d',
  timerange: '',
  startup_candle: '',
});
const rcJob = ref<FtBackgroundJob | null>(null);
const rcResult = ref<any>(null);
const rcSubmitting = ref(false);

/** 当前 Tab 的任务 —— 两个分析共用这一套「job 状态 + 进度 + 错误」展示 */
const curJob = computed(() => (anaTab.value === 'lookahead' ? laJob.value : rcJob.value));
const curJobPercent = computed(() => toPercent(curJob.value?.progress));
const curJobText = computed(() => {
  const j = curJob.value;
  if (!j) return '';
  if (j.error) return '失败';
  return j.running ? '运行中' : j.status || '已结束';
});
const curJobColor = computed(() => {
  const j = curJob.value;
  if (!j) return 'default';
  if (j.error) return 'error';
  return j.running ? 'processing' : 'success';
});

async function submitLookahead() {
  if (!laForm.strategy) {
    message.warning('请先选择策略');
    return;
  }
  laSubmitting.value = true;
  laResult.value = null;
  try {
    const r = await postLookaheadAnalysis({
      strategy: laForm.strategy,
      timeframe: laForm.timeframe || undefined,
      timerange: laForm.timerange || undefined,
      minimum_trade_amount: optNum(laForm.minimum_trade_amount),
      targeted_trade_amount: optNum(laForm.targeted_trade_amount),
      lookahead_allow_limit_orders: laForm.lookahead_allow_limit_orders === true,
    });
    laJob.value = { job_id: r.job_id, running: true, status: r.status };
    message.success('前瞻分析已提交');
    watchJob('lookahead', r.job_id, laJob, async (id) => {
      laResult.value = await getLookaheadResult(id);
      message.success('前瞻分析完成');
    });
  } catch (e) {
    message.error(errText(e));
  } finally {
    laSubmitting.value = false;
  }
}

/** "10, 20, 40" → [10, 20, 40]；留空或全非法则返回 undefined，让后端走默认 */
function parseStartupCandle(text: any): number[] | undefined {
  const nums = String(text ?? '')
    .split(/[\s,;，、]+/)
    .map((s) => Number(s))
    .filter((n) => Number.isFinite(n) && n > 0);
  return nums.length > 0 ? nums : undefined;
}

async function submitRecursive() {
  if (!rcForm.strategy) {
    message.warning('请先选择策略');
    return;
  }
  rcSubmitting.value = true;
  rcResult.value = null;
  try {
    const r = await postRecursiveAnalysis({
      strategy: rcForm.strategy,
      timeframe: rcForm.timeframe || undefined,
      timerange: rcForm.timerange || undefined,
      startup_candle: parseStartupCandle(rcForm.startup_candle),
    });
    rcJob.value = { job_id: r.job_id, running: true, status: r.status };
    message.success('递归分析已提交');
    watchJob('recursive', r.job_id, rcJob, async (id) => {
      rcResult.value = await getRecursiveResult(id);
      message.success('递归分析完成');
    });
  } catch (e) {
    message.error(errText(e));
  } finally {
    rcSubmitting.value = false;
  }
}

/**
 * 前瞻分析结果：可能直接返回指标对象，也可能按策略名再包一层，
 * 这里递归探测出真正带 has_bias / total_signals 的那一层。
 */
function pickBiasResult(raw: any): any {
  if (Array.isArray(raw)) return raw.length > 0 ? pickBiasResult(raw[0]) : null;
  if (!raw || typeof raw !== 'object') return null;
  if ('has_bias' in raw || 'total_signals' in raw) return raw;
  for (const k of Object.keys(raw)) {
    const hit = pickBiasResult(raw[k]);
    if (hit) return hit;
  }
  return null;
}

const laSummary = computed<any>(() => pickBiasResult(laResult.value));
const laHasBias = computed(() => laSummary.value?.has_bias === true);
const laBiasText = computed(() => {
  const v = laSummary.value?.has_bias;
  if (v === true) return '存在前视偏差（has_bias = true）';
  if (v === false) return '未发现前视偏差（has_bias = false）';
  return '未返回 has_bias';
});
const laCounts = computed(() => {
  const s = laSummary.value;
  if (!s) return [];
  return [
    { label: '信号总数', value: valText(s.total_signals) },
    { label: '入场信号有偏', value: valText(s.biased_entry_signals) },
    { label: '出场信号有偏', value: valText(s.biased_exit_signals) },
  ];
});

/** 指标可能包在 `strategy.<策略名>` 下 */
function unwrapMetrics(v: any): any {
  if (
    v &&
    typeof v === 'object' &&
    !Array.isArray(v) &&
    v.strategy &&
    typeof v.strategy === 'object'
  ) {
    const k = Object.keys(v.strategy)[0];
    if (k) {
      const inner = v.strategy[k];
      if (inner && typeof inner === 'object') return inner;
    }
  }
  return v;
}

/** 只保留能铺进单元格的标量字段，避免把大对象塞进表格 */
function scalarFields(v: any): Record<string, any> {
  const out: Record<string, any> = {};
  if (!v || typeof v !== 'object') return out;
  Object.keys(v).forEach((k) => {
    const val = v[k];
    if (val === null || typeof val !== 'object') out[k] = val;
  });
  return out;
}

/**
 * 递归分析结果是「不同 startup_candle 数 → 各项指标」的对比。
 * Freqtrade 各版本包装方式不一样（直接一层对象 / 包 result / 包策略名），
 * 这里都兼容；实在铺不成表格时退化成原始 JSON。
 */
const rcRows = computed<any[]>(() => {
  const raw = rcResult.value;
  if (!raw || typeof raw !== 'object') return [];
  let src: any = raw;
  if (src.result && typeof src.result === 'object') src = src.result;
  if (Array.isArray(src)) {
    return src.map((item: any, i: number) => {
      const fields = scalarFields(unwrapMetrics(item));
      return { key: fields.startup_candle ?? i + 1, ...fields };
    });
  }
  if (typeof src === 'object') {
    const keys = Object.keys(src);
    const allScalar = keys.every((k) => src[k] === null || typeof src[k] !== 'object');
    if (allScalar) return [];
    return keys.map((k) => ({ key: k, ...scalarFields(unwrapMetrics(src[k])) }));
  }
  return [];
});

const rcColumns = computed<any[]>(() => {
  const keys = new Set<string>();
  rcRows.value.forEach((row) => {
    Object.keys(row).forEach((k) => {
      if (k !== 'key') keys.add(k);
    });
  });
  return [
    { dataIndex: 'key', key: 'key', title: '起始 K 线数', width: 120 },
    ...Array.from(keys).map((k) => ({
      dataIndex: k,
      key: k,
      title: k,
      ellipsis: true,
    })),
  ];
});

const rcRecommendText = computed(() => {
  const raw: any = rcResult.value;
  if (!raw || typeof raw !== 'object') return '—';
  const v = raw.recommended_startup_candle ?? raw.recommended ?? raw.startup_candle ?? null;
  if (v === null || v === undefined) return '未返回显式推荐值';
  if (Array.isArray(v)) {
    return v.length > 0
      ? `已测试 ${v.length} 个起始 K 线数：${v.join('、')}`
      : '已测试 0 个起始 K 线数';
  }
  return valText(v);
});

/* ══════════════════ 首次加载 ══════════════════ */

/** 策略列表是三个表单的共同来源，加载完顺手把空着的策略选上第一个 */
async function loadStrategies() {
  try {
    const r = await getWebStrategies();
    strategies.value = Array.isArray(r?.strategies) ? r.strategies : [];
    const first = strategies.value[0] ?? '';
    if (first) {
      if (!btForm.strategy) btForm.strategy = first;
      if (!laForm.strategy) laForm.strategy = first;
      if (!rcForm.strategy) rcForm.strategy = first;
    }
  } catch (e) {
    reportBannerError(e);
  }
}

onMounted(() => {
  void loadStrategies();
  void loadPairs();
  void loadWhitelist();
  void loadHistory(true);
  void loadBacktestStatus();
});
</script>

<template>
  <div class="p-4">
    <!-- ① 说明条 / 可读错误 -->
    <Alert :type="bannerError ? 'error' : 'info'" show-icon>
      <template #message>
        {{
          bannerError
            ? '研究实例（Freqtrade webserver 8891）不可用'
            : '本页的任务跑在独立的研究实例（Freqtrade webserver）上'
        }}
      </template>
      <template #description>
        <div v-if="bannerError" class="text-xs">
          {{ bannerError }}
          <div class="mt-1 text-gray-400">
            上方下拉与历史列表可能为空；确认认证服务与 8891 上的研究实例已启动后刷新重试。
          </div>
        </div>
        <div v-else class="text-xs">
          这里只做回测 / 下载数据 / 前瞻分析 / 递归分析，<b>不会下单</b>，不影响正在运行的实盘机器人。
          任务进度每秒刷新一次，运行结束后自动停止轮询。
        </div>
      </template>
    </Alert>

    <!-- ② 回测 -->
    <Card :bordered="false" class="mt-3 shadow-sm" title="回测">
      <template #extra>
        <Tag :color="btStateColor">{{ btStateText }}</Tag>
      </template>

      <Row :gutter="[12, 12]">
        <Col :lg="6" :md="8" :sm="12" :xs="24">
          <div class="mb-1 text-xs text-gray-400">策略 *</div>
          <Select
            v-model:value="btForm.strategy"
            :options="strategyOptions"
            class="w-full"
            option-filter-prop="label"
            placeholder="选择研究实例上的策略"
            show-search
          />
        </Col>
        <Col :lg="6" :md="8" :sm="12" :xs="24">
          <div class="mb-1 text-xs text-gray-400">周期 timeframe</div>
          <Select v-model:value="btForm.timeframe" :options="TIMEFRAME_OPTIONS" class="w-full" />
        </Col>
        <Col :lg="6" :md="8" :sm="12" :xs="24">
          <div class="mb-1 text-xs text-gray-400">明细周期 timeframe_detail</div>
          <Select
            v-model:value="btForm.timeframe_detail"
            :allow-clear="true"
            :options="TIMEFRAME_OPTIONS"
            class="w-full"
            placeholder="可选，如 5m"
          />
        </Col>
        <Col :lg="6" :md="8" :sm="12" :xs="24">
          <div class="mb-1 text-xs text-gray-400">时间段 timerange</div>
          <Input v-model:value="btForm.timerange" placeholder="20250101-20251001" />
        </Col>
        <Col :lg="6" :md="8" :sm="12" :xs="24">
          <div class="mb-1 text-xs text-gray-400">最大持仓 max_open_trades</div>
          <InputNumber
            v-model:value="btForm.max_open_trades"
            :min="1"
            class="w-full"
            placeholder="留空用配置默认值"
          />
        </Col>
        <Col :lg="6" :md="8" :sm="12" :xs="24">
          <div class="mb-1 text-xs text-gray-400">起始资金 dry_run_wallet</div>
          <InputNumber
            v-model:value="btForm.dry_run_wallet"
            :min="1"
            class="w-full"
            placeholder="留空用配置默认值"
          />
        </Col>
        <Col :lg="6" :md="8" :sm="12" :xs="24">
          <div class="mb-1 text-xs text-gray-400">启用 protections</div>
          <Switch v-model:checked="btForm.enable_protections" />
          <span class="ml-2 text-xs text-gray-400">默认关闭</span>
        </Col>
        <Col :lg="6" :md="8" :sm="12" :xs="24">
          <div class="mb-1 text-xs text-gray-400">回测缓存 backtest_cache</div>
          <Select v-model:value="btForm.backtest_cache" :options="CACHE_OPTIONS" class="w-full" />
        </Col>
      </Row>

      <div class="mt-3 flex flex-wrap items-center gap-2">
        <Button type="primary" :loading="btSubmitting" @click="startBacktest">开始回测</Button>
        <Button danger :disabled="!btRunning" @click="confirmAbortBacktest">中止</Button>
        <Button danger @click="confirmResetBacktest">清空结果</Button>
        <span class="text-xs text-gray-400">
          {{ btRunning ? '运行中，每秒轮询进度' : '未在轮询（仅运行中才每秒请求）' }}
        </span>
      </div>

      <!-- 进度 -->
      <div v-if="btStatus" class="mt-3 rounded border border-gray-100 p-3 dark:border-gray-700">
        <Progress :percent="toPercent(btStatus.progress)" :status="btProgressStatus" />
        <Row :gutter="[12, 12]" class="mt-2">
          <Col :lg="6" :sm="12" :xs="24">
            <div class="text-xs text-gray-400">状态</div>
            <div class="text-sm font-semibold">{{ btStatus.status_msg || '—' }}</div>
          </Col>
          <Col :lg="6" :sm="12" :xs="24">
            <div class="text-xs text-gray-400">步骤 step</div>
            <div class="text-sm font-semibold">{{ btStatus.step || '—' }}</div>
          </Col>
          <Col :lg="6" :sm="12" :xs="24">
            <div class="text-xs text-gray-400">已回测交易 trade_count</div>
            <div class="text-sm font-semibold">{{ btStatus.trade_count ?? '—' }}</div>
          </Col>
          <Col :lg="6" :sm="12" :xs="24">
            <div class="text-xs text-gray-400">轮询</div>
            <div class="text-sm font-semibold">{{ btRunning ? '每秒刷新中' : '已停止' }}</div>
          </Col>
        </Row>
      </div>
      <div v-else class="mt-3 py-6 text-center text-xs text-gray-400">
        暂无回测状态（进页面读取一次；只有 running = true 才会开始轮询）
      </div>

      <!-- KPI -->
      <template v-if="btStrategyMetrics">
        <Row :gutter="[12, 12]" class="mt-3">
          <Col v-for="k in btKpis" :key="k.label" :lg="4" :md="6" :sm="8" :xs="12">
            <div class="rounded bg-gray-50 p-2 dark:bg-gray-800">
              <div class="text-xs text-gray-400">{{ k.label }}</div>
              <div class="mt-1 text-base font-semibold" :class="k.cls">
                {{ k.value }}
                <span v-if="k.unit" class="text-xs font-normal text-gray-400">{{ k.unit }}</span>
              </div>
            </div>
          </Col>
        </Row>
      </template>
      <div v-else-if="btStatus && !btRunning" class="mt-3 text-xs text-gray-400">
        无结果明细（未回测 / 已清空 / 结果结构里没有 backtest_result.strategy）
      </div>

      <!-- 完整 JSON：默认收起，避免大对象把页面撑爆 -->
      <Collapse class="mt-3">
        <CollapsePanel key="raw" header="完整回测 JSON（可能很大，默认收起）">
          <pre
            v-if="btRawJson"
            class="max-h-96 overflow-auto rounded bg-gray-50 p-2 text-xs whitespace-pre-wrap dark:bg-gray-900"
          >{{ btRawJson }}</pre>
          <div v-else class="py-2 text-xs text-gray-400">无结果明细</div>
        </CollapsePanel>
      </Collapse>
    </Card>

    <!-- ③ 回测历史 -->
    <Card :bordered="false" class="mt-3 shadow-sm" title="回测历史">
      <template #extra>
        <span class="mr-2 text-xs text-gray-400">磁盘上的 backtest_results/*.json</span>
        <Button size="small" :loading="historyLoading" @click="loadHistory()">刷新</Button>
      </template>
      <Table
        :columns="histColumns"
        :data-source="history"
        :loading="historyLoading"
        :pagination="{ pageSize: 10, showSizeChanger: false }"
        :scroll="{ x: 900 }"
        row-key="filename"
        size="small"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'timeframe'">
            <span class="font-mono text-xs">
              {{ record.timeframe || '—' }}
              <template v-if="record.timeframe_detail">（{{ record.timeframe_detail }}）</template>
            </span>
          </template>
          <template v-else-if="column.key === 'time'">
            <span class="text-xs">{{ fmtSec(record.backtest_start_time) }}</span>
          </template>
          <template v-else-if="column.key === 'notes'">
            <span v-if="record.notes" class="text-xs">{{ record.notes }}</span>
            <span v-else class="text-xs text-gray-400">—</span>
          </template>
          <template v-else-if="column.key === 'action'">
            <Button size="small" type="link" @click="openNotes(record)">编辑备注</Button>
            <Button size="small" danger type="link" @click="confirmDeleteHistory(record)">
              删除
            </Button>
          </template>
        </template>
        <template #emptyText>
          <div class="py-6 text-xs text-gray-400">暂无历史回测结果</div>
        </template>
      </Table>
    </Card>

    <!-- ④ 下载数据 -->
    <Card :bordered="false" class="mt-3 shadow-sm" title="下载数据">
      <Row :gutter="[12, 12]">
        <Col :lg="12" :xs="24">
          <div class="mb-1 text-xs text-gray-400">币种来源（二选一）</div>
          <RadioGroup v-model:value="dlMode" button-style="solid" size="small">
            <RadioButton value="list">从交易对列表选择</RadioButton>
            <RadioButton value="manual">手工输入</RadioButton>
          </RadioGroup>
        </Col>
        <Col :lg="12" :xs="24">
          <div class="mb-1 text-xs text-gray-400">周期 timeframe（默认 5m + 1h）</div>
          <Select
            v-model:value="dlTimeframes"
            :options="TIMEFRAME_OPTIONS"
            class="w-full"
            mode="multiple"
            placeholder="选择要下载的周期"
          />
        </Col>
      </Row>

      <div class="mt-3">
        <div class="mb-1 text-xs text-gray-400">
          币种（当前 {{ resolvedPairs().length }} 个；初始值取运行期白名单前 5 个）
        </div>
        <Select
          v-if="dlMode === 'list'"
          v-model:value="orderedDlPairs"
          :max-tag-count="6"
          :options="pairOptions"
          class="w-full"
          mode="multiple"
          option-filter-prop="label"
          placeholder="搜索并选择交易对"
          show-search
        />
        <Textarea
          v-else
          v-model:value="dlPairsText"
          :rows="3"
          placeholder="每行一个或用逗号分隔，例如：BTC/USDT, ETH/USDT"
        />
      </div>

      <div class="mt-3 flex flex-wrap items-end gap-3">
        <div>
          <div class="mb-1 text-xs text-gray-400">时间范围（二者互斥，切换会清空另一个）</div>
          <RadioGroup
            v-model:value="dlRangeMode"
            button-style="solid"
            size="small"
            @change="onRangeModeChange"
          >
            <RadioButton value="days">最近 N 天</RadioButton>
            <RadioButton value="timerange">自定义 timerange</RadioButton>
          </RadioGroup>
        </div>
        <div v-if="dlRangeMode === 'days'" style="width: 180px">
          <div class="mb-1 text-xs text-gray-400">天数 days</div>
          <InputNumber v-model:value="dlDays" :min="1" class="w-full" placeholder="30" />
        </div>
        <div v-else style="width: 280px">
          <div class="mb-1 text-xs text-gray-400">timerange</div>
          <Input v-model:value="dlTimerange" placeholder="20250101-20251001" />
        </div>
      </div>

      <Collapse class="mt-3">
        <CollapsePanel key="adv" header="高级选项（erase / prepend_data / download_trades / trading_mode）">
          <div class="flex flex-wrap items-center gap-6">
            <div class="flex items-center gap-2">
              <Switch v-model:checked="dlErase" />
              <span class="text-xs">
                erase —— <span class="text-red-500">抹掉已有本地数据（危险）</span>，提交前会再确认一次
              </span>
            </div>
            <div class="flex items-center gap-2">
              <Switch v-model:checked="dlPrepend" />
              <span class="text-xs">prepend_data —— 向已有数据之前补充更早的 K 线</span>
            </div>
            <div class="flex items-center gap-2">
              <Switch v-model:checked="dlTrades" />
              <span class="text-xs">download_trades —— 同时下载逐笔成交</span>
            </div>
            <div class="flex items-center gap-2">
              <span class="text-xs text-gray-400">trading_mode</span>
              <Select
                v-model:value="dlTradingMode"
                :options="TRADING_MODE_OPTIONS"
                style="width: 180px"
              />
            </div>
          </div>
        </CollapsePanel>
      </Collapse>

      <div class="mt-3">
        <Button type="primary" :loading="dlSubmitting" @click="startDownload">开始下载</Button>
      </div>

      <!-- 下载任务：job 状态 + 进度 + 错误 -->
      <div v-if="dlJob" class="mt-3 rounded border border-gray-100 p-3 dark:border-gray-700">
        <div class="flex items-center justify-between">
          <span class="text-xs text-gray-400">任务 {{ dlJob.job_id }}</span>
          <Tag :color="dlJob.error ? 'error' : dlJob.running ? 'processing' : 'success'">
            {{ dlJob.error ? '失败' : dlJob.running ? '运行中' : dlJob.status || '已结束' }}
          </Tag>
        </div>
        <Progress
          class="mt-2"
          :percent="toPercent(dlJob.progress)"
          :status="dlJob.error ? 'exception' : dlJob.running ? 'active' : 'success'"
        />
        <div v-if="dlJob.error" class="text-xs text-red-500">{{ dlJob.error }}</div>
      </div>
    </Card>

    <!-- ⑤ 分析：前瞻 / 递归 -->
    <Card :bordered="false" class="mt-3 shadow-sm" title="分析">
      <Tabs :active-key="anaTab" size="small" @change="onAnaTabChange">
        <TabPane key="lookahead" tab="前瞻分析（Lookahead）">
          <Row :gutter="[12, 12]">
            <Col :lg="6" :md="8" :sm="12" :xs="24">
              <div class="mb-1 text-xs text-gray-400">策略 *</div>
              <Select
                v-model:value="laForm.strategy"
                :options="strategyOptions"
                class="w-full"
                option-filter-prop="label"
                placeholder="选择策略"
                show-search
              />
            </Col>
            <Col :lg="6" :md="8" :sm="12" :xs="24">
              <div class="mb-1 text-xs text-gray-400">周期 timeframe</div>
              <Select
                v-model:value="laForm.timeframe"
                :options="TIMEFRAME_OPTIONS"
                class="w-full"
              />
            </Col>
            <Col :lg="6" :md="8" :sm="12" :xs="24">
              <div class="mb-1 text-xs text-gray-400">时间段 timerange</div>
              <Input v-model:value="laForm.timerange" placeholder="留空则由后端决定" />
            </Col>
            <Col :lg="6" :md="8" :sm="12" :xs="24">
              <div class="mb-1 text-xs text-gray-400">minimum_trade_amount</div>
              <InputNumber
                v-model:value="laForm.minimum_trade_amount"
                :min="1"
                class="w-full"
              />
            </Col>
            <Col :lg="6" :md="8" :sm="12" :xs="24">
              <div class="mb-1 text-xs text-gray-400">targeted_trade_amount</div>
              <InputNumber
                v-model:value="laForm.targeted_trade_amount"
                :min="1"
                class="w-full"
              />
            </Col>
            <Col :lg="6" :md="8" :sm="12" :xs="24">
              <div class="mb-1 text-xs text-gray-400">允许限价单</div>
              <Switch v-model:checked="laForm.lookahead_allow_limit_orders" />
              <span class="ml-2 text-xs text-gray-400">默认关闭</span>
            </Col>
          </Row>
          <div class="mt-3">
            <Button type="primary" :loading="laSubmitting" @click="submitLookahead">
              开始前瞻分析
            </Button>
          </div>
        </TabPane>

        <TabPane key="recursive" tab="递归分析（Recursive）">
          <Row :gutter="[12, 12]">
            <Col :lg="6" :md="8" :sm="12" :xs="24">
              <div class="mb-1 text-xs text-gray-400">策略 *</div>
              <Select
                v-model:value="rcForm.strategy"
                :options="strategyOptions"
                class="w-full"
                option-filter-prop="label"
                placeholder="选择策略"
                show-search
              />
            </Col>
            <Col :lg="6" :md="8" :sm="12" :xs="24">
              <div class="mb-1 text-xs text-gray-400">周期 timeframe</div>
              <Select
                v-model:value="rcForm.timeframe"
                :options="TIMEFRAME_OPTIONS"
                class="w-full"
              />
            </Col>
            <Col :lg="6" :md="8" :sm="12" :xs="24">
              <div class="mb-1 text-xs text-gray-400">时间段 timerange</div>
              <Input v-model:value="rcForm.timerange" placeholder="留空则由后端决定" />
            </Col>
            <Col :lg="6" :md="8" :sm="12" :xs="24">
              <div class="mb-1 text-xs text-gray-400">startup_candle（逗号分隔）</div>
              <Input v-model:value="rcForm.startup_candle" placeholder="留空走默认，例如 10,20,40" />
            </Col>
          </Row>
          <div class="mt-3">
            <Button type="primary" :loading="rcSubmitting" @click="submitRecursive">
              开始递归分析
            </Button>
          </div>
        </TabPane>
      </Tabs>

      <!-- 两个分析共用同一套任务轮询展示 -->
      <div v-if="curJob" class="mt-3 rounded border border-gray-100 p-3 dark:border-gray-700">
        <div class="flex items-center justify-between">
          <span class="text-xs text-gray-400">
            当前 Tab 的任务 · {{ curJob.job_id }}
            <template v-if="curJob.job_category"> · {{ curJob.job_category }}</template>
          </span>
          <Tag :color="curJobColor">{{ curJobText }}</Tag>
        </div>
        <Progress
          class="mt-2"
          :percent="curJobPercent"
          :status="curJob.error ? 'exception' : curJob.running ? 'active' : 'success'"
        />
        <div v-if="curJob.error" class="text-xs text-red-500">{{ curJob.error }}</div>
        <div v-else class="text-xs text-gray-400">
          前瞻与递归共用这套任务展示；切换 Tab 看到的是各自的任务状态。
        </div>
      </div>
      <div v-else class="mt-3 text-xs text-gray-400">当前 Tab 暂无分析任务</div>

      <!-- 前瞻结果 -->
      <div
        v-if="anaTab === 'lookahead' && laResult"
        class="mt-3 rounded border border-gray-100 p-3 dark:border-gray-700"
      >
        <div v-if="laSummary" class="flex flex-wrap items-center gap-3">
          <Tag :color="laHasBias ? 'error' : 'success'">{{ laBiasText }}</Tag>
          <span v-for="c in laCounts" :key="c.label" class="text-xs text-gray-500">
            {{ c.label }}：<b>{{ c.value }}</b>
          </span>
        </div>
        <div v-else class="text-xs text-gray-400">
          结果里没有 has_bias / total_signals 字段（结构可能随 Freqtrade 版本变化），请看下方原始 JSON。
        </div>
        <Collapse class="mt-2">
          <CollapsePanel key="raw" header="完整分析 JSON">
            <pre
              class="max-h-96 overflow-auto rounded bg-gray-50 p-2 text-xs whitespace-pre-wrap dark:bg-gray-900"
            >{{ jsonText(laResult) }}</pre>
          </CollapsePanel>
        </Collapse>
      </div>

      <!-- 递归结果 -->
      <div
        v-if="anaTab === 'recursive' && rcResult"
        class="mt-3 rounded border border-gray-100 p-3 dark:border-gray-700"
      >
        <div class="flex flex-wrap items-center gap-3">
          <span class="text-xs text-gray-500">
            推荐 startup_candle：<b>{{ rcRecommendText }}</b>
          </span>
          <span class="text-xs text-gray-400">共 {{ rcRows.length }} 组结果</span>
        </div>
        <Table
          v-if="rcRows.length > 0"
          class="mt-2"
          :columns="rcColumns"
          :data-source="rcRows"
          :pagination="false"
          :scroll="{ x: 'max-content' }"
          row-key="key"
          size="small"
        />
        <div v-else class="mt-2 text-xs text-gray-400">
          结果里没有可铺成表格的标量指标，请看下方原始 JSON。
        </div>
        <Collapse class="mt-2">
          <CollapsePanel key="raw" header="完整分析 JSON">
            <pre
              class="max-h-96 overflow-auto rounded bg-gray-50 p-2 text-xs whitespace-pre-wrap dark:bg-gray-900"
            >{{ jsonText(rcResult) }}</pre>
          </CollapsePanel>
        </Collapse>
      </div>
    </Card>

    <!-- 备注弹窗 -->
    <Modal
      v-model:open="notesOpen"
      :confirm-loading="notesSaving"
      cancel-text="取消"
      ok-text="保存备注"
      title="编辑回测备注"
      @ok="saveNotes"
    >
      <div class="mb-1 break-all text-xs text-gray-400">文件：{{ notesFilename }}</div>
      <div class="mb-2 text-xs text-gray-400">
        策略：{{ notesStrategy || '—' }}
        <span class="ml-1">（保存备注时后端要求一并提交策略名，来自这条历史记录）</span>
      </div>
      <Textarea
        v-model:value="notesValue"
        :rows="4"
        placeholder="写下这次回测的结论 / 用途，方便以后在历史列表里筛选"
      />
    </Modal>
  </div>
</template>

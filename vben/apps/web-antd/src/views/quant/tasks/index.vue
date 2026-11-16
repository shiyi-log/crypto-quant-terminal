<script lang="ts" setup>
import type {
  FtBacktestHistoryEntry,
  FtBacktestRequest,
  FtLookaheadRequest,
} from '#/api/freqtrade';

import { computed, onMounted, onUnmounted, reactive, ref } from 'vue';

import {
  Alert,
  Button,
  Input,
  InputNumber,
  Modal,
  Select,
  Switch,
  Textarea,
} from 'ant-design-vue';

import {
  abortBacktest,
  getBackgroundJob,
  getBacktestHistory,
  getBacktestStatus,
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
} from '#/api/freqtrade';

import {
  type CommandKind,
  createTaskQueue,
  metricText,
  UNKNOWN_RETRY_ACK,
} from './taskQueue';

const tools = [
  { key: 'backtest', label: '回测' },
  { key: 'download', label: '数据下载' },
  { key: 'lookahead', label: '前瞻分析' },
  { key: 'recursive', label: '递归分析' },
] as const;
const queue = createTaskQueue({
  getBacktestStatus,
  getBacktestHistory,
  getWebStrategies,
  getWebAvailablePairs,
  getWhitelist,
  postBacktest,
  abortBacktest,
  resetBacktest,
  postDownloadData,
  postLookaheadAnalysis,
  postRecursiveAnalysis,
  getBackgroundJob,
  getLookaheadResult,
  getRecursiveResult,
  setBacktestNotes,
});
const state = queue.state;
type ToolKey = (typeof tools)[number]['key'];
const activeTool = ref<ToolKey>('backtest');
const selected = ref<FtBacktestHistoryEntry | null>(null);
const notes = ref('');
const eraseConfirm = ref(false);
const pendingDownload = ref<any>(null);
const unknownConfirmKind = ref<CommandKind | null>(null);
const recoveryReviewed = ref(false);
const recoveryPhrase = ref('');
let timer: null | ReturnType<typeof setInterval> = null;
const bt = reactive<FtBacktestRequest>({
  strategy: '',
  timeframe: '1d',
  enable_protections: false,
  backtest_cache: 'none',
});
const dl = reactive({
  mode: 'list',
  pairs: [] as string[],
  manual: '',
  timeframes: ['5m', '1h'],
  rangeMode: 'days',
  days: 30 as number | undefined,
  timerange: '',
  erase: false,
  prepend_data: false,
  download_trades: false,
  trading_mode: '',
});
const la = reactive<FtLookaheadRequest>({
  strategy: '',
  timeframe: '1d',
  minimum_trade_amount: 10,
  targeted_trade_amount: 20,
  lookahead_allow_limit_orders: false,
});
const rc = reactive({
  strategy: '',
  timeframe: '1d',
  timerange: '',
  startup_text: '',
});
const selectedMetrics = computed(() => {
  const strategies = (state.backtest?.backtest_result as any)?.strategy;
  if (!strategies || typeof strategies !== 'object') return null;
  const name = Object.keys(strategies)[0];
  return name ? strategies[name] : null;
});
function choose(row: FtBacktestHistoryEntry) {
  selected.value = row;
  notes.value = String(row.notes ?? '');
}
async function refresh() {
  await queue.refresh();
  if (!selected.value && state.history.length) choose(state.history[0]!);
  if (
    selected.value &&
    !state.history.some(
      (row) =>
        row.filename === selected.value?.filename &&
        row.strategy === selected.value?.strategy,
    )
  )
    selected.value = null;
}
async function startBacktest() {
  if (!bt.strategy) {
    state.errors.command = '请先选择策略。';
    return;
  }
  state.errors.command = '';
  await queue.startBacktest(bt);
}
function abort() {
  Modal.confirm({
    title: '请求中止回测',
    content: '响应仅确认收到命令；之后仍需核对回测状态。',
    okText: '请求中止',
    cancelText: '返回',
    okButtonProps: { danger: true },
    onOk: () => queue.abort(),
  });
}
function reset() {
  Modal.confirm({
    title: '清空研究实例内存结果',
    content: '只清理当前内存中的回测结果，不删除磁盘历史产物。',
    okText: '清空内存结果',
    cancelText: '返回',
    okButtonProps: { danger: true },
    onOk: () => queue.reset(),
  });
}
function openUnknownConfirmation(kind: CommandKind) {
  unknownConfirmKind.value = kind;
  recoveryReviewed.value = false;
  recoveryPhrase.value = '';
}
async function confirmUnknown() {
  const kind = unknownConfirmKind.value;
  if (!kind) return;
  await queue.confirmUnknown(kind, {
    reviewed: recoveryReviewed.value,
    phrase: recoveryPhrase.value,
  });
  if (!state.commandsUnknown[kind]) unknownConfirmKind.value = null;
}
function submitDownload() {
  const pairs =
    dl.mode === 'list'
      ? [...dl.pairs]
      : dl.manual
          .split(/[\s,;，、]+/)
          .map((v) => v.trim())
          .filter(Boolean);
  if (!pairs.length) {
    state.errors.download = '至少选择或填写一个交易对。';
    return;
  }
  if (dl.rangeMode === 'timerange' && !dl.timerange.trim()) {
    state.errors.download = '自定义时间范围不能为空。';
    return;
  }
  pendingDownload.value = {
    pairs,
    timeframes: [...dl.timeframes],
    days: dl.rangeMode === 'days' ? dl.days : undefined,
    timerange: dl.rangeMode === 'timerange' ? dl.timerange.trim() : undefined,
    erase: dl.erase,
    prepend_data: dl.prepend_data,
    download_trades: dl.download_trades,
    trading_mode: dl.trading_mode || undefined,
  };
  if (dl.erase) eraseConfirm.value = true;
  else void queue.startJob('download', pendingDownload.value);
}
function confirmErase() {
  eraseConfirm.value = false;
  const payload = pendingDownload.value;
  pendingDownload.value = null;
  if (payload) void queue.startJob('download', payload);
}
function submitLookahead() {
  if (la.strategy) void queue.startJob('lookahead', { ...la });
  else state.errors.lookahead = '请先选择策略。';
}
function submitRecursive() {
  if (!rc.strategy) {
    state.errors.recursive = '请先选择策略。';
    return;
  }
  const startup = rc.startup_text
    .split(/[\s,;，、]+/)
    .map(Number)
    .filter((v) => Number.isFinite(v) && v > 0);
  void queue.startJob('recursive', {
    strategy: rc.strategy,
    timeframe: rc.timeframe,
    timerange: rc.timerange || undefined,
    startup_candle: startup.length ? startup : undefined,
  });
}
async function saveNotes() {
  if (selected.value) await queue.saveNotes(selected.value, notes.value);
}
function fmtSec(value: number) {
  return Number.isFinite(value) && value > 0
    ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false })
    : '时间未知';
}
onMounted(() => {
  void refresh();
  timer = setInterval(() => {
    if (queue.needsPoll()) void queue.poll();
  }, 1000);
});
onUnmounted(() => {
  if (timer) clearInterval(timer);
  queue.dispose();
});
</script>

<template>
  <main class="task-workspace">
    <header class="task-heading">
      <div>
        <h1>研究任务队列</h1>
        <p>研究实例的执行状态与实验判定分开记录。</p>
      </div>
      <Button
        data-testid="refresh-tasks"
        :loading="state.loading"
        @click="refresh"
        >刷新记录</Button
      >
    </header>
    <Alert
      v-if="Object.values(state.errors).some(Boolean)"
      type="warning"
      show-icon
      ><template #message>部分任务事实暂不可用</template
      ><template #description
        ><span v-for="(error, key) in state.errors" :key="key">{{
          error
        }}</span></template
      ></Alert
    >
    <section
      v-if="Object.values(state.commandsUnknown).some(Boolean)"
      class="unknown-commands"
    >
      <strong>有命令回执未知，重复提交已锁定</strong>
      <p>刷新不会解除锁定。先在研究实例后台核对原请求是否创建任务。</p>
      <template v-for="(locked, kind) in state.commandsUnknown" :key="kind"
        ><div v-if="locked" class="unknown-command">
          <span>{{ kind }}：结果未知</span
          ><Button
            :data-unknown-kind="kind"
            @click="openUnknownConfirmation(kind as CommandKind)"
            >核对后解锁</Button
          >
        </div></template
      >
    </section>
    <div class="task-layout">
      <nav class="task-nav" aria-label="研究工具">
        <button
          v-for="tool in tools"
          :key="tool.key"
          type="button"
          :aria-pressed="activeTool === tool.key"
          :class="{ active: activeTool === tool.key }"
          :data-tool="tool.key"
          @click="activeTool = tool.key"
        >
          {{ tool.label }}
        </button>
      </nav>
      <section class="task-primary">
        <div class="queue-summary">
          <div>
            <strong>{{
              state.backtest?.status === 'stopping'
                ? '等待回测停止'
                : state.backtest?.running
                  ? '回测运行中'
                  : state.backtest
                    ? '回测已结束'
                    : '回测状态未知'
            }}</strong
            ><span>研究实例 · 执行事实</span>
          </div>
          <div v-if="selectedMetrics" class="summary-metrics">
            <span
              >观察收益
              <b>{{ metricText(selectedMetrics.profit_total, true) }}</b></span
            ><span
              >交易笔数
              <b>{{ metricText(selectedMetrics.total_trades) }}</b></span
            ><span>成本完整度 <b>未知</b></span>
          </div>
          <div class="queue-actions">
            <Button
              v-if="state.backtest?.running && activeTool === 'backtest'"
              danger
              :disabled="state.busy.backtest || state.commandsUnknown.backtest"
              @click="abort"
              >请求中止</Button
            ><Button
              v-if="state.backtest && activeTool === 'backtest'"
              :disabled="state.busy.backtest || state.commandsUnknown.backtest"
              @click="reset"
              >清空内存结果</Button
            >
          </div>
        </div>
        <section v-if="activeTool === 'backtest'" class="command-panel">
          <h2>提交回测</h2>
          <p>回测执行状态不构成研究通过。</p>
          <div class="form-grid">
            <label
              >策略<Select
                name="bt-strategy"
                v-model:value="bt.strategy"
                :options="state.strategies.map((v) => ({ label: v, value: v }))"
                show-search
                placeholder="选择研究实例策略" /></label
            ><label
              >主周期<Select
                v-model:value="bt.timeframe"
                :options="
                  ['1m', '5m', '15m', '30m', '1h', '4h', '1d'].map((v) => ({
                    label: v,
                    value: v,
                  }))
                " /></label
            ><label
              >明细周期<Select
                name="bt-detail"
                v-model:value="bt.timeframe_detail"
                allow-clear
                :options="
                  ['1m', '5m', '15m', '30m', '1h', '4h', '1d'].map((v) => ({
                    label: v,
                    value: v,
                  }))
                " /></label
            ><label
              >时间范围<Input
                name="bt-range"
                v-model:value="bt.timerange"
                placeholder="20250101-20251001" /></label
            ><label
              >最大持仓<InputNumber
                v-model:value="bt.max_open_trades"
                :min="1" /></label
            ><label
              >起始资金<InputNumber
                name="bt-wallet"
                v-model:value="bt.dry_run_wallet"
                :min="1" /></label
            ><label
              >缓存<Select
                v-model:value="bt.backtest_cache"
                :options="
                  ['none', 'day', 'week', 'month'].map((v) => ({
                    label: v,
                    value: v,
                  }))
                " /></label
            ><label class="switch-field"
              >启用 protections <Switch v-model:checked="bt.enable_protections"
            /></label>
          </div>
          <Button
            data-submit-backtest
            type="primary"
            :loading="state.busy.backtest"
            :disabled="
              state.backtest?.running || state.commandsUnknown.backtest
            "
            @click="startBacktest"
            >开始回测</Button
          >
          <p v-if="state.errors.command" class="inline-error" role="alert">
            {{ state.errors.command }}
          </p>
          <p data-command-outcome class="command-outcome">
            {{ state.outcome }}
          </p>
          <div class="verdict-strip">
            <span
              >执行状态 <b>{{ state.backtest?.status ?? '未知' }}</b></span
            ><span>研究判定 <b>未由回测状态给出</b></span
            ><span>手续费以外成本 <b>未知</b></span
            ><span>状态接口无 run_id，不归属所选历史运行</span>
          </div>
        </section>
        <section v-else-if="activeTool === 'download'" class="command-panel">
          <h2>下载研究数据</h2>
          <p>
            提交成功不代表覆盖完整或满足研究门槛。运行白名单仅用于旧引擎预填。
          </p>
          <div class="form-grid">
            <label
              >输入方式<Select
                name="dl-mode"
                v-model:value="dl.mode"
                :options="[
                  { label: '可用列表', value: 'list' },
                  { label: '手动填写', value: 'manual' },
                ]" /></label
            ><label
              >交易对<template v-if="dl.mode === 'list'"
                ><Select
                  v-model:value="dl.pairs"
                  mode="multiple"
                  :options="
                    state.pairs.map((v) => ({ label: v, value: v }))
                  " /></template
              ><Input
                v-else
                v-model:value="dl.manual"
                name="dl-manual"
                placeholder="BTC/USDT:USDT, ETH/USDT:USDT" /></label
            ><label
              >时间框架<Select
                v-model:value="dl.timeframes"
                mode="multiple"
                :options="
                  ['1m', '5m', '15m', '30m', '1h', '4h', '1d'].map((v) => ({
                    label: v,
                    value: v,
                  }))
                " /></label
            ><label
              >范围模式<Select
                v-model:value="dl.rangeMode"
                :options="[
                  { label: '最近天数', value: 'days' },
                  { label: '自定义 timerange', value: 'timerange' },
                ]" /></label
            ><label v-if="dl.rangeMode === 'days'"
              >天数<InputNumber v-model:value="dl.days" :min="1" /></label
            ><label v-else
              >timerange<Input v-model:value="dl.timerange" /></label
            ><label
              >trading_mode<Select
                v-model:value="dl.trading_mode"
                :options="[
                  { label: '未指定', value: '' },
                  { label: 'spot', value: 'spot' },
                  { label: 'futures', value: 'futures' },
                ]" /></label
            ><label
              >下载交易历史
              <Switch v-model:checked="dl.download_trades" /></label
            ><label
              >前置旧数据 <Switch v-model:checked="dl.prepend_data" /></label
            ><label class="danger-field"
              >先删除本地数据 erase
              <Switch v-model:checked="dl.erase" name="dl-erase"
            /></label>
          </div>
          <Button
            data-submit-download
            type="primary"
            :loading="state.busy.download"
            :disabled="state.commandsUnknown.download"
            @click="submitDownload"
            >提交下载任务</Button
          >
          <p role="alert" class="inline-error">{{ state.errors.download }}</p>
          <section v-if="state.jobs.download" class="job-fact">
            <b>{{ state.jobs.download.status }}</b
            ><span>任务 {{ state.jobs.download.job_id }}</span
            ><span v-if="state.jobs.download.status === 'success'"
              >任务完成；数据质量尚未评定</span
            >
          </section>
        </section>
        <section v-else-if="activeTool === 'lookahead'" class="command-panel">
          <h2>前瞻偏差分析</h2>
          <p>has_bias 仅对应本次分析，不代表整个研究通过。</p>
          <div class="form-grid">
            <label
              >策略<Select
                v-model:value="la.strategy"
                :options="state.strategies.map((v) => ({ label: v, value: v }))"
                show-search /></label
            ><label
              >周期<Select
                v-model:value="la.timeframe"
                :options="
                  ['1m', '5m', '15m', '30m', '1h', '4h', '1d'].map((v) => ({
                    label: v,
                    value: v,
                  }))
                " /></label
            ><label>时间范围<Input v-model:value="la.timerange" /></label
            ><label
              >最小交易额<InputNumber
                v-model:value="la.minimum_trade_amount"
                :min="0" /></label
            ><label
              >目标交易额<InputNumber
                v-model:value="la.targeted_trade_amount"
                :min="0" /></label
            ><label
              >允许限价单
              <Switch v-model:checked="la.lookahead_allow_limit_orders"
            /></label>
          </div>
          <Button
            type="primary"
            :loading="state.busy.lookahead"
            :disabled="state.commandsUnknown.lookahead"
            @click="submitLookahead"
            >提交分析</Button
          >
          <p role="alert" class="inline-error">{{ state.errors.lookahead }}</p>
          <section v-if="state.results.lookahead" class="result-block">
            <h3>结果 · {{ state.results.lookahead.jobId }}</h3>
            <pre>{{
              JSON.stringify(state.results.lookahead.data, null, 2)
            }}</pre>
          </section>
        </section>
        <section v-else class="command-panel">
          <h2>递归分析</h2>
          <p>保留各个 startup_candle 结果和原始 JSON。</p>
          <div class="form-grid">
            <label
              >策略<Select
                v-model:value="rc.strategy"
                :options="state.strategies.map((v) => ({ label: v, value: v }))"
                show-search /></label
            ><label
              >周期<Select
                v-model:value="rc.timeframe"
                :options="
                  ['1m', '5m', '15m', '30m', '1h', '4h', '1d'].map((v) => ({
                    label: v,
                    value: v,
                  }))
                " /></label
            ><label>时间范围<Input v-model:value="rc.timerange" /></label
            ><label
              >startup_candle 多值<Input
                v-model:value="rc.startup_text"
                placeholder="10, 20, 40"
            /></label>
          </div>
          <Button
            type="primary"
            :loading="state.busy.recursive"
            :disabled="state.commandsUnknown.recursive"
            @click="submitRecursive"
            >提交分析</Button
          >
          <p role="alert" class="inline-error">{{ state.errors.recursive }}</p>
          <section v-if="state.results.recursive" class="result-block">
            <h3>结果 · {{ state.results.recursive.jobId }}</h3>
            <pre>{{
              JSON.stringify(state.results.recursive.data, null, 2)
            }}</pre>
          </section>
        </section>
        <section class="history-section">
          <header>
            <div>
              <h2>历史运行与产物</h2>
              <span>{{
                state.historyKnown
                  ? `${state.history.length} 条历史记录`
                  : '历史尚未读取'
              }}</span>
            </div>
            <Button @click="refresh">刷新记录</Button>
          </header>
          <div class="history-split">
            <div class="history-table" role="listbox" aria-label="回测历史">
              <button
                v-for="row in state.history"
                :key="`${row.filename}:${row.strategy}`"
                type="button"
                role="option"
                :aria-selected="
                  selected?.filename === row.filename &&
                  selected.strategy === row.strategy
                "
                data-history-row
                @click="choose(row)"
              >
                <span class="run-status">历史产物</span
                ><strong>{{ row.strategy }}</strong
                ><small
                  >{{ row.run_id }} · {{ row.timeframe ?? '周期未知' }}</small
                ><small
                  >{{ fmtSec(row.backtest_start_time) }} ·
                  {{ row.filename }}</small
                >
              </button>
              <p v-if="state.historyKnown && !state.history.length">
                尚无历史回测记录
              </p>
              <p v-else-if="!state.historyKnown">
                历史读取结果未知，不能当作空记录。
              </p>
            </div>
            <aside v-if="selected" class="artifact-inspector">
              <h3>选中运行</h3>
              <dl>
                <dt>文件</dt>
                <dd>{{ selected.filename }}</dd>
                <dt>策略</dt>
                <dd>{{ selected.strategy }}</dd>
                <dt>run_id</dt>
                <dd>{{ selected.run_id }}</dd>
                <dt>时间</dt>
                <dd>{{ fmtSec(selected.backtest_start_time) }}</dd>
                <dt>周期</dt>
                <dd>{{ selected.timeframe ?? '未知' }}</dd>
                <dt>成本完整度</dt>
                <dd>未知</dd>
              </dl>
              <p class="artifact-unmapped">
                当前历史接口没有提供该 run_id 到回测明细的可验证映射。
              </p>
              <h4>历史备注</h4>
              <Textarea
                v-model:value="notes"
                :rows="3"
                name="history-notes"
              /><Button
                data-save-notes
                :loading="state.busy.notes"
                @click="saveNotes"
                >保存备注</Button
              >
            </aside>
            <aside v-else class="artifact-inspector">
              <h3>选择一条历史运行</h3>
              <p>产物缺失时显示不可用，不会自动换成最近记录。</p>
            </aside>
          </div>
          <p>历史文件删除已退出正式研究入口，现有磁盘产物保留。</p>
        </section>
        <p class="source-footnote">
          研究实例 Freqtrade
          webserver。执行成功、分析完成、研究通过和数据质量结论彼此独立。
        </p>
      </section>
    </div>
    <Modal
      :open="eraseConfirm"
      title="确认删除并重新下载本地数据"
      ok-text="确认 erase"
      cancel-text="取消"
      ok-type="danger"
      @ok="confirmErase"
      @cancel="eraseConfirm = false"
      ><p>将先删除本地已有数据，不会删除历史回测产物。</p>
      <p>已冻结币对：{{ pendingDownload?.pairs?.join('、') }}</p>
      <p>
        时间框架：{{ pendingDownload?.timeframes?.join('、') }}；erase={{
          pendingDownload?.erase
        }}
      </p></Modal
    >
    <Modal
      :open="unknownConfirmKind !== null"
      title="核对未知命令回执"
      ok-text="解除本页重复提交锁"
      cancel-text="继续锁定"
      :ok-button-props="{
        disabled: !recoveryReviewed || recoveryPhrase !== UNKNOWN_RETRY_ACK,
      }"
      @ok="confirmUnknown"
      @cancel="unknownConfirmKind = null"
      ><p>
        请先刷新相关状态，并在研究实例后台确认原请求没有创建任务。检查未完成时保持锁定。
      </p>
      <label class="recovery-check"
        ><input
          v-model="recoveryReviewed"
          data-recovery-reviewed
          type="checkbox"
        />我已在后台核对，确认没有对应任务</label
      ><label class="recovery-phrase"
        >输入“{{ UNKNOWN_RETRY_ACK }}”<Input
          v-model:value="recoveryPhrase"
          name="recovery-phrase"
      /></label>
      <p>
        解锁只允许你再次提交，不会自动重发原命令；再次提交可能产生重复任务。
      </p></Modal
    >
    <Modal
      :open="unknownConfirmKind !== null"
      title="核对未知命令回执"
      ok-text="解除本页重复提交锁"
      cancel-text="继续锁定"
      :ok-button-props="{
        disabled: !recoveryReviewed || recoveryPhrase !== UNKNOWN_RETRY_ACK,
      }"
      @ok="confirmUnknown"
      @cancel="unknownConfirmKind = null"
      ><p>
        请先刷新相关状态，并在研究实例后台确认原请求没有创建任务。检查未完成时保持锁定。
      </p>
      <label class="recovery-check"
        ><input
          v-model="recoveryReviewed"
          data-recovery-reviewed
          type="checkbox"
        />我已在后台核对，确认没有对应任务</label
      ><label class="recovery-phrase"
        >输入“{{ UNKNOWN_RETRY_ACK }}”<Input
          v-model:value="recoveryPhrase"
          name="recovery-phrase"
      /></label>
      <p>
        解锁只允许你再次提交，不会自动重发原命令；再次提交可能产生重复任务。
      </p></Modal
    >
  </main>
</template>

<style scoped>
.task-workspace {
  --navy: #0c1b2a;
  --surface: #132b3e;
  --line: #29485c;
  --text: #e5edf4;
  --muted: #a2b8c9;
  --blue: #69b4e4;
  min-width: 0;
  max-width: 100%;
  padding: 20px 24px 36px;
  color: var(--text);
  font-family:
    'IBM Plex Sans', 'Source Han Sans SC', 'Noto Sans SC', sans-serif;
}
.task-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}
.task-heading h1 {
  margin: 0;
  font-size: 22px;
}
.task-heading p {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 13px;
}
.task-layout {
  display: grid;
  grid-template-columns: 184px minmax(0, 1fr);
  gap: 22px;
  margin-top: 18px;
}
.task-nav {
  display: flex;
  flex-direction: column;
  gap: 3px;
  border-right: 1px solid var(--line);
  padding-right: 12px;
}
.task-nav button {
  border: 0;
  background: transparent;
  text-align: left;
  color: var(--muted);
  padding: 10px 12px;
  font: inherit;
  cursor: pointer;
}
.task-nav button.active,
.task-nav button:hover {
  background: var(--surface);
  color: var(--text);
  box-shadow: inset 2px 0 var(--blue);
}
.task-nav button:focus-visible,
.task-workspace button:focus-visible {
  outline: 2px solid var(--blue);
  outline-offset: 2px;
}
.task-primary {
  min-width: 0;
}
.queue-summary {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 16px 28px;
  border-block: 1px solid var(--line);
  padding: 14px 0;
  margin-bottom: 16px;
}
.queue-summary > div:first-child {
  display: grid;
  gap: 3px;
}
.queue-summary span {
  font-size: 12px;
  color: var(--muted);
}
.summary-metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 20px;
}
.queue-actions {
  display: flex;
  gap: 8px;
  margin-left: auto;
}
.command-panel,
.history-section {
  border-top: 1px solid var(--line);
  padding: 16px 0 20px;
}
.command-panel h2,
.history-section h2 {
  font-size: 16px;
  margin: 0 0 4px;
}
.command-panel > p,
.history-section > p {
  font-size: 12px;
  color: var(--muted);
  margin: 4px 0 14px;
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px 16px;
  margin: 12px 0 16px;
}
.form-grid label {
  display: grid;
  gap: 5px;
  min-width: 0;
  font-size: 12px;
  color: var(--muted);
}
.form-grid :deep(.ant-select),
.form-grid :deep(.ant-input-number) {
  width: 100%;
  min-width: 0;
}
.switch-field,
.danger-field {
  display: flex !important;
  align-items: center;
  gap: 12px !important;
}
.danger-field {
  color: #e0a6a6 !important;
}
.queue-summary b {
  color: var(--text);
}
.verdict-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 20px;
  margin-top: 12px;
  border-top: 1px solid var(--line);
  padding-top: 10px;
}
.verdict-strip span {
  font-size: 12px;
  color: var(--muted);
}
.verdict-strip b {
  color: var(--text);
  font-weight: 500;
}
.command-outcome {
  min-height: 18px;
  color: #9ac8e6;
}
.inline-error {
  color: #e4af93;
  font-size: 12px;
}
.job-fact {
  display: flex;
  gap: 18px;
  padding: 12px;
  border-block: 1px solid var(--line);
  margin-top: 12px;
}
.history-section > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.history-section > header span {
  font-size: 12px;
  color: var(--muted);
}
.history-split {
  display: grid;
  grid-template-columns: minmax(0, 1.25fr) minmax(260px, 0.75fr);
  gap: 20px;
  margin-top: 12px;
}
.history-table {
  min-width: 0;
  max-height: 420px;
  overflow: auto;
}
.history-table button {
  display: grid;
  width: 100%;
  grid-template-columns: 90px minmax(0, 1fr);
  gap: 4px 10px;
  padding: 10px 8px;
  text-align: left;
  color: var(--text);
  background: transparent;
  border: 0;
  border-bottom: 1px solid var(--line);
  cursor: pointer;
}
.history-table button[aria-selected='true'] {
  background: #18344a;
}
.history-table strong,
.history-table small {
  overflow-wrap: anywhere;
}
.history-table small {
  grid-column: 2;
  color: var(--muted);
  font-size: 11px;
}
.run-status {
  grid-row: span 3;
  color: #9fc7df;
  font-size: 11px;
}
.artifact-inspector {
  min-width: 0;
  border-left: 1px solid var(--line);
  padding: 0 0 0 18px;
}
.artifact-inspector h3 {
  font-size: 15px;
  margin: 0 0 12px;
}
.artifact-inspector h4 {
  font-size: 13px;
  margin: 12px 0 6px;
}
.artifact-inspector dl {
  display: grid;
  grid-template-columns: 100px minmax(0, 1fr);
  gap: 6px;
  margin: 0;
}
.artifact-inspector dt {
  color: var(--muted);
  font-size: 12px;
}
.artifact-inspector dd {
  margin: 0;
  overflow-wrap: anywhere;
  font-size: 12px;
}
.artifact-inspector a {
  display: inline-block;
  margin-top: 12px;
  color: #9bcbed;
}
.artifact-unmapped {
  margin: 12px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.artifact-inspector :deep(.ant-input) {
  margin: 8px 0;
}
.source-footnote {
  border-top: 1px solid var(--line);
  padding-top: 10px;
  color: var(--muted);
  font-size: 11px;
}
.result-block {
  margin-top: 14px;
}
.result-block h3 {
  font-size: 13px;
}
.result-block pre {
  max-height: 300px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  padding: 10px;
  background: var(--surface);
  color: var(--text);
}
@media (max-width: 1050px) {
  .task-layout {
    grid-template-columns: 1fr;
  }
  .task-nav {
    flex-direction: row;
    overflow-x: auto;
    border-right: 0;
    border-bottom: 1px solid var(--line);
    padding: 0 0 8px;
  }
  .task-nav button {
    white-space: nowrap;
  }
  .task-nav button.active,
  .task-nav button:hover {
    box-shadow: inset 0 -2px var(--blue);
  }
}
@media (max-width: 720px) {
  .task-workspace {
    padding: 14px 12px 28px;
  }
  .form-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .history-split {
    grid-template-columns: 1fr;
  }
  .artifact-inspector {
    border-left: 0;
    border-top: 1px solid var(--line);
    padding: 14px 0 0;
  }
  .summary-metrics {
    width: 100%;
  }
}
@media (max-width: 420px) {
  .form-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .task-heading h1 {
    font-size: 19px;
  }
  .queue-actions {
    margin-left: 0;
  }
}
.unknown-commands {
  margin-bottom: 16px;
  border: 1px solid #81583b;
  border-left: 3px solid #d99a5a;
  padding: 12px 14px;
}
.unknown-commands > strong {
  color: #f0c59b;
}
.unknown-commands > p {
  margin: 4px 0 8px;
  color: var(--muted);
  font-size: 12px;
}
.unknown-command {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 8px;
}
.recovery-check,
.recovery-phrase {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 12px 0;
}
.recovery-phrase :deep(.ant-input) {
  max-width: 280px;
}
</style>

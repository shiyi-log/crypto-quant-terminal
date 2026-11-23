<script lang="ts" setup>
import { computed, nextTick, ref, watch } from 'vue';

type Scenario = 'ongoing' | 'missing' | 'hard-failure';
type DayEvidence = {
  day: number;
  date: string;
  openingEquity: number | null;
  equity: number | null;
  deposit: number | null;
  pnl: number | null;
  roundTrips: number | null;
  costsComplete: boolean | null;
  reason: string;
  outage?: boolean;
};
type RunLog = {
  id: number;
  time: string;
  level: '信息' | '警告';
  phase: string;
  outcome: string;
  reason: string;
  durationMs: number;
  detail: string;
};
const props = defineProps<{
  onAction?: (label: string, category: string) => void;
  onApprove?: (scope: 'small-live') => void;
}>();
const selectedDay = ref(18);
const scenario = ref<Scenario>('ongoing');
const approvalOpen = ref(false);
const approvalSaved = ref(false);
const approvalTrigger = ref<HTMLButtonElement | null>(null);
const approvalCancel = ref<HTMLButtonElement | null>(null);
let focusBeforeApproval: HTMLElement | null = null;
const runLogs = ref<RunLog[]>([]);
let nextLogId = 1;
const notice = ref('固定演示证据 · 日期均按北京时间自然日统计。');
// 组件内的静态快照仅供设计验收，不代表真实账户或验收引擎结果。
function dateForDay(day: number) {
  return new Date(Date.UTC(2026, 9, 24 + day)).toISOString().slice(0, 10);
}
const demoPnl = (day: number) =>
  day === 18 ? 38.2 : day % 6 === 0 ? -7.5 : 4.7;
const demoTrades = (day: number) => (day === 18 ? 4 : day % 3 === 0 ? 3 : 2);
const evidence: DayEvidence[] = [];
let closingEquity: number | null = null;
for (let day = 1; day <= 30; day += 1) {
  if (day === 7) {
    evidence.push({
      day,
      date: dateForDay(day),
      openingEquity: null,
      equity: null,
      deposit: null,
      pnl: null,
      roundTrips: null,
      costsComplete: null,
      reason: 'demo_collection_stopped',
      outage: true,
    });
    closingEquity = null;
    continue;
  }
  if (day === 13) {
    evidence.push({
      day,
      date: dateForDay(day),
      openingEquity: closingEquity,
      equity: null,
      deposit: 0,
      pnl: null,
      roundTrips: 2,
      costsComplete: false,
      reason: 'funding_fee_observation_missing',
    });
    closingEquity = null;
    continue;
  }
  // 停机或成本缺失之后仅从显式演示快照重新锚定，不推算缺口日的权益。
  const openingEquity: number | null =
    day === 1 ? 1000 : day === 8 ? 1027.3 : day === 14 ? 1040 : closingEquity;
  if (day >= 24) {
    evidence.push({
      day,
      date: dateForDay(day),
      openingEquity,
      equity: null,
      deposit: 0,
      pnl: null,
      roundTrips: null,
      costsComplete: null,
      reason: 'demo_evidence_not_recorded',
    });
    closingEquity = null;
    continue;
  }
  const pnl = demoPnl(day);
  const equity: number | null =
    openingEquity === null ? null : Number((openingEquity + pnl).toFixed(2));
  evidence.push({
    day,
    date: dateForDay(day),
    openingEquity,
    equity,
    deposit: 0,
    pnl,
    roundTrips: demoTrades(day),
    costsComplete: true,
    reason:
      day === 8
        ? 'demo_balance_snapshot_after_outage'
        : day === 14
          ? 'demo_balance_snapshot_after_cost_gap'
          : 'demo_day_observed',
  });
  closingEquity = equity;
}
const dayRecord = computed(() => evidence[selectedDay.value - 1]!);
const knownDays = computed(() => evidence.filter((item) => item.pnl !== null));
const totalPnl = computed(() =>
  knownDays.value.reduce((sum, item) => sum + (item.pnl ?? 0), 0),
);
const totalTrades = computed(() =>
  knownDays.value.reduce((sum, item) => sum + (item.roundTrips ?? 0), 0),
);
const profitDays = computed(
  () => knownDays.value.filter((item) => (item.pnl ?? 0) > 0).length,
);
const allCostsComplete = computed(
  () =>
    knownDays.value.length === 30 &&
    knownDays.value.every((item) => item.costsComplete === true),
);
const segments = computed(() =>
  [
    { label: '第 1–10 天', start: 1, end: 10 },
    { label: '第 11–20 天', start: 11, end: 20 },
    { label: '第 21–30 天', start: 21, end: 30 },
  ].map((segment) => {
    const rows = evidence.filter(
      (item) => item.day >= segment.start && item.day <= segment.end,
    );
    const complete = rows.every((item) => item.pnl !== null);
    const pnl = complete
      ? rows.reduce((sum, item) => sum + (item.pnl ?? 0), 0)
      : null;
    return {
      ...segment,
      pnl,
      result: pnl === null ? '未知' : pnl > 0 ? '正收益' : '未达标',
    };
  }),
);
const verdict = computed(() => {
  if (scenario.value === 'hard-failure')
    return {
      title: '硬失败场景演示',
      reason: 'daily_net_loss_hard_limit_exceeded',
      detail:
        '固定演练预置硬失败状态，仅用于展示结论优先级，不代表账户发生超限。',
    };
  if (scenario.value === 'missing')
    return {
      title: '证据缺失，无法评估',
      reason: 'acceptance_evidence_incomplete',
      detail: '缺失事实保持未知；不得按零值、停机日或未来日期补算。',
    };
  return {
    title: '验收进行中',
    reason: 'acceptance_window_in_progress',
    detail: '固定窗口尚未结束，当前演示样本不形成验收结论。',
  };
});
function money(value: number | null, signed = false) {
  if (value === null) return '未知';
  const prefix = signed && value > 0 ? '+' : '';
  return (
    prefix +
    value.toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }) +
    ' USDT'
  );
}
const gates = computed(() => [
  { label: '连续自然日 30 天', value: '未知', ok: false },
  {
    label: '净利润至少 1,500 USDT',
    value: knownDays.value.length === 30 ? money(totalPnl.value) : '未知',
    ok: knownDays.value.length === 30 && totalPnl.value >= 1500,
  },
  {
    label: '100 笔完整往返',
    value:
      knownDays.value.length < 30 ? '未知' : totalTrades.value + ' / 100 笔',
    ok: totalTrades.value >= 100,
  },
  {
    label: '至少 18 个盈利日',
    value: knownDays.value.length < 30 ? '未知' : profitDays.value + ' / 18 天',
    ok: profitDays.value >= 18,
  },
  {
    label: '三个固定分段均为正收益',
    value: segments.value.every((item) => item.pnl !== null)
      ? segments.value.map((item) => item.result).join(' / ')
      : '未知',
    ok: segments.value.every((item) => item.pnl !== null && item.pnl > 0),
  },
  {
    label: '手续费与资金费完整',
    value: allCostsComplete.value ? '完整' : '未知或缺失',
    ok: allCostsComplete.value,
  },
]);
function changeScenario(value: Scenario) {
  const startedAt = performance.now();
  scenario.value = value;
  const labels: Record<Scenario, string> = {
    ongoing: '切换到进行中场景',
    missing: '切换到证据缺失场景',
    'hard-failure': '切换到硬失败场景',
  };
  notice.value = labels[value] + '；只更新当前页面演示状态。';
  appendRunLog(
    value === 'hard-failure' ? '警告' : '信息',
    '验收场景演练',
    '本地演练完成',
    verdict.value.reason,
    `${labels[value]}；窗口为 2026-10-25 至 2026-11-23（北京时间），不连接交易账户，生产资格保持未授权。`,
    startedAt,
  );
  props.onAction?.(labels[value], 'acceptance-scenario');
}
function appendRunLog(
  level: RunLog['level'],
  phase: string,
  outcome: string,
  reason: string,
  detail: string,
  startedAt: number,
) {
  // 日志只追加到当前组件内存，不请求服务端日志接口或持久化到磁盘。
  runLogs.value.unshift({
    id: nextLogId++,
    time: new Intl.DateTimeFormat('zh-CN', {
      dateStyle: 'medium',
      timeStyle: 'medium',
      hour12: false,
      timeZone: 'Asia/Shanghai',
    }).format(new Date()),
    level,
    phase,
    outcome,
    reason,
    durationMs: Math.max(0, Math.round(performance.now() - startedAt)),
    detail,
  });
}
function closeApproval() {
  approvalOpen.value = false;
}
watch(approvalOpen, async (open) => {
  if (open) {
    focusBeforeApproval =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : approvalTrigger.value;
    await nextTick();
    approvalCancel.value?.focus();
    return;
  }
  await nextTick();
  focusBeforeApproval?.focus();
  focusBeforeApproval = null;
});
function recordApproval() {
  const startedAt = performance.now();
  closeApproval();
  approvalSaved.value = true;
  notice.value = '演示审批已记录在当前页面；生产资格仍未授权。';
  appendRunLog(
    '信息',
    '审批流程演练',
    '已记录本地演练回执',
    'approval_preview_only',
    '申请范围：小额实盘审批演练；结果仅存在当前页面内存；账户、资金、生产权限均未变更。',
    startedAt,
  );
  props.onApprove?.('small-live');
  props.onAction?.('记录小额实盘审批演练', 'approval-preview');
}
</script>
<template>
  <section class="acceptance-preview" aria-labelledby="acceptance-title">
    <header class="page-head">
      <div>
        <p class="eyebrow">READINESS EVIDENCE</p>
        <h1 id="acceptance-title">验收与上线</h1>
        <h2 class="page-subtitle">Demo 验收证据</h2>
        <p class="subhead">固定 30 天窗口 · 北京时间</p>
      </div>
      <span class="demo-flag"><i />固定演示数据</span>
    </header>
    <p class="notice" role="status" aria-live="polite">{{ notice }}</p>
    <section
      class="verdict"
      :data-scenario="scenario"
      data-testid="acceptance-verdict"
      aria-live="polite"
    >
      <div>
        <span class="verdict-label">当前演示判定</span>
        <h2>{{ verdict.title }}</h2>
        <p>{{ verdict.detail }}</p>
      </div>
      <code>{{ verdict.reason }}</code>
    </section>
    <div class="scenario-switch" role="group" aria-label="切换本地验收演示场景">
      <button
        type="button"
        :aria-pressed="scenario === 'ongoing'"
        @click="changeScenario('ongoing')"
      >
        进行中</button
      ><button
        type="button"
        :aria-pressed="scenario === 'missing'"
        @click="changeScenario('missing')"
      >
        证据缺失</button
      ><button
        type="button"
        data-scenario="hard-failure"
        :aria-pressed="scenario === 'hard-failure'"
        @click="changeScenario('hard-failure')"
      >
        硬失败
      </button>
    </div>
    <section class="window-panel" aria-labelledby="window-title">
      <div class="panel-head">
        <div>
          <h2 id="window-title">固定验收窗口</h2>
          <p data-testid="acceptance-window">
            2026-10-25 至 2026-11-23 · 北京时间 00:00–23:59
          </p>
        </div>
        <span class="window-count"
          >{{ knownDays.length }} / 30 日有完整净损益证据</span
        >
      </div>
      <div
        class="calendar"
        data-testid="acceptance-calendar"
        aria-label="30 天验收日历，北京时间"
      >
        <span
          v-for="weekday in ['日', '一', '二', '三', '四', '五', '六']"
          :key="weekday"
          class="weekday"
          >{{ weekday }}</span
        ><button
          v-for="item in evidence"
          :key="item.day"
          type="button"
          data-testid="calendar-day"
          :data-day="item.day"
          :class="{
            selected: selectedDay === item.day,
            unknown: item.pnl === null,
            positive: item.pnl !== null && item.pnl > 0,
            negative: item.pnl !== null && item.pnl <= 0,
          }"
          :aria-label="
            item.date +
            '，北京时间：' +
            (item.outage
              ? '停机日'
              : item.pnl === null
                ? '证据未知'
                : money(item.pnl, true))
          "
          :aria-pressed="selectedDay === item.day"
          @click="selectedDay = item.day"
        >
          <span>{{ String(item.day).padStart(2, '0') }}</span
          ><small>{{
            item.outage
              ? '停机'
              : item.pnl === null
                ? '未知'
                : item.pnl > 0
                  ? '盈利'
                  : '亏损'
          }}</small>
        </button>
      </div>
      <p class="calendar-note">
        停机日继续保留在自然日窗口中；未来与成本缺失的净损益显示“未知”，不以 0
        替代。
      </p>
    </section>
    <div class="evidence-layout">
      <section
        class="day-panel"
        data-testid="day-evidence"
        aria-labelledby="day-title"
      >
        <div class="panel-head">
          <div>
            <p class="eyebrow">
              {{ dayRecord.date }} · 第 {{ selectedDay }} 天 · 北京时间
            </p>
            <h2 id="day-title">当日证据</h2>
          </div>
          <code>{{ dayRecord.reason }}</code>
        </div>
        <dl class="metrics">
          <div>
            <dt>日初净权益</dt>
            <dd
              data-testid="opening-equity"
              :data-value="dayRecord.openingEquity ?? ''"
            >
              {{ money(dayRecord.openingEquity) }}
            </dd>
          </div>
          <div>
            <dt>日末净权益</dt>
            <dd
              data-testid="closing-equity"
              :data-value="dayRecord.equity ?? ''"
            >
              {{ money(dayRecord.equity) }}
            </dd>
          </div>
          <div>
            <dt>外部净入金</dt>
            <dd data-testid="net-deposit" :data-value="dayRecord.deposit ?? ''">
              {{ money(dayRecord.deposit) }}
            </dd>
          </div>
          <div>
            <dt>日净损益</dt>
            <dd data-testid="daily-pnl" :data-value="dayRecord.pnl ?? ''">
              {{ money(dayRecord.pnl, true) }}
            </dd>
          </div>
          <div>
            <dt>完整往返</dt>
            <dd>
              {{
                dayRecord.roundTrips === null
                  ? '未知'
                  : dayRecord.roundTrips + ' 笔'
              }}
            </dd>
          </div>
          <div>
            <dt>手续费与资金费</dt>
            <dd>
              {{
                dayRecord.costsComplete === null
                  ? '未知'
                  : dayRecord.costsComplete
                    ? '成本完整'
                    : '成本缺失'
              }}
            </dd>
          </div>
          <div>
            <dt>证据原因码</dt>
            <dd>
              <code>{{ dayRecord.reason }}</code>
            </dd>
          </div>
        </dl>
      </section>
      <section class="segments-panel" aria-labelledby="segments-title">
        <div class="panel-head">
          <div>
            <p class="eyebrow">固定分段</p>
            <h2 id="segments-title">稳定性检查</h2>
          </div>
        </div>
        <div class="segment-list">
          <article
            v-for="segment in segments"
            :key="segment.start"
            data-testid="acceptance-segment"
          >
            <div>
              <strong>{{ segment.label }}</strong
              ><span>{{ segment.result }}</span>
            </div>
            <p>
              {{
                segment.pnl === null
                  ? '分段证据不完整'
                  : money(segment.pnl, true)
              }}
            </p>
          </article>
        </div>
      </section>
    </div>
    <section
      class="gate-panel"
      data-testid="acceptance-gates"
      aria-labelledby="gates-title"
    >
      <div class="panel-head">
        <div>
          <p class="eyebrow">上线前置条件</p>
          <h2 id="gates-title">验收门槛</h2>
        </div>
        <span class="muted">所有门槛须同时满足</span>
      </div>
      <ul>
        <li v-for="gate in gates" :key="gate.label" :data-met="gate.ok">
          <span class="gate-mark">{{ gate.ok ? '✓' : '—' }}</span
          ><span>{{ gate.label }}</span
          ><strong>{{ gate.value }}</strong>
        </li>
      </ul>
    </section>
    <section
      class="run-log-panel"
      data-testid="acceptance-run-log"
      aria-labelledby="run-log-title"
    >
      <div class="panel-head">
        <div>
          <p class="eyebrow">当前页面内存</p>
          <h2 id="run-log-title">演示运行记录</h2>
        </div>
        <span class="muted">不写服务器日志 · 不代表真实交易审计</span>
      </div>
      <p v-if="runLogs.length === 0" class="empty-log">
        尚无演练事件。切换场景或记录审批演练后，会在此显示中文事件详情。
      </p>
      <ol v-else class="run-log-list">
        <li v-for="log in runLogs" :key="log.id" :data-level="log.level">
          <header>
            <time>{{ log.time }}（北京时间）</time><span>{{ log.level }}</span>
          </header>
          <h3>{{ log.phase }} · {{ log.outcome }}</h3>
          <p>{{ log.detail }}</p>
          <footer>
            <code>{{ log.reason }}</code
            ><span>页面内处理耗时 {{ log.durationMs }} ms</span>
          </footer>
        </li>
      </ol>
    </section>
    <aside class="eligibility-panel">
      <div class="eligibility-head">
        <div>
          <p class="eyebrow">账户与资金阶段</p>
          <h2>授权保持关闭</h2>
        </div>
        <span class="locked" data-testid="production-eligibility">未授权</span>
      </div>
      <dl class="identity">
        <div>
          <dt>当前身份</dt>
          <dd>Binance Demo · 固定样本</dd>
        </div>
        <div>
          <dt>计划阶段</dt>
          <dd>Demo → 100 → 300 → 1000 USDT</dd>
        </div>
        <div>
          <dt>生产资格</dt>
          <dd>未授权</dd>
        </div>
      </dl>
      <button
        ref="approvalTrigger"
        class="approval-button"
        data-action="preview-approval"
        type="button"
        @click="approvalOpen = true"
      >
        演练审批记录
      </button>
      <p class="disclaimer">
        审批只触发本地演练回调，不改变准入、账户或资金阶段。
      </p>
      <p v-if="approvalSaved" class="receipt" role="status">
        本地回执：演示审批已记录，资格仍未授权。
      </p>
    </aside>
    <div v-if="approvalOpen" class="modal-shade">
      <section
        class="approval-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="approval-title"
        @keydown.esc.stop.prevent="closeApproval"
      >
        <p class="eyebrow">仅本地演练</p>
        <h2 id="approval-title">记录小额实盘审批演练？</h2>
        <p>这只会触发演示回调，不会授权实盘或修改账户。</p>
        <div>
          <button
            ref="approvalCancel"
            type="button"
            class="cancel"
            data-action="cancel-approval"
            @click="closeApproval"
          >
            取消</button
          ><button
            type="button"
            class="approve"
            data-action="confirm-approval"
            @click="recordApproval"
          >
            确认记录
          </button>
        </div>
      </section>
    </div>
  </section>
</template>
<style scoped>
.acceptance-preview {
  /* 验收读数继承全站色标，缺少外层 Shell 时使用文档定义色值。 */
  --bg: var(--qt-ocean, #0c1b2a);
  --surface: var(--qt-workspace, #132b3e);
  --line: var(--qt-divider, #29485c);
  --text: var(--qt-foreground, #e5edf4);
  --muted: var(--qt-muted, #a2b8c9);
  --blue: var(--qt-interactive, #69b4e4);
  --green: var(--qt-profit, #49c7a5);
  --amber: var(--qt-attention, #e8b65c);
  min-height: 100%;
  padding: 30px clamp(18px, 4vw, 56px) 60px;
  background: var(--bg);
  color: var(--text);
  font-family: var(
    --qt-ui-font,
    'IBM Plex Sans',
    'Source Han Sans SC',
    'Noto Sans SC',
    sans-serif
  );
  font-variant-numeric: tabular-nums;
}
.acceptance-preview * {
  box-sizing: border-box;
}
h1,
h2,
p,
dl,
dd {
  margin: 0;
}
h1 {
  font-size: 29px;
  letter-spacing: 0.01em;
  font-weight: 600;
}
h2 {
  font-size: 18px;
  font-weight: 600;
}
p {
  line-height: 1.65;
}
.page-head,
.panel-head,
.eligibility-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 16px;
}
.eyebrow {
  font-size: 11px;
  color: var(--blue);
  letter-spacing: 0.06em;
  margin-bottom: 5px;
}
.subhead {
  margin-top: 5px;
  color: var(--muted);
  font-size: 13px;
}
.demo-flag {
  display: flex;
  align-items: center;
  gap: 9px;
  color: var(--muted);
  font-size: 12px;
}
.demo-flag i {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--amber);
}
.notice {
  margin: 20px 0;
  padding: 11px 14px;
  border-left: 2px solid var(--blue);
  background: #132b3eaa;
  color: var(--muted);
  font-size: 12px;
}
.verdict {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  padding: 20px 22px;
  border: 1px solid var(--line);
  border-left: 3px solid var(--blue);
  background: var(--surface);
}
.verdict[data-scenario='hard-failure'] {
  border-left-color: #e47b70;
}
.verdict[data-scenario='missing'] {
  border-left-color: var(--amber);
}
.verdict-label {
  font-size: 11px;
  color: var(--muted);
}
.verdict h2 {
  margin: 5px 0;
  color: var(--blue);
}
.verdict[data-scenario='hard-failure'] h2 {
  color: #ef958a;
}
.verdict[data-scenario='missing'] h2 {
  color: var(--amber);
}
.verdict p {
  font-size: 12px;
  color: var(--muted);
}
code {
  color: #a9c8dc;
  font:
    11px 'IBM Plex Mono',
    monospace;
  overflow-wrap: anywhere;
}
.scenario-switch {
  display: flex;
  gap: 8px;
  margin: 14px 0 22px;
}
.scenario-switch button,
.cancel,
.approve,
.approval-button {
  min-height: 38px;
  padding: 8px 14px;
  border: 1px solid var(--line);
  color: var(--text);
  background: var(--surface);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.scenario-switch button[aria-pressed='true'] {
  border-color: var(--blue);
  color: var(--blue);
  background: #69b4e416;
}
.window-panel,
.day-panel,
.segments-panel,
.gate-panel,
.eligibility-panel {
  border: 1px solid var(--line);
  background: var(--surface);
  padding: 20px;
}
.panel-head {
  align-items: flex-start;
}
.panel-head p {
  font-size: 12px;
  color: var(--muted);
}
.window-count,
.muted {
  color: var(--muted);
  font-size: 12px;
}
.calendar {
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 5px;
  margin-top: 18px;
}
.weekday {
  text-align: center;
  color: var(--muted);
  font-size: 11px;
  padding: 3px;
}
.calendar button {
  min-width: 0;
  min-height: 54px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  justify-content: space-between;
  padding: 7px 8px;
  border: 1px solid var(--line);
  color: var(--text);
  background: #0f2333;
  cursor: pointer;
  text-align: left;
}
.calendar button span {
  font-size: 13px;
  font-weight: 500;
}
.calendar button small {
  font-size: 9px;
  color: var(--muted);
}
.calendar button.positive small {
  color: var(--green);
}
.calendar button.negative small {
  color: var(--amber);
}
.calendar button.unknown {
  background: #101f2b;
  color: #8598a8;
}
.calendar button.selected {
  outline: 2px solid var(--blue);
  outline-offset: 1px;
  border-color: var(--blue);
}
button:focus-visible {
  outline: 2px solid var(--blue);
  outline-offset: 3px;
}
.calendar-note {
  margin-top: 13px;
  color: var(--muted);
  font-size: 11px;
}
.evidence-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(260px, 0.85fr);
  gap: 18px;
  margin-top: 18px;
}
.day-panel,
.segments-panel {
  min-width: 0;
}
.day-panel .panel-head {
  align-items: center;
}
.day-panel .eyebrow {
  margin: 0;
}
.metrics {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  margin-top: 18px;
}
.metrics > div {
  min-width: 0;
  padding: 13px 10px;
  border-top: 1px solid var(--line);
}
.metrics dt {
  font-size: 11px;
  color: var(--muted);
}
.metrics dd {
  margin-top: 6px;
  font-size: 14px;
  overflow-wrap: anywhere;
}
.segment-list {
  display: grid;
  margin-top: 16px;
}
.segment-list article {
  padding: 14px 0;
  border-top: 1px solid var(--line);
}
.segment-list article div {
  display: flex;
  justify-content: space-between;
  gap: 10px;
}
.segment-list strong {
  font-size: 12px;
  font-weight: 500;
}
.segment-list span,
.segment-list p {
  font-size: 11px;
  color: var(--muted);
}
.segment-list p {
  margin-top: 7px;
}
.gate-panel {
  margin-top: 18px;
}
.gate-panel ul {
  list-style: none;
  padding: 0;
  margin: 14px 0 0;
}
.gate-panel li {
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr) minmax(100px, auto);
  gap: 8px;
  align-items: center;
  padding: 12px 0;
  border-top: 1px solid var(--line);
  font-size: 12px;
}
.gate-panel li strong {
  font-weight: 500;
  text-align: right;
  color: var(--muted);
}
.gate-mark {
  color: var(--amber);
}
.eligibility-panel {
  margin-top: 18px;
}
.eligibility-head {
  align-items: center;
}
.locked {
  padding: 5px 10px;
  border: 1px solid #f0c17b77;
  color: var(--amber);
  font-size: 12px;
}
.identity {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin: 18px 0;
}
.identity div {
  padding: 12px;
  border-top: 1px solid var(--line);
}
.identity dt {
  color: var(--muted);
  font-size: 10px;
}
.identity dd {
  margin-top: 6px;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.approval-button,
.approve {
  border-color: var(--blue);
  background: var(--blue);
  color: var(--bg);
}
.disclaimer,
.receipt {
  margin-top: 10px;
  color: var(--muted);
  font-size: 11px;
}
.receipt {
  color: var(--green);
}
.modal-shade {
  position: fixed;
  inset: 0;
  z-index: 20;
  display: grid;
  place-items: center;
  padding: 18px;
  background: #06111dcc;
}
.approval-dialog {
  width: min(440px, 100%);
  padding: 24px;
  border: 1px solid var(--line);
  background: var(--surface);
}
.approval-dialog h2 {
  margin: 8px 0;
}
.approval-dialog > p:not(.eyebrow) {
  color: var(--muted);
  font-size: 13px;
}
.approval-dialog > div {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 20px;
}
.cancel {
  background: transparent;
}
.scenario-switch button:hover,
.cancel:hover {
  border-color: var(--blue);
  color: var(--blue);
}
@media (max-width: 720px) {
  .acceptance-preview {
    padding: 22px 16px 40px;
  }
  .page-head {
    align-items: flex-start;
  }
  .page-head h1 {
    font-size: 23px;
  }
  .demo-flag {
    font-size: 10px;
  }
  .verdict {
    align-items: flex-start;
    flex-direction: column;
    padding: 16px;
  }
  .evidence-layout {
    grid-template-columns: 1fr;
  }
  .identity {
    grid-template-columns: 1fr;
  }
  .calendar {
    gap: 3px;
  }
  .calendar button {
    min-height: 44px;
    padding: 5px;
  }
  .calendar button small {
    font-size: 8px;
  }
  .gate-panel li {
    grid-template-columns: 20px minmax(0, 1fr) auto;
    font-size: 11px;
  }
  .gate-panel li strong {
    max-width: 112px;
    font-size: 10px;
  }
}
@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    scroll-behavior: auto !important;
    transition: none !important;
  }
}
</style>
<style scoped>
.run-log-panel {
  margin-top: 18px;
  padding: 20px;
  border: 1px solid #29485c;
  background: #132b3e;
}
.empty-log {
  margin-top: 16px;
  color: #a2b8c9;
  font-size: 12px;
}
.run-log-list {
  display: grid;
  gap: 10px;
  margin: 16px 0 0;
  padding: 0;
  list-style: none;
}
.run-log-list li {
  padding: 14px;
  border: 1px solid #29485c;
  background: #0f2333;
}
.run-log-list header,
.run-log-list footer {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}
.run-log-list header,
.run-log-list footer span {
  color: #a2b8c9;
  font-size: 10px;
}
.run-log-list header span {
  padding: 3px 7px;
  border: 1px solid #69b4e477;
  color: #69b4e4;
}
.run-log-list [data-level='警告'] header span {
  border-color: #f0c17b77;
  color: #f0c17b;
}
.run-log-list h3 {
  margin: 9px 0 4px;
  font-size: 12px;
  font-weight: 600;
}
.run-log-list p {
  font-size: 11px;
  color: #a2b8c9;
}
.run-log-list footer {
  margin-top: 10px;
  align-items: center;
}
.run-log-list code {
  overflow-wrap: anywhere;
}
@media (max-width: 720px) {
  .run-log-panel {
    padding: 15px;
  }
  .run-log-list header,
  .run-log-list footer {
    flex-direction: column;
    gap: 6px;
  }
}
</style>

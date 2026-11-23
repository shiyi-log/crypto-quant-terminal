<script lang="ts" setup>
import { computed, ref, watch } from 'vue';

const props = defineProps<{ workspace: string; eventId?: unknown }>();

type EvidenceKey = 'calendar' | 'trades' | 'costs';
type Density = 'compact' | 'comfortable';
type LogLevel = 'all' | 'warning' | 'info';
type DemoLog = { id: string; level: Exclude<LogLevel, 'all'>; time: string; stage: string; result: string; reason: string; duration: string; title: string; detail: string };

const evidenceKey = ref<EvidenceKey>('calendar');
const approvalOpen = ref(false);
const approvalRecorded = ref(false);
const symbolFilter = ref('all');
const timeframeFilter = ref('all');
const collectionQueue = ref<string[]>([]);
const logLevel = ref<LogLevel>('all');
const selectedLog = ref('evt-1041');
const density = ref<Density>('compact');
const savedDensity = ref<Density>('compact');
const statusMessage = ref('演示数据已加载；所有操作仅影响本地预览。');

const evidence = [
  { key: 'calendar' as const, label: '30 天日历', title: '固定窗口与日界', value: '18 / 30 天', note: '当前演示窗口保留停机日，未授予验收资格。' },
  { key: 'trades' as const, label: '完整交易', title: '往返交易证据', value: '73 / 100', note: '演示账本含 73 笔完整往返，仍低于准入门槛。' },
  { key: 'costs' as const, label: '成本对账', title: '手续费与资金费', value: '部分缺失', note: '资金费数据存在缺口，结果保持不可评估。' },
];
const selectedEvidence = computed(() => evidence.find((item) => item.key === evidenceKey.value) ?? {
  key: 'calendar' as const,
  label: '30 天日历',
  title: '固定窗口与日界',
  value: '18 / 30 天',
  note: '当前演示窗口保留停机日，未授予验收资格。',
});

const coverageRows = [
  { symbol: 'BTCUSDT', timeframes: ['5m', '15m', '1h'], timeframeLabel: '5m / 15m / 1h', coverage: '96%', freshness: '12 分钟前', quality: '可观察' },
  { symbol: 'ETHUSDT', timeframes: ['5m', '15m', '1h'], timeframeLabel: '5m / 15m / 1h', coverage: '88%', freshness: '18 分钟前', quality: '有缺口' },
  { symbol: 'TRXUSDT', timeframes: ['5m', '15m'], timeframeLabel: '5m / 15m', coverage: '71%', freshness: '42 分钟前', quality: '有缺口' },
];
// 覆盖率是交易对级固定摘要；周期只用于筛选支持范围，不派生周期指标。
const filteredCoverage = computed(() =>
  coverageRows.filter(
    (row) =>
      (symbolFilter.value === 'all' || row.symbol === symbolFilter.value) &&
      (timeframeFilter.value === 'all' || row.timeframes.includes(timeframeFilter.value)),
  ),
);
function resetCoverageFilters() {
  symbolFilter.value = 'all';
  timeframeFilter.value = 'all';
}

const logs = ref<DemoLog[]>([
  { id: 'evt-1041', level: 'info', time: '22:12:04', stage: '风险快照', result: '演示样本已记录', reason: 'demo_snapshot_recorded', duration: '4 毫秒', title: '本地风险快照已封存', detail: '固定演示权益与风险摘要已记录；等待下一份演示样本。' },
  { id: 'evt-1042', level: 'warning', time: '22:10:31', stage: '资金费对账', result: '保持未完成', reason: 'funding_fee_observation_delayed', duration: '0 毫秒', title: '资金费观测延迟', detail: '保留最后成功样本，资金费未完成对账；不补零、不推断缺失现金流。' },
  { id: 'evt-1043', level: 'warning', time: '21:58:11', stage: '行情完整性', result: '拒绝合并', reason: 'market_sequence_gap', duration: '2 毫秒', title: '盘口序列存在缺口', detail: '标记固定样本中的数据缺口；不伪造连续序号或交易信号。' },
]);
const filteredLogs = computed(() => logLevel.value === 'all' ? logs.value : logs.value.filter((log) => log.level === logLevel.value));
const eventIdIsInvalid = computed(
  () =>
    props.eventId !== undefined &&
    (typeof props.eventId !== 'string' || !/^evt-\d{4,6}$/.test(props.eventId)),
);
const requestedLog = computed(() =>
  props.eventId && !eventIdIsInvalid.value
    ? logs.value.find((log) => log.id === props.eventId)
    : undefined,
);
const eventIdIsMissing = computed(
  () => props.eventId !== undefined && !eventIdIsInvalid.value && !requestedLog.value,
);

// 筛选切换后，详情必须始终对应列表中仍可见的事件。
watch([filteredLogs, () => props.eventId], ([visibleLogs, eventId]) => {
  if (eventId) {
    selectedLog.value =
      typeof eventId === 'string' &&
      logs.value.some((log) => log.id === eventId)
        ? eventId
        : '';
  } else if (!visibleLogs.some((log) => log.id === selectedLog.value)) {
    selectedLog.value = visibleLogs[0]?.id ?? '';
  }
}, { immediate: true });
const currentLog = computed(() =>
  logs.value.find((log) => log.id === selectedLog.value),
);
const currentLogUrl = computed(() =>
  currentLog.value
    ? `/#/quant/operations?event=${encodeURIComponent(currentLog.value.id)}`
    : '',
);
const densityLabel = computed(() => savedDensity.value === 'comfortable' ? '舒适' : '紧凑');

// 演示事件只追加在当前页面内存，不代表服务端审计或真实持久化。
let nextLogSequence = 1044;
function appendDemoLog(entry: Omit<DemoLog, 'id' | 'time' | 'duration'>) {
  const id = 'evt-' + String(nextLogSequence++);
  logs.value.unshift({ ...entry, id, time: '22:12:05', duration: '1 毫秒' });
  selectedLog.value = id;
}

function recordApproval() {
  approvalOpen.value = false;
  approvalRecorded.value = true;
  statusMessage.value = '已记录演示审批；仅本地演练，生产资格仍未授权。';
  appendDemoLog({ level: 'info', stage: '人工审批演练', result: '本地状态已更新', reason: 'demo_approval_preview_only', title: '演示审批记录已新增', detail: '审批内容只保存在当前页面，生产资格仍未授权，未发送账户或交易请求。' });
}

function queueCollection() {
  const symbol = symbolFilter.value === 'all' ? 'BTCUSDT' : symbolFilter.value;
  const alreadyQueued = collectionQueue.value.includes(symbol);
  if (alreadyQueued) {
    statusMessage.value = `${symbol} 已在本地队列；仅本地演练。`;
    return;
  }

  collectionQueue.value.push(symbol);
  statusMessage.value = `已将 ${symbol} 加入本地采集演练队列；仅本地演练。`;
  appendDemoLog({ level: 'info', stage: '数据补采演练', result: '已加入本地队列', reason: 'demo_collection_queued', title: '本地补采演练已排队', detail: symbol + ' 仅加入当前页面队列；未启动采集器或请求行情服务。' });
}

function savePreferences() {
  savedDensity.value = density.value;
  statusMessage.value = '已保存本地演示偏好；不会写入账户或服务配置。';
  appendDemoLog({ level: 'info', stage: '界面偏好', result: '页面状态已更新', reason: 'demo_preference_updated', title: '演示显示偏好已修改', detail: '偏好只影响当前页面状态，未写入账户或服务配置。' });
}

function resetPreferences() {
  density.value = 'compact';
  savedDensity.value = 'compact';
  statusMessage.value = '已重置本地演示偏好。';
  appendDemoLog({ level: 'info', stage: '界面偏好', result: '恢复默认页面状态', reason: 'demo_preference_reset', title: '演示显示偏好已重置', detail: '仅恢复当前页面显示密度，不修改账户或服务配置。' });
}
</script>

<template>
  <section class="governance-preview" :data-density="savedDensity" :aria-label="props.workspace === 'acceptance' ? '验收与上线' : props.workspace === 'data-quality' ? '数据中心' : props.workspace === 'operations' ? '运维与证据' : '设置与系统'">
    <header class="preview-header">
      <div>
        <p class="eyebrow">LOCAL DEMO / GOVERNANCE</p>
        <h1>{{ props.workspace === 'acceptance' ? '验收与上线' : props.workspace === 'data-quality' ? '数据中心' : props.workspace === 'operations' ? '运维与证据' : '设置与系统' }}</h1>
      </div>
      <div class="demo-badge" aria-label="数据环境"><span class="badge-dot" />演示数据 · 不连接交易账户</div>
    </header>

    <div class="status-line" role="status" aria-live="polite">{{ statusMessage }}</div>

    <section v-if="props.workspace === 'acceptance'" class="workspace-grid">
      <div class="main-column">
        <div class="section-heading"><div><p class="eyebrow">READINESS EVIDENCE</p><h2>Demo 验收证据</h2></div><span class="muted">固定身份 · 本地快照</span></div>
        <div class="evidence-list">
          <button v-for="item in evidence" :key="item.key" class="evidence-row" :class="{ active: evidenceKey === item.key }" :data-evidence="item.key" type="button" @click="evidenceKey = item.key">
            <span class="evidence-label">{{ item.label }}</span><strong>{{ item.value }}</strong><span class="chevron">›</span>
          </button>
        </div>
        <article class="detail-panel" data-testid="evidence-detail">
          <div class="detail-topline"><span class="eyebrow">{{ selectedEvidence.title }}</span><span class="signal">演示快照</span></div>
          <strong class="detail-value">{{ selectedEvidence.value }}</strong>
          <p>{{ selectedEvidence.note }}</p>
        </article>
      </div>
      <aside class="side-column">
        <section class="fact-panel"><p class="eyebrow">PRODUCTION GATE</p><h2>生产资格</h2><div class="gate-state" data-testid="production-eligibility"><span class="gate-icon">×</span><div><strong>未授权</strong><p>演示审批不会改变真实准入状态。</p></div></div><dl class="facts"><div><dt>账户</dt><dd>Binance Demo</dd></div><div><dt>策略</dt><dd>未冻结</dd></div><div><dt>保护单</dt><dd>未核验</dd></div></dl></section>
        <button class="primary-action" data-action="preview-approval" type="button" @click="approvalOpen = true">演练审批记录</button>
        <p class="scope-note">命令只写入当前浏览器的本地演示状态，不发送请求，也不授予策略、增资或实盘权限。</p>
      </aside>
    </section>

    <section v-else-if="props.workspace === 'data-quality'" class="workspace-stack">
      <div class="section-heading coverage-controls">
        <div><p class="eyebrow">DATA COVERAGE</p><h2>覆盖与新鲜度</h2></div>
        <div class="coverage-filters">
          <label class="control-label">交易对筛选<select v-model="symbolFilter" aria-label="交易对筛选"><option value="all">全部交易对</option><option value="BTCUSDT">BTCUSDT</option><option value="ETHUSDT">ETHUSDT</option><option value="TRXUSDT">TRXUSDT</option></select></label>
          <label class="control-label">周期筛选<select v-model="timeframeFilter" aria-label="周期筛选"><option value="all">全部周期</option><option value="5m">5m</option><option value="15m">15m</option><option value="1h">1h</option></select></label>
          <button class="secondary-action" data-action="reset-coverage-filters" type="button" @click="resetCoverageFilters">重置筛选</button>
        </div>
      </div>
      <p class="coverage-scope" data-testid="coverage-scope">覆盖率、最近观测与质量均为交易对级摘要；周期筛选只匹配固定样本声明的支持范围。</p>
      <div v-if="filteredCoverage.length" class="table-scroll"><table data-testid="coverage-table"><thead><tr><th>交易对</th><th>支持周期</th><th>摘要覆盖率</th><th>最近观测</th><th>质量</th></tr></thead><tbody><tr v-for="row in filteredCoverage" :key="row.symbol"><td><strong>{{ row.symbol }}</strong></td><td>{{ row.timeframeLabel }}</td><td>{{ row.coverage }}</td><td>{{ row.freshness }}</td><td><span class="quality" :data-state="row.quality === '可观察' ? 'good' : 'warn'">{{ row.quality }}</span></td></tr></tbody></table></div>
      <div v-else class="coverage-empty" data-testid="coverage-empty" role="status">当前筛选下没有固定演示样本</div>
      <div class="collection-bar"><div><p class="eyebrow">LOCAL COLLECTION QUEUE</p><strong data-testid="collection-queue">{{ collectionQueue.length ? collectionQueue.join(' · ') : '队列为空' }}</strong></div><button class="secondary-action" data-action="queue-collection" type="button" @click="queueCollection">加入本地补采演练</button></div>
    </section>

    <section v-else-if="props.workspace === 'operations'" class="workspace-grid operations">
      <div class="main-column"><div class="section-heading"><div><p class="eyebrow">OPERATIONS LOG</p><h2>运行事件</h2></div><label class="control-label">日志级别<select v-model="logLevel" aria-label="日志级别"><option value="all">全部</option><option value="warning">警告</option><option value="info">信息</option></select></label></div><p class="log-scope" role="note">固定演示日志 · 北京时间 · 操作只追加到当前页面内存，不代表真实服务运行记录</p><div class="log-list" data-testid="log-list"><button v-for="log in filteredLogs" :key="log.id" :data-log="log.id" type="button" class="log-row" :class="{ active: selectedLog === log.id }" @click="selectedLog = log.id"><span class="log-time">{{ log.time }}</span><span class="log-level" :data-level="log.level">{{ log.level === 'warning' ? '警告' : '信息' }}</span><strong>{{ log.title }}</strong><span class="log-stage">阶段 · {{ log.stage }}</span><span class="log-result">结果 · {{ log.result }}</span><span class="log-id">{{ log.id }}</span></button></div></div>
      <aside class="side-column"><p class="eyebrow">EVENT INSPECTOR</p><h2>事件详情</h2><article v-if="currentLog && !eventIdIsInvalid && !eventIdIsMissing" class="detail-panel log-inspector" data-testid="log-detail"><span class="log-id">{{ currentLog.id }} · {{ currentLog.time }}</span><h3>{{ currentLog.title }}</h3><p>{{ currentLog.detail }}</p><dl class="log-fields"><div><dt>处理阶段</dt><dd>{{ currentLog.stage }}</dd></div><div><dt>处理结果</dt><dd>{{ currentLog.result }}</dd></div><div><dt>固定原因码</dt><dd><code>{{ currentLog.reason }}</code></dd></div><div><dt>样本耗时</dt><dd>{{ currentLog.duration }}</dd></div></dl><a class="history-locator" data-testid="history-locator" :href="currentLogUrl">打开此事件定位链接</a></article><div v-else-if="eventIdIsInvalid" class="history-state" data-testid="history-invalid" role="status">历史事件编号格式无效：{{ props.eventId }}</div><div v-else-if="eventIdIsMissing" class="history-state" data-testid="history-missing" role="status">固定演示样本中找不到事件 {{ props.eventId }}，没有其他历史记录可展示。</div><div v-else class="history-state" role="status">当前筛选条件下没有可展示的演示事件。</div><p class="scope-note">日志是本地合成证据，时间按北京时间显示；不会替代服务端审计记录。</p></aside>
    </section>

    <section v-else class="settings-panel">
      <div class="section-heading"><div><p class="eyebrow">LOCAL PREFERENCES</p><h2>演示偏好</h2></div><span class="muted">只保存到当前页面</span></div>
      <div class="preference-row"><label class="control-label" for="density">信息密度<select id="density" v-model="density" aria-label="信息密度"><option value="compact">紧凑</option><option value="comfortable">舒适</option></select></label><div class="preference-summary" data-testid="preference-summary"><span class="eyebrow">当前演示偏好</span><strong>{{ densityLabel }}</strong></div></div>
      <div class="button-row"><button class="primary-action" data-action="save-preferences" type="button" @click="savePreferences">保存本地偏好</button><button class="secondary-action" data-action="reset-preferences" type="button" @click="resetPreferences">重置</button></div>
    </section>

    <div v-if="approvalOpen" class="dialog-backdrop"><section class="dialog" role="dialog" aria-modal="true" aria-labelledby="approval-title"><p class="eyebrow">LOCAL ONLY</p><h2 id="approval-title">记录演示审批？</h2><p>这只是<strong>仅本地演练</strong>，不会改变生产资格，不会发送交易请求。</p><div class="button-row"><button class="secondary-action" data-action="cancel-approval" type="button" @click="approvalOpen = false">取消</button><button class="primary-action" data-action="confirm-approval" type="button" @click="recordApproval">确认记录</button></div></section></div>
    <p v-if="approvalRecorded" class="approval-note">本地回执：已记录演示审批，生产资格仍为未授权。</p>
  </section>
</template>

<style scoped>
.governance-preview { /* 运维证据页继承全站色标，并保留单页测试的固定回退值。 */ --bg:var(--qt-ocean,#0c1b2a); --surface:var(--qt-workspace,#132b3e); --surface-2:var(--qt-raised,#18364b); --line:var(--qt-divider,#29485c); --text:var(--qt-foreground,#e5edf4); --muted:var(--qt-muted,#a2b8c9); --blue:var(--qt-interactive,#69b4e4); --green:var(--qt-profit,#49c7a5); --warn:var(--qt-attention,#e8b65c); min-height:100%; padding:28px clamp(18px,4vw,56px) 56px; color:var(--text); background:var(--bg); font-family:var(--qt-ui-font,'IBM Plex Sans','Source Han Sans SC','Noto Sans SC',sans-serif); font-variant-numeric:tabular-nums; }
.governance-preview *, .governance-preview *::before, .governance-preview *::after { box-sizing:border-box; }
h1,h2,h3,p,dl,dd { margin:0; } h1{font-size:29px;font-weight:600;letter-spacing:.01em;} h2{font-size:18px;font-weight:600;} h3{font-size:15px;font-weight:600;margin:9px 0;} p{line-height:1.65;}
.preview-header,.section-heading,.collection-bar,.preference-row,.button-row,.detail-topline { display:flex; align-items:center; justify-content:space-between; gap:18px; }
.preview-header { padding-bottom:20px; border-bottom:1px solid var(--line); }
.eyebrow { color:var(--blue); font-size:11px; letter-spacing:.09em; line-height:1.5; }
.demo-badge { display:flex; align-items:center; gap:8px; color:var(--muted); font-size:12px; } .badge-dot { width:8px;height:8px;border-radius:50%;background:var(--warn); box-shadow:0 0 0 3px rgba(240,193,123,.16); }
.status-line { margin:18px 0; padding:11px 14px; border-left:2px solid var(--blue); color:var(--muted); background:rgba(19,43,62,.66); font-size:12px; }
.workspace-grid { display:grid; grid-template-columns:minmax(0,1fr) 300px; gap:30px; } .workspace-stack,.settings-panel { display:grid; gap:20px; } .main-column,.side-column{min-width:0;}
.section-heading { align-items:flex-end; } .section-heading .eyebrow { margin-bottom:4px; } .muted,.scope-note{ color:var(--muted); font-size:12px; }
.evidence-list,.log-list { display:grid; border-top:1px solid var(--line); }
.evidence-row,.log-row { appearance:none; width:100%; border:0; border-bottom:1px solid var(--line); color:var(--text); background:transparent; text-align:left; cursor:pointer; }
.evidence-row { display:grid; grid-template-columns:minmax(110px,1fr) auto 22px; align-items:center; gap:14px; padding:17px 12px; } .evidence-row:hover,.evidence-row.active,.log-row:hover,.log-row.active{background:var(--surface);} .evidence-label{color:var(--muted);font-size:13px;} .evidence-row strong{font-size:16px;font-weight:500;} .chevron{color:var(--blue);font-size:22px;}
.detail-panel,.fact-panel,.settings-panel { border:1px solid var(--line); background:var(--surface); } .detail-panel{margin-top:20px;padding:22px;} .detail-topline{align-items:flex-start;} .signal{color:var(--green);font-size:11px;} .detail-value{display:block;margin:24px 0 7px;font-size:31px;font-weight:500;} .detail-panel p{color:var(--muted);font-size:13px;}
.side-column { display:grid; align-content:start; gap:15px; border-left:1px solid var(--line); padding-left:24px; } .fact-panel{padding:20px;} .fact-panel h2{margin:6px 0 18px;} .gate-state{display:flex;gap:11px;align-items:flex-start;padding:13px;border:1px solid rgba(240,193,123,.45);background:rgba(240,193,123,.07);} .gate-icon{display:grid;width:20px;height:20px;place-items:center;color:var(--warn);border:1px solid var(--warn);font-size:13px;} .gate-state strong{font-size:14px;color:var(--warn);} .gate-state p{margin-top:3px;color:var(--muted);font-size:11px;} .facts{display:grid;gap:10px;margin-top:18px;font-size:12px;} .facts div{display:flex;justify-content:space-between;gap:12px;} .facts dt{color:var(--muted);} .facts dd{text-align:right;}
.primary-action,.secondary-action { min-height:38px; padding:8px 14px; border:1px solid var(--blue); font:inherit; font-size:12px; cursor:pointer; } .primary-action{color:var(--bg);background:var(--blue);} .secondary-action{color:var(--text);background:var(--surface);} .primary-action:hover{filter:brightness(1.08);} .secondary-action:hover{border-color:var(--blue);color:var(--blue);} button:focus-visible,select:focus-visible{outline:2px solid var(--blue);outline-offset:3px;}
.scope-note{line-height:1.65;} .history-locator{display:inline-block;margin-top:15px;color:var(--blue);font-size:12px;text-decoration:underline;text-underline-offset:3px;} .history-state{padding:16px;border-left:2px solid var(--warn);color:var(--muted);background:var(--surface);font-size:12px;line-height:1.65;overflow-wrap:anywhere;}
.control-label{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:12px;} select{min-width:130px;padding:8px 28px 8px 9px;border:1px solid var(--line);color:var(--text);background:var(--surface);font:inherit;font-size:12px;}
.coverage-scope{margin:-2px 0 12px;color:var(--muted);font-size:11px;}
.coverage-filters{display:flex;align-items:flex-end;gap:12px;} .coverage-empty{display:grid;min-height:170px;place-items:center;border:1px solid var(--line);padding:20px;color:var(--muted);background:rgba(19,43,62,.45);text-align:center;}
.table-scroll{overflow-x:auto;border-top:1px solid var(--line);border-bottom:1px solid var(--line);} table{width:100%;min-width:650px;border-collapse:collapse;font-size:12px;} th,td{padding:13px 14px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap;} th{color:var(--muted);font-weight:500;background:var(--surface);} tbody tr:last-child td{border-bottom:0;} .quality{color:var(--warn);} .quality[data-state='good']{color:var(--green);} .collection-bar{padding:16px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line);} .collection-bar strong{display:block;margin-top:5px;font-size:14px;font-weight:500;}
.log-scope{margin:12px 0;color:var(--muted);font-size:11px;} .log-row{display:grid;grid-template-columns:78px 54px minmax(0,1fr) 118px 142px;align-items:center;gap:10px;padding:13px 12px;font-size:12px;} .log-time,.log-id{color:var(--muted);font-family:'IBM Plex Mono',monospace;font-size:11px;} .log-level{color:var(--green);}.log-level[data-level='warning']{color:var(--warn);} .log-row strong{font-weight:500;} .log-stage,.log-result{grid-column:3;color:var(--muted);font-size:10px;} .log-id{grid-column:5;grid-row:1 / span 2;text-align:right;} .log-fields{display:grid;gap:0;margin-top:18px;border-top:1px solid var(--line);font-size:11px;} .log-fields div{display:grid;grid-template-columns:78px minmax(0,1fr);gap:9px;padding:9px 0;border-bottom:1px solid var(--line);} .log-fields dt{color:var(--muted);} .log-fields dd{min-width:0;margin:0;text-align:right;overflow-wrap:anywhere;} .log-fields code{color:var(--blue);font-family:'IBM Plex Mono',monospace;font-size:10px;}
.operations .detail-panel{margin-top:0;} .settings-panel{padding:24px;}.preference-row{align-items:flex-end;padding:22px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line);} .preference-summary{display:grid;gap:6px;min-width:170px;} .preference-summary strong{font-size:22px;font-weight:500;}
.dialog-backdrop{position:fixed;inset:0;z-index:20;display:grid;place-items:center;padding:20px;background:rgba(5,13,22,.76);} .dialog{width:min(430px,100%);padding:25px;border:1px solid var(--blue);background:var(--surface);box-shadow:0 18px 50px rgba(0,0,0,.35);} .dialog h2{margin:7px 0 10px;} .dialog p{color:var(--muted);font-size:13px;} .dialog p strong{color:var(--warn);font-weight:500;} .dialog .button-row{justify-content:flex-end;margin-top:24px;} .approval-note{margin-top:15px;color:var(--green);font-size:12px;}
@media (max-width:800px){.governance-preview{padding:22px 16px 42px}.preview-header{align-items:flex-start;flex-direction:column;gap:12px}.workspace-grid{grid-template-columns:1fr;gap:24px}.side-column{border-left:0;border-top:1px solid var(--line);padding:22px 0 0}.section-heading{align-items:flex-start;flex-direction:column;gap:12px}.coverage-filters{width:100%;align-items:stretch;flex-direction:column}.coverage-filters .secondary-action{width:100%}.collection-bar,.preference-row{align-items:flex-start;flex-direction:column;gap:16px}.collection-bar .secondary-action{width:100%}.control-label{width:100%;justify-content:space-between}.control-label select{flex:1;max-width:190px}.log-row{grid-template-columns:68px 48px minmax(0,1fr);grid-template-rows:auto auto auto;}.log-row strong{grid-column:3;grid-row:1}.log-stage{grid-column:1 / span 2;grid-row:2}.log-result{grid-column:3;grid-row:2}.log-id{grid-column:3;grid-row:3;text-align:right}.settings-panel{padding:18px 16px}}
@media (max-width:430px){h1{font-size:25px}.evidence-row{grid-template-columns:1fr auto;gap:5px 10px}.evidence-row strong{grid-column:1}.chevron{grid-column:2;grid-row:1 / span 2}.detail-value{font-size:27px}.button-row{align-items:stretch;flex-direction:column}.button-row button{width:100%}.dialog .button-row{flex-direction:column-reverse}}
</style>

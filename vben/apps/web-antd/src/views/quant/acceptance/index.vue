<script lang="ts" setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';

import { Alert, Button, Empty, Spin, Table, Tabs, Tag } from 'ant-design-vue';

import { getBinanceDemoEvidence, getBinanceDemoFills, getBinanceDemoOrders, getBinanceDemoPositions, getBinanceDemoStatus } from '#/api/binance-demo';
import type { DemoEvidence, DemoFill, DemoOrder, DemoStatus } from '#/api/binance-demo';

import { readSessionSnapshot } from '../trade-desk/sessionSnapshot';
import { beijingTime, environmentLabel, fillDirection, guardState, numberText, orderState, positionDirection, snapshotState } from '../trade-desk/tradeDeskDisplay';
import { acceptanceDecision, evidenceMetric, evidenceState } from './acceptanceDisplay';

const status = ref<DemoStatus | null>(null);
const evidence = ref<DemoEvidence | null>(null);
const fills = ref<DemoFill[]>([]);
const orders = ref<DemoOrder[]>([]);
const positions = ref<Record<string, unknown>[]>([]);
const error = ref('');
const loading = ref(false);
const updatedAt = ref<null | number>(null);
const now = ref(Date.now());
const activeTable = ref('positions');
let timer: ReturnType<typeof setInterval> | undefined;
let controller: AbortController | undefined;
let requestId = 0;

// 当前只读 API 尚未发布轮次裁定；成交证据不替代完整日结裁定。
const verdict = computed(() => acceptanceDecision(null));
const environment = computed(() => environmentLabel(status.value?.environment, status.value?.endpoint));
const risk = computed(() => guardState(status.value?.runner));
const freshness = computed(() => error.value
  ? { label: status.value ? '保留上次数据，刷新失败' : '尚未取得数据', color: 'warning' }
  : loading.value && status.value
    ? { label: '正在刷新，显示上次数据', color: 'warning' }
    : snapshotState(status.value?.runner, now.value));
const feeStatus = computed(() => evidenceState(evidence.value?.questions.fees.state ?? 'unknown'));
const executionStatus = computed(() => evidenceState(evidence.value?.questions.execution.state ?? 'unknown'));
const feeMetric = computed(() => evidenceMetric(
  evidence.value?.questions.fees.state ?? 'unknown',
  evidence.value && evidence.value.questions.fees.fillCount > 0
    ? `${evidence.value.questions.fees.knownFeeFillCount} / ${evidence.value.questions.fees.fillCount} 笔成交已知`
    : null,
));
const realizedPnl = computed(() => evidenceMetric(
  evidence.value?.questions.secondsProfit.state ?? 'unknown',
  evidence.value?.questions.secondsProfit.closedNetTradingPnlUsdt,
));

function positionKey(row: Record<string, unknown>, index?: number) {
  return String(row.symbol ?? 'unknown') + ':' + String(row.positionSide ?? index ?? 'unknown');
}

function orderKey(row: Record<string, unknown>, index?: number) {
  return String(row.symbol ?? 'unknown') + ':' + String(row.clientOrderId ?? row.orderId ?? index ?? 'unknown');
}

function fillKey(row: Record<string, unknown>, index?: number) {
  return String(row.symbol ?? 'unknown') + ':' + String(row.id ?? index ?? 'unknown');
}

async function load() {
  if (loading.value) return;
  loading.value = true;
  error.value = '';
  controller?.abort();
  const nextController = new AbortController();
  controller = nextController;
  const current = ++requestId;
  try {
    const signal = nextController.signal;
    // 固定历史记录的会话身份，整组读取完成后再次核对，避免策略切换混账。
    const snapshot = await readSessionSnapshot(
      () => getBinanceDemoStatus(signal),
      async (session) => {
        const [nextEvidence, nextOrders, nextFills, nextPositions] = await Promise.all([
          getBinanceDemoEvidence(signal, session), getBinanceDemoOrders(signal, session),
          getBinanceDemoFills(signal, session), getBinanceDemoPositions(signal),
        ]);
        return { nextEvidence, nextOrders, nextFills, nextPositions };
      },
      signal,
    );
    if (current !== requestId || signal.aborted) return;
    status.value = snapshot.status;
    evidence.value = snapshot.data.nextEvidence;
    orders.value = snapshot.data.nextOrders.items;
    fills.value = snapshot.data.nextFills.items;
    positions.value = snapshot.data.nextPositions.items;
    updatedAt.value = Date.now();
  } catch (reason) {
    if (current === requestId && !nextController.signal.aborted) {
      error.value = reason instanceof Error ? reason.message : 'Demo 验收证据读取失败';
    }
  } finally {
    if (current === requestId) {
      now.value = Date.now();
      loading.value = false;
    }
  }
}

onMounted(() => {
  void load();
  timer = setInterval(() => { now.value = Date.now(); void load(); }, 30_000);
});
onUnmounted(() => {
  requestId += 1;
  controller?.abort();
  if (timer) clearInterval(timer);
});
</script>

<template>
  <main class="acceptance-page">
    <header class="acceptance-header">
      <div>
        <p class="environment">{{ environment }} · 验收与上线</p>
        <h1>30 天验收</h1>
      </div>
      <div class="header-actions">
        <span>页面刷新 {{ beijingTime(updatedAt) }}</span>
        <Button :loading="loading" @click="load">刷新证据</Button>
      </div>
    </header>

    <Alert v-if="error" type="error" show-icon :message="error" description="本次响应未发布。已有数据保留为上次快照，请核对会话身份后刷新。" />

    <section class="fact-band" aria-label="事实时间与运行风险">
      <div><span>运行风险</span><Tag :color="risk.color">{{ risk.label }}</Tag></div>
      <div><span>数据状态</span><Tag :color="freshness.color">{{ freshness.label }}</Tag></div>
      <div><span>服务循环时间</span><strong>{{ beijingTime(status?.runner.last_cycle_ms) }}</strong></div>
      <div><span>证据生成时间</span><strong>{{ evidence?.snapshotAt ?? '未知' }}</strong></div>
    </section>

    <section class="verdict-band" aria-labelledby="verdict-title">
      <div class="verdict-main">
        <div class="verdict-title-row">
          <h2 id="verdict-title">这轮是否达标</h2>
          <Tag :color="verdict.color">{{ verdict.label }}</Tag>
        </div>
        <p>尚未取得连续 30 个北京时间自然日的完整日结裁定。以下 Demo 成交和成本证据用于复核，实盘盈利仍未验证。</p>
        <p class="scope-note">当前页面不提供启动验收或资金升级命令。</p>
      </div>
      <div class="requirements">
        <div><strong>净收益</strong><span>≥ 1,500 USDT</span></div>
        <div><strong>完整往返</strong><span>≥ 100 笔</span></div>
        <div><strong>盈利日</strong><span>≥ 18 天</span></div>
        <div><strong>稳定性</strong><span>三个固定 10 日段均为正</span></div>
      </div>
    </section>

    <Spin :spinning="loading && !status">
      <div class="evidence-workspace">
        <section class="account-band" aria-label="Demo 账户身份">
          <dl>
            <div><dt>环境与端点</dt><dd>{{ environment }}<small>{{ status?.endpoint ?? '未知端点' }}</small></dd></div>
            <div><dt>策略身份</dt><dd>{{ status?.runner.selectedAccountId ?? '未选择' }}</dd></div>
            <div class="session-identity"><dt>账本会话</dt><dd class="identifier">{{ status?.runner.virtualAccountKey ?? '未绑定' }}</dd></div>
            <div><dt>账户状态</dt><dd>{{ status?.runner.state ?? '未知' }}</dd></div>
          </dl>
        </section>

      <section class="evidence-grid" aria-label="验收证据">
        <article class="evidence-section">
          <div class="section-heading"><h2>收益与费用</h2><Tag :color="feeStatus.color">{{ feeStatus.label }}</Tag></div>
          <dl>
            <div><dt>已平仓净交易损益</dt><dd>{{ realizedPnl }}<small>USDT，资金费未纳入</small></dd></div>
            <div><dt>手续费记录</dt><dd>{{ feeMetric }}</dd></div>
            <div><dt>资金费</dt><dd class="caution">证据缺失</dd></div>
            <div><dt>实盘盈利验证</dt><dd>未验证</dd></div>
          </dl>
        </article>
        <article class="evidence-section">
          <div class="section-heading"><h2>执行与对账</h2><Tag :color="executionStatus.color">{{ executionStatus.label }}</Tag></div>
          <dl>
            <div><dt>已接受入场</dt><dd>{{ evidence?.questions.execution.exchangeAcceptedEntryCount ?? '未知' }}</dd></div>
            <div><dt>终态零成交比例</dt><dd>{{ evidence?.questions.execution.acceptedTerminalNonfillRatio == null ? '未知' : `${(evidence.questions.execution.acceptedTerminalNonfillRatio * 100).toFixed(1)}%` }}</dd></div>
            <div><dt>未知提交</dt><dd>{{ evidence?.questions.execution.unknownEntryCount ?? '未知' }}</dd></div>
            <div><dt>上线许可</dt><dd>需要人工复核</dd></div>
          </dl>
        </article>
      </section>

      <section class="records-section" aria-label="账户与账本记录">
        <div class="section-heading"><h2>账户与账本记录</h2><span class="scope-note">订单与成交固定到上方会话</span></div>
        <Tabs v-model:active-key="activeTable">
          <Tabs.TabPane key="positions" :tab="'共享账户持仓 (' + positions.length + ')'">
          <p class="scope-note table-note">这里是共享交易账户的总持仓，不等同于当前策略持仓。持仓响应未提供独立交易所观测时间。</p>
          <Table size="small" :scroll="{ x: 660 }" :pagination="false" :data-source="positions" :row-key="positionKey">
            <Table.Column title="交易对" data-index="symbol" />
            <Table.Column title="方向"><template #default="{ record }">{{ positionDirection(record) }}</template></Table.Column>
            <Table.Column title="数量"><template #default="{ record }">{{ numberText(record.positionAmt) }}</template></Table.Column>
            <Table.Column title="未实现损益 / USDT"><template #default="{ record }">{{ numberText(record.unRealizedProfit) }}</template></Table.Column>
            <template #emptyText><Empty description="持仓列表为空，请结合数据状态核对" /></template>
          </Table>
          </Tabs.TabPane>
          <Tabs.TabPane key="orders" :tab="'账本订单 (' + orders.length + ')'">
          <p class="scope-note table-note">账本状态不能证明交易所原生条件保护仍有效；未知提交需要查询确认。</p>
          <Table size="small" :scroll="{ x: 760 }" :pagination="false" :data-source="orders" :row-key="orderKey">
            <Table.Column title="交易对" data-index="symbol" />
            <Table.Column title="方向"><template #default="{ record }">{{ fillDirection(record) }}</template></Table.Column>
            <Table.Column title="类型" data-index="type" />
            <Table.Column title="状态"><template #default="{ record }"><Tag :color="orderState(record).color">{{ orderState(record).label }}</Tag></template></Table.Column>
            <Table.Column title="已成交数量"><template #default="{ record }">{{ numberText(record.executedQty) }}</template></Table.Column>
            <template #emptyText><Empty description="当前会话没有返回订单记录" /></template>
          </Table>
          </Tabs.TabPane>
          <Tabs.TabPane key="fills" :tab="'最近成交 (' + fills.length + ')'">
          <p class="scope-note table-note">最多展示最近 20 笔。缺失手续费保留为未知，不按配置费率补算。</p>
          <Table size="small" :scroll="{ x: 820 }" :pagination="false" :data-source="fills.slice(0, 20)" :row-key="fillKey">
            <Table.Column title="交易对" data-index="symbol" />
            <Table.Column title="方向"><template #default="{ record }">{{ fillDirection(record) }}</template></Table.Column>
            <Table.Column title="价格"><template #default="{ record }">{{ numberText(record.price) }}</template></Table.Column>
            <Table.Column title="数量"><template #default="{ record }">{{ numberText(record.qty) }}</template></Table.Column>
            <Table.Column title="手续费"><template #default="{ record }">{{ numberText(record.commission) }} {{ record.commissionAsset ?? '资产未知' }}</template></Table.Column>
            <Table.Column title="成交时间 / 北京"><template #default="{ record }">{{ beijingTime(record.time) }}</template></Table.Column>
            <template #emptyText><Empty description="当前会话没有返回成交记录" /></template>
          </Table>
          </Tabs.TabPane>
        </Tabs>
      </section>
      </div>
    </Spin>
  </main>
</template>

<style scoped>
.acceptance-page { --workspace: #132b3e; --line: #29485c; --muted: #a2b8c9; --interactive: #69b4e4; display: grid; gap: 22px; min-width: 0; padding: 24px; color: #e5edf4; background: #0c1b2a; font-family: 'IBM Plex Sans', 'Noto Sans SC', 'Source Han Sans SC', sans-serif; }
.acceptance-header, .header-actions, .verdict-title-row, .section-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.acceptance-header { align-items: end; border-bottom: 1px solid var(--line); padding-bottom: 20px; }
h1, h2, p, dl, dd { margin: 0; }
h1 { font-size: 28px; font-weight: 600; line-height: 1.25; }
h2 { font-size: 17px; font-weight: 600; line-height: 1.4; }
.environment { margin-bottom: 8px; color: var(--interactive); font-size: 13px; }
.header-actions { flex-wrap: wrap; justify-content: flex-end; color: var(--muted); font-size: 12px; }
.fact-band { display: grid; grid-template-columns: 1fr 1fr 1.3fr 1.3fr; gap: 16px; padding: 16px 18px; border: 1px solid var(--line); background: var(--workspace); }
.fact-band > div { display: grid; align-content: start; justify-items: start; gap: 7px; min-width: 0; }
.fact-band span, .scope-note, dt { color: var(--muted); font-size: 12px; }
.fact-band strong { font-size: 13px; font-weight: 400; overflow-wrap: anywhere; }
.verdict-band { display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(320px, 1fr); gap: 32px; padding: 24px 22px; border-left: 4px solid var(--interactive); background: var(--workspace); }
.verdict-main { display: grid; align-content: start; gap: 14px; }
.verdict-title-row { justify-content: flex-start; flex-wrap: wrap; gap: 12px; }
.verdict-main p { max-width: 64ch; color: var(--muted); line-height: 1.7; font-size: 14px; }
.verdict-main .scope-note { font-size: 12px; }
.requirements { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px 24px; align-content: center; }
.requirements > div { display: grid; gap: 6px; border-bottom: 1px solid var(--line); padding-bottom: 10px; }
.requirements strong { color: var(--muted); font-size: 12px; font-weight: 400; }
.requirements span { font-size: 14px; font-variant-numeric: tabular-nums; }
.evidence-workspace { display: grid; min-width: 0; gap: 26px; }
.account-band { border-bottom: 1px solid var(--line); padding-bottom: 20px; }
.account-band dl { display: grid; grid-template-columns: 1.2fr 1fr 1.6fr .7fr; gap: 18px; }
.account-band dl > div { display: grid; align-content: start; gap: 7px; min-width: 0; }
.account-band dd { font-size: 13px; overflow-wrap: anywhere; }
small { display: block; margin-top: 4px; color: var(--muted); font-size: 12px; }
.identifier { font-family: 'IBM Plex Mono', 'SFMono-Regular', Consolas, monospace; font-size: 12px !important; }
.evidence-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 32px; }
.evidence-section { display: grid; min-width: 0; gap: 18px; border-bottom: 1px solid var(--line); padding-bottom: 18px; }
.evidence-section dl { display: grid; gap: 14px; }
.evidence-section dl > div { display: grid; grid-template-columns: minmax(100px, 1fr) minmax(0, 1.4fr); gap: 14px; }
.evidence-section dd { text-align: right; font-size: 14px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.caution { color: #efbf77; }
.records-section { display: grid; min-width: 0; gap: 8px; }
.records-section > .section-heading { flex-wrap: wrap; }
.table-note { margin-bottom: 14px; line-height: 1.65; }
:deep(.ant-spin-nested-loading), :deep(.ant-spin-container), :deep(.ant-tabs), :deep(.ant-tabs-content-holder), :deep(.ant-tabs-tabpane) { min-width: 0; }
:deep(.ant-table), :deep(.ant-table-container), :deep(.ant-table-tbody > tr > td) { color: #e5edf4; background: var(--workspace); border-color: var(--line); }
:deep(.ant-table-thead > tr > th) { color: var(--muted); background: #102436; border-color: var(--line); white-space: nowrap; }
:deep(.ant-table-tbody > tr.ant-table-row:hover > td) { background: #1a374c; }
:deep(.ant-table-cell) { font-variant-numeric: tabular-nums; }
:deep(.ant-tabs-nav::before) { border-color: var(--line); }
:deep(.ant-tabs-tab) { color: var(--muted); }
:deep(.ant-tabs-tab-active .ant-tabs-tab-btn) { color: var(--interactive); }
:deep(.ant-tabs-ink-bar) { background: var(--interactive); }
:deep(.ant-empty-description) { color: var(--muted); }
:deep(.ant-empty-img-simple-path) { fill: var(--workspace); }
:deep(.ant-btn-default) { color: #e5edf4; border-color: #4a7188; background: var(--workspace); box-shadow: none; }
:deep(.ant-btn-default:hover) { color: var(--interactive); border-color: var(--interactive); }
:deep(.ant-btn:focus-visible), :deep(.ant-tabs-tab-btn:focus-visible) { outline: 2px solid var(--interactive); outline-offset: 3px; }
:deep(.ant-tag) { margin-inline-end: 0; white-space: normal; }
@media (max-width: 1000px) {
  .fact-band { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .account-band dl { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .verdict-band { grid-template-columns: 1fr; gap: 24px; }
}
@media (max-width: 720px) {
  .acceptance-page { padding: 16px; gap: 18px; }
  .acceptance-header { align-items: start; flex-direction: column; gap: 16px; }
  .header-actions { width: 100%; justify-content: space-between; }
  .evidence-grid { grid-template-columns: 1fr; gap: 22px; }
  .fact-band { gap: 18px 12px; padding: 14px; }
  .verdict-band { padding: 20px 16px; }
  .requirements { gap: 16px; }
  .requirements span { font-size: 13px; }
}
@media (max-width: 420px) {
  .acceptance-page { padding: 14px 12px; }
  h1 { font-size: 25px; }
  .account-band dl { gap: 18px 14px; }
  .session-identity { grid-column: 1 / -1; }
  .header-actions { font-size: 11px; }
}
</style>

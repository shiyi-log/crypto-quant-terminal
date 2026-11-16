<script lang="ts" setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';

import { Alert, Button, Empty, Spin, Table, Tag } from 'ant-design-vue';

import type { DemoAccountSnapshot, DemoFill, DemoOrder, DemoStatus } from '#/api/binance-demo';

import { getBinanceDemoAccount, getBinanceDemoFills, getBinanceDemoOrders, getBinanceDemoPositions, getBinanceDemoStatus } from '#/api/binance-demo';

import { readSessionSnapshot } from './sessionSnapshot';
import { beijingTime, environmentLabel, fillDirection, guardState, numberText, orderState, positionDirection, riskFacts, snapshotState } from './tradeDeskDisplay';

const status = ref<DemoStatus | null>(null);
const account = ref<DemoAccountSnapshot | null>(null);
const positions = ref<Record<string, unknown>[]>([]);
const orders = ref<DemoOrder[]>([]);
const fills = ref<DemoFill[]>([]);
const loading = ref(false);
const error = ref('');
const updatedAt = ref<number | null>(null);
const now = ref(Date.now());
let refreshTimer: ReturnType<typeof setInterval> | undefined;
let clockTimer: ReturnType<typeof setInterval> | undefined;
let controller: AbortController | undefined;
let requestId = 0;

const environment = computed(() => environmentLabel(status.value?.environment, status.value?.endpoint));
const protection = computed(() => guardState(status.value?.runner));
const facts = computed(() => riskFacts(status.value?.runner));
const freshness = computed(() => error.value
  ? { label: status.value ? '读取失败 · 保留旧数据' : '尚未取得数据', color: 'error' }
  : loading.value && status.value
    ? { label: '正在刷新 · 显示上次数据', color: 'warning' }
    : snapshotState(status.value?.runner, now.value));
const riskPolicy = computed(() => {
  const value = status.value?.runner.risk_policy;
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
});
const riskReason = computed(() => {
  const reasons: Record<string, string> = {
    terminated: '硬风险条件已触发，本轮失败',
    lock_until_reconciled: '撤单和平仓后仍需完成对账',
    stop_until_next_day: '当天新开仓保持关闭',
    cooldown: '风险冷却期间暂停新增风险',
    blocked_evidence_incomplete: '权益、日初或资金事实不完整',
    enabled: '风险服务允许新增风险，仍需逐笔预检',
  };
  return reasons[String(riskPolicy.value.entries_mode)] ?? '尚未收到完整风险状态';
});
const cooldownRemaining = computed(() => {
  const until = riskPolicy.value.cooldown_until_ms;
  if (typeof until !== 'number') return '未知';
  const seconds = Math.max(0, Math.ceil((until - now.value) / 1000));
  return seconds ? `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分 ${seconds % 60} 秒` : '未在定时冷却中';
});

function rowKey(row: Record<string, unknown>, index?: number) {
  return `${String(row.symbol ?? '')}:${String(row.clientOrderId ?? row.id ?? row.orderId ?? row.positionSide ?? index)}`;
}

function direction(row: Record<string, unknown>) {
  const value = fillDirection(row);
  return value === '买入' ? '↑ 买入' : value === '卖出' ? '↓ 卖出' : value;
}

async function load() {
  if (loading.value) return;
  loading.value = true;
  error.value = '';
  controller?.abort();
  const active = new AbortController();
  controller = active;
  const current = ++requestId;
  try {
    // 共享账户快照与固定会话账本一起发布；会话切换后的旧响应全部丢弃。
    const result = await readSessionSnapshot(
      () => getBinanceDemoStatus(active.signal),
      (session) => Promise.all([
        getBinanceDemoAccount(active.signal), getBinanceDemoPositions(active.signal),
        getBinanceDemoOrders(active.signal, session), getBinanceDemoFills(active.signal, session),
      ]), active.signal,
    );
    if (current !== requestId || active.signal.aborted) return;
    const [nextAccount, nextPositions, nextOrders, nextFills] = result.data;
    status.value = result.status;
    account.value = nextAccount;
    positions.value = nextPositions.items;
    orders.value = nextOrders.items;
    fills.value = nextFills.items;
    updatedAt.value = Date.now();
  } catch (reason) {
    if (current === requestId && !active.signal.aborted) error.value = reason instanceof Error ? reason.message : '交易台读取失败';
  } finally {
    if (current === requestId) loading.value = false;
  }
}

onMounted(() => {
  void load();
  refreshTimer = setInterval(() => void load(), 10_000);
  clockTimer = setInterval(() => { now.value = Date.now(); }, 1000);
});
onUnmounted(() => {
  requestId += 1;
  controller?.abort();
  if (refreshTimer) clearInterval(refreshTimer);
  if (clockTimer) clearInterval(clockTimer);
});
</script>

<template>
  <main class="desk-page">
    <header class="desk-header">
      <div><p class="eyebrow">{{ environment }} ／ 交易台</p><h1>账户与风险</h1></div>
      <div class="header-actions"><Tag :color="freshness.color">{{ freshness.label }}</Tag><Button :loading="loading" @click="load">刷新事实</Button></div>
    </header>
    <Alert v-if="error" type="error" show-icon :message="error" description="下方保留最近一次成功读取的数据，请以原始事实时间核对，不代表当前账户。" />
    <section class="identity-band" aria-label="账户身份与数据时间">
      <div><span class="label">策略账户</span><strong>{{ status?.runner.selectedAccountId ?? '未选择' }}</strong><small class="identifier">{{ status?.runner.virtualAccountKey ?? '未绑定会话' }}</small></div>
      <div><span class="label">交易环境</span><strong>{{ environment }}</strong><small>{{ status?.endpoint ?? '未知端点' }}</small></div>
      <div class="fact-times"><span>服务周期 {{ beijingTime(status?.runner.last_cycle_ms) }}</span><span>账户观测 {{ beijingTime(account?.observedAt) }}</span><span>请求完成 {{ beijingTime(updatedAt) }} · 北京时间</span></div>
    </section>
    <section class="risk-band" aria-labelledby="risk-title">
      <div class="section-heading"><div><p class="eyebrow">ACCOUNT RISK</p><h2 id="risk-title">常驻风险栏</h2></div><Tag :color="protection.color">{{ protection.label }}</Tag></div>
      <dl class="risk-metrics">
        <div><dt>实测净权益 · USDT</dt><dd tabindex="0" aria-label="实测净权益完整金额，可横向滚动" :title="numberText(account?.account.totalMarginBalance)">{{ numberText(account?.account.totalMarginBalance) }}</dd><small>共享交易账户快照 · 长金额可横向查看</small></div>
        <div><dt>当日净损益 · USDT</dt><dd tabindex="0" aria-label="当日净损益完整金额，可横向滚动" :title="facts.dailyPnl">{{ facts.dailyPnl }}</dd><small>北京时间自然日 · 完整证据才计算</small></div>
        <div><dt>峰值回撤 · USDT</dt><dd tabindex="0" aria-label="峰值回撤完整金额，可横向滚动" :title="facts.drawdown">{{ facts.drawdown }}</dd><small>阶段上限 {{ numberText(riskPolicy.stage_drawdown_limit_usdt) }}</small></div>
        <div><dt>可用计划风险 · USDT</dt><dd>未知</dd><small>尚无事务风险预占汇总</small></div>
      </dl>
      <div class="risk-foot"><span>{{ riskReason }}</span><span>冷却剩余 {{ cooldownRemaining }} · 截止 {{ facts.cooldownUntil }}</span></div>
    </section>
    <Spin :spinning="loading && !status">
      <div class="workspace">
        <div class="ledger-column">
          <section class="data-section">
            <div class="section-heading"><div><p class="eyebrow">SHARED ACCOUNT</p><h2>当前持仓</h2></div><span>{{ positions.length }} 项</span></div>
            <p class="scope-note">共享交易账户快照；订单和成交固定到上方会话。持仓接口未提供独立观测时间。</p>
            <Table size="small" :pagination="false" :data-source="positions" :row-key="rowKey" :scroll="{ x: 760 }">
              <Table.Column title="交易对" data-index="symbol" />
              <Table.Column title="方向"><template #default="{ record }">{{ positionDirection(record) }}</template></Table.Column>
              <Table.Column title="数量"><template #default="{ record }">{{ numberText(record.positionAmt) }}</template></Table.Column>
              <Table.Column title="开仓均价"><template #default="{ record }">{{ numberText(record.entryPrice) }}</template></Table.Column>
              <Table.Column title="标记价"><template #default="{ record }">{{ numberText(record.markPrice) }}</template></Table.Column>
              <Table.Column title="未实现损益"><template #default="{ record }">{{ numberText(record.unRealizedProfit) }}</template></Table.Column>
              <template #emptyText><Empty description="未返回持仓记录" /></template>
            </Table>
          </section>
          <section class="data-section">
            <div class="section-heading"><div><p class="eyebrow">SESSION LEDGER</p><h2>订单与提交状态</h2></div><span>最近 {{ orders.length }} 项</span></div>
            <Table size="small" :pagination="false" :data-source="orders" :row-key="rowKey" :scroll="{ x: 760 }">
              <Table.Column title="交易对" data-index="symbol" />
              <Table.Column title="方向"><template #default="{ record }">{{ direction(record) }}</template></Table.Column>
              <Table.Column title="类型" data-index="type" />
              <Table.Column title="状态"><template #default="{ record }"><Tag :color="orderState(record).color">{{ orderState(record).label }}</Tag></template></Table.Column>
              <Table.Column title="数量"><template #default="{ record }">{{ numberText(record.origQty) }}</template></Table.Column>
              <Table.Column title="已成交"><template #default="{ record }">{{ numberText(record.executedQty) }}</template></Table.Column>
              <template #emptyText><Empty description="会话账本没有返回订单" /></template>
            </Table>
          </section>
          <section class="data-section">
            <div class="section-heading"><h2>最近成交</h2><span>展示 {{ Math.min(fills.length, 20) }} / 返回 {{ fills.length }} 笔</span></div>
            <Table size="small" :pagination="false" :data-source="fills.slice(0, 20)" :row-key="rowKey" :scroll="{ x: 760 }">
              <Table.Column title="交易对" data-index="symbol" />
              <Table.Column title="方向"><template #default="{ record }">{{ direction(record) }}</template></Table.Column>
              <Table.Column title="价格"><template #default="{ record }">{{ numberText(record.price) }}</template></Table.Column>
              <Table.Column title="数量"><template #default="{ record }">{{ numberText(record.qty) }}</template></Table.Column>
              <Table.Column title="手续费"><template #default="{ record }">{{ numberText(record.commission) }} {{ record.commissionAsset ?? '资产未知' }}</template></Table.Column>
              <Table.Column title="成交时间 · 北京"><template #default="{ record }">{{ beijingTime(record.time) }}</template></Table.Column>
              <template #emptyText><Empty description="会话账本没有返回成交" /></template>
            </Table>
          </section>
        </div>
        <aside class="protection-rail" aria-label="策略与保护">
          <section><p class="eyebrow">EXECUTION FACTS</p><h2>执行与保护</h2><dl>
            <div><dt>服务状态</dt><dd>{{ status?.runner.state ?? '未知' }}</dd></div>
            <div><dt>新增风险</dt><dd>{{ status ? (status.runner.tradingEnabled ? '开闸' : '关闭') : '未知' }}</dd></div>
            <div><dt>实际杠杆</dt><dd>{{ facts.leverage }}</dd></div>
            <div><dt>保证金模式</dt><dd>{{ facts.marginMode }}</dd></div>
            <div><dt>原生条件保护</dt><dd class="attention">未取得有效条件单证据</dd></div>
          </dl><p class="scope-note">账本订单不能证明交易所条件止损或止盈仍有效。当前页面没有条件单事实接口。</p></section>
          <section class="command-note"><h2>交易命令</h2><p>当前交易台为只读。开仓、撤单、平仓与资金升级需要完整风险预检和独立批准流程。</p></section>
        </aside>
      </div>
    </Spin>
  </main>
</template>

<style scoped>
.desk-page { --desk-bg: #0c1b2a; --desk-surface: #132b3e; --desk-border: #29485c; --desk-text: #e5edf4; --desk-muted: #a2b8c9; --desk-blue: #69b4e4; min-width: 0; display: grid; gap: 20px; padding: 24px; background: var(--desk-bg); color: var(--desk-text); font-family: 'IBM Plex Sans', 'Noto Sans SC', 'Source Han Sans SC', sans-serif; font-variant-numeric: tabular-nums; }
.desk-page *, .desk-page *::before, .desk-page *::after { box-sizing: border-box; }
h1, h2, p, dl, dd { margin: 0; }
h1 { margin-top: 7px; font-size: 28px; font-weight: 600; letter-spacing: .02em; }
h2 { font-size: 16px; font-weight: 600; }
.desk-header, .header-actions, .section-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.desk-header { border-bottom: 1px solid var(--desk-border); padding-bottom: 18px; }
.eyebrow { color: var(--desk-blue); font-size: 11px; font-weight: 500; letter-spacing: .08em; line-height: 1.5; }
.header-actions, .section-heading > span, .label, small, dt, .scope-note { color: var(--desk-muted); }
.identity-band { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr) minmax(260px, 1fr); gap: 24px; }
.identity-band > div { display: grid; gap: 5px; align-content: start; min-width: 0; }
.label { font-size: 12px; }
.identity-band strong { font-size: 16px; font-weight: 500; overflow-wrap: anywhere; }
.identifier { font-family: 'IBM Plex Mono', 'SFMono-Regular', monospace; overflow-wrap: anywhere; }
.fact-times { font-size: 11px; color: var(--desk-muted); line-height: 1.5; }
.risk-band { padding: 18px 20px; border: 1px solid var(--desk-border); border-top: 2px solid var(--desk-blue); background: var(--desk-surface); }
.risk-metrics { display: grid; grid-template-columns: minmax(0, 1.4fr) repeat(3, minmax(0, 1fr)); gap: 24px; padding: 20px 0; }
.risk-metrics > div { min-width: 0; border-left: 1px solid var(--desk-border); padding-left: 16px; }
.risk-metrics > div:first-child { border-left: none; padding-left: 0; }
.risk-metrics dt { font-size: 12px; }
/* 金额不舍入、不拆数字；窄列在自身范围滚动，键盘焦点可查看完整精度。 */
.risk-metrics dd { max-width: 100%; overflow-x: auto; white-space: nowrap; margin: 5px 0; font-size: clamp(20px, 2vw, 29px); font-weight: 500; line-height: 1.25; scrollbar-width: thin; scrollbar-color: var(--desk-border) transparent; }
.risk-metrics dd:focus-visible { outline: 2px solid var(--desk-blue); outline-offset: 3px; }
.risk-metrics small { font-size: 11px; }
.risk-foot { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 10px 24px; border-top: 1px solid var(--desk-border); padding-top: 12px; color: var(--desk-muted); font-size: 12px; line-height: 1.5; }
.workspace { display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: 24px; }
.ledger-column, .data-section, .protection-rail { min-width: 0; }
.ledger-column { display: grid; gap: 24px; }
.data-section { display: grid; gap: 12px; padding-bottom: 20px; border-bottom: 1px solid var(--desk-border); }
.section-heading { font-size: 12px; }
.section-heading .eyebrow { margin-bottom: 4px; }
.scope-note { font-size: 12px; line-height: 1.65; }
.protection-rail { align-self: start; display: grid; gap: 24px; border-left: 1px solid var(--desk-border); padding-left: 24px; }
.protection-rail h2 { margin: 5px 0 16px; }
.protection-rail dl { display: grid; gap: 14px; margin-bottom: 16px; font-size: 12px; }
.protection-rail dl div { display: flex; justify-content: space-between; gap: 16px; }
.protection-rail dd { max-width: 60%; text-align: right; overflow-wrap: anywhere; }
.attention { color: #f0c17b; }
.command-note { border-top: 1px solid var(--desk-border); padding-top: 20px; }
.command-note p { color: var(--desk-muted); font-size: 12px; line-height: 1.7; }
.desk-page :deep(.ant-table) { color: var(--desk-text); background: var(--desk-bg); font-family: inherit; }
.desk-page :deep(.ant-table-thead > tr > th), .desk-page :deep(.ant-table-cell) { border-color: var(--desk-border); }
.desk-page :deep(.ant-table-thead > tr > th) { color: var(--desk-muted); background: var(--desk-surface); font-size: 12px; font-weight: 500; }
.desk-page :deep(.ant-table-tbody > tr:hover > td) { background: var(--desk-surface); }
.desk-page :deep(.ant-table-placeholder:hover > td) { background: var(--desk-bg); }
.desk-page :deep(.ant-empty-description) { color: var(--desk-muted); }
.desk-page :deep(.ant-btn-default) { border-color: var(--desk-border); background: var(--desk-surface); color: var(--desk-text); }
.desk-page :deep(.ant-btn-default:hover) { border-color: var(--desk-blue); color: var(--desk-blue); }
.desk-page :deep(.ant-btn:focus-visible) { outline: 2px solid var(--desk-blue); outline-offset: 3px; }
.desk-page :deep(.ant-tag) { white-space: normal; }
.desk-page :deep(.ant-spin-nested-loading), .desk-page :deep(.ant-spin-container), .desk-page :deep(.ant-table-wrapper) { min-width: 0; }
@media (max-width: 1200px) { .workspace { grid-template-columns: minmax(0, 1fr); } .protection-rail { grid-template-columns: 1fr 1fr; border-left: none; border-top: 1px solid var(--desk-border); padding: 20px 0 0; } .command-note { border-top: none; padding-top: 0; } }
@media (max-width: 900px) { .identity-band { grid-template-columns: 1fr 1fr; gap: 16px; } .fact-times { grid-column: 1 / -1; } .risk-metrics { grid-template-columns: 1fr 1fr; gap: 20px; } .risk-metrics > div:nth-child(3) { border-left: none; padding-left: 0; } }
@media (max-width: 600px) { .desk-page { gap: 16px; padding: 16px 12px; } .desk-header { align-items: start; flex-direction: column; } h1 { font-size: 24px; } .header-actions { width: 100%; } .identity-band { grid-template-columns: minmax(0, 1fr); } .risk-band { padding: 16px 12px; } .risk-metrics { gap: 20px 10px; } .risk-metrics > div { padding-left: 10px; } .risk-metrics dd { font-size: 20px; } .risk-metrics small { display: block; line-height: 1.5; } .protection-rail { grid-template-columns: minmax(0, 1fr); } .command-note { border-top: 1px solid var(--desk-border); padding-top: 16px; } }
</style>

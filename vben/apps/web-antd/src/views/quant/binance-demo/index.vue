<script lang="ts" setup>
import type { DemoFill, DemoOrder, DemoRecord, DemoStatus } from '#/api/binance-demo';

import { computed, onMounted, onUnmounted, reactive, ref } from 'vue';

import { Alert, Button, Card, Descriptions, Empty, Form, Input, message, Popconfirm, Select, Table, Tabs, Tag } from 'ant-design-vue';

import {
  checkBinanceDemoConnection, getBinanceDemoAccount, getBinanceDemoComparisons,
  getBinanceDemoFills, getBinanceDemoOrders, getBinanceDemoPositions,
  getBinanceDemoStatus, saveBinanceDemoCredentials, startBinanceDemo, stopBinanceDemo,
} from '#/api/binance-demo';

import { accountLabel, canStartDemo, comparisonObservation, decimalText, demoError, demoState, exchangeTime, fillSide, isSubmissionUnknown, makerText, orderRecordStatus, submitCredentials, unfilledRatio, valueText } from './display';

const status = ref<DemoStatus | null>(null);
const orders = ref<DemoOrder[]>([]);
const fills = ref<DemoFill[]>([]);
const positions = ref<DemoRecord[]>([]);
const balances = ref<DemoRecord[]>([]);
const comparisons = ref<DemoRecord[]>([]);
const credentials = reactive({ apiKey: '', secret: '' });
const selectedAccount = ref('');
const activeTab = ref('orders');
const loading = ref(false);
const action = ref('');
const error = ref('');
const recordError = ref('');
const updatedAt = ref<number | null>(null);
let timer: ReturnType<typeof setInterval> | undefined;
let controller: AbortController | undefined;
let sequence = 0;

const state = computed(() => demoState(status.value));
const running = computed(() => Boolean(status.value?.runner?.tradingEnabled || status.value?.runner?.state === 'running'));
const accountLocked = computed(() => Boolean(running.value || status.value?.runner?.workerRunning || status.value?.runner?.accountLocked));
const accounts = computed(() => (status.value?.accounts ?? []).map((account) => ({
  label: accountLabel(account), value: account.accountId,
  disabled: !account.enabled || !account.eligible,
})));
const selected = computed(() => status.value?.accounts?.find((account) => account.accountId === selectedAccount.value));
const startAllowed = computed(() => canStartDemo(status.value, selectedAccount.value) && !running.value);
const metrics = computed(() => status.value?.metrics ?? {});
const credentialsConfigured = computed(() => Boolean(status.value?.credentials?.configured));
const credentialsReadable = computed(() => credentialsConfigured.value && status.value?.credentials?.decryptionAvailable !== false);
const comparisonRows = computed(() => comparisons.value.map(comparisonObservation));
const roundTrips = computed(() => {
  const rows = status.value?.runner?.round_trips;
  return Array.isArray(rows) ? rows.filter((row): row is DemoRecord => Boolean(row) && typeof row === 'object') : [];
});
const actualFees = computed(() => {
  const rows = metrics.value.actual_commission_by_asset;
  return Array.isArray(rows) ? rows.filter((row): row is DemoRecord => Boolean(row) && typeof row === 'object') : [];
});
const riskAlert = computed(() => {
  const value = status.value?.runner?.risk_alert;
  const labels: Record<string, string> = {
    frozen_demo_entry_atr_unavailable: '入场 ATR 缺失，当前风险保护无法计算止损',
    closed_candle_unavailable_for_risk_protection: '风险保护所需已收盘 K 线不可用',
    actual_demo_entry_price_unavailable: '实际 Demo 入场均价尚未取得，当前风险保护无法计算止损',
  };
  return typeof value === 'string' ? labels[value] ?? `风险保护待核对：${value}` : '';
});
const ownershipConflicts = computed(() => {
  const values = status.value?.runner?.ownership_conflicts;
  return Array.isArray(values) ? values.filter((value): value is string => typeof value === 'string').join('；') : '';
});
const credentialRules = {
  apiKey: [{ required: true, message: '请输入 Demo API Key' }],
  secret: [{ required: true, message: '请输入 Demo Secret Key' }],
};

const orderColumns = [
  { title: '交易对 · symbol', dataIndex: 'symbol', key: 'symbol', width: 130 },
  { title: '订单号 · orderId', dataIndex: 'orderId', key: 'orderId', width: 170 },
  { title: '客户订单号 · clientOrderId', dataIndex: 'clientOrderId', key: 'clientOrderId', width: 240 },
  { title: '方向 · side', dataIndex: 'side', key: 'side', width: 95 },
  { title: '类型 · type', dataIndex: 'type', key: 'type', width: 110 },
  { title: '状态 · status', key: 'status', width: 180 },
  { title: '委托价 · price', key: 'price', width: 130 },
  { title: '委托数量 · origQty', key: 'origQty', width: 145 },
  { title: '已成交 · executedQty', key: 'executedQty', width: 150 },
  { title: '均价 · avgPrice', key: 'avgPrice', width: 140 },
  { title: '创建时间 · time', key: 'time', width: 185 },
  { title: '更新时间 · updateTime', key: 'updateTime', width: 185 },
];
const fillColumns = [
  { title: '交易对 · symbol', dataIndex: 'symbol', key: 'symbol', width: 125 },
  { title: '成交号 · id', dataIndex: 'id', key: 'id', width: 145 },
  { title: '订单号 · orderId', dataIndex: 'orderId', key: 'orderId', width: 165 },
  { title: '方向 · side', dataIndex: 'side', key: 'side', width: 95 },
  { title: '成交价 · price', key: 'price', width: 135 },
  { title: '成交量 · qty', key: 'qty', width: 130 },
  { title: '成交额 · quoteQty', key: 'quoteQty', width: 145 },
  { title: '手续费 · commission', key: 'commission', width: 175 },
  { title: '手续费资产 · commissionAsset', dataIndex: 'commissionAsset', key: 'commissionAsset', width: 185 },
  { title: '已实现损益 · realizedPnl', key: 'realizedPnl', width: 175 },
  { title: '成交身份 · maker', key: 'maker', width: 135 },
  { title: '成交时间 · time', key: 'time', width: 185 },
];
const positionColumns = [
  { title: '交易对 · symbol', dataIndex: 'symbol', key: 'symbol', width: 135 },
  { title: '仓位方向 · positionSide', dataIndex: 'positionSide', key: 'positionSide', width: 160 },
  { title: '仓位数量 · positionAmt', key: 'positionAmt', width: 175 },
  { title: '开仓均价 · entryPrice', key: 'entryPrice', width: 175 },
  { title: '标记价 · markPrice', key: 'markPrice', width: 160 },
  { title: '未实现损益 · unRealizedProfit', key: 'unRealizedProfit', width: 215 },
  { title: '强平价 · liquidationPrice', key: 'liquidationPrice', width: 190 },
  { title: '杠杆 · leverage', dataIndex: 'leverage', key: 'leverage', width: 125 },
  { title: '更新时间 · updateTime', key: 'updateTime', width: 185 },
];
const accountColumns = [
  { title: '资产 · asset', dataIndex: 'asset', key: 'asset', width: 125 },
  { title: '钱包余额 · walletBalance', key: 'walletBalance', width: 185 },
  { title: '可用余额 · availableBalance', key: 'availableBalance', width: 195 },
  { title: '保证金余额 · marginBalance', key: 'marginBalance', width: 195 },
  { title: '未实现损益 · unrealizedProfit', key: 'unrealizedProfit', width: 195 },
  { title: '更新时间 · updateTime', key: 'updateTime', width: 185 },
];
const comparisonColumns = [
  { title: '记录时间', key: 'observedAt', width: 185 },
  { title: '信号标识', dataIndex: 'signalId', key: 'signalId', width: 200 },
  { title: '交易对', dataIndex: 'pair', key: 'pair', width: 155 },
  { title: '对照事件阶段', dataIndex: 'stage', key: 'stage', width: 175 },
  { title: '内部模拟事件', dataIndex: 'paperEventType', key: 'paperEventType', width: 160 },
  { title: '内部模拟成交价', key: 'internalPrice', width: 160 },
  { title: '原因', dataIndex: 'reason', key: 'reason', width: 210 },
  { title: '输入指纹', dataIndex: 'inputFingerprint', key: 'inputFingerprint', width: 260, ellipsis: true },
];
const roundTripColumns = [
  { title: '交易对', dataIndex: 'pair', key: 'pair', width: 150 },
  { title: '开仓时间', key: 'opened_ms', width: 180 },
  { title: '平仓时间', key: 'closed_ms', width: 180 },
  { title: '持仓秒数', key: 'hold_seconds', width: 130 },
  { title: '实收毛损益（USDT）', key: 'actual_realized_pnl_usdt', width: 175 },
  { title: '手续费（按资产）', key: 'actual_commission_by_asset', width: 190 },
  { title: '净交易损益（USDT）', key: 'actual_net_trading_pnl_usdt', width: 180 },
  { title: '费用完整性', key: 'fee_complete', width: 150 },
];
const decimalKeys = new Set([
  'price', 'origQty', 'executedQty', 'avgPrice', 'qty', 'quoteQty', 'realizedPnl',
  'positionAmt', 'entryPrice', 'markPrice', 'unRealizedProfit', 'liquidationPrice',
  'walletBalance', 'availableBalance', 'marginBalance', 'unrealizedProfit',
]);

function rowKey(row: DemoRecord, index?: number) {
  return `${valueText(row.symbol)}:${valueText(row.id ?? row.orderId ?? row.signalId ?? row.asset)}:${index ?? 0}`;
}

function metric(key: string) {
  return valueText(metrics.value[key]);
}

function feesByAsset(value: unknown) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return '未知';
  const values = Object.entries(value as DemoRecord).map(([asset, amount]) => `${decimalText(amount)} ${asset}`);
  return values.length ? values.join(' · ') : '未知';
}

async function refresh() {
  controller?.abort();
  controller = new AbortController();
  const requestSequence = ++sequence;
  const signal = controller.signal;
  loading.value = true;
  const results = await Promise.allSettled([
    getBinanceDemoStatus(signal), getBinanceDemoOrders(signal), getBinanceDemoFills(signal),
    getBinanceDemoPositions(signal), getBinanceDemoAccount(signal), getBinanceDemoComparisons(signal),
  ]);
  if (requestSequence !== sequence) return;
  const [overview, orderResult, fillResult, positionResult, accountResult, comparisonResult] = results;
  if (overview?.status === 'fulfilled') {
    status.value = overview.value;
    if ((!selectedAccount.value || overview.value.runner?.accountLocked || overview.value.runner?.workerRunning) && overview.value.runner?.selectedAccountId) {
      selectedAccount.value = overview.value.runner.selectedAccountId;
    }
    error.value = '';
  } else {
    status.value = null;
    error.value = demoError(overview?.status === 'rejected' ? overview.reason : null);
  }
  orders.value = orderResult?.status === 'fulfilled' ? orderResult.value.items : [];
  fills.value = fillResult?.status === 'fulfilled' ? fillResult.value.items : [];
  positions.value = positionResult?.status === 'fulfilled' ? positionResult.value.items : [];
  balances.value = accountResult?.status === 'fulfilled' ? accountResult.value.items : [];
  comparisons.value = comparisonResult?.status === 'fulfilled' ? comparisonResult.value.items : [];
  const failed = results.slice(1).find((result) => result.status === 'rejected');
  recordError.value = failed?.status === 'rejected' ? `交易记录读取失败：${demoError(failed.reason)}` : '';
  updatedAt.value = Date.now();
  loading.value = false;
}

async function runAction(name: string, work: () => Promise<unknown>, success: string) {
  action.value = name;
  try {
    await work();
    message.success(success);
    await refresh();
  } catch (reason) {
    message.error(demoError(reason));
  } finally {
    action.value = '';
  }
}

function saveCredentials() {
  if (!credentials.apiKey.trim() || !credentials.secret.trim()) return;
  return runAction('save', () => submitCredentials(credentials, saveBinanceDemoCredentials), 'Demo 凭据已加密保存，输入框已清空');
}

function checkConnection() {
  return runAction('check', checkBinanceDemoConnection, '只读连接检查完成，请查看连接状态');
}

function start() {
  if (!startAllowed.value) return;
  return runAction('start', () => startBinanceDemo(selectedAccount.value), '已提交 Demo 启动请求');
}

function stop() {
  return runAction('stop', stopBinanceDemo, '已提交暂停新开仓请求，已有仓位继续风险保护');
}

onMounted(() => {
  void refresh();
  timer = setInterval(() => { if (!action.value && !loading.value) void refresh(); }, 15_000);
});
onUnmounted(() => {
  ++sequence;
  controller?.abort();
  if (timer) clearInterval(timer);
  credentials.apiKey = '';
  credentials.secret = '';
});
</script>

<template>
  <div class="demo-page p-4">
    <div class="demo-header mb-4">
      <div>
        <div class="flex items-center gap-3">
          <h1 class="text-xl font-semibold">币安 Demo Trading</h1>
          <Tag :color="state.color">{{ state.label }}</Tag>
        </div>
        <p class="mt-1 text-sm text-muted-foreground">官方模拟盘 · 实际订单与成交回报 · 内部模拟对照</p>
      </div>
      <Button :loading="loading" @click="refresh">刷新记录</Button>
    </div>

    <Alert v-if="error" class="mb-3" show-icon type="error" :message="error" />
    <Alert
      class="mb-4" show-icon type="info"
      message="Demo 的订单簿定价和执行可能与实盘不同。费用以每笔实际成交回报为准。"
      description="前向结果用于评估成交、费用和策略表现；当前不代表策略盈利或秒级交易已验证。上线须另经人工决定。"
    />

    <div class="grid grid-cols-1 gap-4 xl:grid-cols-2">
      <Card :bordered="false" title="Demo 密钥配置">
        <div class="mb-4 flex flex-wrap items-center gap-2 text-sm">
          <Tag :color="credentialsConfigured ? 'blue' : 'default'">{{ credentialsConfigured ? '已加密保存' : '尚未配置' }}</Tag>
          <span v-if="status?.credentials?.maskedApiKey" class="font-mono text-xs">{{ status.credentials.maskedApiKey }}</span>
          <span v-if="status?.credentials?.updatedAt" class="text-xs text-muted-foreground">更新 {{ exchangeTime(status.credentials.updatedAt) }}</span>
        </div>
        <Form :model="credentials" :rules="credentialRules" layout="vertical" @finish="saveCredentials">
          <Form.Item label="Demo API Key" name="apiKey">
            <Input.Password v-model:value="credentials.apiKey" autocomplete="off" placeholder="仅填写币安 Demo 的 API Key" :disabled="accountLocked || Boolean(action)" />
          </Form.Item>
          <Form.Item label="Demo Secret Key" name="secret">
            <Input.Password v-model:value="credentials.secret" autocomplete="new-password" placeholder="仅填写币安 Demo 的 Secret Key" :disabled="accountLocked || Boolean(action)" />
          </Form.Item>
          <div class="flex flex-wrap items-center gap-2">
            <Button html-type="submit" type="primary" :loading="action === 'save'" :disabled="accountLocked || Boolean(action && action !== 'save')">{{ credentialsConfigured ? '替换并加密保存' : '加密保存' }}</Button>
            <Button :loading="action === 'check'" :disabled="!credentialsReadable || Boolean(action && action !== 'check')" @click="checkConnection">只读连接检查</Button>
            <a href="https://demo.binance.com/en/my/settings/api-management" target="_blank" rel="noopener noreferrer" class="text-xs">创建 Demo 密钥 ↗</a>
          </div>
        </Form>
        <p class="mt-3 text-xs leading-5 text-muted-foreground">密钥加密保存在数据库，页面不回显完整密钥；提交后清空输入。Demo 账户绑定期间锁定凭据和策略，暂停新开仓后仍继续保护已有仓位。</p>
        <Alert v-if="credentialsConfigured && !credentialsReadable" class="mt-3" show-icon type="error" message="已保存配置暂不可解密，请检查本机加密服务；当前不能检查连接或启动交易。" />
      </Card>

      <Card :bordered="false" title="选择单个策略并前向运行">
        <Descriptions :column="1" size="small" class="mb-3">
          <Descriptions.Item label="连接环境">Binance Demo Trading</Descriptions.Item>
          <Descriptions.Item label="API 端点"><span class="font-mono text-xs">demo-fapi.binance.com</span></Descriptions.Item>
          <Descriptions.Item label="最近只读检查">{{ status?.lastCheck?.connected === true ? '连通' : status?.lastCheck?.connected === false ? '未连通' : '尚未检查' }} · {{ exchangeTime(status?.lastCheck?.checkedAt) }}</Descriptions.Item>
          <Descriptions.Item label="运行账户">{{ status?.runner?.selectedAccountId || '尚未选择' }}</Descriptions.Item>
        </Descriptions>
        <Select v-model:value="selectedAccount" :options="accounts" :disabled="accountLocked || Boolean(action)" show-search option-filter-prop="label" placeholder="选择一个策略身份（规则与执行参数冻结）" class="w-full" />
        <p class="mt-2 text-xs leading-5 text-muted-foreground">一次绑定一个策略账户，内部模拟按同一信号做对照。Demo 共享净头寸，多个纸面账户不能视作独立交易钱包。</p>
        <Alert v-if="selected && !selected.eligible" class="mt-3" show-icon type="warning" :message="selected.blockedReason || '该策略暂不满足 Demo 执行条件'" />
        <div v-if="selected" class="mt-3 identity-details text-xs text-muted-foreground">
          <div>规则标识：{{ selected.ruleHash || '未知' }}</div>
          <div>执行标识：{{ selected.executionHash || '未知' }}</div>
        </div>
        <div class="mt-4 flex flex-wrap gap-2">
          <Popconfirm title="开始币安 Demo 前向交易？" description="所选策略将向官方 Demo 发送订单，并记录实际成交。" ok-text="开始 Demo" cancel-text="取消" :disabled="!startAllowed || Boolean(action)" @confirm="start">
            <Button type="primary" :loading="action === 'start'" :disabled="!startAllowed || Boolean(action && action !== 'start')">手动开始 Demo</Button>
          </Popconfirm>
          <Button :loading="action === 'stop'" :disabled="!running || Boolean(action && action !== 'stop')" @click="stop">暂停新开仓</Button>
        </div>
        <div class="mt-2 text-xs leading-5 text-muted-foreground">暂停会停止新开仓并撤销未成交入口单；已有仓位继续执行风险保护，仓位变化可在下方查看。</div>
        <Alert v-if="status?.runner?.lastError" class="mt-3" show-icon type="error" :message="status.runner.lastError" />
        <Alert v-if="riskAlert" class="mt-3" show-icon type="error" :message="riskAlert" />
        <Alert v-if="ownershipConflicts" class="mt-3" show-icon type="error" message="仓位或订单归属未对账，新开仓已阻断" :description="ownershipConflicts" />
      </Card>
    </div>

    <div class="my-4 grid grid-cols-2 gap-3 xl:grid-cols-6">
      <div class="metric"><div class="metric-label">已记录订单</div><div class="metric-value">{{ metric('orderCount') }}</div></div>
      <div class="metric"><div class="metric-label">实际成交回报</div><div class="metric-value">{{ metric('fillCount') }}</div></div>
      <div class="metric"><div class="metric-label">终结入口单未成交率</div><div class="metric-value">{{ unfilledRatio(metrics.unfilledEntryRatio) }}</div><div class="text-xs text-muted-foreground">{{ metric('unfilledEntryCount') }} / {{ metric('terminalEntryCount') }} 已终结入口单</div></div>
      <div class="metric"><div class="metric-label">部分成交订单</div><div class="metric-value">{{ metric('partiallyFilledOrderCount') }}</div></div>
      <div class="metric"><div class="metric-label">拒单</div><div class="metric-value">{{ metric('rejectedOrderCount') }}</div></div>
      <div class="metric"><div class="metric-label">提交至回报延迟中位（ms）</div><div class="metric-value">{{ metric('medianLatencyMs') }}</div></div>
    </div>

    <Card :bordered="false" title="Demo 交易记录">
      <template #extra><span class="text-xs text-muted-foreground">{{ exchangeTime(updatedAt) }} · 最近 100 条</span></template>
      <Alert v-if="recordError" class="mb-3" show-icon type="error" :message="recordError" />
      <Tabs v-model:active-key="activeTab">
        <Tabs.TabPane key="orders" tab="订单">
          <Table :columns="orderColumns" :data-source="orders" :row-key="rowKey" :loading="loading" :scroll="{ x: 1960 }" size="small" :pagination="{ pageSize: 10, showSizeChanger: false }">
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'status'"><Tag :color="isSubmissionUnknown(record) ? 'warning' : 'default'">{{ orderRecordStatus(record) }}</Tag></template>
              <template v-else-if="column.key === 'time' || column.key === 'updateTime'">{{ exchangeTime(record[String(column.key)]) }}<div v-if="column.key === 'time' && record.demoAudit?.exchangeTimeObserved === false" class="text-xs text-muted-foreground">本机提交时间</div></template>
              <template v-else-if="decimalKeys.has(String(column.key))"><span class="decimal">{{ decimalText(record[String(column.key)]) }}</span></template>
            </template>
            <template #emptyText><Empty description="尚无 Demo 订单；完成配置并手动启动后积累前向记录" /></template>
          </Table>
        </Tabs.TabPane>
        <Tabs.TabPane key="fills" tab="成交与手续费">
          <div class="mb-3 text-xs text-muted-foreground">一笔订单可能对应多条成交回报。手续费金额、资产、Maker/Taker 和已实现损益直接展示币安字段；缺失不会用配置费率补值。</div>
          <Table :columns="fillColumns" :data-source="fills" :row-key="rowKey" :loading="loading" :scroll="{ x: 2000 }" size="small" :pagination="{ pageSize: 10, showSizeChanger: false }">
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'commission'">{{ decimalText(record.commission) }}</template>
              <template v-else-if="column.key === 'commissionAsset'">{{ record.commissionAsset || '未知' }}</template>
              <template v-else-if="column.key === 'side'">{{ fillSide(record) }}</template>
              <template v-else-if="column.key === 'maker'">{{ makerText(record.maker) }}</template>
              <template v-else-if="column.key === 'time'">{{ exchangeTime(record.time) }}</template>
              <template v-else-if="decimalKeys.has(String(column.key))"><span class="decimal">{{ decimalText(record[String(column.key)]) }}</span></template>
            </template>
            <template #emptyText><Empty description="尚无实际成交回报，费用与净收益暂不可评估" /></template>
          </Table>
        </Tabs.TabPane>
        <Tabs.TabPane key="positions" tab="持仓快照">
          <Table :columns="positionColumns" :data-source="positions" :row-key="rowKey" :loading="loading" :scroll="{ x: 1550 }" size="small" :pagination="{ pageSize: 10 }">
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'updateTime'">{{ exchangeTime(record.updateTime) }}</template>
              <template v-else-if="decimalKeys.has(String(column.key))">{{ decimalText(record[String(column.key)]) }}</template>
            </template>
            <template #emptyText><Empty description="尚无持仓快照" /></template>
          </Table>
        </Tabs.TabPane>
        <Tabs.TabPane key="account" tab="账户快照">
          <Table :columns="accountColumns" :data-source="balances" :row-key="rowKey" :loading="loading" :scroll="{ x: 1100 }" size="small" :pagination="{ pageSize: 10 }">
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'updateTime'">{{ exchangeTime(record.updateTime) }}</template>
              <template v-else-if="decimalKeys.has(String(column.key))">{{ decimalText(record[String(column.key)]) }}</template>
            </template>
            <template #emptyText><Empty description="尚无账户快照，请先执行只读连接检查" /></template>
          </Table>
        </Tabs.TabPane>
        <Tabs.TabPane key="comparisons" tab="内部模拟 vs Demo">
          <div class="mb-3 text-xs text-muted-foreground">对照审计按信号标识和输入指纹记录。内部成交价只在已记录成交事件时显示；Demo 实际成交见“成交与手续费”。两条成交路径不同，不把审计事件等同于完整组合收益对照。</div>
          <Table :columns="comparisonColumns" :data-source="comparisonRows" :row-key="rowKey" :loading="loading" :scroll="{ x: 1500 }" size="small" :pagination="{ pageSize: 10 }">
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'observedAt'">{{ exchangeTime(record.observedAt) }}</template>
              <template v-else-if="column.key === 'internalPrice'">{{ decimalText(record.internalPrice) }}</template>
            </template>
            <template #emptyText><Empty description="尚无同信号前向对照，不构造成交或盈利结果" /></template>
          </Table>
        </Tabs.TabPane>
        <Tabs.TabPane key="net-pnl" tab="实收费用与净损益">
          <div class="mb-3 text-xs leading-5 text-muted-foreground">净交易损益来自已平仓的 Demo 实际成交回报，扣除已确认 USDT 手续费，尚未包含资金费。费用缺失或手续费资产无法换算为 USDT 时净损益显示未知；不据此宣称策略或秒级交易已盈利。下方按资产汇总已回报手续费，未回报部分不补零。</div>
          <div class="mb-3 flex flex-wrap gap-2">
            <Tag v-for="(fee, index) in actualFees" :key="index">已回报手续费：{{ decimalText(fee.commission) }} {{ fee.commission_asset || '资产未知' }} · {{ valueText(fee.fill_count) }} 笔</Tag>
            <span v-if="!actualFees.length" class="text-xs text-muted-foreground">暂无实收手续费汇总</span>
          </div>
          <Table :columns="roundTripColumns" :data-source="roundTrips" :row-key="rowKey" :loading="loading" :scroll="{ x: 1500 }" size="small" :pagination="{ pageSize: 10 }">
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'opened_ms' || column.key === 'closed_ms'">{{ exchangeTime(record[String(column.key)]) }}</template>
              <template v-else-if="column.key === 'actual_commission_by_asset'">{{ feesByAsset(record.actual_commission_by_asset) }}</template>
              <template v-else-if="column.key === 'fee_complete'">{{ record.fee_complete === true ? '实收费已齐' : '费用未知' }}<div v-if="record.usdt_conversion_known !== true" class="text-xs text-muted-foreground">USDT 换算未知</div></template>
              <template v-else-if="column.key !== 'pair'">{{ decimalText(record[String(column.key)]) }}</template>
            </template>
            <template #emptyText><Empty description="尚无完整平仓成交，无法评估实际净收益" /></template>
          </Table>
        </Tabs.TabPane>
      </Tabs>
    </Card>
  </div>
</template>

<style scoped>
.demo-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.metric { min-width: 0; border: 1px solid hsl(var(--border)); border-radius: 8px; padding: 14px; background: hsl(var(--card)); }
.metric-label { min-height: 34px; color: #6b7280; font-size: 12px; }
.metric-value { margin-top: 4px; font-size: 23px; font-weight: 600; overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
.identity-details { overflow-wrap: anywhere; line-height: 1.7; }
.decimal { font-variant-numeric: tabular-nums; }
</style>

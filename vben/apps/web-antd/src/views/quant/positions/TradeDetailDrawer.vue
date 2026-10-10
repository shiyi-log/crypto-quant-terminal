<script lang="ts" setup>
import type { FtTrade } from '#/api/freqtrade';

import { computed, onUnmounted, ref, watch } from 'vue';

import { Alert, Button, Drawer, Table, Tag } from 'ant-design-vue';

import { errText, getTrade } from '#/api/freqtrade';

const props = defineProps<{ open: boolean; record: FtTrade | null }>();
const emit = defineEmits<{ 'update:open': [value: boolean] }>();
const loading = ref(false);
const error = ref('');
const payload = ref<FtTrade | null>(null);
let requestId = 0;

const trade = computed(() => ({ ...props.record, ...payload.value }));
const hasTradeId = computed(
  () => props.record?.trade_id !== undefined && props.record?.trade_id !== null,
);
const baseCurrency = computed(
  () => trade.value.base_currency ?? trade.value.pair?.split('/')[0] ?? '',
);
const stakeCurrency = computed(
  () =>
    trade.value.stake_currency ??
    trade.value.pair?.split(':')[1] ??
    trade.value.pair?.split('/')[1] ??
    '',
);

function number(value: unknown): number | null {
  if (
    (typeof value !== 'number' && typeof value !== 'string') ||
    (typeof value === 'string' && value.trim() === '')
  )
    return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function format(value: unknown, digits = 8, unit = '') {
  const parsed = number(value);
  if (parsed === null) return '—';
  return `${parsed.toLocaleString('en-US', { maximumFractionDigits: digits })}${unit ? ` ${unit}` : ''}`;
}

function signed(value: unknown, digits = 2, unit = '') {
  const parsed = number(value);
  return parsed === null
    ? '—'
    : `${parsed > 0 ? '+' : ''}${format(parsed, digits, unit)}`;
}

function fullTime(value: unknown) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value !== 'number' && typeof value !== 'string') return '—';
  // Freqtrade 的无时区日期字符串为 UTC；优先使用接口的毫秒时间戳。
  const raw =
    typeof value === 'string' &&
    /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(value)
      ? `${value.replace(' ', 'T')}Z`
      : value;
  const date = new Date(raw);
  return Number.isNaN(date.getTime())
    ? String(value)
    : date.toLocaleString('zh-CN', { hour12: false });
}

const profitRatio = computed(() => {
  const t = trade.value;
  const ratio = number(t.profit_ratio ?? t.close_profit);
  return ratio === null ? number(t.profit_pct) : ratio * 100;
});
const profitAmount = computed(
  () => trade.value.profit_abs ?? trade.value.close_profit_abs,
);
const summaryFields = computed(() => {
  const t = trade.value;
  return [
    { label: '交易编号', value: t.trade_id ?? '—' },
    { label: '策略', value: t.strategy ?? '—' },
    { label: '杠杆', value: format(t.leverage, 2, 'x') },
    { label: '开仓价', value: format(t.open_rate, 8, stakeCurrency.value) },
    {
      label: t.is_open ? '现价' : '平仓价',
      value: format(
        t.is_open ? t.current_rate : t.close_rate,
        8,
        stakeCurrency.value,
      ),
    },
    {
      label: t.is_open ? '持仓数量' : '交易数量',
      value: format(t.amount, 8, baseCurrency.value),
    },
    {
      label: '投入金额',
      value: format(t.stake_amount, 4, stakeCurrency.value),
    },
    {
      label: t.is_open ? '浮动盈亏' : '已平仓盈亏',
      value: signed(profitAmount.value, 4, stakeCurrency.value),
      profit: number(profitAmount.value),
    },
    {
      label: '收益率',
      value: profitRatio.value === null ? '—' : `${signed(profitRatio.value)}%`,
      profit: profitRatio.value,
    },
    { label: '开仓时间', value: fullTime(t.open_timestamp ?? t.open_date) },
    { label: '平仓时间', value: fullTime(t.close_timestamp ?? t.close_date) },
    { label: '开仓标签', value: t.enter_tag || '—' },
    { label: '退出原因', value: t.exit_reason || '—' },
    {
      label: '当前止损价',
      value: format(t.stop_loss_abs, 8, stakeCurrency.value),
    },
    {
      label: '强平价',
      value: format(t.liquidation_price, 8, stakeCurrency.value),
    },
    {
      label: '开仓手续费率',
      value:
        number(t.fee_open) === null
          ? '—'
          : `${format(Number(t.fee_open) * 100, 4)}%`,
    },
    {
      label: '平仓手续费率',
      value:
        number(t.fee_close) === null
          ? '—'
          : `${format(Number(t.fee_close) * 100, 4)}%`,
    },
    {
      label: '开仓手续费',
      value: format(
        t.fee_open_cost,
        8,
        t.fee_open_currency ?? stakeCurrency.value,
      ),
    },
    {
      label: '平仓手续费',
      value: format(
        t.fee_close_cost,
        8,
        t.fee_close_currency ?? stakeCurrency.value,
      ),
    },
    { label: '资金费', value: signed(t.funding_fees, 4, stakeCurrency.value) },
  ];
});

const orders = computed(() =>
  Array.isArray(trade.value.orders)
    ? trade.value.orders
        .filter(
          (order: any) =>
            order && typeof order === 'object' && !Array.isArray(order),
        )
        .map((order: Record<string, any>, index: number) => ({
          ...order,
          row_key: `${order.order_id ?? order.id ?? 'order'}-${index}`,
        }))
    : [],
);
const orderCols = [
  { key: 'id', title: '订单编号', width: 200 },
  { key: 'side', title: '用途 / 买卖', width: 130 },
  { key: 'type', title: '类型', width: 100 },
  { key: 'status', title: '状态', width: 130 },
  { key: 'price', title: '订单价格', align: 'right' as const, width: 110 },
  {
    key: 'amount',
    title: '已成交 / 委托数量',
    align: 'right' as const,
    width: 170,
  },
  { key: 'remaining', title: '剩余数量', align: 'right' as const, width: 110 },
  { key: 'cost', title: '成交金额', align: 'right' as const, width: 120 },
  { key: 'fee', title: '基础币手续费', align: 'right' as const, width: 130 },
  { key: 'tag', title: '订单标签', width: 150 },
  { key: 'time', title: '时间', width: 230 },
];
const statuses: Record<string, string> = {
  canceled: '已撤销',
  cancelled: '已撤销',
  closed: '已完成',
  expired: '已过期',
  open: '未完成',
  rejected: '已拒绝',
};
const orderTypes: Record<string, string> = {
  limit: '限价',
  market: '市价',
  stop: '止损',
};
function orderSide(order: Record<string, any>) {
  const side = order.ft_order_side ?? order.side;
  const action =
    side === 'buy' ? '买入' : side === 'sell' ? '卖出' : (side ?? '—');
  const use =
    typeof order.ft_is_entry === 'boolean'
      ? order.ft_is_entry
        ? '入场'
        : '出场'
      : side === 'stoploss'
        ? '止损'
        : typeof trade.value.is_short === 'boolean' &&
            (side === 'buy' || side === 'sell')
          ? side === (trade.value.is_short ? 'sell' : 'buy')
            ? '入场'
            : '出场'
          : '';
  return use ? `${use} / ${action}` : action;
}

async function loadDetail() {
  const current = ++requestId;
  const selected = props.record;
  error.value = '';
  if (!props.open || !hasTradeId.value || !selected) {
    loading.value = false;
    return;
  }
  loading.value = true;
  try {
    const data = await getTrade(selected.trade_id!);
    if (current !== requestId) return;
    if (
      !data ||
      typeof data !== 'object' ||
      Array.isArray(data) ||
      String(data.trade_id) !== String(selected.trade_id)
    ) {
      throw new Error('交易详情响应无效，请重试');
    }
    payload.value = data;
  } catch (e) {
    if (current === requestId) error.value = errText(e);
  } finally {
    if (current === requestId) loading.value = false;
  }
}

watch(
  () => [props.open, props.record] as const,
  () => {
    payload.value = null;
    void loadDetail();
  },
  { immediate: true },
);
onUnmounted(() => {
  requestId++;
});
</script>

<template>
  <Drawer
    :open="open"
    title="订单详情"
    width="min(1100px, 100vw)"
    @update:open="emit('update:open', $event)"
  >
    <template #extra>
      <Button
        :disabled="!hasTradeId"
        :loading="loading"
        size="small"
        @click="loadDetail"
      >
        {{ error ? '重试' : '刷新详情' }}
      </Button>
    </template>
    <div class="mb-4 flex flex-wrap items-center gap-2">
      <span class="text-lg font-semibold">{{ trade.pair ?? '—' }}</span>
      <Tag :color="trade.is_short ? 'green' : 'red'">
        {{
          trade.is_short === undefined
            ? '方向未知'
            : trade.is_short
              ? '做空'
              : '做多'
        }}
      </Tag>
      <Tag :color="trade.is_open ? 'warning' : 'default'">
        {{
          trade.is_open === undefined
            ? '状态未知'
            : trade.is_open
              ? '持仓中'
              : '已平仓'
        }}
      </Tag>
    </div>
    <Alert
      v-if="error"
      class="mb-4"
      :message="`详情加载失败：${error}`"
      :description="
        payload
          ? '保留上次加载的详情，可点击重试。'
          : '暂展示列表快照，可点击重试。'
      "
      show-icon
      type="warning"
    />
    <Alert
      v-else-if="!hasTradeId"
      class="mb-4"
      message="此记录缺少交易编号，仅展示列表快照。"
      show-icon
      type="info"
    />
    <div class="mb-6 text-xs text-gray-400">
      {{
        loading
          ? '正在加载最新详情…'
          : payload
            ? '交易详情快照，可手动刷新。'
            : '列表快照。'
      }}
      时间均为本地时间（{{
        Intl.DateTimeFormat().resolvedOptions().timeZone
      }}）。
    </div>
    <div
      class="mb-6 grid grid-cols-1 gap-x-6 gap-y-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4"
    >
      <div v-for="field in summaryFields" :key="field.label" class="min-w-0">
        <div class="mb-1 text-xs text-gray-400">{{ field.label }}</div>
        <div
          class="break-words text-sm font-medium"
          :class="
            Number(field.profit) > 0
              ? 'text-red-500'
              : Number(field.profit) < 0
                ? 'text-emerald-500'
                : ''
          "
        >
          {{ field.value }}
        </div>
      </div>
    </div>
    <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h3 class="text-base font-semibold">
        订单记录
        <span
          v-if="payload || orders.length"
          class="text-xs font-normal text-gray-400"
          >{{ orders.length }} 笔</span
        >
      </h3>
      <span class="text-xs text-gray-400"
        >价格 / 金额：{{ stakeCurrency || '—' }} · 数量：{{
          baseCurrency || '—'
        }}</span
      >
    </div>
    <Table
      :columns="orderCols"
      :data-source="orders"
      :loading="loading"
      :pagination="false"
      :scroll="{ x: 'max-content' }"
      row-key="row_key"
      size="small"
    >
      <template #bodyCell="{ column, record: order }">
        <template v-if="column.key === 'id'">
          <span class="break-all font-mono text-xs">{{
            order.order_id ?? order.id ?? '—'
          }}</span>
        </template>
        <template v-else-if="column.key === 'side'">{{
          orderSide(order)
        }}</template>
        <template v-else-if="column.key === 'type'">{{
          orderTypes[order.order_type ?? order.type] ??
          order.order_type ??
          order.type ??
          '—'
        }}</template>
        <template v-else-if="column.key === 'status'">
          <Tag
            :color="
              order.status === 'closed'
                ? 'success'
                : order.status === 'open'
                  ? 'processing'
                  : 'default'
            "
            >{{ statuses[order.status] ?? order.status ?? '—' }}</Tag
          >
        </template>
        <template v-else-if="column.key === 'price'">{{
          format(order.safe_price)
        }}</template>
        <template v-else-if="column.key === 'amount'"
          >{{ format(order.filled) }} / {{ format(order.amount) }}</template
        >
        <template v-else-if="column.key === 'remaining'">{{
          format(order.remaining)
        }}</template>
        <template v-else-if="column.key === 'cost'">{{
          format(order.cost, 4)
        }}</template>
        <template v-else-if="column.key === 'fee'">{{
          format(order.ft_fee_base, 8, baseCurrency)
        }}</template>
        <template v-else-if="column.key === 'tag'">{{
          order.ft_order_tag || '—'
        }}</template>
        <template v-else-if="column.key === 'time'">
          <div class="whitespace-nowrap text-xs">
            创建：{{ fullTime(order.order_timestamp ?? order.order_date) }}
          </div>
          <div class="whitespace-nowrap text-xs text-gray-400">
            成交：{{
              fullTime(order.order_filled_timestamp ?? order.order_filled_date)
            }}
          </div>
        </template>
      </template>
      <template #emptyText>
        <div class="py-6 text-sm text-gray-400">
          {{
            error
              ? '订单列表未能加载，可点击重试。'
              : !hasTradeId
                ? '缺少交易编号，无法查询订单列表。'
                : '暂无订单记录。'
          }}
        </div>
      </template>
    </Table>
    <div class="mt-3 text-xs text-gray-400">
      列表包含接口返回的已成交或仍开放的订单，不代表完整撤单历史。订单价格不单独区分委托价与成交均价；基础币手续费不代表全部手续费。未提供的字段显示“—”。
    </div>
  </Drawer>
</template>

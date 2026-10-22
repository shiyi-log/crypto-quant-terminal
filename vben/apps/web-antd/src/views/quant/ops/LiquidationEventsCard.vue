<script lang="ts" setup>
import { computed, onMounted, ref } from 'vue';

import { Alert, Button, Card, Empty, Spin, Table, Tag } from 'ant-design-vue';

import { getLiquidationEvents, type LiquidationEvent } from '#/api/liquidation';

const loading = ref(false);
const error = ref('');
const events = ref<LiquidationEvent[]>([]);

async function refresh() {
  loading.value = true;
  error.value = '';
  try {
    const response = await getLiquidationEvents({ limit: 100 });
    events.value = response.events;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
    // 失败时清空，避免把上一次的数据当成当前结果
    events.value = [];
  } finally {
    loading.value = false;
  }
}

function formatMs(ms?: null | number): string {
  if (ms === null || ms === undefined) return '未知';
  return new Date(ms).toLocaleString('zh-CN');
}

function fixed(value: null | number | undefined, digits = 2): string {
  return value === null || value === undefined ? '未知' : value.toFixed(digits);
}

function pct(value: null | number | undefined): string {
  return value === null || value === undefined ? '未知' : `${(value * 100).toFixed(3)}%`;
}

const columns = [
  { dataIndex: 'candle_ms', key: 'candle_ms', title: '触发时间', width: 170 },
  { dataIndex: 'strategy_id', key: 'strategy_id', title: '策略', width: 120 },
  { dataIndex: 'pair', key: 'pair', title: '交易对', width: 140 },
  { dataIndex: 'side', key: 'side', title: '方向', width: 80 },
  { dataIndex: 'maintenance_margin_rate', key: 'maintenance_margin_rate', title: '维持保证金率', align: 'right' as const },
  { dataIndex: 'liquidation_price_theory', key: 'liquidation_price_theory', title: '理论强平价', align: 'right' as const },
  { dataIndex: 'fill_price_with_slippage', key: 'fill_price_with_slippage', title: '含滑点成交价', align: 'right' as const },
  { dataIndex: 'realized_loss_estimate', key: 'realized_loss_estimate', title: '估计损失', align: 'right' as const },
  { dataIndex: 'triggered_by_mark', key: 'triggered_by_mark', title: '触发路径', width: 130 },
];

const totalLoss = computed(() =>
  events.value.reduce((sum, item) => sum + (item.realized_loss_estimate ?? 0), 0),
);

onMounted(() => {
  void refresh();
});
</script>

<template>
  <Card class="mt-4" title="⚠ 强平事件（强平模型）">
    <template #extra>
      <Button size="small" :loading="loading" @click="refresh">刷新</Button>
    </template>

    <Alert v-if="error" class="mb-3" show-icon type="error" :message="`强平事件加载失败：${error}`" />

    <Spin :spinning="loading">
      <div v-if="!loading && !error && events.length === 0" class="py-10 text-center">
        <Empty description="暂无强平事件" />
      </div>

      <template v-else-if="events.length > 0">
        <div class="mb-3 flex flex-wrap gap-2">
          <Tag color="red">强平 {{ events.length }} 次</Tag>
          <Tag color="orange">估计损失合计 {{ totalLoss.toFixed(2) }} USDT</Tag>
        </div>

        <Table
          :columns="columns"
          :data-source="events"
          :pagination="{ pageSize: 10, showSizeChanger: false }"
          :scroll="{ x: 1200 }"
          row-key="event_id"
          size="small"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'candle_ms'">
              {{ formatMs(record.candle_ms) }}
            </template>
            <template v-else-if="column.key === 'strategy_id'">
              <span class="font-mono text-xs">{{ record.strategy_id }}</span>
            </template>
            <template v-else-if="column.key === 'side'">
              <Tag :color="record.side === 'long' ? 'green' : 'red'">
                {{ record.side === 'long' ? '多头' : record.side === 'short' ? '空头' : '未知' }}
              </Tag>
            </template>
            <template v-else-if="column.key === 'maintenance_margin_rate'">
              {{ pct(record.maintenance_margin_rate) }}
            </template>
            <template v-else-if="column.key === 'liquidation_price_theory'">
              {{ fixed(record.liquidation_price_theory) }}
            </template>
            <template v-else-if="column.key === 'fill_price_with_slippage'">
              {{ fixed(record.fill_price_with_slippage) }}
            </template>
            <template v-else-if="column.key === 'realized_loss_estimate'">
              <span class="font-mono text-red-600">
                {{ record.realized_loss_estimate === null || record.realized_loss_estimate === undefined
                  ? '未知'
                  : `-${fixed(record.realized_loss_estimate)}` }}
              </span>
            </template>
            <template v-else-if="column.key === 'triggered_by_mark'">
              <Tag v-if="record.triggered_by_mark" color="red">标记价</Tag>
              <Tag v-else-if="record.triggered_by_trade" color="orange">成交价</Tag>
              <Tag v-else>未知</Tag>
            </template>
          </template>
        </Table>
      </template>
    </Spin>
  </Card>
</template>

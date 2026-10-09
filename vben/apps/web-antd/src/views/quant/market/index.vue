<script lang="ts" setup>
import { onMounted, onUnmounted, ref } from 'vue';

import { Alert, Button, Card, Radio, Spin } from 'ant-design-vue';

import { getConfig, getOpenTrades, getWhitelist } from '#/api/freqtrade';

import PairChart from './PairChart.vue';

const pairs = ref<string[]>([]);
const timeframe = ref('1h');
const loading = ref(false);
const error = ref('');
const periods = [
  { label: '5分', value: '5m' },
  { label: '15分', value: '15m' },
  { label: '30分', value: '30m' },
  { label: '1时', value: '1h' },
  { label: '4时', value: '4h' },
  { label: '1日', value: '1d' },
  { label: '1周', value: '1w' },
];
let disposed = false;
let timer: ReturnType<typeof setInterval> | undefined;

async function loadUniverse(initial = false) {
  if (loading.value) return;
  loading.value = true;
  try {
    const [whitelist, trades, config] = await Promise.all([
      getWhitelist(),
      getOpenTrades().catch(() => []),
      initial ? getConfig().catch(() => null) : Promise.resolve(null),
    ]);
    if (disposed) return;
    if (
      initial &&
      config &&
      periods.some((period) => period.value === config.timeframe)
    ) {
      timeframe.value = config.timeframe;
    }
    // 展示真实选币池，并保留仍有持仓但已退出选币池的币种。
    pairs.value = [
      ...new Set([
        ...(whitelist.whitelist ?? []),
        ...trades
          .map((trade) => trade.pair)
          .filter((pair): pair is string => !!pair),
      ]),
    ].sort();
    error.value = '';
  } catch {
    if (!disposed) error.value = '选币名单加载失败，请重试';
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  void loadUniverse(true);
  timer = setInterval(() => void loadUniverse(), 60_000);
});
onUnmounted(() => {
  disposed = true;
  clearInterval(timer);
});
</script>

<template>
  <div class="space-y-4 p-4">
    <Card :bordered="false" title="行情图表">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <span class="text-sm text-gray-400"
          >全部选币 · {{ pairs.length }} 个币种 · 每排两张图</span
        >
        <div class="flex flex-wrap items-center gap-3">
          <Radio.Group
            v-model:value="timeframe"
            button-style="solid"
            size="small"
          >
            <Radio.Button
              v-for="period in periods"
              :key="period.value"
              :value="period.value"
            >
              {{ period.label }}
            </Radio.Button>
          </Radio.Group>
          <Button :loading="loading" size="small" @click="loadUniverse()"
            >刷新选币</Button
          >
        </div>
      </div>
    </Card>
    <Alert v-if="error" :message="error" type="warning" show-icon />
    <Spin v-if="loading && !pairs.length" />
    <Card v-else-if="!pairs.length" :bordered="false">暂无选币</Card>
    <!-- 桌面固定两列；手机保留单列以便读取 K 线。 -->
    <div class="grid grid-cols-1 gap-4 md:grid-cols-2">
      <PairChart
        v-for="pair in pairs"
        :key="pair"
        :pair="pair"
        :timeframe="timeframe"
      />
    </div>
  </div>
</template>

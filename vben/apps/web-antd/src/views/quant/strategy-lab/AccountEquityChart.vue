<script lang="ts" setup>
import type { EchartsUIType } from '@vben/plugins/echarts';
import type { StrategyLabRow } from '#/api/strategy-lab';

import { onMounted, ref, watch } from 'vue';

import { EchartsUI, useEcharts } from '@vben/plugins/echarts';

import { fullOrderTime } from '../utils/orderDisplayId';

const props = defineProps<{ points: StrategyLabRow[] }>();
const chartRef = ref<EchartsUIType>();
const { renderEcharts } = useEcharts(chartRef);

function numberOf(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  if (String(value).trim() === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function draw() {
  const points = props.points
    .filter((point) => numberOf(point.timestamp) !== null)
    .toSorted((left, right) => Number(left.timestamp) - Number(right.timestamp));
  void renderEcharts({
    animation: false,
    color: ['#2563eb'],
    grid: { left: 64, top: 28, right: 24, bottom: 44 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: points.map((point) => fullOrderTime(point.timestamp)) },
    yAxis: { type: 'value', scale: true, name: '账户净值 USDT' },
    series: [{
      name: '净值', type: 'line',
      data: points.map((point) => numberOf(point.equity)),
      connectNulls: false, showSymbol: points.length <= 1, symbolSize: 8,
      lineStyle: { width: 2 },
    }],
  });
}

onMounted(draw);
watch(() => props.points, draw, { flush: 'post' });
</script>

<template>
  <EchartsUI ref="chartRef" height="300px" />
</template>

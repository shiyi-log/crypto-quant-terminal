<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue';
import {
  getBackgroundJobs,
  getBalance,
  getConfig,
  getIteration,
  getOps,
  getOpenTrades,
  getProfit,
  getResearchProgress,
  getServices,
  getRunProgress,
  getTrades,
  getWhitelist,
} from '#/api/freqtrade';
import OverviewWorkspace from './OverviewWorkspace.vue';
import { createOverviewState } from './overviewState';
const state = createOverviewState({
  config: getConfig,
  profit: getProfit,
  balance: getBalance,
  positions: getOpenTrades,
  trades: () => getTrades(20),
  ops: getOps,
  iteration: getIteration,
  progress: getRunProgress,
  researchProgress: getResearchProgress,
  jobs: getBackgroundJobs,
  services: getServices,
  whitelist: getWhitelist,
});
let timer: ReturnType<typeof setInterval> | undefined;
onMounted(() => {
  void state.refresh();
  timer = setInterval(() => void state.refresh(), 5000);
});
onUnmounted(() => {
  clearInterval(timer);
  state.dispose();
});
</script>
<template><OverviewWorkspace :state="state" /></template>

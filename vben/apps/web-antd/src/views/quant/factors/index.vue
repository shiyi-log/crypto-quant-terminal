<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue';

import { getOps } from '#/api/freqtrade';

import FactorWorkspace from './FactorWorkspace.vue';
import { createFactorReader } from './factorSnapshot';

const reader = createFactorReader();
const now = ref(Date.now());
// 现有 GET API 尚无signal参数，reader仍以取消与请求代数阻止迟到发布。
async function refresh() {
  await reader.load(() => getOps());
  now.value = Date.now();
}
let timer: ReturnType<typeof setInterval> | undefined;
onMounted(() => {
  void refresh();
  timer = setInterval(() => {
    now.value = Date.now();
  }, 30_000);
});
onUnmounted(() => {
  reader.cancel();
  if (timer) clearInterval(timer);
});
</script>

<template>
  <FactorWorkspace
    :snapshot="reader.state.snapshot"
    :loading="reader.state.loading"
    :error="reader.state.error"
    :now
    @refresh="refresh"
  />
</template>

<script lang="ts" setup>
import { onMounted, onUnmounted } from 'vue';

import {
  addBlacklist,
  deleteBlacklist,
  deleteLock,
  getBlacklist,
  getLocks,
  getWhitelist,
} from '#/api/freqtrade';

import PairPolicyWorkspace from './PairPolicyWorkspace.vue';
import { createPairPolicy } from './pairPolicy';

const policy = createPairPolicy({
  whitelist: getWhitelist,
  blacklist: getBlacklist,
  locks: getLocks,
  add: addBlacklist,
  remove: deleteBlacklist,
  unlock: deleteLock,
});
let timer: ReturnType<typeof setInterval>;
onMounted(() => {
  void policy.refresh();
  timer = setInterval(() => {
    void policy.refresh();
  }, 30_000);
});
onUnmounted(() => {
  clearInterval(timer);
  policy.dispose();
});
</script>

<template><PairPolicyWorkspace :policy="policy" /></template>

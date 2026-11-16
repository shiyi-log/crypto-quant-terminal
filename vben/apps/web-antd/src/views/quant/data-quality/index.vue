<script setup lang="ts">
import { ftClient, unwrap } from '#/api/freqtrade/client';
import { requireSession } from '#/api/session';

import DataQualityWorkspace from './DataQualityWorkspace.vue';

async function loadInventory(signal: AbortSignal): Promise<unknown> {
  requireSession('请先登录后查看数据质量');
  // 保留原始响应合同；现有便利适配器会把缺失集合转为空数组。
  return unwrap(await ftClient.get('/locals/database/status', { signal }));
}
</script>

<template><DataQualityWorkspace :load-inventory="loadInventory" /></template>

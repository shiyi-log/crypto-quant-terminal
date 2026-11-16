<script setup lang="ts">
import { ftClient, unwrap } from '#/api/freqtrade/client';
import { requireSession } from '#/api/session';
import SurgeWorkspace from './SurgeWorkspace.vue';
import type { SurgeQuery } from './surgeResearch';
async function loadList(
  query: SurgeQuery,
  signal: AbortSignal,
): Promise<unknown> {
  requireSession('请先登录后查看订单流研究');
  return unwrap(
    await ftClient.get('/locals/orderflow/surges', {
      params: { ...query, limit: 100 },
      signal,
    }),
  );
}
async function loadStats(signal: AbortSignal): Promise<unknown> {
  requireSession('请先登录后查看订单流研究');
  return unwrap(
    await ftClient.get('/locals/orderflow/surge-stats', { signal }),
  );
}
async function loadDetail(id: string, signal: AbortSignal): Promise<unknown> {
  requireSession('请先登录后查看订单流研究');
  return unwrap(
    await ftClient.get('/locals/orderflow/surges/' + encodeURIComponent(id), {
      signal,
    }),
  );
}
</script>
<template>
  <SurgeWorkspace
    :load-list="loadList"
    :load-stats="loadStats"
    :load-detail="loadDetail"
  />
</template>

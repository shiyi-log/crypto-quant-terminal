<script setup lang="ts">
import { useRoute } from 'vue-router';

import { getServices } from '#/api/freqtrade';
import { ftClient, unwrap } from '#/api/freqtrade/client';
import { getRuntimeHealth } from '#/api/runtime-health';
import { requireSession } from '#/api/session';

import OperationsWorkspace from './OperationsWorkspace.vue';

const route = useRoute();
const initialResearchId =
  typeof route.query.research_id === 'string' ? route.query.research_id : '';

// The current shared health/services readers have no signal parameter. The
// workspace generation guard discards their late results after refresh/disposal.
function loadHealth(_signal: AbortSignal): Promise<unknown> {
  requireSession('请先登录后再查看运行健康');
  return getRuntimeHealth();
}
function loadServices(_signal: AbortSignal): Promise<unknown> {
  requireSession('请先登录后再查看服务事实');
  return getServices();
}
async function loadEvents(
  researchId: string,
  signal: AbortSignal,
): Promise<unknown> {
  requireSession('请先登录后再查看研究账本');
  const params: Record<string, number | string> = { limit: 50 };
  if (researchId) params.research_id = researchId;
  // Preserve the actual endpoint response for strict parsing. The shared
  // getResearchLedgerEvents convenience reader coerces malformed bodies to [].
  return unwrap<unknown>(
    await ftClient.get('/locals/research-ledger-events', { params, signal }),
  );
}
</script>

<template>
  <OperationsWorkspace
    :load-health="loadHealth"
    :load-services="loadServices"
    :load-events="loadEvents"
    :initial-research-id="initialResearchId"
  />
</template>

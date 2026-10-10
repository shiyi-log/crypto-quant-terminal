<script lang="ts" setup>
import { computed } from 'vue';

import { Alert, Card, Tag } from 'ant-design-vue';

const props = defineProps<{ inspection: any }>();

// During rollout the API may still return the old document. It is archival
// evidence either way: the legacy inspection entry point has been disabled.
const archivedData = computed(() => {
  if (props.inspection?.archive) return props.inspection.archive.data ?? null;
  return props.inspection?.checks ? props.inspection : null;
});
const lastRunAt = computed(() =>
  props.inspection?.last_run_at ?? archivedData.value?.t ?? null,
);
const archiveText = computed(() =>
  JSON.stringify(
    {
      status: 'archive_only',
      validity: 'invalidated',
      data: archivedData.value,
      history: (props.inspection?.archive?.history ?? props.inspection?.history ?? []).slice(0, 20),
    },
    null,
    2,
  ),
);
</script>

<template>
  <Card :bordered="false" class="shadow-sm" title="自动迭代巡检">
    <template #extra>
      <Tag>巡检已停</Tag>
    </template>
    <Alert
      message="口径已作废 · 巡检已停"
      description="C1/C2/C3 旧结论已撤回，不再用于验证策略或实际收益。旧状态与历史轮次仅保留作废记录；实际订单和收益请查看订单详情及研究证据账。"
      show-icon
      type="warning"
    />
    <div class="mt-3 text-xs text-muted-foreground">
      <template v-if="lastRunAt">最后留档时间：{{ lastRunAt }} · 非实时结果</template>
      <template v-else-if="inspection">暂无历史巡检记录</template>
      <template v-else>状态接口暂不可用；旧巡检仍处于禁用状态</template>
    </div>
    <details v-if="archivedData" class="mt-3 rounded-lg border p-3 text-xs text-muted-foreground">
      <summary class="cursor-pointer">查看已作废原始记录（仅留档）</summary>
      <pre class="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words">{{ archiveText }}</pre>
    </details>
  </Card>
</template>

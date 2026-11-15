<script setup lang="ts">
import type { IterationRun } from '#/api/freqtrade';

import { computed, onMounted, onUnmounted, reactive, ref } from 'vue';

import { Alert, Button, Card, Empty, Modal, Progress, Spin, Table, Tag } from 'ant-design-vue';

import { getIterationRun, getIterationRuns } from '#/api/freqtrade';

import {
  createRunDetail,
  createRunSnapshot,
  executionView,
  formatRunMetric,
  formatRunTime,
  refreshRunDetail,
  refreshRunSnapshot,
  researchView,
  runMetrics,
} from './runDisplay';

const snapshot = reactive(createRunSnapshot());
const detail = reactive(createRunDetail());
const open = ref(false);
const columns = [
  { title: '运行编号', key: 'run', width: 220 },
  { title: '策略 / 模型', key: 'model', width: 200 },
  { title: '执行状态', key: 'execution', width: 120 },
  { title: '研究裁定', key: 'research', width: 120 },
  { title: '更新时间（北京时间）', key: 'time', width: 200 },
  { title: '证据', key: 'detail', width: 90 },
];

function updated(run: IterationRun): string {
  return formatRunTime(run.updated_at ?? run.finished_at ?? run.completed_at ?? run.timestamp ?? run.started_at);
}

const values = computed(() => runMetrics(detail.item));
const evidence = computed(() => detail.item?.evidence ?? detail.item?.entry?.evidence ?? null);
const evidenceText = computed(() => evidence.value ? JSON.stringify(evidence.value, null, 2) : '尚无证据引用');
const source = computed(() => {
  const value = detail.item?.result_source ?? evidence.value?.source;
  if (value === 'cache') return '身份校验后的缓存';
  if (value === 'fresh' || value === 'new') return '本轮新计算';
  return typeof value === 'string' ? value : '未记录';
});
const progress = computed(() => {
  const value = detail.item?.progress?.percent;
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;
});
const reasons = computed(() => detail.item?.gate?.reasons ?? []);

async function load() {
  await refreshRunSnapshot(snapshot, getIterationRuns);
}

async function showRun(runId: string) {
  open.value = true;
  await refreshRunDetail(detail, runId, getIterationRun);
}

let timer: ReturnType<typeof setInterval> | null = null;
onMounted(() => {
  void load();
  timer = setInterval(() => {
    void load();
    if (open.value && detail.selectedRunId && !detail.loading) void showRun(detail.selectedRunId);
  }, 15_000);
});
onUnmounted(() => { if (timer) clearInterval(timer); });
</script>

<template>
  <Card :bordered="false" class="mb-4 shadow-sm" title="自动迭代运行记录">
    <template #extra>
      <Button size="small" :loading="snapshot.loading" @click="load">刷新运行记录</Button>
    </template>
    <p class="mb-3 text-xs text-muted-foreground">
      执行状态说明任务是否正常完成；研究裁定沿用冻结判据。每轮证据按运行编号查询。
    </p>
    <Alert
      v-if="snapshot.error"
      class="mb-3"
      type="warning"
      show-icon
      :message="snapshot.error"
      :description="snapshot.hasSnapshot ? '当前保留上次成功读取的记录。' : '尚未读取到运行快照。'"
    />
    <Spin :spinning="snapshot.loading && !snapshot.hasSnapshot">
      <Table
        v-if="snapshot.items.length"
        :columns="columns"
        :data-source="snapshot.items"
        :pagination="{ pageSize: 10, showSizeChanger: false }"
        :scroll="{ x: 950 }"
        size="small"
        row-key="run_id"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'run'">
            <span class="font-mono text-xs">{{ record.run_id }}</span>
          </template>
          <template v-else-if="column.key === 'model'">
            <div>{{ record.strategy || '策略未记录' }}</div>
            <div class="text-xs text-muted-foreground">{{ record.model || record.identifier || record.version_id || '模型未记录' }}</div>
          </template>
          <template v-else-if="column.key === 'execution'">
            <Tag :color="executionView(record.execution_status).color">{{ executionView(record.execution_status).label }}</Tag>
          </template>
          <template v-else-if="column.key === 'research'">
            <Tag :color="researchView(record.research_verdict).color">{{ researchView(record.research_verdict).label }}</Tag>
          </template>
          <template v-else-if="column.key === 'time'">{{ updated(record) }}</template>
          <template v-else-if="column.key === 'detail'">
            <Button type="link" size="small" @click="showRun(record.run_id)">查看</Button>
          </template>
        </template>
      </Table>
      <Empty v-else-if="snapshot.hasSnapshot" :image="Empty.PRESENTED_IMAGE_SIMPLE" description="暂无运行记录" />
    </Spin>
    <div v-if="snapshot.hasSnapshot" class="mt-2 text-xs text-muted-foreground">
      已读取 {{ snapshot.items.length }} / {{ snapshot.total }} 轮
    </div>
  </Card>

  <Modal v-model:open="open" title="本轮运行与研究证据" :footer="null" :width="800">
    <div class="mb-3 break-all font-mono text-xs">{{ detail.selectedRunId }}</div>
    <Alert v-if="detail.error" class="mb-3" type="warning" show-icon :message="detail.error">
      <template #description>
        <span v-if="detail.item" class="mr-2">当前保留该轮上次成功读取的快照。</span>
        <Button size="small" @click="showRun(detail.selectedRunId)">重试</Button>
      </template>
    </Alert>
    <Spin :spinning="detail.loading">
      <template v-if="detail.item">
        <div class="mb-3 flex flex-wrap items-center gap-2">
          <Tag :color="executionView(detail.item.execution_status).color">{{ executionView(detail.item.execution_status).label }}</Tag>
          <Tag :color="researchView(detail.item.research_verdict).color">{{ researchView(detail.item.research_verdict).label }}</Tag>
          <span class="text-xs text-muted-foreground">{{ updated(detail.item) }}（北京时间）</span>
        </div>
        <Alert v-if="detail.item.error_code" class="mb-3" type="error" :message="`故障原因：${detail.item.error_code}`" />
        <Progress v-if="progress !== null" :percent="progress" :status="detail.item.execution_status === 'failed' ? 'exception' : undefined" />
        <div v-if="detail.item.progress?.phase" class="mb-3 text-xs text-muted-foreground">{{ detail.item.progress.phase }}</div>
        <div class="mb-3 grid grid-cols-2 gap-3 text-sm">
          <div><span class="text-muted-foreground">策略：</span>{{ detail.item.strategy || '未记录' }}</div>
          <div><span class="text-muted-foreground">模型：</span>{{ detail.item.model || detail.item.identifier || detail.item.version_id || '未记录' }}</div>
          <div class="col-span-2"><span class="text-muted-foreground">数据区间：</span>{{ detail.item.timerange || '未记录' }}</div>
          <div><span class="text-muted-foreground">平仓交易：</span>{{ formatRunMetric(values.trades, '', 0) }}</div>
          <div><span class="text-muted-foreground">净收益：</span>{{ formatRunMetric(values.profit_pct, '%') }}</div>
          <div><span class="text-muted-foreground">最大回撤：</span>{{ formatRunMetric(values.max_drawdown_pct, '%') }}</div>
          <div><span class="text-muted-foreground">Sharpe：</span>{{ formatRunMetric(values.sharpe) }}</div>
          <div class="col-span-2"><span class="text-muted-foreground">结果来源：</span>{{ source }}</div>
        </div>
        <Alert v-if="reasons.length" class="mb-3" type="info" message="本轮判据说明" :description="reasons.join('；')" />
        <div class="mb-2 text-sm font-medium">冻结产物与指纹</div>
        <pre class="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded border p-3 text-xs">{{ evidenceText }}</pre>
      </template>
    </Spin>
  </Modal>
</template>

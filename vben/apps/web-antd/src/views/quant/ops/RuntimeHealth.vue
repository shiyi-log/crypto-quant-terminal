<script setup lang="ts">
import { onMounted, onUnmounted, reactive } from 'vue';

import { Alert, Button, Card, Col, Row, Spin, Tag } from 'ant-design-vue';

import { getRuntimeHealth } from '#/api/runtime-health';

import { createHealthSnapshot, formatHealthMetric, healthStateView, refreshHealthSnapshot } from './runtimeHealthDisplay';

const snapshot = reactive(createHealthSnapshot());
const metricLabels: Record<string, string> = {
  write_failure_batches: '待重试写入批次',
  dead_letter_batches: '无法自动重试批次',
  data_gap_possible: '存在可能缺口',
  gap_alert_count: '缺口告警累计',
  pending_rows: '等待归档行数',
  pending_datasets: '等待归档数据集',
  errors: '同步错误',
  storage_errors: '存储错误',
  error_count: '检查问题',
  dataset_count: '检查数据集',
  failed_dataset_count: '检查失败数据集',
  successful_dataset_count: '检查成功数据集',
  external: '已配置外盘',
  free_bytes: '可用空间（字节）',
  received: '已接收消息', persisted: '已落库记录', spool_batches: '暂存批次',
  queue_length: '排队批次', database_failures: '数据库失败累计',
  dropped: '丢弃记录', corrupt_batches: '损坏批次',
  files: '本轮处理文件', rows: '本轮处理记录', rejected_rows: '拒绝记录',
  failures: '归档失败累计', rejected_offers: '归档拒绝批次',
};
const errorLabels: Record<string, string> = {
  status_missing: '尚无状态记录', status_invalid: '状态记录格式异常',
  status_stale: '状态更新已超期', status_clock_skew: '状态时间超前',
  status_timestamp_missing: '状态缺少更新时间',
  collection_no_messages: '尚未收到行情', collection_stale: '行情接收已超期',
  collection_storage_error: '采集落库异常', collection_error: '采集异常',
  collection_gap: '存在可能的数据缺口', storage_warning: '存储需要检查',
  database_error: '数据库写入异常', write_failures: '存在写入失败批次',
  dead_letter_batches: '存在无法自动重试的批次', sync_error: '文件同步失败',
  sync_unavailable: '文件同步状态暂不可用', sync_stale: '文件同步已超期',
  archive_error: '归档失败', archive_pending: '仍有数据等待归档',
  archive_unavailable: '归档状态暂不可用', archive_closed: '归档器已关闭',
  integrity_error: '数据完整性检查未通过', integrity_failed: '数据完整性检查未通过',
  storage_external_not_configured: '运行数据尚未切换至外盘',
  storage_unavailable: '外盘不可用', storage_identity_mismatch: '外盘身份不符',
  storage_not_writable: '外盘不可写', storage_low_space: '外盘空间不足',
  storage_space_low: '外盘空间不足',
  collection_not_running: '采集器未运行', collection_degraded: '采集器处于降级状态',
  collection_messages_stale: '行情接收已超期', collection_no_flush: '尚无落库成功记录',
  collection_flush_stale: '落库成功记录已超期', collection_database_error: '采集落库异常',
  collection_write_pending: '存在等待重试的写入批次',
  collection_dead_letter: '存在无法自动重试的采集批次', collection_corrupt: '存在损坏采集批次',
  collection_gap_possible: '存在可能的数据缺口', collection_storage_warning: '采集存储需要检查',
};

function timeText(value: string | null): string {
  if (!value || !Number.isFinite(Date.parse(value))) return '未记录';
  return new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
}

function metricRows(metrics: Record<string, boolean | number | null>) {
  return Object.entries(metrics).filter(([key]) => metricLabels[key]).map(([key, value]) => ({
    key, label: metricLabels[key], value: formatHealthMetric(value),
  }));
}

function errorText(codes: string[]): string {
  return codes.map((code) => errorLabels[code] ?? '存在未识别的状态告警').join('；');
}

async function load() { await refreshHealthSnapshot(snapshot, getRuntimeHealth); }
let timer: ReturnType<typeof setInterval> | null = null;
onMounted(() => { void load(); timer = setInterval(() => { void load(); }, 30_000); });
onUnmounted(() => { if (timer) clearInterval(timer); });
</script>

<template>
  <Card :bordered="false" class="mb-4 shadow-sm" title="数据管道与存储健康">
    <template #extra>
      <Button size="small" :loading="snapshot.loading" @click="load">刷新健康状态</Button>
    </template>
    <p class="mb-3 text-xs text-muted-foreground">
      采集、文件同步、归档和完整性检查分别验收。文件同步完成只说明来源文件已处理。时间均为北京时间。
    </p>
    <Alert v-if="snapshot.error" class="mb-3" type="warning" show-icon :message="snapshot.error"
      :description="snapshot.hasSnapshot ? '当前保留上次成功读取的快照。' : '尚未读取到运行健康快照。'" />
    <Spin :spinning="snapshot.loading && !snapshot.hasSnapshot">
      <template v-if="snapshot.value">
        <div class="mb-3 text-xs text-muted-foreground">
          快照 {{ timeText(snapshot.value.generated_at) }}
          <Tag :color="snapshot.value.ok ? 'green' : 'orange'" class="ml-2">
            {{ snapshot.value.ok ? '各项正常' : '部分状态需检查' }}
          </Tag>
        </div>
        <Row :gutter="[12, 12]">
          <Col v-for="item in snapshot.value.components" :key="item.key" :xs="24" :md="12" :xl="8">
            <div class="h-full rounded-lg border border-gray-100 p-3 dark:border-gray-800">
              <div class="mb-2 flex items-center justify-between gap-2">
                <span class="text-sm font-medium">{{ item.label }}</span>
                <Tag :color="healthStateView(item.state).color">{{ healthStateView(item.state).label }}</Tag>
              </div>
              <div class="text-xs text-muted-foreground">状态更新：{{ timeText(item.updated_at) }}</div>
              <div class="mt-1 text-xs text-muted-foreground">最近成功：{{ timeText(item.last_success_at) }}</div>
              <div v-for="metric in metricRows(item.metrics)" :key="metric.key" class="mt-1 text-xs">
                {{ metric.label }}：{{ metric.value }}
              </div>
              <div v-if="item.error_codes.length" class="mt-2 text-xs text-orange-600 dark:text-orange-400">
                {{ errorText(item.error_codes) }}
              </div>
            </div>
          </Col>
        </Row>
      </template>
      <div v-else class="py-4 text-center text-xs text-muted-foreground">
        {{ snapshot.loading ? '正在读取健康状态…' : '健康状态暂不可用。' }}
      </div>
    </Spin>
  </Card>
</template>

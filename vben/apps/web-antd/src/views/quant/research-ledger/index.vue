<script lang="ts" setup>
import { computed, onMounted, ref } from 'vue';

import {
  Alert,
  Button,
  Card,
  Empty,
  Select,
  SelectOption,
  Spin,
  Tag,
  Timeline,
  TimelineItem,
} from 'ant-design-vue';

import {
  getResearchLedgerEvents,
  type ResearchLedgerEvent,
} from '#/api/research-ledger';

const loading = ref(false);
const error = ref('');
const events = ref<ResearchLedgerEvent[]>([]);
const researchId = ref<string | undefined>();
const eventType = ref<string | undefined>();
const expanded = ref<Set<string>>(new Set());

const EVENT_LABELS: Record<string, string> = {
  risk_gate: '风险门控',
  manifest: '规格冻结',
  snapshot: '数据访问',
  decision: '统计裁定',
  deployment: '部署决策',
  liquidation_model_details: '强平详情',
  research_lesson: '研究教训',
  research_direction: '研究方向',
};

const EVENT_COLORS: Record<string, string> = {
  risk_gate: 'orange',
  manifest: 'blue',
  snapshot: 'cyan',
  decision: 'green',
  deployment: 'purple',
  liquidation_model_details: 'red',
};

function label(type: string): string {
  return EVENT_LABELS[type] ?? type;
}

function color(type: string): string {
  return EVENT_COLORS[type] ?? 'gray';
}

const counts = computed(() => {
  const result = new Map<string, number>();
  for (const item of events.value) {
    result.set(item.event_type, (result.get(item.event_type) ?? 0) + 1);
  }
  return [...result.entries()].sort((a, b) => b[1] - a[1]);
});

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const response = await getResearchLedgerEvents({
      event_type: eventType.value,
      limit: 300,
      research_id: researchId.value || undefined,
    });
    // research_id 由后端按 payload 过滤；这里不做二次猜测
    events.value = response.events;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
    events.value = [];
  } finally {
    loading.value = false;
  }
}

function toggle(eventId: string) {
  const next = new Set(expanded.value);
  if (next.has(eventId)) next.delete(eventId);
  else next.add(eventId);
  expanded.value = next;
}

function formatTime(timestamp: string): string {
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? timestamp : date.toLocaleString('zh-CN');
}

onMounted(() => {
  void load();
});
</script>

<template>
  <div class="p-4">
    <Card title="研究账本 · 不可变审计事件">
      <template #extra>
        <Button :loading="loading" size="small" @click="load">刷新</Button>
      </template>

      <Alert
        v-if="error"
        class="mb-3"
        show-icon
        type="error"
        :message="`加载失败：${error}`"
      />

      <div class="mb-4 flex flex-wrap items-end gap-3">
        <div>
          <div class="mb-1 text-xs text-gray-500">研究ID</div>
          <input
            v-model="researchId"
            class="rounded border border-gray-300 px-2 py-1 text-sm"
            placeholder="如 s01_forward_2026"
            @keyup.enter="load"
          />
        </div>
        <div>
          <div class="mb-1 text-xs text-gray-500">事件类型</div>
          <Select
            v-model:value="eventType"
            allow-clear
            class="w-48"
            placeholder="全部类型"
            size="small"
            @change="load"
          >
            <SelectOption v-for="(text, key) in EVENT_LABELS" :key="key" :value="key">
              {{ text }}
            </SelectOption>
          </Select>
        </div>
        <Button size="small" type="primary" @click="load">筛选</Button>
      </div>

      <div v-if="counts.length" class="mb-4 flex flex-wrap gap-2">
        <Tag v-for="[type, count] in counts" :key="type" :color="color(type)">
          {{ label(type) }} {{ count }}
        </Tag>
      </div>

      <Spin :spinning="loading">
        <Empty v-if="!loading && events.length === 0" description="暂无账本事件" />

        <Timeline v-else>
          <TimelineItem v-for="event in events" :key="event.event_id" :color="color(event.event_type)">
            <div class="cursor-pointer" @click="toggle(event.event_id)">
              <div class="flex flex-wrap items-center gap-2">
                <Tag :color="color(event.event_type)">{{ label(event.event_type) }}</Tag>
                <span class="text-xs text-gray-500">{{ formatTime(event.timestamp) }}</span>
                <Tag v-if="event.research_id" color="purple">{{ event.research_id }}</Tag>
                <Tag v-if="event.gate_type">{{ event.gate_type }}</Tag>
              </div>
              <div class="mt-1 font-mono text-[11px] text-gray-400">
                {{ event.event_id }}
              </div>
              <pre
                v-if="expanded.has(event.event_id)"
                class="mt-2 max-h-64 overflow-auto rounded bg-gray-50 p-2 text-[11px]"
              >{{ JSON.stringify(event.payload, null, 2) }}</pre>
            </div>
          </TimelineItem>
        </Timeline>
      </Spin>
    </Card>
  </div>
</template>

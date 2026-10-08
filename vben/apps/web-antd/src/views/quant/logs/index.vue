<script lang="ts" setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';

import { Card, Radio, Space, Switch } from 'ant-design-vue';

import { getLogs } from '#/api/freqtrade';

import { levelCn, translateLog } from '../utils/logText';

const logs = ref<any[]>([]);
const total = ref(0);
const level = ref('');
const autoScroll = ref(true);
const showRaw = ref(false);
const boxRef = ref<HTMLElement>();
const atBottom = ref(true);

/** 是否停在底部（决定要不要继续自动滚动） */
function onScroll() {
  const b = boxRef.value;
  if (!b) return;
  atBottom.value = b.scrollHeight - b.scrollTop - b.clientHeight < 60;
}

function jumpToLatest() {
  const b = boxRef.value;
  if (b) b.scrollTop = b.scrollHeight;
}

// ⚠️ 不要 reverse：日志按【旧 → 新】显示，最新的在底部。
//    之前是「新→旧」显示却又「滚到底部」，结果用户看到的是最旧的日志，
//    最新的反而在顶部看不见 —— 排序方向与滚动方向必须一致。
const shown = computed(() =>
  logs.value.filter((l) => !level.value || l[3] === level.value),
);

const localTime = (v: string) => {
  if (!v) return '';
  const s = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(v) && !/[Zz]|[+-]\d{2}:?\d{2}$/.test(v)
    ? `${v.replace(' ', 'T')}Z`
    : v;
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return String(v);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

const levelColor = (lv: string) =>
  lv === 'ERROR'
    ? 'text-red-500'
    : lv === 'WARNING'
      ? 'text-amber-500'
      : lv === 'DEBUG'
        ? 'text-gray-400'
        : 'text-gray-500';

async function load() {
  try {
    const d = await getLogs(200);
    logs.value = d.logs ?? [];
    total.value = d.log_count ?? 0;
    if (autoScroll.value && atBottom.value) {
      requestAnimationFrame(() => {
        if (boxRef.value) boxRef.value.scrollTop = boxRef.value.scrollHeight;
      });
    }
  } catch {
    /* 单次失败忽略 */
  }
}

let timer: any = null;
onMounted(() => {
  load();
  timer = setInterval(load, 4000);
});
onUnmounted(() => clearInterval(timer));

</script>

<template>
  <div class="p-4">
    <Card :bordered="false" class="shadow-sm" title="运行日志">
      <template #extra>
        <Space>
          <span class="text-xs text-gray-400">共 {{ total }} 条</span>
          <Radio.Group v-model:value="level" button-style="solid" size="small">
            <Radio.Button value="">全部</Radio.Button>
            <Radio.Button value="INFO">信息</Radio.Button>
            <Radio.Button value="WARNING">警告</Radio.Button>
            <Radio.Button value="ERROR">错误</Radio.Button>
          </Radio.Group>
          <span class="text-xs text-gray-400">
            显示原文
            <Switch v-model:checked="showRaw" class="ml-1" size="small" />
          </span>
          <span class="text-xs text-gray-400">
            自动滚动
            <Switch v-model:checked="autoScroll" class="ml-1" size="small" />
          </span>
          <a
            v-if="!atBottom"
            class="text-xs text-blue-500"
            @click="jumpToLatest"
          >
            跳到最新 ↓
          </a>
        </Space>
      </template>

      <div
        ref="boxRef"
        @scroll="onScroll"
        class="max-h-[66vh] overflow-auto rounded border border-gray-100 bg-gray-50/50 font-mono text-xs dark:border-gray-700 dark:bg-gray-900/40"
      >
        <div v-if="!shown.length" class="py-16 text-center text-gray-400">
          暂无日志
        </div>
        <div
          v-for="(l, i) in shown"
          :key="i"
          class="flex gap-2 border-b border-gray-100 px-3 py-1.5 hover:bg-gray-100/60 dark:border-gray-800 dark:hover:bg-gray-800/40"
        >
          <span class="w-16 shrink-0 text-gray-400">{{ localTime(l[0]) }}</span>
          <span class="w-16 shrink-0 font-semibold" :class="levelColor(l[3])">
            {{ levelCn(l[3]) }}
          </span>
          <span class="min-w-0 break-words whitespace-pre-wrap text-gray-600 dark:text-gray-300">
            {{ showRaw ? l[4] : translateLog(l[4]) }}
          </span>
        </div>
      </div>
    </Card>
  </div>
</template>

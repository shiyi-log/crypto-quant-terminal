<script lang="ts" setup>
/**
 * 模型训练 / 回测任务进度横幅
 *
 * 数据来自 run_backtest_task.py 写入的 /run_progress.json（经认证服务代理）。
 * 放在每个页面顶部，任务运行时可全局看到进度。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue';

import { getRunProgress } from '#/api/freqtrade';

import { translateLog } from '../utils/logText';

const props = withDefaults(
  defineProps<{ alwaysShow?: boolean }>(),
  { alwaysShow: false },
);

interface Progress {
  status?: string;
  phase?: string;
  model?: string;
  strategy?: string;
  timerange?: string;
  current_pair?: string;
  current_train?: number;
  trains_total_per_pair?: number;
  trained?: number;
  total?: number;
  percent?: number;
  rate_per_min?: number;
  eta_seconds?: number;
  elapsed_seconds?: number;
  updated_at?: string;
  recent_log?: string[];
  result?: any;
}

const p = ref<Progress | null>(null);
const showLog = ref(false);

const running = computed(() => p.value?.status === 'running');
const visible = computed(() => running.value || props.alwaysShow);
const percent = computed(() => Math.min(100, Math.max(0, p.value?.percent ?? 0)));

const stateText = computed(() => {
  const s = p.value?.status;
  return s === 'done' ? '已完成' : s === 'failed' ? '失败' : '运行中';
});

const stateColor = computed(() => {
  const s = p.value?.status;
  return s === 'done'
    ? 'bg-emerald-500'
    : s === 'failed'
      ? 'bg-red-500'
      : 'bg-blue-500';
});

const barColor = computed(() => {
  const s = p.value?.status;
  return s === 'failed'
    ? 'bg-gradient-to-r from-red-500 to-amber-500'
    : s === 'done'
      ? 'bg-gradient-to-r from-emerald-500 to-emerald-400'
      : 'bg-gradient-to-r from-blue-500 to-emerald-500';
});

const etaText = computed(() => {
  const s = p.value?.eta_seconds;
  if (!running.value || s === null || s === undefined) {
    return running.value ? '计算中' : '—';
  }
  return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
});

function dur(sec?: number) {
  const s = Math.max(0, Math.floor(sec ?? 0));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d}天${h}小时`;
  if (h) return `${h}小时${m}分`;
  return `${m}分${s % 60}秒`;
}

function localTime(v?: string) {
  if (!v) return '—';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return String(v);
  const q = (n: number) => String(n).padStart(2, '0');
  return `${q(d.getHours())}:${q(d.getMinutes())}:${q(d.getSeconds())}`;
}

async function load() {
  const d = await getRunProgress();
  if (d) {
    p.value = d;
  }
}

let timer: any = null;
onMounted(() => {
  load();
  timer = setInterval(load, 3000);
});
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <!--
    页面根节点已有 p-4 内边距，这里不能再加 px，否则横幅会比下方卡片窄；
    同时必须有下边距，否则会和紧随其后的卡片贴在一起。
  -->
  <div v-if="visible" class="mb-4">
    <div
      class="rounded-xl border p-4 shadow-sm transition-colors"
      :class="
        running
          ? 'border-blue-200 bg-blue-50/60 dark:border-blue-900 dark:bg-blue-950/30'
          : p?.status === 'failed'
            ? 'border-red-200 bg-red-50/60 dark:border-red-900 dark:bg-red-950/30'
            : 'border-emerald-200 bg-emerald-50/60 dark:border-emerald-900 dark:bg-emerald-950/30'
      "
    >
      <!-- 头部 -->
      <div class="mb-3 flex flex-wrap items-center gap-2">
        <span class="relative flex h-2.5 w-2.5">
          <span
            v-if="running"
            class="absolute inline-flex h-full w-full animate-ping rounded-full opacity-75"
            :class="stateColor"
          ></span>
          <span class="relative inline-flex h-2.5 w-2.5 rounded-full" :class="stateColor"></span>
        </span>
        <span class="text-sm font-semibold">
          {{ running ? '模型训练中' : `任务${stateText}` }}
        </span>
        <span class="text-xs text-muted-foreground">
          {{ p?.model }} · {{ p?.strategy }}
        </span>
        <span class="flex-1"></span>
        <span class="text-xs text-muted-foreground">
          {{ p?.timerange }} · 更新于 {{ localTime(p?.updated_at) }}
        </span>
        <a
          v-if="p?.recent_log?.length"
          class="text-xs text-blue-500 hover:underline"
          @click="showLog = !showLog"
        >
          {{ showLog ? '收起日志' : '最新日志' }}
        </a>
      </div>

      <!-- 进度条 -->
      <div class="mb-2 flex items-baseline justify-between">
        <span class="text-sm font-medium">
          {{ p?.phase || '—' }}
          <span v-if="p?.current_pair" class="ml-1 text-xs text-muted-foreground">
            {{ p.current_pair }}
            <template v-if="p?.trains_total_per_pair">
              · 第 {{ p.current_train }}/{{ p.trains_total_per_pair }} 轮
            </template>
          </span>
        </span>
        <span class="font-mono text-lg font-semibold">
          {{ percent.toFixed(1) }}%
        </span>
      </div>
      <div class="h-2 w-full overflow-hidden rounded-full bg-white/70 dark:bg-black/30">
        <div
          class="h-full rounded-full transition-all duration-500"
          :class="barColor"
          :style="{ width: `${percent}%` }"
        ></div>
      </div>

      <!-- 指标 -->
      <div class="mt-3 grid grid-cols-2 gap-3 text-xs sm:grid-cols-3 lg:grid-cols-6">
        <div>
          <div class="text-muted-foreground">已训练轮次</div>
          <div class="font-mono text-sm font-semibold">
            {{ p?.trained ?? 0 }}
            <span class="font-normal text-muted-foreground">/ {{ p?.total ?? '—' }}</span>
          </div>
        </div>
        <div>
          <div class="text-muted-foreground">训练速率</div>
          <div class="font-mono text-sm font-semibold">
            {{ p?.rate_per_min ?? '—' }}
            <span class="font-normal text-muted-foreground">轮/分</span>
          </div>
        </div>
        <div>
          <div class="text-muted-foreground">预计剩余</div>
          <div class="font-mono text-sm font-semibold">{{ etaText }}</div>
        </div>
        <div>
          <div class="text-muted-foreground">已用时间</div>
          <div class="font-mono text-sm font-semibold">{{ dur(p?.elapsed_seconds) }}</div>
        </div>
        <div>
          <div class="text-muted-foreground">当前币对</div>
          <div class="font-mono text-sm font-semibold">{{ p?.current_pair ?? '—' }}</div>
        </div>
        <div v-if="p?.result && !p.result.error">
          <div class="text-muted-foreground">最终收益</div>
          <div
            class="font-mono text-sm font-semibold"
            :class="p.result.profit_pct > 0 ? 'text-red-500' : 'text-emerald-500'"
          >
            {{ p.result.profit_pct > 0 ? '+' : '' }}{{ p.result.profit_pct }}%
          </div>
        </div>
      </div>

      <!-- 日志 -->
      <div
        v-if="showLog && p?.recent_log?.length"
        class="mt-3 max-h-40 overflow-auto rounded bg-black/80 p-2 font-mono text-[11px] leading-relaxed text-gray-300"
      >
        <div v-for="(l, i) in p.recent_log.slice(-12)" :key="i" class="truncate">
          {{ translateLog(l) }}
        </div>
      </div>
    </div>
  </div>
</template>

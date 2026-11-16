<script lang="ts" setup>
/**
 * 顶部机器人控制条
 *
 * 放在 BasicLayout 的 header-right-1 插槽里，全局可见，随时能看状态、动开关。
 * 只做两件事：读状态（每 5 秒轮询 show_config + count），写操作（全部二次确认）。
 *
 * 注意：这里直连 Freqtrade 的 /v1/*，没有额外的权限层，
 * 所以每一个写操作都必须先用 Modal.confirm 明确写出后果再执行。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue';

import { Alert, Button, Modal, Tag, message } from 'ant-design-vue';

import {
  getConfig,
  getCount,
  postForceExit,
  postReloadConfig,
  postStartBot,
  postStopBot,
  postStopBuy,
} from '#/api/freqtrade';
import { onFtWsMessage, useFtWs } from '#/views/quant/utils/useFtWs';

/** 机器人运行状态：'running' / 'stopped' / 'paused' */
const state = ref<string>('');
const connectionError = ref('');
const available = ref(false);
const countAvailable = ref(false);
const dryRun = ref(false);
const strategy = ref('');
const tradingMode = ref('');
const exchange = ref('');
const configKnown = ref(false);
const countKnown = ref(false);
const configFresh = ref(false);
const countFresh = ref(false);

/** 开仓数 current / 上限 max */
const current = ref(0);
const maxOpen = ref<number | string>('—');
const maxOpenText = computed(() =>
  Number(maxOpen.value) === -1 ? '不限' : maxOpen.value,
);

/** 正在执行的动作名，既用于按钮 loading，也用于期间禁用其它按钮 */
const busy = ref('');

/** 状态标签：运行中 / 已暂停买入 / 已停止 */
const status = computed<{ color?: string; text: string }>(() => {
  if (!available.value)
    return {
      color: 'warning',
      text: connectionError.value ? '旧引擎状态未知' : '正在连接旧引擎',
    };
  if (state.value === 'running')
    return {
      color: 'green',
      text: configFresh.value ? '运行中' : '运行中 · 快照过期',
    };
  if (state.value === 'paused')
    return {
      color: 'orange',
      text: configFresh.value ? '已暂停买入' : '已暂停买入 · 快照过期',
    };
  if (state.value === 'stopped')
    return {
      color: undefined,
      text: configFresh.value ? '已停止' : '已停止 · 快照过期',
    };
  return { color: 'warning', text: '状态未知' };
});

let loadSequence = 0;
let disposed = false;
async function load() {
  const sequence = ++loadSequence;
  // 两个接口互不依赖，任一失败也不能让整条控制条消失
  const [cfg, cnt] = await Promise.allSettled([getConfig(), getCount()]);
  if (disposed || sequence !== loadSequence) return;
  const validConfig =
    cfg.status === 'fulfilled' &&
    cfg.value &&
    typeof cfg.value === 'object' &&
    typeof cfg.value.dry_run === 'boolean' &&
    (cfg.value.state === undefined || typeof cfg.value.state === 'string');
  const validCount =
    cnt.status === 'fulfilled' &&
    cnt.value &&
    typeof cnt.value === 'object' &&
    typeof cnt.value.current === 'number' &&
    Number.isSafeInteger(cnt.value.current) &&
    cnt.value.current >= 0 &&
    typeof cnt.value.max === 'number' &&
    Number.isFinite(cnt.value.max) &&
    Number(cnt.value.max) >= -1;
  configFresh.value = Boolean(validConfig);
  countFresh.value = Boolean(validCount);
  if (validConfig) configKnown.value = true;
  if (validCount) countKnown.value = true;
  available.value = Boolean(validConfig);
  countAvailable.value = Boolean(validCount);
  connectionError.value = !validConfig
    ? '未能核实旧 Freqtrade 引擎与运行环境；所有控制命令已禁用。'
    : !validCount
      ? '持仓数量读取无效或失败；全部平仓命令已禁用。'
      : '';
  if (validConfig) {
    const c = cfg.value;
    state.value = c.state ?? '';
    dryRun.value = c.dry_run === true;
    strategy.value = c.strategy ?? '';
    tradingMode.value = c.trading_mode ?? '';
    exchange.value = c.exchange ?? '';
    maxOpen.value = c.max_open_trades ?? '—';
  }
  if (validCount) {
    current.value = Number(cnt.value.current);
    maxOpen.value = cnt.value.max ?? maxOpen.value;
  }
  available.value = configFresh.value;
  countAvailable.value = countFresh.value;
}

let timer: any = null;
/**
 * WebSocket：成交/撤单回报到达时立即刷新持仓计数。
 * 5 秒轮询保留作为兜底，WS 不可用时行为与之前一致。
 */
let offWs: (() => void) | null = null;
const fills = new Set(['entry_fill', 'exit_fill']);
const { connected: wsConnected } = useFtWs();

onMounted(() => {
  load();
  timer = setInterval(load, 5000);
  offWs = onFtWsMessage((m) => {
    if (fills.has(m.type)) {
      load();
    }
  });
});
onUnmounted(() => {
  disposed = true;
  loadSequence++;
  clearInterval(timer);
  offWs?.();
});

/** 执行写操作：期间上锁，成功后提示并立即刷新，失败提示可读错误 */
async function execute(
  name: string,
  action: () => Promise<any>,
  okMsg: string,
) {
  const allowed =
    name === 'start'
      ? available.value && ['stopped', 'paused'].includes(state.value)
      : name === 'forceexit'
        ? available.value && countAvailable.value && current.value > 0
        : name === 'reload'
          ? available.value
          : available.value && state.value === 'running';
  if (!allowed || disposed) return;
  busy.value = name;
  try {
    const acknowledgement = await action();
    if (disposed) return;
    const acknowledged =
      acknowledgement &&
      typeof acknowledgement === 'object' &&
      !Array.isArray(acknowledgement) &&
      acknowledgement.error === undefined &&
      !['error', 'failed', 'rejected', 'denied'].includes(
        String(
          acknowledgement.status ?? acknowledgement.result ?? '',
        ).toLowerCase(),
      ) &&
      !/\b(?:error|failed|rejected|denied)\b/i.test(
        String(acknowledgement.result ?? ''),
      ) &&
      (typeof acknowledgement.result === 'string' ||
        typeof acknowledgement.status === 'string');
    if (acknowledged) message.info(okMsg);
    else message.warning('命令回执无效，结果未知；请刷新旧引擎状态核验。');
    await load();
  } catch {
    if (!disposed) message.error('命令结果未知，请刷新旧引擎状态核验。');
  } finally {
    if (!disposed) busy.value = '';
  }
}

/** 统一入口：任何写操作都必须先过二次确认弹窗 */
function confirmAction(opts: {
  /** 动作名，对应 busy 的值 */
  name: string;
  title: string;
  content: string;
  okText: string;
  danger?: boolean;
  action: () => Promise<any>;
  okMsg: string;
}) {
  Modal.confirm({
    title: opts.title,
    content: opts.content,
    okText: opts.okText,
    cancelText: '取消',
    okButtonProps: { danger: opts.danger === true },
    onOk: () => execute(opts.name, opts.action, opts.okMsg),
  });
}

function onStart() {
  confirmAction({
    name: 'start',
    title: '启动机器人',
    content: `启动后旧 Freqtrade 引擎会扫描行情，并可能开立新仓。当前环境为 ${dryRun.value ? '模拟盘 dry-run' : '实盘配置'}。确认后只是提交启动命令。`,
    okText: '确定启动',
    action: () => postStartBot(),
    okMsg: '启动命令已提交，状态待后续查询核验',
  });
}

function onStop() {
  confirmAction({
    name: 'stop',
    title: '停止机器人',
    content:
      '停止旧引擎后不再开新仓，也不再处理已有持仓。确认后只是提交停止命令。',
    okText: '确定停止',
    danger: true,
    action: () => postStopBot(),
    okMsg: '停止命令已提交，状态待后续查询核验',
  });
}

function onStopBuy() {
  confirmAction({
    name: 'stopbuy',
    title: '暂停买入',
    content:
      '暂停后旧引擎将不再开新仓、不加仓，已有持仓仍按策略管理。确认后只是提交暂停命令。',
    okText: '确定暂停买入',
    action: () => postStopBuy(),
    okMsg: '暂停命令已提交，状态待后续查询核验',
  });
}

function onReload() {
  confirmAction({
    name: 'reload',
    title: '重载配置',
    content:
      '重新加载旧引擎配置与策略，运行期临时改动会丢失。确认后只是提交重载命令。',
    okText: '确定重载',
    action: () => postReloadConfig(),
    okMsg: '重载命令已提交，状态待后续查询核验',
  });
}

function onForceExit() {
  /**
   * ⚠️ 必须显式传 ordertype: 'market'，不能依赖服务端默认值。
   *
   * rpc.py 的 __exec_force_exit 里是：
   *   order_type = ordertype or strategy.order_types.get("force_exit", strategy.order_types["exit"])
   * 而本 bot 的 order_types 中没有 force_exit 键（实测 /v1/show_config：
   * entry/exit 都是 limit），于是会回退到 exit = 'limit' —— 不传 ordertype
   * 会挂一张限价单，可能迟迟不成交，与「立即全部平仓」的语义正好相反。
   */
  confirmAction({
    name: 'forceexit',
    title: '全部平仓',
    content: `向旧 Freqtrade 引擎提交对 ${current.value} 笔持仓的市价退出命令。命令回执不代表交易所已成交。`,
    okText: '确定全部平仓',
    danger: true,
    action: () => postForceExit({ ordertype: 'market', tradeid: 'all' }),
    okMsg: '退出请求已提交，成交待后续查询核验',
  });
}
</script>

<template>
  <div class="space-y-3">
    <Alert
      v-if="connectionError"
      :message="connectionError"
      type="warning"
      show-icon
    />
    <!-- 独立业务工具栏允许换行，状态文字和操作按钮保持完整可见。 -->
    <div class="flex flex-wrap items-center gap-x-3 gap-y-2 text-sm">
      <Tag :color="status.color" class="m-0">{{ status.text }}</Tag>

      <!-- 实时推送指示灯：绿=WS 已连接，灰=退回轮询（功能不受影响） -->
      <span
        class="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
        :class="wsConnected ? 'bg-emerald-500' : 'bg-gray-400'"
        :title="
          wsConnected ? '实时推送已连接' : '未连接实时推送，当前为轮询模式'
        "
      ></span>

      <!-- 模拟盘必须一眼可见：实盘 bot 若跑在 dry-run，所有盈亏都不是真钱 -->
      <Tag
        v-if="configKnown"
        :color="dryRun ? 'red' : 'orange'"
        class="m-0 font-medium"
      >
        {{ dryRun ? '旧引擎 · 模拟盘 dry-run' : '旧引擎 · 实盘配置'
        }}{{ configFresh ? '' : ' · 快照过期' }}
      </Tag>
      <Tag v-else color="orange" class="m-0 font-medium">旧引擎 · 环境未知</Tag>

      <span class="shrink-0 text-gray-500 dark:text-gray-400">
        持仓
        <span class="font-medium text-gray-800 dark:text-gray-200">
          {{
            countFresh ? current : countKnown ? current + '（旧快照）' : '—'
          }}/{{ countKnown ? maxOpenText : '—' }}
        </span>
      </span>

      <span
        v-if="available && strategy"
        class="shrink-0 text-gray-500 dark:text-gray-400"
        :title="`策略：${strategy}`"
      >
        策略
        <span class="text-gray-800 dark:text-gray-200">{{ strategy }}</span>
      </span>

      <span
        v-if="available && (tradingMode || exchange)"
        class="shrink-0 text-gray-500 dark:text-gray-400"
      >
        {{ tradingMode }}<template v-if="tradingMode && exchange"> · </template
        >{{ exchange }}
      </span>

      <span class="mx-1 h-4 w-px shrink-0 bg-gray-200 dark:bg-gray-700"></span>

      <Button
        data-command="start"
        :disabled="
          !available || busy !== '' || !['stopped', 'paused'].includes(state)
        "
        :loading="busy === 'start'"
        size="small"
        @click="onStart"
      >
        启动
      </Button>
      <Button
        data-command="stop"
        :disabled="!available || busy !== '' || state !== 'running'"
        :loading="busy === 'stop'"
        size="small"
        @click="onStop"
      >
        停止
      </Button>
      <Button
        data-command="stopbuy"
        :disabled="!available || busy !== '' || state !== 'running'"
        :loading="busy === 'stopbuy'"
        size="small"
        @click="onStopBuy"
      >
        暂停买入
      </Button>
      <Button
        data-command="reload"
        :disabled="!available || busy !== ''"
        :loading="busy === 'reload'"
        size="small"
        @click="onReload"
      >
        重载配置
      </Button>
      <Button
        data-command="forceexit"
        danger
        :disabled="!available || !countAvailable || busy !== '' || current <= 0"
        :loading="busy === 'forceexit'"
        size="small"
        @click="onForceExit"
      >
        全部平仓
      </Button>
    </div>
  </div>
</template>

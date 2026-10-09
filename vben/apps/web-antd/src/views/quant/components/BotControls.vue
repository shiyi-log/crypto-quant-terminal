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
  errText,
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

/** 开仓数 current / 上限 max */
const current = ref(0);
const maxOpen = ref<number | string>('—');

/** 正在执行的动作名，既用于按钮 loading，也用于期间禁用其它按钮 */
const busy = ref('');

/** 状态标签：运行中 / 已暂停买入 / 已停止 */
const status = computed<{ color?: string; text: string }>(() => {
  if (!available.value) return { color: 'warning', text: connectionError.value ? '交易服务不可用' : '正在连接' };
  if (state.value === 'running') return { color: 'green', text: '运行中' };
  if (state.value === 'paused') return { color: 'orange', text: '已暂停买入' };
  if (state.value === 'stopped') return { color: undefined, text: '已停止' };
  return { color: 'warning', text: '状态未知' };
});

async function load() {
  // 两个接口互不依赖，任一失败也不能让整条控制条消失
  const [cfg, cnt] = await Promise.allSettled([getConfig(), getCount()]);
  available.value = cfg.status === 'fulfilled';
  countAvailable.value = cnt.status === 'fulfilled';
  connectionError.value = cfg.status === 'rejected'
    ? '无法读取交易状态，请检查交易 API 服务。服务恢复后会自动重连。'
    : cnt.status === 'rejected'
      ? '持仓数量读取失败，暂时无法执行全部平仓。'
      : '';
  if (cfg.status === 'fulfilled') {
    const c = cfg.value;
    state.value = c.state ?? '';
    dryRun.value = c.dry_run === true;
    strategy.value = c.strategy ?? '';
    tradingMode.value = c.trading_mode ?? '';
    exchange.value = c.exchange ?? '';
    maxOpen.value = c.max_open_trades ?? '—';
  }
  if (cnt.status === 'fulfilled') {
    current.value = Number(cnt.value.current ?? 0);
    maxOpen.value = cnt.value.max ?? maxOpen.value;
  }
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
  clearInterval(timer);
  offWs?.();
});

/** 执行写操作：期间上锁，成功后提示并立即刷新，失败提示可读错误 */
async function execute(name: string, action: () => Promise<any>, okMsg: string) {
  busy.value = name;
  try {
    await action();
    message.success(okMsg);
    await load();
  } catch (e) {
    message.error(errText(e));
  } finally {
    busy.value = '';
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
    content:
      '启动后机器人会立即开始扫描行情并可能开新仓，将使用真实交易逻辑（当前若为模拟盘则只记模拟成交）。确定启动？',
    okText: '确定启动',
    action: () => postStartBot(),
    okMsg: '机器人已启动',
  });
}

function onStop() {
  confirmAction({
    name: 'stop',
    title: '停止机器人',
    content: '停止后不再开新仓，也不再处理已有持仓。确定停止机器人？',
    okText: '确定停止',
    danger: true,
    action: () => postStopBot(),
    okMsg: '机器人已停止',
  });
}

function onStopBuy() {
  confirmAction({
    name: 'stopbuy',
    title: '暂停买入',
    content:
      '暂停后将不再开新仓、不加仓，已有持仓仍会继续按策略管理直到平仓。确定暂停买入？',
    okText: '确定暂停买入',
    action: () => postStopBuy(),
    okMsg: '已暂停买入',
  });
}

function onReload() {
  confirmAction({
    name: 'reload',
    title: '重载配置',
    content:
      '将重新加载配置文件与策略，运行期临时改动会丢失。确定重载？',
    okText: '确定重载',
    action: () => postReloadConfig(),
    okMsg: '配置已重载',
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
    content: `将以市价立即平掉当前 ${current.value} 笔持仓，操作不可撤销。确定全部平仓？`,
    okText: '确定全部平仓',
    danger: true,
    action: () => postForceExit({ ordertype: 'market', tradeid: 'all' }),
    okMsg: '已提交全部平仓（市价）',
  });
}
</script>

<template>
  <div class="space-y-3">
    <Alert v-if="connectionError" :message="connectionError" type="warning" show-icon />
  <!-- 独立业务工具栏允许换行，状态文字和操作按钮保持完整可见。 -->
  <div
    class="flex flex-wrap items-center gap-x-3 gap-y-2 text-sm"
  >
    <Tag :color="status.color" class="m-0">{{ status.text }}</Tag>

    <!-- 实时推送指示灯：绿=WS 已连接，灰=退回轮询（功能不受影响） -->
    <span
      class="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
      :class="wsConnected ? 'bg-emerald-500' : 'bg-gray-400'"
      :title="wsConnected ? '实时推送已连接' : '未连接实时推送，当前为轮询模式'"
    ></span>

    <!-- 模拟盘必须一眼可见：实盘 bot 若跑在 dry-run，所有盈亏都不是真钱 -->
    <Tag v-if="available && dryRun" color="red" class="m-0 font-medium">
      模拟盘 dry-run
    </Tag>

    <span class="shrink-0 text-gray-500 dark:text-gray-400">
      持仓
      <span class="font-medium text-gray-800 dark:text-gray-200">
        {{ available && countAvailable ? current : '—' }}/{{ available ? maxOpen : '—' }}
      </span>
    </span>

    <span
      v-if="available && strategy"
      class="shrink-0 text-gray-500 dark:text-gray-400"
      :title="`策略：${strategy}`"
    >
      策略 <span class="text-gray-800 dark:text-gray-200">{{ strategy }}</span>
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
      :disabled="!available || busy !== '' || state === 'running'"
      :loading="busy === 'start'"
      size="small"
      @click="onStart"
    >
      启动
    </Button>
    <Button
      :disabled="!available || busy !== '' || state !== 'running'"
      :loading="busy === 'stop'"
      size="small"
      @click="onStop"
    >
      停止
    </Button>
    <Button
      :disabled="!available || busy !== '' || state !== 'running'"
      :loading="busy === 'stopbuy'"
      size="small"
      @click="onStopBuy"
    >
      暂停买入
    </Button>
    <Button
      :disabled="!available || busy !== ''"
      :loading="busy === 'reload'"
      size="small"
      @click="onReload"
    >
      重载配置
    </Button>
    <Button
      danger
      :disabled="!available || !countAvailable || busy !== '' || current === 0"
      :loading="busy === 'forceexit'"
      size="small"
      @click="onForceExit"
    >
      全部平仓
    </Button>
  </div>
  </div>
</template>

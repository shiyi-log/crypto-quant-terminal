<script lang="ts" setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue';

import {
  Button,
  Card,
  Dropdown,
  InputNumber,
  Menu,
  MenuItem,
  message,
  Modal,
  Table,
  Tag,
} from 'ant-design-vue';

import {
  cancelOpenOrder,
  deleteTrade,
  errText,
  getBalance,
  getOpenTrades,
  getTradeCustomData,
  getTrades,
  postForceExit,
  reloadTrade,
} from '#/api/freqtrade';
import { onFtWsMessage, useFtWs } from '#/views/quant/utils/useFtWs';

const open = ref<any[]>([]);
const trades = ref<any[]>([]);
const total = ref(0);
const bal = ref<any>({});
const loading = ref(false);

/** 持仓时长：只用真实已平仓成交算，不再写死「约 33 天」 */
const holdStats = computed(() => {
  const days = trades.value
    .filter((t: any) => t.open_date && t.close_date)
    .map(
      (t: any) =>
        (new Date(t.close_date).getTime() - new Date(t.open_date).getTime()) /
        86_400_000,
    )
    .filter((d: number) => Number.isFinite(d) && d >= 0)
    .sort((a, b) => a - b);
  if (!days.length) return null;
  return {
    mean: Math.round(days.reduce((a, b) => a + b, 0) / days.length),
    median: Math.round(days[Math.floor(days.length / 2)]!),
    n: days.length,
  };
});

const localTime = (v?: string) => {
  if (!v) return '—';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return String(v);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
const fmt = (v: any, n = 2) =>
  v === null || v === undefined || Number.isNaN(Number(v))
    ? '—'
    : Number(v).toFixed(n);
const pct = (t: any) =>
  t.profit_ratio !== undefined ? t.profit_ratio * 100 : (t.profit_pct ?? 0);

/**
 * 「最近成交」的行。
 *
 * Freqtrade 的 /v1/trades 只返回【已平仓】成交，当前一笔都没平，
 * 表格就整片空白 —— 但持仓里明明有 10 笔开仓记录。
 * 这里把持仓中的开仓记录并入，标记 is_open，前端显示为「持仓中」+ 浮动盈亏。
 */
const recentRows = computed(() => [
  ...open.value.map((t: any) => ({ ...t, is_open: true })),
  ...trades.value,
]);

const openCols = [
  { dataIndex: 'pair', key: 'pair', title: '币对' },
  { key: 'side', title: '方向', width: 80 },
  { dataIndex: 'open_rate', key: 'open_rate', title: '开仓价', align: 'right' as const },
  { dataIndex: 'current_rate', key: 'current_rate', title: '现价', align: 'right' as const },
  { dataIndex: 'stake_amount', key: 'stake_amount', title: '投入', align: 'right' as const },
  { dataIndex: 'leverage', key: 'leverage', title: '杠杆', align: 'right' as const, width: 70 },
  { key: 'pnl', title: '浮动盈亏', align: 'right' as const },
  { dataIndex: 'open_date', key: 'open_date', title: '开仓时间', width: 120 },
  { key: 'action', title: '操作', width: 90, fixed: 'right' as const },
];

const tradeCols = [
  { dataIndex: 'pair', key: 'pair', title: '币对' },
  { key: 'side', title: '方向', width: 70 },
  { dataIndex: 'open_rate', key: 'open_rate', title: '开仓价', align: 'right' as const, width: 100 },
  { dataIndex: 'close_rate', key: 'close_rate', title: '平仓价', align: 'right' as const, width: 100 },
  { key: 'pnl', title: '收益率', align: 'right' as const, width: 90 },
  { key: 'abs', title: '盈亏', align: 'right' as const, width: 90 },
  { dataIndex: 'open_date', key: 'open_date', title: '开仓', width: 110 },
  { key: 'close', title: '平仓', width: 160 },
  { key: 'action', title: '操作', width: 90, fixed: 'right' as const },
];

const balCols = [
  { dataIndex: 'currency', key: 'currency', title: '币种' },
  { dataIndex: 'free', key: 'free', title: '可用', align: 'right' as const },
  { dataIndex: 'used', key: 'used', title: '占用', align: 'right' as const },
  { dataIndex: 'balance', key: 'balance', title: '总计', align: 'right' as const },
  { dataIndex: 'est_stake', key: 'est_stake', title: '折合', align: 'right' as const },
];

/* ══════════ 行级交易操作 ══════════
 * 全部走 Freqtrade api_trading 的实盘控制接口；当前 bot 是 dry-run，操作只影响模拟盘。
 * 统一约定：行级 loading + 成功 message.success 并立即刷新 + 失败 message.error(errText)。
 */

/** 行级 loading：trade_id -> 是否进行中（同时只允许一行有操作在跑） */
const busy = ref<Record<string, boolean>>({});
const rowId = (r: any) => String(r?.trade_id ?? '');
const isBusy = (r: any) => !!busy.value[rowId(r)];

/** 带行级 loading 的操作包装：异常一律转成 message.error，绝不冒泡白屏 */
async function run(r: any, fn: () => Promise<any>, okText: string) {
  const id = rowId(r);
  busy.value = { ...busy.value, [id]: true };
  try {
    await fn();
    message.success(okText);
    await load();
  } catch (e: any) {
    message.error(errText(e));
  } finally {
    const next = { ...busy.value };
    delete next[id];
    busy.value = next;
  }
}

/** 浮动盈亏文案，确认框里要写清楚 */
const pnlText = (r: any) =>
  r
    ? `${pct(r) > 0 ? '+' : ''}${fmt(pct(r))}%（${r.profit_abs === undefined ? '—' : fmt(r.profit_abs)}）`
    : '—';

/* ── 输入类弹窗：限价平仓 / 部分平仓 ── */
const dlg = reactive<{
  mode: 'limit' | 'partial' | null;
  record: any;
  value: any;
}>({ mode: null, record: null, value: null });

const dlgOpen = computed({
  get: () => dlg.mode !== null,
  set: (v: boolean) => {
    if (!v) dlg.mode = null;
  },
});
const dlgTitle = computed(() => {
  const name = dlg.mode === 'limit' ? '限价平仓' : '部分平仓';
  return `${name} · ${dlg.record?.pair ?? ''}`;
});
const dlgBase = computed(
  () => String(dlg.record?.pair ?? '').split('/')[0] || '基础币',
);

function openLimit(r: any) {
  dlg.record = r;
  dlg.value = Number(r?.current_rate ?? r?.open_rate ?? 0);
  dlg.mode = 'limit';
}
function openPartial(r: any) {
  dlg.record = r;
  dlg.value = Number(r?.amount ?? 0);
  dlg.mode = 'partial';
}
async function submitDlg() {
  const r = dlg.record;
  const v = Number(dlg.value);
  if (!r) return;
  if (!Number.isFinite(v) || v <= 0) {
    message.warning('请输入大于 0 的数值');
    return;
  }
  if (dlg.mode === 'partial' && v > Number(r.amount ?? 0)) {
    message.warning(`部分平仓数量不能超过当前持仓 ${fmt(r.amount, 8)} ${dlgBase.value}`);
    return;
  }
  const mode = dlg.mode;
  const text =
    mode === 'limit'
      ? `${r.pair} 已提交限价平仓 @ ${v}`
      : `${r.pair} 已提交部分平仓 ${v} ${dlgBase.value}`;
  const payload =
    mode === 'limit'
      ? { tradeid: r.trade_id, ordertype: 'limit' as const, price: v }
      : { tradeid: r.trade_id, ordertype: 'market' as const, amount: v };
  dlg.mode = null;
  await run(r, () => postForceExit(payload), text);
}

/* ── 确认类操作 ── */
function marketExit(r: any) {
  Modal.confirm({
    title: `市价平仓 · ${r.pair}`,
    content: `将以市价全部平掉该笔持仓。当前浮动盈亏 ${pnlText(r)}（模拟盘）。`,
    okText: '确认市价平仓',
    okButtonProps: { danger: true },
    cancelText: '取消',
    onOk: () =>
      run(
        r,
        () => postForceExit({ tradeid: r.trade_id, ordertype: 'market' }),
        `${r.pair} 已市价平仓`,
      ),
  });
}

function cancelOrder(r: any) {
  Modal.confirm({
    title: `撤销挂单 · ${r.pair}`,
    content: '将撤销该笔交易在交易所的未成交挂单，持仓本身不变。',
    okText: '确认撤销',
    cancelText: '取消',
    onOk: () => run(r, () => cancelOpenOrder(r.trade_id), `${r.pair} 挂单已撤销`),
  });
}

function doReload(r: any) {
  Modal.confirm({
    title: `从交易所重载 · ${r.pair}`,
    content: '将从交易所重新拉取该笔交易的成交与订单状态，并同步覆盖本地记录。',
    okText: '确认重载',
    cancelText: '取消',
    onOk: () => run(r, () => reloadTrade(r.trade_id), `${r.pair} 已从交易所重载`),
  });
}

function removeTrade(r: any) {
  Modal.confirm({
    title: `删除记录 · ${r.pair}`,
    content:
      '只删除本地交易记录，不会平仓。若该笔仍在持仓，盘中统计与收益曲线会随之变化，且记录不可恢复。',
    okText: '确认删除',
    okButtonProps: { danger: true },
    cancelText: '取消',
    onOk: () => run(r, () => deleteTrade(r.trade_id), `${r.pair} 本地记录已删除`),
  });
}

/* ── 查看自定义数据 ── */
const customOpen = ref(false);
const customLoading = ref(false);
const customPair = ref('');
const customText = ref('');

async function viewCustom(r: any) {
  customPair.value = r.pair ?? '';
  customText.value = '';
  customOpen.value = true;
  customLoading.value = true;
  try {
    const data = await getTradeCustomData(r.trade_id);
    // 形状可能是裸数据，也可能是 { custom_data: ... }
    const raw =
      data && typeof data === 'object' && 'custom_data' in data
        ? (data as any).custom_data
        : data;
    const empty =
      raw === null ||
      raw === undefined ||
      raw === '' ||
      (typeof raw === 'object' && Object.keys(raw).length === 0);
    customText.value = empty
      ? '暂无自定义数据（策略未写入 custom_data）'
      : typeof raw === 'string'
        ? raw
        : JSON.stringify(raw, null, 2);
  } catch (e: any) {
    /**
     * 实测：策略没写过 custom_data 时，接口返回的是
     *   404 `{"detail":"No custom-data found for Trade ID: 1."}`
     * 而本项目的 TrendFollowing 策略并不写 custom_data —— 也就是说
     * 这条路径在当前 bot 上是**常态**，不该当成报错弹红条。
     */
    const msg = errText(e);
    if (/no custom-data/i.test(msg)) {
      customText.value = '暂无自定义数据（当前策略未写入 custom_data）';
    } else {
      customOpen.value = false;
      message.error(msg);
    }
  } finally {
    customLoading.value = false;
  }
}

/** Dropdown 菜单统一入口（Menu 的 click 事件只带 { key }） */
const onMenu = (r: any) => (info: any) => onAction(String(info?.key ?? ''), r);

function onAction(key: string, r: any) {
  switch (key) {
    case 'cancel-order': {
      cancelOrder(r);
      break;
    }
    case 'custom': {
      viewCustom(r);
      break;
    }
    case 'delete': {
      removeTrade(r);
      break;
    }
    case 'exit-limit': {
      openLimit(r);
      break;
    }
    case 'exit-market': {
      marketExit(r);
      break;
    }
    case 'exit-partial': {
      openPartial(r);
      break;
    }
    case 'reload': {
      doReload(r);
      break;
    }
    default: {
      break;
    }
  }
}

async function load() {
  loading.value = true;
  try {
    const [o, t, b] = await Promise.all([
      getOpenTrades().catch(() => []),
      getTrades(30).catch(() => ({})),
      getBalance().catch(() => ({})),
    ]);
    open.value = o as any[];
    trades.value = (t as any).trades ?? [];
    total.value = (t as any).total_trades ?? 0;
    bal.value = b;
  } finally {
    loading.value = false;
  }
}

let timer: any = null;
/**
 * WebSocket 推送：成交/撤单回报一到就立刻刷新，不必干等 6 秒轮询。
 * 轮询保留作为兜底 —— WS 断开时行为与改动前完全一致。
 */
const WS_FILL_TYPES = new Set([
  'entry_fill',
  'entry_cancel',
  'exit_fill',
  'exit_cancel',
]);
let offWs: (() => void) | null = null;
const { connected: wsConnected } = useFtWs();

onMounted(() => {
  load();
  timer = setInterval(load, 6000);
  offWs = onFtWsMessage((m) => {
    if (WS_FILL_TYPES.has(m.type)) {
      load();
    }
  });
});
onUnmounted(() => {
  clearInterval(timer);
  offWs?.();
});

</script>

<template>
  <div class="p-4">
    <!-- 顺序保持与改动前一致：先看持仓，再看成交与余额 -->
    <Card :bordered="false" class="shadow-sm" title="当前持仓">
      <template #extra>
        <span class="mr-3 text-xs text-gray-400">
          <span
            class="mr-1 inline-block h-1.5 w-1.5 rounded-full align-middle"
            :class="wsConnected ? 'bg-emerald-500' : 'bg-gray-300'"
          ></span>
          {{ wsConnected ? '实时推送已连接' : '轮询模式' }}
        </span>
        <span class="text-xs text-gray-400">{{ open.length }} 笔</span>
      </template>
      <Table
        :columns="openCols"
        :data-source="open"
        :loading="loading"
        :pagination="false"
        :scroll="{ x: 'max-content' }"
        row-key="trade_id"
        size="small"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'pair'">
            <span class="font-medium">{{ record.pair }}</span>
          </template>
          <template v-else-if="column.key === 'side'">
            <Tag :color="record.is_short ? 'green' : 'red'">
              {{ record.is_short ? '空' : '多' }}
            </Tag>
          </template>
          <template v-else-if="column.key === 'pnl'">
            <span
              :class="pct(record) > 0 ? 'text-red-500' : 'text-emerald-500'"
              class="font-medium"
            >
              {{ pct(record) > 0 ? '+' : '' }}{{ fmt(pct(record)) }}%
            </span>
          </template>
          <template v-else-if="column.key === 'open_date'">
            <span class="text-xs text-gray-400">{{ localTime(record.open_date) }}</span>
          </template>
          <template v-else-if="column.key === 'leverage'">
            {{ record.leverage ? `${fmt(record.leverage, 1)}x` : '—' }}
          </template>
          <template v-else-if="column.key === 'action'">
            <Dropdown :trigger="['click']" placement="bottomRight">
              <Button :loading="isBusy(record)" size="small">操作</Button>
              <template #overlay>
                <Menu @click="onMenu(record)">
                  <MenuItem key="exit-market">市价平仓</MenuItem>
                  <MenuItem key="exit-limit">限价平仓</MenuItem>
                  <MenuItem key="exit-partial">部分平仓</MenuItem>
                  <MenuItem key="cancel-order">撤销挂单</MenuItem>
                  <MenuItem key="reload">从交易所重载</MenuItem>
                  <MenuItem key="custom">查看自定义数据</MenuItem>
                  <MenuItem key="delete" danger>删除记录</MenuItem>
                </Menu>
              </template>
            </Dropdown>
          </template>
          <template v-else>
            {{ fmt(record[column.dataIndex as string], 4) }}
          </template>
        </template>
        <template #emptyText>
          <div class="py-8 text-sm text-gray-400">当前无持仓</div>
        </template>
      </Table>
    </Card>

    <div class="mt-3 grid grid-cols-1 gap-3 xl:grid-cols-5">
      <Card
        :bordered="false"
        class="shadow-sm xl:col-span-3"
        title="最近成交"
      >
      <template #extra>
        <span class="text-xs text-gray-400">
          持仓 {{ open.length }} 笔 · 已平仓 {{ total }} 笔<template v-if="holdStats">
            · 平均持有 {{ holdStats.mean }} 天（中位 {{ holdStats.median }} 天）
          </template>
        </span>
      </template>
        <Table
          :columns="tradeCols"
          :data-source="recentRows"
          :loading="loading"
          :pagination="false"
          :scroll="{ x: 'max-content', y: 420 }"
          row-key="trade_id"
          size="small"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'pair'">
              <span class="font-medium">{{ record.pair }}</span>
            </template>
            <template v-else-if="column.key === 'side'">
              <Tag :color="record.is_short ? 'green' : 'red'">
                {{ record.is_short ? '空' : '多' }}
              </Tag>
            </template>
            <template v-else-if="column.key === 'pnl'">
              <span
                :class="pct(record) > 0 ? 'text-red-500' : 'text-emerald-500'"
              >
                {{ pct(record) > 0 ? '+' : '' }}{{ fmt(pct(record)) }}%
              </span>
            </template>
            <template v-else-if="column.key === 'abs'">
              <span
                :class="
                  record.profit_abs > 0 ? 'text-red-500' : 'text-emerald-500'
                "
              >
                {{ record.profit_abs > 0 ? '+' : '' }}{{ fmt(record.profit_abs) }}
              </span>
            </template>
            <template v-else-if="column.key === 'open_date'">
              <span class="text-xs text-gray-400">{{ localTime(record.open_date) }}</span>
            </template>
            <template v-else-if="column.key === 'close'">
              <Tag v-if="record.is_open" color="warning">持仓中</Tag>
              <span v-else class="text-xs text-gray-400">
                {{ localTime(record.close_date) }}
              </span>
            </template>
            <template v-else-if="column.key === 'action'">
              <!-- 持仓中的行是 /v1/trades 里没有的虚拟行，只能回到「当前持仓」表操作 -->
              <Button
                v-if="!record.is_open"
                :loading="isBusy(record)"
                danger
                size="small"
                type="link"
                @click="removeTrade(record)"
              >
                删除记录
              </Button>
              <span v-else class="text-xs text-gray-400">—</span>
            </template>
            <template v-else>
              {{ fmt(record[column.dataIndex as string], 4) }}
            </template>
          </template>
          <template #emptyText>
            <div class="py-8 text-sm text-gray-400">暂无持仓与成交记录</div>
          </template>
        </Table>
      </Card>

      <Card
        :bordered="false"
        class="shadow-sm xl:col-span-2"
        title="账户余额"
      >
        <template #extra>
          <span class="text-xs text-gray-400">
            合计 {{ fmt(bal.total) }} {{ bal.symbol ?? 'USDT' }}
          </span>
        </template>
        <Table
          :columns="balCols"
          :data-source="
            (bal.currencies ?? []).filter(
              (c: any) => Math.abs(c.balance ?? 0) > 1e-9,
            )
          "
          :loading="loading"
          :pagination="false"
          row-key="currency"
          size="small"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'currency'">
              <span class="font-medium">{{ record.currency }}</span>
            </template>
            <template v-else-if="column.key === 'balance'">
              <span class="font-medium">{{ fmt(record.balance, 6) }}</span>
            </template>
            <template v-else>
              {{ fmt(record[column.dataIndex as string], 6) }}
            </template>
          </template>
          <template #emptyText>
            <div class="py-8 text-sm text-gray-400">无余额数据</div>
          </template>
        </Table>
      </Card>
    </div>

    <!-- 限价平仓 / 部分平仓：同一弹窗按 mode 切换 -->
    <Modal
      v-model:open="dlgOpen"
      :confirm-loading="!!dlg.record && isBusy(dlg.record)"
      :ok-text="dlg.mode === 'limit' ? '确认限价平仓' : '确认部分平仓'"
      :title="dlgTitle"
      cancel-text="取消"
      @ok="submitDlg"
    >
      <div class="text-xs">
        <div class="mb-1 text-gray-500">
          <template v-if="dlg.mode === 'limit'">
            限价单价格（默认当前价，未立即成交会挂在交易所）
          </template>
          <template v-else>
            平仓数量（基础币种 {{ dlgBase }}，当前持仓
            {{ fmt(dlg.record?.amount, 8) }} {{ dlgBase }}，可部分平仓）
          </template>
        </div>
        <InputNumber
          v-model:value="dlg.value"
          :min="0"
          class="w-full"
          placeholder="请输入数值"
        />
        <div class="mt-2 text-gray-400">
          币对 {{ dlg.record?.pair }} · 开仓价
          {{ fmt(dlg.record?.open_rate, 6) }} · 现价
          {{ fmt(dlg.record?.current_rate, 6) }} · 浮动盈亏 {{ pnlText(dlg.record) }}
        </div>
      </div>
    </Modal>

    <!-- 自定义数据：纯只读展示 -->
    <Modal
      v-model:open="customOpen"
      :footer="null"
      :title="`自定义数据 · ${customPair}`"
      width="640px"
    >
      <div v-if="customLoading" class="py-6 text-center text-xs text-gray-400">
        加载中…
      </div>
      <pre
        v-else
        class="max-h-96 overflow-auto rounded bg-gray-50 p-2 text-xs whitespace-pre-wrap dark:bg-gray-900"
      >{{ customText }}</pre>
    </Modal>
  </div>
</template>

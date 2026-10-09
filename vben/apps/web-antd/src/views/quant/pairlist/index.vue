<script lang="ts" setup>
import { sortCoins } from '../utils/coinOrder';
/**
 * 交易对与风控锁
 *
 * 三个板块回答的是同一个问题：机器人「现在」到底在交易哪些币、又不让交易哪些币。
 *  - 白名单：运行期生效的名单（pairlist 过滤后的结果，不是配置文件里的静态副本）
 *  - 黑名单：临时拉黑的交易对，加入后立即生效，重载配置后失效
 *  - Pair Lock：protections 触发的冷却锁，锁住某个交易对/方向，到期前不会再开仓
 *
 * 数据全部来自 api_trading 路由，实盘（当前 dry-run）真实可用。
 * 页面每 30 秒轮询一次，三个接口各自兜底：某个接口挂了不影响另外两块的展示。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue';

import {
  Alert,
  Button,
  Card,
  Input,
  message,
  Modal,
  Space,
  Table,
  Tag,
} from 'ant-design-vue';

import {
  addBlacklist,
  deleteBlacklist,
  deleteLock,
  errText,
  getBlacklist,
  getLocks,
  getWhitelist,
} from '#/api/freqtrade';

const whitelist = ref<string[]>([]);
const blacklist = ref<string[]>([]);
/** 展开后的实际生效名单（带 * 通配的条目会被展开成一个个具体交易对） */
const expanded = ref<string[]>([]);
const blErrors = ref<Record<string, string>>({});
const blMethod = ref<string[]>([]);
const locks = ref<any[]>([]);
const lockCount = ref(0);

const loading = ref(false);
/** 只有首屏才显示 loading；30 秒一次的轮询静默刷新，避免表格反复闪 */
const inited = ref(false);

const newPairs = ref('');
const showExpanded = ref(false);
const adding = ref(false);
const removing = ref(false);
/** 正在解除的那把锁的 id，用于给单个「解除」按钮转圈 */
const unlockingId = ref<any>(null);

const selectedRowKeys = ref<any[]>([]);

/** 表格只显示交易对，用对象包一层以满足 Table 的 dataSource 结构 */
const blackRows = computed(() => blacklist.value.map((p) => ({ pair: p })));
const blacklistErrors = computed(() =>
  sortCoins(Object.entries(blErrors.value), ([pair]) => pair),
);

/** 输入框内容 → 交易对数组：支持逗号（中英文）、分号、空格分隔，顺手去重 */
const parsedPairs = computed(() => {
  const raw = newPairs.value
    .split(/[\s,，;；]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  return sortCoins([...new Set(raw)]);
});

const blackCols = [
  { dataIndex: 'pair', key: 'pair', title: '交易对' },
];

const lockCols = [
  { dataIndex: 'pair', key: 'pair', title: '交易对' },
  { key: 'side', title: '方向', width: 90 },
  { dataIndex: 'lock_time', key: 'lock_time', title: '锁开始', width: 145 },
  { dataIndex: 'lock_end_time', key: 'lock_end_time', title: '锁结束', width: 145 },
  { key: 'remain', title: '剩余', width: 110 },
  { dataIndex: 'reason', key: 'reason', title: '原因' },
  { key: 'action', title: '操作', width: 100 },
];

/** 锁的时间戳是秒；naive 字符串用 new Date 解析失败时原样回显 */
const timeText = (v?: number | string) => {
  if (v === null || v === undefined || v === '') return '—';
  const d = new Date(
    typeof v === 'number' ? v * 1000 : v,
  );
  if (Number.isNaN(d.getTime())) return String(v);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

/** 剩余时间：前端算，避免把「还剩多久」这种事交给后端格式化 */
const remainText = (t: any) => {
  const end = Number(t?.lock_end_timestamp ?? 0);
  if (!end) return '—';
  const ms = end * 1000 - Date.now();
  if (ms <= 0) return '已到期';
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  return h > 0 ? `${h} 小时 ${m} 分` : `${m} 分`;
};

/** 锁的 side 可能是 * / long / short */
const sideText = (s?: string) => {
  const map: Record<string, string> = { '*': '多空', long: '多', short: '空' };
  return map[String(s ?? '*')] ?? String(s ?? '—');
};
const sideColor = (s?: string) => {
  if (s === 'long') return 'red';
  if (s === 'short') return 'green';
  return 'default';
};

function onSelectChange(keys: any) {
  selectedRowKeys.value = Array.isArray(keys) ? keys : [];
}

/** 破坏性操作统一走这里：二次确认 + 按钮 loading + 成功提示 + 立即刷新 */
function confirmDo(
  title: string,
  content: string,
  run: () => Promise<void>,
  okText = '确认',
) {
  Modal.confirm({
    cancelText: '取消',
    content,
    okButtonProps: { danger: true },
    okText,
    onOk: () => run(),
    title,
  });
}

/**
 * 后端 blacklist 的错误值不是字符串，而是 `{ error_msg: "..." }` 对象：
 *   {"errors":{"FAKE/USDT":{"error_msg":"Pair FAKE/USDT is not in the current blacklist."}}}
 * 直接塞进模板会渲染成 [object Object]，这里统一压平成 «交易对 -> 可读文案»。
 */
function normalizeErrors(raw: any): Record<string, string> {
  const out: Record<string, string> = {};
  if (!raw || typeof raw !== 'object') return out;
  for (const [pair, v] of Object.entries(raw as Record<string, any>)) {
    if (typeof v === 'string') {
      out[pair] = v;
    } else if (v && typeof v === 'object') {
      out[pair] = String(v.error_msg ?? v.detail ?? JSON.stringify(v));
    } else {
      out[pair] = String(v);
    }
  }
  return out;
}

async function load() {
  if (!inited.value) loading.value = true;
  try {
    const [w, b, l] = await Promise.all([
      getWhitelist().catch(() => null),
      getBlacklist().catch(() => null),
      getLocks().catch(() => null),
    ]);
    whitelist.value = w?.whitelist ?? [];
    blacklist.value = b?.blacklist ?? [];
    expanded.value = b?.blacklist_expanded ?? [];
    blErrors.value = normalizeErrors(b?.errors);
    blMethod.value = b?.method ?? [];
    locks.value = l?.locks ?? [];
    lockCount.value = l?.lock_count ?? locks.value.length;
    // 名单可能被别处改过，勾选里已经不存在的交易对要丢掉
    selectedRowKeys.value = selectedRowKeys.value.filter((k: any) =>
      blacklist.value.includes(String(k)),
    );
  } finally {
    loading.value = false;
    inited.value = true;
  }
}

function askAdd() {
  const pairs = parsedPairs.value;
  if (!pairs.length) {
    message.warning('请输入至少一个交易对，如 BTC/USDT:USDT');
    return;
  }
  // 已经在黑名单里的直接跳过，不再重复提交
  const dup = pairs.filter((p) => blacklist.value.includes(p));
  const fresh = pairs.filter((p) => !blacklist.value.includes(p));
  if (!fresh.length) {
    message.warning('这些交易对已经在黑名单里了');
    return;
  }
  const tail = dup.length ? `（${dup.join('、')} 已在黑名单中，将跳过）` : '';
  confirmDo(
    '加入黑名单',
    `确认将 ${fresh.join('、')} 加入黑名单？加入后立即生效，直到重载配置。${tail}`,
    async () => {
      adding.value = true;
      try {
        await addBlacklist(fresh);
        message.success(`已加入黑名单：${fresh.join('、')}`);
        newPairs.value = '';
        await load();
      } catch (e: any) {
        message.error(errText(e));
      } finally {
        adding.value = false;
      }
    },
    '加入',
  );
}

function askRemoveSelected() {
  const pairs = sortCoins(selectedRowKeys.value.map((k: any) => String(k)));
  if (!pairs.length) {
    message.warning('请先勾选要移出的交易对');
    return;
  }
  confirmDo(
    '移出黑名单',
    `确认将 ${pairs.length} 个交易对移出黑名单？移出后它们可以立即恢复交易。\n${pairs.join('、')}`,
    async () => {
      removing.value = true;
      try {
        await deleteBlacklist(pairs);
        message.success(`已移出 ${pairs.length} 个交易对`);
        selectedRowKeys.value = [];
        await load();
      } catch (e: any) {
        message.error(errText(e));
      } finally {
        removing.value = false;
      }
    },
    '移出',
  );
}

function askUnlock(lock: any) {
  confirmDo(
    '解除保护锁',
    `确认解除 ${lock.pair} 的保护锁？解除后该交易对可立即重新开仓。`,
    async () => {
      unlockingId.value = lock.id;
      try {
        await deleteLock(lock.id);
        message.success(`已解除 ${lock.pair} 的保护锁`);
        await load();
      } catch (e: any) {
        message.error(errText(e));
      } finally {
        unlockingId.value = null;
      }
    },
    '解除',
  );
}

let timer: any = null;
onMounted(() => {
  load();
  timer = setInterval(load, 30_000);
});
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <div class="p-4">
    <!-- 白名单：机器人实际在找机会的币对 -->
    <Card :bordered="false" class="shadow-sm" title="交易对白名单">
      <template #extra>
        <span class="text-xs text-gray-400">{{ whitelist.length }} 笔</span>
      </template>
      <div class="mb-3 text-xs text-gray-400">
        这是运行期生效的交易对名单 —— 机器人只在这些交易对上寻找入场机会，取自当前实际使用的
        pairlist（已过过滤），不是配置文件里的静态副本。
      </div>
      <div v-if="whitelist.length" class="flex flex-wrap gap-1">
        <Tag v-for="p in sortCoins(whitelist)" :key="p" color="blue">{{ p }}</Tag>
      </div>
      <div v-else class="py-6 text-center text-sm text-gray-400">白名单为空</div>
    </Card>

    <!-- 黑名单：临时拉黑，立即生效 -->
    <Card :bordered="false" class="mt-3 shadow-sm" title="黑名单">
      <template #extra>
        <Space>
          <span class="text-xs text-gray-400">{{ blacklist.length }} 笔</span>
          <Button
            danger
            :disabled="!selectedRowKeys.length"
            :loading="removing"
            size="small"
            @click="askRemoveSelected"
          >
            移出黑名单{{ selectedRowKeys.length ? `（${selectedRowKeys.length}）` : '' }}
          </Button>
        </Space>
      </template>

      <div class="mb-3 text-xs text-gray-400">
        黑名单里的交易对不会被开仓（含已有持仓的加仓）。加入后立即生效，直到重载配置
        （reload_config）或重启机器人。
      </div>

      <Space class="mb-3" wrap>
        <Input
          v-model:value="newPairs"
          :disabled="adding"
          allow-clear
          placeholder="输入交易对，如 BTC/USDT:USDT，可逗号/空格分隔多个"
          style="width: 380px"
          @press-enter="askAdd"
        />
        <Button type="primary" :loading="adding" @click="askAdd">加入黑名单</Button>
      </Space>

      <div v-if="blMethod.length" class="mb-3 flex flex-wrap items-center gap-1">
        <span class="text-xs text-gray-400">黑名单方法：</span>
        <Tag v-for="m in blMethod" :key="m" color="purple">{{ m }}</Tag>
      </div>

      <div v-if="Object.keys(blErrors).length" class="mb-3">
        <Alert
          v-for="[pair, msg] in blacklistErrors"
          :key="pair"
          :message="`${pair}：${msg}`"
          banner
          class="mb-1"
          show-icon
          type="warning"
        />
      </div>

      <Table
        :columns="blackCols"
        :data-source="sortCoins(blackRows, (row) => row.pair)"
        :loading="loading"
        :pagination="false"
        :row-selection="{
          selectedRowKeys: selectedRowKeys,
          onChange: onSelectChange,
        }"
        row-key="pair"
        size="small"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'pair'">
            <span class="font-medium">{{ record.pair }}</span>
          </template>
        </template>
        <template #emptyText>
          <div class="py-8 text-sm text-gray-400">黑名单为空</div>
        </template>
      </Table>

      <div v-if="expanded.length" class="mt-3">
        <Button class="px-0" size="small" type="link" @click="showExpanded = !showExpanded">
          {{ showExpanded ? '收起实际生效名单' : `查看展开后的实际生效名单（${expanded.length}）` }}
        </Button>
        <div v-show="showExpanded" class="mt-2 flex flex-wrap gap-1">
          <Tag v-for="p in sortCoins(expanded)" :key="p">{{ p }}</Tag>
        </div>
      </div>
    </Card>

    <!-- Pair Lock：protections 触发的冷却锁 -->
    <Card :bordered="false" class="mt-3 shadow-sm" title="Pair Lock（保护锁）">
      <template #extra>
        <span class="text-xs text-gray-400">
          {{ lockCount }} 笔 · 生效中 {{ locks.filter((l: any) => l.active).length }}
        </span>
      </template>

      <div class="mb-3 text-xs text-gray-400">
        保护锁由 protections 触发（如连续亏损后冷却）。锁住的交易对在锁结束前不会再开仓，
        解锁后该交易对可立即重新开仓。
      </div>

      <Table
        :columns="lockCols"
        :data-source="sortCoins(locks, (row) => row.pair)"
        :loading="loading"
        :pagination="false"
        :scroll="{ y: 360 }"
        row-key="id"
        size="small"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'pair'">
            <span class="font-medium">{{ record.pair }}</span>
          </template>
          <template v-else-if="column.key === 'side'">
            <Tag :color="sideColor(record.side)">{{ sideText(record.side) }}</Tag>
          </template>
          <template v-else-if="column.key === 'lock_time'">
            <span class="text-xs">{{ timeText(record.lock_timestamp || record.lock_time) }}</span>
          </template>
          <template v-else-if="column.key === 'lock_end_time'">
            <span class="text-xs">
              {{ timeText(record.lock_end_timestamp || record.lock_end_time) }}
            </span>
          </template>
          <template v-else-if="column.key === 'remain'">
            <span v-if="record.active" class="text-xs text-amber-500">
              {{ remainText(record) }}
            </span>
            <Tag v-else>已失效</Tag>
          </template>
          <template v-else-if="column.key === 'reason'">
            <span class="text-xs text-gray-500">{{ record.reason || '—' }}</span>
          </template>
          <template v-else-if="column.key === 'action'">
            <Button
              v-if="record.active"
              danger
              :loading="unlockingId === record.id"
              size="small"
              type="link"
              @click="askUnlock(record)"
            >
              解除
            </Button>
            <span v-else class="text-xs text-gray-400">无需操作</span>
          </template>
        </template>
        <template #emptyText>
          <div class="py-8 text-sm text-gray-400">当前无保护锁</div>
        </template>
      </Table>
    </Card>
  </div>
</template>

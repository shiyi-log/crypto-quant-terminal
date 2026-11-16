<script setup lang="ts">
import type { PositionsState, Trade } from './positionsState';
import { computed, onMounted, onUnmounted, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { Alert, Button, Input, Modal } from 'ant-design-vue';
import { shortOrderTime, tradeDisplayId } from '../utils/orderDisplayId';
import TradeDetailDrawer from './TradeDetailDrawer.vue';
import { formatNumber, profitPercent, signedNumber } from './positionsState';
import { onFtWsMessage, useFtWs } from '../utils/useFtWs';

const props = defineProps<{ state: PositionsState }>();
const state = props.state;
const route = useRoute();
const router = useRouter();
const { connected } = useFtWs();
const rows = computed(() => state.data.positions ?? []);
const closed = computed(() => state.data.history ?? []);
const activeRows = computed(() =>
  state.data.view === 'closed' ? closed.value : rows.value,
);
const selected = computed(() => state.selectedSnapshot());
const currency = computed(
  () => state.data.balance?.stake ?? state.data.balance?.symbol ?? 'USDT',
);
const environment = computed(() =>
  state.data.errors.config
    ? '环境读取失败；上次状态：' +
      (state.data.environment === 'dry'
        ? '模拟 dry-run'
        : state.data.environment === 'live'
          ? '实盘模式'
          : '未知')
    : state.data.environment === 'dry'
      ? '模拟 dry-run'
      : state.data.environment === 'live'
        ? '实盘模式'
        : '环境未知',
);
const number = (value: unknown) =>
  typeof value === 'number'
    ? Number.isFinite(value)
      ? value
      : null
    : typeof value === 'string' &&
        /^[+-]?[0-9]+(?:[.][0-9]+)?$/.test(value.trim()) &&
        Number.isFinite(Number(value))
      ? Number(value)
      : null;
const pairs = computed(() =>
  [...new Set([...rows.value, ...closed.value].map((row) => row.pair))].sort(),
);
const filteredRows = computed(() =>
  activeRows.value.filter(
    (row) =>
      (!state.data.pair || row.pair === state.data.pair) &&
      (!state.data.query ||
        (row.pair + ' ' + row.trade_id + ' ' + tradeDisplayId(row))
          .toLowerCase()
          .includes(state.data.query.toLowerCase())),
  ),
);
function pct(row: Trade) {
  const value = profitPercent(row);
  return value === null ? '—' : signedNumber(value) + '%';
}
function quantity(row: Trade) {
  const value = number(row.amount);
  return value === null ? '—' : formatNumber(value, 8);
}
function idText(value: unknown) {
  return typeof value === 'string' || typeof value === 'number'
    ? String(value)
    : '—';
}
function selectRow(row: Trade) {
  void router.replace({
    query: { ...route.query, trade_id: String(row.trade_id) },
  });
}
function syncSelection() {
  const requested = route.query.trade_id;
  if (requested === undefined) {
    void state.select(null);
    return;
  }
  void state.select(requested);
}
watch(() => route.query.trade_id, syncSelection, { immediate: true });
function selectView(view: 'open' | 'closed') {
  state.data.view = view;
  state.data.pair = '';
}
function title(command: string) {
  return (
    (
      {
        market: '市价退出这笔持仓',
        limit: '提交限价退出',
        partial: '提交部分退出',
        cancel: '撤销这笔挂单',
        reload: '从交易所重载',
      } as Record<string, string>
    )[command] ?? '发送交易指令'
  );
}
function request(
  command: 'market' | 'limit' | 'partial' | 'cancel' | 'reload',
) {
  const trade = selected.value;
  if (!trade || !state.requestCommand(command, trade.trade_id)) return;
  if (command === 'limit' || command === 'partial')
    state.data.commandFormOpen = true;
  else state.openConfirmation();
}
function beginConfirmation() {
  state.data.commandFormOpen = false;
  state.openConfirmation();
}
let timer: ReturnType<typeof setInterval> | undefined;
let stopWs: (() => void) | null = null;
onMounted(() => {
  void state.refresh();
  timer = setInterval(() => void state.refresh(), 6000);
  stopWs = onFtWsMessage((event) => {
    if (
      ['entry_fill', 'entry_cancel', 'exit_fill', 'exit_cancel'].includes(
        event.type,
      )
    )
      void state.refresh();
  });
});
watch(
  () => connected.value,
  (value) => {
    state.data.connected = value;
  },
  { immediate: true },
);
onUnmounted(() => {
  if (timer) clearInterval(timer);
  stopWs?.();
  state.dispose();
});
</script>

<template>
  <main class="positions-page">
    <header class="environment-bar">
      <div>
        <p class="engine-label">旧 Freqtrade 交易引擎</p>
        <h1>持仓与成交</h1>
      </div>
      <div class="engine-facts">
        <span class="environment" :class="state.data.environment">{{
          environment
        }}</span
        ><span>{{
          state.data.connected ? '实时成交推送已连接' : '轮询刷新模式'
        }}</span
        ><span
          >账户余额：{{
            state.data.balance
              ? formatNumber(state.data.balance.total) + ' ' + currency
              : '未知'
          }}</span
        ><span>策略：{{ state.data.config?.strategy ?? '未知' }}</span
        ><span>采样：{{ state.data.readAt.positions ?? '未读取' }}</span>
      </div>
      <Button :loading="state.data.loading" @click="state.refresh"
        >刷新事实</Button
      >
    </header>
    <Alert
      v-if="Object.keys(state.data.errors).length"
      class="read-warning"
      type="warning"
      show-icon
      message="部分旧引擎数据刷新失败；保留最近成功快照。"
      :description="Object.values(state.data.errors).join(' ')"
    ></Alert>
    <section class="ledger-layout">
      <div class="ledger-column">
        <div class="ledger-heading">
          <div>
            <h2>交易账本</h2>
            <p>旧引擎账户 · 不与 Binance Demo 合并</p>
          </div>
          <span
            >{{
              state.data.view === 'closed'
                ? (state.data.total ?? '—')
                : (state.data.positions?.length ?? '—')
            }}
            笔</span
          >
        </div>
        <div class="toolbar">
          <div class="view-switch" role="tablist" aria-label="交易账本范围">
            <button
              role="tab"
              :aria-selected="state.data.view === 'open'"
              @click="selectView('open')"
            >
              当前持仓</button
            ><button
              role="tab"
              :aria-selected="state.data.view === 'closed'"
              @click="selectView('closed')"
            >
              最近已平仓
            </button>
          </div>
          <select v-model="state.data.pair" aria-label="按币对筛选">
            <option value="">全部币对</option>
            <option v-for="pair in pairs" :key="pair" :value="pair">
              {{ pair }}
            </option></select
          ><Input
            v-model:value="state.data.query"
            aria-label="搜索交易编号或币对"
            placeholder="搜索币对或交易 ID"
            allow-clear
          />
        </div>
        <p v-if="!activeRows.length" class="ledger-state">
          {{
            state.data.view === 'closed'
              ? state.data.history === null
                ? '已平仓交易尚未读取。'
                : '最近记录中没有已平仓交易。'
              : state.data.positions === null
                ? '当前持仓尚未读取。'
                : '当前没有开放持仓。'
          }}
        </p>
        <div
          v-else
          class="trade-list"
          role="listbox"
          aria-label="旧引擎交易账本"
        >
          <button
            v-for="row in filteredRows"
            :key="row.trade_id"
            :data-trade="row.trade_id"
            class="trade-row"
            :class="{
              selected: state.data.selectedId === String(row.trade_id),
            }"
            role="option"
            :aria-selected="state.data.selectedId === String(row.trade_id)"
            @click="selectRow(row)"
          >
            <span class="trade-top"
              ><b>{{ row.pair }}</b
              ><span
                class="direction"
                :class="
                  row.is_short === undefined
                    ? 'unknown'
                    : row.is_short
                      ? 'short'
                      : 'long'
                "
                >{{
                  row.is_short === undefined
                    ? '方向未知'
                    : row.is_short
                      ? '空'
                      : '多'
                }}</span
              ><span>{{ row.is_open ? '持仓中' : '已平仓' }}</span></span
            ><span class="trade-id"
              >{{ tradeDisplayId(row) }} · ID {{ idText(row.trade_id) }}</span
            ><span class="trade-bottom"
              ><span
                >开仓
                {{ shortOrderTime(row.open_timestamp, row.open_date) }}</span
              ><span>数量 {{ quantity(row) }}</span
              ><strong
                :class="
                  number(row.profit_abs) === null
                    ? 'unknown'
                    : number(row.profit_abs)! > 0
                      ? 'positive'
                      : number(row.profit_abs)! < 0
                        ? 'negative'
                        : ''
                "
                >{{ signedNumber(row.profit_abs, 4) }} {{ currency }} ({{
                  pct(row)
                }})</strong
              ></span
            >
          </button>
          <p v-if="filteredRows.length === 0" class="ledger-state">
            当前筛选下没有交易。
          </p>
        </div>
        <p class="ledger-footnote">
          接口返回最近 {{ closed.length }} 笔 / 共
          {{ state.data.total ?? '未知' }}
          笔已平仓交易。列表编号仅用于展示；原始交易 ID 和订单 ID 保留。
        </p>
      </div>
      <aside class="inspector" aria-label="所选交易检查区">
        <template v-if="selected"
          ><div class="inspect-title">
            <div>
              <p>所选交易</p>
              <h2>{{ selected.pair }}</h2>
            </div>
            <span>{{
              selected.is_short === undefined
                ? '方向未知'
                : selected.is_short
                  ? '做空'
                  : '做多'
            }}</span>
          </div>
          <p class="inspect-id">
            原始交易 ID <code>{{ idText(selected.trade_id) }}</code>
          </p>
          <p class="inspect-order">
            开仓编号 <code>{{ tradeDisplayId(selected) }}</code>
          </p>
          <p v-if="state.data.detailError" class="inline-state error">
            {{ state.data.detailError }}
          </p>
          <p v-else-if="state.data.detailLoading" class="inline-state">
            正在读取所选交易详情…
          </p>
          <Button
            v-if="state.data.detailError"
            size="small"
            @click="state.select(state.data.selectedId)"
            >重试精确交易</Button
          >
          <div v-if="state.data.detail" class="facts-grid">
            <div>
              <span>状态</span
              ><b>{{
                state.data.detail.is_open === true
                  ? '持仓中'
                  : state.data.detail.is_open === false
                    ? '已平仓'
                    : '未知'
              }}</b>
            </div>
            <div>
              <span>开仓价</span
              ><b>{{ formatNumber(state.data.detail.open_rate, 8) }}</b>
            </div>
            <div>
              <span>现价 / 平仓</span
              ><b>{{
                formatNumber(
                  state.data.detail.is_open
                    ? state.data.detail.current_rate
                    : state.data.detail.close_rate,
                  8,
                )
              }}</b>
            </div>
            <div>
              <span>投入</span
              ><b
                >{{ formatNumber(state.data.detail.stake_amount) }}
                {{ currency }}</b
              >
            </div>
            <div>
              <span>持仓数量</span><b>{{ quantity(state.data.detail) }}</b>
            </div>
            <div>
              <span>杠杆</span
              ><b>{{ formatNumber(state.data.detail.leverage, 2) }}×</b>
            </div>
            <div>
              <span>手续费</span
              ><b
                >{{ formatNumber(state.data.detail.fee_open_cost) }} /
                {{ formatNumber(state.data.detail.fee_close_cost) }}</b
              >
            </div>
            <div>
              <span>退出原因</span
              ><b>{{ state.data.detail.exit_reason ?? '—' }}</b>
            </div>
          </div>
          <Button
            v-if="state.data.detail"
            size="small"
            @click="state.openDetail"
            >打开完整订单详情</Button
          >
          <p v-if="state.data.detail" class="source-caption">
            详情与订单来自精确的 /v1/trade/{{
              idText(selected.trade_id)
            }}。接口未提供的值保持未知；订单列表不代表完整撤单历史。
          </p>
          <details v-if="state.data.detail" class="orders">
            <summary>
              原始订单（{{
                Array.isArray(state.data.detail.orders)
                  ? state.data.detail.orders.length
                  : '未知'
              }}）
            </summary>
            <div v-if="state.data.detail.orders?.length" class="order-list">
              <article
                v-for="(order, index) in state.data.detail.orders"
                :key="order.order_id ?? index"
              >
                <b
                  >{{ order.ft_order_side ?? order.side ?? '用途未知' }} ·
                  {{ order.order_type ?? order.type ?? '类型未知' }}</b
                ><code>交易所订单号 {{ idText(order.order_id) }}</code
                ><span
                  >状态 {{ order.status ?? '未知' }} · 已成交
                  {{ formatNumber(order.filled, 8) }} /
                  {{ formatNumber(order.amount, 8) }}</span
                >
              </article>
            </div>
            <p v-else>接口当前没有返回订单。</p>
          </details>
          <section v-if="selected.is_open" class="command-panel">
            <h3>旧引擎指令</h3>
            <p>环境：{{ environment }}。提交请求不代表执行或成交。</p>
            <div class="command-buttons">
              <Button
                data-command="market"
                danger
                :disabled="
                  state.data.environment === 'unknown' ||
                  !!state.data.errors.config ||
                  !!state.data.errors.positions
                "
                @click="request('market')"
                >市价退出</Button
              ><Button
                data-command="limit"
                :disabled="
                  state.data.environment === 'unknown' ||
                  !!state.data.errors.config ||
                  !!state.data.errors.positions
                "
                @click="request('limit')"
                >限价退出</Button
              ><Button
                data-command="partial"
                :disabled="
                  state.data.environment === 'unknown' ||
                  !!state.data.errors.config ||
                  !!state.data.errors.positions
                "
                @click="request('partial')"
                >部分退出</Button
              ><Button
                data-command="cancel"
                :disabled="
                  state.data.environment === 'unknown' ||
                  !!state.data.errors.config ||
                  !!state.data.errors.positions
                "
                @click="request('cancel')"
                >撤销挂单</Button
              ><Button
                data-command="reload"
                :disabled="
                  state.data.environment === 'unknown' ||
                  !!state.data.errors.config ||
                  !!state.data.errors.positions
                "
                @click="request('reload')"
                >从交易所重载</Button
              >
            </div>
            <div
              v-if="state.data.commandFormOpen && state.data.command"
              class="command-form"
            >
              <label
                >{{
                  state.data.command.kind === 'limit'
                    ? '限价（USDT）'
                    : '部分退出数量（基础币）'
                }}<Input
                  v-model:value="state.data.commandValue"
                  inputmode="decimal"
              /></label>
              <p>
                {{
                  state.data.command.kind === 'limit'
                    ? '限价单可能不会立即成交。'
                    : '当前数量 ' + quantity(state.data.command.trade)
                }}
              </p>
              <Button
                :disabled="state.data.busy"
                type="primary"
                @click="beginConfirmation"
                >核对指令</Button
              ><Button :disabled="state.data.busy" @click="state.cancelCommand"
                >取消</Button
              >
              <p v-if="state.data.commandError" class="inline-state error">
                {{ state.data.commandError }}
              </p>
            </div>
            <p
              v-if="state.data.receipt"
              class="inline-state receipt"
              role="status"
            >
              {{ state.data.receipt }}
            </p>
          </section>
          <section v-else class="command-panel">
            <h3>只读历史</h3>
            <p>
              删除记录仅移除本地记录，不会平仓，并会破坏历史审计。删除已退出工作流；请保留原始记录。
            </p>
          </section>
          <section class="custom-data">
            <div>
              <h3>策略自定义数据</h3>
              <Button
                size="small"
                :loading="state.data.customLoading"
                @click="state.loadCustom"
                >读取</Button
              >
            </div>
            <p v-if="state.data.customError" class="inline-state error">
              {{ state.data.customError }}
            </p>
            <pre v-else-if="state.data.custom">{{ state.data.custom }}</pre>
            <p v-else>尚未读取。未写入自定义数据不会改变交易记录。</p>
          </section>
        </template>
        <div v-else class="empty-inspector">
          <h2>交易检查区</h2>
          <p>
            {{
              route.query.trade_id !== undefined
                ? '请求 ID 无效或在当前账本缺失。不会自动跳到其他交易。'
                : '选择一笔交易，查看精确 ID、订单记录和指令。'
            }}
          </p>
          <p v-if="route.query.trade_id !== undefined">
            请求 ID：{{
              Array.isArray(route.query.trade_id)
                ? '重复参数无效'
                : route.query.trade_id
            }}
          </p>
        </div>
      </aside>
    </section>
    <Modal
      v-model:open="state.data.confirmationOpen"
      :confirm-loading="state.data.busy"
      :ok-text="
        state.data.command ? title(state.data.command.kind) : '确认发送'
      "
      cancel-text="取消"
      title="确认旧引擎交易指令"
      :ok-button-props="{
        danger:
          state.data.command?.kind === 'market' ||
          state.data.command?.kind === 'partial',
      }"
      @ok="state.confirmCommand"
      @cancel="state.cancelCommand"
      ><p v-if="state.data.command">
        交易 {{ state.data.command.trade.pair }} · 原始交易 ID
        {{ idText(state.data.command.trade.trade_id) }} · 环境
        {{
          state.data.command.environment === 'dry' ? '模拟 dry-run' : '实盘模式'
        }}
      </p>
      <p v-if="state.data.command?.kind === 'limit'">
        限价 {{ state.data.commandValue }} USDT
      </p>
      <p v-if="state.data.command?.kind === 'partial'">
        部分退出数量 {{ state.data.commandValue }} 基础币
      </p>
      <p>
        该确认只发送一次请求。回执不代表交易所已执行、已撤单或已经成交；如结果未知，请先核对交易详情，不要盲目重发。
      </p>
      <p v-if="state.data.commandError" class="inline-state error">
        {{ state.data.commandError }}
      </p></Modal
    >
    <details class="balance-ledger">
      <summary>
        余额明细 ·
        {{ state.data.balance?.currencies?.length ?? '未读取' }} 个币种
      </summary>
      <div v-if="state.data.balance" class="balance-table">
        <div v-for="coin in state.data.balance.currencies" :key="coin.currency">
          <b>{{ coin.currency }}</b
          ><span>可用 {{ formatNumber(coin.free) }}</span
          ><span>占用 {{ formatNumber(coin.used) }}</span
          ><span>余额 {{ formatNumber(coin.balance) }}</span>
        </div>
      </div>
      <p v-else>余额接口尚未提供数据；不会与持仓盈亏重复加总。</p>
    </details>
    <p class="retirement-note">
      删除交易记录不等同于平仓。此页面不发送 DELETE 请求，并保留原始历史供检查。
    </p>
    <TradeDetailDrawer
      :open="state.data.detailOpen"
      :record="selected as any"
      @update:open="
        (open) => {
          if (!open) state.closeDetail();
        }
      "
    />
  </main>
</template>

<style scoped>
.positions-page {
  min-width: 0;
  min-height: 100%;
  padding: 18px;
  background: #0c1b2a;
  color: #e5edf4;
  font-family: Inter, 'PingFang SC', 'Noto Sans CJK SC', sans-serif;
}
.environment-bar {
  display: grid;
  grid-template-columns: minmax(180px, 1fr) minmax(0, 2fr) auto;
  align-items: center;
  gap: 16px;
  padding: 14px 16px;
  border: 1px solid #29485c;
  background: #132b3e;
}
.environment-bar h1,
.ledger-heading h2,
.inspect-title h2,
.empty-inspector h2 {
  margin: 0;
  font-size: 21px;
  font-weight: 650;
}
.engine-label,
.inspect-title p {
  margin: 0 0 4px;
  color: #a2b8c9;
  font-size: 12px;
}
.engine-facts {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 18px;
  color: #a2b8c9;
  font-size: 12px;
}
.environment {
  padding: 5px 9px;
  border: 1px solid #f0c17b;
  color: #f0c17b;
}
.environment.dry {
  border-color: #69b4e4;
  color: #69b4e4;
}
.environment.live {
  border-color: #f19a9a;
  color: #f19a9a;
}
.read-warning {
  margin: 12px 0;
}
.ledger-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.65fr) minmax(320px, 0.85fr);
  gap: 16px;
  margin-top: 16px;
}
.ledger-column,
.inspector,
.balance-ledger {
  min-width: 0;
  border: 1px solid #29485c;
  background: #132b3e;
}
.ledger-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px;
  border-bottom: 1px solid #29485c;
}
.ledger-heading h2 {
  font-size: 17px;
}
.ledger-heading p,
.source-caption {
  margin: 4px 0 0;
  color: #a2b8c9;
  font-size: 12px;
}
.toolbar {
  display: grid;
  grid-template-columns: auto minmax(120px, 180px) minmax(130px, 1fr);
  gap: 8px;
  padding: 10px 12px;
  border-bottom: 1px solid #29485c;
}
.view-switch {
  display: flex;
  border: 1px solid #29485c;
}
.view-switch button {
  padding: 7px 10px;
  border: 0;
  background: transparent;
  color: #a2b8c9;
  cursor: pointer;
}
.view-switch button[aria-selected='true'] {
  background: #1d4059;
  color: #e5edf4;
}
.toolbar select,
.toolbar :deep(input) {
  min-width: 0;
  height: 34px;
  border: 1px solid #29485c;
  background: #0c1b2a;
  color: #e5edf4;
  padding: 0 9px;
}
.trade-list {
  max-height: min(68vh, 850px);
  overflow: auto;
}
.trade-row {
  display: block;
  width: 100%;
  padding: 12px 14px;
  border: 0;
  border-bottom: 1px solid #29485c;
  background: transparent;
  color: #e5edf4;
  text-align: left;
  cursor: pointer;
}
.trade-row:hover,
.trade-row.selected {
  background: #19364d;
}
.trade-row.selected {
  box-shadow: inset 3px 0 #69b4e4;
}
.trade-top,
.trade-bottom {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
}
.trade-top b {
  font-size: 14px;
}
.trade-top > span:not(:first-child) {
  color: #a2b8c9;
  font-size: 11px;
}
.direction {
  flex: none;
  color: #a2b8c9;
}
.trade-id {
  display: block;
  max-width: 100%;
  overflow: hidden;
  margin: 5px 0;
  color: #a2b8c9;
  text-overflow: ellipsis;
  white-space: nowrap;
  font:
    11px ui-monospace,
    monospace;
}
.trade-bottom {
  justify-content: space-between;
  flex-wrap: wrap;
  color: #a2b8c9;
  font-size: 11px;
}
.trade-bottom strong {
  color: #e5edf4;
  font:
    600 12px ui-monospace,
    monospace;
}
.trade-bottom strong.positive {
  color: #7acbaa;
}
.trade-bottom strong.negative {
  color: #f19a9a;
}
.ledger-state,
.ledger-footnote {
  padding: 17px 14px;
  color: #a2b8c9;
  font-size: 12px;
}
.ledger-footnote {
  margin: 0;
  border-top: 1px solid #29485c;
}
.inspector {
  padding: 16px;
}
.inspect-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.inspect-title h2 {
  overflow-wrap: anywhere;
  font-size: 18px;
}
.inspect-id,
.inspect-order {
  margin: 12px 0 0;
  color: #a2b8c9;
  font-size: 12px;
}
.inspect-id code,
.inspect-order code,
.order-list code {
  overflow-wrap: anywhere;
  color: #e5edf4;
  font:
    12px ui-monospace,
    monospace;
}
.inspect-order {
  margin-top: 5px;
}
.facts-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0;
  margin-top: 14px;
  border-top: 1px solid #29485c;
}
.facts-grid > div {
  min-width: 0;
  padding: 9px 5px;
  border-bottom: 1px solid #29485c;
}
.facts-grid span,
.facts-grid b {
  display: block;
  overflow-wrap: anywhere;
}
.facts-grid span {
  margin-bottom: 3px;
  color: #a2b8c9;
  font-size: 11px;
}
.facts-grid b {
  font-size: 12px;
  font-weight: 600;
}
.source-caption {
  margin: 10px 0;
}
.orders,
.balance-ledger {
  padding: 11px 12px;
}
.orders summary,
.balance-ledger summary {
  cursor: pointer;
  color: #69b4e4;
  font-size: 12px;
}
.order-list {
  max-height: 250px;
  overflow: auto;
}
.order-list article {
  display: grid;
  gap: 5px;
  padding: 9px 0;
  border-bottom: 1px solid #29485c;
  font-size: 11px;
}
.order-list article span {
  color: #a2b8c9;
}
.command-panel,
.custom-data {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid #29485c;
}
.command-panel h3,
.custom-data h3 {
  margin: 0;
  font-size: 13px;
}
.command-panel > p,
.custom-data > p,
.balance-ledger > p {
  color: #a2b8c9;
  font-size: 11px;
  line-height: 1.55;
}
.command-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}
.command-form {
  display: grid;
  gap: 8px;
  margin-top: 10px;
  padding: 10px;
  border: 1px solid #29485c;
}
.command-form label {
  display: grid;
  gap: 6px;
  color: #a2b8c9;
  font-size: 11px;
}
.command-form p {
  margin: 0;
  color: #a2b8c9;
  font-size: 11px;
}
.inline-state {
  color: #a2b8c9;
  font-size: 12px;
}
.inline-state.error {
  color: #f0c17b;
}
.inline-state.receipt {
  padding: 8px;
  border-left: 2px solid #f0c17b;
  background: #102539;
}
.custom-data > div {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.custom-data pre {
  max-height: 220px;
  overflow: auto;
  padding: 9px;
  background: #0c1b2a;
  color: #e5edf4;
  font:
    11px/1.5 ui-monospace,
    monospace;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.empty-inspector {
  padding: 16px;
}
.empty-inspector h2 {
  font-size: 15px;
}
.empty-inspector p {
  color: #a2b8c9;
  font-size: 12px;
  line-height: 1.55;
  overflow-wrap: anywhere;
}
.balance-ledger {
  margin-top: 14px;
}
.balance-table {
  max-height: 240px;
  overflow: auto;
}
.balance-table > div {
  display: grid;
  grid-template-columns: minmax(80px, 1fr) repeat(3, minmax(90px, 1fr));
  gap: 10px;
  padding: 7px;
  border-bottom: 1px solid #29485c;
  font-size: 11px;
}
.balance-table > div span {
  text-align: right;
  color: #a2b8c9;
}
.retirement-note {
  margin: 12px 0;
  color: #a2b8c9;
  font-size: 11px;
  line-height: 1.5;
}
.trade-row:focus-visible,
.view-switch button:focus-visible,
.orders summary:focus-visible,
.balance-ledger summary:focus-visible {
  outline: 2px solid #69b4e4;
  outline-offset: -2px;
}
@media (max-width: 1000px) {
  .ledger-layout {
    grid-template-columns: minmax(0, 1.1fr) minmax(300px, 0.9fr);
  }
  .environment-bar {
    grid-template-columns: 1fr auto;
  }
  .engine-facts {
    grid-column: 1/-1;
    grid-row: 2;
  }
}
@media (max-width: 720px) {
  .positions-page {
    padding: 10px;
  }
  .environment-bar {
    grid-template-columns: minmax(0, 1fr) auto;
    padding: 11px;
    gap: 10px;
  }
  .environment-bar h1 {
    font-size: 18px;
  }
  .engine-facts {
    grid-column: 1/-1;
    grid-row: auto;
    gap: 7px 11px;
  }
  .ledger-layout {
    grid-template-columns: minmax(0, 1fr);
    gap: 10px;
    margin-top: 10px;
  }
  .trade-list {
    max-height: 52vh;
  }
  .toolbar {
    grid-template-columns: minmax(0, 1fr) minmax(120px, 0.8fr);
  }
  .view-switch {
    grid-column: 1/-1;
  }
  .view-switch button {
    flex: 1;
  }
  .toolbar :deep(.ant-input-affix-wrapper) {
    grid-column: 1/-1;
  }
  .inspector {
    padding: 12px;
  }
  .command-buttons > * {
    flex: 1 1 110px;
  }
  .balance-table > div {
    grid-template-columns: minmax(60px, 1fr) repeat(3, minmax(72px, 1fr));
    gap: 5px;
    font-size: 10px;
  }
}
@media (max-width: 420px) {
  .environment-bar {
    align-items: start;
  }
  .engine-facts {
    font-size: 11px;
  }
  .ledger-heading {
    padding: 12px;
  }
  .trade-bottom {
    gap: 4px 8px;
  }
  .trade-bottom strong {
    width: 100%;
  }
  .balance-table > div {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .balance-table > div span {
    text-align: left;
  }
}
@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    scroll-behavior: auto !important;
    transition: none !important;
  }
}
</style>

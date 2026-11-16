<script lang="ts" setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { getConfig, getOpenTrades, getWhitelist } from '#/api/freqtrade';

import { compareCoins } from '../utils/coinOrder';
import PairChart from './PairChart.vue';

const route = useRoute();
const router = useRouter();
const periods = [
  { label: '5 分', value: '5m' },
  { label: '15 分', value: '15m' },
  { label: '30 分', value: '30m' },
  { label: '1 小时', value: '1h' },
  { label: '4 小时', value: '4h' },
  { label: '1 日', value: '1d' },
  { label: '1 周', value: '1w' },
];
const whitelist = ref<string[] | null>(null);
const positionPairs = ref<string[] | null>(null);
const config = ref<Record<string, unknown> | null>(null);
const sourceErrors = ref<string[]>([]);
const staleWhitelist = ref(false);
const stalePositions = ref(false);
const staleConfig = ref(false);
const lastRead = ref('');
const loading = ref(false);
const search = ref('');
let disposed = false;
let timer: ReturnType<typeof setInterval> | undefined;
const isPair = (value: unknown): value is string =>
  typeof value === 'string' &&
  /^[A-Za-z0-9_.-]+[/][A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?$/.test(value) &&
  value.length <= 120;
const pairs = computed(() =>
  [
    ...new Set([...(whitelist.value ?? []), ...(positionPairs.value ?? [])]),
  ].sort(compareCoins),
);
const visiblePairs = computed(() =>
  pairs.value.filter((pair) =>
    pair.toLowerCase().includes(search.value.trim().toLowerCase()),
  ),
);
const defaultTimeframe = computed(() =>
  periods.some((period) => period.value === config.value?.timeframe)
    ? String(config.value?.timeframe)
    : '1h',
);
const invalidQuery = computed(() => {
  const pair = route.query.pair;
  const timeframe = route.query.timeframe;
  return (
    (pair !== undefined && !isPair(pair)) ||
    (timeframe !== undefined &&
      (typeof timeframe !== 'string' ||
        !periods.some((period) => period.value === timeframe)))
  );
});
const selectedPair = computed(() =>
  route.query.pair === undefined
    ? (pairs.value[0] ?? '')
    : typeof route.query.pair === 'string'
      ? route.query.pair
      : '无效交易对参数',
);
const timeframe = computed(() =>
  typeof route.query.timeframe === 'string'
    ? route.query.timeframe
    : defaultTimeframe.value,
);
const knownSelection = computed(
  () => !invalidQuery.value && pairs.value.includes(selectedPair.value),
);
const selectionError = computed(() =>
  invalidQuery.value
    ? '查询参数无效。交易对与周期必须各为一个有效值，请重新选择目录中的交易对与周期。'
    : selectedPair.value && !knownSelection.value && !loading.value
      ? '指定交易对不在最近成功的目录中。保留原始目标，请刷新目录或选择其他交易对。'
      : '',
);
const environment = computed(() =>
  config.value?.dry_run === true
    ? '模拟 dry-run'
    : config.value?.dry_run === false
      ? '实盘'
      : '环境未知',
);
const strategy = computed(() =>
  typeof config.value?.strategy === 'string' && config.value.strategy
    ? config.value.strategy
    : '策略未知',
);
const stateLabel = computed(
  () =>
    ({ running: '运行中', stopped: '已停止', paused: '已暂停开仓' })[
      String(config.value?.state) as 'running' | 'stopped' | 'paused'
    ] ?? '状态未知',
);

function selectPair(pair: string) {
  void router.push({
    query: {
      ...route.query,
      pair,
      timeframe: invalidQuery.value ? defaultTimeframe.value : timeframe.value,
    },
  });
}
function selectTimeframe(event: Event) {
  const value = (event.target as HTMLSelectElement).value;
  void router.push({
    query: {
      ...route.query,
      pair:
        route.query.pair === undefined
          ? selectedPair.value || undefined
          : route.query.pair,
      timeframe: value,
    },
  });
}
function pairStatus(pair: string) {
  const labels: string[] = [];
  if (whitelist.value?.includes(pair))
    labels.push(staleWhitelist.value ? '白名单旧快照' : '白名单');
  if (positionPairs.value?.includes(pair))
    labels.push(stalePositions.value ? '持仓旧快照' : '有持仓');
  return labels.join(' / ');
}
async function loadUniverse() {
  if (loading.value || disposed) return;
  loading.value = true;
  const results = await Promise.allSettled([
    getWhitelist(),
    getOpenTrades(),
    getConfig(),
  ]);
  if (disposed) return;
  const failures: string[] = [];
  const [list, trades, cfg] = results;
  if (
    list.status === 'fulfilled' &&
    list.value &&
    Array.isArray(list.value.whitelist) &&
    list.value.whitelist.every(isPair)
  ) {
    whitelist.value = [...new Set(list.value.whitelist)];
    staleWhitelist.value = false;
  } else {
    staleWhitelist.value = true;
    failures.push('白名单目录读取失败，保留最近成功的白名单。');
  }
  if (
    trades.status === 'fulfilled' &&
    Array.isArray(trades.value) &&
    trades.value.every(
      (trade) => trade && typeof trade === 'object' && isPair(trade.pair),
    )
  ) {
    positionPairs.value = [
      ...new Set(trades.value.map((trade) => trade.pair as string)),
    ];
    stalePositions.value = false;
  } else {
    stalePositions.value = true;
    failures.push('持仓目录读取失败，保留最近成功的持仓交易对。');
  }
  if (
    cfg.status === 'fulfilled' &&
    cfg.value &&
    typeof cfg.value === 'object' &&
    !Array.isArray(cfg.value)
  ) {
    config.value = cfg.value;
    staleConfig.value = false;
  } else {
    staleConfig.value = true;
    failures.push('引擎配置读取失败，环境信息以最近成功的旧配置为准。');
  }
  if (!failures.length)
    lastRead.value = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  sourceErrors.value = failures;
  loading.value = false;
}
onMounted(() => {
  void loadUniverse();
  timer = setInterval(() => void loadUniverse(), 60_000);
});
onUnmounted(() => {
  disposed = true;
  clearInterval(timer);
});
</script>

<template>
  <main class="legacy-market-workspace">
    <header class="market-heading">
      <div>
        <h1>行情观察台</h1>
        <p>旧 Freqtrade 交易引擎的交易对目录与市场图表</p>
      </div>
      <div class="market-environment" data-testid="market-environment">
        <strong :class="config?.dry_run === false ? 'environment-live' : ''">{{
          environment
        }}</strong>
        <span v-if="staleConfig">旧配置</span><span>{{ stateLabel }}</span
        ><span>{{ strategy }}</span>
      </div>
    </header>
    <div
      v-if="sourceErrors.length || selectionError"
      class="market-alert"
      role="alert"
    >
      <p v-for="message in sourceErrors" :key="message">{{ message }}</p>
      <p v-if="selectionError">{{ selectionError }}</p>
    </div>
    <div class="market-terminal">
      <aside class="pair-directory" aria-label="旧引擎交易对目录">
        <div class="directory-title">
          <h2>交易对目录</h2>
          <span
            >{{
              whitelist === null && positionPairs === null
                ? '未知'
                : pairs.length
            }}
            个</span
          >
        </div>
        <p class="directory-description">
          运行白名单与仍有持仓的交易对合并显示。
        </p>
        <label class="market-search"
          >筛选交易对<input
            v-model="search"
            type="search"
            placeholder="输入币种或交易对"
        /></label>
        <div class="pair-list" :aria-busy="loading">
          <button
            v-for="pair in visiblePairs"
            :key="pair"
            :data-pair="pair"
            :aria-pressed="selectedPair === pair && !invalidQuery"
            @click="selectPair(pair)"
          >
            <span>{{ pair }}</span
            ><small>{{ pairStatus(pair) }}</small>
          </button>
          <p v-if="!visiblePairs.length" class="directory-empty">
            {{
              loading && !pairs.length
                ? '正在读取真实目录…'
                : pairs.length
                  ? '没有匹配的交易对，请调整筛选。'
                  : whitelist === null || positionPairs === null
                    ? '目录尚未完整读取，请刷新重试。'
                    : '最近成功的目录暂无交易对。'
            }}
          </p>
        </div>
        <footer class="directory-footer">
          <button :disabled="loading" @click="loadUniverse">
            {{ loading ? '读取中…' : '刷新目录' }}</button
          ><span>每 60 秒读取<br />最近完整读取 {{ lastRead || '尚无' }}</span>
        </footer>
      </aside>
      <section class="market-chart-workspace" aria-label="行情图表工作区">
        <div class="chart-selection">
          <div data-testid="market-selection">
            <span>当前观察</span>
            <h2>{{ selectedPair || '等待交易对' }}</h2>
            <p>{{ timeframe }} 周期</p>
          </div>
          <label
            >蜡烛周期<select
              :value="timeframe"
              aria-label="行情蜡烛周期"
              @change="selectTimeframe"
            >
              <option
                v-for="period in periods"
                :key="period.value"
                :value="period.value"
              >
                {{ period.label }}
              </option>
            </select></label
          >
        </div>
        <div v-if="knownSelection" data-testid="market-chart">
          <PairChart
            :key="selectedPair + '|' + timeframe"
            :pair="selectedPair"
            :timeframe="timeframe"
          />
        </div>
        <div v-else class="market-no-selection">
          <h3>
            {{ selectionError ? '当前目标尚未核实' : '选择交易对后查看图表' }}
          </h3>
          <p>
            {{
              selectedPair
                ? '保留当前目标；未读取其他交易对的图表。'
                : '目录中的每个真实交易对均可独立浏览。'
            }}
          </p>
        </div>
        <div class="market-source-guide">
          <h3>图表来源</h3>
          <p>
            价格优先读取公共市场行情代理；不可用时回退到本地历史。策略信号来自旧引擎的策略蜡烛，只有时间与周期匹配时才叠加。
          </p>
          <p>页面不包含新 Binance Demo 账户余额、订单或验收结论。</p>
        </div>
      </section>
    </div>
  </main>
</template>

<style scoped>
.legacy-market-workspace {
  min-width: 0;
  padding: 24px;
  background: #0c1b2a;
  color: #e5edf4;
  font-family:
    'IBM Plex Sans', 'Source Han Sans SC', 'Noto Sans CJK SC', sans-serif;
}
.market-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 24px;
  margin-bottom: 22px;
}
.market-heading h1 {
  margin: 0 0 6px;
  font-size: 26px;
  font-weight: 650;
  letter-spacing: -0.02em;
}
.market-heading p,
.directory-description {
  margin: 0;
  color: #a2b8c9;
  font-size: 13px;
  line-height: 1.7;
}
.market-environment {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  align-items: center;
  gap: 8px 14px;
  max-width: 50%;
  font-size: 12px;
  color: #a2b8c9;
}
.market-environment strong {
  padding: 5px 9px;
  background: #19394a;
  color: #7acbaa;
  border: 1px solid #29485c;
  border-radius: 4px;
}
.market-environment .environment-live {
  color: #f0c17b;
}
.market-alert {
  padding: 12px 16px;
  border-left: 3px solid #f0c17b;
  background: #24323b;
  color: #f0c17b;
  margin-bottom: 16px;
  font-size: 13px;
  line-height: 1.7;
}
.market-alert p {
  margin: 0;
}
.market-terminal {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  border: 1px solid #29485c;
  background: #132b3e;
  min-width: 0;
}
.pair-directory {
  display: flex;
  flex-direction: column;
  min-width: 0;
  border-right: 1px solid #29485c;
}
.directory-title {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  padding: 19px 18px 7px;
}
.directory-title h2 {
  font-size: 15px;
  margin: 0;
}
.directory-title > span {
  font-size: 12px;
  color: #a2b8c9;
}
.directory-description {
  padding: 0 18px;
  font-size: 12px;
}
.market-search {
  display: grid;
  gap: 6px;
  padding: 17px 18px;
  font-size: 12px;
  color: #a2b8c9;
}
.market-search input,
.chart-selection select {
  width: 100%;
  min-width: 0;
  border: 1px solid #29485c;
  border-radius: 3px;
  background: #0c1b2a;
  color: #e5edf4;
  padding: 9px 10px;
  font: inherit;
}
.pair-list {
  flex: 1;
  max-height: 650px;
  overflow-y: auto;
  min-height: 230px;
}
.pair-list button {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 5px;
  width: 100%;
  padding: 12px 18px;
  border: 0;
  border-top: 1px solid #29485c;
  background: transparent;
  text-align: left;
  color: #e5edf4;
  cursor: pointer;
  font-size: 13px;
  overflow-wrap: anywhere;
}
.pair-list button[aria-pressed='true'] {
  background: #1a3b51;
  box-shadow: inset 3px 0 #69b4e4;
}
.pair-list button:hover {
  background: #19364a;
}
.pair-list small {
  font-size: 11px;
  color: #a2b8c9;
}
.directory-empty {
  padding: 20px 18px;
  color: #a2b8c9;
  font-size: 12px;
  line-height: 1.8;
}
.directory-footer {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: center;
  padding: 17px 18px;
  border-top: 1px solid #29485c;
}
.directory-footer button {
  padding: 7px 10px;
  border: 1px solid #69b4e4;
  border-radius: 3px;
  background: transparent;
  color: #69b4e4;
  cursor: pointer;
  white-space: nowrap;
  font-size: 12px;
}
.directory-footer button:disabled {
  opacity: 0.55;
  cursor: wait;
}
.directory-footer span {
  font-size: 10px;
  line-height: 1.8;
  color: #a2b8c9;
  text-align: right;
}
.market-chart-workspace {
  min-width: 0;
}
.chart-selection {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 18px;
  padding: 19px 24px;
  border-bottom: 1px solid #29485c;
}
.chart-selection span {
  font-size: 12px;
  color: #a2b8c9;
}
.chart-selection h2 {
  font-size: 21px;
  margin: 5px 0;
  overflow-wrap: anywhere;
}
.chart-selection p {
  margin: 0;
  font-size: 12px;
  color: #a2b8c9;
}
.chart-selection label {
  display: grid;
  gap: 6px;
  flex-shrink: 0;
  font-size: 12px;
  color: #a2b8c9;
}
.chart-selection select {
  min-width: 112px;
}
.market-no-selection {
  min-height: 380px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 32px;
  text-align: center;
  color: #a2b8c9;
}
.market-no-selection h3 {
  color: #e5edf4;
  font-size: 17px;
}
.market-no-selection p {
  font-size: 13px;
  line-height: 1.8;
}
.market-source-guide {
  padding: 17px 24px;
  border-top: 1px solid #29485c;
  font-size: 12px;
  line-height: 1.8;
  color: #a2b8c9;
}
.market-source-guide h3 {
  font-size: 13px;
  color: #e5edf4;
  margin: 0 0 6px;
}
.market-source-guide p {
  margin: 3px 0;
  max-width: 78ch;
}
.legacy-market-workspace :is(button, input, select):focus-visible {
  outline: 2px solid #69b4e4;
  outline-offset: 3px;
}
@media (min-width: 1600px) {
  .market-terminal {
    grid-template-columns: 300px minmax(0, 1fr);
  }
}
@media (max-width: 900px) {
  .legacy-market-workspace {
    padding: 16px;
  }
  .market-heading {
    flex-direction: column;
    gap: 14px;
  }
  .market-environment {
    max-width: 100%;
    justify-content: flex-start;
  }
  .market-terminal {
    grid-template-columns: 210px minmax(0, 1fr);
  }
  .chart-selection {
    padding: 16px;
    align-items: flex-start;
  }
  .chart-selection h2 {
    font-size: 17px;
  }
  .chart-selection select {
    min-width: 90px;
  }
  .market-source-guide {
    padding: 16px;
  }
}
@media (max-width: 600px) {
  .legacy-market-workspace {
    padding: 12px;
  }
  .market-heading h1 {
    font-size: 23px;
  }
  .market-terminal {
    grid-template-columns: minmax(0, 1fr);
  }
  .pair-directory {
    border-right: 0;
    border-bottom: 1px solid #29485c;
  }
  .pair-list {
    min-height: 0;
    max-height: 205px;
  }
  .market-search {
    padding: 12px 16px;
  }
  .directory-title {
    padding: 16px 16px 5px;
  }
  .directory-description {
    padding: 0 16px;
  }
  .pair-list button {
    padding: 10px 16px;
  }
  .directory-footer {
    padding: 12px 16px;
  }
  .chart-selection {
    gap: 12px;
  }
  .chart-selection h2 {
    font-size: 17px;
  }
  .chart-selection label {
    font-size: 11px;
  }
  .market-no-selection {
    min-height: 240px;
    padding: 24px;
  }
}
@media (prefers-reduced-motion: reduce) {
  * {
    scroll-behavior: auto !important;
  }
}
</style>

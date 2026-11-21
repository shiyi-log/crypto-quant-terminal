<script lang="ts" setup>
import { computed, ref } from 'vue';

type SymbolKey = 'BTCUSDT' | 'ETHUSDT' | 'TRXUSDT';
type Scenario = 'normal' | 'stale';

const props = withDefaults(defineProps<{ scenario?: Scenario }>(), {
  scenario: 'normal',
});

// 固定本地样本只供设计预览，所有交互都留在当前组件状态内。
const markets: Record<SymbolKey, { label: string; price: string; change: string; high: string; low: string }> = {
  BTCUSDT: { label: 'BTC / USDT', price: '67,842.10', change: '+1.82%', high: '68,210.00', low: '66,624.30' },
  ETHUSDT: { label: 'ETH / USDT', price: '3,142.80', change: '+0.94%', high: '3,188.50', low: '3,097.20' },
  TRXUSDT: { label: 'TRX / USDT', price: '0.16482', change: '-0.36%', high: '0.16704', low: '0.16310' },
};
const symbols = Object.keys(markets) as SymbolKey[];
const selectedSymbol = ref<SymbolKey>('BTCUSDT');
const selectedTimeframe = ref('15m');
const strategyState = ref('运行中');
const showPauseDialog = ref(false);
const price = computed(() => markets[selectedSymbol.value]);
const isStale = computed(() => props.scenario === 'stale');
const chartLine = computed(() => selectedSymbol.value === 'ETHUSDT'
  ? '0,44 22,37 44,41 66,26 88,31 110,17 132,25 154,12 176,18 198,7 220,14 242,4'
  : selectedSymbol.value === 'TRXUSDT'
    ? '0,20 22,25 44,18 66,32 88,28 110,39 132,33 154,42 176,35 198,46 220,39 242,48'
    : '0,42 22,35 44,39 66,22 88,29 110,13 132,20 154,8 176,16 198,5 220,12 242,3');

function confirmPause() {
  strategyState.value = '已暂停';
  showPauseDialog.value = false;
}
</script>

<template>
  <section class="desk-preview" aria-labelledby="desk-title">
    <div class="desk-topline">
      <div>
        <h2 id="desk-title">演示交易台</h2>
        <span class="desk-caption">合成行情与本地状态 · Binance USDⓈ-M</span>
      </div>
      <div class="desk-health" :class="{ 'is-stale': isStale }" role="status">
        <span class="health-dot" aria-hidden="true" />
        <span>{{ isStale ? '快照已过期 · 保留最后样本' : '样本更新时间 14:32:18' }}</span>
      </div>
    </div>

    <div class="desk-market-bar">
      <div class="symbol-switch" aria-label="演示交易对">
        <button
          v-for="symbol in symbols" :key="symbol" type="button"
          :data-symbol="symbol" :aria-pressed="selectedSymbol === symbol"
          @click="selectedSymbol = symbol"
        >{{ markets[symbol].label }}</button>
      </div>
      <div class="price-summary">
        <strong data-testid="selected-price">{{ price.price }}</strong>
        <span :class="price.change.startsWith('+') ? 'positive' : 'negative'">{{ price.change }}</span>
      </div>
    </div>

    <div class="desk-metrics">
      <div><span>演示权益</span><strong>1,000.00 <small>USDT</small></strong></div>
      <div><span>今日净损益</span><strong class="negative">-2.35 <small>USDT</small></strong></div>
      <div><span>计划风险占用</span><strong>0.42% <small>/ 2.00%</small></strong></div>
      <div><span>阶段回撤</span><strong>2.35 <small>/ 150 USDT</small></strong></div>
      <div class="strategy-metric"><span>策略状态</span><strong data-testid="strategy-state"><i :class="strategyState === '运行中' ? 'live' : 'paused'" />{{ strategyState }}</strong></div>
      <button class="pause-button" type="button" data-action="pause" @click="showPauseDialog = true">暂停新开仓</button>
    </div>

    <div class="desk-grid">
      <section class="market-panel" aria-labelledby="market-title">
        <div class="panel-heading">
          <div><h3 id="market-title">{{ markets[selectedSymbol].label }}</h3><span data-testid="chart-symbol">{{ selectedSymbol.slice(0, -4) }} · 价格走势</span></div>
          <div class="timeframes" aria-label="图表周期">
            <button v-for="frame in ['1m', '5m', '15m', '1h']" :key="frame" type="button" :data-timeframe="frame" :aria-pressed="selectedTimeframe === frame" @click="selectedTimeframe = frame">{{ frame }}</button>
          </div>
        </div>
        <div class="chart-wrap">
          <div class="chart-y-labels"><span>{{ price.high }}</span><span>{{ price.price }}</span><span>{{ price.low }}</span></div>
          <svg class="price-chart" viewBox="0 0 244 56" preserveAspectRatio="none" role="img" :aria-label="`${markets[selectedSymbol].label} 本地样本价格走势`">
            <line v-for="y in [5, 28, 51]" :key="y" x1="0" :y1="y" x2="244" :y2="y" class="chart-grid" />
            <polyline :points="chartLine" class="chart-line" />
            <circle cx="242" cy="4" r="3" class="chart-point" />
          </svg>
        </div>
        <div class="chart-times"><span>13:00</span><span>13:30</span><span>14:00</span><span>14:30</span></div>
      </section>

      <section class="risk-panel" aria-labelledby="risk-title">
        <div class="panel-heading"><div><h3 id="risk-title">账户风险</h3><span>本地策略快照</span></div><span class="risk-badge">入口受控</span></div>
        <dl class="risk-list">
          <div><dt>保证金模式</dt><dd>逐仓</dd></div>
          <div><dt>杠杆上限</dt><dd>10×</dd></div>
          <div><dt>保护单状态</dt><dd><span class="warning-dot" />待确认样本</dd></div>
          <div><dt>冷却状态</dt><dd>未触发</dd></div>
          <div><dt>信号质量</dt><dd class="muted-value">未知</dd></div>
        </dl>
      </section>
    </div>

    <section class="positions-panel" aria-labelledby="positions-title">
      <div class="panel-heading positions-heading"><div><h3 id="positions-title">持仓与委托样本</h3><span>仅展示固定示例，不对应交易所账户</span></div><span class="sample-tag">本地样本</span></div>
      <div class="table-scroll">
        <table><thead><tr><th>交易对</th><th>方向</th><th>数量</th><th>开仓均价</th><th>标记价格</th><th>未实现盈亏</th><th>保护</th></tr></thead>
          <tbody><tr v-for="position in [{ symbol: 'BTCUSDT', side: '多仓', qty: '0.002', entry: '67,850.00', mark: '67,842.10', pnl: '-0.02', protection: '待确认样本' }]" :key="position.symbol">
            <td><strong>{{ position.symbol }}</strong></td><td><span class="direction-long">{{ position.side }}</span></td><td>{{ position.qty }}</td><td>{{ position.entry }}</td><td>{{ position.mark }}</td><td class="negative">{{ position.pnl }} USDT</td><td><span class="warning-dot" />{{ position.protection }}</td>
          </tr></tbody>
        </table>
      </div>
    </section>

    <div v-if="showPauseDialog" class="dialog-backdrop" role="presentation" @click.self="showPauseDialog = false">
      <section class="pause-dialog" role="dialog" aria-modal="true" aria-labelledby="pause-title">
        <h3 id="pause-title">暂停新开仓</h3>
        <p>此操作仅影响演示数据，不会访问账户或发送交易命令。</p>
        <div class="dialog-actions">
          <button type="button" data-action="cancel" @click="showPauseDialog = false">取消</button>
          <button type="button" class="confirm-button" data-action="confirm" @click="confirmPause">确认暂停</button>
        </div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.desk-preview { --desk-bg: #0c1b2a; --desk-surface: #132b3e; --desk-line: #29485c; --desk-text: #e5edf4; --desk-muted: #a2b8c9; --desk-blue: #69b4e4; --desk-green: #65c998; --desk-red: #ef7d7d; min-width: 0; padding: 0 26px 24px; color: var(--desk-text); background: var(--desk-bg); font-family: 'IBM Plex Sans', 'Noto Sans SC', sans-serif; }
.desk-preview *, .desk-preview *::before, .desk-preview *::after { box-sizing: border-box; }
.desk-topline, .desk-market-bar, .panel-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.desk-topline { padding: 20px 0 15px; }
.desk-topline h2 { margin: 0; font-size: 19px; font-weight: 600; }
.desk-caption, .panel-heading span { color: var(--desk-muted); font-size: 11px; }
.desk-health { display: flex; align-items: center; gap: 8px; color: #9bd9ae; font-size: 11px; }
.health-dot, .strategy-metric i { width: 7px; height: 7px; border-radius: 50%; background: var(--desk-green); }
.desk-health.is-stale { color: #edc978; }
.desk-health.is-stale .health-dot { background: #edc978; }
.desk-market-bar { justify-content: flex-start; flex-wrap: wrap; padding: 12px 0; border-top: 1px solid var(--desk-line); border-bottom: 1px solid var(--desk-line); }
.symbol-switch, .timeframes { display: flex; gap: 4px; }
button { font: inherit; cursor: pointer; }
.symbol-switch button, .timeframes button { border: 1px solid transparent; color: var(--desk-muted); background: transparent; }
.symbol-switch button { min-height: 32px; padding: 0 10px; font-size: 12px; }
.symbol-switch button[aria-pressed='true'], .timeframes button[aria-pressed='true'] { border-color: #42637a; color: var(--desk-text); background: #1a3549; }
.price-summary { display: flex; align-items: baseline; gap: 10px; margin-left: auto; }
.price-summary strong { font-size: 22px; font-variant-numeric: tabular-nums; }
.price-summary span, .positive { color: var(--desk-green); font-size: 12px; }
.negative { color: var(--desk-red) !important; }
.desk-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)) minmax(100px, .8fr) auto; align-items: center; gap: 18px; padding: 15px 0; border-bottom: 1px solid var(--desk-line); }
.desk-metrics > div { display: grid; gap: 6px; min-width: 0; }
.desk-metrics > div > span { color: var(--desk-muted); font-size: 11px; }
.desk-metrics strong { display: flex; align-items: center; gap: 6px; min-width: 0; font-size: 14px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.desk-metrics small { color: var(--desk-muted); font-size: 10px; font-weight: 400; }
.strategy-metric strong { gap: 7px; }
.strategy-metric i.paused { background: #edc978; }
.pause-button { min-height: 34px; padding: 0 12px; border: 1px solid #875759; color: #f2aaaa; background: transparent; font-size: 11px; white-space: nowrap; }
.pause-button:hover { background: #3a272d; }
.desk-grid { display: grid; grid-template-columns: minmax(0, 1.8fr) minmax(245px, .8fr); gap: 14px; padding: 16px 0; }
.market-panel, .risk-panel, .positions-panel { min-width: 0; border: 1px solid var(--desk-line); background: var(--desk-surface); }
.market-panel, .risk-panel { padding: 15px; }
.panel-heading h3 { margin: 0 0 3px; font-size: 13px; font-weight: 600; }
.panel-heading > div > span { font-size: 10px; }
.timeframes button { min-width: 35px; min-height: 27px; padding: 0 6px; font-size: 10px; }
.chart-wrap { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: stretch; gap: 10px; height: 132px; padding-top: 14px; }
.chart-y-labels { display: flex; flex-direction: column; justify-content: space-between; color: var(--desk-muted); font-size: 9px; font-variant-numeric: tabular-nums; }
.price-chart { width: 100%; height: 100%; overflow: visible; }
.chart-grid { stroke: #29485c; stroke-width: .7; stroke-dasharray: 2 3; }
.chart-line { fill: none; stroke: var(--desk-blue); stroke-width: 1.7; vector-effect: non-scaling-stroke; }
.chart-point { fill: #9bd9ee; }
.chart-times { display: flex; justify-content: space-between; padding: 6px 0 0 40px; color: var(--desk-muted); font-size: 9px; }
.risk-badge, .sample-tag { padding: 4px 7px; border: 1px solid #44664f; color: #abd9b0 !important; font-size: 10px !important; white-space: nowrap; }
.risk-list { display: grid; gap: 0; margin: 9px 0 0; }
.risk-list div { display: flex; justify-content: space-between; gap: 12px; padding: 8px 0; border-top: 1px solid #29485c; font-size: 10px; }
.risk-list dt { color: var(--desk-muted); }
.risk-list dd { margin: 0; text-align: right; }
.warning-dot { display: inline-block; width: 6px; height: 6px; margin-right: 6px; border-radius: 50%; background: #edc978; }
.muted-value { color: #edc978; }
.positions-heading { padding: 12px 14px; border-bottom: 1px solid var(--desk-line); }
.table-scroll { max-width: 100%; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 10px; text-align: left; white-space: nowrap; }
th, td { padding: 11px 13px; border-bottom: 1px solid #29485c; }
th { color: var(--desk-muted); font-size: 9px; font-weight: 500; }
tbody tr:last-child td { border-bottom: 0; }
.direction-long { color: #9bd9ae; }
.dialog-backdrop { position: fixed; z-index: 20; inset: 0; display: grid; place-items: center; padding: 20px; background: rgb(0 0 0 / 58%); }
.pause-dialog { width: min(100%, 380px); padding: 20px; border: 1px solid #42637a; background: #132b3e; box-shadow: 0 16px 48px rgb(0 0 0 / 35%); }
.pause-dialog h3 { margin: 0 0 10px; font-size: 16px; }
.pause-dialog p { margin: 0; color: var(--desk-muted); font-size: 12px; line-height: 1.6; }
.dialog-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 22px; }
.dialog-actions button { min-height: 34px; padding: 0 12px; border: 1px solid #42637a; color: var(--desk-text); background: transparent; font-size: 11px; }
.dialog-actions .confirm-button { border-color: #875759; color: #f2aaaa; background: #382a31; }
button:focus-visible { outline: 2px solid var(--desk-blue); outline-offset: 2px; }
@media (max-width: 960px) { .desk-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 13px; } .pause-button { justify-self: start; } }
@media (max-width: 680px) { .desk-preview { padding: 0 14px 18px; } .desk-topline { align-items: flex-start; flex-direction: column; gap: 8px; } .desk-health { font-size: 10px; } .desk-market-bar { align-items: flex-start; flex-direction: column; gap: 9px; } .symbol-switch { width: 100%; overflow-x: auto; } .symbol-switch button { flex: 0 0 auto; padding-inline: 8px; } .price-summary { margin-left: 0; } .desk-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 13px 10px; } .strategy-metric { min-height: 40px; } .desk-grid { grid-template-columns: minmax(0, 1fr); } .market-panel, .risk-panel { padding: 12px; } .positions-heading { align-items: flex-start; } }
</style>

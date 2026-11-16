<script setup lang="ts">
import { computed, ref } from 'vue';
import { RouterLink } from 'vue-router';
import { shortOrderTime, tradeDisplayId } from '../utils/orderDisplayId';
import {
  environmentLabel,
  finiteNumber,
  sourceNames,
  sumKnown,
} from './overviewState';
import type { Fact, OverviewSource, OverviewState } from './overviewState';
const props = defineProps<{ state: OverviewState }>();
const facts = computed(() => props.state.facts);
const ledger = ref<'open' | 'recent'>('open');
const monitor = ref<'factors' | 'tasks' | 'services'>('factors');
const selected = ref<string | null>(null);
const rows = computed<Fact[]>(() =>
  ledger.value === 'open'
    ? (facts.value.positions ?? [])
    : (facts.value.trades?.trades ?? []),
);
const selectedRow = computed(() =>
  selected.value === null
    ? null
    : (rows.value.find((row) => String(row.trade_id) === selected.value) ??
      null),
);
const loadedLedger = computed(() =>
  ledger.value === 'open'
    ? facts.value.positions !== null
    : facts.value.trades !== null,
);
const unit = computed(
  () =>
    facts.value.balance?.stake ??
    facts.value.balance?.symbol ??
    facts.value.config?.stake_currency ??
    '计价币未知',
);
const openProfit = computed(() =>
  facts.value.positions === null
    ? null
    : sumKnown(facts.value.positions, 'profit_abs'),
);
const openStake = computed(() =>
  facts.value.positions === null
    ? null
    : sumKnown(facts.value.positions, 'stake_amount'),
);
const closedCount = computed(() =>
  finiteNumber(facts.value.profit?.closed_trade_count),
);
const winRate = computed(() => {
  const count = closedCount.value;
  const rate = finiteNumber(facts.value.profit?.winrate);
  return count !== null && count > 0 && rate !== null && rate >= 0 && rate <= 1
    ? rate * 100
    : null;
});
function amount(value: unknown, digits = 2) {
  const n = finiteNumber(value);
  return n === null
    ? '金额未知'
    : n.toLocaleString('en-US', {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      });
}
function signed(value: unknown, digits = 2) {
  const n = finiteNumber(value);
  return n === null ? '未知' : (n > 0 ? '+' : '') + amount(n, digits);
}
function tone(value: unknown) {
  const n = finiteNumber(value);
  return n === null || n === 0 ? '' : n > 0 ? 'positive' : 'negative';
}
function percent(row: Fact) {
  const n = finiteNumber(row.profit_ratio);
  return n === null ? finiteNumber(row.profit_pct) : n * 100;
}
function date(value: unknown) {
  if (typeof value !== 'string') return '尚未成功读取';
  const d = new Date(value);
  return Number.isNaN(d.getTime())
    ? value
    : d.toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' });
}
function sourceTime(key: OverviewSource) {
  return date(props.state.updated[key]);
}
function switchLedger(next: 'open' | 'recent') {
  ledger.value = next;
  selected.value = null;
}
function taskPercent(value: unknown) {
  const n = finiteNumber(value);
  return n !== null && n >= 0 && n <= 100 ? n : null;
}
function status(value: unknown) {
  const labels: Record<string, string> = {
    running: '运行中',
    stopped: '已停止',
    paused: '已暂停买入',
    done: '已完成',
    failed: '失败',
    pending: '等待中',
    queued: '排队中',
    idle: '待命',
    cancelled: '已取消',
    canceled: '已取消',
  };
  return typeof value === 'string' ? (labels[value] ?? value) : '未知';
}
const tasks = computed(() => {
  const output: Fact[] = [];
  if (facts.value.progress)
    output.push({
      ...facts.value.progress,
      key: 'backtest',
      name: '回测 / 训练',
      note: [
        facts.value.progress.phase,
        facts.value.progress.model,
        facts.value.progress.current_pair,
      ]
        .filter(Boolean)
        .join(' · '),
    });
  const research = facts.value.researchProgress?.progress;
  if (research && typeof research === 'object')
    output.push({
      ...research,
      key: 'research',
      name: '旧模型自动迭代',
      note:
        '轮次 ' +
        (research.round ?? '未知') +
        ' / trial ' +
        (research.trial ?? '未知') +
        '，' +
        (research.phase ?? '阶段未知'),
    });
  for (const job of facts.value.jobs ?? [])
    output.push({
      ...job,
      key: String(job.job_id ?? job.id),
      name: job.kind ?? job.name ?? '后台任务',
      note: job.message ?? job.phase ?? '',
    });
  return output;
});
const strategyFields = computed(() => [
  ['策略', facts.value.config?.strategy ?? '未知'],
  ['策略周期', facts.value.config?.timeframe ?? '未知'],
  ['交易模式', facts.value.config?.trading_mode ?? '未知'],
  ['保证金模式', facts.value.config?.margin_mode ?? '未知'],
  [
    '选币名单',
    facts.value.whitelist
      ? facts.value.whitelist.whitelist.length + ' 个交易对'
      : '未知',
  ],
  [
    '离场通道',
    facts.value.iteration?.live?.params?.exit_period === undefined
      ? '未知'
      : facts.value.iteration.live.params.exit_period + ' 日',
  ],
]);
const sources = Object.keys(sourceNames) as OverviewSource[];
</script>
<template>
  <main class="legacy-overview" aria-label="旧引擎总览">
    <header class="overview-heading">
      <div>
        <h1>旧引擎总览</h1>
        <p>旧 Freqtrade 交易引擎。账户、交易与研究快照保留各自来源和时间。</p>
      </div>
      <button class="refresh" :disabled="state.loading" @click="state.refresh">
        {{ state.loading ? '正在读取…' : '刷新总览事实' }}
      </button>
    </header>
    <div class="environment-strip">
      <strong>旧 Freqtrade 交易引擎</strong
      ><span class="environment">{{ environmentLabel(facts.config) }}</span>
      <span>引擎状态：{{ status(facts.config?.state) }}</span
      ><span>每 5 秒读取</span>
      <RouterLink to="/quant/trade-desk">Binance Demo 交易台</RouterLink>
    </div>
    <div v-if="Object.keys(state.errors).length" class="failure" role="alert">
      <strong>部分来源刷新失败</strong>
      <p>
        保留上次成功快照；首次未读取的事实显示未知。{{
          Object.values(state.errors).join(' ')
        }}
      </p>
    </div>
    <section class="account-tape" aria-label="旧引擎账户快照">
      <div>
        <span>账户权益</span
        ><strong
          >{{ amount(facts.balance?.total) }} <small>{{ unit }}</small></strong
        ><em>余额读取 {{ sourceTime('balance') }}</em>
      </div>
      <div>
        <span>总盈亏</span
        ><strong :class="tone(facts.profit?.profit_all_coin)"
          >{{ signed(facts.profit?.profit_all_coin) }}
          <small>{{ unit }}</small></strong
        ><em
          >已平仓 {{ signed(facts.profit?.profit_closed_coin) }} {{ unit }}</em
        >
      </div>
      <div>
        <span>已平仓样本</span>
        ><strong
          >{{
            closedCount === null ? '未知' : closedCount.toLocaleString('zh-CN')
          }}
          <small>笔</small></strong
        ><em
          >胜 {{ facts.profit?.winning_trades ?? '未知' }} / 负
          {{ facts.profit?.losing_trades ?? '未知' }}</em
        >
      </div>
      <div>
        <span>当前持仓 / 上限</span
        ><strong
          >{{ facts.positions === null ? '未知' : facts.positions.length }}
          <small
            >/
            {{
              facts.config?.max_open_trades === -1
                ? '不限'
                : (facts.config?.max_open_trades ?? '未知')
            }}</small
          ></strong
        ><em>投入 {{ amount(openStake) }} {{ unit }}</em>
      </div>
      <div>
        <span>浮动盈亏</span
        ><strong :class="tone(openProfit)"
          >{{ signed(openProfit) }} <small>{{ unit }}</small></strong
        ><em>直接汇总引擎返回金额</em>
      </div>
      <div>
        <span>已平仓胜率</span>
        ><strong
          >{{ winRate === null ? '未知' : winRate.toFixed(1) }}
          <small>{{ winRate === null ? '' : '%' }}</small></strong
        ><em>只使用引擎返回的样本与比例</em>
      </div>
    </section>
    <div class="overview-body">
      <section class="ledger-panel" aria-label="旧交易账本">
        <header class="section-heading">
          <div>
            <h2>交易账本</h2>
            <p>
              {{
                ledger === 'open' ? '当前持仓' : '最近已平仓'
              }}，点击交易编号检查原始记录。
            </p>
          </div>
          <RouterLink to="/quant/positions">完整持仓与成交</RouterLink>
        </header>
        <nav class="segment" aria-label="交易账本范围">
          <button
            data-ledger="open"
            :aria-pressed="ledger === 'open'"
            @click="switchLedger('open')"
          >
            当前持仓
            {{
              facts.positions === null ? '未知' : facts.positions.length
            }}</button
          ><button
            data-ledger="recent"
            :aria-pressed="ledger === 'recent'"
            @click="switchLedger('recent')"
          >
            最近已平仓 {{ facts.trades?.trades.length ?? '未知' }}
          </button>
        </nav>
        <div
          class="table-scroll"
          tabindex="0"
          aria-label="交易账本，可横向滚动"
        >
          <table>
            <thead>
              <tr>
                <th>交易 / 开仓编号</th>
                <th>交易对</th>
                <th>方向</th>
                <th>投入</th>
                <th>收益率</th>
                <th>盈亏</th>
                <th>{{ ledger === 'open' ? '开仓时间' : '平仓时间' }}</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in rows"
                :key="row.trade_id"
                :class="{ selected: selected === String(row.trade_id) }"
              >
                <td>
                  <button
                    :data-trade="row.trade_id"
                    :aria-pressed="selected === String(row.trade_id)"
                    @click="selected = String(row.trade_id)"
                  >
                    <b>#{{ row.trade_id }}</b
                    ><small class="mono">{{ tradeDisplayId(row) }}</small>
                  </button>
                </td>
                <td>{{ row.pair }}</td>
                <td>
                  {{
                    row.is_short === true
                      ? '做空'
                      : row.is_short === false
                        ? '做多'
                        : '未知'
                  }}
                </td>
                <td>{{ amount(row.stake_amount) }}</td>
                <td :class="tone(percent(row))">
                  {{ signed(percent(row))
                  }}{{ percent(row) === null ? '' : '%' }}
                </td>
                <td :class="tone(row.profit_abs)">
                  {{ signed(row.profit_abs) }}
                </td>
                <td>
                  {{
                    ledger === 'open'
                      ? shortOrderTime(row.open_timestamp, row.open_date)
                      : shortOrderTime(row.close_timestamp, row.close_date)
                  }}
                </td>
              </tr>
            </tbody>
          </table>
          <p v-if="!rows.length" class="empty">
            {{
              loadedLedger
                ? ledger === 'open'
                  ? '当前没有持仓记录。'
                  : '本次最近成交快照没有已平仓记录。'
                : '尚未成功读取交易账本。'
            }}
          </p>
        </div>
        <footer>
          来源
          {{
            ledger === 'open' ? '/v1/status' : '/v1/trades，最近 20 笔'
          }}。读取
          {{ sourceTime(ledger === 'open' ? 'positions' : 'trades') }}。金额单位
          {{ unit }}。
        </footer>
        <section class="monitor-panel">
          <nav class="segment" aria-label="运行证据类型">
            <button
              data-monitor="factors"
              :aria-pressed="monitor === 'factors'"
              @click="monitor = 'factors'"
            >
              因子监控</button
            ><button
              data-monitor="tasks"
              :aria-pressed="monitor === 'tasks'"
              @click="monitor = 'tasks'"
            >
              任务进度</button
            ><button
              data-monitor="services"
              :aria-pressed="monitor === 'services'"
              @click="monitor = 'services'"
            >
              程序状态
            </button>
          </nav>
          <template v-if="monitor === 'factors'"
            ><div class="table-scroll" tabindex="0">
              <table>
                <thead>
                  <tr>
                    <th>历史因子</th>
                    <th>基线 IC</th>
                    <th>近期 IC</th>
                    <th>原始判断</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="(factor, index) in facts.ops?.factors ?? []"
                    :key="factor.factor ?? index"
                  >
                    <td class="mono">{{ factor.factor ?? '未知' }}</td>
                    <td>{{ amount(factor.ic_base, 4) }}</td>
                    <td>{{ amount(factor.ic_recent, 4) }}</td>
                    <td>
                      {{ factor.status ?? '未知'
                      }}<span v-if="factor.alert === true" class="warning">
                        · 告警</span
                      >
                    </td>
                  </tr>
                </tbody>
              </table>
              <p v-if="!facts.ops?.factors?.length" class="empty">
                {{ facts.ops ? '此快照没有因子条目。' : '因子快照尚未读取。' }}
              </p>
            </div>
            <footer>
              历史因子监控不构成新策略准入结论。快照生成
              {{ facts.ops?.generated_at ?? '未知' }}。<RouterLink
                to="/quant/research"
                >查看策略研究</RouterLink
              >
            </footer></template
          >
          <template v-else-if="monitor === 'tasks'"
            ><ol class="task-list">
              <li v-for="task in tasks" :key="task.key">
                <div>
                  <strong>{{ task.name }}</strong
                  ><span>{{ status(task.status) }}</span
                  ><b>{{
                    taskPercent(task.percent) === null
                      ? '进度未知'
                      : taskPercent(task.percent) + '%'
                  }}</b>
                </div>
                <progress
                  v-if="taskPercent(task.percent) !== null"
                  max="100"
                  :value="taskPercent(task.percent)!"
                  :aria-label="task.name + '进度'"
                />
                <p>{{ task.note }}</p>
              </li>
            </ol>
            <p v-if="!tasks.length" class="empty">
              没有可读取的任务快照。此状态不能证明后台没有任务。
            </p>
            <footer>
              <RouterLink to="/quant/tasks">任务队列与产物</RouterLink>
              <RouterLink to="/quant/iteration">旧模型迭代</RouterLink>
            </footer></template
          >
          <template v-else
            ><ul class="service-list">
              <li
                v-for="(service, index) in facts.services?.services ?? []"
                :key="service.key ?? index"
              >
                <strong>{{
                  service.name ?? service.key ?? '服务名称未知'
                }}</strong
                ><span
                  :class="
                    service.running === true
                      ? 'positive'
                      : service.running === false
                        ? 'negative'
                        : 'warning'
                  "
                  >{{
                    service.running === true
                      ? '探测运行中'
                      : service.running === false
                        ? '探测未运行'
                        : '状态未知'
                  }}</span
                ><small
                  >端口 {{ service.port ?? '未知' }} / PID
                  {{ service.pid ?? '未知' }}</small
                >
              </li>
            </ul>
            <p v-if="!facts.services?.services?.length" class="empty">
              程序探测结果尚无服务条目。
            </p>
            <footer>
              进程探测不证明交易可用或允许新增风险。<RouterLink to="/quant/ops"
                >运维历史</RouterLink
              >
            </footer></template
          >
        </section>
      </section>
      <aside class="inspector" aria-label="旧引擎事实检查器">
        <h2>{{ selectedRow ? '所选交易' : '引擎与数据来源' }}</h2>
        <template v-if="selectedRow"
          ><p class="selected-pair">{{ selectedRow.pair }}</p>
          <dl>
            <div>
              <dt>原始交易 ID</dt>
              <dd class="mono">{{ selectedRow.trade_id }}</dd>
            </div>
            <div>
              <dt>交易状态</dt>
              <dd>{{ ledger === 'open' ? '当前持仓快照' : '已平仓快照' }}</dd>
            </div>
            <div>
              <dt>开仓价</dt>
              <dd>{{ amount(selectedRow.open_rate, 6) }}</dd>
            </div>
            <div>
              <dt>{{ ledger === 'open' ? '现价' : '平仓价' }}</dt>
              <dd>
                {{
                  amount(
                    ledger === 'open'
                      ? selectedRow.current_rate
                      : selectedRow.close_rate,
                    6,
                  )
                }}
              </dd>
            </div>
            <div>
              <dt>退出原因</dt>
              <dd>{{ selectedRow.exit_reason ?? '未提供' }}</dd>
            </div>
          </dl>
          <RouterLink
            data-trade-detail
            :to="{
              path: '/quant/positions',
              query: { trade_id: String(selectedRow.trade_id) },
            }"
            >查看此交易完整订单</RouterLink
          ><button class="clear-selection" @click="selected = null">
            返回来源检查
          </button></template
        >
        <template v-else
          ><dl>
            <div v-for="field in strategyFields" :key="field[0]">
              <dt>{{ field[0] }}</dt>
              <dd>{{ field[1] }}</dd>
            </div>
          </dl>
          <p v-if="selected !== null" class="warning">
            所选交易已不在当前快照中，请重新选择；没有替换为其他交易。
          </p>
          <details>
            <summary>独立来源读取记录</summary>
            <ul class="source-list">
              <li v-for="key in sources" :key="key">
                <strong>{{ sourceNames[key] }}</strong
                ><span :class="state.errors[key] ? 'warning' : ''">{{
                  state.errors[key]
                    ? '读取失败，保留快照'
                    : state.updated[key]
                      ? '已读取'
                      : '未读取'
                }}</span
                ><small>{{ sourceTime(key) }}</small>
              </li>
            </ul>
          </details></template
        >
        <div class="audit-note">
          <strong>快照边界</strong>
          <p>
            旧引擎权益与 Binance Demo
            分开记录。未知手续费、资金费或缺失金额不会据此补成零；本页不计算新账户风险额度或验收资格。
          </p>
          <RouterLink to="/quant/market">打开旧选币行情</RouterLink>
        </div>
      </aside>
    </div>
  </main>
</template>
<style scoped>
.legacy-overview {
  min-width: 0;
  max-width: 100%;
  padding: 24px;
  background: #0c1b2a;
  color: #e5edf4;
  font:
    14px/1.55 'IBM Plex Sans',
    'Source Han Sans SC',
    'Noto Sans CJK SC',
    sans-serif;
}
.overview-heading,
.section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.overview-heading h1 {
  font-size: 26px;
  line-height: 1.2;
  margin: 0 0 8px;
  font-weight: 650;
}
.overview-heading p,
.section-heading p {
  color: #a2b8c9;
  margin: 0;
}
.legacy-overview button,
.legacy-overview a {
  color: #69b4e4;
}
.legacy-overview button {
  cursor: pointer;
  font: inherit;
}
.legacy-overview button:disabled {
  cursor: wait;
  opacity: 0.65;
}
.refresh {
  border: 1px solid #69b4e4;
  background: #132b3e;
  padding: 8px 14px;
  border-radius: 4px;
  white-space: nowrap;
}
.environment-strip {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px 18px;
  margin: 20px 0 0;
  padding: 12px 0;
  border-top: 1px solid #29485c;
  border-bottom: 1px solid #29485c;
  color: #a2b8c9;
}
.environment-strip strong {
  color: #e5edf4;
}
.environment {
  color: #f0c17b;
  border: 1px solid #6c573b;
  padding: 2px 8px;
  border-radius: 3px;
}
.environment-strip a {
  margin-left: auto;
}
.failure {
  padding: 12px 16px;
  border: 1px solid #8a714b;
  color: #f0c17b;
  margin: 16px 0 0;
  background: #302b26;
}
.failure p {
  margin: 4px 0 0;
}
.account-tape {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 0;
  border-bottom: 1px solid #29485c;
  margin: 0 0 24px;
}
.account-tape > div {
  padding: 20px 18px;
  min-width: 0;
  border-right: 1px solid #29485c;
}
.account-tape > div:first-child {
  padding-left: 0;
}
.account-tape > div:last-child {
  border-right: 0;
}
.account-tape span,
.account-tape em {
  display: block;
  color: #a2b8c9;
  font-size: 12px;
  font-style: normal;
}
.account-tape strong {
  display: block;
  font-size: 25px;
  line-height: 1.4;
  font-weight: 550;
  overflow-wrap: anywhere;
  font-variant-numeric: tabular-nums;
  margin: 5px 0;
}
.account-tape small {
  font-size: 12px;
  font-weight: 400;
}
.overview-body {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 290px;
  gap: 24px;
  align-items: start;
}
.ledger-panel,
.inspector {
  min-width: 0;
}
.section-heading {
  margin-bottom: 14px;
}
.section-heading h2,
.inspector h2 {
  margin: 0 0 5px;
  font-size: 17px;
}
.section-heading > a {
  font-size: 12px;
  white-space: nowrap;
}
.segment {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  border-bottom: 1px solid #29485c;
}
.segment button {
  border: 0;
  background: transparent;
  padding: 10px 15px;
  color: #a2b8c9;
}
.segment button[aria-pressed='true'] {
  background: #132b3e;
  box-shadow: inset 0 -2px #69b4e4;
  color: #e5edf4;
}
.table-scroll {
  max-width: 100%;
  min-width: 0;
  overflow: auto;
  background: #102435;
}
.table-scroll table {
  border-collapse: collapse;
  width: 100%;
  min-width: 790px;
  text-align: left;
  font-size: 12px;
}
.table-scroll th {
  font-weight: 500;
  color: #a2b8c9;
  background: #132b3e;
  white-space: nowrap;
  padding: 11px 12px;
  border-bottom: 1px solid #29485c;
}
.table-scroll td {
  padding: 12px;
  border-bottom: 1px solid #29485c;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.table-scroll td button {
  border: 0;
  background: none;
  text-align: left;
  padding: 0;
}
.table-scroll td button b {
  font-size: 14px;
  font-weight: 500;
}
.table-scroll td small {
  display: block;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  color: #a2b8c9;
  font-size: 10px;
  margin-top: 3px;
}
.selected {
  background: #183a51;
}
.legacy-overview footer {
  color: #a2b8c9;
  font-size: 11px;
  margin: 10px 0 0;
  overflow-wrap: anywhere;
}
.legacy-overview footer a {
  margin-left: 8px;
}
.empty {
  color: #a2b8c9;
  padding: 28px 14px;
  margin: 0;
  white-space: normal;
}
.monitor-panel {
  margin-top: 30px;
}
.monitor-panel table {
  min-width: 600px;
}
.inspector {
  background: #132b3e;
  border: 1px solid #29485c;
  border-radius: 5px;
  padding: 18px;
}
.inspector h2 {
  padding-bottom: 13px;
  border-bottom: 1px solid #29485c;
}
.inspector dl {
  margin: 0;
}
.inspector dl > div {
  padding: 11px 0;
  display: flex;
  justify-content: space-between;
  gap: 14px;
  border-bottom: 1px solid #29485c;
}
.inspector dt {
  color: #a2b8c9;
  font-size: 12px;
  flex-shrink: 0;
}
.inspector dd {
  margin: 0;
  text-align: right;
  overflow-wrap: anywhere;
  min-width: 0;
}
.inspector summary {
  cursor: pointer;
  color: #69b4e4;
  padding: 18px 0;
}
.source-list,
.service-list,
.task-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.source-list li {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 2px 8px;
  padding: 10px 0;
  border-bottom: 1px solid #29485c;
  font-size: 11px;
}
.source-list strong {
  font-weight: 400;
}
.source-list small {
  grid-column: 1/-1;
  color: #a2b8c9;
}
.audit-note {
  padding-top: 20px;
  margin-top: 20px;
  border-top: 1px solid #29485c;
  color: #a2b8c9;
  font-size: 12px;
}
.audit-note p {
  margin: 8px 0 12px;
}
.audit-note strong {
  color: #e5edf4;
  font-weight: 500;
}
.selected-pair {
  font-size: 19px;
  overflow-wrap: anywhere;
}
.clear-selection {
  display: block;
  padding: 8px 0 0;
  margin-top: 10px;
  background: none;
  border: 0;
}
.task-list li {
  padding: 14px;
  border-bottom: 1px solid #29485c;
  background: #102435;
}
.task-list li > div {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 15px;
}
.task-list strong {
  font-weight: 500;
  margin-right: auto;
}
.task-list span,
.task-list p {
  color: #a2b8c9;
  font-size: 12px;
}
.task-list p {
  margin: 4px 0;
  overflow-wrap: anywhere;
}
.task-list progress {
  width: 100%;
  height: 5px;
  accent-color: #69b4e4;
  margin-top: 10px;
}
.service-list li {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 5px;
  padding: 13px;
  border-bottom: 1px solid #29485c;
  background: #102435;
}
.service-list small {
  color: #a2b8c9;
  grid-column: 1/-1;
}
.mono {
  font-family: 'IBM Plex Mono', ui-monospace, monospace;
}
.legacy-overview .positive {
  color: #7acbaa;
}
.legacy-overview .negative {
  color: #f19a9a;
}
.legacy-overview .warning {
  color: #f0c17b;
}
.legacy-overview :focus-visible {
  outline: 2px solid #69b4e4;
  outline-offset: 3px;
}
.legacy-overview a {
  text-underline-offset: 3px;
}
.legacy-overview a:hover {
  text-decoration: underline;
}
@media (min-width: 1700px) {
  .overview-body {
    grid-template-columns: minmax(0, 1fr) 340px;
  }
  .account-tape strong {
    font-size: 30px;
  }
}
@media (max-width: 1100px) {
  .overview-body {
    grid-template-columns: minmax(0, 1fr);
  }
  .inspector dl {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0 24px;
  }
  .inspector {
    padding: 18px;
  }
  .account-tape strong {
    font-size: 21px;
  }
  .account-tape {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  .account-tape > div:nth-child(3) {
    border-right: 0;
  }
  .account-tape > div:nth-child(4) {
    padding-left: 0;
  }
}
@media (max-width: 600px) {
  .legacy-overview {
    padding: 16px;
  }
  .overview-heading {
    flex-wrap: wrap;
  }
  .overview-heading h1 {
    font-size: 24px;
  }
  .environment-strip a {
    margin-left: 0;
  }
  .account-tape {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    margin-bottom: 20px;
  }
  .account-tape > div {
    padding: 16px 10px;
    border-bottom: 1px solid #29485c;
  }
  .account-tape > div:nth-child(odd) {
    padding-left: 0;
  }
  .account-tape > div:nth-child(even) {
    border-right: 0;
  }
  .account-tape strong {
    font-size: 20px;
  }
  .section-heading {
    flex-wrap: wrap;
    gap: 8px;
  }
  .overview-body {
    gap: 20px;
  }
  .inspector dl {
    display: block;
  }
  .segment button {
    padding: 10px 12px;
  }
  .task-list li {
    padding: 12px;
  }
  .table-scroll table {
    min-width: 750px;
  }
}
</style>

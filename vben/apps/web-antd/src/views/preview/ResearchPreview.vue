<script lang="ts" setup>
import { computed, ref, watch } from 'vue';

type Factor = {
  id: string;
  name: string;
  family: string;
  description: string;
  detail: string;
  inputQuality: 'complete' | 'stale' | 'unknown';
  inputQualityLabel: string;
  inputQualityReason: string;
};
type Strategy = {
  id: string;
  candidateId: string;
  dataWindow: string;
  costAssumption: string;
  name: string;
  description: string;
  parameters: string[];
  curve: string;
};
type Experiment = {
  id: string;
  name: string;
  status: 'running' | 'failed' | 'completed';
  progress: string;
  reason: string;
};
type Flow = {
  symbol: string;
  price: string;
  change: string;
  spread: string;
  depth: string;
  notional: string;
  tradeCount: string;
  aggressiveBuy: string;
  aggressiveSell: string;
  maturity: string;
  fillRate: string;
  adverseSelection: string;
  freshness: 'valid' | 'stale' | 'unknown';
  observedAt: string;
  trades: string[];
};

const props = defineProps<{ workspace: string }>();
const workspaces = [
  { id: 'factors', label: '因子目录' },
  { id: 'research', label: '策略研究' },
  { id: 'experiments', label: '实验任务' },
  { id: 'orderflow', label: '订单流' },
];
const activeWorkspace = ref(
  workspaces.some((item) => item.id === props.workspace)
    ? props.workspace
    : 'factors',
);
watch(
  () => props.workspace,
  (workspace) => {
    activeWorkspace.value = workspaces.some((item) => item.id === workspace)
      ? workspace
      : 'factors';
  },
);
const factors: Factor[] = [
  // 输入质量只描述下面的固定演示样本，不代表真实行情完整性或策略准入，也不写入事件日志。
  {
    id: 'volume_breakout',
    name: '成交量突破',
    family: '成交量',
    description: '比较当前成交量与历史基线，识别异常放量。',
    detail: '以近 20 根 K 线的中位成交量建立基线，当前收盘确认后计算放量倍数。',
    inputQuality: 'complete',
    inputQualityLabel: '演示样本完整',
    inputQualityReason: '20 根已收盘 K 线基线齐全',
  },
  {
    id: 'momentum_continuation',
    name: '动量延续',
    family: '价格',
    description: '观察多周期收益是否保持同向。',
    detail: '由 5m、15m 和 1h 的已收盘收益组成，缺口会使该因子不可评估。',
    inputQuality: 'stale',
    inputQualityLabel: '演示样本过期',
    inputQualityReason: '最后一段样本超过演示有效窗口',
  },
  {
    id: 'book_imbalance',
    name: '盘口失衡',
    family: '盘口',
    description: '记录可见档位的买卖深度差异。',
    detail: '只使用连续可见盘口快照；断档或序列不连续时保留为未知。',
    inputQuality: 'unknown',
    inputQualityLabel: '演示样本未知',
    inputQualityReason: '盘口序列含断档，不能判定输入完整性',
  },
];
// 候选与成本字段都是演示声明；前端展示不产生冻结、准入或运行事件。
const strategies: Strategy[] = [
  {
    id: 'trend',
    candidateId: 'STRAT-DEMO-TREND-01',
    dataWindow: '2026-10-01 至 2026-10-31 · BTC/ETH/TRX · 5m',
    costAssumption: '每边 10 bps 手续费 + 8 bps 保守滑点；资金费单列',
    name: '趋势跟随',
    description: '用成交量确认的方向突破跟随趋势。',
    parameters: ['EMA 12 / 48', '成交量阈值 2.0x', 'ATR 跟踪止损 1.0'],
    curve: '趋势曲线关注突破后的延续，压力场景会单独计入滑点和资金费。',
  },
  {
    id: 'range',
    candidateId: 'STRAT-DEMO-RANGE-01',
    dataWindow: '2026-10-01 至 2026-10-31 · BTC/ETH/TRX · 15m',
    costAssumption: '每边 10 bps 手续费 + 8 bps 保守滑点；资金费单列',
    name: '均值回归',
    description: '在波动收敛时交易布林带偏离。',
    parameters: ['布林带 20 / 2.0', '偏离阈值 1.5σ', '持仓上限 4 小时'],
    curve: '均值回归曲线关注布林带偏离后的回归速度，并冻结成本假设。',
  },
  {
    id: 'orderflow',
    candidateId: 'STRAT-DEMO-ORDERFLOW-01',
    dataWindow: '2026-10-01 至 2026-10-31 · BTC/ETH/TRX · 1m',
    costAssumption: '每边 10 bps 手续费 + 12 bps 保守滑点；资金费单列',
    name: '订单流确认',
    description: '结合主动成交和盘口深度判断成交质量。',
    parameters: ['主动买卖差 1.8x', '可见深度 20 档', '最大报价年龄 2 秒'],
    curve: '订单流策略同时记录成交率、部分成交率和逆向选择。',
  },
];
const experiments: Experiment[] = [
  {
    id: 'EXP-024',
    name: '压力成本复核',
    status: 'failed',
    progress: '已停止 · 68%',
    reason: '资金费数据存在 2 处缺口，压力场景无法完成成本归属。',
  },
  {
    id: 'EXP-023',
    name: '动量参数搜索',
    status: 'running',
    progress: '运行中 · 42%',
    reason: '正在完成验证窗口；当前只显示演示进度，不代表研究结论。',
  },
  {
    id: 'EXP-021',
    name: '订单流覆盖盘点',
    status: 'completed',
    progress: '已完成 · 100%',
    reason: '已生成覆盖率摘要与不可评估区间清单。',
  },
];
// 订单流指标和新鲜度均为固定演示事实；此视图不读取行情、账户或执行结果。
const flows: Flow[] = [
  {
    symbol: 'BTCUSDT',
    price: '91,520.40',
    change: '+1.24%',
    spread: '0.80',
    depth: '2.8m USDT',
    notional: '842.6k USDT',
    tradeCount: '1,284 笔',
    aggressiveBuy: '468.2k USDT',
    aggressiveSell: '374.4k USDT',
    maturity: '已成熟 · 128 个事件',
    fillRate: '72.4%',
    adverseSelection: '3.8 bps',
    freshness: 'valid',
    observedAt: '2026-11-23 14:32',
    trades: [
      '91,482.00 · 主动买 · 0.032 BTC',
      '91,520.40 · 主动卖 · 0.018 BTC',
    ],
  },
  {
    symbol: 'ETHUSDT',
    price: '3,184.62',
    change: '+0.82%',
    spread: '0.12',
    depth: '1.4m USDT',
    notional: '未知',
    tradeCount: '未知',
    aggressiveBuy: '未知',
    aggressiveSell: '未知',
    maturity: '未知',
    fillRate: '未知',
    adverseSelection: '未知',
    freshness: 'stale',
    observedAt: '2026-11-23 14:31',
    trades: ['3,184.58 · 主动买 · 1.24 ETH', '3,184.62 · 主动卖 · 0.86 ETH'],
  },
  {
    symbol: 'TRXUSDT',
    price: '0.2841',
    change: '-0.18%',
    spread: '0.0001',
    depth: '420k USDT',
    notional: '未知',
    tradeCount: '未知',
    aggressiveBuy: '未知',
    aggressiveSell: '未知',
    maturity: '未知',
    fillRate: '未知',
    adverseSelection: '未知',
    freshness: 'unknown',
    observedAt: '2026-11-23 14:30',
    trades: ['0.2840 · 主动买 · 2,400 TRX', '0.2841 · 主动卖 · 1,800 TRX'],
  },
];

const factorQuery = ref('');
const experimentQuery = ref('');
const selectedFactorId = ref(factors[0]!.id);
const selectedStrategyId = ref(strategies[0]!.id);
const experimentFilter = ref<'all' | Experiment['status']>('all');
const selectedExperimentId = ref(experiments[0]!.id);
const selectedFlowSymbol = ref(flows[0]!.symbol);
const filteredFactors = computed(() => {
  const q = factorQuery.value.trim().toLowerCase();
  return q
    ? factors.filter((f) =>
        [f.id, f.name, f.family, f.description]
          .join(' ')
          .toLowerCase()
          .includes(q),
      )
    : factors;
});
const selectedFactor = computed<Factor | null>(
  () =>
    filteredFactors.value.find(
      (factor) => factor.id === selectedFactorId.value,
    ) ??
    filteredFactors.value[0] ??
    null,
);
const selectedStrategy = computed<Strategy>(
  () =>
    strategies.find((s) => s.id === selectedStrategyId.value) ?? strategies[0]!,
);
// 演示任务仅在本页按固定样本筛选，不发起网络请求或写入实验记录。
const filteredExperiments = computed(() => {
  const q = experimentQuery.value.trim().toLowerCase();
  return experiments.filter((experiment) => {
    const matchesStatus =
      experimentFilter.value === 'all' ||
      experiment.status === experimentFilter.value;
    const matchesQuery =
      !q ||
      [experiment.id, experiment.name, experiment.progress, experiment.reason]
        .join(' ')
        .toLowerCase()
        .includes(q);
    return matchesStatus && matchesQuery;
  });
});
const selectedExperiment = computed<Experiment | null>(
  () =>
    filteredExperiments.value.find(
      (experiment) => experiment.id === selectedExperimentId.value,
    ) ??
    filteredExperiments.value[0] ??
    null,
);
const selectedFlow = computed<Flow>(
  () => flows.find((f) => f.symbol === selectedFlowSymbol.value) ?? flows[0]!,
);
// 搜索只展示当前可见的固定样本，详情始终跟随首个匹配项；无匹配时清空选择。
watch(filteredFactors, (visible) => {
  if (!visible.some((factor) => factor.id === selectedFactorId.value)) {
    selectedFactorId.value = visible[0]?.id ?? '';
  }
});
watch(filteredExperiments, (visible) => {
  if (!visible.some((experiment) => experiment.id === selectedExperimentId.value)) {
    selectedExperimentId.value = visible[0]?.id ?? '';
  }
});
function statusLabel(status: Experiment['status']) {
  return { running: '运行中', failed: '失败', completed: '已完成' }[status];
}
</script>

<template>
  <!-- 研究预览嵌入应用主内容时使用区域语义，标签 ID 与对应面板保持一一关联。 -->
  <section class="research-preview" aria-labelledby="research-preview-title">
    <header class="research-header">
      <div>
        <p class="eyebrow">LOCAL RESEARCH PREVIEW</p>
        <h1 id="research-preview-title">研究与证据工作区</h1>
        <p class="lede">
          固定演示样本 · 不连接交易账户 · 研究结论不会授予 Demo 或实盘资格
        </p>
      </div>
      <span class="demo-badge">本地演示数据</span>
    </header>
    <nav class="workspace-tabs" aria-label="研究工作区" role="tablist">
      <button
        v-for="item in workspaces"
        :key="item.id"
        :id="'research-tab-' + item.id"
        type="button"
        role="tab"
        :data-workspace="item.id"
        :aria-selected="activeWorkspace === item.id"
        :aria-controls="'research-panel-' + item.id"
        @click="activeWorkspace = item.id"
      >
        {{ item.label }}
      </button>
    </nav>
    <section
      v-if="activeWorkspace === 'factors'"
      id="research-panel-factors"
      class="workspace-panel"
      role="tabpanel"
      aria-labelledby="research-tab-factors"
    >
      <div class="panel-heading">
        <div>
          <p class="eyebrow">FACTOR CATALOG</p>
          <h2 id="factor-title">因子实验室</h2>
        </div>
        <span class="muted"
          >{{ filteredFactors.length }} / {{ factors.length }} 个因子</span
        >
      </div>
      <div class="control-row">
        <label class="field-label"
          >搜索因子<input
            v-model="factorQuery"
            aria-label="搜索因子"
            type="search"
            placeholder="名称、家族或 ID" /></label
        ><span class="scope-note">按已收盘样本展示；缺口不会填零。</span>
      </div>
      <div class="split-layout">
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>因子</th>
                <th>家族</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="factor in filteredFactors"
                :key="factor.id"
                :class="{ selected: factor.id === selectedFactor?.id }"
              >
                <td>
                  <button
                    class="row-button"
                    type="button"
                    :data-factor="factor.id"
                    @click="selectedFactorId = factor.id"
                  >
                    {{ factor.name }}<small>{{ factor.id }}</small>
                  </button>
                </td>
                <td>{{ factor.family }}</td>
                <td>{{ factor.description }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <article
          v-if="selectedFactor"
          class="detail-pane"
          data-testid="factor-detail"
        >
          <p class="eyebrow">SELECTED FACTOR</p>
          <h3>{{ selectedFactor.name }}</h3>
          <code>{{ selectedFactor.id }}</code>
          <p>{{ selectedFactor.detail }}</p>
          <dl>
            <div
              class="factor-quality"
              data-testid="factor-input-quality"
              :data-quality="selectedFactor.inputQuality"
            >
              <dt>输入状态</dt>
              <dd>{{ selectedFactor.inputQualityLabel }}</dd>
              <dt>样本原因</dt>
              <dd>{{ selectedFactor.inputQualityReason }}</dd>
            </div>
            <div>
              <dt>版本</dt>
              <dd>factor-demo-02</dd>
            </div>
          </dl>
        </article>
        <article
          v-else
          class="detail-pane empty-state"
          data-testid="factor-empty"
          role="status"
        >
          <h3>没有符合条件的因子</h3>
          <p>调整搜索词后查看本地演示样本。</p>
        </article>
      </div>
    </section>
    <section
      v-else-if="activeWorkspace === 'research'"
      id="research-panel-research"
      class="workspace-panel"
      role="tabpanel"
      aria-labelledby="research-tab-research"
    >
      <div class="panel-heading">
        <div>
          <p class="eyebrow">STRATEGY CANDIDATES</p>
          <h2 id="strategy-title">策略研究</h2>
        </div>
        <span class="muted">冻结前比较</span>
      </div>
      <div class="strategy-grid">
        <button
          v-for="strategy in strategies"
          :key="strategy.id"
          type="button"
          class="strategy-option"
          :class="{ selected: strategy.id === selectedStrategyId }"
          :data-strategy="strategy.id"
          @click="selectedStrategyId = strategy.id"
        >
          <strong>{{ strategy.name }}</strong
          ><span>{{ strategy.description }}</span
          ><small>{{ strategy.id }}</small>
        </button>
      </div>
      <article class="strategy-detail" data-testid="strategy-detail">
        <div>
          <p class="eyebrow">演示候选 · 未冻结</p>
          <h3>{{ selectedStrategy.name }}</h3>
          <p>{{ selectedStrategy.description }}</p>
          <code data-testid="strategy-identity">{{ selectedStrategy.candidateId }}</code>
        </div>
        <ul>
          <li v-for="parameter in selectedStrategy.parameters" :key="parameter">
            {{ parameter }}
          </li>
        </ul>
        <dl class="strategy-evidence">
          <div>
            <dt>候选身份</dt>
            <dd>{{ selectedStrategy.candidateId }}</dd>
          </div>
          <div>
            <dt>数据窗口</dt>
            <dd>{{ selectedStrategy.dataWindow }}</dd>
          </div>
          <div>
            <dt>成本假设</dt>
            <dd>{{ selectedStrategy.costAssumption }}</dd>
          </div>
          <div>
            <dt>留出集</dt>
            <dd>未评估</dd>
          </div>
          <div>
            <dt>准入状态</dt>
            <dd>未冻结 · 不进入 Demo 预检</dd>
          </div>
        </dl>
        <p class="curve-note">{{ selectedStrategy.curve }}</p>
      </article>
    </section>
    <section
      v-else-if="activeWorkspace === 'experiments'"
      id="research-panel-experiments"
      class="workspace-panel"
      role="tabpanel"
      aria-labelledby="research-tab-experiments"
    >
      <div class="panel-heading">
        <div>
          <p class="eyebrow">EXPERIMENT QUEUE</p>
          <h2 id="experiment-title">实验任务</h2>
        </div>
        <div class="experiment-controls">
          <label class="search-field">
            <span>搜索实验任务</span>
            <input
              v-model="experimentQuery"
              type="search"
              aria-label="搜索实验任务"
              placeholder="名称、编号、进度或原因"
            />
          </label>
          <label class="compact-field">
            任务状态<select v-model="experimentFilter" aria-label="任务状态">
              <option value="all">全部</option>
              <option value="running">运行中</option>
              <option value="failed">失败</option>
              <option value="completed">已完成</option>
            </select>
          </label>
        </div>
      </div>
      <div class="split-layout">
        <div v-if="filteredExperiments.length" class="task-list" data-testid="experiment-list">
          <button
            v-for="experiment in filteredExperiments"
            :key="experiment.id"
            type="button"
            class="task-row"
            :class="{ selected: experiment.id === selectedExperimentId }"
            :data-experiment="experiment.id"
            @click="selectedExperimentId = experiment.id"
          >
            <span
              ><strong>{{ experiment.name }}</strong
              ><small
                >{{ experiment.id }} · {{ experiment.progress }}</small
              ></span
            ><em :data-status="experiment.status">{{
              statusLabel(experiment.status)
            }}</em>
          </button>
        </div>
        <div v-else class="experiment-empty-state" data-testid="experiment-empty" role="status">
          没有符合条件的实验任务
        </div>
        <article v-if="selectedExperiment" class="detail-pane" data-testid="experiment-detail">
          <p class="eyebrow">TASK DETAIL</p>
          <h3>{{ selectedExperiment.name }}</h3>
          <code>{{ selectedExperiment.id }}</code>
          <p>{{ selectedExperiment.reason }}</p>
          <div class="status-line">
            {{ statusLabel(selectedExperiment.status) }} ·
            {{ selectedExperiment.progress }}
          </div>
        </article>
      </div>
    </section>
    <section
      v-else-if="activeWorkspace === 'orderflow'"
      id="research-panel-orderflow"
      class="workspace-panel"
      role="tabpanel"
      aria-labelledby="research-tab-orderflow"
    >
      <div class="panel-heading">
        <div>
          <p class="eyebrow">ORDER FLOW SNAPSHOT</p>
          <h2 id="flow-title">订单流与价格</h2>
        </div>
        <span class="muted">仅展示演示快照</span>
      </div>
      <div class="symbol-tabs" role="tablist" aria-label="交易对">
        <button
          v-for="flow in flows"
          :key="flow.symbol"
          :id="'symbol-tab-' + flow.symbol"
          type="button"
          role="tab"
          class="symbol-tab"
          :class="{ selected: flow.symbol === selectedFlowSymbol }"
          :data-symbol="flow.symbol"
          aria-controls="symbol-panel"
          :aria-selected="flow.symbol === selectedFlowSymbol"
          @click="selectedFlowSymbol = flow.symbol"
        >
          {{ flow.symbol }}
        </button>
      </div>
      <div
        id="symbol-panel"
        class="symbol-panel"
        role="tabpanel"
        :aria-labelledby="'symbol-tab-' + selectedFlow.symbol"
      >
        <div class="flow-grid">
          <article class="flow-quote">
            <span class="muted">最新价</span
            ><strong data-testid="flow-price">{{ selectedFlow.price }}</strong
            ><span
              :class="selectedFlow.change.startsWith('-') ? 'loss' : 'profit'"
              >{{ selectedFlow.change }}</span
            >
          </article>
          <article>
            <span class="muted">买卖价差</span
            ><strong>{{ selectedFlow.spread }}</strong
            ><span class="muted">可见深度 {{ selectedFlow.depth }}</span>
          </article>
        </div>
        <div
          class="flow-evidence"
          data-testid="flow-evidence"
          :data-freshness="selectedFlow.freshness"
        >
          <div class="flow-evidence-heading">
            <h3>订单流样本摘要</h3>
            <span
              class="freshness-status"
              :class="'freshness-' + selectedFlow.freshness"
            >
              {{
                selectedFlow.freshness === 'valid'
                  ? '可用演示样本'
                  : selectedFlow.freshness === 'stale'
                    ? '数据过期'
                    : '数据未知'
              }}
            </span>
          </div>
          <dl>
            <div><dt>成交额</dt><dd>{{ selectedFlow.notional }}</dd></div>
            <div><dt>成交笔数</dt><dd>{{ selectedFlow.tradeCount }}</dd></div>
            <div><dt>主动买</dt><dd>{{ selectedFlow.aggressiveBuy }}</dd></div>
            <div><dt>主动卖</dt><dd>{{ selectedFlow.aggressiveSell }}</dd></div>
            <div><dt>事件成熟度</dt><dd>{{ selectedFlow.maturity }}</dd></div>
            <div><dt>成交率</dt><dd>{{ selectedFlow.fillRate }}</dd></div>
            <div><dt>逆向选择</dt><dd>{{ selectedFlow.adverseSelection }}</dd></div>
          </dl>
          <p>观测时间 {{ selectedFlow.observedAt }} · 固定演示快照，不代表实时市场数据或交易结果。</p>
        </div>
        <article class="trades-panel" data-testid="flow-trades">
          <div class="panel-heading small-heading">
            <h3>逐笔成交</h3>
            <span class="muted">{{ selectedFlow.symbol }} · 本地快照</span>
          </div>
          <ul>
            <li v-for="trade in selectedFlow.trades" :key="trade">
              {{ trade }}
            </li>
          </ul>
        </article>
      </div>
    </section>
    <section v-else class="workspace-panel empty-panel">
      <p class="eyebrow">RESEARCH PREVIEW</p>
      <h2>选择一个研究工作区</h2>
      <p>当前入口没有绑定研究视图。</p>
    </section>
  </section>
</template>

<style scoped>
.research-preview {
  /* 研究页采用和交易台一致的共享色标，独立预览仍有确定回退值。 */
  --bg: var(--qt-ocean, #0c1b2a);
  --surface: var(--qt-workspace, #132b3e);
  --line: var(--qt-divider, #29485c);
  --text: var(--qt-foreground, #e5edf4);
  --muted: var(--qt-muted, #a2b8c9);
  --accent: var(--qt-interactive, #69b4e4);
  --profit: var(--qt-profit, #49c7a5);
  --loss: var(--qt-loss, #f27f78);
  min-width: 0;
  min-height: 100%;
  padding: 24px;
  color: var(--text);
  background: var(--bg);
  font-family: var(
    --qt-ui-font,
    'IBM Plex Sans',
    'Source Han Sans SC',
    'Noto Sans SC',
    sans-serif
  );
}
.research-preview *,
.research-preview *::before,
.research-preview *::after {
  box-sizing: border-box;
}
h1,
h2,
h3,
p,
ul,
dl {
  margin: 0;
}
h1 {
  font-size: 28px;
  font-weight: 600;
}
h2 {
  font-size: 18px;
  font-weight: 600;
}
h3 {
  font-size: 17px;
  font-weight: 600;
}
.research-header,
.panel-heading,
.control-row,
.strategy-detail,
.flow-grid {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
}
.flow-evidence {
  margin-top: 18px;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  padding: 14px 0;
}
.flow-evidence-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.flow-evidence-heading h3 {
  margin: 0;
  font-size: 14px;
}
.freshness-status {
  font-size: 12px;
  font-weight: 600;
}
.freshness-valid {
  color: var(--profit);
}
.freshness-stale,
.freshness-unknown {
  color: var(--muted);
}
.flow-evidence dl {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px 18px;
  margin: 14px 0;
}
.flow-evidence dl div {
  display: grid;
  gap: 5px;
  min-width: 0;
}
.flow-evidence dt {
  color: var(--muted);
  font-size: 11px;
}
.flow-evidence dd {
  margin: 0;
  overflow-wrap: anywhere;
  font-size: 12px;
}
.flow-evidence p {
  margin: 0;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.6;
}
.research-header {
  border-bottom: 1px solid var(--line);
  padding-bottom: 20px;
}
.eyebrow {
  margin-bottom: 6px;
  color: var(--accent);
  font-size: 11px;
  letter-spacing: 0.08em;
}
.lede,
.muted,
.scope-note {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}
.lede {
  margin-top: 8px;
}
.demo-badge {
  border: 1px solid var(--accent);
  padding: 6px 10px;
  color: var(--accent);
  font-size: 12px;
  white-space: nowrap;
}
.workspace-tabs {
  display: flex;
  gap: 4px;
  overflow-x: auto;
  border-bottom: 1px solid var(--line);
  scrollbar-width: thin;
}
.workspace-tabs button {
  min-height: 43px;
  border: 0;
  border-bottom: 2px solid transparent;
  padding: 0 14px;
  color: var(--muted);
  background: transparent;
  font: inherit;
  font-size: 13px;
  white-space: nowrap;
  cursor: pointer;
}
.workspace-tabs button[aria-selected='true'] {
  border-bottom-color: var(--accent);
  color: var(--text);
}
.workspace-tabs button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -3px;
}
.workspace-panel {
  display: grid;
  gap: 20px;
  padding-top: 24px;
}
.panel-heading {
  align-items: flex-end;
}
.control-row {
  align-items: flex-end;
  justify-content: flex-start;
  padding-bottom: 4px;
  border-bottom: 1px solid var(--line);
}
.field-label,
.compact-field {
  display: grid;
  gap: 7px;
  color: var(--muted);
  font-size: 12px;
}
input,
select {
  min-width: 220px;
  border: 1px solid var(--line);
  border-radius: 0;
  padding: 9px 11px;
  color: var(--text);
  background: var(--surface);
  font: inherit;
}
input:focus-visible,
select:focus-visible,
button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.split-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(240px, 0.8fr);
  gap: 24px;
  align-items: start;
}
.table-wrap {
  min-width: 0;
  overflow-x: auto;
}
table {
  width: 100%;
  min-width: 560px;
  border-collapse: collapse;
  font-size: 13px;
}
th,
td {
  border-bottom: 1px solid var(--line);
  padding: 12px 10px;
  text-align: left;
  vertical-align: top;
}
th {
  color: var(--muted);
  font-size: 11px;
  font-weight: 500;
}
tr.selected td {
  background: rgba(105, 180, 228, 0.08);
}
.row-button {
  display: grid;
  gap: 3px;
  border: 0;
  padding: 0;
  color: var(--text);
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.row-button small,
.strategy-option small,
.task-row small {
  color: var(--muted);
  font-size: 11px;
}
.detail-pane {
  display: grid;
  gap: 12px;
  min-width: 0;
  border-left: 2px solid var(--accent);
  padding: 16px 0 16px 18px;
  background: rgba(19, 43, 62, 0.55);
}
.detail-pane p {
  color: var(--muted);
  font-size: 13px;
  line-height: 1.7;
}
code {
  color: var(--accent);
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12px;
}
dl {
  display: grid;
  gap: 8px;
}
dl div {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  color: var(--muted);
  font-size: 12px;
}
dd {
  color: var(--text);
}
.factor-quality {
  display: grid;
  grid-template-columns: minmax(72px, auto) minmax(0, 1fr);
  gap: 6px 12px;
  align-items: start;
}
.factor-quality dd {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
}
.factor-quality[data-quality='complete'] dd:first-of-type {
  color: var(--profit);
}
.factor-quality[data-quality='stale'] dd:first-of-type {
  color: #f0c17b;
}
.factor-quality[data-quality='unknown'] dd:first-of-type {
  color: var(--loss);
}
.strategy-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}
.strategy-option {
  display: grid;
  gap: 8px;
  min-height: 112px;
  border: 1px solid var(--line);
  border-radius: 0;
  padding: 15px;
  color: var(--text);
  background: var(--surface);
  text-align: left;
  cursor: pointer;
}
.strategy-option span {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.5;
}
.strategy-option.selected {
  border-color: var(--accent);
  box-shadow: inset 3px 0 var(--accent);
}
.strategy-detail {
  display: grid;
  grid-template-columns: minmax(180px, 0.8fr) minmax(180px, 0.7fr) minmax(260px, 1.5fr);
  gap: 18px;
  align-items: flex-start;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  padding: 18px 0;
}
.strategy-detail > div {
  min-width: 0;
  overflow-wrap: anywhere;
}
.curve-note {
  max-width: 54ch;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.65;
}
.strategy-detail ul {
  display: grid;
  gap: 8px;
  margin-left: auto;
  padding-left: 20px;
  color: var(--muted);
  font-size: 12px;
}
.strategy-evidence {
  min-width: 0;
}
.strategy-evidence div {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  align-items: start;
}
.strategy-evidence dd {
  min-width: 0;
  overflow-wrap: anywhere;
  text-align: right;
}
.strategy-detail code {
  overflow-wrap: anywhere;
  color: var(--accent);
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12px;
}
.task-list {
  display: grid;
  gap: 1px;
  border-top: 1px solid var(--line);
}
.task-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  border: 0;
  border-bottom: 1px solid var(--line);
  padding: 14px 12px;
  color: var(--text);
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.task-row span {
  display: grid;
  gap: 5px;
}
.task-row.selected {
  background: rgba(105, 180, 228, 0.08);
}
.task-row em {
  font-style: normal;
  font-size: 12px;
}
[data-status='failed'] {
  color: var(--loss);
}
[data-status='running'] {
  color: #f0c17b;
}
[data-status='completed'] {
  color: var(--profit);
}
.status-line {
  border-top: 1px solid var(--line);
  padding-top: 12px;
  color: #f0c17b;
  font-size: 12px;
}
.compact-field {
  align-items: end;
}
.compact-field select {
  min-width: 150px;
}

.experiment-controls {
  display: flex;
  align-items: end;
  gap: 12px;
}

.search-field {
  display: grid;
  gap: 5px;
  color: var(--muted);
  font-size: 12px;
}

.search-field input {
  min-width: 180px;
  min-height: 36px;
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 0 10px;
  color: var(--text);
  background: #0a1723;
  font: inherit;
}

.experiment-empty-state {
  display: grid;
  grid-column: 1 / -1;
  border: 1px solid var(--line);
  min-height: 120px;
  place-items: center;
  padding: 20px;
  color: var(--muted);
  text-align: center;
}

@media (max-width: 640px) {
  .experiment-controls {
    width: 100%;
    align-items: stretch;
    flex-direction: column;
  }

  .search-field input {
    width: 100%;
    min-width: 0;
  }
}
.symbol-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  border-bottom: 1px solid var(--line);
  padding-bottom: 10px;
}
.symbol-tab {
  border: 1px solid var(--line);
  border-radius: 0;
  padding: 8px 12px;
  color: var(--muted);
  background: transparent;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.symbol-tab.selected {
  border-color: var(--accent);
  color: var(--text);
  background: var(--surface);
}
.flow-grid {
  align-items: stretch;
  justify-content: flex-start;
  gap: 1px;
}
.flow-grid article {
  display: grid;
  gap: 8px;
  min-width: 220px;
  border-bottom: 1px solid var(--line);
  padding: 18px 20px 18px 0;
}
.flow-grid strong {
  font-size: 28px;
  font-weight: 500;
}
.flow-quote .profit {
  color: var(--profit);
}
.flow-quote .loss {
  color: var(--loss);
}
.trades-panel {
  max-width: 720px;
  border-top: 1px solid var(--line);
  padding-top: 18px;
}
.small-heading {
  align-items: center;
}
.trades-panel ul {
  display: grid;
  gap: 10px;
  padding: 14px 0 0 18px;
  color: var(--muted);
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12px;
}
.empty-panel {
  min-height: 250px;
  align-content: center;
}
@media (max-width: 800px) {
  .research-preview {
    padding: 18px;
  }
  .research-header {
    align-items: flex-start;
    flex-direction: column;
  }
  .split-layout,
  .strategy-grid {
    grid-template-columns: 1fr;
  }
  .strategy-detail {
    align-items: flex-start;
    grid-template-columns: minmax(0, 1fr);
  }
  .strategy-detail ul {
    margin-left: 0;
  }
  .flow-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
  }
  .flow-evidence dl {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .flow-grid article {
    min-width: 0;
    padding-right: 12px;
  }
}
@media (max-width: 480px) {
  h1 {
    font-size: 23px;
  }
  .control-row,
  .panel-heading {
    align-items: flex-start;
    flex-direction: column;
  }
  input,
  select {
    width: 100%;
    min-width: 0;
  }
  .flow-grid {
    grid-template-columns: 1fr;
  }
  .flow-grid strong {
    font-size: 24px;
  }
}
</style>

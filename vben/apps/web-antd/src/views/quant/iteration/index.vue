<script lang="ts" setup>
/**
 * 模型迭代 —— 走查（walk-forward）四变量拆解结果
 *
 * 数据来源: bot/build_iteration.py → user_data/iteration_summary.json
 * 评估框架: bot/walkforward.py
 *
 * 为什么单独一页：
 *   策略研究页已积累 9 个板块，迭代结果排在第 8 个，很难找到。
 *   迭代是本项目的持续工作，值得独立成页。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue';

import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Input,
  message,
  Modal,
  Progress,
  Row,
  Spin,
  Table,
  TabPane,
  Tabs,
  Tag,
} from 'ant-design-vue';

import {
  getIteration,
  getMlStatus,
  getModelVersions,
  promoteModelVersion,
} from '#/api/freqtrade';

import ResearchLedger from './ResearchLedger.vue';

const it = ref<any>(null);
const ml = ref<any>(null);
// 供模板使用（Empty 的预置图）
const EMPTY_SIMPLE = Empty.PRESENTED_IMAGE_SIMPLE;

/** 研究内容分页：把原先一路堆叠的卡片按用途分组 */
const tab = ref('result');

/** 候选列表默认只显示最新 3 个 —— 否则 11 个候选会把页面撑得很长 */
const verExpanded = ref(false);
const CAND_PREVIEW = 3;
function visibleChallengers(list: any[]) {
  if (!list?.length) return [];
  return verExpanded.value ? list : list.slice(-CAND_PREVIEW);
}
const loading = ref(false);

/* ── 模型版本管理（正在使用 vs 最新迭代）── */
const ver = ref<any>(null);
const promoteOpen = ref(false);
const promoteNote = ref('');
const target = ref<any>(null);
const promoting = ref(false);

const LAYER_NAME: Record<string, string> = {
  live_strategy: '实盘策略',
  ml_model: '研究模型',
};
function layerName(k: string) {
  return LAYER_NAME[k] || k;
}
function metricsText(v: any) {
  const m = v?.metrics || {};
  if (v?.metrics_validity === 'invalidated' || v?.id === 'live-trend-20-20') {
    return '研究口径已作废 · 实际年化未知（旧值仅留档）';
  }
  if (m.ic_period !== null && m.ic_period !== undefined) {
    return `IC ${Number(m.ic_period).toFixed(4)} · t=${Number(m.t_period).toFixed(2)} · 正窗口 ${m.pos_windows}/${m.n_windows}`;
  }
  if ('research_annual' in m || 'live_annual' in m) {
    const percent = (value: unknown) =>
      typeof value === 'number' && Number.isFinite(value)
        ? `${(value * 100).toFixed(1)}%`
        : '未知';
    return `研究年化 ${percent(m.research_annual)} · 实际年化 ${percent(m.live_annual)}`;
  }
  return '—';
}

function gateInvalidated(v: any) {
  return (
    v?.metrics_validity === 'invalidated' ||
    v?.gate?.status === 'invalidated'
  );
}

function gatePassed(v: any) {
  return !gateInvalidated(v) && v?.gate?.passed === true;
}

function gateLabel(v: any) {
  if (gateInvalidated(v)) return '口径已作废';
  return gatePassed(v) ? '通过' : '未通过';
}

function gateColor(v: any) {
  if (gateInvalidated(v)) return 'default';
  return gatePassed(v) ? 'green' : 'orange';
}

/* ══════════ 结论全部由数据推导 ══════════
 * 这一页原来是写死最多的地方：结论文案、轮次、IC 显著性、风险收益区间
 * 都是手抄进模板的，数据一变就与事实不符。这里统一改为计算属性。
 */
const live = computed<any>(() => it.value?.live ?? null);
const liveParams = computed<any>(() => live.value?.params ?? {});
const opt = computed<any>(() => it.value?.optimization ?? null);

/** top_n 边际影响里，哪个取值样本外 Calmar 最高 */
const bestTopN = computed(() => {
  const entries = Object.entries(opt.value?.by_top_n ?? {}) as [
    string,
    number,
  ][];
  if (!entries.length) return null;
  const [key, value] = entries.reduce((a, b) => (b[1] > a[1] ? b : a));
  return { key, value };
});
const currentTopN = computed(() => String(liveParams.value?.top_n ?? ''));
const topNIsBest = computed(
  () => bestTopN.value !== null && currentTopN.value === bestTopN.value.key,
);
const currentCombo = computed(() => {
  const p = liveParams.value;
  if (p.enter_period === undefined) return null;
  return `${p.enter_period}/${p.exit_period}/top${p.top_n}`;
});

/** 幸存者偏差：有偏名单 vs 无偏名单的 Calmar 比值 */
const universeBias = computed(() => {
  const uc = it.value?.universe?.universe_comparison;
  const a = uc?.static20_ins?.calmar;
  const b = uc?.static21_ins?.calmar;
  if (typeof a !== 'number' || typeof b !== 'number' || !b) return null;
  return {
    biased: a,
    unbiased: b,
    ratio: a / b,
    inflatedPct: (a / b - 1) * 100,
  };
});
const pitRows = computed<any[]>(
  () => it.value?.universe?.universe_comparison?.pit ?? [],
);

/**
 * 变量 A 的表格数据。
 *
 * 模板原来直接取 `it.universe.static`，但 build_iteration.py 写出的是
 * `universe_comparison`（含 static20_ins / static21_ins）—— 字段名对不上，
 * 这张卡片一直是「暂无数据」。这里优先用 `static`，没有就从
 * `universe_comparison` 组装，避免再出现空卡片。
 */
const universeStaticRows = computed<any[]>(() => {
  const u = it.value?.universe;
  if (Array.isArray(u?.static) && u.static.length) return u.static;
  const uc = u?.universe_comparison;
  if (!uc) return [];
  return [
    { label: '静态 20（当前名单·有偏差）', ...uc.static20_ins },
    { label: '静态 21（无偏差·同期）', ...uc.static21_ins },
  ];
});
/** 时点选池序列是否单调（非单调 = 噪声） */
const pitMonotonic = computed(() => {
  const vals = pitRows.value
    .map((r: any) => r?.calmar)
    .filter((v: any) => typeof v === 'number');
  if (vals.length < 3) return null;
  const up = vals.every((v, i) => i === 0 || v >= vals[i - 1]!);
  const down = vals.every((v, i) => i === 0 || v <= vals[i - 1]!);
  return up || down;
});
const pitSeqText = computed(() =>
  pitRows.value.map((r: any) => `${r.n}→${r.calmar}`).join(' · '),
);

/** 风险收益曲线：当前实盘名单 / 无偏选池 */
const riskOos = computed<Record<string, any[]>>(
  () => it.value?.risk_curve?.oos ?? {},
);
function pickRiskRows(keys: string[], fallbackIdx: number) {
  const all = riskOos.value;
  const name = Object.keys(all).find((k) => keys.some((x) => k.includes(x)));
  return (name ? all[name] : Object.values(all)[fallbackIdx]) ?? [];
}
const riskCurrent = computed<any[]>(() => pickRiskRows(['静态', '当前'], 0));
const riskUnbiased = computed<any[]>(() => pickRiskRows(['动态', '无偏'], 1));

function calmarRange(rows: any[]) {
  const vals = rows.map((r) => r?.calmar).filter((v) => typeof v === 'number');
  return vals.length
    ? { min: Math.min(...vals), max: Math.max(...vals) }
    : null;
}
const currentCalmarRange = computed(() => calmarRange(riskCurrent.value));
/** 达到 30% 年化所需的最低敞口档 */
const exposureForTarget = computed(() => {
  const target = 30;
  return riskCurrent.value.find((r) => (r?.ann ?? -Infinity) >= target) ?? null;
});
const unbiasedBest = computed(() => {
  const rows = riskUnbiased.value.filter(Boolean);
  if (!rows.length) return null;
  return rows.reduce((a, b) =>
    (b.ann ?? -Infinity) > (a.ann ?? -Infinity) ? b : a,
  );
});
/** 建议目标：回撤可控（> -25%）的档位年化区间 */
const suggestedTarget = computed(() => {
  const rows = riskCurrent.value.filter((r) => r && r.mdd > -25);
  if (!rows.length) return null;
  const anns = rows.map((r) => r.ann);
  return {
    exposures: [rows[0]!.exposure, rows.at(-1)!.exposure],
    max: Math.max(...anns),
    // 这些档位里最差的一档回撤，作为「回撤控制在 X% 以内」的依据
    maxMdd: Math.min(...rows.map((r) => r.mdd)),
    min: Math.min(...anns),
  };
});

/** 深度学习：逐窗口显著性与多重比较结果 */
// ── 模型自动迭代（第 24/25 轮）──
const iterProg = computed<any>(() => ml.value?.iteration?.progress ?? null);
const maxSeeds = computed<number>(() => {
  const rows = (ml.value?.iteration?.recent ?? []) as any[];
  return rows.reduce((m, r) => Math.max(m, Number(r.n_seeds || 1)), 1);
});
const bestT = computed<number | null>(() => {
  const rows = (ml.value?.iteration?.passing ?? []) as any[];
  if (!rows.length) return null;
  return Math.max(...rows.map((r) => Number(r.t_quarter || 0)));
});
const passCols = [
  { title: '模型', dataIndex: 'kind', width: 110 },
  { title: 'seq', dataIndex: 'seq_len', width: 60 },
  { title: 'hidden', dataIndex: 'hidden', width: 70 },
  { title: '层', dataIndex: 'layers', width: 50 },
  { title: 't(月)', dataIndex: 't_month', width: 80 },
  { title: 't(季)', dataIndex: 't_quarter', width: 80 },
  { title: 't(半年)', dataIndex: 't_half', width: 85 },
  { title: '稳健', dataIndex: 'robust', width: 70 },
  { title: 'q', dataIndex: 'q', width: 80 },
  { title: '种子', dataIndex: 'n_seeds', width: 60 },
];
const passRows = computed<any[]>(() =>
  ((ml.value?.iteration?.passing ?? []) as any[])
    .slice(-12)
    .reverse()
    .map((r) => ({
      ...r,
      key: `${r.kind}-${r.seq_len}-${r.hidden}-${r.layers}-${r.n_seeds ?? 1}`,
      t_month: r.t_month == null ? '—' : Number(r.t_month).toFixed(2),
      t_quarter: r.t_quarter == null ? '—' : Number(r.t_quarter).toFixed(2),
      t_half: r.t_half == null ? '—' : Number(r.t_half).toFixed(2),
      robust: `${r.robust_pass ?? '—'}/${r.robust_total ?? '—'}`,
      q: r.q == null ? '—' : Number(r.q).toFixed(4),
      n_seeds: r.n_seeds ?? 1,
    })),
);

const wfT = computed<number | null>(
  () => ml.value?.conclusion?.walkforward_ic_t ?? null,
);
const wfSignificant = computed(() => wfT.value !== null && wfT.value > 2);
const regimePassed = computed(() => ml.value?.regime_neutral_t_gt2 ?? 0);
const regimeTotal = computed(() => (ml.value?.regime_groups ?? []).length);
const bestSeq = computed(() => {
  const entries = Object.entries(ml.value?.seq ?? {}) as [string, any][];
  if (!entries.length) return null;
  const [name, v] = entries.reduce((a, b) =>
    (b[1]?.t_period ?? -9e9) > (a[1]?.t_period ?? -9e9) ? b : a,
  );
  return { name, ...v };
});

/** 上线闸门判据：直接取自版本注册表，不在前端重抄一遍 */
const gateCriteria = computed(() => target.value?.gate?.criteria ?? '');

function openPromote(c: any) {
  target.value = c;
  promoteNote.value = '';
  promoteOpen.value = true;
}
async function doPromote() {
  if (promoteNote.value.trim().length < 4) {
    message.warning('请填写至少 4 个字的登记理由（会写入版本历史）');
    return;
  }
  promoting.value = true;
  try {
    await promoteModelVersion(
      target.value.id,
      promoteNote.value.trim(),
      !gatePassed(target.value),
    );
    message.success(`已登记为优选版本：${target.value.id}`);
    promoteOpen.value = false;
    await load();
  } catch (error: any) {
    message.error(error?.message || '登记失败');
  } finally {
    promoting.value = false;
  }
}

async function load() {
  // 只有首屏显示 Spin；轮询刷新时静默，避免每 60 秒闪一次
  if (!it.value) loading.value = true;
  try {
    it.value = await getIteration();
    ml.value = await getMlStatus().catch(() => null);
    ver.value = await getModelVersions().catch(() => null);
  } finally {
    loading.value = false;
  }
}
let timer: ReturnType<typeof setInterval> | null = null;
onMounted(() => {
  load();
  timer = setInterval(load, 60_000);
});
onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>

<template>
  <div class="p-4">
    <ResearchLedger />
    <Spin :spinning="loading">
      <!-- ══════════ 版本登记：优选记录与实际部署分开 ══════════ -->
      <Card v-if="ver" :bordered="false" class="shadow-sm">
        <template #title>
          <span>🔖 模型版本管理</span>
          <span class="ml-2 text-xs font-normal text-muted-foreground">
            优选登记（champion）vs 最新迭代（challenger）；实际部署见证据账
          </span>
        </template>
        <template #extra>
          <span class="text-xs text-muted-foreground">
            更新 {{ ver.updated || '—' }}
            <Tag v-if="ver.running" color="processing" class="ml-2">
              自动迭代运行中 (PID {{ ver.running.pid }})
            </Tag>
          </span>
        </template>

        <div v-for="(l, key) in ver.layers || {}" :key="key" class="mb-3">
          <div class="mb-1 text-sm font-medium">
            {{ layerName(String(key)) }}
          </div>
          <Row :gutter="[12, 12]">
            <!-- 注册表优选，不等于运行中的模型 -->
            <Col :lg="10" :xs="24">
              <div
                class="h-full rounded-lg border border-emerald-200 bg-emerald-50/40 p-3 dark:border-emerald-900 dark:bg-emerald-950/20"
              >
                <div class="flex flex-wrap items-center gap-2">
                  <Tag :color="gateInvalidated(l.champion) ? 'default' : 'green'">
                    {{ gateInvalidated(l.champion) ? '口径已作废' : '优选登记' }}
                  </Tag>
                  <span class="font-mono text-xs">{{
                    l.champion?.id || '（未设置）'
                  }}</span>
                </div>
                <div class="mt-1 text-xs text-muted-foreground">
                  {{ l.champion?.label || '—' }}
                </div>
                <div class="mt-1 text-[11px] text-muted-foreground">
                  历史登记指标 ·
                  {{ metricsText(l.champion) }}
                </div>
                <div class="mt-1 text-[11px] text-orange-500">
                  {{
                    `登记判据${gateLabel(l.champion)}`
                  }}
                  · 注册表配置
                  {{
                    l.champion?.deployed?.config || '暂无'
                  }}；实际部署状态以证据账核验
                </div>
              </div>
            </Col>

            <!-- 最新迭代 -->
            <Col :lg="14" :xs="24">
              <div
                class="rounded-lg border border-gray-100 p-3 dark:border-gray-800"
              >
                <div class="mb-2 text-xs font-medium">
                  🧪 最新迭代（候选 {{ l.challengers?.length || 0 }} 个）
                </div>
                <div
                  v-if="!l.challengers?.length"
                  class="text-xs text-muted-foreground"
                >
                  暂无候选 —— 运行
                  <code class="mx-1">python bot/auto_research.py</code> 自动产出
                </div>
                <div
                  v-for="c in visibleChallengers(l.challengers)"
                  :key="c.id"
                  class="mb-2 flex flex-wrap items-center gap-2 border-b border-dashed border-gray-100 pb-2 last:border-0 last:pb-0 dark:border-gray-800"
                >
                  <span class="font-mono text-xs">{{ c.id }}</span>
                  <Tag :color="gateColor(c)">
                    登记判据{{ gateLabel(c) }}
                  </Tag>
                  <span v-if="c.round" class="text-[11px] text-blue-500">
                    第 {{ c.round }} 轮
                  </span>
                  <span class="font-mono text-[11px] text-muted-foreground">
                    历史登记指标 · {{ metricsText(c) }}
                  </span>
                  <Button
                    size="small"
                    type="primary"
                    ghost
                    :disabled="gateInvalidated(c)"
                    @click="openPromote(c)"
                  >
                    设为优选登记
                  </Button>
                </div>
              </div>
            </Col>
          </Row>
        </div>

        <div class="text-xs text-muted-foreground">
          自动迭代账本：{{ ver.trials?.total || 0 }} 条 trial ·
          {{ ver.trials?.runs || 0 }} 轮
          <span v-if="ver.trials?.best">
            · 历史最优 t={{ Number(ver.trials.best.t_period).toFixed(2) }} （{{
              ver.trials.best.config?.kind
            }}
            L={{ ver.trials.best.config?.seq_len }}）
          </span>
          <div class="mt-1 text-[11px] text-orange-500">
            ⚠
            自动迭代只生产候选；「设为优选登记」只改版本注册表，不代表实际部署或改变订单
          </div>
        </div>
      </Card>

      <!-- ══════════ 模型自动迭代（第 24/25 轮：自洽判据 + 多种子） ══════════ -->
      <Card v-if="ml?.iteration" :bordered="false" class="mt-3 shadow-sm">
        <template #title>
          <span>🔬 模型自动迭代</span>
          <span class="ml-2 text-xs font-normal text-muted-foreground">
            逐窗口 IC 多种子 + 多窗口宽度稳健性
          </span>
        </template>
        <template #extra>
          <span class="text-xs text-muted-foreground">
            {{ iterProg?.run_id ? `run ${iterProg.run_id}` : '—' }}
          </span>
        </template>

        <!-- 进度 -->
        <div class="mb-3">
          <div class="mb-1 flex flex-wrap items-center gap-2 text-sm">
            <Tag
              :color="iterProg?.status === 'running' ? 'processing' : 'default'"
            >
              {{
                iterProg?.status === 'running'
                  ? '运行中'
                  : iterProg?.status || '空闲'
              }}
            </Tag>
            <span>第 {{ iterProg?.round ?? '—' }} 轮</span>
            <span class="text-muted-foreground">
              trial {{ iterProg?.trial ?? 0 }}/{{ iterProg?.total ?? 0 }}
            </span>
            <span class="text-muted-foreground">{{
              iterProg?.phase || ''
            }}</span>
          </div>
          <Progress
            :percent="Number(iterProg?.percent || 0)"
            :stroke-color="'#1677ff'"
            size="small"
          />
        </div>

        <!-- 判据 -->
        <Alert
          class="mb-3"
          type="info"
          :show-icon="true"
          :message="ml.iteration.criteria"
        >
          <template #description>
            <span class="text-xs">
              旧判据「t &gt; 2 且 正窗口占比 ≥ 80%」数学上不自洽 （n=22 时 80%
              正窗口 ⟺ t ≥ 3.95）；且该指标随窗口宽度从 65% 变到 90%。
              已改为「多窗口宽度下 t 都 &gt; 2」，不可被单一参数操纵。
            </span>
          </template>
        </Alert>

        <!-- 汇总 -->
        <Row :gutter="[12, 12]" class="mb-3">
          <Col :lg="6" :xs="12">
            <div class="rounded-lg border p-3">
              <div class="text-xs text-muted-foreground">trial 总数</div>
              <div class="text-xl font-semibold">
                {{ ml.iteration.n_trials }}
              </div>
            </div>
          </Col>
          <Col :lg="6" :xs="12">
            <div class="rounded-lg border p-3">
              <div class="text-xs text-muted-foreground">通过判据</div>
              <div
                class="text-xl font-semibold"
                :class="
                  ml.iteration.n_passing > 0
                    ? 'text-emerald-600'
                    : 'text-muted-foreground'
                "
              >
                {{ ml.iteration.n_passing }}
              </div>
            </div>
          </Col>
          <Col :lg="6" :xs="12">
            <div class="rounded-lg border p-3">
              <div class="text-xs text-muted-foreground">多种子</div>
              <div class="text-xl font-semibold">
                {{ maxSeeds > 1 ? `${maxSeeds} 个/配置` : '关闭' }}
              </div>
            </div>
          </Col>
          <Col :lg="6" :xs="12">
            <div class="rounded-lg border p-3">
              <div class="text-xs text-muted-foreground">最优 t（季度）</div>
              <div class="text-xl font-semibold">
                {{ bestT == null ? '—' : bestT.toFixed(2) }}
              </div>
            </div>
          </Col>
        </Row>

        <!-- 通过判据的配置 -->
        <div class="mb-1 text-sm font-medium">
          ✅ 通过判据的配置
          <span class="ml-2 text-xs font-normal text-muted-foreground">
            三种窗口宽度下 t 都 &gt; 2 且 FDR q &lt; 0.05
          </span>
        </div>
        <Table
          v-if="(ml.iteration.passing || []).length > 0"
          :columns="passCols"
          :data-source="passRows"
          :pagination="false"
          size="small"
          row-key="key"
        />
        <Empty
          v-else
          :image="EMPTY_SIMPLE"
          description="尚无配置通过 —— 门槛是实质性的"
        />
      </Card>

      <!-- 上线确认弹窗（人工闸门） -->
      <Modal
        v-model:open="promoteOpen"
        :confirm-loading="promoting"
        ok-text="确认设为优选登记"
        title="登记为优选版本（人工确认）"
        @ok="doPromote"
      >
        <div class="text-xs">
          <div>
            目标版本：<span class="font-mono">{{ target?.id }}</span>
          </div>
          <div
            v-if="target && !gatePassed(target)"
            class="mt-2 rounded bg-orange-50 p-2 text-orange-600 dark:bg-orange-950/20"
          >
            ⚠ 该版本<b>{{ gateInvalidated(target) ? '口径已作废' : '未通过冻结判据' }}</b>：
            {{ (target.gate?.reasons || []).join('；') }}
          </div>
          <div
            v-else
            class="mt-2 rounded bg-emerald-50 p-2 text-emerald-700 dark:bg-emerald-950/20"
          >
            ✅ 已通过冻结判据<template v-if="gateCriteria"
              >（{{ gateCriteria }}）</template
            >
          </div>
          <div class="mt-3 mb-1">登记理由（必填，写入版本历史）</div>
          <Input.TextArea
            v-model:value="promoteNote"
            :rows="3"
            placeholder="例：样本外 t=2.4 且 FDR 校正后 q=0.01，人工复核通过"
          />
        </div>
      </Modal>

      <div v-if="!it" class="py-16 text-center text-muted-foreground">
        暂无迭代数据 —— 运行
        <code class="mx-1">python build_iteration.py</code> 生成
      </div>

      <template v-else>
        <Alert
          class="mb-3"
          type="warning"
          show-icon
          message="以下内容是历史研究记录，不能视为实际收益或部署证据"
          description="旧回测与 IC 结果存在已撤回或未验证口径；收益判断以证据账里的实际已平仓记录为准。"
        />
        <Tabs v-model:activeKey="tab" size="small">
          <!-- 顶部结论 -->
          <TabPane key="result" tab="迭代结果">
            <Card :bordered="false" class="shadow-sm">
              <div class="flex flex-wrap items-center gap-3">
                <Tag color="blue" class="!px-3 !py-1 text-sm">核心改进</Tag>
                <span class="text-base font-semibold">{{
                  it.conclusion.key_improvement
                }}</span>
                <span class="text-xs text-muted-foreground">
                  池 {{ it.universe_size }} 币 · 生成 {{ it.generated_at }}
                </span>
              </div>
              <ul
                class="mt-2 list-inside list-disc text-xs text-muted-foreground"
              >
                <li v-for="(e, i) in it.conclusion.evidence" :key="i">
                  {{ e }}
                </li>
              </ul>
            </Card>

            <!-- v1 vs v2 -->
            <Card
              v-if="it.v1_vs_v2"
              :bordered="false"
              class="mt-4 shadow-sm"
              title="v1 → v2 改进对比"
            >
              <Row :gutter="[12, 12]">
                <Col
                  v-for="x in [
                    {
                      k: '版本',
                      v1: it.v1_vs_v2.v1.name,
                      v2: it.v1_vs_v2.v2.name,
                      hi: false,
                    },
                    {
                      k: '年化',
                      v1: it.v1_vs_v2.v1.ann + '%',
                      v2: it.v1_vs_v2.v2.ann + '%',
                      hi: false,
                      c: 'text-red-500',
                    },
                    {
                      k: '最大回撤',
                      v1: it.v1_vs_v2.v1.mdd + '%',
                      v2: it.v1_vs_v2.v2.mdd + '%',
                      hi: true,
                    },
                    {
                      k: 'Calmar',
                      v1: it.v1_vs_v2.v1.calmar,
                      v2: it.v1_vs_v2.v2.calmar,
                      hi: true,
                    },
                    {
                      k: 't 值',
                      v1: it.v1_vs_v2.v1.t,
                      v2: it.v1_vs_v2.v2.t,
                      hi: true,
                    },
                    {
                      k: '年化/波动',
                      v1: it.v1_vs_v2.v1.vol_ret_ratio,
                      v2: it.v1_vs_v2.v2.vol_ret_ratio,
                      hi: true,
                    },
                  ]"
                  :key="x.k"
                  :lg="4"
                  :md="8"
                  :xs="12"
                >
                  <div
                    class="rounded-lg border border-gray-100 p-3 dark:border-gray-800"
                  >
                    <div class="text-xs text-muted-foreground">{{ x.k }}</div>
                    <div
                      class="mt-1 text-xs text-muted-foreground line-through"
                    >
                      {{ x.v1 }}
                    </div>
                    <div
                      class="font-semibold"
                      :class="x.hi ? 'text-emerald-600' : ''"
                    >
                      {{ x.v2 }}
                    </div>
                  </div>
                </Col>
              </Row>
            </Card>

            <div class="mt-4 grid gap-4 lg:grid-cols-2">
              <!-- 变量 B -->
              <Card
                :bordered="false"
                class="shadow-sm"
                title="变量 B · 信号选择（改进所在）"
              >
                <Table
                  :columns="[
                    { dataIndex: 'label', title: '配置' },
                    { key: 'ann', title: '年化', align: 'right' },
                    { key: 'mdd', title: '回撤', align: 'right' },
                    { key: 'calmar', title: 'Calmar', align: 'right' },
                    { key: 't', title: 't值', align: 'right' },
                    { key: 'pos', title: '正收益年', align: 'right' },
                  ]"
                  :data-source="it.selection"
                  :pagination="false"
                  row-key="label"
                  size="small"
                >
                  <template #bodyCell="{ column, record }">
                    <template v-if="column.key === 'ann'">
                      <span class="font-mono text-xs text-red-500"
                        >+{{ record.ann }}%</span
                      >
                    </template>
                    <template v-else-if="column.key === 'mdd'">
                      <span class="font-mono text-xs text-emerald-500"
                        >{{ record.mdd }}%</span
                      >
                    </template>
                    <template v-else-if="column.key === 'calmar'">
                      <span class="font-mono text-xs">{{ record.calmar }}</span>
                    </template>
                    <template v-else-if="column.key === 't'">
                      <span
                        class="font-mono text-xs"
                        :class="
                          Math.abs(record.t) > 2
                            ? 'font-semibold text-blue-500'
                            : ''
                        "
                      >
                        {{ record.t }}
                      </span>
                    </template>
                    <template v-else-if="column.key === 'pos'">
                      {{ record.pos_years }}/{{ record.n_years }}
                    </template>
                  </template>
                </Table>
              </Card>

              <!-- 变量 D -->
              <Card
                :bordered="false"
                class="shadow-sm"
                title="变量 D · 参数稳定性（样本内 vs 样本外）"
              >
                <Table
                  :columns="[
                    { dataIndex: 'params', title: 'entry/exit' },
                    { key: 'in_sharpe', title: '样本内', align: 'right' },
                    { key: 'oos_sharpe', title: '样本外', align: 'right' },
                    { key: 'oos_t', title: '样本外t', align: 'right' },
                    { key: 'oos_mdd', title: '样本外回撤', align: 'right' },
                  ]"
                  :data-source="it.params"
                  :pagination="false"
                  row-key="params"
                  size="small"
                >
                  <template #bodyCell="{ column, record }">
                    <template v-if="column.key === 'in_sharpe'">
                      <span class="font-mono text-xs">{{
                        record.in_sharpe
                      }}</span>
                    </template>
                    <template v-else-if="column.key === 'oos_sharpe'">
                      <span class="font-mono text-xs">{{
                        record.oos_sharpe
                      }}</span>
                    </template>
                    <template v-else-if="column.key === 'oos_t'">
                      <span
                        class="font-mono text-xs"
                        :class="
                          Math.abs(record.oos_t) > 2
                            ? 'font-semibold text-blue-500'
                            : ''
                        "
                      >
                        {{ record.oos_t }}
                      </span>
                    </template>
                    <template v-else-if="column.key === 'oos_mdd'">
                      <span class="font-mono text-xs text-emerald-500"
                        >{{ record.oos_mdd }}%</span
                      >
                    </template>
                  </template>
                </Table>
              </Card>
            </div>

            <div class="mt-4 grid gap-4 lg:grid-cols-2">
              <Card
                :bordered="false"
                class="shadow-sm"
                title="变量 A · 池规模（固定仓位方式）"
              >
                <Table
                  :columns="[
                    { dataIndex: 'label', title: '配置' },
                    { key: 'ann', title: '年化', align: 'right' },
                    { key: 'calmar', title: 'Calmar', align: 'right' },
                    { key: 't', title: 't值', align: 'right' },
                  ]"
                  :data-source="universeStaticRows"
                  :pagination="false"
                  row-key="label"
                  size="small"
                >
                  <template #bodyCell="{ column, record }">
                    <template v-if="column.key === 'ann'">
                      <span class="font-mono text-xs">{{ record.ann }}%</span>
                    </template>
                    <template v-else-if="column.key === 'calmar'">
                      <span class="font-mono text-xs">{{ record.calmar }}</span>
                    </template>
                    <template v-else-if="column.key === 't'">
                      <span class="font-mono text-xs">{{ record.t }}</span>
                    </template>
                  </template>
                </Table>
              </Card>

              <Card
                :bordered="false"
                class="shadow-sm"
                title="变量 C · 仓位分配"
              >
                <Table
                  :columns="[
                    { dataIndex: 'label', title: '方式' },
                    { key: 'ann', title: '年化', align: 'right' },
                    { key: 'mdd', title: '回撤', align: 'right' },
                    { key: 'calmar', title: 'Calmar', align: 'right' },
                  ]"
                  :data-source="it.sizing"
                  :pagination="false"
                  row-key="label"
                  size="small"
                >
                  <template #bodyCell="{ column, record }">
                    <template v-if="column.key === 'ann'">
                      <span class="font-mono text-xs">{{ record.ann }}%</span>
                    </template>
                    <template v-else-if="column.key === 'mdd'">
                      <span class="font-mono text-xs text-emerald-500"
                        >{{ record.mdd }}%</span
                      >
                    </template>
                    <template v-else-if="column.key === 'calmar'">
                      <span class="font-mono text-xs">{{ record.calmar }}</span>
                    </template>
                  </template>
                </Table>
              </Card>
            </div>
          </TabPane>

          <!-- 参数优化（走查 96 组网格） -->
          <TabPane key="param" tab="参数与风险">
            <Card
              v-if="it.optimization"
              :bordered="false"
              class="mt-4 shadow-sm"
              title="🎯 参数优化（走查纪律）"
            >
              <template #extra>
                <span class="text-xs text-muted-foreground">
                  {{ it.optimization.combos }} 组网格 · 样本内
                  {{ it.optimization.ins }} · 样本外 {{ it.optimization.oos }}
                </span>
              </template>

              <Row :gutter="[12, 12]">
                <Col :lg="6" :xs="12">
                  <div
                    class="rounded-lg border border-gray-100 p-3 dark:border-gray-800"
                  >
                    <div class="text-xs text-muted-foreground">
                      样本内最优的样本外排名
                    </div>
                    <div class="mt-1 text-lg font-semibold text-orange-500">
                      {{ it.optimization.in_rank_of_best_in_oos }} /
                      {{ it.optimization.combos }}
                    </div>
                    <div class="text-[11px] text-muted-foreground">
                      → 参数不稳定，样本内最优不可外推
                    </div>
                  </div>
                </Col>
                <Col :lg="6" :xs="12">
                  <div
                    class="rounded-lg border border-gray-100 p-3 dark:border-gray-800"
                  >
                    <div class="text-xs text-muted-foreground">
                      样本外 Calmar &gt; 0.5 占比
                    </div>
                    <div class="mt-1 text-lg font-semibold text-emerald-600">
                      {{ it.optimization.oos_summary.pct_calmar_gt05 }}%
                    </div>
                    <div class="text-[11px] text-muted-foreground">
                      → 参数面是宽平台，不是尖峰
                    </div>
                  </div>
                </Col>
                <Col :lg="6" :xs="12">
                  <div
                    class="rounded-lg border border-gray-100 p-3 dark:border-gray-800"
                  >
                    <div class="text-xs text-muted-foreground">
                      样本外 t &gt; 2 占比
                    </div>
                    <div class="mt-1 text-lg font-semibold">
                      {{ it.optimization.oos_summary.pct_t_gt2 }}%
                    </div>
                    <div class="text-[11px] text-muted-foreground">
                      中位 Calmar
                      {{ it.optimization.oos_summary.calmar_median }}
                    </div>
                  </div>
                </Col>
                <Col :lg="6" :xs="12">
                  <div
                    class="rounded-lg border border-gray-100 p-3 dark:border-gray-800"
                  >
                    <div class="text-xs text-muted-foreground">结论</div>
                    <div
                      class="mt-1 font-semibold"
                      :class="topNIsBest ? 'text-blue-500' : 'text-orange-500'"
                    >
                      {{ topNIsBest ? '保持现状' : '需复核' }}
                    </div>
                    <div class="text-[11px] text-muted-foreground">
                      {{ currentCombo || '—' }} 在平台中央
                    </div>
                  </div>
                </Col>
              </Row>

              <div class="mt-4 grid gap-4 lg:grid-cols-2">
                <div>
                  <div class="mb-2 text-sm font-medium">
                    top_n 边际影响（样本外 Calmar 中位数）
                  </div>
                  <Table
                    :columns="[
                      { dataIndex: 'k', title: 'top_n' },
                      { key: 'v', title: '样本外 Calmar', align: 'right' },
                      { key: 'mark', title: '', width: 70 },
                    ]"
                    :data-source="
                      Object.entries(it.optimization.by_top_n).map(
                        ([k, v]) => ({ k, v }),
                      )
                    "
                    :pagination="false"
                    row-key="k"
                    size="small"
                  >
                    <template #bodyCell="{ column, record }">
                      <template v-if="column.key === 'v'">
                        <span class="font-mono text-xs font-semibold">{{
                          record.v
                        }}</span>
                      </template>
                      <template v-else-if="column.key === 'mark'">
                        <Tag v-if="record.k === currentTopN" color="green"
                          >当前</Tag
                        >
                      </template>
                    </template>
                  </Table>
                  <div
                    v-if="bestTopN"
                    class="mt-2 text-xs text-muted-foreground"
                  >
                    <template v-if="topNIsBest">
                      <b>top_n={{ bestTopN.key }} 是这些取值里最优</b>（样本外
                      Calmar {{ bestTopN.value }}）—— 当前配置选对了。
                    </template>
                    <template v-else>
                      <b>top_n={{ bestTopN.key }} 样本外最优</b>（Calmar
                      {{ bestTopN.value }}）， 当前实盘用 top_n={{
                        currentTopN || '—'
                      }}
                      —— 需人工复核是否调整。
                    </template>
                  </div>
                </div>

                <div>
                  <div class="mb-2 text-sm font-medium">
                    样本外表现最好的 6 组
                  </div>
                  <Table
                    :columns="[
                      { key: 'cfg', title: 'entry/exit/top' },
                      {
                        key: 'oos_calmar',
                        title: '样本外Calmar',
                        align: 'right',
                      },
                      { key: 'oos_t', title: 't值', align: 'right' },
                      { key: 'oos_mdd', title: '回撤', align: 'right' },
                    ]"
                    :data-source="it.optimization.top_oos"
                    :pagination="false"
                    row-key="cfg"
                    size="small"
                  >
                    <template #bodyCell="{ column, record }">
                      <template v-if="column.key === 'cfg'">
                        <span class="font-mono text-xs">
                          {{ record.entry }}/{{ record.exit }}/{{
                            record.top_n
                          }}
                        </span>
                      </template>
                      <template v-else-if="column.key === 'oos_calmar'">
                        <span class="font-mono text-xs">{{
                          record.oos_calmar
                        }}</span>
                      </template>
                      <template v-else-if="column.key === 'oos_t'">
                        <span class="font-mono text-xs">{{
                          record.oos_t
                        }}</span>
                      </template>
                      <template v-else-if="column.key === 'oos_mdd'">
                        <span class="font-mono text-xs text-emerald-500"
                          >{{ record.oos_mdd }}%</span
                        >
                      </template>
                    </template>
                  </Table>
                  <div class="mt-2 text-xs text-orange-500">
                    ⚠ 按「样本外最好」挑参数本身就是另一种选择偏差 ——
                    不建议据此改动
                  </div>
                </div>
              </div>
            </Card>

            <!-- 幸存者偏差 -->
            <Card
              v-if="it.universe"
              :bordered="false"
              class="mt-4 shadow-sm"
              title="⚠️ 幸存者偏差量化"
            >
              <div
                class="mb-3 rounded-lg bg-orange-50/60 p-3 text-xs dark:bg-orange-950/20"
              >
                <div class="font-medium text-orange-600">问题</div>
                <div class="mt-1 text-muted-foreground">
                  实盘的 {{ live?.pairs ?? '—' }} 币名单是用<b
                    >今天的知识人工挑的</b
                  >
                  —— 其中部分币在回测起始年份尚未上市，用它回测早年 =
                  严重幸存者偏差。 下表用「无偏同期名单」量化了这个偏差。
                </div>
              </div>

              <div class="grid gap-3 lg:grid-cols-3">
                <div
                  class="rounded-lg border border-gray-100 p-3 dark:border-gray-800"
                >
                  <div class="text-xs text-muted-foreground">
                    静态 20（当前名单·有偏差）
                  </div>
                  <div
                    class="mt-1 font-mono text-lg font-semibold text-orange-500"
                  >
                    Calmar
                    {{ it.universe.universe_comparison.static20_ins.calmar }}
                  </div>
                  <div class="text-[11px] text-muted-foreground">
                    年化 {{ it.universe.universe_comparison.static20_ins.ann }}%
                    ·
                    {{
                      it.universe.universe_comparison.static20_ins.pos_years
                    }}/{{
                      it.universe.universe_comparison.static20_ins.n_years
                    }}
                    正年
                  </div>
                </div>
                <div
                  class="rounded-lg border border-gray-100 p-3 dark:border-gray-800"
                >
                  <div class="text-xs text-muted-foreground">
                    静态 21（无偏差·同期）
                  </div>
                  <div
                    class="mt-1 font-mono text-lg font-semibold text-emerald-600"
                  >
                    Calmar
                    {{ it.universe.universe_comparison.static21_ins.calmar }}
                  </div>
                  <div class="text-[11px] text-muted-foreground">
                    年化 {{ it.universe.universe_comparison.static21_ins.ann }}%
                    ·
                    {{
                      it.universe.universe_comparison.static21_ins.pos_years
                    }}/{{
                      it.universe.universe_comparison.static21_ins.n_years
                    }}
                    正年
                  </div>
                </div>
                <div
                  class="rounded-lg border border-orange-200 bg-orange-50/50 p-3 dark:border-orange-900 dark:bg-orange-950/20"
                >
                  <div class="text-xs text-muted-foreground">偏差</div>
                  <template v-if="universeBias">
                    <div class="mt-1 text-lg font-semibold text-orange-600">
                      Calmar ×{{ universeBias.ratio.toFixed(2) }}
                    </div>
                    <div class="text-[11px] text-muted-foreground">
                      {{ universeBias.biased }} vs {{ universeBias.unbiased }} ·
                      虚高 {{ universeBias.inflatedPct.toFixed(0) }}%
                    </div>
                  </template>
                  <div v-else class="mt-1 text-[11px] text-muted-foreground">
                    —
                  </div>
                </div>
              </div>

              <div class="mt-3 text-xs text-muted-foreground">
                <div class="mb-1 font-medium">
                  动态时点选池（无前视）对比 —— 无显著优势，故未采纳
                </div>
                <div class="flex flex-wrap gap-2">
                  <Tag
                    v-for="x in it.universe.universe_comparison.pit"
                    :key="x.n"
                  >
                    前 {{ x.n }} 名 · Calmar {{ x.calmar }}
                  </Tag>
                </div>
                <div v-if="pitRows.length" class="mt-1 text-orange-500">
                  ⚠ 时点选池 Calmar 序列 {{ pitSeqText }}
                  <template v-if="pitMonotonic === false">
                    （非单调）= 噪声，「最优」只是格子里的最大值
                  </template>
                  <template v-else-if="pitMonotonic === true">
                    （单调递增）—— 池越大越好，但仍需样本外确认
                  </template>
                </div>
              </div>
            </Card>

            <!-- 风险收益曲线 -->
            <Card
              v-if="it.risk_curve"
              :bordered="false"
              class="mt-4 shadow-sm"
              title="📈 风险收益曲线 —— 目标年化的真实代价"
            >
              <template #extra>
                <span class="text-xs text-muted-foreground">
                  样本外 {{ opt?.oos ?? '—' }} · 收益率随敞口近似线性放大
                </span>
              </template>

              <div
                v-for="(rows, name) in it.risk_curve.oos"
                :key="name"
                class="mb-4"
              >
                <div class="mb-1 text-sm font-medium">{{ name }}</div>
                <Table
                  :columns="[
                    { key: 'exposure', title: '敞口', width: 80 },
                    { key: 'ann', title: '年化', align: 'right' },
                    { key: 'mdd', title: '最大回撤', align: 'right' },
                    { key: 'calmar', title: 'Calmar', align: 'right' },
                    { key: 't', title: 't值', align: 'right' },
                    { key: 'feas', title: '可行性' },
                  ]"
                  :data-source="(rows || []).filter(Boolean)"
                  :pagination="false"
                  row-key="exposure"
                  size="small"
                >
                  <template #bodyCell="{ column, record }">
                    <template v-if="column.key === 'exposure'">
                      <span class="font-mono text-xs"
                        >{{ (record.exposure * 100).toFixed(0) }}%</span
                      >
                    </template>
                    <template v-else-if="column.key === 'ann'">
                      <span class="font-mono text-xs text-red-500"
                        >+{{ record.ann }}%</span
                      >
                    </template>
                    <template v-else-if="column.key === 'mdd'">
                      <span
                        class="font-mono text-xs"
                        :class="
                          record.mdd < -30 ? 'text-red-500' : 'text-emerald-500'
                        "
                      >
                        {{ record.mdd }}%
                      </span>
                    </template>
                    <template v-else-if="column.key === 'calmar'">
                      <span class="font-mono text-xs">{{ record.calmar }}</span>
                    </template>
                    <template v-else-if="column.key === 't'">
                      <span
                        class="font-mono text-xs"
                        :class="
                          Math.abs(record.t) > 2
                            ? 'font-semibold text-blue-500'
                            : ''
                        "
                      >
                        {{ record.t }}
                      </span>
                    </template>
                    <template v-else-if="column.key === 'feas'">
                      <Tag v-if="record.ruin" color="red">爆仓</Tag>
                      <Tag v-else-if="record.mdd < -50" color="orange"
                        >极难承受</Tag
                      >
                      <Tag v-else-if="record.mdd < -30" color="gold">痛苦</Tag>
                      <Tag v-else color="green">可承受</Tag>
                    </template>
                  </template>
                </Table>
              </div>

              <div
                class="rounded-lg bg-blue-50/60 p-3 text-xs dark:bg-blue-950/20"
              >
                <div class="font-medium text-blue-600">⭐ 核心结论</div>
                <ul
                  class="mt-1 list-inside list-disc space-y-0.5 text-muted-foreground"
                >
                  <li v-if="currentCalmarRange">
                    <b>
                      Calmar 在所有敞口档位都稳定在
                      {{ currentCalmarRange.min }}~{{ currentCalmarRange.max }}
                    </b>
                    —— 收益随敞口近似线性放大，加杠杆<b>不会</b>改善风险调整收益
                  </li>
                  <li v-if="exposureForTarget">
                    用当前名单：要 30% 年化需
                    <b
                      >{{ (exposureForTarget.exposure * 100).toFixed(0) }}%
                      敞口</b
                    >
                    → 最大回撤 <b>{{ exposureForTarget.mdd }}%</b>
                  </li>
                  <li v-if="unbiasedBest">
                    用无偏选池：即便加到
                    <b>{{ (unbiasedBest.exposure * 100).toFixed(0) }}% 敞口</b>
                    也只有 {{ unbiasedBest.ann }}%（回撤
                    {{ unbiasedBest.mdd }}%）
                  </li>
                  <li v-if="suggestedTarget">
                    <b>
                      建议目标 {{ suggestedTarget.min.toFixed(0) }}~{{
                        suggestedTarget.max.toFixed(0)
                      }}%
                    </b>
                    （{{ (suggestedTarget.exposures[0] * 100).toFixed(0) }}~{{
                      (suggestedTarget.exposures[1] * 100).toFixed(0)
                    }}% 敞口，回撤控制在 {{ suggestedTarget.maxMdd }}% 以内）
                  </li>
                </ul>
              </div>
            </Card>
          </TabPane>

          <!-- 深度学习迭代 -->
          <TabPane key="dl" tab="深度学习">
            <Card
              v-if="ml"
              :bordered="false"
              class="mt-4 shadow-sm"
              title="🤖 深度学习迭代（元标记）"
            >
              <template #extra>
                <span class="text-xs text-muted-foreground">
                  第 {{ ml.rounds }} 轮 · {{ ml.experiments }} 条实验记录
                </span>
              </template>

              <div
                class="mb-3 rounded-lg bg-blue-50/60 p-3 text-xs dark:bg-blue-950/20"
              >
                <div class="font-medium text-blue-600">框架</div>
                <div class="mt-1 text-muted-foreground">
                  {{ ml.conclusion.framing }} —— 不让 ML 预测方向，让 ML
                  判断「这笔该不该做」。
                  <br />
                  早前
                  FreqAI「直接预测收益」的路线已被证伪（见「策略研究」页模型对比），
                  <b>那是框架错了，不是深度学习不行</b>。
                </div>
              </div>

              <Row :gutter="[12, 12]">
                <Col :lg="6" :xs="12">
                  <div
                    class="rounded-lg border border-gray-100 p-3 dark:border-gray-800"
                  >
                    <div class="text-xs text-muted-foreground">
                      走查 IC 的 t 值
                    </div>
                    <div class="mt-1 text-lg font-semibold text-orange-500">
                      {{ ml.conclusion.walkforward_ic_t }}
                    </div>
                    <div
                      class="text-[11px]"
                      :class="
                        wfSignificant
                          ? 'text-emerald-600'
                          : 'text-muted-foreground'
                      "
                    >
                      需 &gt; 2 才算稳健（{{
                        wfSignificant ? '当前已显著' : '当前不显著'
                      }}）
                    </div>
                  </div>
                </Col>
                <Col :lg="6" :xs="12">
                  <div
                    class="rounded-lg border border-gray-100 p-3 dark:border-gray-800"
                  >
                    <div class="text-xs text-muted-foreground">
                      上帝视角 IC（上界）
                    </div>
                    <div class="mt-1 text-lg font-semibold">
                      {{ ml.oracle_ic }}
                    </div>
                    <div class="text-[11px] text-muted-foreground">
                      完美预测能达到的水平
                    </div>
                  </div>
                </Col>
                <Col :lg="6" :xs="12">
                  <div
                    class="rounded-lg border border-gray-100 p-3 dark:border-gray-800"
                  >
                    <div class="text-xs text-muted-foreground">
                      状态组（已做多重比较校正）
                    </div>
                    <div class="mt-1 text-lg font-semibold">
                      {{ ml.regime_groups.length }}
                    </div>
                    <div class="text-[11px] text-muted-foreground">
                      {{ regimePassed }} /
                      {{ regimeTotal }} 组通过基率中性化检验（|t| &gt; 2）
                    </div>
                  </div>
                </Col>
                <Col :lg="6" :xs="12">
                  <div
                    class="rounded-lg border border-orange-200 bg-orange-50/50 p-3 dark:border-orange-900 dark:bg-orange-950/20"
                  >
                    <div class="text-xs text-muted-foreground">当前判定</div>
                    <div class="mt-1 text-sm font-semibold text-orange-600">
                      {{ ml.conclusion.verdict }}
                    </div>
                    <div class="text-[11px] text-muted-foreground">
                      ✅ 远未判定不可行 —— 才第 {{ ml.rounds ?? '—' }} 轮
                    </div>
                  </div>
                </Col>
              </Row>

              <!-- 序列模型 -->
              <div
                v-if="ml.seq && Object.keys(ml.seq).length"
                class="mb-3 mt-3"
              >
                <div class="mb-1 text-xs font-medium">
                  🧠 序列模型（LSTM/Transformer 直接吃 K 线序列）
                </div>
                <Row :gutter="[8, 8]">
                  <Col v-for="(v, k) in ml.seq" :key="k" :lg="6" :xs="12">
                    <div
                      class="rounded-lg border p-2.5"
                      :class="
                        v.t_period > 2
                          ? 'border-emerald-200 dark:border-emerald-900'
                          : 'border-gray-100 dark:border-gray-800'
                      "
                    >
                      <div class="text-xs font-medium">
                        {{ String(k).toUpperCase() }}
                      </div>
                      <div
                        class="mt-0.5 font-mono text-sm font-semibold"
                        :class="v.t_period > 2 ? 'text-emerald-600' : ''"
                      >
                        IC {{ v.ic_period.toFixed(4) }}
                      </div>
                      <div class="text-[11px] text-muted-foreground">
                        t={{ v.t_period.toFixed(2) }} · 正窗口
                        {{ v.pos_windows }}/{{ v.n_windows }}
                      </div>
                    </div>
                  </Col>
                </Row>
                <div
                  v-if="bestSeq"
                  class="mt-1.5 text-[11px]"
                  :class="
                    (bestSeq.t_period ?? 0) > 2
                      ? 'text-emerald-600'
                      : 'text-muted-foreground'
                  "
                >
                  ⭐ 逐窗口（非池化）口径下 t 最高的序列模型：{{
                    String(bestSeq.name).toUpperCase()
                  }}
                  t={{ Number(bestSeq.t_period).toFixed(2) }} · 正窗口
                  {{ bestSeq.pos_windows }}/{{ bestSeq.n_windows }}
                </div>
              </div>

              <div class="mt-3">
                <div class="mb-1 text-xs font-medium">
                  逐窗口 IC（走查，非池化）
                </div>
                <Table
                  v-if="ml.ic_models?.length"
                  :columns="[
                    { dataIndex: 'model', title: '模型' },
                    { key: 'ic_tr', title: '训练IC', align: 'right' },
                    { key: 'ic_te', title: '测试IC', align: 'right' },
                    { key: 't_te', title: 't值', align: 'right' },
                  ]"
                  :data-source="ml.ic_models"
                  :pagination="false"
                  row-key="model"
                  size="small"
                >
                  <template #bodyCell="{ column, record }">
                    <template v-if="column.key === 'ic_tr'">
                      <span class="font-mono text-xs">{{
                        record.ic_tr?.toFixed(4)
                      }}</span>
                    </template>
                    <template v-else-if="column.key === 'ic_te'">
                      <span class="font-mono text-xs">{{
                        record.ic_te?.toFixed(4)
                      }}</span>
                    </template>
                    <template v-else-if="column.key === 't_te'">
                      <span
                        class="font-mono text-xs"
                        :class="
                          Math.abs(record.t_te) > 2
                            ? 'font-semibold text-blue-500'
                            : ''
                        "
                      >
                        {{ record.t_te?.toFixed(2) }}
                      </span>
                    </template>
                  </template>
                </Table>
                <div
                  v-else
                  class="py-3 text-center text-xs text-muted-foreground"
                >
                  暂无 IC 记录
                </div>
              </div>

              <div
                class="mt-3 rounded-lg bg-orange-50/60 p-3 text-xs dark:bg-orange-950/20"
              >
                <div class="font-medium text-orange-600">
                  迭代纪律（每轮必守）
                </div>
                <ul
                  class="mt-1 list-inside list-disc space-y-0.5 text-muted-foreground"
                >
                  <li v-for="(r, i) in ml.conclusion.rules" :key="i">
                    {{ r }}
                  </li>
                </ul>
              </div>
            </Card>
          </TabPane>

          <!-- 判据 -->
          <TabPane key="ref" tab="参考">
            <Card
              :bordered="false"
              class="mt-4 shadow-sm"
              title="判据与注意事项"
            >
              <div class="text-xs text-muted-foreground">
                <div class="font-medium text-orange-600">
                  为什么用 Calmar / 年化波动比，而不是 Sharpe
                </div>
                <ul class="mt-1 list-inside list-disc space-y-0.5">
                  <li v-for="(c, i) in it.conclusion.caveats" :key="i">
                    {{ c }}
                  </li>
                </ul>
              </div>
            </Card>

            <!-- 当前实盘配置 -->
            <Card
              v-if="it.live"
              :bordered="false"
              class="mt-4 shadow-sm"
              title="当前实盘配置"
            >
              <div class="grid grid-cols-2 gap-3 lg:grid-cols-5">
                <div
                  v-for="(v, k) in {
                    策略: it.live.strategy,
                    周期: it.live.timeframe,
                    币对: it.live.pairs + ' 个',
                    干跑: it.live.dry_run ? '是' : '否',
                    top_n: it.live.params.top_n,
                    目标敞口: it.live.params.target_exposure,
                    'entry/exit':
                      it.live.params.enter_period +
                      '/' +
                      it.live.params.exit_period,
                    stoploss: it.live.params.stoploss,
                  }"
                  :key="k"
                  class="rounded-lg border border-gray-100 p-3 dark:border-gray-800"
                >
                  <div class="text-xs text-muted-foreground">{{ k }}</div>
                  <div class="font-mono text-sm font-semibold">{{ v }}</div>
                </div>
              </div>
            </Card>
          </TabPane>
        </Tabs>
      </template>
    </Spin>
  </div>
</template>

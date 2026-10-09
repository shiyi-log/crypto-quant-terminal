<script lang="ts" setup>
import type { EchartsUIType } from '@vben/plugins/echarts';

import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue';

import { EchartsUI, useEcharts } from '@vben/plugins/echarts';

import {
  Alert,
  Button,
  Card,
  Divider,
  Drawer,
  InputNumber,
  Spin,
  Switch,
  Tag,
} from 'ant-design-vue';

import { getLiveKlines, getLocalOhlcv, getPairCandles } from '#/api/freqtrade';

import { withMarketSlot } from './requestQueue';

const props = defineProps<{ pair: string; timeframe: string }>();

/* ------------------------------------------------------------------ *
 * 技术指标配置：本地计算 + localStorage 持久化
 * ------------------------------------------------------------------ */

/** 指标配置在 localStorage 里的键名 */
const STORAGE_KEY = 'quant_market_indicators';
/** MA/EMA 最多可配置的周期条数 */
const MAX_PERIODS = 5;
const MIN_PERIOD = 1;
const MAX_PERIOD = 500;

type PeriodKind = 'ema' | 'ma';
type FlagKey = 'boll' | 'ema' | 'ma' | 'macd' | 'rsi' | 'volume';
type NumKey =
  | 'bollK'
  | 'bollPeriod'
  | 'macdFast'
  | 'macdSignal'
  | 'macdSlow'
  | 'rsiPeriod';

interface IndicatorConfig {
  overlays: {
    boll: { enabled: boolean; k: number; period: number };
    ema: { enabled: boolean; periods: number[] };
    ma: { enabled: boolean; periods: number[] };
  };
  panels: {
    macd: { enabled: boolean; fast: number; signal: number; slow: number };
    rsi: { enabled: boolean; period: number };
    volume: boolean;
  };
}

/** 默认配置：主图 MA20/MA60（与改造前写死的两条一致），副图只开成交量 */
function defaultConfig(): IndicatorConfig {
  return {
    overlays: {
      ma: { enabled: true, periods: [20, 60] },
      ema: { enabled: false, periods: [50] },
      boll: { enabled: false, period: 20, k: 2 },
    },
    panels: {
      volume: true,
      rsi: { enabled: false, period: 14 },
      macd: { enabled: false, fast: 12, slow: 26, signal: 9 },
    },
  };
}

/** 取整并夹到 [min, max]：空值/非数字一律用 fallback（不抛异常） */
function num(v: any, fallback: number, min: number, max: number): number {
  if (v === null || v === undefined || v === '') return fallback;
  const n = Math.round(Number(v));
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

/** 小数版本（BOLL 倍数用），保留两位小数 */
function numFloat(v: any, fallback: number, min: number, max: number): number {
  if (v === null || v === undefined || v === '') return fallback;
  const n = Number(v);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, Number(n.toFixed(2))));
}

/** 布尔兜底：只有真正的 boolean 才采纳，其它一律用 fallback */
function bool(v: any, fallback: boolean): boolean {
  return typeof v === 'boolean' ? v : fallback;
}

/** 周期数组校验：丢弃非法值、去重、截断到 MAX_PERIODS；整体非法时退回默认 */
function sanitizePeriods(v: any, fallback: number[]): number[] {
  if (!Array.isArray(v)) return [...fallback];
  // 用户主动清空是合法状态（不画线），保留空数组
  if (v.length === 0) return [];
  const out: number[] = [];
  for (const item of v) {
    const n = Math.round(Number(item));
    if (!Number.isFinite(n) || n < MIN_PERIOD || n > MAX_PERIOD) continue;
    if (out.includes(n)) continue;
    out.push(n);
    if (out.length >= MAX_PERIODS) break;
  }
  return out.length > 0 ? out : [...fallback];
}

/**
 * 读回指标配置：JSON 解析失败、字段缺失、类型不对、值越界全部退回默认值，
 * 任何情况下都不抛异常（localStorage 不可用时也走默认值）。
 */
function loadConfig(): IndicatorConfig {
  const def = defaultConfig();
  try {
    if (typeof localStorage === 'undefined') return def;
    const txt = localStorage.getItem(STORAGE_KEY);
    if (!txt) return def;
    const raw: any = JSON.parse(txt);
    if (!raw || typeof raw !== 'object') return def;
    const ov: any = raw.overlays ?? {};
    const pn: any = raw.panels ?? {};
    return {
      overlays: {
        ma: {
          enabled: bool(ov?.ma?.enabled, def.overlays.ma.enabled),
          periods: sanitizePeriods(ov?.ma?.periods, def.overlays.ma.periods),
        },
        ema: {
          enabled: bool(ov?.ema?.enabled, def.overlays.ema.enabled),
          periods: sanitizePeriods(ov?.ema?.periods, def.overlays.ema.periods),
        },
        boll: {
          enabled: bool(ov?.boll?.enabled, def.overlays.boll.enabled),
          period: num(
            ov?.boll?.period,
            def.overlays.boll.period,
            2,
            MAX_PERIOD,
          ),
          k: numFloat(ov?.boll?.k, def.overlays.boll.k, 0.1, 10),
        },
      },
      panels: {
        volume: bool(pn?.volume, def.panels.volume),
        rsi: {
          enabled: bool(pn?.rsi?.enabled, def.panels.rsi.enabled),
          period: num(pn?.rsi?.period, def.panels.rsi.period, 2, MAX_PERIOD),
        },
        macd: {
          enabled: bool(pn?.macd?.enabled, def.panels.macd.enabled),
          fast: num(pn?.macd?.fast, def.panels.macd.fast, 1, MAX_PERIOD),
          slow: num(pn?.macd?.slow, def.panels.macd.slow, 1, MAX_PERIOD),
          signal: num(pn?.macd?.signal, def.panels.macd.signal, 1, MAX_PERIOD),
        },
      },
    };
  } catch {
    return def;
  }
}

/** 写入失败（隐私模式、配额不足）时静默忽略，不影响功能 */
function saveConfig() {
  try {
    if (typeof localStorage === 'undefined') return;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg.value));
  } catch {
    /* 忽略写入失败 */
  }
}

const cfg = ref<IndicatorConfig>(loadConfig());
const cfgOpen = ref(false);

type PanelKey = 'macd' | 'rsi' | 'volume';

/** MA / EMA 配色：MA 保持原来的金色 + 蓝色，后面的按顺序循环 */
const MA_COLORS = ['#f0b90b', '#3b82f6', '#a855f7', '#ec4899', '#14b8a6'];
const EMA_COLORS = ['#06b6d4', '#84cc16', '#f97316', '#d946ef', '#0ea5e9'];
/** BOLL 上下轨同色，中轨深一点并虚线，便于区分 */
const BOLL_COLORS = { lower: '#94a3b8', mid: '#64748b', upper: '#94a3b8' };

/** 图表几何（px）：主图沿用原来 62% × 520 ≈ 322 的观感，副图各自固定高度 */
const TOP_PX = 34;
const MAIN_PX = 240;
const GAP_PX = 26;
/** 底部 dataZoom 滑块 + 留白 */
const BOTTOM_PX = 46;
const PANEL_PX: Record<string, number> = { macd: 120, rsi: 100, volume: 84 };

/** 当前开启的副图，按 成交量 → RSI → MACD 从上到下排列 */
const panelKeys = computed<PanelKey[]>(() => {
  const c = cfg.value;
  const list: PanelKey[] = [];
  if (c.panels.volume) list.push('volume');
  if (c.panels.rsi.enabled) list.push('rsi');
  if (c.panels.macd.enabled) list.push('macd');
  return list;
});

/** 图表总高度随副图数量变化，主图高度保持稳定 */
const chartHeight = computed(() => {
  const extra = panelKeys.value.reduce(
    (sum, k) => sum + (PANEL_PX[k] ?? 100) + GAP_PX,
    0,
  );
  return TOP_PX + MAIN_PX + extra + BOTTOM_PX;
});

/** 右上角按钮上的启用数量（主图叠加按组计数，副图逐个计数） */
const activeCount = computed(() => {
  const c = cfg.value;
  let n = 0;
  if (c.overlays.ma.enabled) n += 1;
  if (c.overlays.ema.enabled) n += 1;
  if (c.overlays.boll.enabled) n += 1;
  if (c.panels.volume) n += 1;
  if (c.panels.rsi.enabled) n += 1;
  if (c.panels.macd.enabled) n += 1;
  return n;
});

/** MA/EMA 的周期列表引用（两者共用同一套增删改逻辑） */
function periodList(kind: PeriodKind): number[] {
  return kind === 'ma'
    ? cfg.value.overlays.ma.periods
    : cfg.value.overlays.ema.periods;
}

const PRESET_PERIODS = [5, 10, 20, 30, 60, 120, 250];

/** 新增一条周期：优先挑常用周期，都被占用时取最小的未占用值 */
function addPeriod(kind: PeriodKind) {
  const list = periodList(kind);
  if (list.length >= MAX_PERIODS) return;
  let next = PRESET_PERIODS.find((v) => !list.includes(v));
  if (next === undefined) {
    for (let v = MIN_PERIOD; v <= MAX_PERIOD; v += 1) {
      if (!list.includes(v)) {
        next = v;
        break;
      }
    }
  }
  if (next !== undefined) list.push(next);
}

/** 修改周期：越界夹到 1–500，重复值去重（保留刚输入的这一条） */
function setPeriod(kind: PeriodKind, idx: number, raw: any) {
  if (raw === null || raw === undefined || raw === '') return; // 清空输入框 → 保持原值
  const n = Math.round(Number(raw));
  if (!Number.isFinite(n)) return;
  const val = Math.min(MAX_PERIOD, Math.max(MIN_PERIOD, n));
  const list = periodList(kind);
  const next: number[] = [];
  list.forEach((v, i) => {
    if (i === idx) {
      if (!next.includes(val)) next.push(val);
      return;
    }
    if (v !== val && !next.includes(v)) next.push(v);
  });
  if (!next.includes(val)) next.splice(Math.min(idx, next.length), 0, val);
  list.splice(0, list.length, ...next);
}

function removePeriod(kind: PeriodKind, idx: number) {
  periodList(kind).splice(idx, 1);
}

/** Switch 的 update:checked 值类型是 string | number | boolean，这里统一转成布尔 */
function setFlag(key: FlagKey, raw: any) {
  const v = raw === true || raw === 1 || raw === '1' || raw === 'true';
  const c = cfg.value;
  switch (key) {
    case 'boll': {
      c.overlays.boll.enabled = v;
      break;
    }
    case 'ema': {
      c.overlays.ema.enabled = v;
      break;
    }
    case 'ma': {
      c.overlays.ma.enabled = v;
      break;
    }
    case 'macd': {
      c.panels.macd.enabled = v;
      break;
    }
    case 'rsi': {
      c.panels.rsi.enabled = v;
      break;
    }
    case 'volume': {
      c.panels.volume = v;
      break;
    }
  }
}

/** 单值指标参数（BOLL 周期/倍数、RSI 周期、MACD 三参数）：非法输入保持原值 */
function setNum(key: NumKey, raw: any) {
  const c = cfg.value;
  switch (key) {
    case 'bollK': {
      c.overlays.boll.k = numFloat(raw, c.overlays.boll.k, 0.1, 10);
      break;
    }
    case 'bollPeriod': {
      c.overlays.boll.period = num(raw, c.overlays.boll.period, 2, MAX_PERIOD);
      break;
    }
    case 'macdFast': {
      c.panels.macd.fast = num(raw, c.panels.macd.fast, 1, MAX_PERIOD);
      break;
    }
    case 'macdSignal': {
      c.panels.macd.signal = num(raw, c.panels.macd.signal, 1, MAX_PERIOD);
      break;
    }
    case 'macdSlow': {
      c.panels.macd.slow = num(raw, c.panels.macd.slow, 1, MAX_PERIOD);
      break;
    }
    case 'rsiPeriod': {
      c.panels.rsi.period = num(raw, c.panels.rsi.period, 2, MAX_PERIOD);
      break;
    }
  }
}

function resetCfg() {
  cfg.value = defaultConfig();
}

/* ------------------------------------------------------------------ *
 * 指标计算：全部本地实现，不引第三方库
 * 约定：前导数据不足 / 窗口内出现无效值的位置一律输出 null，
 *       ECharts 会把 null 当断点，不会在图左侧画出假的直线。
 * ------------------------------------------------------------------ */

type Series = (null | number)[];

/** 数值兜底：null / undefined / 空串 / NaN / 非数字字符串一律返回 null（注意不能直接 Number()，Number(null) === 0） */
function toNum(v: any): null | number {
  if (v === null || v === undefined || v === '') return null;
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function round6(v: number): number {
  return Number(v.toFixed(6));
}

function emptySeries(len: number): Series {
  return new Array<null | number>(Math.max(0, len)).fill(null);
}

/** 简单移动平均：窗口内只要有一个无效值，该点留 null（不填 0） */
function sma(values: any[], n: number): Series {
  const len = Array.isArray(values) ? values.length : 0;
  const p = Math.floor(Number(n));
  const out = emptySeries(len);
  if (len === 0 || !Number.isFinite(p) || p < 1) return out;
  let sum = 0;
  let bad = 0; // 当前窗口内无效值的个数
  for (let i = 0; i < len; i += 1) {
    const cur = toNum(values[i]);
    if (cur === null) bad += 1;
    else sum += cur;
    if (i >= p) {
      const drop = toNum(values[i - p]);
      if (drop === null) bad -= 1;
      else sum -= drop;
    }
    if (i >= p - 1 && bad === 0) out[i] = round6(sum / p);
  }
  return out;
}

/**
 * 指数移动平均：alpha = 2 / (n + 1)。
 * 种子用「前 n 个有效值的简单平均」，因此 i < n - 1 的位置都是 null；
 * 遇到无效值不更新（也不当成 0），只在该位置留空。
 */
function ema(values: any[], n: number): Series {
  const len = Array.isArray(values) ? values.length : 0;
  const p = Math.floor(Number(n));
  const out = emptySeries(len);
  if (len === 0 || !Number.isFinite(p) || p < 1) return out;
  const alpha = 2 / (p + 1);
  const win: (null | number)[] = [];
  let prev: null | number = null;
  for (let i = 0; i < len; i += 1) {
    const cur = toNum(values[i]);
    if (prev === null) {
      // 还没播种：累计 p 个有效值后用简单平均做种子
      win.push(cur);
      if (win.length > p) win.shift();
      if (win.length === p && win.every((v) => v !== null)) {
        let sum = 0;
        for (const v of win) sum += v ?? 0;
        prev = sum / p;
        out[i] = round6(prev);
      }
      continue;
    }
    if (cur === null) continue;
    prev = alpha * cur + (1 - alpha) * prev;
    out[i] = round6(prev);
  }
  return out;
}

/** 布林带：中轨 = MA(n)，上下轨 = 中轨 ± k × 总体标准差（除以 n） */
function boll(
  values: any[],
  n: number,
  k: number,
): { lower: Series; mid: Series; upper: Series } {
  const mid = sma(values, n);
  const len = mid.length;
  const upper = emptySeries(len);
  const lower = emptySeries(len);
  const p = Math.floor(Number(n));
  const kk = Number.isFinite(Number(k)) ? Number(k) : 2;
  if (len === 0 || !Number.isFinite(p) || p < 1) return { lower, mid, upper };
  for (let i = p - 1; i < len; i += 1) {
    const m = mid[i];
    if (m === null || m === undefined) continue;
    let sum = 0;
    let bad = false;
    for (let j = i - p + 1; j <= i; j += 1) {
      const v = toNum(values[j]);
      if (v === null) {
        bad = true;
        break;
      }
      sum += v;
    }
    if (bad) continue;
    const mean = sum / p;
    let sq = 0;
    for (let j = i - p + 1; j <= i; j += 1) {
      const v = toNum(values[j]) ?? mean;
      sq += (v - mean) ** 2;
    }
    const sd = Math.sqrt(sq / p);
    upper[i] = round6(m + kk * sd);
    lower[i] = round6(m - kk * sd);
  }
  return { lower, mid, upper };
}

/**
 * RSI（Wilder 平滑）：首次用前 n 个涨跌幅的简单平均，
 * 其后 avg = (avg × (n - 1) + cur) / n；前 n 个点无有效样本，留 null。
 */
function rsi(values: any[], n: number): Series {
  const len = Array.isArray(values) ? values.length : 0;
  const p = Math.floor(Number(n));
  const out = emptySeries(len);
  if (len === 0 || !Number.isFinite(p) || p < 1) return out;
  let prev: null | number = null;
  const seedUp: number[] = [];
  const seedDown: number[] = [];
  let avgUp = 0;
  let avgDown = 0;
  let ready = false;
  for (let i = 0; i < len; i += 1) {
    const cur = toNum(values[i]);
    if (cur === null) {
      // 数据缺口：重置状态，缺口之后重新累积
      prev = null;
      ready = false;
      seedUp.length = 0;
      seedDown.length = 0;
      continue;
    }
    if (prev === null) {
      prev = cur;
      continue;
    }
    const diff = cur - prev;
    prev = cur;
    const up = diff > 0 ? diff : 0;
    const down = diff < 0 ? -diff : 0;
    if (!ready) {
      seedUp.push(up);
      seedDown.push(down);
      if (seedUp.length < p) continue;
      let su = 0;
      for (const v of seedUp) su += v;
      let sd = 0;
      for (const v of seedDown) sd += v;
      avgUp = su / p;
      avgDown = sd / p;
      ready = true;
    } else {
      avgUp = (avgUp * (p - 1) + up) / p;
      avgDown = (avgDown * (p - 1) + down) / p;
    }
    const total = avgUp + avgDown;
    // 完全没有波动时约定为中性 50
    out[i] = total === 0 ? 50 : round6((avgUp / total) * 100);
  }
  return out;
}

/** MACD：DIF = EMA(fast) − EMA(slow)，DEA = EMA(DIF, signal)，柱 = (DIF − DEA) × 2 */
function macd(
  values: any[],
  fast: number,
  slow: number,
  signal: number,
): { dea: Series; dif: Series; hist: Series } {
  const fastLine = ema(values, fast);
  const slowLine = ema(values, slow);
  const len = Math.max(fastLine.length, slowLine.length);
  const dif = emptySeries(len);
  for (let i = 0; i < len; i += 1) {
    const a = fastLine[i];
    const b = slowLine[i];
    if (a === null || a === undefined || b === null || b === undefined)
      continue;
    dif[i] = round6(a - b);
  }
  const dea = ema(dif, signal);
  const hist = emptySeries(len);
  for (let i = 0; i < len; i += 1) {
    const d = dif[i];
    const e = dea[i];
    if (d === null || d === undefined || e === null || e === undefined)
      continue;
    hist[i] = round6((d - e) * 2);
  }
  return { dea, dif, hist };
}

interface OverlayDef {
  color: string;
  dashed?: boolean;
  kind: 'boll-lower' | 'boll-mid' | 'boll-upper' | 'ema' | 'ma';
  name: string;
  period: number;
}

/** 主图叠加线的定义（名称/颜色/周期）：图例、图表与底部说明共用同一套来源 */
function overlayDefs(c: IndicatorConfig): OverlayDef[] {
  const defs: OverlayDef[] = [];
  if (c.overlays.ma.enabled) {
    c.overlays.ma.periods.forEach((p, i) => {
      defs.push({
        color: MA_COLORS[i % MA_COLORS.length] ?? '#f0b90b',
        kind: 'ma',
        name: `MA${p}`,
        period: p,
      });
    });
  }
  if (c.overlays.ema.enabled) {
    c.overlays.ema.periods.forEach((p, i) => {
      defs.push({
        color: EMA_COLORS[i % EMA_COLORS.length] ?? '#06b6d4',
        kind: 'ema',
        name: `EMA${p}`,
        period: p,
      });
    });
  }
  if (c.overlays.boll.enabled) {
    defs.push({
      color: BOLL_COLORS.upper,
      kind: 'boll-upper',
      name: 'BOLL上轨',
      period: c.overlays.boll.period,
    });
    defs.push({
      color: BOLL_COLORS.mid,
      dashed: true,
      kind: 'boll-mid',
      name: 'BOLL中轨',
      period: c.overlays.boll.period,
    });
    defs.push({
      color: BOLL_COLORS.lower,
      kind: 'boll-lower',
      name: 'BOLL下轨',
      period: c.overlays.boll.period,
    });
  }
  return defs;
}

/** 按当前配置算出主图叠加线的数据（不足前导的位置为 null，ECharts 自然断开） */
function overlaySeries(closes: number[]): any[] {
  const c = cfg.value;
  const defs = overlayDefs(c);
  const needBoll = defs.some((d) => d.kind !== 'ema' && d.kind !== 'ma');
  const band = needBoll
    ? boll(closes, c.overlays.boll.period, c.overlays.boll.k)
    : null;
  return defs.map((d) => {
    let data: Series = [];
    if (d.kind === 'ma') data = sma(closes, d.period);
    else if (d.kind === 'ema') data = ema(closes, d.period);
    else if (d.kind === 'boll-upper') data = band?.upper ?? [];
    else if (d.kind === 'boll-mid') data = band?.mid ?? [];
    else data = band?.lower ?? [];
    return {
      data,
      itemStyle: { color: d.color },
      lineStyle: d.dashed ? { type: 'dashed', width: 1 } : { width: 1.5 },
      name: d.name,
      showSymbol: false,
      smooth: true,
      type: 'line',
      xAxisIndex: 0,
      yAxisIndex: 0,
    };
  });
}

/** 底部图例：与图表里的主图叠加线同名同色 */
const overlayLegend = computed(() =>
  overlayDefs(cfg.value).map((d) => ({ color: d.color, name: d.name })),
);

/* ------------------------------------------------------------------ *
 * 行情数据与图表
 * ------------------------------------------------------------------ */

const pair = computed(() => props.pair);
const tf = computed(() => props.timeframe);
const error = ref('');
let disposed = false;
const loading = ref(false);
/** 'live' = 服务端代理的 Binance 实时行情；'local' = 本地已下载 feather */
const sourceKind = ref<'live' | 'local'>('live');
const lastCandle = ref('');
const loadedTimeframe = ref('');
const chartRef = ref<EchartsUIType>();
const { renderEcharts } = useEcharts(chartRef);

interface ChartData {
  closes: number[];
  dates: string[];
  kline: number[][];
  marks: any[];
  vol: any[];
}

/** 最近一次取到的行情：改指标配置时直接用它重绘，不必重新请求 */
const chartData = shallowRef<ChartData | null>(null);

const localTime = (v: string) => {
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return String(v);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};

/**
 * 取 K 线：优先实时源（服务端代理 Binance），失败回落到本地 feather。
 *
 * 为什么要实时源：本地 feather 是「下载时点」的快照，机器人不重启就不会更新，
 * 图表会一直停在旧数据上（实测停在两天前）。
 */
async function fetchKlines(requestPair: string, requestTf: string) {
  try {
    const live = await getLiveKlines(requestPair, requestTf, 200);
    if (live?.data?.length) {
      sourceKind.value = 'live';
      return live;
    }
  } catch {
    /* 落到本地 */
  }
  const local = await getLocalOhlcv(requestPair, requestTf, 200);
  sourceKind.value = 'local';
  return local;
}

async function load() {
  if (!pair.value || disposed || loading.value) return;
  const requestPair = pair.value;
  const requestTf = tf.value;
  loading.value = true;
  error.value = '';
  if (loadedTimeframe.value !== requestTf) {
    chartData.value = null;
    lastCandle.value = '';
  }
  try {
    await withMarketSlot(async () => {
      if (disposed) return;
      const res = await fetchKlines(requestPair, requestTf);
      const ix = (n: string) => res.columns.indexOf(n);
      const [iO, iH, iL, iC, iV] = [
        ix('open'),
        ix('high'),
        ix('low'),
        ix('close'),
        ix('volume'),
      ];

      // 策略信号仍从 Freqtrade 取；只有周期与机器人一致时才有数据，取不到就跳过
      const sigByDate = new Map<
        string,
        { el: number; es: number; xl: number; xs: number }
      >();
      try {
        const pc = await getPairCandles(requestPair, requestTf, 200);
        if (pc.data?.length) {
          const jx = (n: string) => pc.columns.indexOf(n);
          const [jEL, jES, jXL, jXS] = [
            jx('enter_long'),
            jx('enter_short'),
            jx('exit_long'),
            jx('exit_short'),
          ];
          pc.data.forEach((r) => {
            sigByDate.set(String(r[0]), {
              el: jEL >= 0 ? +r[jEL] || 0 : 0,
              es: jES >= 0 ? +r[jES] || 0 : 0,
              xl: jXL >= 0 ? +r[jXL] || 0 : 0,
              xs: jXS >= 0 ? +r[jXS] || 0 : 0,
            });
          });
        }
      } catch {
        /* 信号非必需 */
      }

      const dates: string[] = [];
      const kline: number[][] = [];
      const vol: any[] = [];
      const marks: any[] = [];
      const closes: number[] = [];

      res.data.forEach((r, i) => {
        const t = localTime(r[0]);
        dates.push(t);
        const o = +r[iO];
        const c = +r[iC];
        closes.push(c);
        kline.push([o, c, +r[iL], +r[iH]]);
        vol.push({
          itemStyle: {
            color: c >= o ? 'rgba(240,66,79,.5)' : 'rgba(18,184,134,.5)',
          },
          value: +r[iV] || 0,
        });
        const sg = sigByDate.get(String(r[0]));
        if (sg?.el) marks.push({ coord: [i, +r[iL]], value: '▲' });
        if (sg?.es) marks.push({ coord: [i, +r[iH]], value: '▼' });
        if (sg?.xl) marks.push({ coord: [i, +r[iH]], value: '×' });
        if (sg?.xs) marks.push({ coord: [i, +r[iL]], value: '×' });
      });

      if (disposed || pair.value !== requestPair || tf.value !== requestTf)
        return;
      lastCandle.value = res.data.length
        ? localTime(String(res.data.at(-1)![0]))
        : '';
      loadedTimeframe.value = requestTf;
      chartData.value = { closes, dates, kline, marks, vol };
      renderChart();
    });
  } catch {
    error.value = '该币种行情暂不可用，请稍后重试';
  } finally {
    loading.value = false;
    // 切换周期时忽略旧响应，完成后补取当前周期。
    if (!disposed && (pair.value !== requestPair || tf.value !== requestTf))
      void load();
  }
}

/**
 * 画图：K 线 + 成交量 + 用户开启的主图叠加线 / RSI / MACD 副图。
 *
 * 这里把主图和所有副图放在**同一个 ECharts 实例**的多个 grid 里：
 * dataZoom 只要把 xAxisIndex 列全就能覆盖所有子图，十字光标用
 * axisPointer.link 就能联动，不需要跨实例 connect（也就不会出现
 * 两个实例缩放/光标不同步的情况）。
 */
function renderChart() {
  const d = chartData.value;
  if (!d) return;
  const c = cfg.value;
  const panels = panelKeys.value;
  const { closes, dates } = d;
  const axisIndexes = Array.from({ length: panels.length + 1 }, (_, i) => i);

  const grid: any[] = [{ height: MAIN_PX, left: 60, right: 24, top: TOP_PX }];
  const xAxis: any[] = [
    {
      axisLabel: { fontSize: 10 },
      boundaryGap: true,
      data: dates,
      gridIndex: 0,
      type: 'category',
    },
  ];
  const yAxis: any[] = [
    {
      gridIndex: 0,
      scale: true,
      splitLine: { lineStyle: { color: '#f1f5f9' } },
      type: 'value',
    },
  ];

  const overlays = overlaySeries(closes);
  const series: any[] = [
    {
      data: d.kline,
      itemStyle: {
        color: '#f0424f',
        color0: '#12b886',
        borderColor: '#f0424f',
        borderColor0: '#12b886',
      },
      markPoint: {
        data: d.marks,
        label: { color: '#f0b90b', fontSize: 13, fontWeight: 'bold' },
        symbol: 'pin',
        symbolSize: 1,
      },
      name: 'K线',
      type: 'candlestick',
      xAxisIndex: 0,
      yAxisIndex: 0,
    },
    ...overlays,
  ];
  // 图例保持改造前的 K线 + 主图叠加线（成交量原来就不在图例里）
  const legend: string[] = ['K线', ...overlayDefs(c).map((x) => x.name)];

  let cursor = TOP_PX + MAIN_PX + GAP_PX;
  panels.forEach((key, i) => {
    const gi = i + 1;
    const height = PANEL_PX[key] ?? 100;
    grid.push({ height, left: 60, right: 24, top: cursor });
    cursor += height + GAP_PX;
    xAxis.push({
      axisLabel: { show: false },
      boundaryGap: true,
      data: dates,
      gridIndex: gi,
      type: 'category',
    });
    const y: any = { gridIndex: gi, splitNumber: 2, type: 'value' };
    if (key === 'rsi') {
      y.max = 100;
      y.min = 0;
    }
    if (key === 'macd') y.scale = true;
    yAxis.push(y);

    if (key === 'volume') {
      series.push({
        data: d.vol,
        name: '成交量',
        type: 'bar',
        xAxisIndex: gi,
        yAxisIndex: gi,
      });
    } else if (key === 'rsi') {
      const p = c.panels.rsi.period;
      const name = `RSI${p}`;
      legend.push(name);
      series.push({
        data: rsi(closes, p),
        itemStyle: { color: '#a855f7' },
        markLine: {
          data: [{ yAxis: 70 }, { yAxis: 30 }],
          label: { fontSize: 10 },
          lineStyle: { color: '#cbd5e1', type: 'dashed' },
          silent: true,
          symbol: 'none',
        },
        name,
        showSymbol: false,
        smooth: true,
        type: 'line',
        xAxisIndex: gi,
        yAxisIndex: gi,
      });
    } else {
      const m = c.panels.macd;
      const { dea, dif, hist } = macd(closes, m.fast, m.slow, m.signal);
      legend.push('MACD柱', 'DIF', 'DEA');
      series.push({
        data: hist.map((v) => ({
          itemStyle: {
            color: (v ?? 0) >= 0 ? 'rgba(240,66,79,.6)' : 'rgba(18,184,134,.6)',
          },
          value: v,
        })),
        markLine: {
          data: [{ yAxis: 0 }],
          label: { show: false },
          lineStyle: { color: '#cbd5e1', type: 'dashed' },
          silent: true,
          symbol: 'none',
        },
        name: 'MACD柱',
        type: 'bar',
        xAxisIndex: gi,
        yAxisIndex: gi,
      });
      series.push({
        data: dif,
        itemStyle: { color: '#f0b90b' },
        name: 'DIF',
        showSymbol: false,
        smooth: true,
        type: 'line',
        xAxisIndex: gi,
        yAxisIndex: gi,
      });
      series.push({
        data: dea,
        itemStyle: { color: '#3b82f6' },
        name: 'DEA',
        showSymbol: false,
        smooth: true,
        type: 'line',
        xAxisIndex: gi,
        yAxisIndex: gi,
      });
    }
  });

  renderEcharts({
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    dataZoom: [
      { end: 100, start: 60, type: 'inside', xAxisIndex: axisIndexes },
      {
        bottom: 8,
        end: 100,
        start: 60,
        type: 'slider',
        xAxisIndex: axisIndexes,
      },
    ],
    grid,
    legend: { data: legend, top: 0 },
    series,
    tooltip: { axisPointer: { type: 'cross' }, trigger: 'axis' },
    xAxis,
    yAxis,
  });
}

watch([pair, tf], load);
// 指标配置变化：写回 localStorage 并就地重绘（不重新请求行情）
watch(
  cfg,
  () => {
    saveConfig();
    renderChart();
  },
  { deep: true, flush: 'post' },
);

let timer: any = null;
onMounted(() => {
  void load();
  timer = setInterval(load, 30_000);
});
onUnmounted(() => {
  disposed = true;
  clearInterval(timer);
});
</script>

<template>
  <div class="min-w-0">
    <Card :bordered="false" class="shadow-sm" :title="pair">
      <template #extra>
        <Button size="small" @click="cfgOpen = true"
          >指标 {{ activeCount }}</Button
        >
      </template>
      <Alert
        v-if="error"
        class="mb-3"
        :message="error"
        type="warning"
        show-icon
      />
      <Spin :spinning="loading && !chartData">
        <EchartsUI
          v-show="chartData"
          ref="chartRef"
          :height="chartHeight + 'px'"
        />
      </Spin>
      <div class="mt-2 flex flex-wrap items-center gap-2 text-xs text-gray-400">
        <Tag
          v-if="chartData"
          :color="sourceKind === 'live' ? 'green' : 'orange'"
        >
          {{ sourceKind === 'live' ? '实时行情' : '数据库快照' }}
        </Tag>
        <span>{{ tf }} · {{ lastCandle || '等待行情' }}</span>
        <span
          v-for="o in overlayLegend"
          :key="o.name"
          :style="{ color: o.color }"
          >{{ o.name }}</span
        >
      </div>
    </Card>

    <Drawer
      v-model:open="cfgOpen"
      :width="360"
      placement="right"
      title="技术指标设置"
    >
      <div class="text-xs leading-5 text-gray-400">
        指标全部在前端本地计算，配置保存在浏览器本地（localStorage），下次打开自动恢复。
      </div>

      <Divider orientation="left" plain>主图叠加</Divider>

      <!-- MA -->
      <div class="mb-4">
        <div class="flex items-center justify-between">
          <span class="text-sm">MA 移动平均</span>
          <Switch
            :checked="cfg.overlays.ma.enabled"
            size="small"
            @change="(v) => setFlag('ma', v)"
          />
        </div>
        <div v-if="cfg.overlays.ma.enabled" class="mt-2">
          <div class="flex items-center justify-between text-xs text-gray-400">
            <span
              >周期 {{ MIN_PERIOD }}–{{ MAX_PERIOD }}，最多
              {{ MAX_PERIODS }} 条</span
            >
            <Button
              :disabled="cfg.overlays.ma.periods.length >= MAX_PERIODS"
              size="small"
              type="link"
              @click="addPeriod('ma')"
            >
              添加周期
            </Button>
          </div>
          <div class="mt-1 flex flex-wrap items-center gap-2">
            <div
              v-for="(p, i) in cfg.overlays.ma.periods"
              :key="'ma-' + p"
              class="flex items-center gap-1"
            >
              <InputNumber
                :max="MAX_PERIOD"
                :min="MIN_PERIOD"
                :value="p"
                size="small"
                style="width: 84px"
                @change="(v) => setPeriod('ma', i, v)"
              />
              <Button
                danger
                size="small"
                type="text"
                @click="removePeriod('ma', i)"
              >
                删除
              </Button>
            </div>
            <span
              v-if="!cfg.overlays.ma.periods.length"
              class="text-xs text-gray-400"
            >
              未设置周期
            </span>
          </div>
        </div>
      </div>

      <!-- EMA -->
      <div class="mb-4">
        <div class="flex items-center justify-between">
          <span class="text-sm">EMA 指数移动平均</span>
          <Switch
            :checked="cfg.overlays.ema.enabled"
            size="small"
            @change="(v) => setFlag('ema', v)"
          />
        </div>
        <div v-if="cfg.overlays.ema.enabled" class="mt-2">
          <div class="flex items-center justify-between text-xs text-gray-400">
            <span
              >周期 {{ MIN_PERIOD }}–{{ MAX_PERIOD }}，最多
              {{ MAX_PERIODS }} 条</span
            >
            <Button
              :disabled="cfg.overlays.ema.periods.length >= MAX_PERIODS"
              size="small"
              type="link"
              @click="addPeriod('ema')"
            >
              添加周期
            </Button>
          </div>
          <div class="mt-1 flex flex-wrap items-center gap-2">
            <div
              v-for="(p, i) in cfg.overlays.ema.periods"
              :key="'ema-' + p"
              class="flex items-center gap-1"
            >
              <InputNumber
                :max="MAX_PERIOD"
                :min="MIN_PERIOD"
                :value="p"
                size="small"
                style="width: 84px"
                @change="(v) => setPeriod('ema', i, v)"
              />
              <Button
                danger
                size="small"
                type="text"
                @click="removePeriod('ema', i)"
              >
                删除
              </Button>
            </div>
            <span
              v-if="!cfg.overlays.ema.periods.length"
              class="text-xs text-gray-400"
            >
              未设置周期
            </span>
          </div>
        </div>
      </div>

      <!-- BOLL -->
      <div class="mb-4">
        <div class="flex items-center justify-between">
          <span class="text-sm">BOLL 布林带</span>
          <Switch
            :checked="cfg.overlays.boll.enabled"
            size="small"
            @change="(v) => setFlag('boll', v)"
          />
        </div>
        <div
          v-if="cfg.overlays.boll.enabled"
          class="mt-2 flex items-center gap-2 text-xs text-gray-400"
        >
          <span>周期</span>
          <InputNumber
            :max="MAX_PERIOD"
            :min="2"
            :value="cfg.overlays.boll.period"
            size="small"
            style="width: 84px"
            @change="(v) => setNum('bollPeriod', v)"
          />
          <span>倍数</span>
          <InputNumber
            :max="10"
            :min="0.1"
            :step="0.1"
            :value="cfg.overlays.boll.k"
            size="small"
            style="width: 84px"
            @change="(v) => setNum('bollK', v)"
          />
        </div>
      </div>

      <Divider orientation="left" plain>副图</Divider>

      <div class="mb-4 flex items-center justify-between">
        <span class="text-sm">成交量</span>
        <Switch
          :checked="cfg.panels.volume"
          size="small"
          @change="(v) => setFlag('volume', v)"
        />
      </div>

      <!-- RSI -->
      <div class="mb-4">
        <div class="flex items-center justify-between">
          <span class="text-sm">RSI 相对强弱</span>
          <Switch
            :checked="cfg.panels.rsi.enabled"
            size="small"
            @change="(v) => setFlag('rsi', v)"
          />
        </div>
        <div
          v-if="cfg.panels.rsi.enabled"
          class="mt-2 flex items-center gap-2 text-xs text-gray-400"
        >
          <span>周期</span>
          <InputNumber
            :max="MAX_PERIOD"
            :min="2"
            :value="cfg.panels.rsi.period"
            size="small"
            style="width: 84px"
            @change="(v) => setNum('rsiPeriod', v)"
          />
          <span>参考线 30 / 70</span>
        </div>
      </div>

      <!-- MACD -->
      <div class="mb-4">
        <div class="flex items-center justify-between">
          <span class="text-sm">MACD</span>
          <Switch
            :checked="cfg.panels.macd.enabled"
            size="small"
            @change="(v) => setFlag('macd', v)"
          />
        </div>
        <div
          v-if="cfg.panels.macd.enabled"
          class="mt-2 flex flex-wrap items-center gap-2 text-xs text-gray-400"
        >
          <span>快线</span>
          <InputNumber
            :max="MAX_PERIOD"
            :min="1"
            :value="cfg.panels.macd.fast"
            size="small"
            style="width: 72px"
            @change="(v) => setNum('macdFast', v)"
          />
          <span>慢线</span>
          <InputNumber
            :max="MAX_PERIOD"
            :min="1"
            :value="cfg.panels.macd.slow"
            size="small"
            style="width: 72px"
            @change="(v) => setNum('macdSlow', v)"
          />
          <span>信号</span>
          <InputNumber
            :max="MAX_PERIOD"
            :min="1"
            :value="cfg.panels.macd.signal"
            size="small"
            style="width: 72px"
            @change="(v) => setNum('macdSignal', v)"
          />
        </div>
      </div>

      <Divider plain />
      <Button block @click="resetCfg">恢复默认</Button>
    </Drawer>
  </div>
</template>

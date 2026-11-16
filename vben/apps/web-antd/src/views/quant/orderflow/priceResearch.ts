import { reactive } from 'vue';

export type PriceRecord = Record<string, unknown>;
export interface PriceQuery {
  pair: string;
  direction: 'any' | 'up' | 'down';
}
export interface PriceEvent extends PriceRecord {
  event_id: string;
  pair: string;
  timestamp: number;
  status: string;
  observation: PriceRecord;
  outcome: PriceRecord | null;
  profile: PriceRecord | null;
}
export interface PriceSnapshot {
  items: PriceEvent[];
  query: PriceQuery;
  receivedAt: number;
  method: string;
  stats: {
    by_pair: Record<string, PriceRecord>;
    profile_by_pair: Record<string, PriceRecord>;
  };
  warmup: Record<string, PriceRecord>;
  limitations: string[];
}
export interface ProfilePoint {
  offset: number;
  value: number;
}
export const priceBands = [
  { key: 'lt1', label: '<1x' },
  { key: '1to2', label: '1–2x' },
  { key: '2to4', label: '2–4x' },
  { key: '4to8', label: '4–8x' },
  { key: '8to20', label: '8–20x' },
  { key: 'ge20', label: '≥20x' },
];
const method = 'orderflow-price-spike-close3-v4';
function record(value: unknown): value is PriceRecord {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}
export function priceNumber(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  if (typeof value === 'string' && !value.trim()) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}
function validTime(value: unknown): value is number {
  return (
    typeof value === 'number' &&
    Number.isSafeInteger(value) &&
    value >= 0 &&
    value <= 8640000000000000
  );
}
function contract(): never {
  throw new Error('价格研究响应合同不完整');
}
function probability(value: unknown) {
  const n = priceNumber(value);
  return value === null || (n !== null && n >= 0 && n <= 1);
}
function validateStats(bucket: unknown): bucket is PriceRecord {
  if (!record(bucket)) return false;
  for (const [key, value] of Object.entries(bucket)) {
    if (
      key.endsWith('_count') &&
      (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0)
    )
      return false;
    if (
      ['base_p_any', 'p_up', 'p_down', 'p_any'].includes(key) &&
      !probability(value)
    )
      return false;
    if (
      key === 'lift' &&
      value !== null &&
      (priceNumber(value) === null || Number(value) < 0)
    )
      return false;
    if (
      key === 'bands' &&
      (!record(value) || Object.values(value).some((v) => !validateStats(v)))
    )
      return false;
  }
  return true;
}
function validateProfile(value: unknown) {
  if (!Array.isArray(value)) return false;
  const seen = new Set<number>();
  return value.every((point: unknown) => {
    if (
      !record(point) ||
      typeof point.offset !== 'number' ||
      !Number.isInteger(point.offset) ||
      point.offset < -12 ||
      point.offset > 12 ||
      seen.has(point.offset)
    )
      return false;
    seen.add(point.offset);
    return (
      typeof point.sample_count === 'number' &&
      Number.isSafeInteger(point.sample_count) &&
      point.sample_count >= 0 &&
      (point.mean_volume_ratio === null ||
        (priceNumber(point.mean_volume_ratio) !== null &&
          Number(point.mean_volume_ratio) >= 0))
    );
  });
}
export function parsePriceSnapshot(
  raw: unknown,
  query: PriceQuery,
  receivedAt = Date.now(),
): PriceSnapshot {
  if (
    !record(raw) ||
    !Array.isArray(raw.items) ||
    raw.market !== 'futures' ||
    raw.timeframe !== '5m' ||
    raw.direction !== query.direction ||
    raw.sample_scope !== 'live_online_only' ||
    raw.method_version !== method ||
    !record(raw.stats) ||
    !record(raw.warmup) ||
    !Array.isArray(raw.limitations) ||
    !raw.limitations.every((v) => typeof v === 'string')
  )
    contract();
  const stats = raw.stats;
  if (
    stats.method_version !== method ||
    stats.forward_bars !== 3 ||
    stats.event_threshold_pct !== 0.015 ||
    stats.sample_scope !== 'live_online_only' ||
    stats.unknown_excluded !== true ||
    stats.invalid_excluded !== true ||
    !Array.isArray(stats.ratio_bands) ||
    stats.ratio_bands.join('|') !== priceBands.map((v) => v.key).join('|') ||
    !record(stats.by_pair) ||
    !record(stats.profile_by_pair) ||
    !validateStats(stats.by_pair[query.pair])
  )
    contract();
  const profiles = stats.profile_by_pair[query.pair];
  if (
    !record(profiles) ||
    !validateProfile(profiles.up) ||
    !validateProfile(profiles.down)
  )
    contract();
  const seen = new Set<string>();
  for (const item of raw.items) {
    if (
      !record(item) ||
      typeof item.event_id !== 'string' ||
      !item.event_id ||
      item.event_id.length > 512 ||
      seen.has(item.event_id) ||
      item.pair !== query.pair ||
      !validTime(item.timestamp) ||
      !['unknown', 'invalid', 'matured'].includes(String(item.status)) ||
      !record(item.observation) ||
      (item.outcome !== null && !record(item.outcome)) ||
      (item.profile !== null && !record(item.profile)) ||
      (item.profile_status !== null &&
        item.profile_status !== undefined &&
        !['unknown', 'invalid', 'matured'].includes(
          String(item.profile_status),
        ))
    )
      contract();
    if (
      item.status === 'matured' &&
      (!record(item.outcome) ||
        !['up', 'down', 'none'].includes(String(item.outcome.direction)) ||
        priceNumber(item.outcome.return_pct) === null)
    )
      contract();
    if (
      query.direction !== 'any' &&
      (!record(item.outcome) || item.outcome.direction !== query.direction)
    )
      contract();
    seen.add(item.event_id);
  }
  for (const warmup of Object.values(raw.warmup)) {
    if (!record(warmup)) contract();
    for (const key of ['valid_bars', 'required_bars'])
      if (
        key in warmup &&
        (typeof warmup[key] !== 'number' ||
          !Number.isSafeInteger(warmup[key]) ||
          Number(warmup[key]) < 0)
      )
        contract();
    if ('ready' in warmup && typeof warmup.ready !== 'boolean') contract();
  }
  // 完整副本保证后来修改调用者对象不会改变已展示的来源快照。
  return JSON.parse(
    JSON.stringify({
      items: raw.items,
      query: { ...query },
      receivedAt,
      method,
      stats,
      warmup: raw.warmup,
      limitations: raw.limitations,
    }),
  ) as PriceSnapshot;
}
export function outcomeLabel(item: { status?: unknown; outcome?: unknown }) {
  if (item.status === 'invalid') return '无效 · 数据缺口';
  if (item.status !== 'matured') return '待成熟';
  const direction = record(item.outcome) ? item.outcome.direction : null;
  return direction === 'up'
    ? '上涨 +'
    : direction === 'down'
      ? '下跌 −'
      : direction === 'none'
        ? '未达到阈值'
        : '方向未知';
}
export function selectPriceEvent<T extends { event_id: string }>(
  rows: T[],
  id: string,
) {
  const selected = id
    ? (rows.find((row) => row.event_id === id) ?? null)
    : null;
  return { selected, missing: !!id && selected === null };
}
export function profileSegments(points: unknown): ProfilePoint[][] {
  if (!Array.isArray(points)) return [];
  const map = new Map<number, PriceRecord>();
  for (const point of points)
    if (record(point) && typeof point.offset === 'number')
      map.set(point.offset, point);
  const segments: ProfilePoint[][] = [];
  let current: ProfilePoint[] = [];
  for (let offset = -12; offset <= 12; offset += 1) {
    const point = map.get(offset);
    const value = priceNumber(point?.mean_volume_ratio);
    if (
      !point ||
      value === null ||
      priceNumber(point.sample_count) === null ||
      Number(point.sample_count) <= 0
    ) {
      if (current.length) segments.push(current);
      current = [];
    } else current.push({ offset, value });
  }
  if (current.length) segments.push(current);
  return segments;
}
export function createPriceReader() {
  const state = reactive<{
    snapshot: PriceSnapshot | null;
    loading: boolean;
    error: string;
  }>({ snapshot: null, loading: false, error: '' });
  let generation = 0;
  let controller: AbortController | undefined;
  return {
    state,
    async load(
      query: PriceQuery,
      fetch: (signal: AbortSignal) => Promise<unknown>,
    ) {
      controller?.abort();
      controller = new AbortController();
      const current = ++generation;
      state.loading = true;
      state.error = '';
      try {
        const raw = await fetch(controller.signal);
        if (current !== generation) return;
        state.snapshot = parsePriceSnapshot(raw, query);
      } catch {
        if (current === generation)
          state.error = '价格研究读取失败，请重试；上次成功事实未更新';
      } finally {
        if (current === generation) state.loading = false;
      }
    },
    cancel() {
      generation += 1;
      controller?.abort();
      state.loading = false;
    },
  };
}

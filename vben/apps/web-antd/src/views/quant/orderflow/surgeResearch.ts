import { reactive } from 'vue';

export type Fact = Record<string, unknown>;
export interface SurgeEvent extends Fact {
  event_id: string;
  pair: string;
  timeframe: string;
  timestamp: number | string;
  observation?: Fact;
  status: string;
}
export interface SurgeDetail {
  observation: SurgeEvent;
  events: Fact[];
}
export interface SurgeQuery {
  pair?: string;
  status?: string;
  timeframe?: string;
}
export interface SurgeStatistics extends Fact {
  by_pair: Fact[];
}
export interface MaturityWindow extends Fact {
  horizon: string;
  status: string;
}
const failed = () => new Error('订单流证据合同不完整，不能视作无记录');
function object(value: unknown): Fact {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw failed();
  return value as Fact;
}
function text(value: unknown): string {
  if (
    typeof value !== 'string' ||
    !value.trim() ||
    value.length > 512 ||
    /[\u0000-\u001F]/.test(value)
  )
    throw failed();
  return value;
}
export function finite(value: unknown): number | null {
  if (
    typeof value === 'string' &&
    !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value)
  )
    return null;
  if (
    value === null ||
    value === undefined ||
    value === '' ||
    typeof value === 'boolean' ||
    !['number', 'string'].includes(typeof value)
  )
    return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
export function timeMs(value: unknown): number | null {
  if (
    typeof value === 'number' ||
    (typeof value === 'string' && /^\d+$/.test(value))
  ) {
    const n = finite(value);
    return n !== null &&
      Number.isSafeInteger(n) &&
      n >= 0 &&
      Number.isFinite(new Date(n).getTime())
      ? n
      : null;
  }
  if (typeof value !== 'string') return null;
  const m =
    /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-]\d{2}:?\d{2})?$/.exec(
      value,
    );
  if (!m) return null;
  const [y, mo, d, h, mi, s] = m.slice(1, 7).map(Number),
    date = new Date(Date.UTC(y!, mo! - 1, d!));
  if (
    date.getUTCFullYear() !== y ||
    date.getUTCMonth() + 1 !== mo ||
    date.getUTCDate() !== d ||
    h! > 23 ||
    mi! > 59 ||
    s! > 59
  )
    return null;
  const n = Date.parse(m[7] ? value : value.replace(' ', 'T') + 'Z');
  return Number.isFinite(n) ? n : null;
}
export function valueOf(row: Fact, key: string): unknown {
  const nested = row.observation;
  return nested && typeof nested === 'object' && !Array.isArray(nested)
    ? ((nested as Fact)[key] ?? row[key])
    : row[key];
}
function parseEvent(value: unknown): SurgeEvent {
  const row = object(value);
  text(row.event_id);
  text(row.pair);
  const timeframe = row.timeframe ?? '5m';
  text(timeframe);
  if (timeMs(row.timestamp) === null) throw failed();
  if (row.observation !== undefined && row.observation !== null) {
    const observation = object(row.observation);
    if (observation.pair !== undefined && observation.pair !== row.pair)
      throw failed();
    if (
      observation.timestamp !== undefined &&
      timeMs(observation.timestamp) !== timeMs(row.timestamp)
    )
      throw failed();
  }
  const status = row.status ?? 'unknown';
  if (!['unknown', 'matured', 'invalid'].includes(String(status)))
    throw failed();
  for (const name of [
    'quote_volume',
    'baseline_quote_volume',
    'volume_ratio',
    'num_trades',
    'flow_imbal',
  ]) {
    const v = valueOf(row, name);
    if (v !== undefined && v !== null && finite(v) === null) throw failed();
  }
  return {
    ...row,
    event_id: text(row.event_id),
    pair: text(row.pair),
    timeframe: String(timeframe),
    timestamp: row.timestamp as number | string,
    status: String(status),
  };
}
export function parseSurgeList(value: unknown): SurgeEvent[] {
  const source = object(value);
  if (!Array.isArray(source.items)) throw failed();
  const rows = source.items.map(parseEvent),
    ids = new Set<string>();
  for (const row of rows) {
    if (ids.has(row.event_id)) throw failed();
    ids.add(row.event_id);
  }
  return rows;
}
export function parseSurgeDetail(value: unknown, eventId: string): SurgeDetail {
  const source = object(value);
  const observation = parseEvent(
    source.event_id !== undefined
      ? source
      : (source.observation ?? source.item),
  );
  if (observation.event_id !== eventId) throw failed();
  const revisions = source.events ?? source.revisions;
  if (!Array.isArray(revisions)) throw failed();
  const events = revisions.map((raw) => {
    const row = object(raw);
    if (row.event_id !== undefined && row.event_id !== eventId) throw failed();
    if (row.observation_id !== undefined && row.observation_id !== eventId)
      throw failed();
    return { ...row };
  });
  return { observation, events };
}
export function maturityWindows(detail: SurgeDetail): MaturityWindow[] {
  const results = new Map<string, Fact>();
  const events = [detail.observation, ...detail.events].sort(
    (a, b) =>
      (finite(a.revision_no ?? a.sequence_id) ?? 0) -
      (finite(b.revision_no ?? b.sequence_id) ?? 0),
  );
  for (const event of events) {
    const payload =
      event.payload &&
      typeof event.payload === 'object' &&
      !Array.isArray(event.payload)
        ? (event.payload as Fact)
        : {};
    const raw = event.result ?? payload.result ?? payload;
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue;
    const result = raw as Fact;
    // 上层修订判为无效时，历史嵌套结果不能恢复成可统计收益。
    const status =
      event.status === 'invalid' ? 'invalid' : (result.status ?? event.status);
    if (
      typeof result.horizon === 'string' &&
      ['matured', 'invalid'].includes(String(status))
    )
      results.set(result.horizon, { ...result, status });
  }
  return ['5m', '30m', '1h', '4h'].map((horizon) => {
    const r = results.get(horizon);
    return r?.status === 'matured'
      ? { ...r, horizon, status: 'matured' }
      : { horizon, status: r?.status === 'invalid' ? 'invalid' : 'unknown' };
  });
}
export function parseSurgeStats(value: unknown): SurgeStatistics {
  const source = object(value),
    raw = source.by_pair ?? source.pairs;
  if (!Array.isArray(raw) && (!raw || typeof raw !== 'object')) throw failed();
  const pairs = Array.isArray(raw)
    ? raw
    : Object.entries(raw as Fact).map(([pair, row]) => ({
        ...object(row),
        pair,
      }));
  const ids = new Set<string>();
  const by_pair = pairs.map((raw) => {
    const row = object(raw),
      pair = text(row.pair);
    if (ids.has(pair)) throw failed();
    ids.add(pair);
    validateMetrics(row);
    if (row.horizons !== undefined) {
      for (const bucket of Object.values(object(row.horizons)))
        validateMetrics(object(bucket));
    }
    return { ...row, pair };
  });
  if (source.warmup !== undefined) {
    for (const raw of Object.values(object(source.warmup))) {
      const row = object(raw);
      for (const key of ['valid_bars', 'required_bars'])
        if (
          row[key] !== undefined &&
          (typeof row[key] !== 'number' ||
            !Number.isSafeInteger(row[key]) ||
            row[key] < 0)
        )
          throw failed();
      if (row.ready !== undefined && typeof row.ready !== 'boolean')
        throw failed();
      if (
        row.last_timestamp !== undefined &&
        row.last_timestamp !== null &&
        timeMs(row.last_timestamp) === null
      )
        throw failed();
    }
  }
  return { ...source, by_pair };
}
function validateMetrics(row: Fact) {
  for (const key of [
    'total',
    'unknown',
    'matured',
    'invalid',
    'known',
    'sample_count',
  ]) {
    const v = row[key];
    if (
      v !== undefined &&
      (finite(v) === null || !Number.isSafeInteger(finite(v)) || finite(v)! < 0)
    )
      throw failed();
  }
  for (const key of [
    'gross_return_mean',
    'gross_mean',
    'mfe_mean',
    'mae_mean',
    'required_fee_rate',
    'actual_fee_rate',
    'win_rate',
  ]) {
    const v = row[key];
    if (v !== undefined && v !== null && finite(v) === null) throw failed();
  }
  if (
    row.win_rate !== undefined &&
    row.win_rate !== null &&
    (finite(row.win_rate)! < 0 || finite(row.win_rate)! > 1)
  )
    throw failed();
}
export function pairStats(
  stats: SurgeStatistics | null,
  pair: string,
): Fact | null {
  return stats?.by_pair.find((r) => r.pair === pair) ?? null;
}
export function createResearchReader<T, Q extends object>(
  load: (query: Q, signal: AbortSignal) => Promise<unknown>,
  parse: (value: unknown, query: Q) => T,
) {
  const state = reactive({
    snapshot: null as T | null,
    snapshotQuery: null as Q | null,
    query: {} as Q,
    loading: false,
    error: '',
    status: 'unread' as 'unread' | 'ready' | 'stale' | 'failed',
    updatedAt: null as number | null,
  });
  let generation = 0,
    controller: AbortController | undefined,
    disposed = false;
  async function refresh(query: Q) {
    if (disposed) return;
    controller?.abort();
    controller = new AbortController();
    const current = ++generation;
    state.query = { ...query } as typeof state.query;
    state.loading = true;
    state.error = '';
    try {
      const raw = await load(query, controller.signal);
      if (disposed || current !== generation) return;
      const snapshot = parse(raw, query);
      state.snapshot = snapshot as typeof state.snapshot;
      state.snapshotQuery = { ...query } as typeof state.snapshotQuery;
      state.updatedAt = Date.now();
      state.status = 'ready';
    } catch {
      if (disposed || current !== generation) return;
      state.status = state.snapshot === null ? 'failed' : 'stale';
      state.error =
        state.snapshot === null
          ? '读取失败或证据合同不完整，请重试'
          : '读取失败或证据合同不完整，保留上次成功事实';
    } finally {
      if (!disposed && current === generation) state.loading = false;
    }
  }
  function dispose() {
    disposed = true;
    ++generation;
    controller?.abort();
  }
  return { state, refresh, dispose };
}
export function createSurgeReader(
  load: (query: SurgeQuery, signal: AbortSignal) => Promise<unknown>,
) {
  return createResearchReader(load, (value, query) => {
    const rows = parseSurgeList(value);
    if (
      rows.some(
        (row) =>
          (query.pair && row.pair !== query.pair) ||
          (query.timeframe && row.timeframe !== query.timeframe) ||
          (query.status && row.status !== query.status),
      )
    )
      throw failed();
    return rows;
  });
}

import { reactive } from 'vue';

export interface InventoryDataset {
  id: string;
  exchange: string;
  market: string;
  pair: string;
  timeframe: string;
  candleType: string;
  rows: number;
  firstTimestamp: null | number | string;
  lastTimestamp: null | number | string;
}

export interface InventorySource {
  key: string;
  kind: string;
  rowCount: null | number;
  updatedAt: null | number | string;
  error: null | string;
}

export interface DataInventory {
  engine: string;
  databaseName: null | string;
  schemaVersion: null | string;
  datasets: InventoryDataset[];
  sources: InventorySource[];
  counts: Record<string, null | number>;
  collectorState: string;
  collectorError: null | string;
  collectorDiskState: string;
  syncState: string;
  syncLastSuccess: null | number | string;
  syncErrors: string[];
  researchEligible: false;
  qualityChecks: {
    key: string;
    title: string;
    requirement: string;
    state: 'unavailable';
  }[];
}

const countKeys = [
  'candles',
  'ticks',
  'orderbooks',
  'series_points',
  'documents',
  'events',
  'trade_records',
  'artifacts',
];
const contractError = () =>
  new Error('数据清单合同不完整，不能将缺失信息当作空记录');

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw contractError();
  return value as Record<string, unknown>;
}

function text(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) throw contractError();
  return value;
}

function optionalText(value: unknown): null | string {
  if (value === undefined || value === null || value === '') return null;
  return text(value);
}

function count(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0)
    throw contractError();
  return value;
}

function timestamp(value: unknown): null | number | string {
  if (value === null || value === undefined) return null;
  if (
    typeof value === 'number' &&
    Number.isSafeInteger(value) &&
    value >= 0 &&
    Number.isFinite(new Date(value).getTime())
  )
    return value;
  if (typeof value === 'string') {
    const match =
      /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(?:Z|[+-](\d{2}):?(\d{2}))?$/.exec(
        value,
      );
    if (!match) throw contractError();
    const [year, month, day, hour, minute, second] = match
      .slice(1, 7)
      .map(Number);
    const calendar = new Date(Date.UTC(year!, month! - 1, day!));
    if (
      calendar.getUTCFullYear() !== year ||
      calendar.getUTCMonth() + 1 !== month ||
      calendar.getUTCDate() !== day ||
      hour! > 23 ||
      minute! > 59 ||
      second! > 59 ||
      (match[7] !== undefined && Number(match[7]) > 23) ||
      (match[8] !== undefined && Number(match[8]) > 59) ||
      !Number.isFinite(Date.parse(value))
    )
      throw contractError();
    return value;
  }
  throw contractError();
}

function milliseconds(value: number | string) {
  return typeof value === 'number' ? value : Date.parse(value);
}

/** 只校验清单事实。首末跨度和数量均不足以证明无缺口或通过研究门槛。 */
export function parseDataInventory(value: unknown): DataInventory {
  const source = object(value);
  if (!Array.isArray(source.datasets) || !Array.isArray(source.sources))
    throw contractError();
  const identities = new Set<string>();
  const datasets = source.datasets.map((raw): InventoryDataset => {
    const row = object(raw);
    const exchange = text(row.exchange),
      market = text(row.market),
      pair = text(row.pair);
    const timeframe = text(row.timeframe),
      candleType = text(row.candle_type);
    const id = JSON.stringify([exchange, market, pair, timeframe, candleType]);
    if (identities.has(id)) throw contractError();
    identities.add(id);
    const firstTimestamp = timestamp(row.first_timestamp),
      lastTimestamp = timestamp(row.last_timestamp);
    if (
      firstTimestamp !== null &&
      lastTimestamp !== null &&
      milliseconds(firstTimestamp) > milliseconds(lastTimestamp)
    )
      throw contractError();
    return {
      id,
      exchange,
      market,
      pair,
      timeframe,
      candleType,
      rows: count(row.rows),
      firstTimestamp,
      lastTimestamp,
    };
  });
  const sourceKeys = new Set<string>();
  const sources = source.sources.map((raw): InventorySource => {
    const row = object(raw);
    const key = text(row.key);
    if (sourceKeys.has(key)) throw contractError();
    sourceKeys.add(key);
    return {
      key,
      kind: text(row.kind),
      rowCount: row.row_count == null ? null : count(row.row_count),
      updatedAt: timestamp(row.updated_at),
      error: optionalText(row.error),
    };
  });
  const rawCounts = source.counts == null ? {} : object(source.counts);
  const counts = Object.fromEntries(
    countKeys.map((key) => [
      key,
      rawCounts[key] == null ? null : count(rawCounts[key]),
    ]),
  );
  const database = source.database == null ? {} : object(source.database);
  let schemaVersion: null | string = null;
  if (database.schema_version != null) {
    schemaVersion =
      typeof database.schema_version === 'number'
        ? String(count(database.schema_version))
        : text(database.schema_version);
  }
  const collector = source.collector == null ? {} : object(source.collector);
  const sync = source.sync == null ? {} : object(source.sync);
  if (sync.running !== undefined && typeof sync.running !== 'boolean')
    throw contractError();
  let syncErrors: string[] = [];
  if (typeof sync.errors === 'number') {
    const errors = count(sync.errors);
    if (errors > 0) syncErrors = [`同步报告 ${errors} 项错误，逐项详情未提供`];
  } else if (sync.errors !== undefined) {
    if (
      !Array.isArray(sync.errors) ||
      sync.errors.some((error) => typeof error !== 'string')
    )
      throw contractError();
    syncErrors = sync.errors;
  }

  return {
    engine: text(source.engine),
    databaseName: optionalText(database.name),
    schemaVersion,
    datasets,
    sources,
    counts,
    collectorState:
      optionalText(collector.state ?? collector.status) ?? 'unknown',
    collectorError: optionalText(collector.last_error ?? collector.error),
    collectorDiskState: optionalText(collector.disk_alert_level) ?? 'unknown',
    syncState:
      sync.running === true
        ? 'running'
        : sync.running === false
          ? 'stopped'
          : 'unknown',
    syncLastSuccess: timestamp(sync.last_success_at),
    syncErrors,
    researchEligible: false,
    qualityChecks: [
      {
        key: 'coverage',
        title: '365 天逐频率覆盖',
        requirement: '需要连续区间、缺口清单和数据版本',
        state: 'unavailable',
      },
      {
        key: 'closed',
        title: '闭盘与可用时间',
        requirement: '需要 closed-only 与 available-at 证据',
        state: 'unavailable',
      },
      {
        key: 'orderbook',
        title: '盘口序列连续性',
        requirement: '需要快照、增量序号与断档校验',
        state: 'unavailable',
      },
      {
        key: 'fingerprint',
        title: '冻结数据指纹',
        requirement: '需要不可变 URI、SHA-256 与质量裁定',
        state: 'unavailable',
      },
    ],
  };
}

export function selectDatasets(
  rows: InventoryDataset[],
  pair: string,
  timeframe: string,
) {
  return rows.filter(
    (row) =>
      (!pair || row.pair === pair) &&
      (!timeframe || row.timeframe === timeframe),
  );
}

export interface InventoryReadState {
  snapshot: DataInventory | null;
  loading: boolean;
  status: 'error' | 'idle' | 'loading' | 'ready' | 'stale';
  error: string;
  fetchedAt: null | number;
}

/** 生命周期持有请求，后发刷新与离开页面使旧响应失效。 */
export function createDataInventoryReader(
  read: (signal: AbortSignal) => Promise<unknown>,
) {
  const state = reactive<InventoryReadState>({
    snapshot: null,
    loading: false,
    status: 'idle',
    error: '',
    fetchedAt: null,
  });
  let generation = 0,
    controller: AbortController | undefined,
    disposed = false;
  async function refresh() {
    if (disposed) return;
    const requestId = ++generation;
    controller?.abort();
    const current = new AbortController();
    controller = current;
    state.loading = true;
    state.status = state.snapshot ? 'stale' : 'loading';
    state.error = '';
    try {
      const raw = await read(current.signal);
      if (disposed || current.signal.aborted || requestId !== generation)
        return;
      state.snapshot = parseDataInventory(raw);
      state.fetchedAt = Date.now();
      state.status = 'ready';
    } catch (error) {
      if (disposed || current.signal.aborted || requestId !== generation)
        return;
      state.error =
        error instanceof Error && error.message.startsWith('数据清单合同不完整')
          ? error.message
          : '读取失败，请检查数据服务后重试';
      state.status = state.snapshot ? 'stale' : 'error';
    } finally {
      if (requestId === generation && !disposed) state.loading = false;
    }
  }
  function dispose() {
    disposed = true;
    ++generation;
    controller?.abort();
    state.loading = false;
  }
  return { state, refresh, dispose };
}

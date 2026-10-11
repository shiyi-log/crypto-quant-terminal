/** 数据库只读接口，统一沿用前端登录会话与认证代理。 */
import { ftClient, unwrap } from '#/api/freqtrade/client';
import { requireSession as requireAuthenticatedSession } from '#/api/session';

/** 后端兼容 ISO 日期字符串和毫秒时间戳；没有时间时返回 null。 */
export type DatabaseTimestamp = null | number | string;

export interface DatabaseSource {
  error?: null | string;
  key: string;
  kind: string;
  path?: string;
  row_count?: number;
  updated_at?: DatabaseTimestamp;
}

export interface DatabaseDataset {
  candle_type: string;
  exchange: string;
  first_timestamp: DatabaseTimestamp;
  last_timestamp: DatabaseTimestamp;
  market: string;
  pair: string;
  rows: number;
  timeframe: string;
}

/** 采集器状态允许后端扩展，页面按已知字段展示。 */
export type DatabaseCollector = Record<string, unknown>;

export interface DatabaseStatus {
  collector?: DatabaseCollector;
  counts?: {
    artifacts?: number;
    candles?: number;
    documents?: number;
    events?: number;
    orderbooks?: number;
    series_points?: number;
    ticks?: number;
    trade_records?: number;
    users?: number;
  };
  database?: {
    name?: string;
    path?: string;
    schema_version?: number | string;
    size_bytes?: number;
  };
  datasets: DatabaseDataset[];
  engine: string;
  sources: DatabaseSource[];
  sync?: {
    /** 原始字符串或对象错误统一转为可直接显示的中文文本列表。 */
    errors?: string[];
    last_at?: DatabaseTimestamp;
    last_success_at?: DatabaseTimestamp;
    running?: boolean;
  };
}

export interface DatabaseMarketQuery {
  exchange?: string;
  limit?: number;
  market?: string;
  pair?: string;
}

export interface DatabaseTick {
  buyer_maker?: boolean;
  exchange?: string;
  is_buyer_maker?: boolean;
  market?: string;
  pair?: string;
  price: number | string;
  quantity: number | string;
  side?: null | string;
  timestamp: DatabaseTimestamp;
  trade_id?: number | string;
}

export interface DatabaseOrderbook {
  asks: [number | string, number | string][];
  bids: [number | string, number | string][];
  exchange?: string;
  last_update_id?: number | string;
  market?: string;
  pair?: string;
  timestamp: DatabaseTimestamp;
  update_id?: number | string;
}

export interface DatabaseItems<T> {
  items: T[];
}

/** 订单流突增的原始观测；后端会随检测器版本增加字段。 */
export interface OrderflowSurgeObservation {
  event_id: string;
  exchange?: string;
  market?: string;
  pair: string;
  timeframe?: string;
  timestamp: DatabaseTimestamp;
  observed_at?: DatabaseTimestamp;
  quote_volume?: number | string | null;
  baseline_quote_volume?: number | string | null;
  volume_ratio?: number | string | null;
  threshold_level?: string | null;
  threshold_band?: string | null;
  threshold_multiple?: number | string | null;
  threshold?: number | string | null;
  num_trades?: number | string | null;
  flow_imbal?: number | string | null;
  taker_buy_base?: number | string | null;
  taker_buy_quote?: number | string | null;
  orderbook_snapshot?: Record<string, unknown> | null;
  method_version?: string | null;
  data_fingerprint?: string | null;
  status?: string | null;
  completed_horizons?: string[];
  required_horizons?: string[];
  observation?: Record<string, unknown> | null;
  result?: Record<string, unknown> | null;
  extras?: Record<string, unknown> | null;
  payload?: Record<string, unknown> | null;
  created_at?: DatabaseTimestamp;
  [key: string]: unknown;
}

export interface OrderflowSurgeRevision {
  sequence_id?: number | string;
  revision_id?: string;
  revision_no?: number | string;
  revision_kind?: string;
  event_id?: string;
  observation_id?: string;
  event_type?: string;
  status?: string | null;
  observation?: Record<string, unknown> | null;
  result?: Record<string, unknown> | null;
  payload?: Record<string, unknown> | null;
  created_at?: DatabaseTimestamp;
  [key: string]: unknown;
}

export interface OrderflowSurgeDetail {
  observation: OrderflowSurgeObservation;
  events: OrderflowSurgeRevision[];
}

export interface OrderflowSurgeStatsBucket {
  total?: number | string;
  unknown?: number | string;
  known?: number | string;
  matured?: number | string;
  mature?: number | string;
  mature_count?: number | string;
  gross_return_mean?: number | string | null;
  gross_mean?: number | string | null;
  required_fee_rate?: number | string | null;
  actual_fee_rate?: number | string | null;
  sample_count?: number | string;
  statistics_horizon?: string;
  horizons?: Record<string, OrderflowSurgeStatsBucket>;
  [key: string]: unknown;
}

export interface OrderflowSurgeStats extends OrderflowSurgeStatsBucket {
  warmup?: Record<string, {
    valid_bars?: number;
    required_bars?: number;
    ready?: boolean;
    last_timestamp?: DatabaseTimestamp;
    source?: string | null;
  }>;
  by_pair?:
    | Record<string, OrderflowSurgeStatsBucket>
    | OrderflowSurgeStatsBucket[];
  pairs?:
    | Record<string, OrderflowSurgeStatsBucket>
    | OrderflowSurgeStatsBucket[];
  [key: string]: unknown;
}

export interface PriceSpikeCandidate {
  event_id: string;
  pair: string;
  timestamp: DatabaseTimestamp;
  observed_at?: DatabaseTimestamp;
  status?: string;
  observation?: Record<string, unknown>;
  outcome?: Record<string, unknown> | null;
  profile?: Record<string, unknown> | null;
  profile_status?: string | null;
  [key: string]: unknown;
}

export interface PriceSpikeStats {
  method_version: string;
  event_threshold_pct: number;
  forward_bars: number;
  ratio_bands: string[];
  by_pair: Record<string, Record<string, unknown>>;
  profile_by_pair: Record<string, Record<string, unknown>>;
  sample_scope: string;
  unknown_excluded: boolean;
  invalid_excluded: boolean;
  [key: string]: unknown;
}

export interface PriceSpikeResponse {
  items: PriceSpikeCandidate[];
  stats: PriceSpikeStats;
  warmup: Record<string, {
    valid_bars?: number;
    required_bars?: number;
    ready?: boolean;
    source?: string | null;
  }>;
  market: string;
  timeframe: string;
  direction: string;
  sample_scope: string;
  limitations: string[];
}

/** 未登录时立即提示，避免页面轮询反复发送无效请求。 */
function requireSession() {
  requireAuthenticatedSession('请先登录后查看数据库数据');
}

/** 同步错误可能来自文件路径或数据源键，保留这些上下文供排查。 */
function normalizeErrors(value: unknown): string[] {
  const errors = Array.isArray(value) ? value : value ? [value] : [];
  return errors.map((item) => {
    if (typeof item === 'string') {
      return item;
    }
    if (item && typeof item === 'object') {
      const error = item as { error?: unknown; key?: unknown; path?: unknown };
      const context = [error.key, error.path].filter(
        (part): part is string => typeof part === 'string' && Boolean(part),
      );
      const message =
        typeof error.error === 'string'
          ? error.error
          : JSON.stringify(error.error ?? item);
      return [...context, message].join('：');
    }
    return String(item);
  });
}

/** 查询最新行情，默认最多取 50 条；由后端负责按时间倒序。 */
function marketParams(query: DatabaseMarketQuery): DatabaseMarketQuery {
  return { ...query, limit: query.limit ?? 50 };
}

export async function getDatabaseStatus(
  signal?: AbortSignal,
): Promise<DatabaseStatus> {
  requireSession();
  const status = unwrap<DatabaseStatus>(
    await ftClient.get('/locals/database/status', { signal }),
  );
  return {
    ...status,
    datasets: Array.isArray(status.datasets) ? status.datasets : [],
    sources: Array.isArray(status.sources) ? status.sources : [],
    sync: status.sync
      ? { ...status.sync, errors: normalizeErrors(status.sync.errors) }
      : undefined,
  };
}

export async function getDatabaseTicks(
  query: DatabaseMarketQuery,
  signal?: AbortSignal,
): Promise<DatabaseItems<DatabaseTick>> {
  requireSession();
  const response = unwrap<DatabaseItems<DatabaseTick>>(
    await ftClient.get('/locals/database/ticks', {
      params: marketParams(query),
      signal,
    }),
  );
  return { items: Array.isArray(response?.items) ? response.items : [] };
}

export async function getDatabaseOrderbooks(
  query: DatabaseMarketQuery,
  signal?: AbortSignal,
): Promise<DatabaseItems<DatabaseOrderbook>> {
  requireSession();
  const response = unwrap<DatabaseItems<DatabaseOrderbook>>(
    await ftClient.get('/locals/database/orderbooks', {
      params: marketParams(query),
      signal,
    }),
  );
  return { items: Array.isArray(response?.items) ? response.items : [] };
}

export async function getOrderflowSurges(
  query: {
    end?: number;
    limit?: number;
    pair?: string;
    start?: number;
    status?: string;
    timeframe?: string;
  } = {},
  signal?: AbortSignal,
): Promise<DatabaseItems<OrderflowSurgeObservation>> {
  requireSession();
  const response = unwrap<DatabaseItems<OrderflowSurgeObservation>>(
    await ftClient.get('/locals/orderflow/surges', {
      params: { ...query, limit: query.limit ?? 100 },
      signal,
    }),
  );
  return { items: Array.isArray(response?.items) ? response.items : [] };
}

export async function getOrderflowSurge(
  eventId: string,
  signal?: AbortSignal,
): Promise<OrderflowSurgeDetail> {
  requireSession();
  const response = unwrap<Record<string, unknown>>(
    await ftClient.get(
      `/locals/orderflow/surges/${encodeURIComponent(eventId)}`,
      { signal },
    ),
  );
  const isRecord = (value: unknown): value is Record<string, unknown> =>
    Boolean(value) && typeof value === 'object' && !Array.isArray(value);
  // Older stores return the latest revision itself, with a revisions array.
  const observation =
    response.event_id !== undefined
      ? response
      : isRecord(response.observation)
        ? response.observation
        : isRecord(response.item)
          ? response.item
          : response;
  const events = response.events ?? response.revisions;
  return {
    observation: observation as OrderflowSurgeObservation,
    events: Array.isArray(events) ? events : [],
  };
}

export async function getOrderflowSurgeStats(
  query: { pair?: string } = {},
  signal?: AbortSignal,
): Promise<OrderflowSurgeStats> {
  requireSession();
  return unwrap<OrderflowSurgeStats>(
    await ftClient.get('/locals/orderflow/surge-stats', {
      params: query,
      signal,
    }),
  );
}

export async function getOrderflowPriceSpikes(
  query: { direction?: 'any' | 'up' | 'down'; limit?: number; pair?: string } = {},
  signal?: AbortSignal,
): Promise<PriceSpikeResponse> {
  requireSession();
  return unwrap<PriceSpikeResponse>(
    await ftClient.get('/locals/orderflow/price-spikes', {
      params: { ...query, limit: query.limit ?? 100 },
      signal,
    }),
  );
}

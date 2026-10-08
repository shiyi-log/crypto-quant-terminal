/** 数据库只读接口，统一沿用前端登录会话与认证代理。 */
import { FT_TOKEN_KEY, ftClient, unwrap } from '#/api/freqtrade/client';

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

/** 未登录时立即提示，避免页面轮询反复发送无效请求。 */
function requireSession() {
  const token =
    typeof localStorage === 'undefined'
      ? null
      : localStorage.getItem(FT_TOKEN_KEY);
  if (!token?.trim()) {
    throw new Error('请先登录后查看数据库数据');
  }
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

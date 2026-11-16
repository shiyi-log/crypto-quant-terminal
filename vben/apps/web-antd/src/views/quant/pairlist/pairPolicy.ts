import { reactive } from 'vue';
import { sortCoins } from '../utils/coinOrder';

export interface PairLock {
  active: boolean;
  id: number;
  pair: string;
  side: string;
  reason?: null | string;
  lock_end_time?: string;
  lock_end_timestamp?: number;
  lock_time?: string;
  lock_timestamp?: number;
}
export interface BlacklistFacts {
  errors: [string, string][];
  expanded: string[];
  methods: string[];
  pairs: string[];
}
interface Evidence<T> {
  at: null | number;
  data: null | T;
  error: null | string;
}
interface PairPolicyApi {
  add: (pairs: string[]) => Promise<unknown>;
  blacklist: () => Promise<unknown>;
  locks: () => Promise<unknown>;
  remove: (pairs: string[]) => Promise<unknown>;
  unlock: (id: number) => Promise<unknown>;
  whitelist: () => Promise<unknown>;
}
interface Command {
  errors: [string, string][];
  label: string;
  status: 'blocked' | 'received' | 'rejected' | 'unknown';
}
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw new Error('响应结构缺失');
  return value as Record<string, unknown>;
}
function strings(value: unknown): string[] {
  if (
    !Array.isArray(value) ||
    value.some((item) => typeof item !== 'string' || !item.trim())
  )
    throw new Error('名单格式无效');
  return sortCoins([...new Set(value as string[])]);
}
function errors(value: unknown): [string, string][] {
  if (value === undefined) return [];
  return Object.entries(record(value)).map(([pair, error]) => {
    if (typeof error === 'string') return [pair, error];
    const fields = record(error);
    const text = fields.error_msg ?? fields.detail;
    if (typeof text !== 'string') throw new Error('逐项回执缺失');
    return [pair, text];
  });
}
export function parseWhitelist(raw: unknown): string[] {
  return strings(record(raw).whitelist);
}
export function parseBlacklist(raw: unknown): BlacklistFacts {
  const body = record(raw);
  return {
    pairs: strings(body.blacklist),
    expanded: strings(body.blacklist_expanded),
    methods: body.method === undefined ? [] : strings(body.method),
    errors: errors(body.errors),
  };
}
export function parseLocks(raw: unknown): { count: number; rows: PairLock[] } {
  const body = record(raw);
  if (
    !Array.isArray(body.locks) ||
    !Number.isSafeInteger(body.lock_count) ||
    Number(body.lock_count) < body.locks.length
  )
    throw new Error('保护锁列表缺失');
  const ids = new Set<number>();
  const rows = body.locks.map((value) => {
    const row = record(value);
    if (
      !Number.isSafeInteger(row.id) ||
      Number(row.id) < 0 ||
      ids.has(Number(row.id)) ||
      typeof row.pair !== 'string' ||
      !row.pair ||
      typeof row.active !== 'boolean' ||
      typeof row.side !== 'string' ||
      !row.side
    )
      throw new Error('保护锁事实无效');
    ids.add(Number(row.id));
    for (const field of ['lock_timestamp', 'lock_end_timestamp'])
      if (
        row[field] !== undefined &&
        (typeof row[field] !== 'number' || !Number.isFinite(row[field]))
      )
        throw new Error('保护锁时间无效');
    for (const field of ['lock_time', 'lock_end_time', 'reason'])
      if (
        row[field] !== undefined &&
        !(field === 'reason' && row[field] === null) &&
        typeof row[field] !== 'string'
      )
        throw new Error('保护锁字段无效');
    return { ...row } as unknown as PairLock;
  });
  return {
    rows: sortCoins(rows, (row) => row.pair),
    count: Number(body.lock_count),
  };
}
export function splitPairs(raw: string): string[] {
  return sortCoins([
    ...new Set(
      raw
        .split(/[\s,，;；]+/)
        .map((part) => part.trim())
        .filter(Boolean),
    ),
  ]);
}
function empty<T>(): Evidence<T> {
  return { data: null, at: null, error: null };
}

/** 仅管理旧引擎名单；命令回执与后续读取事实分开保存。 */
export function createPairPolicy(
  api: PairPolicyApi,
  now: () => number = Date.now,
) {
  const state = reactive({
    whitelist: empty<string[]>(),
    blacklist: empty<BlacklistFacts>(),
    locks: empty<{ count: number; rows: PairLock[] }>(),
    loading: false,
    busy: false,
    command: null as Command | null,
  });
  let generation = 0;
  let disposed = false;
  async function refresh() {
    if (disposed) return;
    const current = ++generation;
    state.loading = true;
    async function read<T>(
      target: Evidence<T>,
      fetch: () => Promise<unknown>,
      parse: (raw: unknown) => T,
    ) {
      try {
        const value = parse(await fetch());
        if (disposed || current !== generation) return;
        target.data = value;
        target.at = now();
        target.error = null;
      } catch {
        if (!disposed && current === generation)
          target.error = '读取失败或响应不完整，请重试';
      }
    }
    await Promise.all([
      read(state.whitelist, api.whitelist, parseWhitelist),
      read(state.blacklist, api.blacklist, parseBlacklist),
      read(state.locks, api.locks, parseLocks),
    ]);
    if (!disposed && current === generation) state.loading = false;
  }
  function current<T>(
    evidence: Evidence<T>,
  ): evidence is Evidence<T> & { data: T } {
    return (
      evidence.data !== null &&
      evidence.at !== null &&
      !evidence.error &&
      now() - evidence.at <= 60_000
    );
  }
  async function write(
    label: string,
    run: () => Promise<unknown>,
    blacklistResponse: boolean,
  ) {
    if (disposed || state.busy) return false;
    state.busy = true;
    state.command = null;
    let success = false;
    try {
      const response = await run();
      if (!blacklistResponse) parseLocks(response);
      const details = blacklistResponse
        ? parseBlacklist(response).errors
        : errors(record(response).errors);
      if (disposed) return false;
      state.command = {
        label,
        status: details.length ? 'rejected' : 'received',
        errors: details,
      };
      success = details.length === 0;
    } catch {
      if (!disposed) state.command = { label, status: 'unknown', errors: [] };
    } finally {
      // 失败也重新读取，不自动重发未知命令。
      if (!disposed) {
        await refresh();
        state.busy = false;
      }
    }
    return success;
  }
  function blocked(label: string) {
    state.command = { label, status: 'blocked', errors: [] };
    return false;
  }
  return {
    state,
    refresh,
    async add(input: string[]) {
      if (state.busy || disposed) return false;
      if (!current(state.blacklist)) return blocked('名单未知或已过期');
      const pairs = strings(input).filter(
        (pair) => !state.blacklist.data!.pairs.includes(pair),
      );
      return pairs.length
        ? write('加入黑名单', () => api.add(pairs), true)
        : false;
    },
    async remove(input: string[]) {
      if (state.busy || disposed) return false;
      if (!current(state.blacklist)) return blocked('名单未知或已过期');
      const pairs = strings(input).filter((pair) =>
        state.blacklist.data!.pairs.includes(pair),
      );
      return pairs.length
        ? write('移出黑名单', () => api.remove(pairs), true)
        : false;
    },
    async unlock(id: number) {
      if (state.busy || disposed) return false;
      if (
        !current(state.locks) ||
        !state.locks.data.rows.some((row) => row.id === id && row.active)
      )
        return blocked('保护锁未知、过期或已失效');
      return write('解除旧引擎保护锁', () => api.unlock(id), false);
    },
    dispose() {
      disposed = true;
      ++generation;
    },
  };
}
export function lockTime(value?: number | string): string {
  if (value === undefined || value === '') return '未知';
  const ms =
    typeof value === 'number'
      ? Math.abs(value) < 1e12
        ? value * 1000
        : value
      : Date.parse(
          /^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$/.test(value)
            ? value.replace(' ', 'T') + 'Z'
            : value,
        );
  return Number.isFinite(ms)
    ? new Date(ms).toLocaleString('zh-CN', { hour12: false })
    : '未知';
}

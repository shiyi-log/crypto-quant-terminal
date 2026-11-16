import { reactive } from 'vue';

export interface FactorRow {
  name: string;
  group: string;
  baseline: null | string;
  recent: null | string;
  t: null | string;
  drift: null | string;
  alert: boolean | null;
  weak: boolean | null;
  flip: boolean | null;
  decay: boolean | null;
  status: null | string;
}

export interface FactorSnapshot {
  factors: FactorRow[];
  generatedAt: null | string;
  generatedMs: null | number;
}

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('因子快照格式不完整');
  }
  return value as Record<string, unknown>;
}

function text(value: unknown, required = false): null | string {
  if (value === undefined || value === null) {
    if (!required) return null;
    throw new Error('因子身份缺失');
  }
  if (typeof value !== 'string' || !value.trim() || value.length > 500) {
    throw new Error('因子文本格式错误');
  }
  return value.trim();
}

function metric(value: unknown): null | string {
  if (value === undefined || value === null) return null;
  if (
    (typeof value !== 'string' && typeof value !== 'number') ||
    (typeof value === 'string' &&
      (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value) ||
        value.length > 128)) ||
    !Number.isFinite(Number(value))
  ) {
    throw new Error('因子统计值格式错误');
  }
  return String(value);
}

function flag(value: unknown): boolean | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== 'boolean') throw new Error('因子标志格式错误');
  return value;
}

function generatedTime(value: null | string): null | number {
  if (!value) return null;
  const calendar =
    /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$/.exec(
      value,
    );
  if (!calendar) return null;
  const [year, month, day, hour, minute, second] = calendar
    .slice(1)
    .map(Number);
  const check = new Date(
    Date.UTC(year!, month! - 1, day!, hour!, minute!, second!),
  );
  // Date.parse 会把 2 月 30 日归一化为 3 月；源时间必须先通过日历检查。
  if (
    check.getUTCFullYear() !== year ||
    check.getUTCMonth() + 1 !== month ||
    check.getUTCDate() !== day ||
    check.getUTCHours() !== hour ||
    check.getUTCMinutes() !== minute ||
    check.getUTCSeconds() !== second
  )
    return null;
  // 旧监控协议明确生成北京时间无时区文本；不能按浏览器本地时区解释。
  const legacy = /^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})$/.exec(value);
  const timestamp = Date.parse(
    legacy ? `${legacy[1]}T${legacy[2]}+08:00` : value,
  );
  if (!legacy && !/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null;
  return Number.isFinite(timestamp) ? timestamp : null;
}

/** 历史横截面监控不是新研究准入；损坏响应不能降级成空记录。 */
export function parseFactorSnapshot(value: unknown): FactorSnapshot {
  const source = record(value);
  if (!Array.isArray(source.factors)) throw new Error('因子列表缺失');
  const seen = new Set<string>();
  const factors = source.factors.map((item) => {
    const row = record(item);
    const name = text(row.factor, true)!;
    if (seen.has(name)) throw new Error('因子身份重复');
    seen.add(name);
    return {
      name,
      group: text(row.group) || '分类未知',
      baseline: metric(row.ic_base),
      recent: metric(row.ic_recent),
      t: metric(row.t_recent),
      drift: metric(row.drift),
      alert: flag(row.alert),
      weak: flag(row.weak),
      flip: flag(row.flip),
      decay: flag(row.decay),
      status: text(row.status),
    };
  });
  const generatedAt = text(source.generated_at);
  return { factors, generatedAt, generatedMs: generatedTime(generatedAt) };
}

export function selectFactors(
  snapshot: FactorSnapshot | null,
  group: string,
  alerts: boolean,
): FactorRow[] {
  return (
    snapshot?.factors.filter(
      (row) =>
        (group === '全部' || row.group === group) &&
        (!alerts || row.alert === true),
    ) || []
  );
}

/** 一小时仅是历史监控展示时效，不授予信号或交易资格。 */
export function snapshotFreshness(
  time: null | number,
  now: number,
): 'fresh' | 'stale' | 'unknown' {
  if (time === null || !Number.isFinite(time) || time > now) return 'unknown';
  return now - time > 3_600_000 ? 'stale' : 'fresh';
}

export function createFactorReader() {
  const state = reactive<{
    snapshot: FactorSnapshot | null;
    error: string;
    loading: boolean;
  }>({
    snapshot: null,
    error: '',
    loading: false,
  });
  let generation = 0;
  let controller: AbortController | undefined;
  function cancel() {
    generation += 1;
    controller?.abort();
    state.loading = false;
  }
  async function load(read: (signal: AbortSignal) => Promise<unknown>) {
    cancel();
    const request = generation;
    controller = new AbortController();
    const signal = controller.signal;
    state.loading = true;
    try {
      const value = await read(signal);
      if (signal.aborted || request !== generation) return;
      const parsed = parseFactorSnapshot(value);
      state.snapshot = parsed;
      state.error = '';
    } catch {
      if (signal.aborted || request !== generation) return;
      // 不将上游异常文本（可能含请求地址）直接带入界面。
      state.error = state.snapshot
        ? '读取失败，保留上次成功快照'
        : '因子快照读取失败，请重试';
    } finally {
      if (request === generation) state.loading = false;
    }
  }
  return { state, load, cancel };
}

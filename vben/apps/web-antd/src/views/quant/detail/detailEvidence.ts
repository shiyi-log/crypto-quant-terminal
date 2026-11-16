export type EvidenceRow = Record<string, unknown>;
export type Target =
  | { kind: 'none' | 'invalid'; id: ''; reason: string }
  | { kind: 'identifier' | 'run_id' | 'job_id'; id: string; reason: '' };

export interface DetailEvidence {
  identifier: string;
  strategy: string;
  summary: EvidenceRow;
  trades: EvidenceRow[];
  by_pair: EvidenceRow[];
  by_exit: EvidenceRow[];
  equity: [string, number][];
  thresholds: null | EvidenceRow;
}
export function object(value: unknown): value is EvidenceRow {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
export function identity(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    value.length <= 180 &&
    /^[A-Za-z0-9][A-Za-z0-9_.:-]*$/.test(value) &&
    !value.includes('..')
  );
}
export function targetFromQuery(query: Record<string, unknown>): Target {
  if (
    Object.keys(query).some(
      (key) => !['identifier', 'run_id', 'job_id'].includes(key),
    )
  )
    return {
      kind: 'invalid',
      id: '',
      reason: '目标参数无效：存在未识别的查询参数。',
    };
  const keys = ['identifier', 'run_id', 'job_id'].filter((key) =>
    Object.hasOwn(query, key),
  );
  if (!keys.length)
    return {
      kind: 'none',
      id: '',
      reason: '选择一份历史产物，或从实验任务按运行身份进入。',
    };
  if (keys.length !== 1)
    return {
      kind: 'invalid',
      id: '',
      reason: '目标参数无效：一次只能指定一种产物身份。',
    };
  const kind = keys[0] as 'identifier' | 'run_id' | 'job_id';
  const id = query[kind];
  if (!identity(id))
    return {
      kind: 'invalid',
      id: '',
      reason: '目标参数无效：身份必须是单个有效值。',
    };
  return { kind, id, reason: '' };
}
export function checkedList(value: unknown): EvidenceRow[] {
  if (
    !object(value) ||
    !Array.isArray(value.items) ||
    !value.items.every((row) => object(row) && identity(row.identifier))
  )
    throw new Error('产物目录合同不完整');
  const rows = value.items as EvidenceRow[];
  if (new Set(rows.map((row) => row.identifier)).size !== rows.length)
    throw new Error('产物目录身份重复');
  return structuredClone(rows);
}
export function checkedDetail(
  value: unknown,
  expected: string,
): DetailEvidence {
  if (!object(value) || value.identifier !== expected)
    throw new Error('返回产物身份不匹配');
  if (
    !object(value.summary) ||
    typeof value.strategy !== 'string' ||
    !['trades', 'by_pair', 'by_exit', 'equity'].every((key) =>
      Array.isArray(value[key]),
    )
  )
    throw new Error('回测产物合同不完整');
  for (const key of ['trades', 'by_pair', 'by_exit']) {
    if (!(value[key] as unknown[]).every(object))
      throw new Error('回测产物合同不完整');
  }
  for (const row of value.equity as unknown[]) {
    if (
      !Array.isArray(row) ||
      row.length !== 2 ||
      typeof row[0] !== 'string' ||
      typeof row[1] !== 'number' ||
      !Number.isFinite(row[1])
    )
      throw new Error('资金曲线合同不完整');
  }
  if (
    value.thresholds !== undefined &&
    value.thresholds !== null &&
    !object(value.thresholds)
  )
    throw new Error('阈值合同不完整');
  return structuredClone({
    ...value,
    thresholds: value.thresholds ?? null,
  }) as unknown as DetailEvidence;
}
export function metric(value: unknown, digits?: number): string {
  if (typeof value !== 'number' && typeof value !== 'string') return '未知';
  if (
    typeof value === 'string' &&
    (!value ||
      value.length > 128 ||
      !/^[-+]?(?:[0-9]+(?:[.][0-9]*)?|[.][0-9]+)(?:[eE][-+]?[0-9]+)?$/.test(
        value,
      ))
  )
    return '未知';
  const number = Number(value);
  if (!Number.isFinite(number)) return '未知';
  return digits === undefined ? String(value) : number.toFixed(digits);
}
export function signed(value: unknown, digits = 2): string {
  const text = metric(value, digits);
  return text === '未知' ? text : (Number(value) > 0 ? '+' : '') + text;
}
export function percent(value: unknown): string {
  const text = metric(value);
  return text === '未知' ? text : signed(value) + '%';
}
export function label(value: unknown): string {
  return typeof value === 'string' && value.trim() ? value : '未知';
}
export function publicJson(value: unknown): string {
  const sanitize = (item: unknown, depth = 0): unknown => {
    if (depth > 9) return '已折叠';
    if (Array.isArray(item))
      return item.slice(0, 1000).map((entry) => sanitize(entry, depth + 1));
    if (object(item))
      return Object.fromEntries(
        Object.entries(item)
          .filter(
            ([key]) =>
              !/password|secret|token|credential|authorization|api.?key|dsn|config/i.test(
                key,
              ),
          )
          .map(([key, entry]) => [key, sanitize(entry, depth + 1)]),
      );
    if (
      typeof item === 'string' &&
      (item.includes('://') ||
        item.startsWith('/') ||
        /^[A-Za-z]:[\\/]/.test(item))
    )
      return '已隐藏路径或连接信息';
    return item;
  };
  return JSON.stringify(sanitize(value), null, 2);
}

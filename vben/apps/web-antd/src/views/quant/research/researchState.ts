/** 历史研究只做展示投影，不能据此生成策略批准或补齐成本证据。 */
export type ResearchRecord = Record<string, any>;
export interface ResearchSnapshot extends ResearchRecord {
  routes: ResearchRecord[];
  models: ResearchRecord[];
  carry: ResearchRecord | null;
}

function record(value: unknown): value is ResearchRecord {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function rows(value: unknown, key?: string): ResearchRecord[] {
  if (!Array.isArray(value) || value.some((row) => !record(row)))
    throw new Error('研究列表格式不完整');
  if (key) {
    const seen = new Set<string>();
    for (const row of value) {
      if (
        typeof row[key] !== 'string' ||
        !row[key].trim() ||
        seen.has(row[key])
      )
        throw new Error('研究身份缺失或重复');
      seen.add(row[key]);
    }
  }
  return value;
}

export function parseResearchSnapshot(value: unknown): ResearchSnapshot {
  if (!record(value)) throw new Error('研究汇总格式不完整');
  rows(value.routes, 'name');
  rows(value.models, 'identifier');
  if (value.carry !== null && !record(value.carry))
    throw new Error('Carry 研究格式不完整');
  if (value.carry) {
    for (const key of ['sweep', 'liquidation', 'buffers', 'schemes'])
      rows(value.carry[key]);
    if (value.carry.cash != null) {
      if (!record(value.carry.cash))
        throw new Error('Carry 收益拆解格式不完整');
      if ('yearly' in value.carry.cash) rows(value.carry.cash.yearly);
    }
  }
  return JSON.parse(JSON.stringify(value)) as ResearchSnapshot;
}

export function researchLevel(value: unknown): string {
  switch (value) {
    case 'bad':
      return '历史证伪';
    case 'warn':
      return '历史辅助';
    case 'good':
      return '历史推荐';
    default:
      return '结论未知';
  }
}

export function metricText(value: unknown, digits = 2, signed = false): string {
  if (
    (typeof value !== 'number' && typeof value !== 'string') ||
    String(value).trim() === ''
  )
    return '未知';
  const number = Number(value);
  if (!Number.isFinite(number)) return '未知';
  return `${signed && number > 0 ? '+' : ''}${number.toFixed(digits)}`;
}

/** 历史脚本的无时区空格格式按北京时间；其它日期必须自带明确时区。 */
export function researchTime(value: unknown): number | null {
  if (typeof value !== 'string') return null;
  const match =
    /^([0-9]{4})-([0-9]{2})-([0-9]{2})[ T]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:[.][0-9]{1,3})?(Z|[+-][0-9]{2}:[0-9]{2})?$/.exec(
      value,
    );
  if (!match) return null;
  const [year, month, day, hour, minute, second] = match
    .slice(1, 7)
    .map(Number) as [number, number, number, number, number, number];
  const calendar = new Date(Date.UTC(year, month - 1, day));
  if (
    calendar.getUTCFullYear() !== year ||
    calendar.getUTCMonth() !== month - 1 ||
    calendar.getUTCDate() !== day ||
    hour > 23 ||
    minute > 59 ||
    second > 59
  )
    return null;
  const zoned = match[7]
    ? value
    : value.includes(' ')
      ? `${value.replace(' ', 'T')}+08:00`
      : '';
  const time = Date.parse(zoned);
  return Number.isFinite(time) ? time : null;
}

export function pickResearchCandidate(
  items: ResearchRecord[],
  identity: string,
): ResearchRecord | null {
  return identity
    ? (items.find((row) => row.name === identity) ?? null)
    : (items[0] ?? null);
}

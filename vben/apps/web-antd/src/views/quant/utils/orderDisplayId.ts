/** 展示编号使用固定北京时间；交易所 ID 和交易 API 的 ID 不作改写。 */
export const ORDER_DISPLAY_TIME_ZONE = 'Asia/Shanghai';

export function parseFreqtradeTime(...values: unknown[]): Date | null {
  for (const value of values) {
    if (typeof value !== 'number' && typeof value !== 'string') continue;
    const input = typeof value === 'string' ? value.trim() : value;
    if (input === '') continue;
    let date: Date;
    if (typeof input === 'number' || /^\d+(?:\.\d+)?$/.test(input)) {
      // Freqtrade *_timestamp 的接口单位为毫秒。
      const timestamp = Number(input);
      if (!Number.isFinite(timestamp)) continue;
      date = new Date(timestamp);
    } else {
      // 无时区的 Freqtrade 日期是 UTC，不使用浏览器本地时区猜测。
      if (
        !/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$/.test(
          input,
        )
      )
        continue;
      const iso = input.replace(' ', 'T');
      date = new Date(/(?:Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`);
    }
    if (!Number.isNaN(date.getTime())) return date;
  }
  return null;
}

function timeParts(...values: unknown[]) {
  const date = parseFreqtradeTime(...values);
  if (!date) return null;
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: ORDER_DISPLAY_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date);
  return Object.fromEntries(parts.map((part) => [part.type, part.value]));
}

export function fullOrderTime(...values: unknown[]): string {
  const p = timeParts(...values);
  return p
    ? `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`
    : '—';
}

export function shortOrderTime(...values: unknown[]): string {
  const p = timeParts(...values);
  return p ? `${p.month}-${p.day} ${p.hour}:${p.minute}` : '—';
}

function sourceId(value: unknown): string | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  const id = String(value).trim();
  return id || null;
}

function baseCoin(pair: unknown, currency?: unknown): string {
  const base = typeof pair === 'string' ? pair.trim().split('/')[0] : currency;
  return typeof base === 'string' && base.trim()
    ? base.trim().toUpperCase()
    : '币种未知';
}

type RecordData = Record<string, any>;

export function orderDisplayId(
  order: RecordData,
  trade: RecordData = {},
): string {
  const p = timeParts(order.order_timestamp, order.order_date);
  const stamp = p
    ? `${p.year}${p.month}${p.day}-${p.hour}${p.minute}${p.second}`
    : '时间未知';
  const coin = baseCoin(order.pair ?? trade.pair, trade.base_currency);
  // 原始交易所号始终参与展示编号；不可用 trade_id、数组序号或当前时间代替。
  return `${stamp}-${coin}-${sourceId(order.order_id) ?? '原号未知'}`;
}

export function tradeDisplayId(trade: RecordData): string {
  const entrySide =
    trade.is_short === true ? 'sell' : trade.is_short === false ? 'buy' : null;
  const entries = Array.isArray(trade.orders)
    ? trade.orders.filter((order: unknown): order is RecordData => {
        if (!order || typeof order !== 'object' || Array.isArray(order))
          return false;
        const row = order as RecordData;
        return typeof row.ft_is_entry === 'boolean'
          ? row.ft_is_entry
          : entrySide !== null && (row.ft_order_side ?? row.side) === entrySide;
      })
    : [];
  // 加仓时仍使用最早的开仓订单；接口数组顺序变化不会切换编号。
  entries.sort((a: RecordData, b: RecordData) => {
    const ta =
      parseFreqtradeTime(a.order_timestamp, a.order_date)?.getTime() ??
      Infinity;
    const tb =
      parseFreqtradeTime(b.order_timestamp, b.order_date)?.getTime() ??
      Infinity;
    return (
      ta - tb ||
      String(a.order_id ?? '').localeCompare(String(b.order_id ?? ''))
    );
  });
  if (entries[0]) return orderDisplayId(entries[0], trade);
  const p = timeParts(trade.open_timestamp, trade.open_date);
  const stamp = p
    ? `${p.year}${p.month}${p.day}-${p.hour}${p.minute}${p.second}`
    : '时间未知';
  return `${stamp}-${baseCoin(trade.pair, trade.base_currency)}-原号未知`;
}

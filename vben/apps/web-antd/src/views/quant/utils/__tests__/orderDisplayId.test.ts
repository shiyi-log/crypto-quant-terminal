import { describe, expect, it } from 'vitest';

import {
  fullOrderDisplayId,
  fullTradeDisplayId,
  orderDisplayId,
  tradeDisplayId,
} from '../orderDisplayId';

describe('order display identifiers', () => {
  it('keeps the exchange identifier out of list labels and intact in details', () => {
    const order = {
      order_date: '2026-10-11 02:24:16',
      order_id: '9223372036854775807123',
    };
    const trade = { pair: 'BTC/USDT:USDT' };
    expect(orderDisplayId(order, trade)).toBe('20261011-102416-BTC');
    expect(fullOrderDisplayId(order, trade)).toBe(
      '20261011-102416-BTC-9223372036854775807123',
    );
    expect(order.order_id).toBe('9223372036854775807123');
  });

  it('uses the earliest entry regardless of order array order or later exits', () => {
    const first = {
      ft_is_entry: true,
      order_date: '2026-10-09 07:49:12',
      order_id: 'entry-first',
    };
    const later = {
      ft_is_entry: true,
      order_date: '2026-10-09 09:00:00',
      order_id: 'entry-later',
    };
    const exit = {
      ft_is_entry: false,
      order_date: '2026-10-11 02:24:16',
      order_id: 'exit',
    };
    const trade = {
      is_short: false,
      orders: [later, exit, first],
      pair: 'ETH/USDT:USDT',
    };
    expect(tradeDisplayId(trade)).toBe('20261009-154912-ETH');
    expect(fullTradeDisplayId(trade)).toBe(
      '20261009-154912-ETH-entry-first',
    );
    expect(trade.orders).toEqual([later, exit, first]);
    expect(tradeDisplayId({ ...trade, orders: [first, later, exit] })).toBe(
      tradeDisplayId(trade),
    );
  });

  it('falls back to the recorded trade time without inventing a source id', () => {
    const trade = {
      open_date: '2026-10-11 00:00:00',
      pair: 'SUI/USDT:USDT',
      trade_id: 123,
    };
    expect(tradeDisplayId(trade)).toBe('20261011-080000-SUI');
    expect(fullTradeDisplayId(trade)).toBe('20261011-080000-SUI-原号未知');
  });

  it('shows missing time and coin explicitly while preserving a known raw id', () => {
    const order = { order_id: 'source-only' };
    expect(orderDisplayId(order)).toBe('时间未知-币种未知');
    expect(fullOrderDisplayId(order)).toBe('时间未知-币种未知-source-only');
  });
});

import { describe, expect, it } from 'vitest';

import { parsePaperDetail, parsePaperOverview } from '../paperEvidence';

const account = (strategy_id = 's03_B15m_R50_k1.0_100x_maker') => ({
  strategy_id,
  initial_capital: '10000',
  cash: '10000',
  equity: '10000',
  realized_pnl: '0',
  unrealized_pnl: '0',
  open_positions: [],
});
const detail = () => ({
  account: account(),
  orders: [],
  trades: [],
  equity_curve: [],
  events: [],
});

describe('纸面账户真实响应合同', () => {
  it('全局执行模型、因子身份和预热计数也不能损坏', () => {
    expect(() =>
      parsePaperOverview({
        accounts: [account()],
        execution: { execution_universe: 7 },
      }),
    ).toThrow();
    expect(() =>
      parsePaperOverview({
        accounts: [
          { ...account(), definition: { factorial_factors: { strategy: 7 } } },
        ],
      }),
    ).toThrow();
    for (const warmup of [
      { ready: true, valid_bars: -1, required_bars: 2 },
      { ready: true, valid_bars: 1, required_bars: 2 },
      { valid_bars: 1.5 },
    ])
      expect(() =>
        parsePaperOverview({ accounts: [{ ...account(), warmup }] }),
      ).toThrow();
  });
  it('兼容数值字符串的完整账户与null冗余行身份', () => {
    expect(
      parsePaperOverview({ accounts: [{ ...account(), equity: '1.5e3' }] })
        .accounts[0]?.equity,
    ).toBe('1.5e3');
    expect(
      parsePaperDetail(
        { ...detail(), events: [{ event_id: 'older', strategy_id: null }] },
        account().strategy_id,
      ).events[0]?.strategy_id,
    ).toBe(account().strategy_id);
  });
  it('损坏信号数组和显式空主集合不能被兼容别名掩盖', () => {
    expect(() =>
      parsePaperDetail(
        { ...detail(), events: [{ payload: { signals: [null] } }] },
        account().strategy_id,
      ),
    ).toThrow();
    expect(() =>
      parsePaperOverview({ accounts: null, items: [account()] }),
    ).toThrow();
  });
  it('损坏文本、真假状态与非十进制资金不能进入展示', () => {
    for (const patch of [
      { label: 7 },
      { rule_hash: [] },
      { equity: '0x10' },
      { warmup: { ready: 'false' } },
      { execution: { execution_enabled: 'false' } },
      { definition: { execution_enabled: 'false' } },
    ])
      expect(() =>
        parsePaperOverview({ accounts: [{ ...account(), ...patch }] }),
      ).toThrow();
  });
  it('按币预热与嵌套记录也必须符合真假和账户身份合同', () => {
    expect(() =>
      parsePaperOverview({
        accounts: [
          { ...account(), warmup: { by_pair: { BTC: { ready: 'false' } } } },
        ],
      }),
    ).toThrow();
    expect(() =>
      parsePaperDetail(
        { ...detail(), events: [{ payload: { strategy_id: 'other' } }] },
        account().strategy_id,
      ),
    ).toThrow();
  });
  it.each([
    null,
    {},
    { accounts: null },
    { accounts: [null] },
    { accounts: [account(), account()] },
  ])('损坏账户清单不能成为成功空结果 %j', (value) => {
    expect(() => parsePaperOverview(value)).toThrow();
  });
  it('明确空结果与真实零收益可分别保留', () => {
    expect(parsePaperOverview({ accounts: [] }).accounts).toEqual([]);
    expect(
      parsePaperOverview({ accounts: [account()] }).accounts[0]?.realized_pnl,
    ).toBe('0');
  });
  it('兼容现有data信封和items别名，不吞掉损坏数值', () => {
    expect(
      parsePaperOverview({ data: { items: [account()] } }).accounts[0]
        ?.strategy_id,
    ).toBe(account().strategy_id);
    expect(() =>
      parsePaperOverview({ accounts: [{ ...account(), equity: 'NaN' }] }),
    ).toThrow();
    expect(() =>
      parsePaperOverview({ accounts: [{ ...account(), equity: true }] }),
    ).toThrow();
  });
  it('明细身份与必需集合均须匹配，缺失集合不表示空档案', () => {
    expect(() => parsePaperDetail(detail(), 'different')).toThrow();
    expect(() =>
      parsePaperDetail(
        { ...detail(), trades: undefined },
        account().strategy_id,
      ),
    ).toThrow();
    expect(() =>
      parsePaperDetail({ ...detail(), orders: [null] }, account().strategy_id),
    ).toThrow();
  });
  it('旧事件缺少冗余策略ID可按已验证作用域标注，跨账户数据拒绝', () => {
    const parsed = parsePaperDetail(
      { ...detail(), events: [{ event_id: 'old-event' }] },
      account().strategy_id,
    );
    expect(parsed.events[0]?.strategy_id).toBe(account().strategy_id);
    expect(() =>
      parsePaperDetail(
        { ...detail(), orders: [{ strategy_id: 'other' }] },
        account().strategy_id,
      ),
    ).toThrow();
    expect(() =>
      parsePaperDetail(
        {
          ...detail(),
          account: { ...account(), open_positions: [{ strategy_id: 'other' }] },
        },
        account().strategy_id,
      ),
    ).toThrow();
  });
});

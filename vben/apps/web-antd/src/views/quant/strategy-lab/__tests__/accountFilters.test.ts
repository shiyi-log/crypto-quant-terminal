import type { StrategyLabAccount, StrategyLabDetail } from '#/api/strategy-lab';

import { describe, expect, it } from 'vitest';

import {
  canonicalLabStrategy,
  filterLabAccounts,
  labAccountPopulation,
  labWarmupCandleTime,
  selectLabAccount,
  selectedLabDetail,
  selectedLabExecution,
} from '../accountFilters';
import { labFamily, labResearchReason } from '../displayLabels';

function account(id: string, extra: Partial<StrategyLabAccount> = {}): StrategyLabAccount {
  return {
    strategy_id: id,
    initial_capital: 10000,
    cash: 10000,
    equity: 10000,
    realized_pnl: 0,
    unrealized_pnl: 0,
    status: 'observing',
    open_positions: [],
    ...extra,
  };
}

const canonicals = Array.from({ length: 30 }, (_, index) => index + 1)
  .filter((number) => number !== 15)
  .map((number) => ({
    3: 's03_tsmom_vol_target',
    5: 's05_bollinger_fade',
    11: 's11_tsmom_bollinger_pullback',
  })[number] ?? `s${String(number).padStart(2, '0')}_fixture`);

const factorial = canonicals.flatMap((canonical) =>
  (['R50', 'R200', 'R1000'] as const).flatMap((risk) =>
    [1, 1.5, 2.5].map((k) => account(`${canonical}_B15m_${risk}_k${k.toFixed(1)}_100x_maker`, {
      definition: {
        variant_of: canonical,
        factorial_design_version: 'atr-risk-full-factorial-v1',
        factorial_factors: { strategy: canonical, risk_budget: risk, stop_width_k: k },
        // A blocked account is still a registered factorial identity.
        execution_enabled: !canonical.startsWith('s04'),
      },
    })),
  ),
);
const controls = canonicals.map((canonical) => account(`${canonical}_B15m_100x_maker`, {
  execution: { max_loss_per_trade_usdt: 50, risk_stop_pct: 0.005 },
}));
const archived = canonicals.slice(0, 20).map((canonical) => account(`${canonical}_100x`, {
  status: 'archived', lifecycle: 'archived',
}));
const allAccounts = [...factorial, ...controls, ...archived];

describe('strategy lab account populations', () => {
  it('keeps all 261 factor identities and narrows default R50/k1.5 to 29 tabs', () => {
    expect(filterLabAccounts(allAccounts, 'factorial', 'all', 'all')).toHaveLength(261);
    expect(filterLabAccounts(allAccounts, 'factorial', 'R50', 'all')).toHaveLength(87);
    expect(filterLabAccounts(allAccounts, 'factorial', 'all', '1.5')).toHaveLength(87);
    const defaults = filterLabAccounts(allAccounts, 'factorial', 'R50', '1.5');
    expect(defaults).toHaveLength(29);
    expect(new Set(defaults.map(canonicalLabStrategy)).size).toBe(29);
    expect(defaults.find((item) => item.strategy_id.startsWith('s04'))?.definition?.execution_enabled).toBe(false);
  });

  it('keeps fixed-R50 old controls and archives outside factorial totals', () => {
    expect(filterLabAccounts(allAccounts, 'controls', 'R1000', '2.5')).toEqual(controls);
    expect(filterLabAccounts(allAccounts, 'archived', 'R50', '1.5')).toEqual(archived);
    expect(labAccountPopulation(account('s07_surge_continuation_B15m_R50_k1.5_100x_maker', {
      definition: { factorial_design_version: 'atr-risk-full-factorial-v1', archive_status: 'keep_pending_joint_review' },
    }))).toBe('factorial');
  });

  it('resolves focused strategies from canonical metadata and retains all variants', () => {
    const focus = allAccounts.filter((item) =>
      ['s03_tsmom_vol_target', 's05_bollinger_fade'].includes(canonicalLabStrategy(item)),
    );
    expect(filterLabAccounts(focus, 'factorial', 'all', 'all')).toHaveLength(18);
    expect(filterLabAccounts(focus, 'factorial', 'R50', '1.5')).toHaveLength(2);
    expect(canonicalLabStrategy(account('custom_identity', {
      definition: { variant_of: 's03_tsmom_vol_target' },
    }))).toBe('s03_tsmom_vol_target');
    expect(canonicalLabStrategy('s05_bollinger_fade_B15m_R1000_k2.5_100x_maker')).toBe('s05_bollinger_fade');
    expect(canonicalLabStrategy('s11_tsmom_bollinger_pullback_B15m_100x_maker')).toBe('s11_tsmom_bollinger_pullback');
  });

  it('preserves the full selected account ID or chooses a valid visible replacement', () => {
    const defaults = filterLabAccounts(allAccounts, 'factorial', 'R50', '1.5');
    const current = defaults.find((item) => canonicalLabStrategy(item) === 's05_bollinger_fade')!.strategy_id;
    expect(selectLabAccount(defaults, current, false)).toBe(current);
    const changed = filterLabAccounts(allAccounts, 'factorial', 'R200', '2.5');
    expect(selectLabAccount(changed, current, true)).toBe('s03_tsmom_vol_target_B15m_R200_k2.5_100x_maker');
    expect(selectLabAccount([], current, false)).toBe('');
  });
});

describe('selected account detail and execution model', () => {
  it('shows the closed warmup input time before any execution candle exists', () => {
    const latestInput = 1791738900000;
    const selected = account('s11_tsmom_bollinger_pullback_B15m_R50_k1.5_100x_maker', {
      warmup: { ready: true, valid_bars: 750, required_bars: 290, last_closed_at: latestInput },
      last_candle_ms: null,
    });
    expect(labWarmupCandleTime(selected)).toBe(latestInput);
    expect(selected.last_candle_ms).toBeNull();
  });

  it('keeps warmup input and processed candle timestamps distinct', () => {
    const selected = account('fixture', {
      warmup: { last_closed_at: '2026-10-11T17:15:00Z' },
      last_candle_ms: 1791738000000,
      last_observed_at_ms: 1791739800000,
    });
    expect(labWarmupCandleTime(selected)).toBe(Date.parse('2026-10-11T17:15:00Z'));
    expect(selected.last_candle_ms).toBe(1791738000000);
    expect(labWarmupCandleTime(account('no_input_timestamp', {
      warmup: { ready: true }, last_candle_ms: 1791738900000,
      last_observed_at_ms: 1791739800000,
    }))).toBeNull();
    expect(labWarmupCandleTime(account('invalid_timestamp', {
      warmup: { last_closed_at: 'invalid' },
    }))).toBeNull();
  });

  it('translates the actual s11 frozen family and other retained mechanism families', () => {
    expect(labFamily('hybrid / trend-filtered mean-reversion')).toBe('组合 · 趋势过滤与均值回归');
    const retainedFamilies = [
      'volatility-regime / breakout', 'trend / multi-horizon', 'volatility / squeeze',
      'calendar / seasonality', 'mean-reversion / range-boundary', 'path-dependent / drawdown',
      'breadth / market-regime', 'intraday-position / momentum', 'mean-reversion / exhaustion',
      'confirmation / trend', 'confirmation / breakout', 'cross-sectional / relative-value',
      'volatility-conditional / momentum', 'volatility-conditional / trend',
      'trend / weekly', 'trend + exit-rule', 'cross-asset / confirmation',
    ];
    for (const family of retainedFamilies) expect(labFamily(family), family).not.toBe('未知机制');
  });

  it('rejects a previous detail for a different R/k identity or an empty filter', () => {
    const stored: StrategyLabDetail = {
      account: factorial[0]!, orders: [], trades: [], equity_curve: [], events: [],
    };
    expect(selectedLabDetail(stored, stored.account.strategy_id)).toBe(stored);
    expect(selectedLabDetail(stored, factorial[1]!.strategy_id)).toBeNull();
    expect(selectedLabDetail(stored, '')).toBeNull();
  });

  it('uses the selected model for actual R/k, traded pairs and fees', () => {
    const global = { max_loss_per_trade_usdt: 50, risk_stop_pct: 0.005, fee_rate_per_side: 0.0005 };
    const own = {
      max_loss_per_trade_usdt: 1000, risk_stop_atr_mult: 2.5,
      fee_rate_per_side: 0.0002, execution_universe: ['ETH/USDT:USDT'],
    };
    const selected = account('s10_lead_lag_btc_eth_B15m_R1000_k2.5_100x_maker', { execution: own });
    expect(selectedLabExecution(selected, global)).toBe(own);
    expect(selectedLabExecution(selected, global).risk_stop_pct).toBeUndefined();
  });

  it('does not substitute legacy R50 when an ATR account lacks its execution projection', () => {
    const global = { max_loss_per_trade_usdt: 50, risk_stop_pct: 0.005, leverage: 100 };
    const selected = account('s03_tsmom_vol_target_B15m_R200_k1.5_100x_maker', {
      definition: {
        factorial_design_version: 'atr-risk-full-factorial-v1',
        max_loss_per_trade_usdt: 200,
        params_frozen: { risk_stop_atr_mult: 1.5 },
      },
    });
    expect(selectedLabExecution(selected, global)).toMatchObject({
      max_loss_per_trade_usdt: 200, risk_stop_atr_mult: 1.5,
    });
    expect(selectedLabExecution(selected, global).risk_stop_pct).toBeUndefined();
    expect(selectedLabExecution(controls[0], global)).toBe(controls[0]?.execution);
    expect(selectedLabExecution(account('legacy_without_model'), global)).toBe(global);
  });

  it('explains pair-stop and paused 20-coin context blockers in Chinese', () => {
    expect(labResearchReason('paired_atr_stop_policy_unimplemented')).toContain('配对级 ATR');
    expect(labResearchReason('paused_research_context_required')).toContain('20 币');
    expect(labResearchReason('cross_sectional_portfolio_execution_unimplemented')).toContain('组合执行');
  });
});

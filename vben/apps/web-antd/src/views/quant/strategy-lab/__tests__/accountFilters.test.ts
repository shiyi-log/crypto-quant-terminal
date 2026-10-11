import type { StrategyLabAccount, StrategyLabDetail } from '#/api/strategy-lab';

import { describe, expect, it } from 'vitest';

import {
  canonicalLabStrategy,
  filterLabAccounts,
  labAccountPopulation,
  labAccountFactors,
  labBookGroup,
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
const newCanonicals = [
  's32_cointegration_spread_1h', 's33_basis_momentum_1h',
  's34_vwap_anchor_reversion_15m', 's35_cascade_continuation_15m',
  's36_correlation_regime_1h',
];
const fnew = newCanonicals.flatMap((canonical) =>
  (['R50', 'R200', 'R1000'] as const).flatMap((risk) =>
    [1, 1.5, 2.5].map((k) => account(`${canonical}_Fnew_${risk}_k${k.toFixed(1)}_100x_${canonical.startsWith('s33') ? 'taker' : 'maker'}`, {
      definition: {
        canonical_strategy_id: canonical,
        fnew_design_version: 'fnew-20261012-v1', sample_population: 'fnew-20261012-v1',
        fnew_factors: { strategy: canonical, risk_budget: risk, stop_width_k: k },
        execution_enabled: !canonical.startsWith('s32') && !canonical.startsWith('s36'),
      },
    })),
  ),
);
const microstructure = (['control', 'filter'] as const).flatMap((group) =>
  (['R50', 'R200', 'R1000'] as const).flatMap((risk) =>
    [1, 1.5, 2.5].map((k) => account(`s37_vol_compression_book_${group}_${risk}_k${k.toFixed(1)}_100x_maker`, {
      definition: {
        canonical_strategy_id: 's14_vol_compression_release',
        microstructure_experiment_version: 'orderbook-confirmation-forward-v1',
        microstructure_group: group,
        factorial_factors: { strategy: 's37_vol_compression_book', risk_budget: risk, stop_width_k: k, book_group: group },
      },
    })),
  ),
);
const expandedAccounts = [...allAccounts, ...fnew, ...microstructure];

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

describe('new candidate and book comparison populations', () => {
  it('keeps frozen populations unchanged while Fnew adds its own 45 identities', () => {
    expect(filterLabAccounts(expandedAccounts, 'factorial', 'all', 'all')).toEqual(factorial);
    expect(filterLabAccounts(expandedAccounts, 'factorial', 'R50', '1.5')).toHaveLength(29);
    expect(filterLabAccounts(expandedAccounts, 'controls', 'R50', '1.5')).toEqual(controls);
    expect(filterLabAccounts(expandedAccounts, 'archived', 'all', 'all')).toEqual(archived);
    expect(filterLabAccounts(expandedAccounts, 'fnew', 'all', 'all')).toHaveLength(45);
    expect(filterLabAccounts(expandedAccounts, 'fnew', 'R200', 'all')).toHaveLength(15);
    expect(filterLabAccounts(expandedAccounts, 'fnew', 'all', '2.5')).toHaveLength(15);
    expect(filterLabAccounts(expandedAccounts, 'fnew', 'R50', '1.5')).toHaveLength(5);
  });

  it('recognizes the active s33 taker identity without inferring archive from its fee suffix', () => {
    const id = 's33_basis_momentum_1h_Fnew_R1000_k2.5_100x_taker';
    expect(canonicalLabStrategy(id)).toBe('s33_basis_momentum_1h');
    expect(labAccountPopulation(account(id))).toBe('fnew');
    expect(labAccountFactors(account(id))).toEqual({ riskBudget: 'R1000', stopWidth: 2.5 });
    expect(labAccountPopulation(account(id, { lifecycle: 'archived' }))).toBe('archived');
  });

  it('keeps all 18 book accounts separate from legacy controls and filters their treatment', () => {
    expect(filterLabAccounts(expandedAccounts, 'microstructure', 'all', 'all')).toHaveLength(18);
    const pair = filterLabAccounts(expandedAccounts, 'microstructure', 'R50', '1.5');
    expect(pair).toHaveLength(2);
    expect(pair.map(labBookGroup)).toEqual(['control', 'filter']);
    const control = filterLabAccounts(expandedAccounts, 'microstructure', 'R50', '1.5', 'control');
    const filtered = filterLabAccounts(expandedAccounts, 'microstructure', 'R50', '1.5', 'filter');
    expect(control).toHaveLength(1);
    expect(filtered).toHaveLength(1);
    expect(canonicalLabStrategy(control[0]!)).toBe(canonicalLabStrategy(filtered[0]!));
    expect(selectLabAccount(filtered, control[0]!.strategy_id, false)).toBe(filtered[0]!.strategy_id);
    expect(selectedLabDetail({ account: control[0]!, orders: [], trades: [], equity_curve: [], events: [] }, filtered[0]!.strategy_id)).toBeNull();
    expect(canonicalLabStrategy(filtered[0]!.strategy_id)).toBe('s37_vol_compression_book');
  });

  it('uses actual new-account R/k/fees and protects missing projections from legacy defaults', () => {
    const global = { max_loss_per_trade_usdt: 50, risk_stop_pct: 0.005, fee_rate_per_side: 0.0002 };
    const own = { max_loss_per_trade_usdt: 1000, risk_stop_atr_mult: 2.5, fee_rate_per_side: 0.0005 };
    const candidate = account('s33_basis_momentum_1h_Fnew_R1000_k2.5_100x_taker', { execution: own });
    expect(selectedLabExecution(candidate, global)).toBe(own);
    const withoutProjection = account(candidate.strategy_id, {
      definition: { fnew_design_version: 'fnew-20261012-v1', max_loss_per_trade_usdt: 1000,
        fee_rate_per_side: 0.0005, params_frozen: { risk_stop_atr_mult: 2.5 } },
    });
    expect(selectedLabExecution(withoutProjection, global)).toMatchObject(own);
    expect(selectedLabExecution(withoutProjection, global).risk_stop_pct).toBeUndefined();
    const book = account('book_metadata_identity', {
      definition: { microstructure_experiment_version: 'orderbook-confirmation-forward-v1', max_loss_per_trade_usdt: 200,
        params: { risk_stop_atr_mult: 1.5 } },
    });
    expect(labAccountFactors(book)).toEqual({ riskBudget: 'R200', stopWidth: 1.5 });
    expect(selectedLabExecution(book, global).max_loss_per_trade_usdt).toBe(200);
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

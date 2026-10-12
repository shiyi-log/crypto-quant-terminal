"""Causal signals for independent forward paper accounts.

This module never creates fills. ``None`` is hold/no instruction; zero is a
close instruction. Decisions use completed candles for the strategy's native
timeframe and actual paper positions. Historical candles initialize
indicators only: the caller invokes ``evaluate`` for new live closes after its
persisted startup boundary.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from statistics import mean, median, stdev
from typing import Any

BAR_MS = 300_000
DAY_MS = 86_400_000
TIMEFRAME_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000,
                "1d": 86_400_000}
BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"
UNIVERSE = [BTC, ETH]
SCHEMA_VERSION = "multi-strategy-signal-v1"
ALGORITHM_VERSION = "closed5m-prior-baseline-actual-position-v1"
IMPLEMENTATION_REVISION = "decimal-threshold-correction-v2"

# The focused leverage/stop experiment keeps execution identity in the
# strategy ID while sharing the underlying signal mechanism.  These suffixes
# are deliberately enumerated so an unsupported arm or leverage cannot be
# silently treated as a supported base strategy.
_TWO_BY_TWO_SUFFIXES = (
    "_2x2_A_L100_close",
    "_2x2_B_L30_close",
    "_2x2_C_L100_intrabar",
    "_2x2_D_L30_intrabar",
)

# Native bars for the later research arms.  The original arm remains 5m by
# default for backwards compatibility; these identities are explicitly daily.
NATIVE_TIMEFRAMES = {
    "s12_surge_expansion_breakout": "15m",
    "s13_daily_trend_multi_horizon": "1d",
    "s14_vol_compression_release": "1h",
    "s16_weekly_seasonality": "1h",
    "s17_overnight_range_fade": "15m",
    "s18_drawdown_recovery": "1h",
    "s19_multi_coin_breadth": "1d",
    "s20_intraday_range_position": "15m",
    "s21_liquidation_cascade_fade": "15m",
    "s22_dual_confirmation_trend": "1d",
    "s23_triple_confirmation_breakout": "1d",
    "s24_cross_sectional_lowvol": "1d",
    "s25_vol_regime_momentum": "1d",
    "s26_surge_gated_trend": "1d",
    "s27_long_horizon_mean_reversion": "1d",
    "s28_weekly_breakout": "1d",
    "s29_trend_with_trailing": "1d",
    "s30_dual_asset_confirmation": "1d",
}

DEFAULTS = {
    "s01_orb_breakout": ({"N_bars": 12, "one_entry_per_day": True,
                           "force_flat_at_day_end": True}, 60),
    "s02_dual_thrust": ({"range_window": 20, "k": 0.5, "H": 48}, 60),
    "s03_tsmom_vol_target": ({"L": 288, "W": 288, "target_vol_annual": 0.2,
                              "size_multiplier_max": 1.0,
                              "rebalance": "entry_or_flip_only"}, 600),
    "s04_btc_eth_pairs": ({"L": 96, "T": 0.004, "exit_band": 0.001, "H": 96}, 300),
    "s05_bollinger_fade": ({"N": 96, "Z": 2.0, "H": 96}, 300),
    "s06_volume_climax_reversal": ({"VR": 8.0, "MV3": 0.01, "mv_window": 3,
                                     "volume_window": 288, "H": 12}, 400),
    "s07_surge_continuation": ({"I": 0.2, "H": 6, "volume_window": 60,
                                 "min_volume_multiple": 0.5}, 100),
    "s08_wick_rejection": ({"W": 0.6, "H": 12, "atr_window": 14,
                             "stop_atr_mult": 1.0, "tp_atr_mult": 1.5}, 100),
    "s09_trend_pullback": ({"ma_fast": 24, "ma_slow": 288, "S": 48,
                             "band": 0.5, "atr_window": 14, "H": 288}, 600),
    "s10_lead_lag_btc_eth": ({"R": 0.004, "H": 3}, 100),
    # A distinct trend-filtered mean-reversion mechanism: long-term
    # momentum chooses the side, while a Bollinger deviation supplies the
    # pullback entry.  It is intentionally a new identity rather than an
    # alias for either component strategy.
    "s11_tsmom_bollinger_pullback": ({
        "L": 288, "W": 288, "target_vol_annual": 0.2,
        "size_multiplier_max": 1.0, "rebalance": "entry_or_flip_only",
        "N": 96, "Z": 2.0, "H": 96,
    }, 600),
    # Generation-2 mechanisms are declared here as research identities.  The
    # execution service currently supplies closed 5m BTC/ETH bars; their
    # native 15m/1h/1d adapters are added below without changing gen-1 rules.
    "s12_surge_expansion_breakout": ({"VR": 10.0, "window": 96,
                                      "valid_bars": 24, "H": 48}, 200),
    "s13_daily_trend_multi_horizon": ({"channels": (10, 20, 40), "H": 120}, 200),
    "s14_vol_compression_release": ({"atr_window": 14, "compression_lookback": 100,
                                      "pctile": 0.2, "H": 72}, 300),
    "s16_weekly_seasonality": ({"entry_hours_utc": (0, 8, 16), "H": 8,
                                 "mode": "momentum"}, 100),
    "s17_overnight_range_fade": ({"H": 32}, 100),
    "s18_drawdown_recovery": ({"N": 168, "X": 0.08, "H": 72}, 200),
    "s19_multi_coin_breadth": ({"ma": 20, "hi": 0.7, "lo": 0.3, "H": 20}, 60),
    "s20_intraday_range_position": ({"H": 32, "hi": 0.9, "lo": 0.1}, 100),
    "s21_liquidation_cascade_fade": ({"k": 4, "M": 0.02, "H": 16}, 100),
    "s22_dual_confirmation_trend": ({"channel": 20, "vr_threshold": 1.5,
                                       "H": 60}, 120),
    "s23_triple_confirmation_breakout": ({"channel": 20, "vr_threshold": 1.5,
                                            "atr_window": 14, "atr_ma": 50,
                                            "H": 60}, 150),
    "s24_cross_sectional_lowvol": ({"rv_window": 20, "k": 3, "channel": 20}, 120),
    "s25_vol_regime_momentum": ({"rv_window": 20, "regime_window": 60,
                                   "channel": 20, "H": 40}, 150),
    "s26_surge_gated_trend": ({"vr_threshold": 2.0, "surge_window": 5,
                                "gate_valid_days": 10, "channel": 20}, 120),
    "s27_long_horizon_mean_reversion": ({"N": 60, "Z": 2.5,
                                          "stop_z": 4.0, "H": 60}, 150),
    "s28_weekly_breakout": ({"weekly_channel": 84, "H": 120}, 200),
    "s29_trend_with_trailing": ({"channel": 20, "trail_dd": 0.12}, 120),
    "s30_dual_asset_confirmation": ({"channel": 20, "H": 60}, 120),
}

# Fixed-entry ATR protection is limited to the 23 agreed single-asset rules.
# s08 retains its existing stop/take-profit contract; cross-asset rules are
# handled separately and must not acquire a single-leg exit here.
ATR_HARD_STOP_STRATEGIES = frozenset({
    "s01_orb_breakout", "s02_dual_thrust", "s03_tsmom_vol_target",
    "s05_bollinger_fade", "s06_volume_climax_reversal", "s07_surge_continuation",
    "s09_trend_pullback", "s11_tsmom_bollinger_pullback",
    "s12_surge_expansion_breakout", "s13_daily_trend_multi_horizon",
    "s14_vol_compression_release", "s16_weekly_seasonality",
    "s17_overnight_range_fade", "s18_drawdown_recovery",
    "s20_intraday_range_position", "s21_liquidation_cascade_fade",
    "s22_dual_confirmation_trend", "s23_triple_confirmation_breakout",
    "s25_vol_regime_momentum", "s26_surge_gated_trend",
    "s27_long_horizon_mean_reversion", "s28_weekly_breakout",
    "s29_trend_with_trailing",
})
LEGACY_DEFAULTS = dict(DEFAULTS)
DEFAULTS = {
    strategy_id: ({**params, "atr_window": params.get("atr_window", 14),
                   "risk_stop_atr_mult": 1.5}, minimum)
    if strategy_id in ATR_HARD_STOP_STRATEGIES else (params, minimum)
    for strategy_id, (params, minimum) in DEFAULTS.items()
}


def _canonical_strategy_id(strategy_id: Any) -> str:
    """Resolve explicitly versioned execution identities to their mechanism.

    The suffix remains part of the normalized identity and rule hash, while the
    signal implementation is shared with the frozen research mechanism.
    """
    value = str(strategy_id)
    for suffix in _TWO_BY_TWO_SUFFIXES:
        if value.endswith(suffix):
            return value[:-len(suffix)]
    for suffix in ("_B15m_100x_maker", "_Cgen2_100x_maker",
                   "_Dgen3_100x_maker", "_100x_maker", "_100x", "_v2"):
        if value.endswith(suffix):
            return value[:-len(suffix)]
    return value


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(body.encode()).hexdigest()


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"{name} must be a finite positive number")
    return number


def _integer(value: Any, name: str, *, positive: bool = True) -> int:
    number = _number(value, name)
    if number != int(number) or (positive and number < 1):
        raise ValueError(f"{name} must be an integer")
    return int(number)


def _required_bars(strategy_id: str, params: dict[str, Any]) -> int:
    strategy_id = _canonical_strategy_id(strategy_id)
    if strategy_id == "s01_orb_breakout":
        return params["N_bars"] + 1
    if strategy_id == "s02_dual_thrust":
        return params["range_window"] + 2
    if strategy_id == "s03_tsmom_vol_target":
        return max(params["L"] + 1, params["W"] + 2)
    if strategy_id == "s11_tsmom_bollinger_pullback":
        return max(params["L"] + 1, params["W"] + 2, params["N"] + 1)
    if strategy_id == "s04_btc_eth_pairs":
        return params["L"] + 1
    if strategy_id == "s05_bollinger_fade":
        return params["N"] + 1
    if strategy_id == "s06_volume_climax_reversal":
        return max(params["volume_window"] + 1, params["mv_window"] + 1)
    if strategy_id == "s07_surge_continuation":
        return params["volume_window"] + 1
    if strategy_id == "s08_wick_rejection":
        return params["atr_window"] + 2
    if strategy_id == "s09_trend_pullback":
        return max(params["ma_slow"] + params["S"] + 1,
                   params["ma_fast"] + 1, params["atr_window"] + 2)
    if strategy_id == "s12_surge_expansion_breakout":
        return params["window"] + params["valid_bars"] + 1
    if strategy_id == "s13_daily_trend_multi_horizon":
        return max(params["channels"]) + 2
    if strategy_id == "s14_vol_compression_release":
        return params["atr_window"] + params["compression_lookback"] + 2
    if strategy_id == "s16_weekly_seasonality":
        return 3
    if strategy_id == "s17_overnight_range_fade":
        return 33
    if strategy_id == "s18_drawdown_recovery":
        return params["N"] + 2
    if strategy_id == "s19_multi_coin_breadth":
        return params["ma"] + 1
    if strategy_id == "s20_intraday_range_position":
        return 2
    if strategy_id == "s21_liquidation_cascade_fade":
        return params["k"] + 1
    if strategy_id == "s22_dual_confirmation_trend":
        return max(params["channel"] + 1, 22)
    if strategy_id == "s23_triple_confirmation_breakout":
        # New ATR-risk rules require the entire configured prior window;
        # legacy rules retain the lookback used by their frozen identity.
        buffer = 2 if "risk_stop_atr_mult" in params else 1
        return max(params["channel"] + 1,
                   params["atr_window"] + params["atr_ma"] + buffer, 22)
    if strategy_id == "s24_cross_sectional_lowvol":
        return max(params["rv_window"] + params["channel"] + 1, 22)
    if strategy_id == "s25_vol_regime_momentum":
        buffer = 2 if "risk_stop_atr_mult" in params else 1
        return max(params["channel"] + 1,
                   params["rv_window"] + params["regime_window"] + buffer)
    if strategy_id == "s26_surge_gated_trend":
        return max(params["channel"] + 1,
                   params["channel"] + params["gate_valid_days"] + 1)
    if strategy_id == "s27_long_horizon_mean_reversion":
        return params["N"] + 1
    if strategy_id == "s28_weekly_breakout":
        return params["weekly_channel"] + 1
    if strategy_id == "s29_trend_with_trailing":
        return params["channel"] + 1
    if strategy_id == "s30_dual_asset_confirmation":
        return params["channel"] + 1
    return 2


def normalize_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze executable semantics; exclude research claims and annotations.

    Changing an executable field under an existing rule_hash is rejected. The
    rule includes capital, fees, fill timing, unknown costs and indicator
    conventions as well as parameters. Same-side momentum targets do not
    rebalance a position; size_multiplier applies only on entry or flip.
    """
    if not isinstance(spec, Mapping):
        raise ValueError("strategy spec must be a mapping")
    strategy_id = spec.get("strategy_id")
    canonical_id = _canonical_strategy_id(strategy_id)
    is_2x2 = any(str(strategy_id).endswith(suffix) for suffix in _TWO_BY_TWO_SUFFIXES)
    experimental_2x2 = is_2x2 and spec.get("execution_variant") == "risk-execution-2x2-v1"
    if canonical_id not in DEFAULTS:
        raise ValueError(f"unsupported strategy_id: {strategy_id}")
    supplied = spec.get("params", spec.get("params_frozen", {}))
    # ATR protection is an explicit rule change. Frozen legacy definitions
    # must retain both their original parameter set and their original hash.
    atr_risk_enabled = (canonical_id in ATR_HARD_STOP_STRATEGIES
                        and isinstance(supplied, Mapping)
                        and "risk_stop_atr_mult" in supplied)
    defaults, minimum = (DEFAULTS if atr_risk_enabled else LEGACY_DEFAULTS)[canonical_id]
    if not isinstance(supplied, Mapping) or set(supplied) - set(defaults):
        raise ValueError("unknown or invalid executable params")
    params = {**defaults, **supplied}
    for key, value in params.items():
        if isinstance(defaults[key], bool):
            if value is not True:
                raise ValueError(f"{key} must remain true")
        elif isinstance(defaults[key], str):
            if value != defaults[key]:
                raise ValueError(f"unsupported {key}")
        elif isinstance(defaults[key], (list, tuple)):
            if not isinstance(value, (list, tuple)) or not value:
                raise ValueError(f"{key} must be a non-empty sequence")
            params[key] = tuple(_integer(item, f"{key} item", positive=False) for item in value)
        elif isinstance(defaults[key], int):
            params[key] = _integer(value, key)
        else:
            params[key] = _number(value, key, positive=True)
    if canonical_id == "s03_tsmom_vol_target" and params["size_multiplier_max"] > 1:
        raise ValueError("size_multiplier_max cannot exceed one")
    if canonical_id == "s11_tsmom_bollinger_pullback" and params["size_multiplier_max"] > 1:
        raise ValueError("size_multiplier_max cannot exceed one")
    if canonical_id == "s07_surge_continuation" and params["I"] >= 1:
        raise ValueError("imbalance threshold must be below one")
    if canonical_id == "s08_wick_rejection" and not 0.5 < params["W"] < 1:
        raise ValueError("wick threshold must be above half and below one")
    if canonical_id in {"s22_dual_confirmation_trend", "s23_triple_confirmation_breakout",
                        "s25_vol_regime_momentum", "s26_surge_gated_trend",
                        "s30_dual_asset_confirmation"} and params["channel"] < 2:
        raise ValueError("channel must contain at least two bars")
    if canonical_id == "s24_cross_sectional_lowvol":
        if params["k"] < 1:
            raise ValueError("cross-sectional selection k must be positive")
    if canonical_id == "s27_long_horizon_mean_reversion":
        if params["stop_z"] <= params["Z"]:
            raise ValueError("stop_z must exceed entry Z")
    if canonical_id == "s29_trend_with_trailing" and not 0 < params["trail_dd"] < 1:
        raise ValueError("trail_dd must be in (0, 1)")
    if canonical_id == "s04_btc_eth_pairs" and params["exit_band"] >= params["T"]:
        raise ValueError("exit_band must be below entry threshold")
    timeframe = str(spec.get("timeframe", NATIVE_TIMEFRAMES.get(canonical_id, "5m")))
    if timeframe not in {"5m", "15m", "1h", "1d"}:
        raise ValueError("unsupported execution timeframe")
    universe = spec.get("universe", UNIVERSE)
    if not isinstance(universe, (list, tuple)) or not universe:
        raise ValueError("universe must be a non-empty sequence")
    universe = list(universe)
    budget = {"target_exposure": 0.30, "max_positions": 2, "max_per_coin": 1,
              "sizing": "剩余预算 / 剩余槽位", "leverage": 1}
    supplied_budget = spec.get("position_budget", {})
    allowed_budget_keys = set(budget) | {"leverage_convention"}
    if not isinstance(supplied_budget, Mapping) or set(supplied_budget) - allowed_budget_keys:
        raise ValueError("unsupported position_budget")
    budget.update(supplied_budget)
    budget["target_exposure"] = _number(budget["target_exposure"], "target_exposure",
                                         positive=True)
    if budget["target_exposure"] > 1:
        raise ValueError("target_exposure cannot exceed one")
    for key, expected in (("max_positions", 2), ("max_per_coin", 1)):
        if _integer(budget[key], key) != expected:
            raise ValueError(f"{key} must equal {expected}")
        budget[key] = expected
    leverage = _integer(budget["leverage"], "leverage")
    allowed_leverages = (1, 30, 100) if experimental_2x2 else (1, 100)
    if leverage not in allowed_leverages:
        if experimental_2x2:
            raise ValueError("leverage must equal 1, 30, or 100 for experimental 2x2")
        raise ValueError("leverage must equal 1 or 100")
    if (str(strategy_id).endswith(("_100x", "_100x_maker")) and leverage != 100):
        raise ValueError("_100x strategy identities require leverage 100")
    budget["leverage"] = leverage
    if "leverage_convention" in budget and budget["leverage_convention"] != \
            "A：保证金不变，名义 ×100，逐根强平":
        raise ValueError("unsupported leverage_convention")
    if budget["sizing"] != "剩余预算 / 剩余槽位":
        raise ValueError("unsupported sizing")
    requested_warmup = _integer(spec.get("min_warmup_bars", minimum), "min_warmup_bars")
    required = _required_bars(strategy_id, params)
    # Explicit requests override the default. Frozen definitions already
    # store their effective warmup, so a valid stored value remains unchanged.
    minimum = max(requested_warmup, required)
    if atr_risk_enabled:
        minimum = max(minimum, params["atr_window"] + 2)
    capital = _number(spec.get("initial_capital", 10_000), "initial_capital", positive=True)
    # Primary forward cost: measured maker fee on the live limit-order path.
    # The 0.05% taker rate remains a separate conservative sensitivity case.
    fee = _number(spec.get("fee_rate_per_side", 0.0002), "fee_rate_per_side")
    if not 0 <= fee < 1:
        raise ValueError("fee_rate_per_side must be in [0, 1)")
    policies = {
        "closed_only": True, "fill_model": "next_closed_bar_open_proxy",
        "slippage": "unknown", "funding": "unknown",
        "input_revision_policy": "first_accepted_closed_version",
        "history_policy": "official_rest_indicator_only",
        "closed_freshness_ms": 90_000,
        "baseline_policy": "exclude_current_completed_candle",
        "true_range_policy": "arithmetic_mean_previous_true_ranges",
        "standard_deviation_ddof": 1,
        "holding_bars_policy": "inclusive_actual_open_candle",
        "flow_imbal_convention": "recompute_2_taker_buy_base_over_volume_minus_1",
        "orderflow_threshold_policy": "decimal_stored_inputs_no_epsilon",
        "atr_risk_policy": "fixed_entry_evidence_atr_close_trigger_next_open",
        "annualization_bars": 365 * 288,
        "same_side_rebalance": False,
    }
    for key, expected in policies.items():
        if key in spec and spec[key] != expected:
            # The research file uses a documented mapping for this convention.
            convention = spec[key]
            if not (key == "flow_imbal_convention" and isinstance(convention, Mapping)
                    and convention.get("mode") == "recompute"):
                raise ValueError(f"unsupported {key}")
    normalized = {
        "schema_version": SCHEMA_VERSION, "algorithm_version": ALGORITHM_VERSION,
        "strategy_id": strategy_id, "name": str(spec.get("name", strategy_id)),
        "mechanism": str(spec.get("mechanism", strategy_id)), "params": params,
        "timeframe": timeframe, "universe": universe, "min_warmup_bars": minimum,
        "position_budget": budget, "initial_capital": capital, "fee_rate_per_side": fee,
        **policies,
    }
    for key, expected in (("schema_version", SCHEMA_VERSION),
                          ("algorithm_version", ALGORITHM_VERSION)):
        if key in spec and spec[key] != expected:
            raise ValueError(f"unsupported {key}")
    rule_hash = _digest(normalized)
    if is_2x2:
        # 2x2 account IDs identify execution arms, while the signal rule hash
        # must remain the canonical base-family hash for causal comparison.
        # Recompute that hash from the normalized signal fields rather than
        # trusting metadata supplied by a caller.
        canonical_signal = dict(normalized, strategy_id=canonical_id)
        # L30 arms change only the execution overlay.  The canonical signal
        # identity uses the base family's 100x budget, matching the source
        # definition that the factory normalized before applying an arm.
        canonical_budget = dict(canonical_signal["position_budget"])
        canonical_budget["leverage"] = 100
        canonical_signal["position_budget"] = canonical_budget
        canonical_hash = _digest(canonical_signal)
        supplied_signal_hash = (spec.get("canonical_signal_rule_hash")
                                or spec.get("signal_rule_hash"))
        if supplied_signal_hash != canonical_hash:
            raise ValueError("2x2 canonical signal rule hash mismatch")
        rule_hash = canonical_hash
    elif spec.get("rule_hash") not in (None, rule_hash):
        raise ValueError("rule hash mismatch requires a new strategy identity")
    normalized["rule_hash"] = rule_hash
    return normalized


def strategy_rule_hash(spec: Mapping[str, Any]) -> str:
    return normalize_spec(spec)["rule_hash"]


def _as_of_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("as_of_utc must be timezone aware")
        return int(value.timestamp() * 1000)
    if isinstance(value, str) and not value.isdigit():
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _as_of_ms(parsed)
    return _integer(value, "as_of_utc", positive=False)


def _rows(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dict") and hasattr(value, "index"):
        result = value.to_dict("records")
        for row, index in zip(result, value.index, strict=True):
            if "timestamp" not in row:
                row["timestamp"] = int(index.timestamp() * 1000)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [dict(row) if isinstance(row, Mapping) else {} for row in value]
    raise ValueError("candles must be records or a timestamp-indexed DataFrame")


def _history(value: Any, as_of_ms: int | None, *, bar_ms: int = BAR_MS
             ) -> tuple[list[dict[str, Any]], str | None]:
    """Return the contiguous valid closed suffix, without inventing missing bars."""
    prepared = []
    try:
        raw_rows = _rows(value)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return [], "invalid_candle_container"
    for raw in raw_rows:
        extras = raw.get("extras", {})
        extras = extras if isinstance(extras, Mapping) else {}
        closed = extras.get("closed", raw.get("closed"))
        if closed is not True:
            continue
        try:
            timestamp = _integer(raw.get("timestamp"), "timestamp", positive=False)
            if timestamp < 0 or timestamp % bar_ms:
                raise ValueError("timestamp is not aligned to execution timeframe")
        except ValueError:
            return [], "invalid_candle_timestamp"
        if as_of_ms is not None and timestamp + bar_ms > as_of_ms:
            continue
        row = {"timestamp": timestamp, "extras": {"closed": True}}
        try:
            for key in ("open", "high", "low", "close"):
                row[key] = _number(raw.get(key), key, positive=True)
            row["volume"] = _number(raw.get("volume"), "volume")
            if row["volume"] < 0 or row["high"] < max(row["open"], row["low"], row["close"]) \
                    or row["low"] > min(row["open"], row["high"], row["close"]):
                raise ValueError("invalid OHLCV bounds")
        except ValueError:
            return [], "invalid_closed_candle_ohlcv"
        # Only signal inputs enter the fingerprint; unrelated extras do not.
        if "taker_buy_base" in extras:
            try:
                row["extras"]["taker_buy_base"] = _number(extras["taker_buy_base"],
                                                          "taker_buy_base")
            except ValueError:
                row["extras"]["taker_buy_base"] = None
        prepared.append(row)
    prepared.sort(key=lambda row: row["timestamp"])
    if len({row["timestamp"] for row in prepared}) != len(prepared):
        return [], "duplicate_closed_candle_timestamp"
    start = len(prepared) - 1
    while start > 0 and prepared[start]["timestamp"] - prepared[start - 1]["timestamp"] == bar_ms:
        start -= 1
    return prepared[max(start, 0):], None


def _atr(rows: list[dict[str, Any]], window: int) -> float:
    # rows[-1] is the decision candle, whose range is excluded from the baseline.
    values = []
    for index in range(len(rows) - window - 1, len(rows) - 1):
        row = rows[index]
        previous = rows[index - 1]["close"]
        values.append(max(row["high"], previous) - min(row["low"], previous))
    return mean(values)


def _position(value: Any, timestamp: int, *, bar_ms: int = BAR_MS
              ) -> tuple[dict[str, Any] | None, str | None]:
    if not value:
        return None, None
    if not isinstance(value, Mapping):
        return None, "invalid_position"
    try:
        side = value.get("side", value.get("direction"))
        side = {"long": 1, "short": -1}.get(side, side)
        side = _integer(side, "position.side", positive=False)
        if side not in (-1, 1):
            raise ValueError("position side must be +/-1")
        entry = _number(value.get("entry_price"), "entry_price", positive=True)
        opened = _integer(value.get("opened_candle_ms"), "opened_candle_ms", positive=False)
        if opened > timestamp or opened < 0 or opened % bar_ms:
            raise ValueError("position open timestamp is invalid")
        return {**value, "side": side, "entry_price": entry, "opened_candle_ms": opened,
                "held_bars": (timestamp - opened) // bar_ms + 1}, None
    except (ValueError, TypeError):
        return None, "invalid_position"


def _signal(rows: list[dict[str, Any]], required: int, reason: str,
            *, ready: bool, error: str | None = None) -> dict[str, Any]:
    return {"target_side": None, "reason": error or reason, "evidence": {},
            "candle_utc": rows[-1]["timestamp"] if rows else None, "size_multiplier": 1.0,
            "warmup": {"valid_bars": len(rows), "required_bars": required, "ready": ready}}


def _evidence(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"history_start_ms": rows[0]["timestamp"],
            "history_end_ms": rows[-1]["timestamp"], "history_bars": len(rows),
            "data_fingerprint": _digest(rows), "algorithm_version": ALGORITHM_VERSION,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "trigger_reference": "closed_close", "fill_model": "next_closed_bar_open_proxy"}


def _utc_day(timestamp: int) -> int:
    """UTC day boundary for native 15m/1h/1d adapters."""
    return int(datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp() * 1000)


def _utc_hour(timestamp: int) -> tuple[int, int]:
    value = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
    return value.weekday(), value.hour


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(math.floor(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _true_range_at(rows: list[dict[str, Any]], index: int) -> float:
    previous = rows[index - 1]["close"]
    row = rows[index]
    return max(row["high"], previous) - min(row["low"], previous)


def _atr_at(rows: list[dict[str, Any]], index: int, window: int) -> float:
    if index < window + 1:
        return 0.0
    return mean(_true_range_at(rows, j) for j in range(index - window, index))


def _channel_state(rows: list[dict[str, Any]], channel: int) -> int:
    """Return the causal, stateful Donchian direction at the last close.

    A channel break updates state; bars inside the prior channel retain the
    previous state.  The current close is compared only with the preceding
    ``channel`` closes, so no future/current-bar extrema enter the baseline.
    """
    if len(rows) <= channel:
        return 0
    state = 0
    for index in range(channel, len(rows)):
        prior = [row["close"] for row in rows[index - channel:index]]
        if rows[index]["close"] > max(prior):
            state = 1
        elif rows[index]["close"] < min(prior):
            state = -1
    return state


def _channel_breakout(rows: list[dict[str, Any]], channel: int) -> tuple[int, float, float]:
    """Return current close breakout side and prior-channel bounds."""
    if len(rows) <= channel:
        return 0, 0.0, 0.0
    prior = [row["close"] for row in rows[-channel - 1:-1]]
    high, low = max(prior), min(prior)
    close = rows[-1]["close"]
    return (1 if close > high else -1 if close < low else 0), high, low


def _close_returns(rows: list[dict[str, Any]], window: int) -> list[float]:
    """Previous-close returns ending immediately before the decision bar."""
    if len(rows) < window + 2:
        return []
    sample = rows[-window - 1:-1]
    return [right["close"] / left["close"] - 1
            for left, right in zip(sample, sample[1:])]


def _volume_ratio(rows: list[dict[str, Any]], window: int) -> tuple[float, float]:
    if len(rows) <= window:
        return 0.0, 0.0
    baseline = median(row["volume"] for row in rows[-window - 1:-1])
    return (rows[-1]["volume"] / baseline if baseline > 0 else 0.0), baseline


def _orb(rows: list[dict[str, Any]], params: dict[str, Any],
         pos: dict[str, Any] | None, *,
         bar_ms: int = BAR_MS) -> tuple[int | None, str, dict[str, Any]]:
    current = rows[-1]
    day = current["timestamp"] // DAY_MS * DAY_MS
    evidence: dict[str, Any] = {"session_start_ms": day}
    if pos and (pos["opened_candle_ms"] < day or current["timestamp"] + bar_ms >= day + DAY_MS):
        return 0, "session_day_end", evidence
    session = [row for row in rows if row["timestamp"] >= day]
    count = params["N_bars"]
    expected = [day + index * bar_ms for index in range(count)]
    if len(session) <= count or [row["timestamp"] for row in session[:count]] != expected:
        return None, "session_range_not_ready", evidence
    hi = max(row["high"] for row in session[:count])
    lo = min(row["low"] for row in session[:count])
    evidence.update(session_high=hi, session_low=lo, range_bars=count)
    if pos:
        if (pos["side"] == 1 and current["close"] < lo) or \
                (pos["side"] == -1 and current["close"] > hi):
            return 0, "opposite_session_breakout", evidence
        return None, "position_held", evidence
    # A historical first breakout consumes the day even when this account was
    # not running/filled then. This does not manufacture an earlier entry.
    first = next((row for row in session[count:]
                  if row["close"] > hi or row["close"] < lo), None)
    evidence["first_breakout_ms"] = first["timestamp"] if first else None
    if current["timestamp"] + bar_ms >= day + DAY_MS:
        return None, "session_day_end_no_entry", evidence
    if first is None:
        return None, "no_session_breakout", evidence
    if first["timestamp"] != current["timestamp"]:
        return None, "daily_first_entry_already_consumed", evidence
    return (1 if current["close"] > hi else -1), "first_session_breakout", evidence


def _single(strategy_id: str, rows: list[dict[str, Any]], params: dict[str, Any],
            pos: dict[str, Any] | None, *,
            bar_ms: int = BAR_MS) -> tuple[int | None, str, dict[str, Any], float]:
    """Check fixed risk before mechanism exits; attach ATR to entries/flips."""
    canonical_id = _canonical_strategy_id(strategy_id)
    if (canonical_id not in ATR_HARD_STOP_STRATEGIES
            or "risk_stop_atr_mult" not in params):
        return _single_mechanism(strategy_id, rows, params, pos, bar_ms=bar_ms)
    close = rows[-1]["close"]
    window = _integer(params.get("atr_window", 14), "atr_window")
    multiplier = _number(params.get("risk_stop_atr_mult", 1.5),
                         "risk_stop_atr_mult", positive=True)
    risk_evidence = {"close": close, "atr_window": window,
                     "risk_stop_atr_mult": multiplier}
    if pos:
        risk_evidence.update(held_bars=pos["held_bars"], entry_price=pos["entry_price"])
        entry_atr = pos.get("entry_atr")
        if entry_atr is None:
            entry_evidence = pos.get("entry_evidence") or {}
            entry_atr = (entry_evidence.get("atr")
                         if isinstance(entry_evidence, Mapping) else None)
        try:
            entry_atr = _number(entry_atr, "entry_atr", positive=True)
        except ValueError:
            return None, "frozen_entry_atr_unavailable", risk_evidence, 1.0
        # Decimal preserves the inclusive close boundary, as in s08. The
        # current ATR never replaces the position's frozen entry baseline.
        stop = (Decimal(str(pos["entry_price"])) - pos["side"]
                * Decimal(str(multiplier)) * Decimal(str(entry_atr)))
        risk_evidence.update(entry_atr=entry_atr, stop_level=float(stop))
        if (Decimal(str(close)) - stop) * pos["side"] <= 0:
            return 0, "stop_loss_close_confirmed", risk_evidence, 1.0
    side, reason, evidence, size = _single_mechanism(strategy_id, rows, params, pos,
                                                    bar_ms=bar_ms)
    # The simulation already deep-copies evidence.atr into each new position.
    # Reversals also need the latest causal ATR for their replacement entry.
    atr = _atr(rows, window) if len(rows) >= window + 2 else None
    evidence = {**evidence, **risk_evidence}
    if atr is not None and math.isfinite(atr) and atr > 0:
        evidence["atr"] = atr
    elif side in (-1, 1) and (not pos or side != pos["side"]):
        return (None, "risk_atr_history_not_ready" if atr is None else "risk_atr_unavailable",
                evidence, 1.0)
    return side, reason, evidence, size


def _single_mechanism(strategy_id: str, rows: list[dict[str, Any]], params: dict[str, Any],
                      pos: dict[str, Any] | None, *, bar_ms: int = BAR_MS
                      ) -> tuple[int | None, str, dict[str, Any], float]:
    strategy_id = _canonical_strategy_id(strategy_id)
    current = rows[-1]
    close = current["close"]
    evidence: dict[str, Any] = {"close": close}
    if pos:
        evidence.update(held_bars=pos["held_bars"], entry_price=pos["entry_price"])
    if strategy_id == "s01_orb_breakout":
        side, reason, orb_evidence = _orb(rows, params, pos, bar_ms=bar_ms)
        return side, reason, {**evidence, **orb_evidence}, 1.0
    if pos and params.get("H") and pos["held_bars"] >= params["H"]:
        evidence["maximum_holding_bars"] = params["H"]
        return 0, "holding_period_elapsed", evidence, 1.0
    if strategy_id == "s02_dual_thrust":
        atr = _atr(rows, params["range_window"])
        upper, lower = current["open"] + params["k"] * atr, current["open"] - params["k"] * atr
        evidence.update(range_mean=atr, center_open=current["open"], upper=upper, lower=lower)
        side = 1 if close > upper else -1 if close < lower else None
        if pos:
            return (0, "opposite_range_breakout", evidence, 1.0) if side == -pos["side"] \
                else (None, "position_held", evidence, 1.0)
        return side, "range_breakout" if side else "no_range_breakout", evidence, 1.0
    if strategy_id == "s12_surge_expansion_breakout":
        window, valid = params["window"], params["valid_bars"]
        candidates = []
        for index in range(max(window, len(rows) - valid - 1), len(rows) - 1):
            baseline = median(row["volume"] for row in rows[index - window:index])
            if baseline > 0 and rows[index]["volume"] >= params["VR"] * baseline:
                candidates.append(index)
        if not candidates:
            return None, "no_surge_root", evidence, 1.0
        root = candidates[-1]
        root_row = rows[root]
        evidence.update(surge_root_ms=root_row["timestamp"],
                        surge_volume_ratio=root_row["volume"] / median(
                            row["volume"] for row in rows[root - window:root]),
                        surge_high=root_row["high"], surge_low=root_row["low"],
                        valid_bars=valid)
        breakout = 1 if close > root_row["high"] else -1 if close < root_row["low"] else None
        if pos:
            if pos["held_bars"] >= params["H"]:
                return 0, "holding_period_elapsed", evidence, 1.0
            if breakout == -pos["side"]:
                return 0, "opposite_surge_breakout", evidence, 1.0
            return None, "position_held", evidence, 1.0
        if breakout is None or len(rows) - 1 - root > valid:
            return None, "no_surge_breakout", evidence, 1.0
        return breakout, "surge_expansion_breakout", evidence, 1.0
    if strategy_id == "s13_daily_trend_multi_horizon":
        states = {}
        for channel in params["channels"]:
            state = 0
            for index in range(channel, len(rows)):
                prior = rows[index - channel:index]
                upper = max(row["high"] for row in prior)
                lower = min(row["low"] for row in prior)
                if rows[index]["close"] > upper:
                    state = 1
                elif rows[index]["close"] < lower:
                    state = -1
            states[str(channel)] = state
        consensus = next(iter(states.values())) if states and len(set(states.values())) == 1 else 0
        evidence.update(channel_states=states, consensus=consensus)
        if pos:
            if pos["held_bars"] >= params["H"]:
                return 0, "holding_period_elapsed", evidence, 1.0
            if consensus and consensus != pos["side"]:
                return 0, "multi_horizon_trend_reversed", evidence, 1.0
            return None, "position_held", evidence, 1.0
        return (consensus, "multi_horizon_consensus" if consensus else
                "no_multi_horizon_consensus", evidence, 1.0)
    if strategy_id == "s14_vol_compression_release":
        atr_window, lookback = params["atr_window"], params["compression_lookback"]
        values = []
        for index in range(max(atr_window + 1, len(rows) - lookback - 1), len(rows)):
            atr = _atr_at(rows, index, atr_window)
            if atr > 0:
                values.append(atr / rows[index]["close"])
        if len(values) < lookback // 2:
            return None, "compression_history_not_ready", evidence, 1.0
        threshold = _percentile(values[:-1], params["pctile"])
        compressed = []
        for index in range(len(rows) - 1):
            atr = _atr_at(rows, index, atr_window)
            if atr > 0 and atr / rows[index]["close"] <= threshold:
                compressed.append(index)
        if not compressed:
            return None, "no_compression_zone", evidence, 1.0
        last = compressed[-1]
        start = last
        while start > 0 and start - 1 in compressed:
            start -= 1
        zone = rows[start:last + 1]
        hi, lo = max(r["high"] for r in zone), min(r["low"] for r in zone)
        breakout = 1 if close > hi else -1 if close < lo else None
        evidence.update(compression_threshold=threshold, compression_start_ms=rows[start]["timestamp"],
                        compression_end_ms=rows[last]["timestamp"], compression_high=hi,
                        compression_low=lo,
                        atr_pct=(_atr_at(rows, len(rows)-1, atr_window) / close))
        if pos:
            if pos["held_bars"] >= params["H"]:
                return 0, "holding_period_elapsed", evidence, 1.0
            if breakout == -pos["side"]:
                return 0, "opposite_compression_breakout", evidence, 1.0
            return None, "position_held", evidence, 1.0
        return (breakout, "compression_release" if breakout else "no_compression_breakout",
                evidence, 1.0)
    if strategy_id == "s16_weekly_seasonality":
        weekday, hour = _utc_hour(current["timestamp"])
        evidence.update(utc_weekday=weekday, utc_hour=hour,
                        entry_hours_utc=list(params["entry_hours_utc"]), mode=params["mode"])
        if pos:
            return (0, "holding_period_elapsed", evidence, 1.0) if pos["held_bars"] >= params["H"] \
                else (None, "position_held", evidence, 1.0)
        if hour not in params["entry_hours_utc"] or len(rows) < 2:
            return None, "seasonality_window_closed", evidence, 1.0
        direction = 1 if close > rows[-2]["close"] else -1 if close < rows[-2]["close"] else None
        return direction, "seasonality_momentum" if direction else "seasonality_no_direction", evidence, 1.0
    if strategy_id == "s17_overnight_range_fade":
        day = _utc_day(current["timestamp"])
        session = [r for r in rows if day <= r["timestamp"] < day + 8 * 3_600_000]
        evidence["overnight_session_start_ms"] = day
        # A contiguous suffix can still begin halfway through the UTC session.
        # Require its start and its last completed bar to cover 00:00--08:00.
        if (len(session) < 8 or session[0]["timestamp"] != day
                or session[-1]["timestamp"] + rows[-1]["timestamp"]
                - rows[-2]["timestamp"] < day + 8 * 3_600_000):
            return None, "overnight_range_not_ready", evidence, 1.0
        hi, lo = max(r["high"] for r in session), min(r["low"] for r in session)
        mid = (hi + lo) / 2
        evidence.update(overnight_high=hi, overnight_low=lo, overnight_mid=mid)
        if pos:
            if pos["held_bars"] >= params["H"]:
                return 0, "holding_period_elapsed", evidence, 1.0
            if (pos["side"] == 1 and close >= mid) or (pos["side"] == -1 and close <= mid):
                return 0, "overnight_midpoint_reached", evidence, 1.0
            if current["timestamp"] >= day + 24 * 3_600_000 - 3_600_000:
                return 0, "utc_day_end", evidence, 1.0
            return None, "position_held", evidence, 1.0
        if current["timestamp"] < day + 8 * 3_600_000:
            return None, "overnight_session_open", evidence, 1.0
        side = -1 if close >= hi else 1 if close <= lo else None
        return side, "overnight_boundary_fade" if side else "no_overnight_boundary", evidence, 1.0
    if strategy_id == "s18_drawdown_recovery":
        n = params["N"]
        peak = max(row["high"] for row in rows[-n - 1:-1])
        dd = 1 - close / peak if peak > 0 else 0.0
        evidence.update(rolling_peak=peak, drawdown=dd, threshold=params["X"])
        if pos:
            entry_peak = pos.get("entry_evidence", {}).get("rolling_peak", peak)
            if close >= entry_peak:
                return 0, "drawdown_recovered", evidence, 1.0
            if dd >= 2 * params["X"]:
                return 0, "drawdown_stop", evidence, 1.0
            if pos["held_bars"] >= params["H"]:
                return 0, "holding_period_elapsed", evidence, 1.0
            return None, "position_held", evidence, 1.0
        confirmed = (current["close"] > current["open"] and rows[-2]["close"] > rows[-2]["open"])
        if dd >= params["X"] and confirmed:
            return 1, "drawdown_recovery", evidence, 1.0
        return None, "no_drawdown_recovery", evidence, 1.0
    if strategy_id == "s20_intraday_range_position":
        day = _utc_day(current["timestamp"])
        day_rows = [r for r in rows if r["timestamp"] >= day]
        if not day_rows or day_rows[0]["timestamp"] != day:
            return None, "intraday_range_not_ready", evidence, 1.0
        hi, lo = max(r["high"] for r in day_rows), min(r["low"] for r in day_rows)
        width = hi - lo
        if width <= 0:
            return None, "intraday_range_zero", evidence, 1.0
        location = (close - lo) / width
        evidence.update(day_high=hi, day_low=lo, range_position=location)
        if pos:
            if pos["held_bars"] >= params["H"]:
                return 0, "holding_period_elapsed", evidence, 1.0
            if abs(location - 0.5) <= 0.05:
                return 0, "range_position_reverted", evidence, 1.0
            if location <= 0.1 and pos["side"] == 1 or location >= 0.9 and pos["side"] == -1:
                return 0, "range_position_reversed", evidence, 1.0
            return None, "position_held", evidence, 1.0
        side = 1 if location >= params["hi"] else -1 if location <= params["lo"] else None
        return side, "intraday_range_position" if side else "no_intraday_extreme", evidence, 1.0
    if strategy_id == "s21_liquidation_cascade_fade":
        k = params["k"]
        if len(rows) < k + 1:
            return None, "cascade_history_not_ready", evidence, 1.0
        window = rows[-k:]
        changes = [row["close"] - row["open"] for row in window]
        up = all(value > 0 for value in changes)
        down = all(value < 0 for value in changes)
        move = close / rows[-k - 1]["close"] - 1
        final = window[-1]
        body = abs(final["close"] - final["open"])
        upper_wick = final["high"] - max(final["open"], final["close"])
        lower_wick = min(final["open"], final["close"]) - final["low"]
        exhaustion = (up and upper_wick >= body) or (down and lower_wick >= body)
        fade = -1 if up else 1 if down else None
        evidence.update(cascade_move=move, cascade_direction="up" if up else "down" if down else None,
                        upper_wick=upper_wick, lower_wick=lower_wick)
        if pos:
            if pos["held_bars"] >= params["H"]:
                return 0, "holding_period_elapsed", evidence, 1.0
            origin = pos.get("entry_evidence", {}).get("cascade_origin")
            if origin is not None and ((pos["side"] == 1 and close >= origin) or
                                       (pos["side"] == -1 and close <= origin)):
                return 0, "cascade_origin_reached", evidence, 1.0
            return None, "position_held", evidence, 1.0
        if abs(move) >= params["M"] and exhaustion and fade is not None:
            evidence["cascade_origin"] = rows[-k - 1]["close"]
            return fade, "liquidation_cascade_fade", evidence, 1.0
        return None, "no_cascade_exhaustion", evidence, 1.0
    if strategy_id == "s22_dual_confirmation_trend":
        channel = params["channel"]
        state = _channel_state(rows, channel)
        vr, baseline = _volume_ratio(rows, channel)
        evidence.update(channel_state=state, channel=channel, volume_ratio=vr,
                        baseline_volume=baseline, vr_threshold=params["vr_threshold"])
        if not state:
            return None, "trend_state_not_ready", evidence, 1.0
        if pos:
            if state != pos["side"]:
                return 0, "trend_state_reversed", evidence, 1.0
            if pos["held_bars"] >= params["H"]:
                return 0, "holding_period_elapsed", evidence, 1.0
            return None, "position_held", evidence, 1.0
        if vr >= params["vr_threshold"]:
            return state, "dual_confirmation_trend", evidence, 1.0
        return None, "volume_confirmation_not_met", evidence, 1.0
    if strategy_id == "s23_triple_confirmation_breakout":
        channel = params["channel"]
        breakout, channel_high, channel_low = _channel_breakout(rows, channel)
        vr, baseline = _volume_ratio(rows, channel)
        index = len(rows) - 1
        atr = _atr_at(rows, index, params["atr_window"])
        atr_values = [_atr_at(rows, j, params["atr_window"])
                      for j in range(max(params["atr_window"] + 1,
                                         index - params["atr_ma"]), index)]
        atr_mean = mean(atr_values) if len(atr_values) >= max(2, params["atr_ma"] // 2) else 0.0
        evidence.update(channel_breakout=breakout, channel_high=channel_high,
                        channel_low=channel_low, volume_ratio=vr,
                        baseline_volume=baseline, atr=atr, atr_mean=atr_mean,
                        atr_window=params["atr_window"], atr_ma=params["atr_ma"])
        if pos:
            if breakout == -pos["side"]:
                return 0, "opposite_channel_breakout", evidence, 1.0
            if pos["held_bars"] >= params["H"]:
                return 0, "holding_period_elapsed", evidence, 1.0
            return None, "position_held", evidence, 1.0
        if breakout and vr >= params["vr_threshold"] and atr_mean > 0 and atr > atr_mean:
            return breakout, "triple_confirmation_breakout", evidence, 1.0
        return None, "triple_confirmation_not_met", evidence, 1.0
    if strategy_id == "s24_cross_sectional_lowvol":
        return None, "multi_asset_context_unavailable", evidence, 1.0
    if strategy_id == "s25_vol_regime_momentum":
        channel = params["channel"]
        state = _channel_state(rows, channel)
        values = _close_returns(rows, params["rv_window"])
        rv = stdev(values) if len(values) >= 2 else 0.0
        index = len(rows) - 1
        rv_values = []
        for end in range(max(params["rv_window"] + 1,
                             index - params["regime_window"]), index):
            sample = [rows[j]["close"] / rows[j - 1]["close"] - 1
                      for j in range(end - params["rv_window"], end)]
            if len(sample) >= 2:
                rv_values.append(stdev(sample))
        regime_median = median(rv_values) if len(rv_values) >= max(2, params["regime_window"] // 2) else 0.0
        high_regime = regime_median > 0 and rv > regime_median
        evidence.update(channel_state=state, realized_vol=rv,
                        regime_median=regime_median, high_volatility_regime=high_regime,
                        rv_window=params["rv_window"], regime_window=params["regime_window"])
        if pos:
            if state != pos["side"]:
                return 0, "trend_state_reversed", evidence, 1.0
            if not high_regime:
                return 0, "volatility_regime_exited", evidence, 1.0
            if pos["held_bars"] >= params["H"]:
                return 0, "holding_period_elapsed", evidence, 1.0
            return None, "position_held", evidence, 1.0
        if high_regime and state:
            return state, "vol_regime_momentum", evidence, 1.0
        return None, "high_volatility_regime_not_met", evidence, 1.0
    if strategy_id == "s26_surge_gated_trend":
        channel = params["channel"]
        state = _channel_state(rows, channel)
        index = len(rows) - 1
        recent = []
        start = max(channel, index - params["surge_window"] + 1)
        for j in range(start, index + 1):
            baseline_j = median(row["volume"] for row in rows[j - channel:j])
            ratio = rows[j]["volume"] / baseline_j if baseline_j > 0 else 0.0
            recent.append((j, ratio))
        gate_recent = []
        gate_start = max(channel, index - params["gate_valid_days"] + 1)
        for j in range(gate_start, index + 1):
            baseline_j = median(row["volume"] for row in rows[j - channel:j])
            ratio = rows[j]["volume"] / baseline_j if baseline_j > 0 else 0.0
            gate_recent.append((j, ratio))
        surge = [(j, ratio) for j, ratio in recent if ratio >= params["vr_threshold"]]
        gate_surges = [(j, ratio) for j, ratio in gate_recent if ratio >= params["vr_threshold"]]
        entry_surge = surge[-1] if surge else None
        last_surge = gate_surges[-1] if gate_surges else None
        evidence.update(channel_state=state,
                        volume_ratio=recent[-1][1] if recent else 0.0,
                        vr_threshold=params["vr_threshold"],
                        surge_window=params["surge_window"],
                        gate_valid_days=params["gate_valid_days"],
                        surge_root_ms=rows[last_surge[0]]["timestamp"] if last_surge else None)
        if pos:
            if state != pos["side"]:
                return 0, "trend_state_reversed", evidence, 1.0
            if not last_surge:
                return 0, "surge_gate_expired", evidence, 1.0
            return None, "position_held", evidence, 1.0
        if state and entry_surge:
            return state, "surge_gated_trend", evidence, 1.0
        return None, "surge_gate_not_met", evidence, 1.0
    if strategy_id == "s27_long_horizon_mean_reversion":
        n = params["N"]
        prior = [row["close"] for row in rows[-n - 1:-1]]
        ma, sd = mean(prior), stdev(prior) if len(prior) >= 2 else 0.0
        z = (close - ma) / sd if sd > 0 else 0.0
        evidence.update(prior_mean=ma, prior_std=sd, zscore=z,
                        entry_zscore=params["Z"], stop_z=params["stop_z"])
        if pos:
            if z * pos["side"] >= 0:
                return 0, "mean_reversion_complete", evidence, 1.0
            if abs(z) >= params["stop_z"]:
                return 0, "mean_reversion_stop", evidence, 1.0
            if pos["held_bars"] >= params["H"]:
                return 0, "holding_period_elapsed", evidence, 1.0
            return None, "position_held", evidence, 1.0
        if sd <= 0:
            return None, "price_standard_deviation_zero", evidence, 1.0
        side = 1 if z <= -params["Z"] else -1 if z >= params["Z"] else None
        return side, "long_horizon_mean_reversion" if side else "no_long_horizon_deviation", evidence, 1.0
    if strategy_id == "s28_weekly_breakout":
        breakout, high, low = _channel_breakout(rows, params["weekly_channel"])
        evidence.update(channel_breakout=breakout, channel_high=high, channel_low=low,
                        channel=params["weekly_channel"])
        if pos:
            if breakout == -pos["side"]:
                return 0, "opposite_weekly_breakout", evidence, 1.0
            if pos["held_bars"] >= params["H"]:
                return 0, "holding_period_elapsed", evidence, 1.0
            return None, "position_held", evidence, 1.0
        return breakout, "weekly_breakout" if breakout else "no_weekly_breakout", evidence, 1.0
    if strategy_id == "s29_trend_with_trailing":
        state = _channel_state(rows, params["channel"])
        evidence.update(channel_state=state, trail_drawdown=params["trail_dd"])
        if pos:
            opened = pos["opened_candle_ms"]
            held_rows = [row for row in rows if row["timestamp"] >= opened]
            favorable = (max(row["high"] for row in held_rows) if pos["side"] == 1
                         else min(row["low"] for row in held_rows)) if held_rows else close
            drawdown = ((favorable - close) / favorable if pos["side"] == 1
                        else (close - favorable) / favorable) if favorable > 0 else 0.0
            evidence.update(favorable_peak=favorable, peak_price=favorable,
                            trailing_drawdown=drawdown, drawdown=drawdown)
            if state == -pos["side"]:
                return 0, "trend_state_reversed", evidence, 1.0
            if drawdown >= params["trail_dd"]:
                return 0, "trailing_drawdown", evidence, 1.0
            return None, "position_held", evidence, 1.0
        if state:
            return state, "trend_with_trailing", evidence, 1.0
        return None, "trend_state_not_ready", evidence, 1.0
    if strategy_id == "s30_dual_asset_confirmation":
        return None, "multi_asset_context_unavailable", evidence, 1.0
    if strategy_id == "s19_multi_coin_breadth":
        # These mechanisms require a native multi-asset context (channel state
        # for four daily pairs, or a 20-coin breadth snapshot).  The legacy
        # evaluate contract only provides the BTC/ETH pair and must fail closed
        # until the service supplies that context.
        return None, "multi_asset_context_unavailable", evidence, 1.0
    if strategy_id == "s03_tsmom_vol_target":
        momentum = close / rows[-1 - params["L"]]["close"] - 1
        prior = rows[-params["W"] - 2:-1]
        returns = [right["close"] / left["close"] - 1
                   for left, right in zip(prior, prior[1:])]
        rv = stdev(returns) * math.sqrt(365 * 288)
        evidence.update(momentum=momentum, realized_vol_annual=rv,
                        target_vol_annual=params["target_vol_annual"],
                        rebalance="entry_or_flip_only")
        side = 1 if momentum > 0 else -1 if momentum < 0 else 0
        if rv <= 0:
            if pos and side != pos["side"]:
                return 0, "momentum_reversed_without_volatility", evidence, 1.0
            return None, "realized_volatility_zero", evidence, 1.0
        size = min(params["target_vol_annual"] / rv, params["size_multiplier_max"])
        return side, "momentum_target", evidence, size
    if strategy_id == "s11_tsmom_bollinger_pullback":
        # The momentum leg is a regime/side filter.  The Bollinger leg waits
        # for a counter-trend pullback before entering, so this is not a
        # duplicate of either standalone strategy.
        momentum = close / rows[-1 - params["L"]]["close"] - 1
        prior = rows[-params["W"] - 2:-1]
        prior_returns = [right["close"] / left["close"] - 1
                         for left, right in zip(prior, prior[1:])]
        rv = stdev(prior_returns) * math.sqrt(365 * 288)
        prior_prices = [row["close"] for row in rows[-params["N"] - 1:-1]]
        ma, sd = mean(prior_prices), stdev(prior_prices)
        evidence.update(momentum=momentum, realized_vol_annual=rv,
                        target_vol_annual=params["target_vol_annual"],
                        prior_mean=ma, prior_std=sd, rebalance="entry_or_flip_only",
                        combination="momentum_direction_and_bollinger_pullback")
        if sd <= 0:
            return None, "price_standard_deviation_zero", evidence, 1.0
        z = (close - ma) / sd
        evidence.update(zscore=z, entry_zscore=params["Z"])
        side = 1 if momentum > 0 else -1 if momentum < 0 else 0
        if pos:
            if side and side != pos["side"]:
                return 0, "momentum_reversed", evidence, 1.0
            if z * pos["side"] >= 0:
                return 0, "mean_reversion_complete", evidence, 1.0
            if rv <= 0:
                return None, "realized_volatility_zero", evidence, 1.0
            return None, "position_held", evidence, min(
                params["target_vol_annual"] / rv, params["size_multiplier_max"])
        if rv <= 0:
            return None, "realized_volatility_zero", evidence, 1.0
        size = min(params["target_vol_annual"] / rv, params["size_multiplier_max"])
        if side == 1 and z <= -params["Z"]:
            return 1, "momentum_confirmed_bollinger_pullback", evidence, size
        if side == -1 and z >= params["Z"]:
            return -1, "momentum_confirmed_bollinger_pullback", evidence, size
        return None, "no_momentum_bollinger_alignment", evidence, size
    if strategy_id == "s05_bollinger_fade":
        prior = [row["close"] for row in rows[-params["N"] - 1:-1]]
        ma, sd = mean(prior), stdev(prior)
        evidence.update(prior_mean=ma, prior_std=sd)
        if sd <= 0:
            return None, "price_standard_deviation_zero", evidence, 1.0
        z = (close - ma) / sd
        evidence.update(zscore=z, entry_zscore=params["Z"])
        if pos:
            return (0, "mean_reversion_complete", evidence, 1.0) if z * pos["side"] >= 0 \
                else (None, "position_held", evidence, 1.0)
        side = 1 if z <= -params["Z"] else -1 if z >= params["Z"] else None
        return side, "statistical_deviation" if side else "no_statistical_deviation", evidence, 1.0
    if strategy_id == "s06_volume_climax_reversal":
        volume_window = params["volume_window"]
        mv_window = params["mv_window"]
        baseline_decimal = median(Decimal(str(row["volume"]))
                                  for row in rows[-volume_window - 1:-1])
        baseline = float(baseline_decimal)
        anchor_close = Decimal(str(rows[-1 - mv_window]["close"]))
        current_close = Decimal(str(close))
        move_decimal = current_close / anchor_close - Decimal("1")
        move = float(move_decimal)
        ratio = current["volume"] / baseline if baseline > 0 else None
        evidence.update(baseline_volume=baseline, volume_ratio=ratio, price_move=move,
                        price_move_3=move, mv_window=mv_window,
                        volume_window=volume_window, anchor_close=float(anchor_close))
        if pos:
            if (close - pos["entry_price"]) * pos["side"] >= 0:
                return 0, "entry_price_recovered", evidence, 1.0
            return None, "position_held", evidence, 1.0
        volume_extreme = baseline_decimal > 0 and Decimal(str(current["volume"])) >= \
            Decimal(str(params["VR"])) * baseline_decimal
        price_extreme = abs(move_decimal) >= Decimal(str(params["MV3"]))
        if volume_extreme and price_extreme:
            return (-1 if move > 0 else 1), "volume_climax_reversal", evidence, 1.0
        return None, "no_volume_climax", evidence, 1.0
    if strategy_id == "s07_surge_continuation":
        volume = current["volume"]
        bought = current["extras"].get("taker_buy_base")
        baseline = median(row["volume"] for row in rows[-params["volume_window"] - 1:-1])
        evidence.update(volume=volume, taker_buy_base=bought, baseline_volume=baseline,
                        volume_window=params["volume_window"],
                        flow_imbal_convention="recompute_2_taker_buy_base_over_volume_minus_1")
        if bought is None or volume <= 0 or not 0 <= bought <= volume:
            return None, "taker_volume_unavailable", evidence, 1.0
        denominator = Decimal(str(volume))
        numerator = 2 * Decimal(str(bought)) - denominator
        imbalance = float(numerator / denominator)
        evidence["flow_imbal"] = imbalance
        if pos:
            return (0, "orderflow_direction_reversed", evidence, 1.0) \
                if numerator * pos["side"] <= 0 else (None, "position_held", evidence, 1.0)
        if baseline <= 0 or volume < params["min_volume_multiple"] * baseline:
            return None, "orderflow_volume_filter_not_met", evidence, 1.0
        threshold = Decimal(str(params["I"])) * denominator
        side = 1 if numerator >= threshold else -1 if numerator <= -threshold else None
        return side, "orderflow_continuation" if side else "no_orderflow_extreme", evidence, 1.0
    if strategy_id == "s08_wick_rejection":
        if pos:
            entry_evidence = pos.get("entry_evidence", {})
            atr = (entry_evidence.get("atr") if isinstance(entry_evidence, Mapping) else None)
            if atr is None:
                atr = pos.get("entry_atr")
            try:
                atr = _number(atr, "entry_atr", positive=True)
            except ValueError:
                return None, "frozen_entry_atr_unavailable", evidence, 1.0
            entry_decimal = Decimal(str(pos["entry_price"]))
            atr_decimal = Decimal(str(atr))
            close_decimal = Decimal(str(close))
            stop = entry_decimal - pos["side"] * Decimal(str(params["stop_atr_mult"])) * atr_decimal
            profit = entry_decimal + pos["side"] * Decimal(str(params["tp_atr_mult"])) * atr_decimal
            evidence.update(entry_atr=atr, stop_level=float(stop), take_profit_level=float(profit))
            if (close_decimal - stop) * pos["side"] <= 0:
                return 0, "stop_loss_close_confirmed", evidence, 1.0
            if (close_decimal - profit) * pos["side"] >= 0:
                return 0, "take_profit_close_confirmed", evidence, 1.0
            return None, "position_held", evidence, 1.0
        rng = current["high"] - current["low"]
        atr = _atr(rows, params["atr_window"])
        evidence.update(atr=atr, candle_range=rng)
        if rng <= 0 or atr <= 0:
            return None, "candle_range_or_atr_zero", evidence, 1.0
        high_decimal, low_decimal = Decimal(str(current["high"])), Decimal(str(current["low"]))
        open_decimal, close_decimal = Decimal(str(current["open"])), Decimal(str(close))
        range_decimal = high_decimal - low_decimal
        upper = high_decimal - max(open_decimal, close_decimal)
        lower = min(open_decimal, close_decimal) - low_decimal
        evidence.update(upper_wick_ratio=float(upper / range_decimal),
                        lower_wick_ratio=float(lower / range_decimal))
        threshold = Decimal(str(params["W"])) * range_decimal
        side = 1 if lower >= threshold and close > current["open"] else \
            -1 if upper >= threshold and close < current["open"] else None
        return side, "wick_rejection" if side else "no_wick_rejection", evidence, 1.0
    if strategy_id == "s09_trend_pullback":
        slow = params["ma_slow"]
        shift = params["S"]
        slow_now = mean(row["close"] for row in rows[-slow - 1:-1])
        slow_before = mean(row["close"] for row in rows[-slow - shift - 1:-shift - 1])
        fast = mean(row["close"] for row in rows[-params["ma_fast"] - 1:-1])
        atr = _atr(rows, params["atr_window"])
        slope = slow_now - slow_before
        evidence.update(prior_slow_mean=slow_now, prior_slow_mean_lagged=slow_before,
                        prior_fast_mean=fast, slow_slope=slope, atr=atr,
                        pullback_distance=abs(close - fast), pullback_band=params["band"] * atr)
        if pos:
            return (0, "slow_trend_broken", evidence, 1.0) \
                if (close - slow_now) * pos["side"] < 0 else (None, "position_held", evidence, 1.0)
        if atr > 0 and abs(close - fast) <= params["band"] * atr:
            side = 1 if slope > 0 else -1 if slope < 0 else None
            return side, "trend_pullback" if side else "trend_slope_zero", evidence, 1.0
        return None, "no_trend_pullback", evidence, 1.0
    raise ValueError(f"strategy requires cross-asset evaluation: {strategy_id}")


def evaluate(spec: Mapping[str, Any], candles_by_pair: Mapping[str, Any],
             positions: Mapping[str, Any] | None = None,
             as_of_utc: Any = None) -> dict[str, Any]:
    """Evaluate latest complete information without altering data or positions.

    ``as_of_utc`` may be UTC milliseconds or an aware datetime/ISO timestamp.
    With None the caller's strict closed markers are authoritative. It should
    provide the observation time when replaying or validating future rows.
    An unready pair prevents new instructions for the entire account.
    """
    normalized = normalize_spec(spec)
    strategy_id = normalized["strategy_id"]
    canonical_id = _canonical_strategy_id(strategy_id)
    required = normalized["min_warmup_bars"]
    params = normalized["params"]
    timeframe = normalized.get("timeframe", "5m")
    bar_ms = TIMEFRAME_MS[timeframe]
    configured_pairs = spec.get("execution_universe", UNIVERSE)
    if canonical_id == "s24_cross_sectional_lowvol" and "execution_universe" not in spec:
        configured_pairs = spec.get("universe", configured_pairs)
    if not isinstance(configured_pairs, (list, tuple)):
        configured_pairs = UNIVERSE
    pairs = tuple(str(pair) for pair in configured_pairs if "/" in str(pair)) or UNIVERSE
    positions = positions or {}
    as_of_ms = _as_of_ms(as_of_utc)
    signals: dict[str, Any] = {}
    histories: dict[str, list[dict[str, Any]]] = {}
    actual_positions: dict[str, Any] = {}
    for pair in pairs:
        rows, error = _history(candles_by_pair.get(pair), as_of_ms, bar_ms=bar_ms)
        histories[pair] = rows
        ready = error is None and len(rows) >= required
        signal = _signal(rows, required, "warmup_incomplete", ready=ready, error=error)
        if ready:
            signal["reason"] = "no_signal"
            signal["evidence"] = _evidence(rows)
            pos, error = _position(positions.get(pair), rows[-1]["timestamp"], bar_ms=bar_ms)
            if error:
                signal["reason"] = error
                signal["warmup"]["ready"] = False
            actual_positions[pair] = pos
        signals[pair] = signal
    # Cross-sectional strategies keep their executable BTC/ETH legs separate
    # from the research universe.  Load the frozen context series into the
    # same causal history map so the later breadth/ranking checks can validate
    # completeness without turning those symbols into order legs.
    if canonical_id in {"s13_daily_trend_multi_horizon", "s19_multi_coin_breadth",
                        "s24_cross_sectional_lowvol"}:
        context_pairs = spec.get("research_universe") or tuple(candles_by_pair)
        for context_pair in tuple(str(pair) for pair in context_pairs if "/" in str(pair)):
            if context_pair in histories:
                continue
            context_rows, _context_error = _history(
                candles_by_pair.get(context_pair), as_of_ms, bar_ms=bar_ms)
            histories[context_pair] = context_rows
    ready = all(signal["warmup"]["ready"] for signal in signals.values())
    group = {"atomic": True, "legs": list(pairs)} if canonical_id == "s04_btc_eth_pairs" else None
    latest = [signal["candle_utc"] for signal in signals.values()]
    warmup = {"ready": ready, "valid_bars": min(len(rows) for rows in histories.values()),
              "required_bars": required,
              "last_closed_at": min(latest) if all(value is not None for value in latest) else None,
              "reason": "ready" if ready else next(signal["reason"] for signal in signals.values()
                                                    if not signal["warmup"]["ready"]),
              "by_pair": {pair: signals[pair]["warmup"] for pair in pairs}}
    result = {"strategy_id": strategy_id, "rule_hash": normalized["rule_hash"],
              "ready": ready, "warmup": warmup,
              "signals": signals, "group": group}
    if not ready:
        for signal in signals.values():
            if signal["warmup"]["ready"]:
                signal["reason"] = "account_peer_data_unready"
        return result
    # All accounts use a single decision instant; a missing leg cannot be
    # replaced by stale data, even for the single-asset hypotheses.
    aligned_stamps = [[row["timestamp"] for row in histories[pair][-required:]] for pair in pairs]
    aligned = all(stamps == aligned_stamps[0] for stamps in aligned_stamps[1:])
    if not aligned:
        result["ready"] = False
        result["warmup"].update(ready=False, reason="pair_timestamps_not_aligned")
        for signal in signals.values():
            signal["reason"] = "pair_timestamps_not_aligned"
        return result
    if as_of_ms is not None and as_of_ms - histories[pairs[0]][-1]["timestamp"] - bar_ms > 90_000:
        result["ready"] = False
        result["warmup"].update(ready=False, reason="closed_candle_stale")
        for signal in signals.values():
            signal["reason"] = "closed_candle_stale"
        return result
    if canonical_id == "s19_multi_coin_breadth":
        # Breadth is a cross-sectional input, not a second tradable leg.  The
        # service must provide the frozen research universe in the same mapping.
        context_pairs = spec.get("research_universe") or tuple(candles_by_pair)
        context_pairs = tuple(str(pair) for pair in context_pairs if "/" in str(pair))
        context = {}
        for pair in context_pairs:
            context_rows, context_error = _history(candles_by_pair.get(pair), as_of_ms,
                                                    bar_ms=bar_ms)
            if context_error or len(context_rows) < params["ma"] + 1:
                result["ready"] = False
                result["warmup"].update(ready=False, reason="multi_asset_context_unavailable")
                for signal in signals.values():
                    signal["reason"] = "multi_asset_context_unavailable"
                return result
            context[pair] = context_rows
        latest_stamps = {rows[-1]["timestamp"] for rows in context.values()}
        if len(latest_stamps) != 1:
            result["ready"] = False
            result["warmup"].update(ready=False, reason="breadth_timestamps_not_aligned")
            for signal in signals.values():
                signal["reason"] = "breadth_timestamps_not_aligned"
            return result
        above, valid = 0, 0
        for rows_for_pair in context.values():
            baseline = rows_for_pair[-params["ma"] - 1:-1]
            if len(baseline) != params["ma"]:
                continue
            valid += 1
            above += int(rows_for_pair[-1]["close"] > mean(row["close"] for row in baseline))
        breadth = above / valid if valid else None
        btc_signal = signals.get(BTC)
        if breadth is None:
            result["ready"] = False
            result["warmup"].update(ready=False, reason="breadth_context_unavailable")
            for signal in signals.values():
                signal["reason"] = "breadth_context_unavailable"
            return result
        evidence = {"breadth": breadth, "breadth_valid_coins": valid,
                    "breadth_above_ma": above, "breadth_ma": params["ma"]}
        btc_pos = actual_positions.get(BTC)
        target = 1 if breadth >= params["hi"] else -1 if breadth <= params["lo"] else None
        if btc_pos:
            if btc_pos["held_bars"] >= params["H"] or params["lo"] < breadth < params["hi"]:
                target, reason = 0, "breadth_reverted"
            elif target == -btc_pos["side"]:
                target, reason = 0, "breadth_direction_reversed"
            else:
                target, reason = None, "position_held"
        else:
            reason = "breadth_regime" if target else "breadth_neutral"
        if btc_signal is not None:
            btc_signal.update(target_side=target, reason=reason,
                              evidence={**btc_signal["evidence"], **evidence})
        for pair, signal in signals.items():
            if pair != BTC:
                signal.update(target_side=None, reason="breadth_observation_only",
                              evidence={**signal["evidence"], **evidence})
        return result
    if canonical_id == "s24_cross_sectional_lowvol":
        # A native portfolio adapter may provide the frozen 20-coin universe
        # through research_universe.  Without all legs, ranking only the two
        # executable pairs would silently change the strategy definition.
        context_pairs = spec.get("research_universe") or tuple(candles_by_pair)
        context_pairs = tuple(str(pair) for pair in context_pairs if "/" in str(pair))
        if len(context_pairs) < 20:
            result["ready"] = False
            result["warmup"].update(ready=False, reason="multi_asset_context_unavailable")
            for signal in signals.values():
                signal["reason"] = "multi_asset_context_unavailable"
            return result
        context: dict[str, list[dict[str, Any]]] = {}
        for pair in context_pairs:
            context_rows, context_error = _history(candles_by_pair.get(pair), as_of_ms,
                                                    bar_ms=bar_ms)
            if context_error or len(context_rows) < required:
                result["ready"] = False
                result["warmup"].update(ready=False, reason="multi_asset_context_unavailable")
                for signal in signals.values():
                    signal["reason"] = "multi_asset_context_unavailable"
                return result
            context[pair] = context_rows
        stamps = {rows_for_pair[-1]["timestamp"] for rows_for_pair in context.values()}
        if len(stamps) != 1:
            result["ready"] = False
            result["warmup"].update(ready=False, reason="breadth_timestamps_not_aligned")
            for signal in signals.values():
                signal["reason"] = "breadth_timestamps_not_aligned"
            return result
        ranked: list[tuple[str, float, int]] = []
        for pair, context_rows in context.items():
            returns = _close_returns(context_rows, params["rv_window"])
            if len(returns) < 2:
                continue
            rv = stdev(returns)
            state = _channel_state(context_rows, params["channel"])
            ranked.append((pair, rv, state))
        longs = {pair for pair, _, state in sorted(
            (item for item in ranked if item[2] > 0), key=lambda item: item[1]
        )[:params["k"]]}
        shorts = {pair for pair, _, state in sorted(
            (item for item in ranked if item[2] < 0), key=lambda item: item[1], reverse=True
        )[:params["k"]]}
        for pair, signal in signals.items():
            pos = actual_positions.get(pair)
            side = 1 if pair in longs else -1 if pair in shorts else None
            reason = "cross_sectional_selection" if side else "outside_cross_sectional_selection"
            if pos:
                if side != pos["side"]:
                    side, reason = 0, "cross_sectional_rebalance"
                else:
                    side, reason = None, "position_held"
            rv = next((value for name, value, _ in ranked if name == pair), None)
            state = next((value for name, _, value in ranked if name == pair), 0)
            signal.update(target_side=side, reason=reason,
                          evidence={**signal["evidence"], "rv": rv,
                                    "channel_state": state, "selected_longs": sorted(longs),
                                    "selected_shorts": sorted(shorts),
                                    "cross_sectional_universe": len(ranked)})
        return result
    if canonical_id == "s30_dual_asset_confirmation":
        if BTC not in histories or ETH not in histories:
            result["ready"] = False
            result["warmup"].update(ready=False, reason="multi_asset_context_unavailable")
            for signal in signals.values():
                signal["reason"] = "multi_asset_context_unavailable"
            return result
        btc_state = _channel_state(histories[BTC], params["channel"])
        eth_state = _channel_state(histories[ETH], params["channel"])
        common = {"btc_channel_state": btc_state, "eth_channel_state": eth_state,
                  "channel": params["channel"]}
        btc_pos, eth_pos = actual_positions.get(BTC), actual_positions.get(ETH)
        if btc_pos or eth_pos:
            for pair, state, pos in ((BTC, btc_state, btc_pos), (ETH, eth_state, eth_pos)):
                signal = signals[pair]
                if pos and (state != pos["side"] or pos["held_bars"] >= params["H"]):
                    reason = "dual_asset_direction_reversed" if state != pos["side"] else "holding_period_elapsed"
                    target = 0
                elif pos:
                    reason, target = "position_held", None
                else:
                    reason, target = "dual_asset_position_inconsistent", 0
                signal.update(target_side=target, reason=reason,
                              evidence={**signal["evidence"], **common})
            return result
        if btc_state and btc_state == eth_state:
            reason, target = "dual_asset_confirmation", btc_state
        else:
            reason, target = "dual_asset_confirmation_not_met", None
        for pair in (BTC, ETH):
            signals[pair].update(target_side=target, reason=reason,
                                  evidence={**signals[pair]["evidence"], **common})
        return result
    if canonical_id in {"s04_btc_eth_pairs", "s10_lead_lag_btc_eth"}:
        btc_rows, eth_rows = histories[BTC], histories[ETH]
        btc_return = btc_rows[-1]["close"] / btc_rows[-1 - params.get("L", 1)]["close"] - 1
        common = {"source_pair": BTC, "btc_return": btc_return,
                  "source_data_fingerprint": signals[BTC]["evidence"]["data_fingerprint"]}
        if canonical_id == "s10_lead_lag_btc_eth":
            signals[BTC]["reason"] = "signal_source_only"
            pos = actual_positions[ETH]
            side, reason = None, "no_lead_lag_trigger"
            if pos:
                common["held_bars"] = pos["held_bars"]
                if pos["held_bars"] >= params["H"]:
                    side, reason = 0, "holding_period_elapsed"
                else:
                    reason = "position_held"
            elif abs(btc_return) >= params["R"]:
                side, reason = (1 if btc_return > 0 else -1), "btc_lead_lag_trigger"
            signals[ETH].update(target_side=side, reason=reason,
                                evidence={**signals[ETH]["evidence"], **common})
            return result
        eth_return = eth_rows[-1]["close"] / eth_rows[-1 - params["L"]]["close"] - 1
        diff = btc_return - eth_return
        common.update(eth_return=eth_return, relative_return=diff,
                      peer_data_fingerprint=signals[ETH]["evidence"]["data_fingerprint"],
                      equal_notional=True, both_legs_required=True)
        btc_pos, eth_pos = actual_positions[BTC], actual_positions[ETH]
        target: tuple[int | None, int | None] = (None, None)
        reason = "no_relative_strength_trigger"
        if btc_pos or eth_pos:
            if not btc_pos or not eth_pos or btc_pos["side"] != -eth_pos["side"] or \
                    btc_pos["opened_candle_ms"] != eth_pos["opened_candle_ms"]:
                target, reason = (0, 0), "atomic_position_inconsistent"
            elif max(btc_pos["held_bars"], eth_pos["held_bars"]) >= params["H"]:
                target, reason = (0, 0), "holding_period_elapsed"
            elif abs(diff) < params["exit_band"]:
                target, reason = (0, 0), "relative_strength_converged"
            else:
                reason = "paired_position_held"
        elif abs(diff) >= params["T"]:
            target = (1, -1) if diff > 0 else (-1, 1)
            reason = "relative_strength_pair"
        for pair, side in zip((BTC, ETH), target):
            signals[pair].update(target_side=side, reason=reason,
                                 evidence={**signals[pair]["evidence"], **common})
        return result
    for pair in pairs:
        side, reason, evidence, size = _single(strategy_id, histories[pair], params,
                                               actual_positions[pair], bar_ms=bar_ms)
        signals[pair].update(target_side=side, reason=reason, size_multiplier=size,
                             evidence={**signals[pair]["evidence"], **evidence})
    return result

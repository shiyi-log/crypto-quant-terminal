"""Causal candle indicators and risk rules for paper execution only.

Percentage values use percent units: 0.5 means 0.5%.  Every indicator row
uses only that row and earlier completed candles; execution selects the
previous row.  OHLC touches create an exit intent, never a target-price fill.
"""
from __future__ import annotations

import copy
import math

import numpy as np
import pandas as pd


EXECUTION_RULES = {
    "dual_touch": "stop_loss_first",
    "gap_fill": "execution_open",
    "exit_timing": "next_open",
    # Capacity released by an exit becomes usable on the next opening cycle.
    "slot_release": "after_exit_fill_next_open",
}


def engine_rules(range_filter=None, take_profit=None, stop_loss=None, execution=None):
    rf = {"kind": "none", "window": 20, "threshold": 0.0,
          "op": "gt", "warmup_bars": 20}
    rf.update(range_filter or {})
    tp = {"kind": "none", "value": 0.0, "atr_window": None}
    tp.update(take_profit or {})
    sl = {"kind": "none", "value": 0.0, "atr_window": None}
    sl.update(stop_loss or {})
    ex = dict(EXECUTION_RULES)
    ex.update(execution or {})
    if ex != EXECUTION_RULES:
        raise ValueError("unsupported paper execution policy")
    if rf["kind"] not in {"none", "adx", "channel_width_pct", "realized_vol_pct"}:
        raise ValueError("unsupported range filter")
    for field in ("window", "warmup_bars"):
        value = rf[field]
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < 1:
            raise ValueError(f"range_filter.{field} must be a positive integer")
    if rf["op"] not in {"gt", "lt"} or not math.isfinite(float(rf["threshold"])):
        raise ValueError("invalid range filter comparison")
    for rule, kinds in ((tp, {"none", "fixed_pct", "atr_multiple", "trailing_pct"}),
                        (sl, {"none", "fixed_pct", "atr_multiple"})):
        if rule["kind"] not in kinds:
            raise ValueError("unsupported exit rule")
        if rule["kind"] != "none":
            value = rule["value"]
            if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError("active risk value must be positive and finite")
            if rule["kind"] != "atr_multiple" and float(value) >= 100:
                raise ValueError("percentage risk value must be below 100")
        if rule["kind"] == "atr_multiple":
            window = rule["atr_window"]
            if isinstance(window, bool) or not isinstance(window, (int, np.integer)) or window < 1:
                raise ValueError("ATR rule needs a positive atr_window")
    return rf, tp, sl, ex


def ohlc_valid(frame):
    out = pd.Series(True, index=frame.index)
    if not {"open", "high", "low", "close"}.issubset(frame.columns):
        return pd.Series(False, index=frame.index)
    for field in ("open", "high", "low", "close"):
        out &= np.isfinite(frame[field]) & (frame[field] > 0)
    out &= (frame["high"] >= frame[["open", "close", "low"]].max(axis=1))
    out &= (frame["low"] <= frame[["open", "close", "high"]].min(axis=1))
    return out


def atr(frame, window):
    if not {"high", "low", "close", "open"}.issubset(frame.columns):
        return pd.Series(np.nan, index=frame.index)
    valid = ohlc_valid(frame)
    previous_close = frame["close"].shift(1)
    tr = pd.concat([frame["high"] - frame["low"],
                    (frame["high"] - previous_close).abs(),
                    (frame["low"] - previous_close).abs()], axis=1).max(axis=1)
    tr = tr.where(valid)
    return tr.ewm(alpha=1 / window, adjust=False, min_periods=window).mean().where(
        valid.rolling(window).sum() == window)


def range_values(frame, rule):
    """Return raw value plus readiness, independent of the threshold/op."""
    kind, window = rule["kind"], rule["window"]
    if kind == "none":
        return pd.DataFrame({"raw_value": np.nan, "ready": True}, index=frame.index)
    close = frame["close"].where(np.isfinite(frame["close"]) & (frame["close"] > 0))
    valid = close.notna()
    if kind == "channel_width_pct":
        raw = (close.rolling(window).max() - close.rolling(window).min()) / close * 100
    elif kind == "realized_vol_pct":
        raw = close.pct_change(fill_method=None).rolling(window).std(ddof=1) * 100
    else:
        valid = ohlc_valid(frame)
        if not valid.any():
            return pd.DataFrame({"raw_value": np.nan, "ready": False}, index=frame.index)
        up = frame["high"].diff()
        down = -frame["low"].diff()
        plus = up.where((up > down) & (up > 0), 0.0).where(valid)
        minus = down.where((down > up) & (down > 0), 0.0).where(valid)
        tr = atr(frame, window)
        plus_di = 100 * plus.ewm(alpha=1 / window, adjust=False, min_periods=window).mean() / tr
        minus_di = 100 * minus.ewm(alpha=1 / window, adjust=False, min_periods=window).mean() / tr
        total = plus_di + minus_di
        dx = (100 * (plus_di - minus_di).abs() / total).where(total != 0, 0.0)
        dx = dx.where(tr.notna())
        raw = dx.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    bars = max(window, rule["warmup_bars"])
    ready = raw.notna() & (valid.rolling(bars).sum() == bars)
    return pd.DataFrame({"raw_value": raw, "ready": ready}, index=frame.index)


def filter_evidence(rule, raw_value=None, ready=False):
    evidence = dict(rule)
    finite = raw_value is not None and np.isfinite(raw_value)
    ready = bool(ready and finite) if rule["kind"] != "none" else True
    passed = ready and (rule["kind"] == "none" or
                        (float(raw_value) > rule["threshold"] if rule["op"] == "gt"
                         else float(raw_value) < rule["threshold"]))
    evidence.update(raw_value=float(raw_value) if finite else None,
                    ready=ready, passed=bool(passed),
                    reason="disabled" if rule["kind"] == "none" else
                           "indicator_not_ready" if not ready else
                           "passed" if passed else "threshold_not_met")
    return evidence


def initial_risk(entry_price, direction, tp, sl, entry_atr):
    state = {"entry_atr": dict(entry_atr), "take_profit_level": None,
             "stop_loss_level": None, "trailing_extreme": None, "trailing_level": None}
    for field, rule, sign in (("take_profit_level", tp, direction),
                              ("stop_loss_level", sl, -direction)):
        if rule["kind"] in {"fixed_pct", "atr_multiple"}:
            distance = (entry_price * rule["value"] / 100 if rule["kind"] == "fixed_pct"
                        else entry_atr[str(rule["atr_window"])] * rule["value"])
            state[field] = float(entry_price + sign * distance)
    if tp["kind"] == "trailing_pct":
        state["trailing_extreme"] = float(entry_price)
        state["trailing_level"] = float(entry_price * (1 - direction * tp["value"] / 100))
    return state


def risk_touch(state, direction, candle, tp, sl, candle_utc):
    """Evaluate against prior trailing state, then advance the extreme.

    We cannot establish whether this candle's high preceded its low. Raising a
    trailing threshold from its high and testing its low would invent a path.
    Only tomorrow may use today's improved extreme.
    """
    high, low = float(candle["high"]), float(candle["low"])
    sl_level = state.get("stop_loss_level")
    tp_level = (state.get("trailing_level") if tp["kind"] == "trailing_pct"
                else state.get("take_profit_level"))
    stop_hit = sl_level is not None and (low <= sl_level if direction > 0 else high >= sl_level)
    profit_hit = tp_level is not None and (
        (low <= tp_level if direction > 0 else high >= tp_level) if tp["kind"] == "trailing_pct"
        else (high >= tp_level if direction > 0 else low <= tp_level))
    trigger = None
    if stop_hit or profit_hit:
        reason = ("stop_loss" if stop_hit else
                  "trailing_take_profit" if tp["kind"] == "trailing_pct" else "take_profit")
        level = sl_level if stop_hit else tp_level
        trigger = {
            "reason": reason, "candle_utc": candle_utc, "trigger_level": float(level),
            "observed_price": low if (stop_hit and direction > 0) or
                              (profit_hit and direction < 0 and tp["kind"] != "trailing_pct") or
                              (profit_hit and direction > 0 and tp["kind"] == "trailing_pct") else high,
            "observed_open": float(candle["open"]), "observed_high": high,
            "observed_low": low, "observed_close": float(candle["close"]),
            "take_profit_level": tp_level, "stop_loss_level": sl_level,
            "dual_touch": bool(stop_hit and profit_hit), "dual_touch_policy": "stop_loss_first",
        }
    if tp["kind"] == "trailing_pct":
        previous = state["trailing_extreme"]
        extreme = max(previous, high) if direction > 0 else min(previous, low)
        state["trailing_extreme"] = float(extreme)
        state["trailing_level"] = float(extreme * (1 - direction * tp["value"] / 100))
    return copy.deepcopy(trigger)


def gap_touch(state, direction, open_price, tp, sl, candle_utc):
    """Create a risk trigger when the execution open has crossed a level.

    This is deliberately separate from ``risk_touch``: the opening price is
    available before an execution decision, while the rest of that candle's
    OHLC is not.  The caller fills at this same opening price and never uses a
    target level as a synthetic fill.
    """
    if not np.isfinite(open_price) or open_price <= 0:
        return None
    sl_level = state.get("stop_loss_level")
    tp_level = (state.get("trailing_level") if tp["kind"] == "trailing_pct"
                else state.get("take_profit_level"))
    stop_hit = sl_level is not None and (open_price <= sl_level if direction > 0
                                         else open_price >= sl_level)
    if tp_level is None:
        profit_hit = False
    elif tp["kind"] == "trailing_pct":
        # A trailing level is a protective floor/ceiling, so crossing it in
        # the adverse direction realizes the protected profit at the gap open.
        profit_hit = (open_price <= tp_level if direction > 0
                      else open_price >= tp_level)
    else:
        profit_hit = (open_price >= tp_level if direction > 0
                      else open_price <= tp_level)
    if not stop_hit and not profit_hit:
        return None
    reason = ("stop_loss" if stop_hit else
              "trailing_take_profit" if tp["kind"] == "trailing_pct" else "take_profit")
    level = sl_level if stop_hit else tp_level
    return {
        "reason": reason, "candle_utc": candle_utc,
        "trigger_level": float(level), "observed_price": float(open_price),
        "observed_open": float(open_price), "observed_high": None,
        "observed_low": None, "observed_close": None,
        "take_profit_level": tp_level, "stop_loss_level": sl_level,
        "dual_touch": bool(stop_hit and profit_hit),
        "dual_touch_policy": "stop_loss_first", "gap_at_open": True,
    }

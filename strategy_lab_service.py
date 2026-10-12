"""Isolated forward paper accounts; history warms indicators, never fills trades.

Closed candle t produces a target. Its continuous successor's open is the paper
fill, recorded when that successor closes. This is a delayed proxy, not an
exchange execution. Restarted sessions discard pending orders from old sessions.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import threading
import time
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
BAR_MS = 300_000
MAX_CLOSE_DELAY_MS = 90_000
NATIVE_WARMUP_REFRESH_MS = 30_000
PAIRS = ("BTC/USDT:USDT", "ETH/USDT:USDT")
STATUS_PATH = ROOT / "database/runtime/strategy_lab_status.json"
ACTIVE_VARIANT_SUFFIX = "_100x_maker"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def is_fnew_spec(spec):
    """Fnew accounts carry their own design version and never share the legacy path."""
    return bool(spec.get("fnew_design_version"))


def feed_series(loaded):
    """Yield ``(name, rows)`` for every closed-candle series in a loaded input.

    s33 declares a separate ``spot`` mapping beside the ordinary perpetual pair
    mapping.  Nested maps must be walked explicitly: treating the mapping itself
    as a row list would raise, and silently dropping it would hide a stale spot
    feed from the alignment checks.
    """
    for name, value in (loaded or {}).items():
        if isinstance(value, dict):
            for nested_name, rows in value.items():
                if isinstance(rows, list):
                    yield f"{name}/{nested_name}", rows
        elif isinstance(value, list):
            yield name, value


def normalize_account_spec(spec):
    if is_fnew_spec(spec):
        from strategy_fnew import normalize_fnew_spec
        return normalize_fnew_spec(spec)
    if spec.get("factorial_design_version"):
        from strategy_factorial_signals import normalize_factorial_spec
        return normalize_factorial_spec(spec)
    from strategy_signals import normalize_spec
    return normalize_spec(spec)


def research_pause_reason(spec):
    from strategy_data import strategy_pairs
    allowed = {"BTC/USDT:USDT", "ETH/USDT:USDT", "TRX/USDT:USDT"}
    if is_fnew_spec(spec):
        pairs = spec.get("data_universe") or spec.get("universe") or []
    elif spec.get("factorial_design_version"):
        pairs = spec.get("data_universe")
    else:
        pairs = strategy_pairs(spec)
    return "paused_research_context_required" if any(pair not in allowed for pair in pairs) else None


def evaluate_account(spec, candles, positions=None, as_of_utc=None):
    if spec.get("execution_enabled") is False:
        return blocked_evaluation(spec, spec.get("blocked_execution_reason", "execution_disabled"))
    reason = research_pause_reason(spec)
    if reason:
        return blocked_evaluation(spec, reason)
    if is_fnew_spec(spec):
        from strategy_fnew_signals import evaluate_fnew
        return evaluate_fnew(spec, candles, positions=positions, as_of_utc=as_of_utc)
    if spec.get("factorial_design_version"):
        from strategy_factorial_signals import evaluate_factorial
        return evaluate_factorial(spec, candles, positions=positions, as_of_utc=as_of_utc)
    from strategy_signals import evaluate
    return evaluate(spec, candles, positions=positions, as_of_utc=as_of_utc)


def _load_strategy_document(path=None):
    """Read the strategy document without applying an execution variant."""
    return json.loads(Path(path or ROOT / "research_multi_strategy/strategy_specs.json")
                      .read_text(encoding="utf-8"))


def load_specs(path=None):
    from strategy_signals import DEFAULTS, normalize_spec
    data = _load_strategy_document(path)
    specs = []
    for item in data["strategies"]:
        # The original ids identify the archived 1x paper accounts.  A change
        # in leverage is an immutable execution-contract change, so the live
        # 100x run gets a distinct account identity while retaining the frozen
        # research mechanism and Chinese display name.
        base_id = str(item["strategy_id"])
        # Research specs may land before their pure signal implementation.
        # Keep those hypotheses in the research document, but do not create a
        # partial account whose rule hash/execution contract cannot be checked.
        canonical_id = base_id
        for suffix in ("_B15m_100x_maker", "_Cgen2_100x_maker",
                       "_Dgen3_100x_maker", "_100x_maker", "_100x", "_v2"):
            if canonical_id.endswith(suffix):
                canonical_id = canonical_id[:-len(suffix)]
                break
        if canonical_id not in DEFAULTS:
            continue
        if base_id.startswith("s01_") or base_id.startswith("s02_") or \
                base_id.startswith("s03_") or base_id.startswith("s04_") or \
                base_id.startswith("s05_") or base_id.startswith("s06_") or \
                base_id.startswith("s07_") or base_id.startswith("s08_") or \
                base_id.startswith("s09_") or base_id.startswith("s10_") or \
                base_id.startswith("s11_"):
            arm_id, arm_suffix = "B_15m_maker", "_B15m_100x_maker"
        elif base_id.startswith("s12_") or base_id.startswith("s13_") or \
                base_id.startswith("s14_") or base_id.startswith("s16_") or \
                base_id.startswith("s17_") or base_id.startswith("s18_") or \
                base_id.startswith("s19_") or base_id.startswith("s20_") or \
                base_id.startswith("s21_"):
            arm_id, arm_suffix = "C_gen2_multitf", "_Cgen2_100x_maker"
        elif base_id.startswith(("s22_", "s23_", "s24_", "s25_", "s26_",
                                 "s27_", "s28_", "s29_", "s30_")):
            arm_id, arm_suffix = "D_gen3_multitf", "_Dgen3_100x_maker"
        else:
            arm_id, arm_suffix = "active_unknown", ACTIVE_VARIANT_SUFFIX
        variant_id = f"{base_id}{arm_suffix}"
        # Keep each research mechanism's native timeframe and data universe in
        # its immutable definition.  The paper engine trades the BTC/ETH legs
        # only; wider universes (s13 breadth, s19 market breadth) remain
        # indicator context and must never be silently treated as trade legs.
        native_universe = list(item.get("universe", PAIRS))
        trade_universe = [pair for pair in PAIRS if pair in native_universe]
        if not trade_universe:
            trade_universe = list(PAIRS)
        budget = dict(item.get("position_budget") or {}, target_exposure=0.3,
                      max_positions=2, max_per_coin=1,
                      sizing="剩余预算 / 剩余槽位", leverage=100,
                      leverage_convention="A：保证金不变，名义 ×100，逐根强平")
        spec = dict(item, strategy_id=variant_id,
                    params=item["params_frozen"], initial_capital=10000.0,
                    fee_rate_per_side=0.0002, position_budget=budget,
                    execution_universe=trade_universe,
                    research_universe=list(item.get("research_universe_context", native_universe)),
                    research_timeframe=item.get("timeframe", "5m"),
                    label=f"{item.get('name', base_id)} · 100× · maker 0.02%/边",
                    variant_of=base_id, variant_label="100× 杠杆新口径", arm_id=arm_id)
        normalized = normalize_spec(spec)
        specs.append({**spec, **normalized})
    if len(specs) < 10 or len({s["strategy_id"] for s in specs}) != len(specs):
        raise ValueError("at least ten distinct strategy identities required")
    for spec in specs:
        budget = spec.get("position_budget", {})
        if (budget.get("target_exposure") != 0.3 or budget.get("max_positions") != 2
                or budget.get("max_per_coin") != 1 or budget.get("leverage") != 100):
            raise ValueError("strategy research and execution budgets must match")
        if spec.get("timeframe", "5m") != "5m":
            # Native timeframe loading is active.  Wider research universes
            # remain context only; they are never silently converted into
            # execution legs.
            spec["data_status"] = "native_timeframe_adapter_active"
        if spec.get("variant_of") == "s24_cross_sectional_lowvol":
            # The signal can rank the 20-coin context, but the current paper
            # ledger cannot express six simultaneous long/short legs.
            spec["data_status"] = "cross_sectional_execution_pending"
    return specs


def load_2x2_specs(path=None, *, activate=False):
    """Load the focused 2x2 execution population behind an explicit gate.

    The source document is read as canonical signal definitions, then the
    declarative factory derives four immutable execution identities per focus
    family.  A normal call returns disabled, auditable candidates.  The lab
    CLI passes ``activate=True`` only when ``--risk-execution-2x2`` is present;
    even then existing persisted identities are protected by
    :func:`resume_frozen_specs` and can never be upgraded in place.
    """
    from strategy_2x2_experiment import (build_2x2_candidates,
                                         materialize_2x2_execution_spec)

    data = _load_strategy_document(path)
    source = data.get("strategies")
    if not isinstance(source, list):
        raise ValueError("strategy document must contain a strategies list")
    candidates = build_2x2_candidates(source)
    materialized = [materialize_2x2_execution_spec(candidate)
                    for candidate in candidates]
    if activate:
        for spec in materialized:
            # This is the only activation boundary.  The factory itself stays
            # disabled so importing or inspecting the experiment cannot start
            # paper execution accidentally.
            spec["execution_enabled"] = True
            spec.pop("blocked_execution_reason", None)
    return materialized


def resume_frozen_specs(store, requested_specs):
    """Resume existing identities from their immutable persisted definitions.

    Research edits apply to a new identity. They must never overwrite an
    existing account or prevent that account from resuming its frozen rules.
    """
    from strategy_simulation import execution_identity

    ids = [spec["strategy_id"] for spec in requested_specs]
    with store._connection() as conn:
        rows = conn.execute("""SELECT strategy_id,definition,rule_hash,execution_hash
            FROM strategy_sim_accounts WHERE strategy_id=ANY(%s)""", (ids,)).fetchall()
    frozen = {row["strategy_id"]: row for row in rows}
    selected, deferred = [], []
    for requested in requested_specs:
        row = frozen.get(requested["strategy_id"])
        if row is None:
            selected.append(requested)
            continue
        definition = copy.deepcopy(row["definition"])
        if definition.get("strategy_id") != row["strategy_id"]:
            raise ValueError("persisted frozen strategy identity mismatch")
        normalized = normalize_account_spec(definition)
        identity = execution_identity(definition)
        if (normalized["rule_hash"] != row["rule_hash"]
                or identity["execution_hash"] != row["execution_hash"]):
            raise ValueError("persisted frozen strategy identity mismatch")
        if (requested["rule_hash"] != row["rule_hash"]
                or execution_identity(requested)["execution_hash"] != row["execution_hash"]):
            deferred.append(requested["strategy_id"])
        selected.append(definition)
    return selected, deferred


def parse_warmup_klines(rows, boundary_ms, fetched_at_ms):
    """Binance public futures REST, explicit closeTime validation, no interpolation."""
    result = []
    for row in rows:
        if len(row) < 11:
            raise ValueError("incomplete REST kline")
        timestamp = int(row[0])
        if timestamp >= boundary_ms or int(row[6]) >= boundary_ms:
            continue
        if timestamp % BAR_MS or int(row[6]) != timestamp + BAR_MS - 1:
            raise ValueError("invalid REST candle boundary")
        values = [float(row[i]) for i in (1, 2, 3, 4, 5, 7, 9, 10)]
        if not all(math.isfinite(x) for x in values) or min(values[:4]) <= 0:
            raise ValueError("invalid REST price/volume")
        op, hi, lo, close, volume, quote, buy_base, buy_quote = values
        if (lo > min(op, close) or hi < max(op, close) or hi < lo
                or min(volume, quote, buy_base, buy_quote) < 0 or buy_base > volume):
            raise ValueError("invalid REST candle range")
        result.append({"timestamp": timestamp, "open": op, "high": hi, "low": lo,
                       "close": close, "volume": volume,
                       "extras": {"closed": True, "quote_volume": quote,
                                  "num_trades": int(row[8]), "taker_buy_base": buy_base,
                                  "taker_buy_quote": buy_quote,
                                  "source": "binance_futures_rest_indicator_warmup",
                                  "fetched_at_ms": fetched_at_ms}})
    result.sort(key=lambda r: r["timestamp"])
    if not result or any(b["timestamp"] - a["timestamp"] != BAR_MS
                         for a, b in zip(result, result[1:])):
        raise ValueError("REST warmup must be continuous")
    if result[-1]["timestamp"] + BAR_MS != boundary_ms:
        raise ValueError("REST warmup must end at session boundary")
    return result


def fetch_warmup(pair, boundary_ms):
    if pair not in PAIRS:
        raise ValueError("unsupported warmup pair")
    params = urlencode({"symbol": pair.split("/")[0] + "USDT", "interval": "5m",
                        "limit": 750, "endTime": boundary_ms - 1})
    with urlopen("https://fapi.binance.com/fapi/v1/klines?" + params, timeout=15) as response:
        data = json.load(response)
    if not isinstance(data, list):
        raise ValueError("unexpected REST warmup response")
    fetched = int(time.time() * 1000)
    return parse_warmup_klines(data, boundary_ms, fetched)


def merge_closed_history(history, live, now_ms, boundary_ms, frozen_live=None):
    # Historical REST snapshots remain indicator-only, and cannot override new
    # live candles or substitute for a missing live close after the boundary.
    combined = {r["timestamp"]: r for r in history if r["timestamp"] < boundary_ms}
    combined.update(frozen_live or {})
    for row in live:
        stamp = int(row["timestamp"])
        if stamp >= boundary_ms and stamp + BAR_MS <= now_ms and row.get("extras", {}).get("closed") is True:
            # Freeze the first accepted closed version in this session. A later
            # shared-DB revision cannot change earlier indicator inputs.
            if stamp not in combined:
                combined[stamp] = copy.deepcopy(row)
    return sorted(combined.values(), key=lambda row: row["timestamp"])[-750:]


def read_service_status(path=STATUS_PATH, now_ms=None):
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    try:
        data = json.loads(Path(path).read_text())
        fresh = 0 <= now - int(data["heartbeat_at"]) <= 90_000
        alive = False
        if fresh and data.get("running"):
            try:
                os.kill(int(data["pid"]), 0)
                alive = True
            except (OSError, ValueError, TypeError):
                pass
        data["running"] = fresh and alive
        if not data["running"]:
            data["status"] = "stopped_or_stale"
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return {"running": False, "status": "not_started", "last_error": None}


def blocked_evaluation(spec, reason="awaiting_native_timeframe_adapter"):
    """Return a fail-closed warmup result for data the 5m collector cannot feed.

    This keeps generation-2 accounts visible and independently identified while
    preventing a 5m row from being misinterpreted as a 15m/1h/1d observation.
    """
    if is_fnew_spec(spec):
        from strategy_fnew import fnew_rule_hash
        rule_hash = spec.get("rule_hash") or fnew_rule_hash(spec)
    else:
        from strategy_signals import strategy_rule_hash
        rule_hash = spec.get("rule_hash") or strategy_rule_hash(spec)
    strategy_id = str(spec["strategy_id"])
    by_pair = {pair: {"ready": False, "valid_bars": 0,
                      "required_bars": int(spec.get("min_warmup_bars", 0)),
                      "reason": reason}
               for pair in spec.get("execution_universe", PAIRS)}
    return {"strategy_id": strategy_id, "rule_hash": rule_hash, "ready": False,
            "warmup": {"ready": False, "valid_bars": 0,
                       "required_bars": int(spec.get("min_warmup_bars", 0)),
                       "reason": reason, "by_pair": by_pair},
            "signals": {pair: {"candle_utc": None, "target_side": None,
                                "reason": reason, "warmup": by_pair[pair]}
                        for pair in by_pair}}


class StrategyLabService:
    def __init__(self, store, simulation, specs, *, now_ms=None, status_path=STATUS_PATH):
        self.store, self.simulation, self.specs = store, simulation, specs
        observed_now = int(time.time() * 1000 if now_ms is None else now_ms)
        self.boundary = observed_now // BAR_MS * BAR_MS
        # Every execution timeframe gets its own startup boundary.  A daily
        # account must wait for the next daily close after startup; sharing the
        # 5m boundary would either replay an old daily bar or reject every bar
        # until the following session.
        self.boundaries = {"5m": self.boundary}
        try:
            from strategy_data import strategy_timeframe, timeframe_milliseconds
            for spec in specs:
                timeframe = strategy_timeframe(spec)
                interval = timeframe_milliseconds(timeframe)
                self.boundaries.setdefault(timeframe, observed_now // interval * interval)
        except (ImportError, TypeError, ValueError):
            # Test doubles and legacy callers may supply only a minimal spec.
            # Their default execution timeframe remains the existing 5m path.
            pass
        self.status_path = Path(status_path)
        self.history = {}
        # Native-timeframe inputs for generation-2 experiments are kept apart
        # from the legacy paired 5m execution window in ``history``.
        self.strategy_history = {}
        self.warmup_refs = {}
        self.live_history = {p: {} for p in PAIRS}
        self.live_refs = {p: {} for p in PAIRS}
        self.last_processed = None
        self.native_last_processed = {}
        self.native_account_last_processed = {}
        self.native_warmup_refresh = {}
        self.native_warmup_diagnostics = {}
        self.input_cache = {}
        self.status = {"running": True, "status": "starting", "pid": os.getpid(),
                       "session_boundary_ms": self.boundary, "account_count": len(specs),
                       "factorial_account_count": sum(bool(s.get("factorial_design_version")) for s in specs),
                       "factorial_execution_enabled_count": sum(bool(s.get("factorial_design_version"))
                                                                  and s.get("execution_enabled") is not False for s in specs),
                       "session_boundaries_ms": dict(self.boundaries),
                       "last_cycle_at": None, "last_error": None,
                       "warmup_source": "official_rest_indicator_only"}

    def load_strategy_inputs(self, spec, *, now_ms=None, limit=750):
        """Load one strategy's native closed-candle input window.

        The active execution loop still consumes ``history`` (paired 5m BTC/ETH)
        for backwards compatibility.  Generation-2 callers use this explicit
        path so 15m/1h/1d and cross-sectional inputs cannot be silently coerced
        into that legacy window.
        """
        from strategy_data import load_spec_candles
        from strategy_data import strategy_pairs

        observed_at = int(time.time() * 1000) if now_ms is None else int(now_ms)
        if spec.get("execution_enabled") is False or research_pause_reason(spec):
            self.strategy_history[str(spec.get("strategy_id", ""))] = {}
            return {}
        input_spec = spec
        if is_fnew_spec(spec):
            # Fnew feeds are declared per mechanism.  s33 additionally requires a
            # genuine spot market; the cache key must therefore include the feed
            # contract so a basis account can never be served a perpetual-only
            # window that a sibling mechanism already loaded.
            from strategy_fnew_data import load_fnew_candles

            feeds = spec.get("data_feeds") or {}
            key = ("fnew", self._timeframe(spec), tuple(spec.get("data_universe") or ()),
                   bool(feeds.get("spot")), limit, observed_at)
            if key not in self.input_cache:
                self.input_cache[key] = load_fnew_candles(
                    self.store, spec, limit=limit, as_of_ms=observed_at)
            loaded = self.input_cache[key]
            self.strategy_history[str(spec.get("strategy_id", ""))] = loaded
            return loaded
        if spec.get("factorial_design_version"):
            input_spec = dict(spec, universe=spec["data_universe"])
        key = (self._timeframe(input_spec), strategy_pairs(input_spec), limit, observed_at)
        if key not in self.input_cache:
            self.input_cache[key] = load_spec_candles(
                self.store, input_spec, limit=limit, as_of_ms=observed_at)
        loaded = self.input_cache[key]
        strategy_id = str(spec.get("strategy_id", ""))
        self.strategy_history[strategy_id] = loaded
        return loaded

    def publish(self, **changes):
        self.status.update(changes, heartbeat_at=int(time.time() * 1000))
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.status, ensure_ascii=False, allow_nan=False))
        temporary.replace(self.status_path)

    @staticmethod
    def _timeframe(spec):
        return str(spec.get("timeframe", "5m"))

    @staticmethod
    def _canonical_id(spec):
        if is_fnew_spec(spec) or spec.get("factorial_design_version"):
            return spec["canonical_strategy_id"]
        value = str(spec.get("strategy_id", ""))
        for suffix in ("_B15m_100x_maker", "_Cgen2_100x_maker",
                       "_Dgen3_100x_maker", "_100x_maker", "_100x", "_v2"):
            if value.endswith(suffix):
                return value[:-len(suffix)]
        return value

    def _native_specs(self):
        return [spec for spec in self.specs if self._timeframe(spec) != "5m"]

    def _evaluation_spec(self, spec, loaded):
        """Give cross-sectional signals the exact pairs loaded for context.

        The research document may contain descriptive universe entries (for
        example s19's ``20 币（算宽度）``).  ``strategy_data`` resolves those
        entries to its frozen pair list; passing that list to the signal layer
        keeps the data contract explicit without changing the execution hash.
        """
        if spec.get("factorial_design_version"):
            return spec
        if self._canonical_id(spec) in {"s13_daily_trend_multi_horizon",
                                        "s19_multi_coin_breadth",
                                        "s24_cross_sectional_lowvol"}:
            evaluated = dict(spec)
            evaluated["research_universe"] = list(loaded)
            return evaluated
        return spec

    @staticmethod
    def _context_candles(loaded):
        """Return latest-aligned full histories for native signal evaluation.

        Every declared series must share one closing timestamp -- including
        s33's separate spot feed.  A basis signal must never be evaluated from a
        window whose spot and perpetual tails disagree.
        """
        if not loaded:
            return {}
        latest = [rows[-1]["timestamp"] for _name, rows in feed_series(loaded) if rows]
        if not latest or len(set(latest)) != 1:
            return {}
        return loaded

    def _native_trade_candles(self, spec, loaded):
        configured = spec.get("execution_universe", PAIRS)
        if not isinstance(configured, (list, tuple)):
            configured = PAIRS
        pairs = [str(pair) for pair in configured if str(pair) in loaded]
        return {pair: loaded[pair] for pair in pairs}

    def _ensure_accounts_by_timeframe(self):
        grouped = {}
        for spec in self.specs:
            grouped.setdefault(self._timeframe(spec), []).append(spec)
        for timeframe, specs in grouped.items():
            boundary = self.boundaries.get(timeframe, self.boundary)
            self.simulation.ensure_accounts(specs, boundary)

    def _native_bootstrap(self, evaluations):
        """Load native histories and produce their initial warmup evaluations."""
        for spec in self._native_specs():
            if spec.get("execution_enabled") is False or research_pause_reason(spec):
                evaluations[spec["strategy_id"]] = evaluate_account(spec, {})
                continue
            timeframe = self._timeframe(spec)
            boundary = self.boundaries.get(timeframe, self.boundary)
            loaded = self.load_strategy_inputs(spec, now_ms=boundary)
            evaluated = self._evaluation_spec(spec, loaded)
            if not spec.get("factorial_design_version") and self._canonical_id(spec) in {"s13_daily_trend_multi_horizon",
                                            "s19_multi_coin_breadth",
                                            "s24_cross_sectional_lowvol"}:
                evaluated = dict(evaluated, execution_universe=list(loaded))
            evaluations[spec["strategy_id"]] = evaluate_account(
                evaluated, loaded, positions={}, as_of_utc=boundary)

    def _refresh_native_warmup(self, spec, loaded, *, now, interval):
        """Persist current indicator diagnostics without replaying any close."""

        strategy_id = spec["strategy_id"]
        latest = tuple(sorted((name, rows[-1]["timestamp"] if rows else None)
                              for name, rows in feed_series(loaded)))
        previous = self.native_warmup_refresh.get(strategy_id)
        if previous is not None and previous[0] == latest \
                and 0 <= now - previous[1] < NATIVE_WARMUP_REFRESH_MS:
            return
        # Warmup measures available closed history. Its diagnostic snapshot
        # uses that history's closing instant; the independent execution path
        # still checks the real observation time and its strict 90s deadline.
        closes = [stamp + interval for _pair, stamp in latest if stamp is not None]
        as_of = min(now, max(closes)) if closes else now
        diagnostic = evaluate_account(self._evaluation_spec(spec, loaded), loaded,
                              positions={}, as_of_utc=as_of)
        warmup = diagnostic["warmup"]
        if self.native_warmup_diagnostics.get(strategy_id) != warmup:
            self.simulation.update_warmup(
                {strategy_id: diagnostic}, observed_at_ms=now, diagnostic_only=True)
            self.native_warmup_diagnostics[strategy_id] = copy.deepcopy(warmup)
        self.native_warmup_refresh[strategy_id] = (latest, now)

    def _cycle_native(self, now):
        """Advance non-5m accounts from native closed-candle windows.

        Each account receives only its own execution legs when persisted.  The
        full loaded mapping is retained for cross-sectional indicator context.
        This method is intentionally independent of the 5m collector so one
        stale feed cannot suppress another timeframe.
        """
        native = self._native_specs()
        if not native:
            return False

        by_timeframe = {}
        native_status = {}
        for spec in native:
            timeframe = self._timeframe(spec)
            by_timeframe.setdefault(timeframe, []).append(spec)

        for timeframe, specs in by_timeframe.items():
            from strategy_data import timeframe_milliseconds
            interval = timeframe_milliseconds(timeframe)
            loaded_by_id = {}
            latest_by_id = {}
            for spec in specs:
                if spec.get("execution_enabled") is False or research_pause_reason(spec):
                    self._refresh_native_warmup(spec, {}, now=now, interval=interval)
                    loaded_by_id[spec["strategy_id"]] = {}
                    latest_by_id[spec["strategy_id"]] = None
                    continue
                loaded = self.load_strategy_inputs(spec, now_ms=now)
                loaded_by_id[spec["strategy_id"]] = loaded
                self._refresh_native_warmup(spec, loaded, now=now, interval=interval)
                trade = self._native_trade_candles(spec, loaded)
                expected_pairs = spec.get("execution_universe", PAIRS)
                complete = set(trade) == set(expected_pairs) and all(trade.values())
                stamps = [rows[-1]["timestamp"] for rows in trade.values()] if complete else []
                latest_by_id[spec["strategy_id"]] = min(stamps) if stamps else None

            latest_values = [value for value in latest_by_id.values() if value is not None]
            boundary = self.boundaries.get(timeframe, self.boundary)
            factorial = any(spec.get("factorial_design_version") for spec in specs)
            # A delayed third asset must not hold back the timely BTC/ETH arm.
            latest = (max(latest_values) if factorial else min(latest_values)) if latest_values else None
            native_status[timeframe] = {"latest_closed_at": latest,
                                        "session_boundary_ms": boundary,
                                        "strategy_count": len(specs)}
            if latest is None or latest < boundary:
                native_status[timeframe]["status"] = "waiting_new_closed_candle"
                continue
            due_ids = [spec["strategy_id"] for spec in specs
                       if latest_by_id.get(spec["strategy_id"]) == latest
                       and (not factorial or self.native_account_last_processed.get(spec["strategy_id"]) != latest)]
            if (not due_ids if factorial else self.native_last_processed.get(timeframe) == latest):
                # An accepted native close stays current until its successor
                # is due. As in the 5m path, the strict 90s deadline applies
                # only to an unprocessed close, never to a repeated status poll.
                native_status[timeframe]["status"] = (
                    "data_stale" if now - (latest + interval) > interval + MAX_CLOSE_DELAY_MS
                    else "observing")
                continue
            if now - (latest + interval) > MAX_CLOSE_DELAY_MS:
                native_status[timeframe]["status"] = "data_stale"
                continue

            # Preview pending next-open fills once for this timeframe. The
            # simulation adapter filters accounts by native timeframe.
            representative = {}
            for spec in specs:
                loaded = loaded_by_id[spec["strategy_id"]]
                trade = self._native_trade_candles(spec, loaded)
                if trade and all(rows and rows[-1]["timestamp"] == latest
                                 for rows in trade.values()):
                    representative.update(trade)
            if not representative:
                native_status[timeframe]["status"] = "waiting_aligned_data"
                continue
            try:
                positions = self.simulation.preview_positions(
                    representative, observed_at_ms=now,
                    session_boundary_ms=boundary, timeframe=timeframe,
                    strategy_ids=due_ids)
            except TypeError:
                # Keep compatibility with older test doubles and services while
                # the optional timeframe argument rolls through deployments.
                positions = self.simulation.preview_positions(
                    representative, observed_at_ms=now,
                    session_boundary_ms=boundary)
            processed = False
            for spec in specs:
                strategy_id = spec["strategy_id"]
                if strategy_id not in due_ids:
                    continue
                loaded = loaded_by_id[strategy_id]
                evaluated = self._evaluation_spec(spec, loaded)
                context = self._context_candles(loaded)
                if not context:
                    continue
                evaluation = evaluate_account(
                    evaluated, context,
                    positions=positions.get(strategy_id, {}), as_of_utc=now)
                if not evaluation.get("ready"):
                    # Signal evaluation is the authoritative freshness and
                    # cross-asset readiness check.  Never advance an account
                    # with a stale/partial native bar merely because a row
                    # exists in the store.
                    continue
                trade = self._native_trade_candles(spec, loaded)
                if not trade or any(not rows or rows[-1]["timestamp"] != latest
                                    for rows in trade.values()):
                    continue
                self.simulation.process_batch(
                    trade, {strategy_id: evaluation}, observed_at_ms=now,
                    session_boundary_ms=boundary, timeframe=timeframe)
                self.native_account_last_processed[strategy_id] = latest
                processed = True
            if processed:
                self.native_last_processed[timeframe] = latest
                native_status[timeframe]["status"] = "observing"
            else:
                native_status[timeframe]["status"] = "warming_up"

        self.publish(native_timeframes=native_status)
        return True

    def bootstrap(self):
        self.input_cache.clear()
        self._ensure_accounts_by_timeframe()
        evaluations = {}
        if any(self._timeframe(spec) == "5m" for spec in self.specs):
            for pair in PAIRS:
                rows = fetch_warmup(pair, self.boundary)
                payload = {"source": "https://fapi.binance.com/fapi/v1/klines", "pair": pair,
                           "timeframe": "5m", "purpose": "indicator_warmup_only",
                           "boundary_ms": self.boundary, "rows": rows, "fingerprint": fingerprint(rows)}
                # Preserve the evidence for old sessions rather than replacing it
                # with a later history snapshot after a restart.
                snapshot_key = ("strategy_lab/warmup/" + pair.split("/")[0] + "/"
                                + str(self.boundary) + "/" + payload["fingerprint"])
                self.store.put_document(snapshot_key, payload)
                self.warmup_refs[pair] = snapshot_key
                self.history[pair] = rows
            stamps = [[r["timestamp"] for r in self.history[pair]] for pair in PAIRS]
            if stamps[0] != stamps[1]:
                raise ValueError("warmup legs not aligned")
            evaluations.update({
                s["strategy_id"]: evaluate_account(s, self.history, positions={},
                                             as_of_utc=self.boundary)
                for s in self.specs if self._timeframe(s) == "5m"
            })
        # Native-period accounts use their own closed-candle windows and
        # timeframe-specific startup boundary.  They are evaluated here so
        # the API exposes real warmup state instead of a permanent blocked
        # placeholder.
        self._native_bootstrap(evaluations)
        self.simulation.update_warmup(evaluations, observed_at_ms=self.boundary)
        native_warmup_bars = {
            strategy_id: {name: len(rows) for name, rows in feed_series(loaded)}
            for strategy_id, loaded in self.strategy_history.items()
        }
        self.publish(status="waiting_new_closed_candle", last_error=None,
                     warmup_bars={p: len(self.history.get(p, [])) for p in PAIRS},
                     native_warmup_bars=native_warmup_bars)

    def cycle(self, now_ms=None):
        from strategy_signals import evaluate
        self.input_cache.clear()
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        self._cycle_native(now)
        if not any(self._timeframe(spec) == "5m" for spec in self.specs):
            # A native-only service has no reason to query the legacy 5m feed.
            # Keep the native status published by _cycle_native as the visible
            # heartbeat state.
            native_states = [item.get("status") for item in
                             self.status.get("native_timeframes", {}).values()]
            if any(value == "observing" for value in native_states):
                overall = "observing"
            elif any(value == "data_stale" for value in native_states):
                overall = "data_stale"
            elif any(value == "warming_up" for value in native_states):
                overall = "warming_up"
            elif any(value == "waiting_aligned_data" for value in native_states):
                overall = "waiting_aligned_data"
            else:
                overall = "waiting_new_closed_candle"
            self.publish(last_cycle_at=now, status=overall, last_error=None)
            return
        candles = {}
        latest = {}
        for pair in PAIRS:
            live = self.store.get_candles("binance", "futures", pair, "5m", "futures", limit=750)
            candles[pair] = merge_closed_history(self.history[pair], live, now, self.boundary,
                                                 self.live_history[pair])
            for row in candles[pair]:
                candle_ms = row["timestamp"]
                if candle_ms < self.boundary or candle_ms in self.live_history[pair]:
                    continue
                row_hash = fingerprint(row)
                input_key = (f"strategy_lab/live_bars/{self.boundary}/{pair.split('/')[0]}/"
                             f"{candle_ms}/{row_hash}")
                self.store.put_document(input_key, {"pair": pair, "candle": row,
                                                    "fingerprint": row_hash, "observed_at_ms": now})
                self.live_history[pair][candle_ms] = row
                self.live_refs[pair][candle_ms] = input_key
            retained = {r["timestamp"] for r in candles[pair] if r["timestamp"] >= self.boundary}
            self.live_history[pair] = {t: r for t, r in self.live_history[pair].items() if t in retained}
            self.live_refs[pair] = {t: r for t, r in self.live_refs[pair].items() if t in retained}
            latest[pair] = candles[pair][-1]["timestamp"] if candles[pair] else None
        self.publish(last_cycle_at=now, latest_closed_at=latest)
        if len(set(latest.values())) != 1:
            self.publish(status="waiting_aligned_data")
            return
        stamp = next(iter(latest.values()))
        if stamp is None or stamp < self.boundary:
            self.publish(status="waiting_new_closed_candle")
            return
        if stamp == self.last_processed:
            # A 5m close stays current until its successor is due. The strict
            # 90s acceptance deadline applies only to an unprocessed close.
            if now - (stamp + BAR_MS) > BAR_MS + MAX_CLOSE_DELAY_MS:
                self.publish(status="data_stale")
            else:
                self.publish(status="observing", last_error=None)
            return
        if now - (stamp + BAR_MS) > MAX_CLOSE_DELAY_MS:
            self.publish(status="data_stale")
            return
        # Signals must see positions after the old pending orders' t.open fills.
        # Preview shares the engine's pure transition; process_batch persists it
        # together with the newly generated targets in the account transaction.
        positions = self.simulation.preview_positions(candles, observed_at_ms=now,
                                                      session_boundary_ms=self.boundary)
        active_specs = [s for s in self.specs if s.get("timeframe", "5m") == "5m"]
        evaluations = {s["strategy_id"]: evaluate(s, candles,
                       positions=positions.get(s["strategy_id"], {}), as_of_utc=now)
                       for s in active_specs}
        # Preserve each live input once. Together with the immutable warmup
        # snapshot this reconstructs later indicator windows after DB revisions.
        current = {p: candles[p][-1] for p in PAIRS}
        current_hash = fingerprint(current)
        live_key = f"strategy_lab/live_inputs/{self.boundary}/{stamp}/{current_hash}"
        self.store.put_document(live_key, {"candles": current, "fingerprint": current_hash,
                                           "observed_at_ms": now,
                                           "window_fingerprint": fingerprint(candles),
                                           "warmup_snapshot_keys": self.warmup_refs,
                                           "live_window_snapshot_keys": self.live_refs,
                                           "revision_policy": "first_accepted_closed_version"})
        for evaluation in evaluations.values():
            for target in evaluation.get("signals", {}).values():
                target.setdefault("evidence", {}).update(
                    warmup_snapshot_keys=self.warmup_refs, live_snapshot_key=live_key)
        if evaluations:
            self.simulation.process_batch(candles, evaluations, observed_at_ms=now,
                                          session_boundary_ms=self.boundary, timeframe="5m")
        self.last_processed = stamp
        self.publish(status="observing", last_error=None, last_signal_candle_ms=stamp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--factorial", action=argparse.BooleanOptionalAction, default=True,
                        help="启用独立 ATR 风险因子账户，保留旧冻结账户")
    parser.add_argument("--fnew", action=argparse.BooleanOptionalAction, default=True,
                        help="启用 Fnew(s32-s36)前向人口，保留旧冻结账户")
    parser.add_argument("--fnew-paired-execution", action=argparse.BooleanOptionalAction,
                        default=False,
                        help="显式启用 s32/s36 原子双腿账户；仅在配对执行通过独立验收后开启")
    parser.add_argument("--risk-execution-2x2", action=argparse.BooleanOptionalAction,
                        default=False,
                        help="显式启用重点策略 2x2 杠杆/止损对照账户（默认关闭）")
    args = parser.parse_args()
    if not 1 <= args.interval <= 60:
        parser.error("interval must be between 1 and 60 seconds")
    from runtime_config import load_environment
    from data_store import DataStore
    from strategy_simulation import StrategySimulation
    load_environment(ROOT)
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = (STATUS_PATH.parent / "strategy_lab.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("strategy lab already running")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    store = DataStore(root=ROOT)
    service = None
    try:
        requested = load_specs()
        if args.risk_execution_2x2:
            # The focus population is opt-in and gets distinct strategy IDs;
            # resume_frozen_specs below protects any already persisted identity.
            requested += load_2x2_specs(activate=True)
        if args.factorial:
            from strategy_factorial import load_factorial_specs
            requested += load_factorial_specs(normalizer=normalize_account_spec)
        if args.fnew:
            # The Fnew population keeps its own design version and sample
            # population.  Paired entries stay blocked unless the operator
            # explicitly enables them after the pair execution policy has been
            # accepted; blocked accounts are still created so their frozen
            # identity and data readiness stay auditable.
            from strategy_fnew import load_fnew_specs
            existing_ids = {spec["strategy_id"] for spec in requested}
            fnew_specs = load_fnew_specs(paired_execution_enabled=args.fnew_paired_execution)
            collisions = existing_ids & {spec["strategy_id"] for spec in fnew_specs}
            if collisions:
                raise ValueError("Fnew identities collide with existing accounts")
            requested += fnew_specs
        specs, deferred = resume_frozen_specs(store, requested)
        service = StrategyLabService(store, StrategySimulation(store), specs)
        service.publish(identity_changes_deferred=deferred)
        service.bootstrap()
        while not stop.is_set():
            try:
                service.cycle()
            except Exception as exc:
                # Exception messages can contain DB credentials; expose class only.
                print(f"[strategy-lab] cycle failed: {type(exc).__name__}", flush=True)
                service.publish(status="error", last_error=type(exc).__name__)
            stop.wait(args.interval)
    except Exception as exc:
        if service:
            service.publish(running=False, status="startup_failed", last_error=type(exc).__name__)
        print(f"[strategy-lab] startup failed: {type(exc).__name__}", flush=True)
        return 1
    finally:
        if service:
            service.publish(running=False, status=("startup_failed" if service.status["status"] == "startup_failed"
                                                   else "stopped"))
        store.close()
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

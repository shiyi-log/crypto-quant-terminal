"""Frozen Demo-only price-burst continuation from live PostgreSQL candles.

This source emits forward entry intents, not paper fills or historical outcome
labels. An explicit bootstrap creates its immutable identity. All checkpoints
and entry facts live in PostgreSQL ``documents``; a restart never replays bars
as new signals. The runner owns exchange execution, protection and accounting.
"""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import time


BURST_STRATEGY_ID = "price_burst_continuation_5m_v1"
BURST_DEFINITION_KEY = "binance_demo_price_burst_v1_definition.json"
BURST_STATE_KEY = "binance_demo_price_burst_v1_checkpoint.json"
BURST_EVENT_PREFIX = "binance_demo_price_burst_v1_event/"
BAR_MS = 300_000
ATR_WINDOW = 14
WARMUP_BARS = ATR_WINDOW + 2
MAX_SIGNAL_LAG_MS = 90_000
BURST_PAIRS = ("BTC/USDT:USDT", "ETH/USDT:USDT", "TRX/USDT:USDT")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid " + name)
    return value


def _decimal(value, *, positive=False):
    if value is None or isinstance(value, bool):
        raise ValueError("invalid candle number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid candle number") from None
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError("invalid candle number")
    return number


def _identity():
    definition = {
        "strategy_id": BURST_STRATEGY_ID,
        "name": "价格爆发确认后延续（Demo 前向实验）",
        "timeframe": "5m", "universe": list(BURST_PAIRS),
        "params": {"move_bars": 3, "move_threshold": 0.015, "H": 3,
                   "atr_window": ATR_WINDOW, "risk_stop_atr_mult": 1.0},
        "min_warmup_bars": WARMUP_BARS,
        "entry_rule": "close[t]/close[t-3]-1 reaches +/-1.5%; previous closed bar "
                      "was not beyond the same directional threshold; follow the move",
        "threshold_comparison": "inclusive_decimal_price_ratio_v1",
        "atr_convention": "mean_prior_14_true_ranges_excluding_decision_candle_v1",
        "signal_source": "database_live_closed_candle_forward_burst",
        "signal_timing": "after_real_5m_close_never_backdated_to_burst_origin",
        "data_policy": "continuous_closed_live_futures_ohlcv;no_future_outcomes;latest_bar_only",
        "episode_policy": "one_entry_intent_per_pair_directional_threshold_crossing",
        "execution_enabled": True, "risk_sizing_mode": "entry_atr",
        "forward_hypothesis": True, "not_profit_claim": True,
        "paper_mirror": False,
    }
    execution = {
        "execution_enabled": True, "blocked_execution_reason": None,
        "execution_universe": list(BURST_PAIRS), "timeframe": "5m",
        "risk_sizing_mode": "entry_atr", "max_loss_per_trade_usdt": 50.0,
        "fee_rate_per_side": 0.0002, "leverage": 100,
        "risk_stop_atr_mult": 1.0,
        "max_hold_bars": 3, "max_hold_ms": 3 * BAR_MS,
        "hold_clock": "first_actual_entry_fill_ms",
        "stop_policy": "close_confirmed_runner_poll",
        "stop_execution_mode": "owned_reduce_only_market_after_closed_candle_confirmation",
        "stop_limitation": "runner_poll_latency_and_execution_gap_can_exceed_risk_budget",
        "fee_convention": "modeled_maker_fee_for_sizing_only;actual_fees_from_exchange_fills",
        "fill_model": "actual_binance_demo_exchange_fills_only",
        "signal_source": definition["signal_source"],
        "max_signal_lag_ms": MAX_SIGNAL_LAG_MS,
    }
    return {"strategy_id": BURST_STRATEGY_ID, "rule_hash": _hash(definition),
            "execution_hash": _hash(execution), "definition": definition,
            "execution": execution, "state": {"positions": {}}}


class PriceBurstEventSource:
    """PaperEventSource-compatible interface without a paper account or fills.

    ``account`` is a read operation. ``ensure_account`` is the explicit bootstrap
    gate; ``latest_sequence`` also anchors a newly created runner at the current
    forward boundary, without manufacturing an entry from an already closed bar.
    """

    def __init__(self, data_store, *, clock_ms=None):
        self.store = data_store
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    @staticmethod
    def _require_strategy(strategy_id):
        if strategy_id != BURST_STRATEGY_ID:
            raise ValueError("unknown Demo price-burst strategy")

    def _lock(self, conn):
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                     ("demo-price-burst:" + str(getattr(self.store, "schema", "quant")),))

    @staticmethod
    def _read_document(conn, key, *, locked=False):
        row = conn.execute("SELECT payload FROM documents WHERE key=%s" +
                           (" FOR UPDATE" if locked else ""), (key,)).fetchone()
        return copy.deepcopy(row["payload"]) if row is not None else None

    def _insert_document(self, conn, key, payload):
        conn.execute("INSERT INTO documents (key,payload) VALUES (%s,%s) "
                     "ON CONFLICT (key) DO NOTHING", (key, self.store._json(payload)))
        if self._read_document(conn, key) != payload:
            raise ValueError("immutable Demo price-burst document mismatch")

    def _save_state(self, conn, state):
        conn.execute("INSERT INTO documents (key,payload) VALUES (%s,%s) "
                     "ON CONFLICT (key) DO UPDATE SET payload=EXCLUDED.payload,"
                     "updated_at=CURRENT_TIMESTAMP", (BURST_STATE_KEY, self.store._json(state)))

    def _account(self, conn):
        account = self._read_document(conn, BURST_DEFINITION_KEY)
        if account is None:
            raise ValueError("Demo price-burst account has not been bootstrapped")
        expected = _identity()
        if any(account.get(key) != value for key, value in expected.items()):
            raise ValueError("frozen Demo price-burst identity mismatch")
        boundary = _integer(account.get("forward_boundary_ms"), "forward boundary")
        if account.get("created_ms") != boundary:
            raise ValueError("frozen Demo price-burst boundary mismatch")
        return account

    def _state(self, conn, account):
        state = self._read_document(conn, BURST_STATE_KEY, locked=True)
        if state is None:
            raise ValueError("Demo price-burst checkpoint is missing")
        if any(state.get(key) != account[key] for key in ("strategy_id", "rule_hash", "execution_hash")):
            raise ValueError("Demo price-burst checkpoint identity mismatch")
        _integer(state.get("sequence_id"), "event sequence")
        if _integer(state.get("forward_boundary_ms"), "forward boundary") < account["forward_boundary_ms"]:
            raise ValueError("Demo price-burst boundary moved backwards")
        checkpoints = state.get("pair_checkpoints")
        if not isinstance(checkpoints, dict) or set(checkpoints) != set(BURST_PAIRS):
            raise ValueError("invalid Demo price-burst pair checkpoints")
        for value in checkpoints.values():
            if value is not None:
                _integer(value, "candle checkpoint")
        return state

    @staticmethod
    def _latest(conn, pair, now, candle_ms=None):
        params = [pair, now - BAR_MS, BAR_MS]
        exact = ""
        if candle_ms is not None:
            exact = " AND timestamp=%s"
            params.append(_integer(candle_ms, "candle timestamp"))
        return conn.execute("SELECT timestamp,open,high,low,close,volume,extras FROM candles "
            "WHERE exchange='binance' AND market='futures' AND candle_type='futures' "
            "AND pair=%s AND timeframe='5m' AND timestamp<=%s AND timestamp %% %s=0 "
            "AND extras->>'closed'='true' AND extras->>'live_event_timestamp' IS NOT NULL" +
            exact + " ORDER BY timestamp DESC LIMIT 1", tuple(params)).fetchone()

    def _anchor(self, conn, state, now):
        for pair in BURST_PAIRS:
            row = self._latest(conn, pair, now)
            if row is not None:
                previous = state["pair_checkpoints"][pair]
                state["pair_checkpoints"][pair] = max(row["timestamp"], previous or 0)
        state["forward_boundary_ms"] = max(state["forward_boundary_ms"], now)
        state["anchored_ms"] = now

    def ensure_account(self):
        """Explicit, atomic and idempotent bootstrap; existing hashes never change."""
        now = _integer(self.clock_ms(), "clock")
        with self.store._connection() as conn:
            self._lock(conn)
            if self._read_document(conn, BURST_DEFINITION_KEY) is not None:
                account = self._account(conn)
                self._state(conn, account)
                return copy.deepcopy(account)
            if self._read_document(conn, BURST_STATE_KEY) is not None:
                raise ValueError("orphaned Demo price-burst checkpoint")
            account = {**_identity(), "created_ms": now, "forward_boundary_ms": now}
            state = {key: account[key] for key in ("strategy_id", "rule_hash", "execution_hash")}
            state.update(sequence_id=0, forward_boundary_ms=now,
                         pair_checkpoints=dict.fromkeys(BURST_PAIRS), observations={})
            self._anchor(conn, state, now)
            self._insert_document(conn, BURST_DEFINITION_KEY, account)
            self._save_state(conn, state)
        return copy.deepcopy(account)

    def account(self, strategy_id):
        self._require_strategy(strategy_id)
        with self.store._connection() as conn:
            return copy.deepcopy(self._account(conn))

    def latest_sequence(self, strategy_id):
        self._require_strategy(strategy_id)
        now = _integer(self.clock_ms(), "clock")
        with self.store._connection() as conn:
            self._lock(conn)
            state = self._state(conn, self._account(conn))
            self._anchor(conn, state, now)
            self._save_state(conn, state)
            return state["sequence_id"]

    def closed_candle(self, pair, timeframe, candle_ms=None):
        if pair not in BURST_PAIRS or timeframe != "5m":
            return None
        now = _integer(self.clock_ms(), "clock")
        with self.store._connection() as conn:
            return copy.deepcopy(self._latest(conn, pair, now, candle_ms))

    @staticmethod
    def _history(conn, pair, timestamp):
        return conn.execute("SELECT timestamp,open,high,low,close,volume,extras FROM candles "
            "WHERE exchange='binance' AND market='futures' AND candle_type='futures' "
            "AND pair=%s AND timeframe='5m' AND timestamp>=%s AND timestamp<=%s "
            "AND extras->>'closed'='true' AND extras->>'live_event_timestamp' IS NOT NULL "
            "ORDER BY timestamp", (pair, timestamp - (WARMUP_BARS - 1) * BAR_MS,
                                    timestamp)).fetchall()

    @staticmethod
    def _signal(rows, timestamp):
        expected = [timestamp - (WARMUP_BARS - index - 1) * BAR_MS
                    for index in range(WARMUP_BARS)]
        if len(rows) != WARMUP_BARS or [row["timestamp"] for row in rows] != expected:
            return None, {"reason": "missing_contiguous_live_candles"}
        facts = []
        try:
            for row in rows:
                facts.append({key: str(_decimal(row[key], positive=key != "volume"))
                              for key in ("open", "high", "low", "close", "volume")})
                fact = facts[-1]
                if (_decimal(fact["volume"]) < 0
                        or _decimal(fact["low"]) > min(_decimal(fact["open"]), _decimal(fact["close"]))
                        or _decimal(fact["high"]) < max(_decimal(fact["open"]), _decimal(fact["close"]))):
                    raise ValueError("invalid candle range")
        except (KeyError, TypeError, ValueError):
            return None, {"reason": "invalid_live_candle"}

        def direction(close, origin):
            price, base = _decimal(close), _decimal(origin)
            return "buy" if price >= base * Decimal("1.015") else \
                "sell" if price <= base * Decimal("0.985") else None

        side = direction(facts[-1]["close"], facts[-4]["close"])
        previous = direction(facts[-2]["close"], facts[-5]["close"])
        if side is None or side == previous:
            return None, {"reason": "no_new_directional_burst", "direction": side,
                          "previous_direction": previous}
        true_ranges = [max(_decimal(facts[index]["high"]), _decimal(facts[index - 1]["close"]))
                       - min(_decimal(facts[index]["low"]), _decimal(facts[index - 1]["close"]))
                       for index in range(1, len(facts) - 1)]
        atr = sum(true_ranges, Decimal(0)) / ATR_WINDOW
        if atr <= 0:
            return None, {"reason": "entry_atr_unavailable"}
        input_candles = [{**fact, "timestamp": row["timestamp"],
                          "live_event_timestamp": row["extras"]["live_event_timestamp"]}
                         for row, fact in zip(rows, facts)]
        evidence = {"entry_atr": str(atr), "atr_window": ATR_WINDOW,
                    "burst_origin_candle_ms": rows[-4]["timestamp"],
                    "burst_origin_close": facts[-4]["close"],
                    "signal_close": facts[-1]["close"],
                    "move_return": str(_decimal(facts[-1]["close"]) / _decimal(facts[-4]["close"]) - 1),
                    "move_threshold": "0.015", "previous_direction": previous,
                    "source": "database_live_closed_candle_forward_burst",
                    "atr_excludes_decision_candle": True,
                    "input_candles": input_candles,
                    "input_fingerprint": _hash(input_candles)}
        return side, evidence

    def events(self, strategy_id, after_sequence, limit=1000):
        self._require_strategy(strategy_id)
        after_sequence = _integer(after_sequence, "event sequence")
        limit = _integer(limit, "event limit")
        if not 1 <= limit <= 1000:
            raise ValueError("invalid event limit")
        now = _integer(self.clock_ms(), "clock")
        with self.store._connection() as conn:
            self._lock(conn)
            account = self._account(conn)
            state = self._state(conn, account)
            changed = False
            for pair in BURST_PAIRS:
                row = self._latest(conn, pair, now)
                if row is None:
                    continue
                timestamp = _integer(row["timestamp"], "candle timestamp")
                previous = state["pair_checkpoints"][pair]
                if previous is not None and timestamp <= previous:
                    continue
                changed = True
                # Checkpoint even rejected/no-signal bars: late repairs may not
                # manufacture a new forward entry for an already observed bar.
                state["pair_checkpoints"][pair] = timestamp
                close_ms = timestamp + BAR_MS
                if close_ms <= state["forward_boundary_ms"]:
                    state["observations"][pair] = {"candle_ms": timestamp, "reason": "before_forward_boundary"}
                    continue
                if not 0 <= now - close_ms <= MAX_SIGNAL_LAG_MS:
                    state["observations"][pair] = {"candle_ms": timestamp, "reason": "closed_candle_stale"}
                    continue
                side, evidence = self._signal(self._history(conn, pair, timestamp), timestamp)
                state["observations"][pair] = {"candle_ms": timestamp,
                                               "reason": "entry_intent" if side else evidence["reason"]}
                if side is None:
                    continue
                identity = {"strategy_id": strategy_id, "rule_hash": account["rule_hash"],
                            "pair": pair, "candle_ms": timestamp}
                event_id = "price-burst-entry:" + _hash(identity)
                payload = {"kind": "entry", "pair": pair, "buy_sell": side,
                           "signal_candle_ms": timestamp, "size_multiplier": 1.0,
                           "reason": "confirmed_price_burst_continuation", "evidence": evidence,
                           "signal_source": account["definition"]["signal_source"],
                           "paper_next_open_is_proxy": False}
                state["sequence_id"] += 1
                event = {**identity, "sequence_id": state["sequence_id"], "event_id": event_id,
                         "execution_hash": account["execution_hash"],
                         "event_type": "order_submitted", "observed_at_ms": close_ms,
                         "generated_at_ms": now,
                         "event_semantics": "forward_entry_intent_not_exchange_order",
                         "data_fingerprint": _hash(payload), "payload": payload}
                self._insert_document(conn, BURST_EVENT_PREFIX + f"{state['sequence_id']:020d}.json", event)
            if changed:
                self._save_state(conn, state)
            rows = conn.execute("SELECT payload FROM documents WHERE left(key,%s)=%s "
                "AND key>%s ORDER BY key LIMIT %s", (len(BURST_EVENT_PREFIX), BURST_EVENT_PREFIX,
                    BURST_EVENT_PREFIX + f"{after_sequence:020d}.json", limit)).fetchall()
            result = [copy.deepcopy(row["payload"]) for row in rows]
            if any(event.get("strategy_id") != strategy_id
                   or event.get("rule_hash") != account["rule_hash"]
                   or event.get("execution_hash") != account["execution_hash"] for event in result):
                raise ValueError("Demo price-burst event identity mismatch")
            return result

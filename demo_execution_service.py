"""One frozen paper identity mirrored into Binance Demo, never a live endpoint.

The paper ledger remains read-only.  Its forward order intents are the common
signal; an exchange fill is never replaced by a paper next-open assumption.
Credentials belong to the caller, which injects a guarded Demo-only adapter.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import threading
import time

DEMO_POLICY_VERSION = "binance-demo-single-paper-v1"
HOST = "demo-fapi.binance.com"
TERMINAL = {"filled", "canceled", "cancelled", "rejected", "expired"}


def _status(value):
    status = str(value).lower()
    return {"new": "open", "partiallyfilled": "partially_filled"}.get(status, status)


def derive_round_trips(fills, orders=None):
    """Actual chronological fill accounting; unknown costs never become zero."""
    active, completed = {}, []
    def order_key(item):
        trade_id = str(item["exchange_trade_id"])
        return (int(item["timestamp_ms"]), 0, int(trade_id)) if trade_id.isdigit() else (int(item["timestamp_ms"]), 1, trade_id)
    for fill in sorted(fills, key=order_key):
        pair = fill["pair"]
        quantity = _decimal(fill["quantity"], positive=True)
        signed = quantity if fill["side"] == "buy" else -quantity
        group = active.setdefault(pair, {"pair": pair, "opened_ms": int(fill["timestamp_ms"]),
            "signed_quantity": Decimal(0), "gross": Decimal(0), "gross_known": True,
            "commission": {}, "fee_known": True, "maker_fills": 0, "taker_fills": 0,
            "unknown_liquidity_fills": 0, "fills": [], "clients": set()})
        if group["signed_quantity"] and group["signed_quantity"] * signed < 0 and abs(signed) > abs(group["signed_quantity"]):
            raise ValueError("fill sequence crosses a net position; cannot attribute round trip")
        group["signed_quantity"] += signed
        group["fills"].append(fill["exchange_trade_id"])
        if fill.get("client_id"):
            group["clients"].add(fill["client_id"])
        pnl = fill.get("realized_pnl")
        if pnl is None:
            group["gross_known"] = False
        else:
            group["gross"] += _decimal(pnl)
        fee, asset = fill.get("commission"), fill.get("commission_asset")
        if fee is None or asset is None or fill.get("commission_conflict"):
            group["fee_known"] = False
        else:
            group["commission"][asset] = group["commission"].get(asset, Decimal(0)) + _decimal(fee)
        maker = fill.get("is_maker")
        group["maker_fills" if maker is True else "taker_fills" if maker is False else "unknown_liquidity_fills"] += 1
        if group["signed_quantity"] == 0:
            fee_usdt_known = group["fee_known"] and set(group["commission"]) <= {"USDT"}
            net = group["gross"] - group["commission"].get("USDT", Decimal(0)) if fee_usdt_known and group["gross_known"] else None
            completed.append({"pair": pair, "opened_ms": group["opened_ms"],
                "closed_ms": int(fill["timestamp_ms"]),
                "hold_seconds": str(Decimal(int(fill["timestamp_ms"]) - group["opened_ms"]) / 1000),
                "actual_realized_pnl_usdt": str(group["gross"]) if group["gross_known"] else None,
                "actual_commission_by_asset": {asset: str(fee) for asset, fee in group["commission"].items()},
                "actual_net_trading_pnl_usdt": str(net) if net is not None else None,
                "fee_complete": group["fee_known"], "usdt_conversion_known": fee_usdt_known,
                "maker_fills": group["maker_fills"], "taker_fills": group["taker_fills"],
                "unknown_liquidity_fills": group["unknown_liquidity_fills"],
                "exchange_trade_ids": group["fills"], "trading_pnl_excludes_funding": True,
                "client_ids": sorted(group["clients"]),
                "environment": "binance_demo", "live_profitability_verified": False})
            del active[pair]
    if orders is not None:
        totals = {}
        for fill in fills:
            client_id = fill["client_id"]
            totals[client_id] = totals.get(client_id, Decimal(0)) + _decimal(fill["quantity"])
        incomplete = {row["client_id"] for row in orders
                      if _decimal(row["filled_quantity"]) != totals.get(row["client_id"], Decimal(0))}
        for trade in completed:
            complete = not bool(set(trade["client_ids"]) & incomplete)
            trade["fill_reconciliation_complete"] = complete
            if not complete:
                trade["actual_realized_pnl_usdt"] = None
                trade["actual_net_trading_pnl_usdt"] = None
                trade["fee_complete"] = False
    return completed


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _decimal(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError("invalid decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("invalid decimal") from exc
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError("invalid decimal")
    return number


def _stamp(value):
    if isinstance(value, bool) or int(value) != value or value < 0:
        raise ValueError("invalid millisecond timestamp")
    return int(value)


def signal_reference_evidence(candle, timeframe):
    """Freeze a live-paper candle close separately from the Demo order price."""
    evidence = {"signal_reference_price": None, "reference_source": None,
                "reference_timestamp_ms": None,
                "reference_comparison_scope": "demo_vs_live_paper_includes_environment_and_latency",
                "live_executable_slippage_verified": False}
    if not isinstance(candle, dict) or candle.get("extras", {}).get("closed") is not True:
        return evidence
    try:
        from strategy_data import TIMEFRAME_MS
        price = str(_decimal(candle["close"], positive=True))
        closed_ms = _stamp(candle["timestamp"]) + TIMEFRAME_MS[timeframe]
    except (KeyError, TypeError, ValueError):
        return evidence
    return {**evidence, "signal_reference_price": price,
            "reference_source": "paper_closed_candle_from_live_market",
            "reference_timestamp_ms": closed_ms}


def attempted_entry_outcomes(orders):
    """Match DemoStore's population, without claiming exchange acceptance."""
    attempted = [order for order in orders if not order.get("reduce_only")
                 and order["status"] in {"filled", "canceled", "rejected", "expired"}
                 and (order.get("submission_attempts") == 1
                      or order.get("exchange_order_id") is not None)]
    unfilled = sum(_decimal(order["filled_quantity"]) == 0 for order in attempted)
    return {"terminal_entry_count": len(attempted), "unfilled_entry_count": unfilled,
            "unfilled_entry_ratio": unfilled / len(attempted) if attempted else None,
            "unfilled_entry_population": "terminal_attempted_entries_not_verified_accepted",
            "unfilled_entry_definition": "zero_reported_filled_quantity_in_terminal_attempted_entry"}


@dataclass(frozen=True)
class DemoRunnerConfig:
    strategy_id: str
    virtual_account_key: str = "demo_default"
    trading_enabled: bool = False
    max_notional_usdt: str = "1000"
    entry_order_ttl_ms: int = 30_000
    max_signal_lag_ms: int = 90_000
    poll_interval_ms: int = 5_000

    def __post_init__(self):
        if not self.strategy_id or not self.virtual_account_key:
            raise ValueError("one explicit paper identity and Demo account are required")
        if not isinstance(self.trading_enabled, bool):
            raise ValueError("trading_enabled must be boolean")
        _decimal(self.max_notional_usdt, positive=True)
        for value in (self.entry_order_ttl_ms, self.max_signal_lag_ms, self.poll_interval_ms):
            if isinstance(value, bool) or int(value) != value or not 100 <= value <= 300_000:
                raise ValueError("invalid Demo timing limit")

    def policy(self):
        return {"version": DEMO_POLICY_VERSION, "endpoint_host": HOST,
                "strategy_id": self.strategy_id, "virtual_account_key": self.virtual_account_key,
                "max_notional_usdt": str(_decimal(self.max_notional_usdt, positive=True)),
                "entry_order_ttl_ms": self.entry_order_ttl_ms,
                "max_signal_lag_ms": self.max_signal_lag_ms,
                "entry_type": "post_only_limit_at_signal_close",
                "protective_exit_type": "reduce_only_market",
                "sizing": "paper_atr_risk_proxy_capped_by_demo_notional",
                "risk_loss_bound": "not_guaranteed_gap_execution_fee_unknown",
                "comparison_scope": "same_signal_execution_only_not_full_portfolio_returns"}


class PaperEventSource:
    """Only reads existing paper identities and append-only execution events."""

    def __init__(self, data_store, *, clock_ms=None):
        self.store = data_store
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def account(self, strategy_id):
        with self.store._connection() as conn:
            row = conn.execute("SELECT * FROM strategy_sim_accounts WHERE strategy_id=%s",
                               (strategy_id,)).fetchone()
        if row is None:
            raise ValueError("paper account does not exist")
        # Validate the actual frozen definition, rather than a mutable proposal.
        from strategy_simulation import execution_identity
        identity = execution_identity(row["definition"])
        if (identity["rule_hash"] != row["rule_hash"]
                or identity["execution_hash"] != row["execution_hash"]):
            raise ValueError("frozen paper identity mismatch")
        return copy.deepcopy(row)

    def latest_sequence(self, strategy_id):
        with self.store._connection() as conn:
            row = conn.execute("SELECT COALESCE(MAX(sequence_id),0) AS sequence_id "
                               "FROM strategy_sim_events WHERE strategy_id=%s",
                               (strategy_id,)).fetchone()
        return int(row["sequence_id"])

    def events(self, strategy_id, after_sequence, limit=1000):
        with self.store._connection() as conn:
            rows = conn.execute("SELECT * FROM strategy_sim_events WHERE strategy_id=%s "
                                "AND sequence_id>%s ORDER BY sequence_id LIMIT %s",
                                (strategy_id, int(after_sequence), int(limit))).fetchall()
        return rows

    def closed_candle(self, pair, timeframe, candle_ms=None):
        from strategy_data import TIMEFRAME_MS
        bar_ms = TIMEFRAME_MS[timeframe]
        now = self.clock_ms()
        params = [pair, timeframe, now - bar_ms, bar_ms]
        exact = ""
        if candle_ms is not None:
            exact = " AND timestamp=%s"
            params.append(int(candle_ms))
        with self.store._connection() as conn:
            return conn.execute("SELECT timestamp,open,high,low,close,volume,extras FROM candles "
                "WHERE exchange='binance' AND market='futures' AND candle_type='futures' "
                "AND pair=%s AND timeframe=%s AND timestamp<=%s AND timestamp %% %s=0 "
                "AND extras->>'closed'='true'" + exact + " ORDER BY timestamp DESC LIMIT 1",
                tuple(params)).fetchone()


class DemoExecutionService:
    """Caller-owned lifecycle: pause still reconciles and protects owned exposure.

    ``start`` is the human Demo gate.  ``cycle`` is serialized and can be called
    by a controller thread.  Stopping entries never abandons an open position.
    A submission is claimed durably BEFORE the HTTP call and is attempted once.
    """

    def __init__(self, adapter, demo_store, paper_source, config, *, clock_ms=None):
        self.adapter, self.store, self.paper = adapter, demo_store, paper_source
        self.config = config
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.policy = config.policy()
        self.policy_hash = _hash(self.policy)
        self.account = None
        self.binding = None
        self.last_sequence = 0
        self._mutex = threading.RLock()
        self._started = False
        self._entries_enabled = False
        self._actual_net = {}
        self._metadata_checked_ms = None
        self._metadata_errors = []
        self._fee_rechecked_ms = {}
        self._status = {"running": False, "entries_enabled": False, "status": "not_started",
                        "environment": "binance_demo", "endpoint_host": HOST,
                        "strategy_id": config.strategy_id, "demo_policy_hash": self.policy_hash,
                        "live_enabled": False, "hypothesis_status": "forward_research",
                        "seconds_hypothesis_tested": False,
                        "experiment_scope": "selected_candle_strategy_execution_only",
                        "commission_source": "actual_exchange_fills_only",
                        "comparison_scope": self.policy["comparison_scope"]}

    def _evidence(self, signal_id, fingerprint=None):
        return {"strategy_id": self.config.strategy_id, "signal_id": str(signal_id),
                "rule_hash": self.account["rule_hash"],
                "execution_hash": self.account["execution_hash"],
                "input_fingerprint": fingerprint or self.binding["input_fingerprint"],
                "demo_policy_hash": self.policy_hash,
                "cannot_attribute_full_portfolio_returns": True}

    def _record(self, stage, signal_id, payload, *, fingerprint=None, now=None):
        return self.store.append_comparison({**self._evidence(signal_id, fingerprint),
                                             **copy.deepcopy(payload)}, stage=stage,
                                            observed_ms=self.clock_ms() if now is None else now)

    def start(self, *, entries_enabled=None):
        with self._mutex:
            if self._started:
                return self.status()
            now = _stamp(self.clock_ms())
            self.account = self.paper.account(self.config.strategy_id)
            execution = self.account["execution"]
            if execution.get("execution_enabled") is False:
                raise ValueError("paper account is blocked")
            if execution.get("risk_sizing_mode") != "entry_atr":
                raise ValueError("Demo MVP requires a frozen single-leg ATR risk definition")
            definition = self.account["definition"]
            if (definition.get("paired_atr_stop_policy")
                    or execution.get("risk_sizing_mode") == "entry_pair_atr"):
                raise ValueError("atomic pair execution is outside this Demo MVP")
            pairs = execution.get("execution_universe") or []
            if not pairs or len(set(pairs)) != len(pairs):
                raise ValueError("invalid paper execution universe")
            if self.store.virtual_account_key != self.config.virtual_account_key:
                raise ValueError("Demo ledger account mapping mismatch")
            existing = self.store.get_binding()
            expected = {"strategy_id": self.config.strategy_id, "signal_id": "binding",
                        "rule_hash": self.account["rule_hash"],
                        "execution_hash": self.account["execution_hash"],
                        "input_fingerprint": _hash(definition),
                        "demo_policy_hash": self.policy_hash, "demo_policy": self.policy}
            if existing:
                # Some stores return the immutable observation wrapper.
                existing = existing.get("payload", existing)
                for key in expected:
                    if existing.get(key) != expected[key]:
                        raise ValueError("Demo account is already bound to another identity or policy")
                self.binding = existing
                checkpoints = self.store.list_comparisons(stage="runner_checkpoint", limit=1)
                self.last_sequence = int((checkpoints[0].get("payload", checkpoints[0])
                                          if checkpoints else existing).get("last_sequence_id", 0))
            else:
                self.last_sequence = self.paper.latest_sequence(self.config.strategy_id)
                self.binding = {**expected, "forward_boundary_ms": now,
                                "last_sequence_id": self.last_sequence,
                                "paper_initially_flat": not bool(self.account["state"].get("positions"))}
                self.store.bind_account(self.binding)
            self._started = True
            self._entries_enabled = self.config.trading_enabled and entries_enabled is not False
            self._status.update(running=True, entries_enabled=self._entries_enabled,
                                status="observing" if self._entries_enabled else "read_only",
                                forward_boundary_ms=self.binding["forward_boundary_ms"],
                                rule_hash=self.account["rule_hash"],
                                execution_hash=self.account["execution_hash"])
            self.cycle()
            return self.status()

    def enable_entries(self):
        with self._mutex:
            if not self._started or not self.config.trading_enabled:
                raise RuntimeError("human Demo start with trading enabled is required")
            self._entries_enabled = True
            self._status["entries_enabled"] = True
            return self.cycle()

    def status(self):
        with self._mutex:
            return copy.deepcopy(self._status)

    def stop(self, *, flatten=False):
        """Pause entries; retain polling/risk protection until all owned exposure closes."""
        with self._mutex:
            self._entries_enabled = False
            self._status.update(entries_enabled=False, status="paused_protecting")
            if self._started:
                now = self.clock_ms()
                self.cycle(now)
                if flatten and self.config.trading_enabled:
                    self._flatten_owned(now, "human_demo_flatten")
                self.cycle(now)
            return self.status()

    def _orders(self):
        return self.store.runner_orders()

    @staticmethod
    def _intent(row):
        return row.get("intent_payload") or row.get("intent") or row.get("raw", {}).get("intent") or row.get("raw", {})

    def _net(self):
        result = self.store.net_fill_positions()
        if isinstance(result, dict):
            return {pair: _decimal(qty) for pair, qty in result.items() if _decimal(qty)}
        return {row["pair"]: _decimal(row["signed_quantity"]) for row in result
                if _decimal(row["signed_quantity"])}

    def _fills(self, row, now):
        exchange_id = row.get("exchange_order_id")
        if not exchange_id:
            return
        trades = self.adapter.fetch_trades(row["pair"], order_id=str(exchange_id), limit=1000)
        from_id = None
        for _ in range(1000):
            for fill in trades:
                if str(fill.get("order_id")) == str(exchange_id):
                    self.store.record_fill(row["client_id"], {**fill, "observed_ms": now})
            confirmed = sum((_decimal(fill["quantity"]) for fill in self.store.runner_fills()
                             if fill["client_id"] == row["client_id"]), Decimal(0))
            if confirmed >= _decimal(row["filled_quantity"]):
                return
            if not trades:
                self._status["reconciliation_alert"] = "filled_quantity_reconciliation_incomplete"
                self.store.append_reconciliation(row["client_id"],
                    {"confirmed_exchange_quantity": str(row["filled_quantity"]),
                     "recorded_fill_quantity": str(confirmed), "fill_complete": False}, observed_ms=now)
                return
            next_id = max(int(fill["fill_id"]) for fill in trades) + 1
            if from_id is not None and next_id <= from_id:
                raise RuntimeError("Demo fill pagination failed to advance")
            from_id = next_id
            # Binance does not document combining fromId with orderId/time.
            # Page the symbol stream and retain only the already-owned order.
            trades = self.adapter.fetch_trades(row["pair"], from_id=from_id, limit=1000)
        raise RuntimeError("Demo fill pagination limit reached")

    def _reconcile(self, now, *, cancel_entries=False):
        for row in self._orders():
            try:
                self._reconcile_order(row, now, cancel_entries=cancel_entries)
            except Exception as exc:
                self._status["reconciliation_alert"] = type(exc).__name__
                # A broken unrelated lookup must not suppress owned protection.
                continue

    def _reconcile_order(self, row, now, *, cancel_entries=False):
            client_id = row["client_id"]
            status = row["status"]
            if status == "intent" and not row.get("reduce_only"):
                expires = int(self._intent(row).get("expires_ms", row["created_ms"] + self.config.entry_order_ttl_ms))
                if cancel_entries or now >= expires:
                    self.store.abandon_unclaimed_order(client_id, "entries_paused" if cancel_entries else "entry_ttl", observed_ms=now)
                    return
            self.adapter.authorize_owned_client_id(client_id)
            existing_fills = [fill for fill in self.store.runner_fills() if fill["client_id"] == client_id]
            filled = sum((_decimal(fill["quantity"]) for fill in existing_fills), Decimal(0))
            fee_pending = any(fill.get("commission") is None or fill.get("commission_asset") is None
                              or fill.get("commission_conflict") or fill.get("realized_pnl") is None
                              or fill.get("is_maker") is None
                              for fill in existing_fills)
            fee_due = fee_pending and (now < row["created_ms"] + 600_000
                                       or now - self._fee_rechecked_ms.get(client_id, 0) >= 60_000)
            needs_lookup = (status not in TERMINAL or filled != _decimal(row["filled_quantity"])
                            or fee_due)
            # Query terminal orders too: commissions may arrive after order closure.
            order = None
            if needs_lookup:
                self._fee_rechecked_ms[client_id] = now
                order = self.adapter.fetch_order_by_client_id(row["pair"], client_id)
            if order:
                row = self.store.record_order_observation(client_id, {**order, "observed_ms": now})
                self._fills(row, now)
            if status in ("submitting", "unknown") and not order:
                self._status["submission_unknown"] = True
                # A timeout / absent lookup is NEVER permission to submit again.
                return
            intent = self._intent(row)
            expires = int(intent.get("expires_ms", row["created_ms"] + self.config.entry_order_ttl_ms))
            is_entry = not row.get("reduce_only")
            if (self.config.trading_enabled and is_entry and row["status"] not in TERMINAL
                    and row.get("exchange_order_id") and (cancel_entries or now >= expires)):
                canceled = self.adapter.cancel_order(row["pair"], row["exchange_order_id"],
                                                      client_order_id=client_id)
                self.store.record_order_observation(client_id, {**canceled, "observed_ms": now})
                # A cancellation acknowledgement does not erase a concurrent partial fill.
                after = self.adapter.fetch_order_by_client_id(row["pair"], client_id)
                if after:
                    row = self.store.record_order_observation(client_id, {**after, "observed_ms": now})
                self._fills(row, now)
                self._record("entry_expiry", row["signal_id"],
                             {"client_id": client_id, "filled_quantity": str(row["filled_quantity"]),
                              "non_fill": _decimal(row["filled_quantity"]) == 0,
                              "cancellation_reason": "paused" if cancel_entries else "entry_ttl"}, now=now)

    def _ownership_conflicts(self, positions, open_orders):
        net = self._net()
        actual = {}
        conflicts = []
        for position in positions:
            qty = _decimal(position.get("signed_qty", 0))
            if qty:
                pair = position.get("pair") or position.get("symbol") or "unknown_symbol"
                if position.get("position_side", "BOTH") != "BOTH":
                    conflicts.append("hedge_mode_position")
                actual[pair] = actual.get(pair, Decimal(0)) + qty
        for pair in set(net) | set(actual):
            if net.get(pair, Decimal(0)) != actual.get(pair, Decimal(0)):
                conflicts.append("unowned_or_unreconciled_position:" + pair)
        known = {row["client_id"] for row in self._orders()}
        if any(order.get("client_id") not in known for order in open_orders):
            conflicts.append("unowned_open_order")
        if any(row["status"] in ("submitting", "unknown") for row in self._orders()):
            conflicts.append("unknown_submission")
        return conflicts

    def _submit(self, intent, *, protective=False):
        existing = self.store.get_order(intent["client_id"])
        if existing is not None:
            # Crash recovery may repeat a source event at a later clock time.
            # Its frozen intent wins; never rebuild expiry/sizing or retry it.
            if (existing["strategy_id"] != intent["strategy_id"]
                    or existing["signal_id"] != intent["signal_id"]
                    or existing["rule_hash"] != intent["rule_hash"]
                    or existing["execution_hash"] != intent["execution_hash"]):
                raise ValueError("persisted Demo client identity mismatch")
            if existing["status"] != "intent":
                return existing
            row = existing
        else:
            row, _ = self.store.reserve_order(intent)
        if not self.config.trading_enabled or (not protective and not self._entries_enabled):
            return row
        if not self.store.claim_submission(row["client_id"], claimed_ms=self.clock_ms()):
            return row
        try:
            if protective:
                order = self.adapter.submit_market_order(row["pair"], row["side"], row["quantity"],
                    row["client_id"], reduce_only=True)
            else:
                order = self.adapter.submit_limit_order(row["pair"], row["side"], row["quantity"],
                    row["price"], row["client_id"], post_only=True, reduce_only=False)
        except Exception as exc:
            # Guard/validation failures did not send a request; exchange rejection
            # is definite.  Every other post-claim outcome is ambiguous.
            kind = getattr(exc, "error_kind", "unknown")
            if kind in ("validation", "safety", "rejected", "exchange_rejected", "invalid_order"):
                self.store.record_order_observation(row["client_id"],
                    {"status": "rejected", "updated_ms": self.clock_ms(),
                     "raw": {"error_kind": kind, "error_type": type(exc).__name__}})
            else:
                self.store.mark_submission_unknown(row["client_id"], type(exc).__name__,
                                                    observed_ms=self.clock_ms())
            self._record("submission_error", row["signal_id"],
                         {"client_id": row["client_id"], "error_kind": kind,
                          "error_type": type(exc).__name__, "protective": protective})
            self._status["last_error"] = "Demo submission " + kind
            return row
        return self.store.record_order_observation(row["client_id"],
                                                   {**order, "observed_ms": self.clock_ms()})

    def _base_intent(self, event, pair, side, role, qty, price, now, *, raw=None):
        signal_id = str(event["event_id"])
        from binance_demo_adapter import stable_client_order_id
        client_id = stable_client_order_id(self.config.virtual_account_key + ":" + self.config.strategy_id,
                                           signal_id, pair, side, role)
        return {**self._evidence(signal_id, event["data_fingerprint"]),
                "client_id": client_id, "intent_key": role + ":" + pair,
                "pair": pair, "side": side, "order_type": "market" if role == "exit" else "limit",
                "quantity": str(qty), "price": str(price) if price else None,
                "reduce_only": role == "exit", "time_in_force": None if role == "exit" else "GTX",
                "signal_ms": int(event.get("candle_ms") or now), "created_ms": now,
                "endpoint_host": HOST, "raw": raw or {}}

    def _exit(self, pair, qty, reason, signal_id, now, fingerprint=None, *, reference_candle=None):
        actual = self._actual_net.get(pair, Decimal(0))
        if actual * qty <= 0:
            qty = Decimal(0)
        else:
            qty = min(abs(qty), abs(actual)) * (1 if qty > 0 else -1)
        if not qty or not self.config.trading_enabled:
            return
        # Do not create another market exit while an earlier one is unresolved.
        if any(row["pair"] == pair and row.get("reduce_only") and row["status"] not in TERMINAL
               for row in self._orders()):
            return
        event = {"event_id": signal_id, "data_fingerprint": fingerprint or self.binding["input_fingerprint"],
                 "candle_ms": now}
        intent = self._base_intent(event, pair, "sell" if qty > 0 else "buy", "exit", abs(qty),
                                   None, now, raw={"exit_reason": reason, "demo_policy_hash": self.policy_hash,
                                       "submitted_limit_price": None, "reference_price": None,
                                       **signal_reference_evidence(reference_candle,
                                           self.account["execution"]["timeframe"])})
        progress = [[row["client_id"], str(row["filled_quantity"])] for row in self._orders()
                    if row["pair"] == pair and row.get("reduce_only") and row["status"] in TERMINAL]
        batch = _hash(sorted(progress))[:16]
        from binance_demo_adapter import stable_client_order_id
        intent["client_id"] = stable_client_order_id(self.config.virtual_account_key + ":" + self.config.strategy_id,
            str(signal_id), pair, intent["side"], "exit:" + batch)
        intent["intent_key"] += ":" + batch
        self._submit(intent, protective=True)

    def _flatten_owned(self, now, reason):
        for pair, qty in self._net().items():
            self._exit(pair, qty, reason, _hash([reason, pair, now]), now)

    def _protect(self, now):
        # Protection uses Demo's actual entry fill, never the paper fill price.
        for pair, qty in self._net().items():
            orders = [row for row in self._orders() if row["pair"] == pair
                      and not row.get("reduce_only") and _decimal(row.get("filled_quantity", 0)) > 0]
            if not orders:
                self._status["risk_alert"] = "owned_demo_position_entry_metadata_unavailable"
                continue
            row = max(orders, key=lambda order: order["created_ms"])
            intent = self._intent(row)
            atr, k = intent.get("entry_atr"), intent.get("risk_stop_atr_mult")
            if atr is None or k is None:
                self._status["risk_alert"] = "frozen_demo_entry_atr_unavailable"
                continue
            candle = self.paper.closed_candle(pair, self.account["execution"]["timeframe"])
            if not candle:
                self._status["risk_alert"] = "closed_candle_unavailable_for_risk_protection"
                continue
            price = row.get("average_price")
            if price is None:
                self._status["risk_alert"] = "actual_demo_entry_price_unavailable"
                continue
            direction = Decimal(1) if qty > 0 else Decimal(-1)
            stop = _decimal(price, positive=True) - direction * _decimal(k, positive=True) * _decimal(atr, positive=True)
            if (_decimal(candle["close"], positive=True) - stop) * direction <= 0:
                # Cancel any residual entry before reducing a partial position.
                self._reconcile(now, cancel_entries=True)
                current = self._net().get(pair, Decimal(0))
                self._exit(pair, current, "stop_loss_close_confirmed",
                           _hash(["demo_stop", row["client_id"], candle["timestamp"]]), now,
                           reference_candle=candle)

    def _paper_event(self, event, now, conflicts):
        if event["rule_hash"] != self.account["rule_hash"]:
            raise ValueError("paper event rule identity mismatch")
        payload = event["payload"]
        if event["event_type"] not in ("order_submitted", "order_filled", "order_cancelled",
                                        "order_rejected", "trade_closed"):
            return
        self._record("paper_event", event["event_id"],
                     {"paper_event_type": event["event_type"], "paper_event": payload,
                      "paper_observed_ms": event["observed_at_ms"],
                      "paper_next_open_is_proxy": True}, fingerprint=event["data_fingerprint"], now=now)
        if event["event_type"] != "order_submitted":
            return
        pair = payload["pair"]
        if pair not in self.account["execution"]["execution_universe"] or payload.get("atomic"):
            raise ValueError("Demo MVP refuses unknown or atomic execution legs")
        net = self._net().get(pair, Decimal(0))
        if payload["kind"] == "exit":
            self._reconcile(now, cancel_entries=True)
            try:
                reference_candle = self.paper.closed_candle(pair, self.account["execution"]["timeframe"],
                                                            int(payload["signal_candle_ms"]))
            except Exception:
                # Missing telemetry must never become an execution gate.
                reference_candle = None
            self._exit(pair, self._net().get(pair, Decimal(0)), payload.get("reason", "paper_exit"),
                       event["event_id"], now, event["data_fingerprint"], reference_candle=reference_candle)
            return
        denied = None
        if not self._entries_enabled:
            denied = "entries_paused_or_read_only"
        elif conflicts:
            denied = "demo_account_ownership_conflict"
        elif not 0 <= now - event["observed_at_ms"] <= self.config.max_signal_lag_ms:
            denied = "paper_signal_stale"
        elif event["observed_at_ms"] < self.binding["forward_boundary_ms"]:
            denied = "before_demo_forward_boundary"
        elif net or any(row["pair"] == pair and row["status"] not in TERMINAL
                        and row["signal_id"] != event["event_id"] for row in self._orders()):
            denied = "existing_demo_position_or_pending_order"
        candle = self.paper.closed_candle(pair, self.account["execution"]["timeframe"],
                                          int(payload["signal_candle_ms"]))
        evidence = payload.get("evidence") or {}
        atr = evidence.get("entry_atr", evidence.get("atr"))
        k = (self.account["definition"].get("params") or self.account["definition"].get("params_frozen") or {}).get("risk_stop_atr_mult")
        if candle is None or atr is None or k is None:
            denied = denied or "signal_candle_or_frozen_atr_unavailable"
        if denied:
            self._record("entry_denied", event["event_id"], {"reason": denied, "pair": pair},
                         fingerprint=event["data_fingerprint"], now=now)
            return
        price = _decimal(candle["close"], positive=True)
        reference_evidence = signal_reference_evidence(candle, self.account["execution"]["timeframe"])
        atr, k = _decimal(atr, positive=True), _decimal(k, positive=True)
        modeled_fee = _decimal(self.account["execution"]["fee_rate_per_side"])
        risk = _decimal(self.account["execution"]["max_loss_per_trade_usdt"], positive=True)
        sizing_notional = risk / (k * atr / price + 2 * modeled_fee)
        notional = min(sizing_notional, _decimal(self.config.max_notional_usdt, positive=True))
        multiplier = _decimal(payload.get("size_multiplier", 1))
        if not 0 < multiplier <= 1:
            self._record("entry_denied", event["event_id"], {"reason": "invalid_or_zero_size", "pair": pair}, now=now)
            return
        qty = notional * multiplier / price
        try:
            prepared = self.adapter.prepare_limit_order(pair, payload["buy_sell"], str(qty), str(price))
            qty, price = _decimal(prepared["quantity"], positive=True), _decimal(prepared["price"], positive=True)
            if qty * price > _decimal(self.config.max_notional_usdt, positive=True):
                raise ValueError("quantized order exceeds Demo notional cap")
        except Exception as exc:
            self._record("entry_denied", event["event_id"],
                         {"reason": "demo_exchange_precision_or_minimum", "pair": pair,
                          "error_type": type(exc).__name__}, now=now)
            return
        intent = self._base_intent(event, pair, payload["buy_sell"], "entry", qty, price, now,
            raw={"paper_signal": payload, "demo_policy_hash": self.policy_hash,
                 "entry_atr": str(atr), "risk_stop_atr_mult": str(k),
                 "reference_price": str(price), "submitted_limit_price": str(price),
                 **reference_evidence, "expires_ms": now + self.config.entry_order_ttl_ms,
                 "sizing_modeled_fee_per_side": str(modeled_fee), "sizing_notional_proxy": str(sizing_notional),
                 "actual_fee_unknown_until_fill": True})
        self._submit(intent)

    def cycle(self, now_ms=None):
        with self._mutex:
            if not self._started:
                raise RuntimeError("Demo runner must be explicitly started")
            now = _stamp(self.clock_ms() if now_ms is None else now_ms)
            self._status.update(last_cycle_ms=now, last_error=None, submission_unknown=False,
                                risk_alert=None, reconciliation_alert=None)
            try:
                frozen = self.paper.account(self.config.strategy_id)
                if frozen["rule_hash"] != self.account["rule_hash"] or frozen["execution_hash"] != self.account["execution_hash"]:
                    raise ValueError("frozen paper identity changed")
                self._reconcile(now, cancel_entries=not self._entries_enabled)
                positions = self.adapter.fetch_positions()
                self._actual_net = {position["pair"]: _decimal(position["signed_qty"])
                                    for position in positions if position.get("pair") in
                                    {"BTC/USDT:USDT", "ETH/USDT:USDT", "TRX/USDT:USDT"}
                                    and _decimal(position["signed_qty"])}
                self.store.append_position_snapshot({"positions": positions, "demo_policy_hash": self.policy_hash}, observed_ms=now)
                # Wallet and open-order snapshots are entry gates. Their failure
                # cannot suppress reduction of an already verified owned position.
                entry_snapshot_errors = []
                if self._metadata_checked_ms is None or now - self._metadata_checked_ms >= 60_000:
                    self._metadata_errors = []
                    try:
                        mode = self.adapter.fetch_position_mode()
                        actual_config = self.adapter.fetch_symbol_config()
                        self._status.update(account_mode=mode, actual_symbol_config=actual_config,
                            paper_leverage=self.account["execution"].get("leverage"),
                            leverage_comparison_scope="actual_demo_config_not_automatically_set_to_paper")
                        if mode["hedged"]:
                            self._metadata_errors.append("demo_hedge_mode_unsupported")
                        supported_config = {row["pair"] for row in actual_config
                                            if row.get("pair") in self.account["execution"]["execution_universe"]}
                        if supported_config != set(self.account["execution"]["execution_universe"]):
                            self._metadata_errors.append("actual_demo_symbol_config_unavailable")
                        self._record("actual_execution_config", "actual_execution_config",
                                     {"account_mode": mode, "symbol_config": actual_config,
                                      "paper_leverage": self.account["execution"].get("leverage")}, now=now)
                    except Exception as exc:
                        self._metadata_errors.append("actual_demo_execution_config_unavailable")
                        self._record("snapshot_error", "actual_execution_config",
                                     {"error_type": type(exc).__name__}, now=now)
                    self._metadata_checked_ms = now
                entry_snapshot_errors.extend(self._metadata_errors)
                try:
                    balance = self.adapter.fetch_balance()
                    self.store.append_account_snapshot({"balance": balance, "demo_policy_hash": self.policy_hash}, observed_ms=now)
                except Exception as exc:
                    entry_snapshot_errors.append("balance_snapshot_unavailable")
                    self._record("snapshot_error", "balance_snapshot", {"error_type": type(exc).__name__}, now=now)
                try:
                    open_orders = [order for order in self.adapter.fetch_open_orders()
                                   if _status(order["status"]) not in TERMINAL]
                except Exception as exc:
                    open_orders = []
                    entry_snapshot_errors.append("open_orders_snapshot_unavailable")
                    self._record("snapshot_error", "open_orders_snapshot", {"error_type": type(exc).__name__}, now=now)
                conflicts = self._ownership_conflicts(positions, open_orders)
                conflicts.extend(entry_snapshot_errors)
                self._status["ownership_conflicts"] = conflicts
                # Conflicting unrelated orders cannot suppress owned protection.
                self._protect(now)
                checkpoint_ms = now
                for event in self.paper.events(self.config.strategy_id, self.last_sequence):
                    # Events can arrive during the preceding account requests.
                    # Assess each at processing time; explicit cycle clocks stay fixed.
                    event_now = _stamp(self.clock_ms()) if now_ms is None else now
                    self._paper_event(event, event_now, conflicts)
                    self.last_sequence = int(event["sequence_id"])
                    checkpoint_ms = max(checkpoint_ms, event_now)
                self._record("runner_checkpoint", "runner_checkpoint",
                             {"last_sequence_id": self.last_sequence, "entries_enabled": self._entries_enabled}, now=checkpoint_ms)
                net = self._net()
                orders = self._orders()
                self._status.update(owned_positions={pair: str(qty) for pair, qty in net.items()},
                    last_sequence_id=self.last_sequence, **attempted_entry_outcomes(orders),
                    status=("blocked_unknown_exposure" if conflicts else
                            "observing" if self._entries_enabled else
                            "paused_protecting" if net else "paused_flat"))
                self._status["round_trips"] = derive_round_trips(self.store.runner_fills(), orders)
                self._status["trading_pnl_excludes_funding"] = True
            except Exception as exc:
                # Never send credential-bearing request/error text to status or logs.
                self._status.update(status="reconciliation_error", last_error=type(exc).__name__,
                                    risk_alert="critical_risk_protection_cycle_incomplete")
            return self.status()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only preflight for one frozen Demo paper identity")
    parser.add_argument("--account", required=True)
    args = parser.parse_args(argv)
    from data_store import DataStore
    store = DataStore().initialize()
    row = PaperEventSource(store).account(args.account)
    print(json.dumps({"strategy_id": row["strategy_id"], "rule_hash": row["rule_hash"],
                      "execution_hash": row["execution_hash"], "execution": row["execution"],
                      "environment": "binance_demo", "trading_enabled": False,
                      "credential_source": "frontend_encrypted_database"}, ensure_ascii=False))


if __name__ == "__main__":
    main()

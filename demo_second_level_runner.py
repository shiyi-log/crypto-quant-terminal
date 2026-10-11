"""Finite, isolated Binance Demo second-level execution experiment.

The operator supplies credentials, the account execution lock and two isolated
DemoStore ledgers. This module opens no database and never chooses a live URL.
Signals are observed forward once; arms alternate before outcomes are known.
Only one attributable account position may exist at a time.
"""
from __future__ import annotations

from collections import Counter, deque
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import math
import re
import time

from binance_demo_adapter import (
    ALLOWED_PAIRS, DEMO_HOST, PAIR_SYMBOLS, DemoExchangeRejected,
    DemoRequestError, DemoSafetyError, DemoSubmissionUnknown,
    stable_client_order_id, summarize_fill_fees, normalize_order,
)
from demo_execution_service import derive_round_trips
from demo_second_level_signals import POLICY_HASH, SecondLevelPaperControl, frozen_policy

VERSION = "finite-second-level-demo-v1"
STRATEGY = "second_level_obi_research"
TERMINAL = {"filled", "canceled", "expired", "rejected"}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def _decimal(value, *, positive=False):
    if isinstance(value, bool):
        raise DemoSafetyError("invalid second-level decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise DemoSafetyError("invalid second-level decimal") from None
    if not number.is_finite() or (positive and number <= 0):
        raise DemoSafetyError("invalid second-level decimal")
    return number


def _timing_evidence(value):
    """Use nonsecret timing names that survive DemoStore's request scrubber."""
    names = {"request_started_ms": "local_http_started_ms",
             "local_request_started_ms": "local_http_started_ms",
             "request_latency_ms": "local_http_elapsed_ms",
             "entry_requested_quantity": "frozen_entry_quantity"}
    if isinstance(value, dict):
        return {names.get(key, key): _timing_evidence(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_timing_evidence(item) for item in value]
    return value


def _paper_result(paper):
    result = _timing_evidence(paper.result)
    if not paper.terminal:
        result.update(status="reference_unavailable", terminal=True,
                      last_reason="execution_ended_without_causal_horizon_quote")
    return result


def _status(value):
    return {"new": "open", "partiallyfilled": "partially_filled", "expired_in_match": "expired",
            "cancelled": "canceled", "closed": "filled"}.get(str(value).lower(), str(value).lower())


def _error(exc, stage):
    kind = ("exchange_rejected" if isinstance(exc, DemoExchangeRejected) else
            "submission_unknown" if isinstance(exc, DemoSubmissionUnknown) else
            "safety_rejected" if isinstance(exc, DemoSafetyError) else
            "request_failed" if isinstance(exc, DemoRequestError) else "storage_or_internal_error")
    code = getattr(exc, "exchange_code", None)
    return {"stage": stage, "kind": kind,
            "exchange_code": code if isinstance(code, int) and not isinstance(code, bool) else None}


def _step(left, right):
    places = max(0, -left.as_tuple().exponent, -right.as_tuple().exponent)
    scale = 10 ** places
    return Decimal(math.lcm(int(left * scale), int(right * scale))) / scale


@dataclass(frozen=True)
class SecondLevelConfig:
    session_id: str
    max_signals: int = 6
    max_duration_seconds: int = 600
    max_notional_usdt: str = "75"
    hold_seconds: int = 10
    maker_ttl_seconds: int = 3
    poll_seconds: float = 1.0
    reconciliation_seconds: int = 60
    ioc_crossing_cap_bp: int = 10
    local_weight_per_minute: int = 300
    local_order_posts_per_minute: int = 10

    def __post_init__(self):
        if not isinstance(self.session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.session_id):
            raise ValueError("invalid second-level session ID")
        if not 0 < _decimal(self.max_notional_usdt) <= 75:
            raise ValueError("second-level entry cap must be at most 75 USDT")
        for name, low, high in (("max_signals", 1, 100), ("max_duration_seconds", 1, 600),
                ("reconciliation_seconds", 1, 120), ("ioc_crossing_cap_bp", 1, 20),
                ("local_weight_per_minute", 100, 300), ("local_order_posts_per_minute", 3, 10)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError("invalid second-level finite execution bound")
        # These horizons are preregistered, rather than tunable after results.
        if (self.hold_seconds != 10 or self.maker_ttl_seconds != 3
                or isinstance(self.poll_seconds, bool) or self.poll_seconds != 1):
            raise ValueError("second-level horizons must match the frozen signal policy")

    def policy(self):
        return {**asdict(self), "version": VERSION, "endpoint_host": DEMO_HOST,
                "signal_policy_hash": POLICY_HASH, "signal_policy": frozen_policy(),
                "entry_arm_assignment": "one_preassigned_arm_per_signal_sequential_account_flat_cycles",
                "maker_entry": "GTX_limit_at_Demo_best_quote_minus_or_plus_preassigned_distance",
                "taker_entry": "crossing_LIMIT_IOC_with_price_cap_partial_or_no_fill_possible",
                "exit": "reduce_only_MARKET_actual_quantity",
                "maker_round_trip_semantics": "maker_entry_market_exit_mixed_execution",
                "fee_source": "actual_Binance_Demo_userTrades_only_unknown_preserved",
                "paper_control": "same_signal_subsequent_Demo_quote_crossing_proxy_maker_fill_unknown",
                "hold_clock": "signal_decision_plus_10_seconds_mapped_to_monotonic",
                "actual_fill_hold": "reported_from_exchange_fill_times_separately",
                "maker_ttl_clock": "3_seconds_from_local_submission_claim_request_start",
                "new_entry_timeout_policy": "stop_new_entries_and_reconcile_protect_owned_exposure",
                "automatic_live_launch": False}


def build_entry_plan(adapter, pair, side, arm, distance_bp, quote, exchange_info, cap="75", crossing_bp=10):
    """Round a small entry on a grid that can also close a partial execution."""
    if pair not in ALLOWED_PAIRS or side not in {"buy", "sell"} or arm not in {"maker", "taker"}:
        raise DemoSafetyError("invalid second-level plan identity")
    if quote.get("pair") != pair:
        raise DemoSafetyError("second-level Demo quote identity mismatch")
    bid, ask = _decimal(quote.get("bid_price"), positive=True), _decimal(quote.get("ask_price"), positive=True)
    if bid >= ask:
        raise DemoSafetyError("second-level Demo quote is crossed")
    if (arm == "maker" and distance_bp not in {0, 1, 2, 5}) or (arm == "taker" and distance_bp is not None):
        raise DemoSafetyError("invalid preassigned maker distance")
    symbol = next((row for row in exchange_info.get("symbols", []) if row.get("symbol") == PAIR_SYMBOLS[pair]), None)
    if symbol is None:
        raise DemoSafetyError("second-level symbol metadata missing")
    filters = {row["filterType"]: row for row in symbol.get("filters", [])}
    try:
        lot, market, price_filter, notional = (filters[name] for name in
            ("LOT_SIZE", "MARKET_LOT_SIZE", "PRICE_FILTER", "MIN_NOTIONAL"))
        lot_step = _decimal(lot["stepSize"], positive=True)
        market_step = _decimal(market["stepSize"], positive=True)
        market_min = _decimal(market["minQty"])
        # A common initial grid is insufficient when a one-lot partial fill
        # cannot be closed. Reject that symbol before creating any exposure.
        if lot_step % market_step or lot_step < market_min:
            raise DemoSafetyError("partial limit fills cannot meet market exit quantity rules")
        tick = _decimal(price_filter["tickSize"], positive=True)
        distance = Decimal(str(distance_bp or 0)) / 10_000
        if arm == "maker":
            target = bid * (1 - distance) if side == "buy" else ask * (1 + distance)
        else:
            spread_cap = Decimal(crossing_bp) / 10_000
            target = ask * (1 + spread_cap) if side == "buy" else bid * (1 - spread_cap)
        price = (target / tick).to_integral_value(rounding=ROUND_FLOOR if side == "buy" else ROUND_CEILING) * tick
        if (arm == "maker" and ((side == "buy" and price >= ask) or (side == "sell" and price <= bid))):
            raise DemoSafetyError("maker plan would cross the Demo quote")
        step = _step(lot_step, market_step)
        minimum = max(_decimal(lot["minQty"]), market_min, _decimal(notional["notional"], positive=True) / price)
        quantity = (minimum / step).to_integral_value(rounding=ROUND_CEILING) * step
        # Both sides' quoted notional are bounded; a short entry must not evade
        # the cap merely by putting its executable limit below the market.
        maximum_notional = quantity * max(price, ask)
        if (quantity > min(_decimal(lot["maxQty"]), _decimal(market["maxQty"]))
                or maximum_notional > _decimal(cap, positive=True)):
            raise DemoSafetyError("second-level minimum entry exceeds the notional cap")
        prepared = adapter.prepare_limit_order(pair, side, quantity, price)
        if _decimal(prepared["quantity"]) != quantity or _decimal(prepared["price"]) != price:
            raise DemoSafetyError("second-level plan changed during precision validation")
        adapter.validate_order(pair, quantity, market=True)
    except KeyError:
        raise DemoSafetyError("second-level symbol filters missing") from None
    return {"pair": pair, "side": side, "arm": arm, "maker_distance_bp": distance_bp,
            "quantity": format(quantity, "f"), "price": format(price, "f"),
            "maximum_quoted_entry_notional_usdt": format(maximum_notional, "f"),
            "demo_quote": _timing_evidence(deepcopy(quote)), "entry_time_in_force": "GTX" if arm == "maker" else "IOC"}


class _Session:
    def __init__(self, adapter, ledgers, source, config, clock_ms, sleep, monotonic, recovery_only=False):
        self.adapter, self.ledgers, self.source, self.config = adapter, ledgers, source, config
        self.now = clock_ms or (lambda: int(time.time() * 1000))
        self.sleep = sleep or time.sleep
        self.mono = monotonic or time.monotonic
        self.policy, self.policy_hash = config.policy(), _hash(config.policy())
        self.recovery_only = recovery_only
        self.observed, self.intents, self.fill_facts = {}, {}, {}
        self.submission_mono = {}
        self.signal_mono_deadlines = {}
        self.weights, self.order_posts = deque(), deque()
        self.weight_limit = config.local_weight_per_minute
        self.post_limit = config.local_order_posts_per_minute
        self.halt_entries = False
        self.namespaces_validated = False
        self.report = {"environment": "binance_demo", "endpoint_host": DEMO_HOST,
            "session_id": config.session_id, "execution_policy_hash": self.policy_hash,
            "signal_policy_hash": POLICY_HASH, "research_status": "forward_research_hypothesis",
            "live_launch_allowed": False, "live_profitability_verified": False,
            "success": False, "position_flat": False, "open_orders_zero": False,
            "requires_operator_recovery": False, "persistence_complete": True,
            "recovery_only": recovery_only, "signal_count": 0, "source_poll_count": 0,
            "source_reason_counts": {}, "attempted_entry_count": 0, "attempted_exit_count": 0,
            "attempt_population": "durably_claimed_attempts_including_local_prePOST_guard_rejections",
            "cycles": [], "errors": [], "started_ms": self.now(),
            "arm_semantics": {"maker": "post_only_entry_market_exit_mixed", "taker": "crossing_IOC_entry_market_exit"},
            "paper_scope": "same_signal_Demo_quote_proxy_not_maker_queue_or_actual_fill",
            "actual_pnl_excludes_funding": True, "sample_sufficiency": "not_assessed_no_profitability_verdict"}
        self.identities = {}
        for arm in ("maker", "taker"):
            namespace = "second_level_" + config.session_id + "_" + arm
            self.identities[arm] = {"strategy_id": STRATEGY, "signal_id": namespace + ":experiment",
                "rule_hash": POLICY_HASH, "execution_hash": self.policy_hash,
                "input_fingerprint": _hash({"session_id": config.session_id, "signal_policy_hash": POLICY_HASH})}

    def error(self, exc, stage):
        item = _error(exc, stage)
        self.report["errors"].append(item)
        # Every request failure stops fresh exposure; protection remains active.
        self.halt_entries = True
        return item

    def persist(self, method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            self.report["persistence_complete"] = False
            self.error(exc, "ledger_projection")
            return None

    def compare(self, arm, identity, stage, payload, *, critical=False):
        value = _timing_evidence({**identity, "demo_policy_hash": self.policy_hash, **payload})
        if critical:
            return self.ledgers[arm].append_comparison(value, stage, self.now())
        return self.persist(self.ledgers[arm].append_comparison, value, stage, self.now())

    def call(self, method, *args, weight=1, order_post=False, protective=False, guard=None, wait_deadline=None, **kwargs):
        # Reserve 20 weights and one order slot for cancellation/protection.
        # GET loops also obey the local limit; an unknown order cannot cause
        # unbounded 40-weight all-symbol polls.
        while True:
            now = self.mono()
            if wait_deadline is not None and now >= wait_deadline:
                raise DemoSafetyError("optional Demo telemetry exceeded its monotonic time budget")
            while self.weights and self.weights[0][0] <= now - 60:
                self.weights.popleft()
            while self.order_posts and self.order_posts[0] <= now - 60:
                self.order_posts.popleft()
            cap = self.weight_limit if protective else max(1, self.weight_limit - 20)
            posts = self.post_limit if protective else max(1, self.post_limit - 1)
            if weight > cap:
                raise DemoSafetyError("Demo request weight exceeds finite local budget")
            if sum(row[1] for row in self.weights) + weight <= cap and (not order_post or len(self.order_posts) < posts):
                break
            self.sleep(self.config.poll_seconds)
        if guard is not None:
            guard()  # Recheck causal freshness after any rate-budget waiting.
        self.weights.append((self.mono(), weight))
        if order_post:
            self.order_posts.append(self.mono())
        return method(*args, **kwargs)

    def budget_available(self):
        now = self.mono()
        while self.weights and self.weights[0][0] <= now - 60:
            self.weights.popleft()
        while self.order_posts and self.order_posts[0] <= now - 60:
            self.order_posts.popleft()
        # Keep room for a full-account foreign-risk check, cancellation, exit
        # and reconciliation. Protection is allowed after this reserve is used.
        return (sum(row[1] for row in self.weights) + 100 <= self.weight_limit
                and len(self.order_posts) + 3 <= self.post_limit)

    def snapshot(self, arm=None, pair=None):
        positions = self.call(self.adapter.fetch_positions, weight=5, protective=pair is not None)
        opens = (self.call(self.adapter.fetch_open_orders, pair, weight=1, protective=True) if pair else
                 self.call(self.adapter.fetch_open_orders, weight=40))
        for key in ([arm] if arm else ("maker", "taker")):
            self.persist(self.ledgers[key].append_position_snapshot,
                         {"positions": positions, "openOrders": opens,
                          "execution_policy_hash": self.policy_hash}, self.now())
        nonzero = []
        for row in positions:
            quantity = _decimal(row.get("signed_qty"))
            if quantity:
                nonzero.append((row, quantity))
        return nonzero, opens

    def initialize(self):
        if (set(self.ledgers) != {"maker", "taker"} or self.adapter.allow_trading is not True):
            raise DemoSafetyError("an explicitly enabled isolated second-level Demo account is required")
        for arm, ledger in self.ledgers.items():
            if (getattr(ledger, "environment", None) != "binance_demo"
                    or getattr(ledger, "endpoint_host", None) != DEMO_HOST
                    or getattr(ledger, "virtual_account_key", None) != "second_level_" + self.config.session_id + "_" + arm):
                raise DemoSafetyError("second-level ledger namespace mismatch")
        self.namespaces_validated = True
        for arm, ledger in self.ledgers.items():
            ledger.bind_account({**self.identities[arm], "demo_policy_hash": self.policy_hash,
                "policy": self.policy, "arm": arm, "research_status": "forward_research_hypothesis"})
        if self.call(self.adapter.fetch_position_mode, weight=30)["hedged"] is not False:
            raise DemoSafetyError("second-level runner requires one-way position mode")
        self.metadata = self.call(self.adapter.fetch_exchange_info, weight=1)
        for limit in self.metadata.get("rateLimits", []):
            if limit.get("interval") == "MINUTE" and limit.get("intervalNum") == 1:
                value = limit.get("limit")
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    if limit.get("rateLimitType") == "REQUEST_WEIGHT":
                        self.weight_limit = min(self.weight_limit, value)
                    elif limit.get("rateLimitType") == "ORDERS":
                        self.post_limit = min(self.post_limit, value)
        self.report["local_rate_budget"] = {"weight_per_minute": self.weight_limit,
            "order_posts_per_minute": self.post_limit, "protective_operations_reserved": True,
            "synchronous_REST_low_latency_HFT_verified": False}
        account = self.call(self.adapter.fetch_balance, weight=5)
        for ledger in self.ledgers.values():
            ledger.append_account_snapshot(account, self.now())
        self.groups = self.load_groups()

    def load_groups(self):
        groups = {}
        for arm, ledger in self.ledgers.items():
            for row in ledger.runner_orders():
                payload = row.get("intent_payload", {})
                if (row.get("strategy_id") != STRATEGY or row.get("rule_hash") != POLICY_HASH
                        or row.get("execution_hash") != self.policy_hash
                        or payload.get("demo_policy_hash") != self.policy_hash):
                    raise DemoSafetyError("second-level ledger contains unrelated frozen orders")
                group = groups.setdefault(row["signal_id"], {"arm": arm, "rows": []})
                if group["arm"] != arm:
                    raise DemoSafetyError("one signal cannot have two actual execution arms")
                group["rows"].append(row)
                self.intents[row["client_id"]] = deepcopy(row)
                self.adapter.authorize_owned_client_id(row["client_id"])
                if row.get("raw", {}).get("orderId"):
                    self.observed[row["client_id"]] = normalize_order(row["raw"], row["pair"])
                elif row.get("status") == "rejected" or (row.get("status") == "expired" and row.get("submission_attempts") == 0):
                    self.observed[row["client_id"]] = {"status": row["status"], "filled_qty": "0",
                        "client_id": row["client_id"], "pair": row["pair"], "side": row["side"],
                        "qty": row["quantity"], "raw": {}}
        for signal_id, group in groups.items():
            entries = [row for row in group["rows"] if not row["reduce_only"]]
            if len(entries) != 1 or len(group["rows"]) > 5:
                raise DemoSafetyError("second-level recovery requires one durable entry per signal")
            group["entry"] = entries[0]
            group["signal"] = entries[0]["intent_payload"]["signal"]
            group["plan"] = entries[0]["intent_payload"]["plan"]
            if group["signal"].get("signal_id") != signal_id:
                raise DemoSafetyError("second-level recovered signal identity mismatch")
        return groups

    def observe(self, arm, client, order):
        if order is None:
            return None
        intent = self.intents[client]
        raw = order.get("raw", {})
        if (order.get("client_id") != client or order.get("pair") != intent["pair"]
                or order.get("side") != intent["side"]
                or _decimal(order.get("qty")) != _decimal(intent["quantity"])
                or not 0 <= _decimal(order.get("filled_qty")) <= _decimal(intent["quantity"])
                or raw.get("type") != intent["order_type"].upper()
                or raw.get("reduceOnly") is not intent["reduce_only"]
                or (not intent["reduce_only"] and (raw.get("timeInForce") != intent["time_in_force"]
                    or _decimal(order.get("price")) != _decimal(intent["price"])))):
            raise DemoSafetyError("second-level exchange observation differs from durable intent")
        previous = self.observed.get(client)
        if previous:
            if str(previous["order_id"]) != str(order["order_id"]):
                raise DemoSafetyError("second-level exchange order identity changed")
            if (_decimal(order["filled_qty"]) < _decimal(previous["filled_qty"])
                    or int(order.get("updated_ms") or 0) < int(previous.get("updated_ms") or 0)):
                return previous
            if _status(previous["status"]) in TERMINAL and _status(order["status"]) != _status(previous["status"]):
                return previous
        self.observed[client] = deepcopy(order)
        self.persist(self.ledgers[arm].record_order_observation, client, order)
        return order

    def lookup(self, arm, client):
        try:
            order = self.call(self.adapter.fetch_order_by_client_id,
                    self.intents[client]["pair"], client, weight=1, protective=True)
            return self.observe(arm, client, order) if order is not None else self.observed.get(client)
        except Exception as exc:
            item = self.error(exc, "order_lookup")
            self.persist(self.ledgers[arm].append_reconciliation, client, item, self.now())
            return self.observed.get(client)

    def submit(self, arm, intent, entry=False):
        ledger, client = self.ledgers[arm], intent["client_id"]
        row, _ = ledger.reserve_order(intent)  # A failed critical write forbids POST.
        self.intents[client] = deepcopy(intent)
        if ledger.claim_submission(client, self.now()):
            self.adapter.authorize_owned_client_id(client)
            started = self.now()
            started_mono = self.mono()
            self.submission_mono[client] = started_mono
            self.report["attempted_entry_count" if entry else "attempted_exit_count"] += 1
            response_received = False
            def entry_guard():
                self.validate_signal(intent["intent_payload"]["signal"])
                if self.mono() >= self.run_deadline:
                    raise DemoSafetyError("finite signal deadline reached before exchange POST")
            try:
                if not entry:
                    order = self.call(self.adapter.submit_market_order, intent["pair"], intent["side"],
                        intent["quantity"], client, reduce_only=True, weight=4, order_post=True, protective=True)
                elif arm == "maker":
                    order = self.call(self.adapter.submit_limit_order, intent["pair"], intent["side"],
                        intent["quantity"], intent["price"], client, post_only=True, weight=4, order_post=True, guard=entry_guard)
                else:
                    order = self.call(self.adapter.submit_ioc_entry, intent["pair"], intent["side"],
                        intent["quantity"], intent["price"], client, weight=4, order_post=True, guard=entry_guard)
                response_received = True
                self.observe(arm, client, order)
            except Exception as exc:
                self.compare(arm, intent, "submission_error", {"client_id": client,
                    "error_kind": _error(exc, "submission")["kind"],
                    "exchange_code": _error(exc, "submission")["exchange_code"],
                    "submitted_ms": started, "response_received_ms": self.now()})
                self.error(exc, "entry_submission" if entry else "exit_submission")
                if not response_received and isinstance(exc, (DemoExchangeRejected, DemoSafetyError)):
                    self.persist(ledger.record_order_observation, client, {"status": "rejected",
                        "filled_qty": "0", "raw": {}, "exchange_updated_ms": self.now()})
                    self.observed[client] = {"status": "rejected", "filled_qty": "0", "client_id": client,
                        "pair": intent["pair"], "side": intent["side"], "qty": intent["quantity"], "raw": {}}
                else:
                    self.persist(ledger.mark_submission_unknown, client,
                        _error(exc, "submission")["kind"], self.now())
            self.compare(arm, intent, "submission_timing", {"client_id": client,
                "local_http_started_ms": started, "response_received_ms": self.now(),
                "local_elapsed_ms": self.now() - started,
                "HTTP_elapsed_monotonic_ms": (self.mono() - started_mono) * 1000,
                "clock_basis": "local_request_start_and_return", "cross_clock_latency_verified": False})
        elif row.get("status") == "rejected":
            self.observed[client] = {"status": "rejected", "filled_qty": "0", "client_id": client,
                "pair": intent["pair"], "side": intent["side"], "qty": intent["quantity"], "raw": {}}
        if client not in self.observed or _status(self.observed[client]["status"]) not in TERMINAL:
            self.lookup(arm, client)  # A previous claim is always lookup-only.
        return self.observed.get(client)

    def frozen_intent(self, arm, signal, plan, role, quantity=None):
        entry = role == "entry"
        side = signal["side"] if entry else ("sell" if signal["side"] == "buy" else "buy")
        identity = {**self.identities[arm], "signal_id": signal["signal_id"],
                    "input_fingerprint": signal["input_fingerprint"]}
        created = self.now()
        return {**identity, "client_id": stable_client_order_id(self.ledgers[arm].virtual_account_key,
                signal["signal_id"], signal["pair"], side, role), "intent_key": role,
                "pair": signal["pair"], "side": side, "order_type": "limit" if entry else "market",
                "quantity": plan["quantity"] if entry else quantity,
                "price": plan["price"] if entry else None,
                "time_in_force": plan["entry_time_in_force"] if entry else None,
                "reduce_only": not entry, "signal_ms": signal["decision_ms"], "created_ms": created,
                "intent_payload": {"demo_policy_hash": self.policy_hash,
                    "experiment_scope": "second_level_forward_research", "signal": deepcopy(signal),
                    "plan": deepcopy(plan), "frozen_role": role,
                    "entry_clock_basis": "local_decision_and_HTTP_receipt", "created_ms": created}}

    def terminalize_entry(self, arm, client, *, recovery):
        ttl = (self.mono() if recovery or self.halt_entries else
               self.submission_mono.get(client, self.mono()) + self.config.maker_ttl_seconds)
        deadline = max(self.mono(), ttl) + self.config.reconciliation_seconds
        first_fill = None
        cancellation_attempted = False
        while True:
            order = self.lookup(arm, client)
            if order is not None and _decimal(order["filled_qty"]) > 0 and first_fill is None:
                first_fill = self.mono()
            if order is not None and _status(order["status"]) in TERMINAL:
                return order, first_fill
            if self.mono() >= ttl and not cancellation_attempted:
                # Cancel once; a DELETE timeout is followed only by GET. A
                # subsequent recovery may cancel an observed still-open order.
                cancellation_attempted = True
                try:
                    self.observe(arm, client, self.call(self.adapter.cancel_order,
                        self.intents[client]["pair"], client_order_id=client, weight=2, protective=True))
                except Exception as exc:
                    self.error(exc, "entry_cancel")
                order = self.observed.get(client)
                if order is not None and _status(order["status"]) in TERMINAL:
                    return order, first_fill
            if self.mono() >= deadline:
                raise DemoSafetyError("second-level entry remains unresolved; new entries stopped")
            self.sleep(self.config.poll_seconds)

    def reconcile_fills(self, arm, clients):
        complete = True
        for client in clients:
            order = self.observed.get(client)
            if order is None:
                complete = False
                continue
            if not order.get("order_id"):
                complete = complete and _decimal(order["filled_qty"]) == 0 and _status(order["status"]) in TERMINAL
                continue
            try:
                fills = self.call(self.adapter.fetch_trades, self.intents[client]["pair"],
                                  order_id=order["order_id"], weight=5)
                if len(fills) >= 1000:
                    raise DemoSafetyError("second-level fills may be truncated")
                seen = set()
                for fill in fills:
                    if (str(fill.get("order_id")) != str(order["order_id"])
                            or fill.get("pair") != self.intents[client]["pair"]
                            or fill.get("side") != self.intents[client]["side"]
                            or fill.get("fill_id") in seen):
                        raise DemoSafetyError("second-level fill identity mismatch")
                    seen.add(fill["fill_id"])
                    _decimal(fill.get("qty"), positive=True)
                if sum((_decimal(fill["qty"]) for fill in fills), Decimal(0)) != _decimal(order["filled_qty"]):
                    complete = False
                self.fill_facts[client] = deepcopy(fills)
                for fill in fills:
                    self.persist(self.ledgers[arm].record_fill, client, fill)
            except Exception as exc:
                self.error(exc, "fill_reconciliation")
                complete = False
        return complete

    def quote(self, signal, paper=None, *, deadline=None):
        started = self.now()
        request_mono = self.mono()
        quote = self.call(self.adapter.fetch_book_ticker, signal["pair"], weight=2, wait_deadline=deadline)
        observed = self.now()
        exchange_time = quote.get("timestamp_ms")
        if (not isinstance(exchange_time, int) or isinstance(exchange_time, bool)
                or abs(observed - exchange_time) > 5000):
            raise DemoSafetyError("Demo quote exchange freshness is unknown or outside clock guard")
        quote = {**quote, "local_http_started_ms": started, "local_received_ms": observed,
                 "HTTP_elapsed_monotonic_ms": (self.mono() - request_mono) * 1000}
        if paper is not None:
            paper.observe_book_ticker(quote, request_started_ms=started, observed_ms=observed)
        return quote

    def validate_signal(self, signal):
        if (not isinstance(signal, dict) or signal.get("policy_hash") != POLICY_HASH
                or signal.get("rule_hash") != POLICY_HASH or signal.get("pair") not in ALLOWED_PAIRS
                or signal.get("side") not in {"buy", "sell"}
                or not isinstance(signal.get("signal_id"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", signal.get("input_fingerprint", ""))):
            raise DemoSafetyError("second-level source signal identity is invalid")
        index = signal.get("eligible_signal_index")
        schedule = frozen_policy()["arm_schedule"]
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise DemoSafetyError("second-level arm assignment index is invalid")
        if any(signal.get(name) != value for name, value in schedule[index % len(schedule)].items()):
            raise DemoSafetyError("second-level arm allocation differs from frozen schedule")
        now = self.now()
        for field in ("decision_ms", "source_event_ms", "database_received_at_ms", "original_received_at_ms"):
            if field == "original_received_at_ms" and signal.get(field) is None:
                continue
            stamp = signal.get(field)
            if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not 0 <= now - stamp <= 5000:
                raise DemoSafetyError("second-level signal was stale or not causally available")
        obi = _decimal(signal.get("obi_5"))
        if abs(obi) < Decimal("0.9") or abs(obi) > 1 or (obi > 0) != (signal["side"] == "buy"):
            raise DemoSafetyError("second-level signal violates the frozen OBI trigger")

    def protect_cycle(self, arm, signal, plan, entry, *, recovery=False, paper=None):
        """Terminalize entry, hold boundedly, then close only attributable size."""
        client = entry["client_id"]
        order, first_fill = self.terminalize_entry(arm, client, recovery=recovery)
        entry_quantity = _decimal(order["filled_qty"])
        clients = [client]
        old_exits = [row for row in self.ledgers[arm].runner_orders()
                     if row["signal_id"] == signal["signal_id"] and row["reduce_only"]]
        unclaimed_exits = []
        for row in old_exits:
            if row.get("submission_attempts") == 0 and row.get("exchange_order_id") is None and row.get("status") == "intent":
                unclaimed_exits.append(row)
            self.intents[row["client_id"]] = deepcopy(row)
            self.adapter.authorize_owned_client_id(row["client_id"])
            self.lookup(arm, row["client_id"])
            clients.append(row["client_id"])
        if (not recovery and not old_exits and not self.halt_entries):
            hold_until = self.signal_mono_deadlines.get(signal["signal_id"], self.mono())
            while self.mono() < hold_until:
                self.sleep(self.config.poll_seconds)
                if self.mono() >= hold_until:
                    break
                try:
                    self.quote(signal, paper, deadline=hold_until)
                except Exception as exc:
                    if self.mono() < hold_until:
                        self.error(exc, "paper_quote")
                    break
        deadline = self.mono() + self.config.reconciliation_seconds
        position_flat, opens_zero = False, False
        while True:
            for exit_id in clients[1:]:
                self.lookup(arm, exit_id)
            exit_orders = [self.observed.get(exit_id) for exit_id in clients[1:]]
            exited = sum((_decimal(item["filled_qty"]) for item in exit_orders if item), Decimal(0))
            expected = entry_quantity - exited
            if expected < 0:
                raise DemoSafetyError("second-level exits exceed attributable entry fills")
            positions, opens = self.snapshot(arm, signal["pair"])
            own_ids = set(clients)
            if any(item.get("client_id") not in own_ids for item in opens):
                raise DemoSafetyError("foreign open order blocks second-level protection attribution")
            if positions and (len(positions) != 1 or positions[0][0].get("pair") != signal["pair"]
                    or positions[0][0].get("position_side") != "BOTH"
                    or (positions[0][1] > 0) != (signal["side"] == "buy")
                    or abs(positions[0][1]) > entry_quantity):
                raise DemoSafetyError("foreign position blocks second-level protection attribution")
            actual_quantity = abs(positions[0][1]) if positions else Decimal(0)
            position_flat, opens_zero = not positions, not opens
            pending_exit = any(item is None or _status(item["status"]) not in TERMINAL for item in exit_orders)
            if actual_quantity == 0 and expected == 0 and opens_zero and not pending_exit:
                break
            # Do not infer zero from a lagging position endpoint after a fill.
            # Require the order ledger and positionRisk to agree before creating
            # a new exit. Previously claimed exits only receive GETs above.
            unclaimed_ids = {row["client_id"] for row in unclaimed_exits}
            pending_claimed_exit = any((item is None or _status(item["status"]) not in TERMINAL)
                and exit_id not in unclaimed_ids for exit_id, item in zip(clients[1:], exit_orders))
            if (actual_quantity == expected and actual_quantity > 0 and unclaimed_exits
                    and not pending_claimed_exit and opens_zero):
                row = unclaimed_exits[0]
                if _decimal(row["quantity"]) > actual_quantity:
                    raise DemoSafetyError("frozen unclaimed exit exceeds currently attributable exposure")
                # Use the original durable intent byte-for-byte, never rebuild
                # a smaller quantity from a partially published position.
                self.submit(arm, row, entry=False)
                unclaimed_exits.pop(0)
                continue
            if actual_quantity == expected and actual_quantity > 0 and not pending_exit and opens_zero:
                if len(clients) >= 5:
                    raise DemoSafetyError("second-level protective remainder limit reached")
                self.adapter.validate_order(signal["pair"], actual_quantity, market=True)
                role = "exit" if len(clients) == 1 else "exit_remainder_" + str(len(clients) - 1)
                exit_intent = self.frozen_intent(arm, signal, plan, role, format(actual_quantity, "f"))
                self.submit(arm, exit_intent, entry=False)
                clients.append(exit_intent["client_id"])
                continue
            if self.mono() >= deadline:
                raise DemoSafetyError("second-level own exposure or exit outcome remains unresolved")
            self.sleep(self.config.poll_seconds)
        if paper is not None:
            if not paper.terminal:
                try:
                    self.quote(signal, paper)
                except Exception as exc:
                    self.error(exc, "paper_horizon")
            self.compare(arm, entry, "second_level_paper_result", {"paper": _paper_result(paper)})
        deadline = self.mono() + self.config.reconciliation_seconds
        fill_complete = self.reconcile_fills(arm, clients)
        while not fill_complete and self.mono() < deadline:
            self.sleep(self.config.poll_seconds)
            fill_complete = self.reconcile_fills(arm, clients)
        rows = [row for row in self.ledgers[arm].runner_orders() if row["signal_id"] == signal["signal_id"]]
        fills = [fill for fill in self.ledgers[arm].runner_fills() if fill["client_id"] in clients]
        round_trips = derive_round_trips(fills, rows)
        actual = [fill for key in clients for fill in self.fill_facts.get(key, [])]
        cycle = {"signal_id": signal["signal_id"], "arm": arm, "side": signal["side"],
            "pair": signal["pair"], "maker_distance_bp": signal.get("maker_distance_bp"),
            "source_event_ms": signal["source_event_ms"], "decision_ms": signal["decision_ms"],
            "source_age_at_decision_ms": signal["decision_ms"] - signal["source_event_ms"],
            "intended_exit_horizon_ms": signal["decision_ms"] + 10_000,
            "first_fill_locally_observed_monotonic_seconds": first_fill,
            "achieved_hold_source": "actual_exchange_fill_timestamps_in_round_trips",
            "input_fingerprint": signal["input_fingerprint"], "recovered": recovery,
            "entry_terminal_status": _status(order["status"]),
            "entry_filled_quantity": str(entry_quantity), "frozen_entry_quantity": str(entry["quantity"]),
            "entry_exchange_accepted": bool(order.get("order_id")),
            "position_flat": position_flat, "open_orders_zero": opens_zero,
            "fill_reconciliation_complete": fill_complete,
            "round_trips": round_trips, "fees": summarize_fill_fees(actual),
            "orders": [self.observed[key].get("raw", {}) for key in clients if key in self.observed],
            "fills": [fill["raw"] for fill in actual],
            "paper": _paper_result(paper) if paper else {"status": "not_reconstructed_on_recovery", "actual_fill": False},
            "maker_is_mixed_execution": arm == "maker", "no_profitability_verdict": True}
        self.compare(arm, entry, "second_level_cycle_result", {"cycle": cycle})
        self.report["cycles"] = [row for row in self.report["cycles"] if row["signal_id"] != signal["signal_id"]]
        self.report["cycles"].append(cycle)
        if not fill_complete:
            self.halt_entries = True
        return cycle

    def recover(self):
        unresolved = []
        for group in self.groups.values():
            rows = group["rows"]
            net = sum((_decimal(row["filled_quantity"]) * (1 if not row["reduce_only"] else -1)
                       for row in rows), Decimal(0))
            if (any(_status(row["status"]) not in TERMINAL for row in rows) or net != 0):
                unresolved.append(group)
        if len(unresolved) > 1:
            raise DemoSafetyError("multiple unresolved signals violate sequential second-level execution")
        all_ids = set(self.intents)
        positions, opens = self.snapshot()
        if any(item.get("client_id") not in all_ids for item in opens):
            raise DemoSafetyError("foreign account open orders block second-level session")
        if not unresolved:
            if positions or opens:
                raise DemoSafetyError("unattributed account risk blocks second-level session")
            # A closed order cycle can still lack durable fill/fee telemetry
            # after a crash. Recovery is GET-only for these completed groups.
            for group in self.groups.values():
                for row in group["rows"]:
                    self.lookup(group["arm"], row["client_id"])
                self.protect_cycle(group["arm"], group["signal"], group["plan"], group["entry"], recovery=True)
            return
        group = unresolved[0]
        entry = group["entry"]
        arm = group["arm"]
        if entry.get("submission_attempts") == 0 and entry.get("exchange_order_id") is None:
            # Never execute a previously prepared stale entry on restart.
            self.ledgers[arm].abandon_unclaimed_order(entry["client_id"], "restart_expired_second_level_signal", self.now())
            if positions or opens:
                raise DemoSafetyError("unclaimed second-level entry cannot explain account risk")
        else:
            self.lookup(arm, entry["client_id"])
            self.protect_cycle(arm, group["signal"], group["plan"], entry, recovery=True)
        # Protection has priority. Only after the unresolved cycle is flat may
        # a previous closed cycle's missing audit fills consume GET budget.
        for previous in self.groups.values():
            if previous is group:
                continue
            for row in previous["rows"]:
                self.lookup(previous["arm"], row["client_id"])
            self.protect_cycle(previous["arm"], previous["signal"], previous["plan"],
                               previous["entry"], recovery=True)

    def run(self):
        start_mono = self.mono()
        self.run_deadline = start_mono + self.config.max_duration_seconds
        reasons = Counter()
        try:
            self.initialize()
            self.recover()
            if not self.recovery_only and not self.halt_entries:
                self.run_new(start_mono, reasons)
            positions, opens = self.snapshot()
            self.report["position_flat"], self.report["open_orders_zero"] = not positions, not opens
            rows = [row for ledger in self.ledgers.values() for row in ledger.runner_orders()]
            unresolved = [row for row in rows if _status(row["status"]) not in TERMINAL]
            self.report["requires_operator_recovery"] = bool(positions or opens or unresolved)
            self.report["success"] = (not self.report["requires_operator_recovery"]
                and self.report["persistence_complete"]
                and all(cycle["fill_reconciliation_complete"] and
                    (_decimal(cycle["entry_filled_quantity"]) == 0 or
                     (cycle["fees"]["commission_complete"] and len(cycle["round_trips"]) == 1
                      and cycle["round_trips"][0].get("actual_net_trading_pnl_usdt") is not None))
                    for cycle in self.report["cycles"]))
        except Exception as exc:
            self.error(exc, "second_level_session")
            # Durable rows remain the source of truth. Reconcile and protect a
            # claimed entry even if pricing, telemetry or storage failed later.
            try:
                if not self.namespaces_validated:
                    raise DemoSafetyError("second-level safety preflight did not authorize recovery")
                self.groups = self.load_groups()
                self.recover()
                positions, opens = self.snapshot()
                self.report["position_flat"], self.report["open_orders_zero"] = not positions, not opens
                rows = [row for ledger in self.ledgers.values() for row in ledger.runner_orders()]
                self.report["requires_operator_recovery"] = bool(positions or opens or
                    any(_status(row["status"]) not in TERMINAL for row in rows))
            except Exception as protect_exc:
                self.error(protect_exc, "protective_recovery")
                self.report["requires_operator_recovery"] = True
        finally:
            self.report["source_reason_counts"] = dict(reasons)
            self.report["observed_arms"] = sorted({cycle["arm"] for cycle in self.report["cycles"]})
            self.report["coverage_population"] = "evaluated_cycles_including_local_prePOST_rejections"
            self.report["accepted_arms"] = sorted({cycle["arm"] for cycle in self.report["cycles"]
                if cycle["entry_exchange_accepted"]})
            self.report["filled_arms"] = sorted({cycle["arm"] for cycle in self.report["cycles"]
                if _decimal(cycle["entry_filled_quantity"]) > 0})
            self.report["observed_maker_distances_bp"] = sorted({cycle["maker_distance_bp"]
                for cycle in self.report["cycles"] if cycle["arm"] == "maker"})
            self.report["finished_ms"] = self.now()
            self.report["entries_halted_after_error"] = self.halt_entries
            self.report["elapsed_monotonic_seconds"] = self.mono() - start_mono
            source_state = getattr(self.source, "state", None)
            if isinstance(source_state, dict):
                self.report["signal_source_state"] = deepcopy(source_state)
            self.report["stop_reason"] = ("requires_operator_recovery" if self.report["requires_operator_recovery"] else
                "request_or_audit_failure" if self.halt_entries else "finite_sample_or_duration_limit")
            for arm in self.identities:
                if self.namespaces_validated and arm in self.ledgers:
                    self.compare(arm, self.identities[arm], "second_level_session_result", {"report": self.report})
            if not self.report["persistence_complete"]:
                self.report["success"] = False
        return self.report

    def run_new(self, start_mono, reasons):
        deadline = start_mono + self.config.max_duration_seconds
        consumed = set(self.groups)
        while self.mono() < deadline and self.report["signal_count"] < self.config.max_signals and not self.halt_entries:
            if not self.budget_available():
                reasons["local_rate_budget_wait"] += 1
                self.sleep(self.config.poll_seconds)
                continue
            self.report["source_poll_count"] += 1
            signal = (self.source.latest_signal(self.now()) if hasattr(self.source, "latest_signal") else self.source(self.now()))
            decision = getattr(self.source, "last_decision", {})
            reason = decision.get("reason", "no_signal" if signal is None else "signal")
            reasons[str(reason)] += 1
            if signal is None:
                if self.report["source_poll_count"] % 30 == 1:
                    self.compare("maker", self.identities["maker"], "second_level_source_observation",
                        {"decision": deepcopy(decision), "reason_counts": dict(reasons)})
                self.sleep(self.config.poll_seconds)
                continue
            self.validate_signal(signal)
            self.signal_mono_deadlines[signal["signal_id"]] = self.mono() + min(10, max(0,
                (signal["decision_ms"] + 10_000 - self.now()) / 1000))
            if signal["signal_id"] in consumed:
                reasons["duplicate_signal_ignored"] += 1
                self.sleep(self.config.poll_seconds)
                continue
            consumed.add(signal["signal_id"])
            self.report["signal_count"] += 1
            arm = signal["arm"]
            identity = {**self.identities[arm], "signal_id": signal["signal_id"],
                        "input_fingerprint": signal["input_fingerprint"]}
            self.compare(arm, identity, "second_level_signal", {"signal": deepcopy(signal)}, critical=True)
            positions, opens = self.snapshot(arm)
            if positions or opens:
                raise DemoSafetyError("new second-level signal requires globally flat account")
            if not self.report["persistence_complete"]:
                raise DemoSafetyError("second-level preflight audit persistence failed")
            # REST receipt is fresh locally; raw exchange time is evidence, not
            # silently substituted for the clock of a live-market signal.
            quote = self.quote(signal)
            try:
                plan = build_entry_plan(self.adapter, signal["pair"], signal["side"], arm,
                    signal.get("maker_distance_bp"), quote, self.metadata,
                    cap=self.config.max_notional_usdt, crossing_bp=self.config.ioc_crossing_cap_bp)
            except DemoSafetyError as exc:
                self.compare(arm, identity, "second_level_plan_rejected", {"reason": "sizing_or_quote_guard",
                    "error": _error(exc, "entry_plan"), "signal": deepcopy(signal)})
                reasons["sizing_or_quote_guard"] += 1
                continue
            if self.now() - signal["decision_ms"] > 5000 or self.mono() >= deadline:
                self.compare(arm, identity, "second_level_signal_expired", {"reason": "preflight_latency_exceeded_signal_budget"})
                reasons["signal_expired_during_preflight"] += 1
                continue
            paper = SecondLevelPaperControl(signal, plan["quantity"])
            # Feed only a causally subsequent receipt into the paper path.
            paper.observe_book_ticker(quote, request_started_ms=quote["local_http_started_ms"],
                                      observed_ms=quote["local_received_ms"])
            self.compare(arm, identity, "second_level_paper_start", {"paper": paper.result}, critical=True)
            try:
                self.validate_signal(signal)  # Immediately before the durable claim and POST.
                if self.mono() >= deadline:
                    raise DemoSafetyError("finite second-level deadline reached before submission")
            except DemoSafetyError:
                self.compare(arm, identity, "second_level_signal_expired",
                             {"reason": "signal_or_source_expired_before_submission"})
                reasons["signal_expired_before_submission"] += 1
                continue
            entry = self.frozen_intent(arm, signal, plan, "entry")
            self.submit(arm, entry, entry=True)
            self.protect_cycle(arm, signal, plan, entry, paper=paper)
            if self.halt_entries:
                break
            self.sleep(self.config.poll_seconds)


def run_second_level_session(adapter, ledgers, source, *, config, clock_ms=None, sleep=None, monotonic=None):
    """Run at most the frozen sample/time bound under the caller's account lock.

    ``ledgers`` must contain maker/taker DemoStore instances with namespaces
    ``second_level_<session_id>_<arm>``. ``source.latest_signal(now_ms)`` (or a
    callable) must emit the frozen signals module's causal, preassigned signal.
    The caller must block concurrent account writers and unfinished other
    sessions before invoking this function. All real orders remain native.
    """
    return _Session(adapter, ledgers, source, config, clock_ms, sleep, monotonic).run()


def recover_second_level_session(adapter, ledgers, *, config, clock_ms=None, sleep=None, monotonic=None):
    """Lookup claimed IDs, cancel remaining entries, and flatten owned exposure.

    New signals are disabled. Existing frozen exit quantities are never rebuilt
    from a smaller current position. A terminal partial exit can receive a new
    uniquely reserved reduce-only remainder order; the claimed ID is not resent.
    """
    return _Session(adapter, ledgers, None, config, clock_ms, sleep, monotonic, recovery_only=True).run()

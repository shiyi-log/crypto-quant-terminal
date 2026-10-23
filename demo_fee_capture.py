"""Bounded, restart-safe Binance Demo fee capture execution core.

Credentials, account locking and report persistence belong to the caller.  This
module accepts only the guarded Demo adapter and an isolated DemoStore ledger.
Every order is durably claimed before its sole submission attempt.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

from binance_demo_adapter import (
    ALLOWED_PAIRS, DEMO_HOST, DemoExchangeRejected, DemoRequestError,
    DemoSafetyError, DemoSubmissionUnknown, stable_client_order_id,
)
from demo_execution_service import derive_round_trips

VERSION = "binance-demo-fee-capture-v1"
STRATEGY_ID = "manual_fee_capture"
TERMINAL = {"filled", "canceled", "rejected", "expired", "expired_in_match"}


def _d(value, *, positive=False, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool):
        raise ValueError("invalid decimal")
    try:
        result = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        raise ValueError("invalid decimal") from None
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError("invalid decimal")
    return result


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def expected_risk_gate_target_binding(account_scope, *, pairs, max_notional_usdt,
                                      maker_ttl_seconds, total_timeout_seconds,
                                      reconciliation_seconds):
    """Build the exact target binding used by this execution core."""
    import research_ledger
    policy = {"version": VERSION, "pairs": list(pairs),
              "max_notional_usdt": format(_d(max_notional_usdt, positive=True), "f"),
              "maker_ttl_seconds": maker_ttl_seconds,
              "total_timeout_seconds": total_timeout_seconds,
              "reconciliation_seconds": reconciliation_seconds,
              "maker_entry": "LIMIT_GTX_fresh_bid", "taker_entry": "LIMIT_IOC_protective_cross",
              "exit": "MARKET_reduce_only"}
    return research_ledger.build_risk_gate_target_binding(account_scope, [{
        "strategy_id": STRATEGY_ID,
        "rule_hash": _hash({"version": VERSION, "pairs": list(pairs)}),
        "execution_hash": _hash(policy),
    }])


def _validate_risk_gate_proof(proof, expected_binding):
    import research_ledger
    if not isinstance(proof, dict) or proof.get("risk_segment_status") != "closed" \
            or proof.get("preflight_status") != "pass":
        raise DemoSafetyError("risk_gate_not_closed")
    try:
        binding = proof["target_binding"]
        if proof.get("target_binding_hash") != research_ledger.risk_gate_target_binding_hash(binding):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise DemoSafetyError("risk_gate_identity_invalid") from None
    if binding != expected_binding:
        raise DemoSafetyError("risk_gate_target_mismatch")
    return dict(proof)


def _status(value):
    text = str(value).lower()
    return {"new": "open", "cancelled": "canceled", "partiallyfilled": "partially_filled",
            "expired_in_match": "expired"}.get(text, text)


def _error(exc, stage):
    if isinstance(exc, DemoSubmissionUnknown):
        kind = "submission_unknown"
    elif isinstance(exc, DemoExchangeRejected):
        kind = "exchange_rejected"
    elif isinstance(exc, DemoSafetyError):
        kind = "safety_rejected"
    elif isinstance(exc, DemoRequestError):
        kind = "request_failed"
    else:
        kind = "storage_or_internal_error"
    code = getattr(exc, "exchange_code", None)
    return {"stage": stage, "kind": kind,
            "exchange_code": code if isinstance(code, int) and not isinstance(code, bool) else None}


def _positions(rows):
    found = []
    for row in rows or []:
        try:
            qty = _d(row.get("signed_qty", row.get("positionAmt", 0)))
        except Exception:
            raise DemoSafetyError("invalid Demo position response") from None
        if qty:
            found.append((row, qty))
    return found


def _market_rules(info, pair):
    symbol = pair.replace("/", "").replace(":USDT", "")
    item = next((row for row in info.get("symbols", []) if row.get("symbol") == symbol), None)
    if item is None or item.get("status") != "TRADING":
        raise DemoSafetyError("fee capture market unavailable")
    filters = {row.get("filterType"): row for row in item.get("filters", [])}
    lot, market, price, notional = (filters.get(key) for key in
                                    ("LOT_SIZE", "MARKET_LOT_SIZE", "PRICE_FILTER", "MIN_NOTIONAL"))
    if any(row is None for row in (lot, market, price, notional)):
        raise DemoSafetyError("fee capture market rules incomplete")
    return symbol, lot, market, price, notional


def _common_step(a, b):
    places = max(0, -a.as_tuple().exponent, -b.as_tuple().exponent)
    scale = 10 ** places
    return Decimal(math.lcm(int(a * scale), int(b * scale))) / scale


def _make_plan(adapter, info, pair, book, cap):
    symbol, lot, market, price_filter, notional_filter = _market_rules(info, pair)
    tick = _d(price_filter["tickSize"], positive=True)
    limit_step = _d(lot["stepSize"], positive=True)
    market_step = _d(market["stepSize"], positive=True)
    step = _common_step(limit_step, market_step)
    min_qty = max(_d(lot["minQty"]), _d(market["minQty"]))
    max_qty = min(_d(lot["maxQty"]), _d(market["maxQty"]))
    min_notional = _d(notional_filter["notional"], positive=True)
    bid, ask = _d(book["bid_price"], positive=True), _d(book["ask_price"], positive=True)
    maker_price = (bid / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    taker_price = (ask * Decimal("1.005") / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    qty = (max(min_qty, min_notional / min(maker_price, taker_price)) / step).to_integral_value(
        rounding=ROUND_CEILING) * step
    if qty > max_qty or qty * taker_price > cap:
        raise DemoSafetyError("fee capture market minimum exceeds notional cap")
    prepared = adapter.prepare_limit_order(pair, "buy", qty, maker_price)
    if _d(prepared["quantity"]) != qty or _d(prepared["price"]) != maker_price:
        raise DemoSafetyError("fee capture maker sizing changed")
    adapter.validate_order(pair, qty, taker_price)
    adapter.validate_order(pair, qty, market=True)
    return {"pair": pair, "symbol": symbol, "quantity": format(qty, "f"),
            "step": format(step, "f"), "maker_price": format(maker_price, "f"),
            "taker_price": format(taker_price, "f"),
            "maker_notional_usdt": format(qty * maker_price, "f"),
            "taker_notional_usdt": format(qty * taker_price, "f")}


def run_fee_capture(adapter, ledger, *, clock_ms=None, sleep=None,
                    pairs=ALLOWED_PAIRS, max_notional_usdt="75",
                    maker_ttl_seconds=30, total_timeout_seconds=600,
                    reconciliation_seconds=60, recovery_only=False,
                    risk_gate_proof=None):
    """Capture actual maker/taker fills for the requested Demo pairs.

    Claimed client IDs are lookup-only on every invocation.  An unresolved
    exchange or storage fact stops new entries and requires same-session
    recovery.  ``recovery_only`` performs protection and reconciliation only.
    """
    now, pause = clock_ms or (lambda: int(time.time() * 1000)), sleep or time.sleep
    namespace = getattr(ledger, "virtual_account_key", "")
    try:
        pairs = tuple(pairs)
    except Exception:
        pairs = ()
    report = {"environment": "binance_demo", "endpoint": DEMO_HOST,
              "experiment_scope": "fee_capture", "strategy_id": STRATEGY_ID,
              "virtual_account_key": namespace, "success": False,
              "recovery_required": False, "recoveryRequired": False,
              "recovery_only": bool(recovery_only), "position_flat": False,
              "open_orders_zero": False, "fill_reconciliation_complete": False,
              "persistence_complete": True, "orders": [], "fills": [],
              "samples": [], "fee_summary": [], "round_trips": [], "errors": [],
              "quoted_rates": [], "entry_submission_attempts": 0,
              "exit_submission_attempts": 0,
              "maker_verified_by_pair": {pair: False for pair in pairs},
              "maker_actual_fill_verified": False}
    try:
        cap = _d(max_notional_usdt, positive=True)
        if (not isinstance(namespace, str) or not namespace.startswith("fee_capture_")
                or getattr(ledger, "environment", None) != "binance_demo"
                or getattr(ledger, "endpoint_host", None) != DEMO_HOST
                or adapter.allow_trading is not True):
            raise DemoSafetyError("isolated Binance Demo fee capture required")
        if (not pairs or len(set(pairs)) != len(pairs) or any(pair not in ALLOWED_PAIRS for pair in pairs)
                or cap > 75 or isinstance(maker_ttl_seconds, bool) or not isinstance(maker_ttl_seconds, int)
                or not 30 <= maker_ttl_seconds <= 60 or isinstance(total_timeout_seconds, bool)
                or not isinstance(total_timeout_seconds, int)
                or not maker_ttl_seconds <= total_timeout_seconds <= 600
                or isinstance(reconciliation_seconds, bool) or not isinstance(reconciliation_seconds, int)
                or not 1 <= reconciliation_seconds <= 120):
            raise DemoSafetyError("fee capture configuration rejected")

        policy = {"version": VERSION, "pairs": list(pairs), "max_notional_usdt": format(cap, "f"),
                  "maker_ttl_seconds": maker_ttl_seconds, "total_timeout_seconds": total_timeout_seconds,
                  "reconciliation_seconds": reconciliation_seconds,
                  "maker_entry": "LIMIT_GTX_fresh_bid", "taker_entry": "LIMIT_IOC_protective_cross",
                  "exit": "MARKET_reduce_only"}
        base = {"strategy_id": STRATEGY_ID, "signal_id": namespace + ":fee_capture",
                "rule_hash": _hash({"version": VERSION, "pairs": list(pairs)}),
                "execution_hash": _hash(policy), "input_fingerprint": _hash(policy)}
        binding = {**base, "policy": policy, "experiment_scope": "fee_capture"}
        old_binding = ledger.get_binding()
        if old_binding is not None and old_binding != binding:
            raise DemoSafetyError("fee capture session binding mismatch")
        ledger.bind_account(binding)

        def persist(method, *args):
            try:
                method(*args)
                return True
            except Exception as exc:
                report["persistence_complete"] = False
                report["errors"].append(_error(exc, "ledger_projection"))
                return False

        if adapter.fetch_position_mode().get("hedged") is not False:
            raise DemoSafetyError("fee capture requires one-way position mode")
        info = adapter.fetch_exchange_info()
        plans, windows = {}, {}
        all_orders = ledger.runner_orders()
        comparisons = ledger.list_comparisons(limit=1000)
        resumed_session = bool(all_orders) or any(
            row.get("stage") == "fee_capture_pair_start" for row in comparisons)
        recovery_only = bool(recovery_only or resumed_session)
        report["recovery_only"] = recovery_only
        if not recovery_only:
            expected_binding = expected_risk_gate_target_binding(
                risk_gate_proof.get("target_binding", {}).get("account_scope")
                if isinstance(risk_gate_proof, dict) else "",
                pairs=pairs, max_notional_usdt=cap,
                maker_ttl_seconds=maker_ttl_seconds,
                total_timeout_seconds=total_timeout_seconds,
                reconciliation_seconds=reconciliation_seconds)
            risk_gate_proof = _validate_risk_gate_proof(risk_gate_proof, expected_binding)
        for pair in pairs:
            book = adapter.fetch_book_ticker(pair)
            plans[pair] = _make_plan(adapter, info, pair, book, cap)
            try:
                report["quoted_rates"].append({"pair": pair, **adapter.fetch_commission_rate(pair),
                                               "source": "demo_signed_account_commission_rate_quote"})
            except Exception as exc:
                report["errors"].append(_error(exc, "commission_quote"))
            pair_id = namespace + ":pair:" + pair
            saved = [row["payload"] for row in comparisons if row.get("stage") == "fee_capture_pair_start"
                     and row.get("payload", {}).get("signal_id") == pair_id]
            pair_orders = [row for row in all_orders if row.get("pair") == pair]
            if len(saved) > 1:
                raise DemoSafetyError("duplicate fee capture pair checkpoint")
            if saved:
                state = saved[0]
                if state.get("policy_hash") != _hash(policy) or state.get("pair") != pair:
                    raise DemoSafetyError("fee capture pair checkpoint mismatch")
                plans[pair] = state["plan"]
                windows[pair] = (state["started_ms"], state["deadline_ms"])
            elif pair_orders:
                raise DemoSafetyError("fee capture pair checkpoint missing")
            else:
                started = now()
                state = {**base, "signal_id": pair_id, "pair": pair, "started_ms": started,
                         "deadline_ms": started + int(total_timeout_seconds * 1000),
                         "policy_hash": _hash(policy), "plan": plans[pair]}
                if not persist(ledger.append_comparison, state, "fee_capture_pair_start", started):
                    raise DemoSafetyError("fee capture checkpoint persistence failed")
                windows[pair] = (started, state["deadline_ms"])

        stop_entries = bool(recovery_only)
        if not all_orders:
            if _positions(adapter.fetch_positions()) or adapter.fetch_open_orders():
                raise DemoSafetyError("fee capture requires a flat account without open orders")
        else:
            known = {row.get("client_id") for row in all_orders}
            if any(row.get("client_id") not in known for row in adapter.fetch_open_orders()):
                raise DemoSafetyError("foreign Demo open order blocks fee capture")

        facts, seen_orders, fills_by_client = {}, {}, {}
        # Keep reconciliation bookkeeping private; the returned report is a
        # persistence boundary and must contain JSON-compatible values only.
        reconciled_clients = set()

        def build_intent(pair, arm, attempt, role, qty, price, stamp, *, reduce_only=False):
            side = "sell" if reduce_only else "buy"
            signal = f"{namespace}:{pair}:{arm}:{attempt}"
            tif = None if reduce_only else ("GTX" if arm == "maker" else "IOC")
            client = stable_client_order_id(namespace, signal, pair, side, role)
            return {**base, "signal_id": signal, "client_id": client, "intent_key": role,
                    "pair": pair, "side": side, "order_type": "market" if reduce_only else "limit",
                    "quantity": str(qty), "price": price, "time_in_force": tif,
                    "reduce_only": reduce_only, "signal_ms": stamp, "created_ms": stamp,
                    "intent_payload": {"scope": "fee_capture", "arm": arm,
                                       "attempt": attempt, "role": role, "policy_hash": _hash(policy)}}

        def strict_observe(client, row, intent):
            if row is None:
                return None
            raw = row.get("raw") or {}
            if (row.get("client_id") != client or row.get("order_id") is None
                    or row.get("pair") != intent["pair"]
                    or str(row.get("side", "")).lower() != intent["side"]
                    or _d(row.get("qty")) != _d(intent["quantity"])
                    or str(raw.get("type", row.get("type", ""))).lower() != intent["order_type"]
                    or raw.get("reduceOnly") is not intent["reduce_only"]):
                raise DemoSafetyError("fee capture order identity/protection mismatch")
            if intent["order_type"] == "limit":
                if raw.get("timeInForce") != intent["time_in_force"]:
                    raise DemoSafetyError("fee capture time-in-force mismatch")
                # A post-only order must remain at the frozen bid.  An IOC
                # protection price is a bound; Binance may return the actual
                # execution/average price in the order's price field.
                if intent["time_in_force"] == "GTX" and _d(raw.get("price")) != _d(intent["price"]):
                    raise DemoSafetyError("fee capture maker price mismatch")
            if raw.get("positionSide") not in (None, "BOTH"):
                raise DemoSafetyError("fee capture order has unexpected position side")
            filled = _d(row.get("filled_qty", 0))
            if filled < 0 or filled > _d(intent["quantity"]):
                raise DemoSafetyError("fee capture execution quantity invalid")
            seen_orders[client] = row
            persist(ledger.record_order_observation, client, row)
            return row

        def lookup(client, intent):
            try:
                return strict_observe(client, adapter.fetch_order_by_client_id(intent["pair"], client), intent)
            except Exception as exc:
                report["errors"].append(_error(exc, "order_reconciliation"))
                persist(ledger.append_reconciliation, client, _error(exc, "order_reconciliation"), now())
                return None

        def submit(intent, arm):
            nonlocal stop_entries
            row, _ = ledger.reserve_order(intent)
            client = intent["client_id"]
            facts[client] = intent
            if ledger.claim_submission(client, now()):
                adapter.authorize_owned_client_id(client)
                is_exit = intent["reduce_only"]
                report["exit_submission_attempts" if is_exit else "entry_submission_attempts"] += 1
                try:
                    if is_exit:
                        result = adapter.submit_market_order(intent["pair"], "sell", intent["quantity"], client,
                                                            reduce_only=True)
                    elif arm == "maker":
                        result = adapter.submit_limit_order(intent["pair"], "buy", intent["quantity"], intent["price"],
                                                            client, post_only=True, reduce_only=False)
                    else:
                        result = adapter.submit_ioc_entry(intent["pair"], "buy", intent["quantity"], intent["price"], client)
                    strict_observe(client, result, intent)
                except Exception as exc:
                    report["errors"].append(_error(exc, "exit_submission" if is_exit else "entry_submission"))
                    stop_entries = True
                    if isinstance(exc, DemoExchangeRejected):
                        persist(ledger.record_order_observation, client,
                                {"status": "rejected", "filled_qty": "0", "raw": {}, "exchange_updated_ms": now()})
                    else:
                        persist(ledger.mark_submission_unknown, client, _error(exc, "submission"), now())
                        report["recovery_required"] = report["recoveryRequired"] = True
            else:
                adapter.authorize_owned_client_id(client)
            return lookup(client, intent) or seen_orders.get(client)

        def fetch_fills(client, order, intent):
            nonlocal stop_entries
            if order is None or order.get("order_id") is None:
                return False, []
            try:
                fills = adapter.fetch_trades(intent["pair"], order_id=order["order_id"])
                if len(fills) >= 1000:
                    raise DemoSafetyError("fee capture trade page may be truncated")
                total, unique, accepted = Decimal(0), set(), []
                for fill in fills:
                    qty = _d(fill.get("qty"), positive=True)
                    stamp = fill.get("ts")
                    if (isinstance(stamp, bool) or not isinstance(stamp, int) or stamp < 0
                            or fill.get("fill_id") in unique or str(fill.get("order_id")) != str(order["order_id"])
                            or fill.get("pair") != intent["pair"] or fill.get("side") != intent["side"]):
                        raise DemoSafetyError("fee capture fill identity mismatch")
                    if fill.get("commission") is not None:
                        _d(fill["commission"])
                    if fill.get("is_maker") is not None and not isinstance(fill.get("is_maker"), bool):
                        raise DemoSafetyError("fee capture maker flag invalid")
                    unique.add(fill["fill_id"])
                    total += qty
                    accepted.append(fill)
                    if not persist(ledger.record_fill, client, fill):
                        stop_entries = True
                complete = total == _d(order.get("filled_qty", 0))
                fills_by_client[client] = accepted
                if complete:
                    reconciled_clients.add(client)
                if not complete:
                    report["errors"].append({"stage": "actual_fill_reconciliation", "kind": "fill_quantity_gap",
                                             "exchange_code": None})
                    stop_entries = True
                return complete, accepted
            except Exception as exc:
                report["errors"].append(_error(exc, "actual_fill_reconciliation"))
                stop_entries = True
                return False, []

        def assert_owned_position(pair, expected, *, wait=True):
            deadline = now() + (int(reconciliation_seconds * 1000) if wait else 0)
            while True:
                rows = _positions(adapter.fetch_positions())
                if not rows and expected == 0:
                    return True
                if len(rows) == 1:
                    row, qty = rows[0]
                    if row.get("pair") == pair and row.get("position_side", "BOTH") == "BOTH" and qty == expected:
                        return True
                if now() >= deadline:
                    return False
                pause(min(1, max(0, (deadline - now()) / 1000)))

        def protect_entry(entry, order, entry_intent):
            nonlocal stop_entries
            target = _d(order.get("filled_qty", 0))
            if target <= 0:
                return True
            exits = [row for row in ledger.runner_orders() if row.get("signal_id") == entry["signal_id"]
                     and row.get("reduce_only")]
            exited = sum((_d(row.get("filled_quantity", 0)) for row in exits), Decimal(0))
            remaining = target - exited
            if remaining < 0:
                report["recovery_required"] = report["recoveryRequired"] = True
                stop_entries = True
                return False
            if any(_status(row.get("status")) not in TERMINAL for row in exits):
                stop_entries = True
                report["recovery_required"] = report["recoveryRequired"] = True
                return False
            if remaining == 0:
                return assert_owned_position(entry["pair"], Decimal(0))
            if not assert_owned_position(entry["pair"], remaining):
                report["recovery_required"] = report["recoveryRequired"] = True
                stop_entries = True
                return False
            adapter.validate_order(entry["pair"], format(remaining, "f"), market=True)
            exit_index = len(exits)
            exit_intent = build_intent(entry["pair"], entry_intent["intent_payload"]["arm"],
                                       entry_intent["intent_payload"]["attempt"], f"exit{exit_index}",
                                       format(remaining, "f"), None, now(), reduce_only=True)
            exit_row = submit(exit_intent, "exit")
            if exit_row is None:
                report["recovery_required"] = report["recoveryRequired"] = True
                stop_entries = True
                return False
            # Existing claimed exits are lookup-only and must be observed through
            # to terminal before any subsequent protective identity is created.
            limit = now() + int(reconciliation_seconds * 1000)
            while _status(exit_row.get("status")) not in TERMINAL and now() < limit:
                pause(1)
                exit_row = lookup(exit_intent["client_id"], exit_intent)
                if exit_row is None:
                    break
            if exit_row is None or _status(exit_row.get("status")) not in TERMINAL:
                report["recovery_required"] = report["recoveryRequired"] = True
                stop_entries = True
                return False
            fetch_fills(exit_intent["client_id"], exit_row, exit_intent)
            residual = remaining - _d(exit_row.get("filled_qty", 0))
            if residual < 0 or not assert_owned_position(entry["pair"], residual):
                report["recovery_required"] = report["recoveryRequired"] = True
                stop_entries = True
                return False
            # A terminal partial market exit may receive another distinct,
            # claimed reduce-only identity after its actual remainder is read.
            while residual > 0 and not stop_entries and len(exits) < 3:
                exits.append({"status": "filled", "filled_quantity": remaining - residual})
                exit_index += 1
                if not assert_owned_position(entry["pair"], residual):
                    break
                adapter.validate_order(entry["pair"], format(residual, "f"), market=True)
                next_exit = build_intent(entry["pair"], entry_intent["intent_payload"]["arm"],
                                         entry_intent["intent_payload"]["attempt"], f"exit{exit_index}",
                                         format(residual, "f"), None, now(), reduce_only=True)
                next_row = submit(next_exit, "exit")
                if next_row is None or _status(next_row.get("status")) not in TERMINAL:
                    report["recovery_required"] = report["recoveryRequired"] = True
                    stop_entries = True
                    return False
                fetch_fills(next_exit["client_id"], next_row, next_exit)
                residual -= _d(next_row.get("filled_qty", 0))
            return residual == 0 and assert_owned_position(entry["pair"], Decimal(0))

        # Restore every durable row first. Recovery abandons stale unclaimed
        # entries and only looks up claimed submissions.
        existing = all_orders
        grouped = defaultdict(list)
        for row in existing:
            grouped[row.get("signal_id")].append(row)
        for signal, rows in grouped.items():
            entry = next((row for row in rows if not row.get("reduce_only")), None)
            if entry is None:
                report["recovery_required"] = report["recoveryRequired"] = True
                stop_entries = True
                continue
            arm = entry.get("intent_payload", {}).get("arm")
            if arm not in {"maker", "taker"} or entry.get("strategy_id") != STRATEGY_ID:
                raise DemoSafetyError("unexpected order in fee capture namespace")
            value = dict(entry)
            facts[entry["client_id"]] = value
            if entry.get("submission_attempts", 0) == 0:
                if recovery_only:
                    ledger.abandon_unclaimed_order(entry["client_id"], "recover_only_no_new_entry", now())
                    continue
                # An unclaimed intent is the sole case eligible for its first
                # POST on resume; the exact frozen payload and client ID remain.
                row = submit(value, arm)
            else:
                adapter.authorize_owned_client_id(entry["client_id"])
                row = lookup(entry["client_id"], value)
            if row is None:
                stop_entries = True
                report["recovery_required"] = report["recoveryRequired"] = True
                continue
            if _status(row.get("status")) not in TERMINAL:
                if arm == "maker" and now() < entry.get("created_ms", now()) + maker_ttl_seconds * 1000:
                    ttl_end = min(entry.get("created_ms", now()) + maker_ttl_seconds * 1000,
                                  now() + int(reconciliation_seconds * 1000))
                    while now() < ttl_end and _status(row.get("status")) not in TERMINAL:
                        pause(min(1, max(0, (ttl_end - now()) / 1000)))
                        row = lookup(entry["client_id"], value)
                        if row is None:
                            break
                if row is not None and _status(row.get("status")) not in TERMINAL:
                    if arm == "maker" or entry.get("time_in_force") == "IOC":
                        try:
                            adapter.authorize_owned_client_id(entry["client_id"])
                            adapter.cancel_order(entry["pair"], client_order_id=entry["client_id"])
                            row = lookup(entry["client_id"], value)
                        except Exception as exc:
                            report["errors"].append(_error(exc, "entry_cancellation"))
                            row = None
                    if row is None or _status(row.get("status")) not in TERMINAL:
                        stop_entries = True
                        report["recovery_required"] = report["recoveryRequired"] = True
                        continue
            complete, _ = fetch_fills(entry["client_id"], row, value)
            if not complete:
                stop_entries = True
            exits = [r for r in rows if r.get("reduce_only")]
            for old_exit in exits:
                exit_value = dict(old_exit)
                facts[old_exit["client_id"]] = exit_value
                adapter.authorize_owned_client_id(old_exit["client_id"])
                exit_row = lookup(old_exit["client_id"], exit_value)
                if exit_row is None or _status(exit_row.get("status")) not in TERMINAL:
                    stop_entries = True
                    report["recovery_required"] = report["recoveryRequired"] = True
                    continue
                if not fetch_fills(old_exit["client_id"], exit_row, exit_value)[0]:
                    stop_entries = True
            if _d(row.get("filled_qty", 0)) > 0:
                protect_entry(entry, row, value)

        # Capture taker first, then maker. Taker IOC is price-bounded; MARKET is
        # reserved for reduce-only exits. One filled sample per role/pair suffices.
        for pair in pairs:
            if stop_entries or resumed_session:
                break
            pair_rows = [row for row in ledger.runner_orders() if row.get("pair") == pair]
            for arm in ("taker", "maker"):
                if stop_entries or recovery_only:
                    break
                arm_rows = [row for row in pair_rows if row.get("intent_payload", {}).get("arm") == arm
                            and not row.get("reduce_only")]
                if any(_d(row.get("filled_quantity", 0)) > 0 for row in arm_rows):
                    continue
                if arm == "taker" and arm_rows:
                    continue
                start, deadline = windows[pair]
                attempt = max((row.get("intent_payload", {}).get("attempt", -1)
                               for row in arm_rows), default=-1) + 1
                while now() < deadline and not stop_entries:
                    if _positions(adapter.fetch_positions()) or adapter.fetch_open_orders():
                        raise DemoSafetyError("account is not flat before fee capture entry")
                    book = adapter.fetch_book_ticker(pair)
                    plan = plans[pair]
                    if arm == "maker":
                        maker_price = _d(book["bid_price"], positive=True)
                        value_price = adapter.prepare_limit_order(pair, "buy", plan["quantity"], maker_price)["price"]
                    else:
                        ask = _d(book["ask_price"], positive=True)
                        tick = _d(_market_rules(info, pair)[3]["tickSize"], positive=True)
                        value_price = format((ask * Decimal("1.005") / tick).to_integral_value(
                            rounding=ROUND_FLOOR) * tick, "f")
                        adapter.validate_order(pair, plan["quantity"], value_price)
                    value = build_intent(pair, arm, attempt, "entry", plan["quantity"], value_price, now())
                    row = submit(value, arm)
                    if row is None:
                        stop_entries = True
                        report["recovery_required"] = report["recoveryRequired"] = True
                        break
                    status = _status(row.get("status"))
                    if status in {"open", "partially_filled"}:
                        ttl_end = min(deadline, now() + (maker_ttl_seconds * 1000 if arm == "maker" else 0))
                        while now() < ttl_end and status not in TERMINAL:
                            pause(min(1, max(0, (ttl_end - now()) / 1000)))
                            latest = lookup(value["client_id"], value)
                            if latest is None:
                                stop_entries = True
                                report["recovery_required"] = report["recoveryRequired"] = True
                                break
                            row, status = latest, _status(latest.get("status"))
                        if stop_entries:
                            break
                        if status not in TERMINAL:
                            try:
                                adapter.authorize_owned_client_id(value["client_id"])
                                adapter.cancel_order(pair, client_order_id=value["client_id"])
                                row = lookup(value["client_id"], value)
                                status = _status(row.get("status")) if row else "unknown"
                            except Exception as exc:
                                report["errors"].append(_error(exc, "entry_cancellation"))
                                row, status = None, "unknown"
                        if row is None or status not in TERMINAL:
                            stop_entries = True
                            report["recovery_required"] = report["recoveryRequired"] = True
                            break
                    reconciled, fills = fetch_fills(value["client_id"], row, value)
                    filled = _d(row.get("filled_qty", 0))
                    if filled > 0:
                        protect_entry(value, row, value)
                        if not reconciled:
                            stop_entries = True
                        break
                    if not reconciled:
                        stop_entries = True
                        break
                    if arm == "taker":
                        break
                    attempt += 1

        final_positions, final_open = adapter.fetch_positions(), adapter.fetch_open_orders()
        report["position_flat"] = not bool(_positions(final_positions))
        report["open_orders_zero"] = not bool(final_open)
        if not report["position_flat"] or not report["open_orders_zero"]:
            report["recovery_required"] = report["recoveryRequired"] = True
        orders, stored_fills = ledger.runner_orders(), ledger.runner_fills()
        report["fill_reconciliation_complete"] = bool(orders)
        for order in orders:
            if _status(order.get("status")) not in TERMINAL:
                report["fill_reconciliation_complete"] = False
                report["recovery_required"] = report["recoveryRequired"] = True
            total = sum((_d(fill.get("quantity")) for fill in stored_fills
                         if fill.get("client_id") == order.get("client_id")), Decimal(0))
            if total != _d(order.get("filled_quantity", 0)):
                report["fill_reconciliation_complete"] = False
        order_by_client = {row.get("client_id"): row for row in orders}
        quotes = {row["pair"]: row for row in report["quoted_rates"]}
        groups = defaultdict(list)
        for fill in stored_fills:
            order = order_by_client.get(fill.get("client_id"), {})
            try:
                price, qty = _d(fill.get("price"), positive=True), _d(fill.get("quantity"), positive=True)
                asset = fill.get("commission_asset")
                commission = fill.get("commission")
                rate = None if commission is None or asset != "USDT" else format(_d(commission) / (price * qty), "f")
            except Exception:
                price, qty, asset, commission, rate = None, None, fill.get("commission_asset"), fill.get("commission"), None
            raw_order = order.get("raw") or {}
            raw_fill = fill.get("raw") or {}
            maker = fill.get("is_maker")
            quote_row = quotes.get(fill.get("pair"), {})
            quoted = quote_row.get("makerCommissionRate" if maker is True else "takerCommissionRate" if maker is False else "")
            matches = None if rate is None or quoted is None else _d(rate) == _d(quoted)
            sample = {"pair": fill.get("pair"), "order_id": fill.get("exchange_order_id"),
                      "client_order_id": fill.get("client_id"), "fill_id": fill.get("exchange_trade_id"),
                      "side": order.get("side"), "price": None if price is None else str(price),
                      "quantity": None if qty is None else str(qty),
                      "commission": None if commission is None else str(commission),
                      "commission_asset": asset, "is_maker": maker, "timestamp_ms": fill.get("timestamp_ms"),
                      "transactTime": raw_fill.get("time"),
                      "order_type": raw_order.get("type", order.get("order_type", "unknown")),
                      "time_in_force": raw_order.get("timeInForce", order.get("time_in_force")),
                      "post_only": raw_order.get("timeInForce", order.get("time_in_force")) == "GTX",
                      "reduce_only": order.get("reduce_only"), "rate": rate,
                      "rate_status": "known" if rate is not None else "unknown",
                      "quoted_rate": quoted, "matches_quote": matches,
                      "rate_comparable": rate is not None and asset == "USDT"}
            report["samples"].append(sample)
            groups[(sample["pair"], maker)].append(sample)
        summaries = []
        for (pair, maker), samples in sorted(groups.items(), key=str):
            rates = [_d(s["rate"]) for s in samples if s["rate"] is not None and s["rate_comparable"]]
            quote = quotes.get(pair, {}).get("makerCommissionRate" if maker is True else "takerCommissionRate" if maker is False else "")
            comparable = [s for s in samples if s["rate_comparable"]]
            summaries.append({"pair": pair, "is_maker": maker, "fill_count": len(samples),
                              "unknown_fee_count": sum(s["rate"] is None for s in samples),
                              "rate_min": format(min(rates), "f") if rates else None,
                              "rate_max": format(max(rates), "f") if rates else None,
                              "quoted_rate": quote,
                              "matches_quote": all(s["matches_quote"] for s in comparable) if comparable else None})
        report["fee_summary"] = summaries
        for pair in pairs:
            report["maker_verified_by_pair"][pair] = any(
                s["pair"] == pair and s["is_maker"] is True and s["rate_comparable"]
                and s["commission_asset"] == "USDT"
                and s["client_order_id"] in reconciled_clients
                for s in report["samples"])
        report["maker_actual_fill_verified"] = bool(pairs) and all(report["maker_verified_by_pair"].values())
        report["round_trips"] = derive_round_trips(stored_fills, orders) if stored_fills else []
        report["orders"] = [row.get("raw", {}) for row in orders]
        report["fills"] = [row.get("raw", {}) for row in stored_fills]
        unresolved = (not report["position_flat"] or not report["open_orders_zero"]
                      or not report["fill_reconciliation_complete"]
                      or any(_status(row.get("status")) not in TERMINAL for row in orders))
        if not unresolved and report["persistence_complete"]:
            report["recovery_required"] = report["recoveryRequired"] = False
        report["success"] = (report["position_flat"] and report["open_orders_zero"]
                             and report["fill_reconciliation_complete"] and report["persistence_complete"]
                             and not report["recovery_required"]
                             and report["maker_actual_fill_verified"]
                             and all(any(s["pair"] == pair and s["is_maker"] is False
                                         and s["rate_comparable"] for s in report["samples"]) for pair in pairs))
    except Exception as exc:
        report["errors"].append(_error(exc, "fee_capture"))
        report["recovery_required"] = report["recoveryRequired"] = True
    finally:
        report["finished_ms"] = now()
    return report

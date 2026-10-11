"""One explicitly authorized Demo open/close diagnostic, with durable claims.

No credential loading, database construction, live endpoint, strategy account or
automatic retry is implemented here. The caller holds the shared execution lock
and injects an isolated ``manual_smoke_*`` DemoStore and guarded trading adapter.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

from binance_demo_adapter import (
    ALLOWED_PAIRS, DEMO_HOST, PAIR_SYMBOLS, DemoExchangeRejected,
    DemoRequestError, DemoSafetyError, DemoSubmissionUnknown,
    stable_client_order_id, summarize_fill_fees,
)
from demo_execution_service import derive_round_trips

VERSION = "manual-demo-open-close-v1"
TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}


def _decimal(value, *, positive=False):
    if isinstance(value, bool):
        raise DemoSafetyError("invalid diagnostic decimal")
    try:
        number = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        raise DemoSafetyError("invalid diagnostic decimal") from None
    if not number.is_finite() or (positive and number <= 0):
        raise DemoSafetyError("invalid diagnostic decimal")
    return number


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _common_step(left, right):
    """Exact common quantity grid; neither decimal step need divide the other."""
    places = max(0, -left.as_tuple().exponent, -right.as_tuple().exponent)
    scale = 10 ** places
    return Decimal(math.lcm(int(left * scale), int(right * scale))) / scale


def _error(exc, stage):
    kind = ("exchange_rejected" if isinstance(exc, DemoExchangeRejected) else
            "submission_unknown" if isinstance(exc, DemoSubmissionUnknown) else
            "safety_rejected" if isinstance(exc, DemoSafetyError) else
            "request_failed" if isinstance(exc, DemoRequestError) else "storage_or_internal_error")
    code = getattr(exc, "exchange_code", None)
    return {"stage": stage, "kind": kind,
            "exchange_code": code if isinstance(code, int) and not isinstance(code, bool) else None}


def _select_plan(adapter, cap):
    info = adapter.fetch_exchange_info()  # Always fresh; no stale sizing metadata.
    symbols = {item["symbol"]: item for item in info["symbols"]}
    for pair in ALLOWED_PAIRS:
        symbol = PAIR_SYMBOLS[pair]
        item = symbols.get(symbol)
        if item is None:
            continue
        filters = {row["filterType"]: row for row in item["filters"]}
        lot, market, price_filter, notional = (filters.get(name) for name in
            ("LOT_SIZE", "MARKET_LOT_SIZE", "PRICE_FILTER", "MIN_NOTIONAL"))
        if any(rule is None for rule in (lot, market, price_filter, notional)):
            continue
        quote = adapter._call("premiumIndex", "fapiPublic", "GET", {"symbol": symbol})
        if not isinstance(quote, dict) or quote.get("symbol") != symbol:
            raise DemoSafetyError("diagnostic mark price identity mismatch")
        mark = _decimal(quote.get("markPrice"), positive=True)
        tick = _decimal(price_filter["tickSize"], positive=True)
        price = ((mark * Decimal("1.005")) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        step = _common_step(_decimal(lot["stepSize"], positive=True),
                            _decimal(market["stepSize"], positive=True))
        minimum = max(_decimal(lot["minQty"]), _decimal(market["minQty"]),
                      _decimal(notional["notional"], positive=True) / min(price, mark))
        quantity = (minimum / step).to_integral_value(rounding=ROUND_CEILING) * step
        if quantity > min(_decimal(lot["maxQty"]), _decimal(market["maxQty"])) or quantity * price > cap:
            continue
        prepared = adapter.prepare_limit_order(pair, "buy", quantity, price)
        if _decimal(prepared["quantity"]) != quantity or _decimal(prepared["price"]) != price:
            raise DemoSafetyError("diagnostic sizing changed after validation")
        adapter.validate_order(pair, quantity, market=True)
        return {"pair": pair, "symbol": symbol, "quantity": format(quantity, "f"),
                "price": format(price, "f"), "mark_price": format(mark, "f"),
                "maximum_entry_notional_usdt": format(quantity * price, "f")}
    raise DemoSafetyError("no permitted market fits the diagnostic notional cap")


def _positions(rows):
    nonzero = []
    for row in rows:
        qty = _decimal(row.get("signed_qty"))
        if qty:
            nonzero.append((row, qty))
    return nonzero


def run_smoke_trade(adapter, ledger, *, clock_ms=None, sleep=None,
                    max_notional_usdt="75", entry_wait_seconds=15,
                    reconciliation_seconds=60):
    """Submit at most one entry and one reduce-only exit, then reconcile facts.

    This function never retries a POST. Re-running the same ledger namespace can
    only reconcile its original entry and submit its previously unclaimed exit.
    Network/storage errors return redacted categories; unresolved exposure is
    explicit. Order/fill projection errors do not prevent a durably claimed
    protective exit based on verified exchange order and position evidence.
    """
    now = clock_ms or (lambda: int(time.time() * 1000))
    pause = sleep or time.sleep
    namespace = getattr(ledger, "virtual_account_key", "")
    report = {"environment": "binance_demo", "endpoint": DEMO_HOST,
              "experiment_scope": "manual_smoke_test", "strategy_or_hft_evidence": False,
              "virtual_account_key": namespace, "success": False,
              "entry_submission_attempts": 0, "exit_submission_attempts": 0,
              "position_flat": False, "open_orders_zero": False,
              "fill_reconciliation_complete": False, "persistence_complete": True,
              "orders": [], "fills": [], "round_trips": [], "errors": [],
              "fee_source": "binance_demo_user_trades", "live_launch_allowed": False}
    facts, observed, actual_fills = {}, {}, {}

    def persist(method, *args, **kwargs):
        try:
            return True, method(*args, **kwargs)
        except Exception as exc:
            report["persistence_complete"] = False
            report["errors"].append(_error(exc, "ledger_projection"))
            return False, None

    def observe(client_id, order):
        if order is None:
            return None
        if order.get("client_id") != client_id or order.get("pair") != facts["pair"]:
            raise DemoSafetyError("diagnostic exchange order identity mismatch")
        intent = facts[client_id]
        if (order.get("side") != intent["side"] or _decimal(order.get("qty")) != _decimal(intent["quantity"])
                or _decimal(order.get("filled_qty")) < 0
                or _decimal(order.get("filled_qty")) > _decimal(intent["quantity"])):
            raise DemoSafetyError("diagnostic exchange order execution mismatch")
        raw = order.get("raw", {})
        if (raw.get("type") != intent["order_type"].upper()
                or raw.get("reduceOnly") is not intent["reduce_only"]):
            raise DemoSafetyError("diagnostic exchange order protection mismatch")
        old = observed.get(client_id)
        if old is not None and str(old["order_id"]) != str(order["order_id"]):
            raise DemoSafetyError("diagnostic exchange order identity changed")
        observed[client_id] = order
        persist(ledger.record_order_observation, client_id, order)
        return order

    def lookup(client_id):
        try:
            return observe(client_id, adapter.fetch_order_by_client_id(facts["pair"], client_id))
        except Exception as exc:
            report["errors"].append(_error(exc, "order_reconciliation"))
            persist(ledger.append_reconciliation, client_id, _error(exc, "order_reconciliation"), now())
            return None

    def submit(intent, *, entry):
        # Critical writes are deliberately outside persist(): without a durable
        # reserve and unique claim, absolutely no exchange POST is permitted.
        row, _created = ledger.reserve_order(intent)
        client_id = intent["client_id"]
        facts[client_id] = intent
        if ledger.claim_submission(client_id, now()):
            adapter.authorize_owned_client_id(client_id)
            report["entry_submission_attempts" if entry else "exit_submission_attempts"] += 1
            response_received = False
            try:
                result = (adapter.submit_limit_order(facts["pair"], "buy", intent["quantity"],
                          intent["price"], client_id, post_only=False) if entry else
                          adapter.submit_market_order(facts["pair"], "sell", intent["quantity"],
                          client_id, reduce_only=True))
                response_received = True
                observe(client_id, result)
            except Exception as exc:
                failure = _error(exc, "entry_submission" if entry else "exit_submission")
                if response_received:
                    # A malformed response cannot prove the venue rejected a
                    # submitted order. Reconcile its durable ID without retry.
                    failure["kind"] = "submission_unknown"
                report["errors"].append(failure)
                persist(ledger.append_comparison, {**identity, "client_id": client_id,
                        "error_kind": failure["kind"],
                        "exchange_code": failure["exchange_code"]},
                        "submission_error", now())
                if not response_received and isinstance(exc, (DemoExchangeRejected, DemoSafetyError)):
                    persist(ledger.record_order_observation, client_id,
                            {"status": "rejected", "filled_qty": "0", "raw": {},
                             "exchange_updated_ms": now()})
                    return False
                persist(ledger.mark_submission_unknown, client_id, failure["kind"], now())
        elif row.get("exchange_order_id"):
            adapter.authorize_owned_client_id(client_id)
        elif row.get("status") == "rejected":
            return False
        # An old claim or UNKNOWN is lookup-only, even when Binance says absent.
        lookup(client_id)
        return True

    def intent(role, quantity, stamp):
        entry = role == "entry"
        client_id = stable_client_order_id(namespace, identity["signal_id"], facts["pair"],
                                          "buy" if entry else "sell", role)
        return {**identity, "client_id": client_id, "intent_key": role,
                "pair": facts["pair"], "side": "buy" if entry else "sell",
                "order_type": "limit" if entry else "market", "quantity": quantity,
                "price": facts["price"] if entry else None,
                "time_in_force": "GTC" if entry else None, "reduce_only": not entry,
                "signal_ms": stamp, "created_ms": stamp,
                "intent_payload": {"experiment_scope": "manual_smoke_test",
                                   "demo_policy_hash": _hash(policy),
                                   "purpose": "open_close_and_actual_fees", "plan": plan}}

    def snapshot():
        positions = adapter.fetch_positions()
        opens = adapter.fetch_open_orders()
        persist(ledger.append_position_snapshot, {"positions": positions, "openOrders": opens}, now())
        return positions, opens

    try:
        cap = _decimal(max_notional_usdt, positive=True)
        if (not isinstance(namespace, str) or not namespace.startswith("manual_smoke_")
                or getattr(ledger, "environment", None) != "binance_demo"
                or getattr(ledger, "endpoint_host", None) != DEMO_HOST
                or adapter.allow_trading is not True or cap > 75):
            raise DemoSafetyError("isolated manually authorized Demo diagnostic required")
        if (isinstance(entry_wait_seconds, bool) or isinstance(reconciliation_seconds, bool)
                or not 0 <= entry_wait_seconds <= 30 or not 1 <= reconciliation_seconds <= 120):
            raise DemoSafetyError("invalid diagnostic timing bound")
        existing = ledger.runner_orders()
        if any(row.get("strategy_id") != "manual_smoke_test" for row in existing) or len(existing) > 2:
            raise DemoSafetyError("diagnostic namespace contains unrelated orders")
        if adapter.fetch_position_mode()["hedged"] is not False:
            raise DemoSafetyError("diagnostic requires one-way position mode")
        start = now()
        if not existing:
            positions, opens = snapshot()
            if _positions(positions) or opens:
                raise DemoSafetyError("diagnostic requires a flat account without open orders")
            account = adapter.fetch_balance()
            ledger.append_account_snapshot(account, now())
            plan = _select_plan(adapter, cap)
        else:
            entries = [row for row in existing if not row["reduce_only"]]
            if len(entries) != 1:
                raise DemoSafetyError("diagnostic recovery requires exactly one durable entry")
            plan = entries[0]["intent_payload"]["plan"]
        facts.update(plan)
        report["plan"] = plan
        policy = {"version": VERSION, "max_notional_usdt": format(cap, "f"),
                  "entry_wait_seconds": entry_wait_seconds, "reconciliation_seconds": reconciliation_seconds,
                  "plan": plan, "entry_post_only": False, "exit_reduce_only": True}
        identity = {"strategy_id": "manual_smoke_test", "signal_id": namespace + ":open_close",
                    "rule_hash": _hash({"version": VERSION, "purpose": "manual_smoke_test"}),
                    "execution_hash": _hash(policy), "input_fingerprint": _hash(plan)}
        ledger.bind_account({**identity, "policy": policy, "demo_policy_hash": _hash(policy),
                             "experiment_scope": "manual_smoke_test"})
        if not report["persistence_complete"]:
            raise DemoSafetyError("preflight audit persistence failed")
        old_entry = next((row for row in existing if not row["reduce_only"]), None)
        entry_intent = intent("entry", facts["quantity"], old_entry["signal_ms"] if old_entry else start)
        entry_id = entry_intent["client_id"]
        if not submit(entry_intent, entry=True):
            positions, opens = snapshot()
            report["position_flat"] = not _positions(positions)
            report["open_orders_zero"] = not opens
            raise DemoSafetyError("diagnostic entry was definitively rejected")
        entry_deadline = now() + int(entry_wait_seconds * 1000)
        while now() < entry_deadline:
            order = observed.get(entry_id)
            if order and order["status"] in TERMINAL:
                break
            pause(min(1, max(0, (entry_deadline - now()) / 1000)))
            lookup(entry_id)
        # Confirm the remaining entry is terminal before opening the exit. A
        # late fill after the exit would otherwise silently recreate exposure.
        deadline = now() + int(reconciliation_seconds * 1000)
        while now() < deadline:
            order = observed.get(entry_id)
            if order and order["status"] in TERMINAL:
                break
            adapter.authorize_owned_client_id(entry_id)
            try:
                observe(entry_id, adapter.cancel_order(facts["pair"], client_order_id=entry_id))
            except Exception as exc:
                report["errors"].append(_error(exc, "entry_cancellation"))
            lookup(entry_id)
            if not observed.get(entry_id) or observed[entry_id]["status"] not in TERMINAL:
                pause(1)
        order = observed.get(entry_id)
        if order is None or order["status"] not in TERMINAL:
            raise DemoSafetyError("entry outcome remains unresolved; no repeat entry permitted")
        positions, opens = snapshot()
        nonzero = _positions(positions)
        owned_filled = _decimal(order["filled_qty"])
        # Order acknowledgements and positionRisk are independently published.
        # A just-filled entry must not be mistaken for zero owned exposure.
        visibility_deadline = now() + int(reconciliation_seconds * 1000)
        old_exit = next((row for row in existing if row["reduce_only"]), None)
        while owned_filled > 0 and not nonzero and old_exit is None and now() < visibility_deadline:
            pause(1)
            positions, opens = snapshot()
            nonzero = _positions(positions)
        if opens and (old_exit is None or any(
                item.get("client_id") != old_exit["client_id"]
                or item.get("raw", {}).get("reduceOnly") is not True
                for item in opens)):
            raise DemoSafetyError("unresolved account open orders after diagnostic entry")
        if nonzero:
            if (len(nonzero) != 1 or nonzero[0][0].get("pair") != facts["pair"]
                    or nonzero[0][0].get("position_side") != "BOTH"
                    or nonzero[0][1] <= 0 or nonzero[0][1] > owned_filled):
                raise DemoSafetyError("position cannot be attributed to the owned diagnostic entry")
            quantity = format(nonzero[0][1], "f")
            adapter.validate_order(facts["pair"], quantity, market=True)
            if old_exit:
                # Preserve the original frozen exit size, even if its partial
                # execution has already reduced positionRisk's remaining size.
                exit_intent = intent("exit", str(old_exit["quantity"]), old_exit["signal_ms"])
            else:
                exit_intent = intent("exit", quantity, now())
            submit(exit_intent, entry=False)
        else:
            old_exit = next((row for row in existing if row["reduce_only"]), None)
            if old_exit:
                exit_intent = intent("exit", str(old_exit["quantity"]), old_exit["signal_ms"])
                facts[exit_intent["client_id"]] = exit_intent
                lookup(exit_intent["client_id"])
        deadline = now() + int(reconciliation_seconds * 1000)
        while True:
            for client_id in list(observed):
                lookup(client_id)
            positions, opens = snapshot()
            report["position_flat"] = not _positions(positions)
            report["open_orders_zero"] = not opens
            all_terminal = all(item["status"] in TERMINAL for item in observed.values())
            if report["position_flat"] and report["open_orders_zero"] and all_terminal:
                break
            if now() >= deadline:
                break
            pause(1)
        # Execution protection above is complete before any fill/fee telemetry.
        deadline = now() + int(reconciliation_seconds * 1000)
        while True:
            complete = bool(observed)
            for client_id, exchange_order in list(observed.items()):
                try:
                    fills = adapter.fetch_trades(facts["pair"], order_id=exchange_order["order_id"])
                    if len(fills) >= 1000:
                        raise DemoSafetyError("diagnostic fill response may be truncated")
                    seen = set()
                    for fill in fills:
                        if (str(fill["order_id"]) != str(exchange_order["order_id"])
                                or fill["pair"] != facts["pair"] or fill["fill_id"] in seen
                                or fill.get("side") != facts[client_id]["side"]):
                            raise DemoSafetyError("diagnostic fill identity mismatch")
                        seen.add(fill["fill_id"])
                    actual_fills[client_id] = fills
                    for fill in fills:
                        persist(ledger.record_fill, client_id, fill)
                    complete = complete and sum((_decimal(fill["qty"]) for fill in fills), Decimal(0)) == _decimal(exchange_order["filled_qty"])
                except Exception as exc:
                    complete = False
                    report["errors"].append(_error(exc, "actual_fill_reconciliation"))
            report["fill_reconciliation_complete"] = complete
            if complete or now() >= deadline:
                break
            pause(1)
        final_balance = adapter.fetch_balance()
        persist(ledger.append_account_snapshot, final_balance, now())
        projected_fills = ledger.runner_fills()
        projected_orders = ledger.runner_orders()
        report["round_trips"] = derive_round_trips(projected_fills, projected_orders)
        flattened = [fill for fills in actual_fills.values() for fill in fills]
        report.update(summarize_fill_fees(flattened))
        report["success"] = (report["position_flat"] and report["open_orders_zero"]
            and report["fill_reconciliation_complete"] and report["persistence_complete"]
            and report["commission_complete"]
            and len(observed) == 2 and len(report["round_trips"]) == 1
            and report["round_trips"][0].get("fee_complete") is True
            and report["round_trips"][0].get("fill_reconciliation_complete") is True)
    except Exception as exc:
        report["errors"].append(_error(exc, "diagnostic"))
    finally:
        report["orders"] = [item["raw"] for item in observed.values()]
        report["fills"] = [item["raw"] for fills in actual_fills.values() for item in fills]
        report["finished_ms"] = now()
        if facts.get("pair"):
            report["pair"] = facts["pair"]
        if "identity" in locals():
            persist(ledger.append_comparison, {**identity, "report": report}, "manual_smoke_result", now())
    return report

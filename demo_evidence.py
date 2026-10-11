"""All-history Demo evidence; never reads credentials or contacts an exchange."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, localcontext

from demo_execution_service import derive_round_trips

ENVIRONMENT = "binance_demo"
ENDPOINT = "demo-fapi.binance.com"
SCOPE = "selected_candle_strategy_execution_only"
TERMINAL = {"filled", "canceled", "expired", "rejected"}


class DemoEvidenceError(ValueError):
    """Fixed public codes, without raw database, credential or request text."""


def _decimal(value, *, positive=False):
    if isinstance(value, bool):
        raise DemoEvidenceError("invalid_demo_evidence_number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise DemoEvidenceError("invalid_demo_evidence_number") from None
    if not number.is_finite() or (positive and number <= 0):
        raise DemoEvidenceError("invalid_demo_evidence_number")
    return number


def _validate(snapshot):
    orders, fills = snapshot["orders"], snapshot["fills"]
    binding = snapshot.get("binding")
    if (orders or fills) and not binding:
        raise DemoEvidenceError("demo_evidence_binding_missing")
    identity_fields = ("strategy_id", "rule_hash", "execution_hash")
    if binding and any(not isinstance(binding.get(field), str) or not binding[field]
                       for field in (*identity_fields, "demo_policy_hash")):
        raise DemoEvidenceError("demo_evidence_identity_missing")
    by_client = {}
    for row in orders:
        if row["client_id"] in by_client:
            raise DemoEvidenceError("duplicate_demo_evidence_order")
        if row["environment"] != ENVIRONMENT or row["endpoint_host"] != ENDPOINT:
            raise DemoEvidenceError("demo_evidence_environment_mismatch")
        if any(row[field] != binding[field] for field in identity_fields):
            raise DemoEvidenceError("demo_evidence_identity_mismatch")
        if (row.get("intent_payload") or {}).get("demo_policy_hash") != binding.get("demo_policy_hash"):
            raise DemoEvidenceError("demo_evidence_policy_mismatch")
        _decimal(row["quantity"], positive=True)
        filled = _decimal(row["filled_quantity"])
        if filled < 0 or filled > _decimal(row["quantity"]):
            raise DemoEvidenceError("invalid_demo_evidence_filled_quantity")
        by_client[row["client_id"]] = row
    seen = set()
    for fill in fills:
        key = (fill["pair"], str(fill["exchange_order_id"]), str(fill["exchange_trade_id"]))
        if key in seen:
            raise DemoEvidenceError("duplicate_demo_evidence_fill")
        seen.add(key)
        order = by_client.get(fill["client_id"])
        if not order or fill["pair"] != order["pair"] or str(fill["exchange_order_id"]) != str(order["exchange_order_id"]):
            raise DemoEvidenceError("demo_evidence_fill_order_mismatch")
        if fill["environment"] != ENVIRONMENT or fill["virtual_account_key"] != order["virtual_account_key"]:
            raise DemoEvidenceError("demo_evidence_environment_mismatch")
        if fill["side"] != order["side"]:
            raise DemoEvidenceError("demo_evidence_fill_direction_mismatch")
        _decimal(fill["price"], positive=True)
        _decimal(fill["quantity"], positive=True)
    for error in snapshot.get("submissionErrors", []):
        if not binding or any(error[field] != binding[field] for field in identity_fields):
            raise DemoEvidenceError("demo_evidence_identity_mismatch")
        if error["payload"].get("client_id") not in by_client:
            raise DemoEvidenceError("demo_evidence_error_order_mismatch")
    return by_client


def build_demo_evidence(snapshot):
    """Pure summary: missing evidence stays unknown; no profitability verdict."""
    with localcontext() as context:
        context.prec = 60
        return _build(snapshot)


def _build(snapshot):
    orders, fills = snapshot["orders"], snapshot["fills"]
    by_client = _validate(snapshot)
    groups, charges = {}, defaultdict(Decimal)
    known_fees, deviations = 0, []
    for fill in fills:
        order = by_client[fill["client_id"]]
        maker = fill.get("is_maker")
        liquidity = "maker" if maker is True else "taker" if maker is False else "unknown"
        role = "exit" if order["reduce_only"] else "entry"
        symbol = fill["pair"].replace("/", "").replace(":USDT", "")
        group = groups.setdefault((symbol, liquidity, role), {
            "symbol": symbol, "liquidity": liquidity, "role": role, "fillCount": 0,
            "commission": Decimal(0), "quote": Decimal(0), "complete": True})
        group["fillCount"] += 1
        fee, asset = fill.get("commission"), fill.get("commission_asset")
        fee_known = fee is not None and bool(asset) and not fill.get("commission_conflict")
        if fee_known:
            known_fees += 1
            charges[asset] += _decimal(fee)
        raw_quote = (fill.get("raw") or {}).get("quoteQty")
        if fee_known and asset == "USDT" and raw_quote is not None:
            group["commission"] += _decimal(fee)
            group["quote"] += _decimal(raw_quote, positive=True)
        else:
            group["complete"] = False
        reference = (order.get("intent_payload") or {}).get("signal_reference_price")
        if reference is not None:
            reference = _decimal(reference, positive=True)
            direction = Decimal(1) if order["side"] == "buy" else Decimal(-1)
            deviations.append(direction * (_decimal(fill["price"]) / reference - 1) * 10_000)
    rates = []
    for key in sorted(groups):
        group = groups[key]
        complete = group["complete"]
        rates.append({name: group[name] for name in ("symbol", "liquidity", "role", "fillCount")} | {
            "knownCommissionUsdt": format(group["commission"], "f") if complete else None,
            "actualReportedQuoteUsdt": format(group["quote"], "f") if complete else None,
            "effectiveFeeRate": format(group["commission"] / group["quote"], "f") if complete else None})
    trips = derive_round_trips(fills, orders)
    known_net = [trip for trip in trips if trip["actual_net_trading_pnl_usdt"] is not None]
    subtotal = sum((_decimal(trip["actual_net_trading_pnl_usdt"]) for trip in known_net), Decimal(0))
    entries = [order for order in orders if not order["reduce_only"]]
    accepted = [order for order in entries if order.get("exchange_order_id") is not None
                and order["status"] != "rejected"]
    terminal = [order for order in accepted if order["status"] in TERMINAL]
    nonfills = sum(_decimal(order["filled_quantity"]) == 0 for order in terminal)
    rejected_ids = {error["payload"]["client_id"] for error in snapshot.get("submissionErrors", [])
                    if error["payload"].get("error_kind") == "exchange_rejected"}
    exchange_rejected = [order for order in entries if order["status"] == "rejected"
                         and order["client_id"] in rejected_ids]
    evidence = {
        "environment": ENVIRONMENT, "endpoint": ENDPOINT,
        "snapshotAt": snapshot.get("snapshotAt"), "scope": SCOPE,
        "questions": {
            "fees": {"state": "no_actual_fills" if not fills else
                     "actual_fees_observed" if known_fees == len(fills) else "actual_fees_partially_known",
                     "fillCount": len(fills), "knownFeeFillCount": known_fees,
                     "unknownFeeFillCount": len(fills) - known_fees, "rates": rates,
                     "chargedByAsset": [{"asset": asset, "knownCommission": format(amount, "f")}
                                        for asset, amount in sorted(charges.items())]},
            "secondsProfit": {
                "state": "seconds_experiment_not_configured", "secondsExperimentConfigured": False,
                "closedRoundTripCount": len(trips), "knownNetRoundTripCount": len(known_net),
                "unknownNetRoundTripCount": len(trips) - len(known_net),
                "closedNetTradingPnlUsdt": format(subtotal, "f") if trips and len(known_net) == len(trips) else None,
                "knownNetSubtotalUsdt": format(subtotal, "f"), "fundingIncluded": False,
                "liveProfitabilityVerified": False},
            "execution": {
                "state": "demo_execution_observed" if accepted or exchange_rejected or fills else "no_exchange_orders",
                "exchangeAcceptedEntryCount": len(accepted), "terminalAcceptedEntryCount": len(terminal),
                "zeroFillTerminalAcceptedEntryCount": nonfills,
                "acceptedTerminalNonfillRatio": nonfills / len(terminal) if terminal else None,
                "exchangeRejectedEntryCount": len(exchange_rejected),
                "localTerminalUnconfirmedEntryCount": sum(order["status"] in TERMINAL
                    and order not in accepted and order not in exchange_rejected for order in entries),
                "pendingEntryCount": sum(order["status"] not in TERMINAL for order in entries),
                "unknownEntryCount": sum(order["status"] in {"submitting", "unknown"} for order in entries),
                "partialTerminalAcceptedEntryCount": sum(0 < _decimal(order["filled_quantity"]) < _decimal(order["quantity"])
                                                         for order in terminal),
                "signalPriceDeviation": {"fillCount": len(fills), "knownReferenceFillCount": len(deviations),
                    "meanAdverseDeviationBps": format(sum(deviations, Decimal(0)) / len(deviations), "f") if deviations else None,
                    "scope": "demo_fill_vs_live_paper_signal_includes_environment_and_latency"},
                "liveLaunchAllowed": False, "launchDecision": "manual_review_required"}},
        "limitations": [
            "Account rate quotes are separate from actually charged fill commissions.",
            "Demo fills and non-fill ratios do not establish live execution equivalence.",
            "Signal price deviation includes environment differences and latency; it is not measured live slippage.",
            "Actual realized P&L already reflects fills: do not subtract price deviation a second time.",
            "Candle strategies, short holds and five-second polling do not constitute a seconds experiment.",
            "Trading P&L excludes funding; unknown or unconverted commissions never become zero."]}
    return evidence


def read_demo_evidence(data_store, virtual_account_key="demo_default"):
    """Direct consistent read; no DataStore.initialize, DDL, credentials or LIMIT."""
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    with psycopg.connect(data_store.dsn, row_factory=dict_row, autocommit=True,
                         connect_timeout=10) as connection:
        with connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            connection.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(data_store.schema)))
            connection.execute("SET LOCAL statement_timeout TO '30s'")
            namespace = (ENVIRONMENT, virtual_account_key)
            stamp = connection.execute("SELECT transaction_timestamp() AS stamp").fetchone()["stamp"]
            orders = connection.execute("SELECT * FROM demo_orders WHERE environment=%s AND virtual_account_key=%s",
                                        namespace).fetchall()
            fills = connection.execute("SELECT f.*,o.side FROM demo_fills f JOIN demo_orders o "
                "ON (f.environment,f.virtual_account_key,f.client_id)=(o.environment,o.virtual_account_key,o.client_id) "
                "WHERE f.environment=%s AND f.virtual_account_key=%s", namespace).fetchall()
            binding = connection.execute("SELECT payload FROM demo_signal_comparisons WHERE environment=%s "
                "AND virtual_account_key=%s AND stage='binding'", namespace).fetchone()
            errors = connection.execute("SELECT strategy_id,rule_hash,execution_hash,payload FROM demo_signal_comparisons "
                "WHERE environment=%s AND virtual_account_key=%s AND stage='submission_error'", namespace).fetchall()
    return build_demo_evidence({"snapshotAt": stamp.isoformat(), "orders": orders, "fills": fills,
                               "binding": binding["payload"] if binding else None,
                               "submissionErrors": errors})

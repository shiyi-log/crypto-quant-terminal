"""已归档成交与现金流的完整往返成本归属；无 I/O、无交易或验收授权。

调用者负责证明归档来源、账户独占和完整扫描。纯函数不能让自报的完整标志
成为权威事实，也不持久化完成事件；同一 trade_id 的旧证明必须由调用方提供。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Context, Decimal, InvalidOperation, localcontext
import hashlib
import json
import logging
import re

LOGGER = logging.getLogger(__name__)
VERSION = "demo-round-trip-costs-v1"
SCOPE = ("environment", "account_id", "stage_id", "strategy_version_id", "run_id")
HASHES = ("code_hash", "strategy_hash", "execution_hash", "risk_policy_hash", "cost_model_hash")
PAIRS = {"BTC/USDT:USDT", "ETH/USDT:USDT", "TRX/USDT:USDT"}
SAFE = re.compile(r"[A-Za-z0-9_./:@+-]{1,160}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
TERMINAL = {"filled", "canceled", "cancelled", "expired", "rejected"}
INCOME_TYPES = {"COMMISSION", "REALIZED_PNL", "FUNDING_FEE", "TRANSFER", "OTHER_COST"}


@dataclass(frozen=True, slots=True)
class RoundTripCostReport:
    """不可变证明；to_dict 返回独立副本，无法改写历史输入。"""

    canonical_json: str
    sha256: str

    def to_dict(self):
        return json.loads(self.canonical_json)


def _canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        default=lambda x: _number(x) if isinstance(x, Decimal) else None,
    )


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _number(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _decimal(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("decimal_invalid")
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("decimal_invalid") from None
    if (
        not number.is_finite()
        or len(str(value)) > 256
        or len(number.as_tuple().digits) > 96
        or abs(number.as_tuple().exponent) > 192
        or (number and abs(number.adjusted()) > 96)
        or (positive and number <= 0)
    ):
        raise ValueError("decimal_invalid")
    return number


def _stamp(value):
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("timestamp_invalid")
    return value


def _id(value):
    if (
        not isinstance(value, (str, int))
        or isinstance(value, bool)
        or not SAFE.fullmatch(str(value))
    ):
        raise ValueError("identity_invalid")
    return str(value)


def _archived(row, scope):
    if not isinstance(row, dict) or any(row.get(key) != scope[key] for key in SCOPE):
        raise ValueError("identity_mismatch")
    if (
        not isinstance(row.get("source_ref"), str)
        or not SAFE.fullmatch(row["source_ref"])
        or not isinstance(row.get("source_hash"), str)
        or not SHA.fullmatch(row["source_hash"])
    ):
        raise ValueError("source_reference_invalid")


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def _sort_fill(row):
    if "_ordered_sequence" in row:
        return row["timestamp_ms"], 0, row["_ordered_sequence"]
    identity = row["exchange_trade_id"]
    return (
        (row["timestamp_ms"], 0, int(identity))
        if identity.isdigit()
        else (row["timestamp_ms"], 1, identity)
    )


def _base(source):
    return {
        "version": VERSION,
        **{key: source.get(key) for key in SCOPE},
        "as_of_ms": source.get("as_of_ms"),
        "complete": False,
        "reason_codes": [],
        "trips": [],
        "open_positions": [],
        "risk_events": [],
        "production_wired": False,
        "acceptance_eligible": False,
    }


def _report(value):
    value["reason_codes"] = list(dict.fromkeys(value["reason_codes"]))
    canonical = _canonical(value)
    return RoundTripCostReport(canonical, hashlib.sha256(canonical.encode()).hexdigest())


def _structural(source, scope):
    """验证后只在本次局部对象里增加 Decimal，不修改归档原文。"""
    for key in SCOPE:
        _id(scope[key])
    if scope["environment"] != "binance_demo":
        raise ValueError("environment_unsupported")
    if any(
        not isinstance(source.get(key), str) or not SHA.fullmatch(source[key]) for key in HASHES
    ):
        raise ValueError("frozen_identity_missing")
    start, end, now = (_stamp(source.get(key)) for key in ("start_ms", "end_ms", "as_of_ms"))
    if not start < end <= now:
        raise ValueError("coverage_window_invalid")
    opening = source.get("opening")
    _archived(opening, scope)
    if (
        opening.get("timestamp_ms") != start
        or opening.get("flat") is not True
        or opening.get("normal_orders_complete") is not True
        or opening.get("conditional_orders_complete") is not True
        or type(opening.get("position_count")) is not int
        or opening["position_count"] != 0
        or type(opening.get("open_order_count")) is not int
        or opening["open_order_count"] != 0
    ):
        raise ValueError("opening_flat_unverified")
    coverage = source.get("coverage")
    _archived(coverage, scope)
    if (
        coverage.get("source") != "archived_exchange_reconciliation"
        or coverage.get("start_ms") != start
        or coverage.get("end_ms") != end
    ):
        raise ValueError("coverage_window_invalid")
    reasons = []
    for flag, reason in (
        ("all_income_types", "income_coverage_incomplete"),
        ("income_complete", "income_coverage_incomplete"),
        ("fills_complete", "fills_coverage_incomplete"),
        ("orders_complete", "orders_coverage_incomplete"),
    ):
        if coverage.get(flag) is not True:
            reasons.append(reason)
    if source.get("commission_rebate_policy") not in {
        "reject_negative",
        "verified_exchange_rebates",
    }:
        raise ValueError("commission_policy_missing")
    for key in ("fills", "orders", "income_rows", "fee_conversions"):
        if not isinstance(source.get(key), list) or len(source[key]) > 10000:
            raise ValueError("archive_rows_invalid")
    fills, orders, incomes, conversions, fill_ids = [], {}, [], {}, set()
    exchange_orders = set()
    for raw in source["fills"]:
        _archived(raw, scope)
        row = dict(raw)
        pair = row.get("pair")
        if pair not in PAIRS or row.get("side") not in {"buy", "sell"}:
            raise ValueError("fill_semantics_invalid")
        for key in ("exchange_trade_id", "exchange_order_id", "client_id"):
            row[key] = _id(row.get(key))
        identity = (pair, row["exchange_trade_id"])
        if identity in fill_ids:
            raise ValueError("duplicate_exchange_trade_id")
        fill_ids.add(identity)
        row["timestamp_ms"] = _stamp(row.get("timestamp_ms"))
        if not start < row["timestamp_ms"] <= end:
            raise ValueError("fill_outside_coverage")
        row["_q"], row["_price"] = (
            _decimal(row.get("quantity"), positive=True),
            _decimal(row.get("price"), positive=True),
        )
        fills.append(row)
    for raw in source["orders"]:
        _archived(raw, scope)
        row = dict(raw)
        row["client_id"], row["exchange_order_id"] = (
            _id(row.get("client_id")),
            _id(row.get("exchange_order_id")),
        )
        if row["client_id"] in orders:
            raise ValueError("duplicate_order_identity")
        exchange_identity = (row.get("pair"), row["exchange_order_id"])
        if exchange_identity in exchange_orders:
            raise ValueError("exchange_order_identity_reused")
        exchange_orders.add(exchange_identity)
        if row.get("pair") not in PAIRS or row.get("side") not in {"buy", "sell"}:
            raise ValueError("order_semantics_invalid")
        row["_q"], row["_filled"] = (
            _decimal(row.get("quantity"), positive=True),
            _decimal(row.get("filled_quantity")),
        )
        if not 0 <= row["_filled"] <= row["_q"]:
            raise ValueError("order_fill_quantity_mismatch")
        if _stamp(row.get("updated_ms")) > end:
            raise ValueError("order_outside_coverage")
        orders[row["client_id"]] = row
    seen_income = set()
    for raw in source["income_rows"]:
        _archived(raw, scope)
        row = dict(raw)
        row["transaction_id"] = _id(row.get("transaction_id"))
        kind = row.get("income_type")
        if kind not in INCOME_TYPES:
            raise ValueError("income_type_unclassified")
        identity = (kind, row["transaction_id"])
        if identity in seen_income:
            raise ValueError("duplicate_income_identity")
        seen_income.add(identity)
        row["timestamp_ms"] = _stamp(row.get("timestamp_ms"))
        if not start < row["timestamp_ms"] <= end:
            raise ValueError("income_outside_coverage")
        row["_amount"] = _decimal(row.get("amount"))
        if kind != "TRANSFER" and row.get("pair") not in PAIRS:
            raise ValueError("income_pair_invalid")
        if kind in {"COMMISSION", "REALIZED_PNL", "OTHER_COST"}:
            row["exchange_trade_id"] = _id(row.get("exchange_trade_id"))
        if not isinstance(row.get("asset"), str) or not re.fullmatch(
            "[A-Z0-9]{1,16}", row["asset"]
        ):
            raise ValueError("income_asset_invalid")
        if kind in {"REALIZED_PNL", "FUNDING_FEE", "OTHER_COST"} and row["asset"] != "USDT":
            raise ValueError("income_conversion_unknown")
        if kind == "OTHER_COST" and row["_amount"] > 0:
            raise ValueError("other_cost_sign_invalid")
        incomes.append(row)
    for raw in source["fee_conversions"]:
        _archived(raw, scope)
        row = dict(raw)
        identity = (row.get("pair"), _id(row.get("exchange_trade_id")))
        if identity in conversions:
            raise ValueError("duplicate_fee_conversion")
        row["_amount"], row["_usdt"] = _decimal(row.get("amount")), _decimal(row.get("usdt_amount"))
        if row.get("verified") is not True:
            raise ValueError("fee_conversion_unverified")
        conversions[identity] = row
    return fills, orders, incomes, conversions, reasons


def _groups(fills, orders):
    active, groups, assigned = {}, [], set()
    same_time = {}
    for fill in fills:
        same_time.setdefault((fill["pair"], fill["timestamp_ms"]), []).append(fill)
    for rows in same_time.values():
        if len(rows) < 2:
            continue
        stream = rows[0].get("event_stream_id")
        if (
            not isinstance(stream, str)
            or not SAFE.fullmatch(stream)
            or any(
                row.get("event_stream_id") != stream
                or type(row.get("event_sequence")) is not int
                or row["event_sequence"] <= 0
                for row in rows
            )
            or len({row["event_sequence"] for row in rows}) != len(rows)
        ):
            raise ValueError("fill_ordering_unknown")
        for row in rows:
            row["_ordered_sequence"] = row["event_sequence"]
    for fill in sorted(fills, key=_sort_fill):
        pair, signed = fill["pair"], fill["_q"] * (1 if fill["side"] == "buy" else -1)
        group = active.setdefault(pair, {"pair": pair, "signed": Decimal(0), "fills": []})
        if group["signed"] and group["signed"] * signed < 0:
            if orders[fill["client_id"]].get("reduce_only") is not True:
                raise ValueError("exit_not_reduce_only")
        else:
            if orders[fill["client_id"]].get("reduce_only") is not False:
                raise ValueError("entry_reduce_only_invalid")
            if fill.get("realized_pnl") is not None and _decimal(fill["realized_pnl"]) != 0:
                raise ValueError("entry_realized_pnl_conflict")
        if group["signed"] and group["signed"] * signed < 0 and abs(signed) > abs(group["signed"]):
            raise ValueError("position_reversal_crossing")
        group["signed"] += signed
        group["fills"].append(fill)
        if not group["signed"]:
            clients = {f["client_id"] for f in group["fills"]}
            if clients & assigned:
                raise ValueError("order_spans_round_trips")
            assigned.update(clients)
            groups.append(group)
            del active[pair]
    if any({f["client_id"] for f in g["fills"]} & assigned for g in active.values()):
        raise ValueError("order_spans_round_trips")
    return groups, active


def _check_orders(fills, orders):
    totals, latest = {}, {}
    for row in fills:
        client = row["client_id"]
        order = orders.get(client)
        if order is None:
            raise ValueError("fill_order_missing")
        if any(order.get(key) != row.get(key) for key in ("pair", "side", "exchange_order_id")):
            raise ValueError("fill_order_identity_mismatch")
        if order.get("position_side") != "BOTH" or type(order.get("reduce_only")) is not bool:
            raise ValueError("order_position_mode_unverified")
        totals[client] = totals.get(client, Decimal(0)) + row["_q"]
        latest[client] = max(latest.get(client, 0), row["timestamp_ms"])
    for client, order in orders.items():
        if totals.get(client, Decimal(0)) != order["_filled"]:
            raise ValueError("order_fill_quantity_mismatch")
        if order["updated_ms"] < latest.get(client, 0):
            raise ValueError("order_fact_precedes_fill")
        if order.get("status") not in TERMINAL:
            raise ValueError("order_not_terminal")
        if order["status"] == "filled" and order["_filled"] != order["_q"]:
            raise ValueError("filled_order_quantity_mismatch")
        if order["status"] == "rejected" and order["_filled"] != 0:
            raise ValueError("rejected_order_has_fills")


def _clean(row):
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _funding_group(row, groups, active, scope):
    pair, timestamp = row["pair"], row["timestamp_ms"]
    possible = [
        g
        for g in groups
        if g["pair"] == pair
        and g["fills"][0]["timestamp_ms"] <= timestamp <= g["fills"][-1]["timestamp_ms"]
    ]
    open_group = active.get(pair)
    if open_group is not None and open_group["fills"][0]["timestamp_ms"] <= timestamp:
        possible.append(open_group)
    boundaries = any(any(f["timestamp_ms"] == timestamp for f in g["fills"]) for g in possible)
    if len(possible) != 1 or boundaries:
        proof = row.get("position_evidence")
        if proof is None:
            return (
                None,
                "funding_boundary_ordering_unknown"
                if possible
                else "funding_without_owned_position",
            )
        _archived(proof, scope)
        if (
            proof.get("ordering_verified") is not True
            or proof.get("timestamp_ms") != timestamp
            or proof.get("pair") != pair
            or proof.get("position_side") != "BOTH"
            or proof.get("owned_position") is not True
            or not isinstance(proof.get("included_fill_ids"), list)
            or not isinstance(row.get("event_stream_id"), str)
            or not SAFE.fullmatch(row["event_stream_id"])
            or proof.get("event_stream_id") != row["event_stream_id"]
            or type(row.get("event_sequence")) is not int
            or row["event_sequence"] <= 0
            or proof.get("event_sequence") != row["event_sequence"]
        ):
            return None, "funding_position_evidence_invalid"
        included = proof["included_fill_ids"]
        matches = []
        for group in possible:
            same_time = [f for f in group["fills"] if f["timestamp_ms"] == timestamp]
            if any(
                f.get("event_stream_id") != row["event_stream_id"]
                or type(f.get("event_sequence")) is not int
                or f["event_sequence"] <= 0
                or f["event_sequence"] == row["event_sequence"]
                for f in same_time
            ):
                return None, "funding_position_evidence_invalid"
            expected = [
                f
                for f in group["fills"]
                if f["timestamp_ms"] < timestamp
                or (f["timestamp_ms"] == timestamp and f["event_sequence"] < row["event_sequence"])
            ]
            if (
                included
                and included == [f["exchange_trade_id"] for f in expected]
                and str(proof.get("fill_watermark")) == included[-1]
            ):
                matches.append(group)
        if len(matches) != 1:
            return None, "funding_position_evidence_invalid"
        chosen = matches[0]
        known = [f for f in chosen["fills"] if f["exchange_trade_id"] in included]
        signed = sum((f["_q"] * (1 if f["side"] == "buy" else -1) for f in known), Decimal(0))
        if (
            not signed
            or _decimal(proof.get("signed_quantity")) != signed
            or any(f["timestamp_ms"] > timestamp for f in known)
        ):
            return None, "funding_position_evidence_invalid"
        return chosen, None
    return possible[0], None


def reconcile_round_trip_costs(evidence, *, previous_completions=None):
    """归属实际现金流，输出可供持久层复核的 risk/acceptance 证明。

    previous_completions 为不可变完成账中的 trade_id→proof_sha256。身份不含成本，
    成本补记改变证明只产生 correction_required，不产生第二笔三连亏事件。
    """
    source = evidence if isinstance(evidence, dict) else {}
    output = _base(source)
    try:
        scope = {key: source.get(key) for key in SCOPE}
        if previous_completions is None:
            previous_completions = {}
        if not isinstance(previous_completions, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) or not SHA.fullmatch(value)
            for key, value in previous_completions.items()
        ):
            raise ValueError("previous_completion_invalid")
        with localcontext(Context(prec=1024)):
            fills, orders, incomes, conversions, common_reasons = _structural(source, scope)
            _check_orders(fills, orders)
            groups, active = _groups(fills, orders)
            consumed, by_fill = set(), {(f["pair"], f["exchange_trade_id"]): f for f in fills}
            funding_map = {id(g): [] for g in groups + list(active.values())}
            for index, row in enumerate(incomes):
                kind = row["income_type"]
                if kind == "TRANSFER":
                    consumed.add(index)
                elif kind == "FUNDING_FEE":
                    group, error = _funding_group(row, groups, active, scope)
                    if error:
                        common_reasons.append(error)
                    else:
                        funding_map[id(group)].append(row)
                        consumed.add(index)
                else:
                    if (row["pair"], row["exchange_trade_id"]) not in by_fill:
                        common_reasons.append("income_without_owned_fill")
                    elif (
                        kind in {"COMMISSION", "REALIZED_PNL"}
                        and row["timestamp_ms"]
                        < by_fill[(row["pair"], row["exchange_trade_id"])]["timestamp_ms"]
                    ):
                        common_reasons.append("income_precedes_linked_fill")
                    else:
                        consumed.add(index)
            for group in groups:
                reasons = list(common_reasons)
                group_fills = group["fills"]
                first, last = group_fills[0], group_fills[-1]
                keys = {(f["pair"], f["exchange_trade_id"]) for f in group_fills}
                linked = [
                    r
                    for r in incomes
                    if r["income_type"] in {"COMMISSION", "REALIZED_PNL", "OTHER_COST"}
                    and (r["pair"], r["exchange_trade_id"]) in keys
                ]
                funding = funding_map[id(group)]
                gross, commission, other = Decimal(0), Decimal(0), Decimal(0)
                for fill in group_fills:
                    raw_pnl, raw_fee = fill.get("realized_pnl"), fill.get("commission")
                    pnl = None if raw_pnl is None else _decimal(raw_pnl)
                    fee = None if raw_fee is None else _decimal(raw_fee)
                    if pnl is None:
                        reasons.append("realized_pnl_missing")
                    else:
                        gross += pnl
                    if fill.get("realized_pnl_conflict") is not False:
                        reasons.append("realized_pnl_conflict")
                    if fill.get("commission_conflict") is not False:
                        reasons.append("commission_conflict")
                    if fee is None or fill.get("commission_asset") is None:
                        reasons.append("commission_missing")
                    elif fee < 0 and (
                        source["commission_rebate_policy"] != "verified_exchange_rebates"
                        or fill.get("commission_rebate_verified") is not True
                    ):
                        reasons.append("commission_rebate_unverified")
                    rows = [
                        r for r in linked if r["exchange_trade_id"] == fill["exchange_trade_id"]
                    ]
                    fees = [r for r in rows if r["income_type"] == "COMMISSION"]
                    realized = [r for r in rows if r["income_type"] == "REALIZED_PNL"]
                    if fee is not None and (
                        sum((r["_amount"] for r in fees), Decimal(0)) != -fee
                        or any(r["asset"] != fill.get("commission_asset") for r in fees)
                        or (fee != 0 and not fees)
                    ):
                        reasons.append("commission_income_mismatch")
                    if pnl is not None and sum((r["_amount"] for r in realized), Decimal(0)) != pnl:
                        reasons.append("realized_income_mismatch")
                    if fee is not None and fill.get("commission_asset") == "USDT":
                        commission += fee
                    elif fee is not None and fill.get("commission_asset") is not None:
                        converted = conversions.get((fill["pair"], fill["exchange_trade_id"]))
                        if converted is None:
                            reasons.append("fee_conversion_missing")
                        elif (
                            converted.get("asset") != fill["commission_asset"]
                            or converted["_amount"] != fee
                            or converted.get("timestamp_ms") != fill["timestamp_ms"]
                            or ((fee < 0) != (converted["_usdt"] < 0))
                            or (fee != 0 and converted["_usdt"] == 0)
                        ):
                            reasons.append("fee_conversion_mismatch")
                        else:
                            commission += converted["_usdt"]
                    other += sum(
                        (r["_amount"] for r in rows if r["income_type"] == "OTHER_COST"), Decimal(0)
                    )
                funding_value = sum((r["_amount"] for r in funding), Decimal(0))
                trade_id = "roundtrip:" + _digest(
                    {
                        **scope,
                        "pair": group["pair"],
                        "exchange_trade_ids": [f["exchange_trade_id"] for f in group_fills],
                    }
                )
                proof = {
                    **scope,
                    **{key: source[key] for key in HASHES},
                    "fills": [_clean(f) for f in group_fills],
                    "orders": [
                        _clean(orders[c]) for c in sorted({f["client_id"] for f in group_fills})
                    ],
                    "income_rows": sorted(
                        [_clean(r) for r in linked + funding],
                        key=lambda r: (r["income_type"], r["transaction_id"]),
                    ),
                    "fee_conversions": [
                        _clean(conversions[k]) for k in sorted(keys) if k in conversions
                    ],
                    # 完整窗口扩大或无关入金不会改写本笔经济事实证明。
                    # 权威原始扫描引用另行保留，纯函数不能认证其真实性。
                    "coverage": {
                        "start_ms": first["timestamp_ms"],
                        "end_ms": last["timestamp_ms"],
                        **{
                            key: source["coverage"].get(key)
                            for key in (
                                "source",
                                "all_income_types",
                                "income_complete",
                                "fills_complete",
                                "orders_complete",
                            )
                        },
                    },
                    "commission_rebate_policy": source["commission_rebate_policy"],
                }
                proof_sha = _digest(proof)
                previous = previous_completions.get(trade_id)
                already = previous == proof_sha
                if previous is not None and not already:
                    reasons.append("completion_correction_required")
                reasons = list(dict.fromkeys(reasons))
                complete = not reasons
                net = gross - commission + funding_value + other if complete else None
                funding_complete = not any(
                    code.startswith(("income_", "funding_")) for code in reasons
                )
                item = {
                    **scope,
                    "trade_id": trade_id,
                    "pair": group["pair"],
                    "opened_ms": first["timestamp_ms"],
                    "closed_ms": last["timestamp_ms"],
                    "opened_at": _iso(first["timestamp_ms"]),
                    "closed_at": _iso(last["timestamp_ms"]),
                    "closed": True,
                    "complete": complete,
                    "reason_codes": reasons,
                    "exchange_trade_ids": [f["exchange_trade_id"] for f in group_fills],
                    "actual_realized_pnl_usdt": None
                    if "realized_pnl_missing" in reasons
                    else _number(gross),
                    "actual_commission_usdt": None
                    if any(code.startswith(("commission_", "fee_")) for code in reasons)
                    else _number(commission),
                    "actual_funding_usdt": _number(funding_value) if funding_complete else None,
                    "actual_other_cashflow_usdt": _number(other),
                    "net_pnl_usdt": _number(net) if net is not None else None,
                    "fill_reconciliation_complete": True,
                    "costs_included": complete,
                    "funding_complete": funding_complete,
                    "proof_sha256": proof_sha,
                    "coverage_source_ref": source["coverage"]["source_ref"],
                    "coverage_source_hash": source["coverage"]["source_hash"],
                    "event_id": "round-trip-closed:" + trade_id.split(":")[1],
                    "already_recorded": already,
                    "production_wired": False,
                    "acceptance_eligible": False,
                    "risk_event": None,
                }
                if complete and not already:
                    item["risk_event"] = {
                        "timestamp_ms": last["timestamp_ms"],
                        "net_pnl_usdt": item["net_pnl_usdt"],
                        "trade_id": trade_id,
                        "event_id": item["event_id"],
                    }
                output["trips"].append(item)
                output["reason_codes"].extend(reasons)
            for group in active.values():
                output["open_positions"].append(
                    {
                        "pair": group["pair"],
                        "signed_quantity": _number(group["signed"]),
                        "pending_funding_usdt": _number(
                            sum((row["_amount"] for row in funding_map[id(group)]), Decimal(0))
                        ),
                        "funding_transaction_ids": [
                            row["transaction_id"] for row in funding_map[id(group)]
                        ],
                    }
                )
            if len(consumed) != len(incomes):
                output["reason_codes"].extend(common_reasons or ["income_attribution_incomplete"])
            output["risk_events"] = [
                g["risk_event"] for g in output["trips"] if g["risk_event"] is not None
            ]
            output["reason_codes"].extend(common_reasons)
            output["complete"] = not output["reason_codes"]
    except (ValueError, KeyError, TypeError, ArithmeticError, OverflowError) as error:
        reason = (
            str(error)
            if isinstance(error, ValueError) and SAFE.fullmatch(str(error))
            else "round_trip_evidence_invalid"
        )
        output["reason_codes"].append(reason)
        output["risk_events"] = []
        output["complete"] = False
        # 前一笔已算出后，后一笔坏事实仍使本次整组归档不可用。
        for item in output["trips"]:
            item["complete"] = False
            item["costs_included"] = False
            item["net_pnl_usdt"] = None
            item["risk_event"] = None
            if reason not in item["reason_codes"]:
                item["reason_codes"].append(reason)
    LOGGER.info(
        "Demo完整往返成本归并 complete=%s trips=%s risk_events=%s reason_codes=%s；未接生产授权",
        output["complete"],
        len(output["trips"]),
        len(output["risk_events"]),
        ",".join(output["reason_codes"]),
    )
    return _report(output)

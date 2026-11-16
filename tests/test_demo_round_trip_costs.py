"""完整往返成本证据：真实成交与完整资金流水，不重复扣滑点。"""

import copy
from dataclasses import FrozenInstanceError
from decimal import localcontext
import importlib
import importlib.util

import pytest

BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"
SCOPE = {
    "environment": "binance_demo",
    "account_id": "account-a",
    "stage_id": "stage-1000",
    "strategy_version_id": "strategy-v1",
    "run_id": "run-1",
}


def archive(**values):
    return {**SCOPE, "source_ref": "archive/fact/1", "source_hash": "a" * 64, **values}


def fill(trade, client, side, timestamp, *, quantity="2", pnl="0", fee="0.1", **changes):
    return archive(
        exchange_trade_id=trade,
        exchange_order_id="order-" + client,
        client_id=client,
        pair=BTC,
        side=side,
        quantity=quantity,
        price="100",
        timestamp_ms=timestamp,
        realized_pnl=pnl,
        commission=fee,
        commission_asset="USDT",
        commission_conflict=False,
        realized_pnl_conflict=False,
        **changes,
    )


def order(client, *, pair=BTC, side="buy", quantity="2", filled="2", updated=3000):
    return archive(
        client_id=client,
        exchange_order_id="order-" + client,
        pair=pair,
        side=side,
        quantity=quantity,
        filled_quantity=filled,
        status="filled",
        updated_ms=updated,
        reduce_only=side == "sell",
        position_side="BOTH",
    )


def income(kind, transaction, timestamp, amount, *, trade=None, pair=BTC, asset="USDT", **changes):
    return archive(
        income_type=kind,
        transaction_id=transaction,
        timestamp_ms=timestamp,
        amount=amount,
        asset=asset,
        pair=pair,
        exchange_trade_id=trade,
        **changes,
    )


def evidence():
    return {
        **SCOPE,
        "code_hash": "1" * 64,
        "strategy_hash": "2" * 64,
        "execution_hash": "3" * 64,
        "risk_policy_hash": "4" * 64,
        "cost_model_hash": "5" * 64,
        "start_ms": 0,
        "end_ms": 4000,
        "as_of_ms": 4000,
        "commission_rebate_policy": "reject_negative",
        "opening": archive(
            timestamp_ms=0,
            flat=True,
            normal_orders_complete=True,
            conditional_orders_complete=True,
            position_count=0,
            open_order_count=0,
        ),
        "coverage": archive(
            source="archived_exchange_reconciliation",
            start_ms=0,
            end_ms=4000,
            all_income_types=True,
            income_complete=True,
            fills_complete=True,
            orders_complete=True,
        ),
        "fills": [
            fill("1", "entry", "buy", 1000),
            fill("2", "exit", "sell", 3000, pnl="10", fee="0.2"),
        ],
        "orders": [order("entry"), order("exit", side="sell")],
        "income_rows": [
            income("COMMISSION", "c1", 1000, "-0.1", trade="1"),
            income("REALIZED_PNL", "p2", 3000, "10", trade="2"),
            income("COMMISSION", "c2", 3000, "-0.2", trade="2"),
            income("FUNDING_FEE", "f1", 2000, "-0.4"),
        ],
        "fee_conversions": [],
    }


@pytest.fixture
def module():
    assert importlib.util.find_spec("demo_round_trip_costs"), "actual roundtrip cost module missing"
    return importlib.import_module("demo_round_trip_costs")


def result(module, data=None, previous=None):
    return module.reconcile_round_trip_costs(
        data or evidence(), previous_completions=previous
    ).to_dict()


def trip(module, data=None):
    return result(module, data)["trips"][0]


def test_complete_trade_uses_actual_signed_funding_and_fees_without_deducting_slippage(module):
    data = evidence()
    data["modeled_slippage_usdt"] = "999"
    item = trip(module, data)
    assert item["complete"] is True
    assert item["actual_realized_pnl_usdt"] == "10"
    assert item["actual_commission_usdt"] == "0.3"
    assert item["actual_funding_usdt"] == "-0.4"
    assert item["net_pnl_usdt"] == "9.3"
    assert item["costs_included"] is True and item["funding_complete"] is True
    assert item["fill_reconciliation_complete"] is True and item["closed"] is True
    assert item["risk_event"]["timestamp_ms"] == 3000
    assert item["risk_event"]["net_pnl_usdt"] == "9.3"
    assert item["risk_event"]["trade_id"] == item["trade_id"]
    assert item["production_wired"] is False and item["acceptance_eligible"] is False


def test_result_is_deeply_immutable_and_source_is_not_mutated(module):
    data = evidence()
    before = copy.deepcopy(data)
    frozen = module.reconcile_round_trip_costs(data)
    with pytest.raises(FrozenInstanceError):
        frozen.sha256 = "changed"
    changed = frozen.to_dict()
    changed["trips"][0]["net_pnl_usdt"] = "999"
    assert frozen.to_dict()["trips"][0]["net_pnl_usdt"] == "9.3"
    assert data == before


def test_fill_sorting_and_split_entry_exit_count_one_complete_roundtrip(module):
    data = evidence()
    data["fills"] = [
        fill("4", "exit", "sell", 3000, quantity="1", pnl="5", fee="0.1"),
        fill("2", "entry", "buy", 1100, quantity="1", fee="0.05"),
        fill("1", "entry", "buy", 1000, quantity="1", fee="0.05"),
        fill("3", "exit", "sell", 2900, quantity="1", pnl="5", fee="0.1"),
    ]
    data["income_rows"] = [
        income(
            "COMMISSION",
            "c" + row["exchange_trade_id"],
            row["timestamp_ms"],
            "-" + row["commission"],
            trade=row["exchange_trade_id"],
        )
        for row in data["fills"]
    ]
    data["income_rows"] += [
        income("REALIZED_PNL", "p3", 2900, "5", trade="3"),
        income("REALIZED_PNL", "p4", 3000, "5", trade="4"),
    ]
    out = result(module, data)
    assert len(out["trips"]) == 1 and out["trips"][0]["complete"] is True
    assert out["trips"][0]["exchange_trade_ids"] == ["1", "2", "3", "4"]
    assert out["trips"][0]["net_pnl_usdt"] == "9.7"


def test_partial_position_is_not_complete_or_counted_as_loss(module):
    data = evidence()
    data["fills"] = data["fills"][:1]
    data["orders"] = data["orders"][:1]
    data["income_rows"] = data["income_rows"][:1]
    out = result(module, data)
    assert out["trips"] == []
    assert out["open_positions"][0]["signed_quantity"] == "2"
    assert out["risk_events"] == []


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("commission", None, "commission_missing"),
        ("commission_asset", None, "commission_missing"),
        ("realized_pnl", None, "realized_pnl_missing"),
        ("commission_conflict", True, "commission_conflict"),
        ("realized_pnl_conflict", True, "realized_pnl_conflict"),
        ("commission", "-0.1", "commission_rebate_unverified"),
    ],
)
def test_unknown_or_conflicting_costs_never_create_complete_risk_event(
    module, field, value, reason
):
    data = evidence()
    data["fills"][0][field] = value
    item = trip(module, data)
    assert item["complete"] is False and item["net_pnl_usdt"] is None
    assert reason in item["reason_codes"] and item["risk_event"] is None


def test_missing_funding_scan_is_unknown_not_zero(module):
    data = evidence()
    data["coverage"]["income_complete"] = False
    item = trip(module, data)
    assert item["complete"] is False and item["funding_complete"] is False
    assert "income_coverage_incomplete" in item["reason_codes"]
    assert item["net_pnl_usdt"] is None


def test_verified_complete_empty_funding_scan_proves_zero(module):
    data = evidence()
    data["income_rows"] = data["income_rows"][:3]
    item = trip(module, data)
    assert item["complete"] is True and item["actual_funding_usdt"] == "0"
    assert item["net_pnl_usdt"] == "9.7"


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda d: d["fills"].append(copy.deepcopy(d["fills"][0])), "duplicate_exchange_trade_id"),
        (lambda d: d["orders"].pop(), "fill_order_missing"),
        (lambda d: d["orders"][0].update(filled_quantity="1"), "order_fill_quantity_mismatch"),
        (lambda d: d["orders"][0].update(status="open"), "order_not_terminal"),
        (lambda d: d["fills"][0].update(account_id="other"), "identity_mismatch"),
        (lambda d: d["coverage"].update(stage_id="other"), "identity_mismatch"),
        (lambda d: d["fills"][0].update(source_hash="bad"), "source_reference_invalid"),
        (
            lambda d: d["income_rows"].append(copy.deepcopy(d["income_rows"][0])),
            "duplicate_income_identity",
        ),
        (lambda d: d["income_rows"][0].update(amount="-0.11"), "commission_income_mismatch"),
        (lambda d: d["income_rows"][1].update(amount="9"), "realized_income_mismatch"),
        (lambda d: d["income_rows"][3].update(pair=ETH), "funding_without_owned_position"),
    ],
)
def test_inconsistent_archive_facts_block_every_risk_event(module, mutate, reason):
    data = evidence()
    mutate(data)
    out = result(module, data)
    assert out["complete"] is False and out["risk_events"] == []
    assert reason in out["reason_codes"]


def test_quantity_crossing_position_cannot_be_split_into_fabricated_roundtrips(module):
    data = evidence()
    data["fills"][1]["quantity"] = "3"
    data["orders"][1].update(quantity="3", filled_quantity="3")
    out = result(module, data)
    assert out["risk_events"] == [] and "position_reversal_crossing" in out["reason_codes"]


@pytest.mark.parametrize("timestamp", [1000, 3000])
def test_funding_at_open_or_close_millisecond_is_ambiguous_without_position_ordering(
    module, timestamp
):
    data = evidence()
    data["income_rows"][3]["timestamp_ms"] = timestamp
    item = trip(module, data)
    assert item["complete"] is False and item["funding_complete"] is False
    assert "funding_boundary_ordering_unknown" in item["reason_codes"]


def test_archived_position_at_funding_time_can_resolve_boundary(module):
    data = evidence()
    funding = data["income_rows"][3]
    funding["timestamp_ms"] = 1000
    data["fills"][0].update(event_stream_id="stream-a", event_sequence=1)
    funding.update(event_stream_id="stream-a", event_sequence=2)
    funding["position_evidence"] = archive(
        timestamp_ms=1000,
        fill_watermark="1",
        included_fill_ids=["1"],
        signed_quantity="2",
        ordering_verified=True,
        event_stream_id="stream-a",
        event_sequence=2,
        pair=BTC,
        position_side="BOTH",
        owned_position=True,
    )
    assert trip(module, data)["net_pnl_usdt"] == "9.3"


def test_other_confirmed_cost_uses_signed_actual_amount(module):
    data = evidence()
    data["income_rows"].append(income("OTHER_COST", "cost1", 2001, "-0.5", trade="2"))
    item = trip(module, data)
    assert item["complete"] is True and item["actual_other_cashflow_usdt"] == "-0.5"
    assert item["net_pnl_usdt"] == "8.8"


def test_external_deposit_does_not_become_trade_profit(module):
    data = evidence()
    data["income_rows"].append(income("TRANSFER", "deposit", 2000, "100", pair=None))
    assert trip(module, data)["net_pnl_usdt"] == "9.3"


def test_verified_non_usdt_fee_requires_exact_conversion_reference(module):
    data = evidence()
    data["fills"][0].update(commission="0.001", commission_asset="BNB")
    data["income_rows"][0].update(amount="-0.001", asset="BNB")
    assert "fee_conversion_missing" in trip(module, data)["reason_codes"]
    data["fee_conversions"] = [
        archive(
            pair=BTC,
            exchange_trade_id="1",
            asset="BNB",
            amount="0.001",
            usdt_amount="0.6",
            timestamp_ms=1000,
            verified=True,
        )
    ]
    item = trip(module, data)
    assert item["complete"] is True and item["net_pnl_usdt"] == "8.8"


def test_negative_commission_requires_frozen_policy_and_verified_exchange_rebate(module):
    data = evidence()
    data["commission_rebate_policy"] = "verified_exchange_rebates"
    data["fills"][0].update(commission="-0.1", commission_rebate_verified=True)
    data["income_rows"][0]["amount"] = "0.1"
    assert trip(module, data)["net_pnl_usdt"] == "9.5"


def test_exact_retry_is_not_second_loss_and_cost_correction_keeps_trade_identity(module):
    data = evidence()
    first = trip(module, data)
    prior = {first["trade_id"]: first["proof_sha256"]}
    repeated = result(module, data, prior)["trips"][0]
    assert repeated["complete"] is True and repeated["already_recorded"] is True
    assert repeated["risk_event"] is None
    data["income_rows"][3]["amount"] = "-0.5"
    changed = result(module, data, prior)["trips"][0]
    assert changed["trade_id"] == first["trade_id"]
    assert changed["proof_sha256"] != first["proof_sha256"]
    assert "completion_correction_required" in changed["reason_codes"]
    assert changed["complete"] is False and changed["risk_event"] is None


def test_high_precision_costs_do_not_depend_on_global_decimal_context(module):
    data = evidence()
    data["fills"][0]["commission"] = "0.12345678901234567890123456789012345"
    data["income_rows"][0]["amount"] = "-0.12345678901234567890123456789012345"
    with localcontext() as context:
        context.prec = 6
        item = trip(module, data)
    assert item["actual_commission_usdt"] == "0.32345678901234567890123456789012345"
    assert item["net_pnl_usdt"] == "9.27654321098765432109876543210987655"


@pytest.mark.parametrize("value", [True, 0.1, "NaN", "Infinity", "1E+1000000", "1E-1000000"])
def test_invalid_or_binary_float_quantities_are_rejected_before_arithmetic(module, value):
    data = evidence()
    data["fills"][0]["quantity"] = value
    out = result(module, data)
    assert out["risk_events"] == [] and "decimal_invalid" in out["reason_codes"]


def test_same_entry_order_cannot_become_multiple_roundtrips_after_partial_flat(module):
    data = evidence()
    data["fills"] = [
        fill("1", "entry", "buy", 1000, quantity="1", fee="0"),
        fill("2", "exit", "sell", 1500, quantity="1", pnl="1", fee="0"),
        fill("3", "entry", "buy", 2000, quantity="1", fee="0"),
        fill("4", "exit2", "sell", 3000, quantity="1", pnl="1", fee="0"),
    ]
    data["orders"] = [
        order("entry"),
        order("exit", side="sell", quantity="1", filled="1"),
        order("exit2", side="sell", quantity="1", filled="1"),
    ]
    data["income_rows"] = [
        income("REALIZED_PNL", "p2", 1500, "1", trade="2"),
        income("REALIZED_PNL", "p4", 3000, "1", trade="4"),
    ]
    out = result(module, data)
    assert out["risk_events"] == [] and "order_spans_round_trips" in out["reason_codes"]


def test_exit_order_must_be_reduce_only(module):
    data = evidence()
    data["orders"][1]["reduce_only"] = False
    out = result(module, data)
    assert out["risk_events"] == [] and "exit_not_reduce_only" in out["reason_codes"]


def test_larger_archive_and_external_deposit_do_not_change_completed_proof(module):
    data = evidence()
    first = trip(module, data)
    data["end_ms"] = 5000
    data["as_of_ms"] = 5000
    data["coverage"]["end_ms"] = 5000
    data["coverage"]["source_ref"] = "archive/later-scan"
    data["coverage"]["source_hash"] = "b" * 64
    data["income_rows"].append(income("TRANSFER", "deposit-later", 4500, "100", pair=None))
    second = trip(module, data)
    assert (
        second["trade_id"] == first["trade_id"] and second["proof_sha256"] == first["proof_sha256"]
    )


def test_boundary_position_flag_without_archived_exchange_ordering_cannot_authorize_funding(module):
    data = evidence()
    row = data["income_rows"][3]
    row["timestamp_ms"] = 1000
    row["position_evidence"] = archive(
        timestamp_ms=1000,
        fill_watermark="1",
        included_fill_ids=["1"],
        signed_quantity="2",
        ordering_verified=True,
    )
    out = trip(module, data)
    assert out["complete"] is False and "funding_position_evidence_invalid" in out["reason_codes"]


def test_funding_proof_cannot_omit_prior_partial_fills(module):
    data = evidence()
    data["fills"] = [
        fill("1", "entry", "buy", 1000, quantity="1", fee="0.05"),
        fill("2", "entry", "buy", 1500, quantity="1", fee="0.05"),
        fill("3", "exit", "sell", 3000, pnl="10", fee="0.2"),
    ]
    data["income_rows"] = [
        income("COMMISSION", "c1", 1000, "-0.05", trade="1"),
        income("COMMISSION", "c2", 1500, "-0.05", trade="2"),
        income("COMMISSION", "c3", 3000, "-0.2", trade="3"),
        income("REALIZED_PNL", "p3", 3000, "10", trade="3"),
        income("FUNDING_FEE", "f1", 1500, "-0.4"),
    ]
    for index, row in enumerate(data["fills"], 1):
        row.update(event_stream_id="stream-a", event_sequence=index)
    funding = data["income_rows"][-1]
    funding.update(event_stream_id="stream-a", event_sequence=3)
    funding["position_evidence"] = archive(
        timestamp_ms=1500,
        fill_watermark="2",
        included_fill_ids=["2"],
        signed_quantity="1",
        ordering_verified=True,
        event_stream_id="stream-a",
        event_sequence=3,
        pair=BTC,
        position_side="BOTH",
        owned_position=True,
    )
    assert "funding_position_evidence_invalid" in trip(module, data)["reason_codes"]


def test_active_position_funding_is_retained_without_counting_a_completed_trade(module):
    data = evidence()
    data["fills"] = data["fills"][:1]
    data["orders"] = data["orders"][:1]
    data["income_rows"] = [data["income_rows"][0], data["income_rows"][3]]
    out = result(module, data)
    assert out["trips"] == [] and out["risk_events"] == []
    assert out["open_positions"][0]["pending_funding_usdt"] == "-0.4"
    assert out["open_positions"][0]["funding_transaction_ids"] == ["f1"]


def test_same_pair_trade_ids_on_different_symbols_are_independent(module):
    data = evidence()
    for raw in list(data["fills"]):
        row = copy.deepcopy(raw)
        row.update(
            pair=ETH,
            client_id=row["client_id"] + "-eth",
            exchange_order_id=row["exchange_order_id"] + "-eth",
        )
        data["fills"].append(row)
    for raw in list(data["orders"]):
        row = copy.deepcopy(raw)
        row.update(
            pair=ETH,
            client_id=row["client_id"] + "-eth",
            exchange_order_id=row["exchange_order_id"] + "-eth",
        )
        data["orders"].append(row)
    for raw in list(data["income_rows"]):
        row = copy.deepcopy(raw)
        row.update(pair=ETH, transaction_id=row["transaction_id"] + "-eth")
        data["income_rows"].append(row)
    out = result(module, data)
    assert len(out["risk_events"]) == 2
    assert [row["net_pnl_usdt"] for row in out["trips"]] == ["9.3", "9.3"]


def test_full_account_order_reconciliation_rejects_zero_filled_order_claimed_filled(module):
    data = evidence()
    data["orders"].append(order("ghost", quantity="1", filled="0"))
    out = result(module, data)
    assert out["risk_events"] == [] and "filled_order_quantity_mismatch" in out["reason_codes"]


def test_exchange_order_cannot_be_borrowed_by_another_client_or_side(module):
    data = evidence()
    data["orders"][1]["exchange_order_id"] = data["orders"][0]["exchange_order_id"]
    data["fills"][1]["exchange_order_id"] = data["orders"][0]["exchange_order_id"]
    out = result(module, data)
    assert out["risk_events"] == [] and "exchange_order_identity_reused" in out["reason_codes"]


def test_boundary_position_evidence_for_other_pair_cannot_attribute_funding(module):
    data = evidence()
    funding = data["income_rows"][3]
    funding.update(timestamp_ms=1000, event_stream_id="stream-a", event_sequence=2)
    data["fills"][0].update(event_stream_id="stream-a", event_sequence=1)
    funding["position_evidence"] = archive(
        timestamp_ms=1000,
        fill_watermark="1",
        included_fill_ids=["1"],
        signed_quantity="2",
        ordering_verified=True,
        event_stream_id="stream-a",
        event_sequence=2,
        pair=ETH,
        position_side="BOTH",
        owned_position=True,
    )
    out = trip(module, data)
    assert out["complete"] is False and "funding_position_evidence_invalid" in out["reason_codes"]


def test_later_malformed_trade_clears_nested_risk_events_from_whole_archive(module):
    data = evidence()
    data["end_ms"] = 8000
    data["as_of_ms"] = 8000
    data["coverage"]["end_ms"] = 8000
    data["fills"] += [
        fill("3", "entry2", "buy", 5000, fee="NaN"),
        fill("4", "exit2", "sell", 7000, pnl="1", fee="0"),
    ]
    data["orders"] += [order("entry2", updated=7000), order("exit2", side="sell", updated=7000)]
    data["income_rows"] += [income("REALIZED_PNL", "p4", 7000, "1", trade="4")]
    out = result(module, data)
    assert out["risk_events"] == []
    assert all(row["risk_event"] is None for row in out["trips"])
    assert all(row["complete"] is False for row in out["trips"])


def test_increasing_from_flat_cannot_realize_pnl_even_if_income_echoes_it(module):
    data = evidence()
    data["fills"][0]["realized_pnl"] = "100"
    data["income_rows"].append(income("REALIZED_PNL", "fake-profit", 1000, "100", trade="1"))
    out = result(module, data)
    assert out["risk_events"] == [] and "entry_realized_pnl_conflict" in out["reason_codes"]


def test_same_millisecond_fills_obey_archived_exchange_sequence(module):
    data = evidence()
    data["fills"][0].update(event_stream_id="stream-a", event_sequence=2)
    data["fills"][1].update(timestamp_ms=1000, event_stream_id="stream-a", event_sequence=1)
    data["income_rows"] = data["income_rows"][:3]
    for row in data["income_rows"]:
        row["timestamp_ms"] = 1000
    out = result(module, data)
    assert out["risk_events"] == [] and "entry_reduce_only_invalid" in out["reason_codes"]


def test_same_millisecond_without_unique_archived_ordering_is_incomplete(module):
    data = evidence()
    data["fills"][1]["timestamp_ms"] = 1000
    data["income_rows"] = data["income_rows"][:3]
    for row in data["income_rows"]:
        row["timestamp_ms"] = 1000
    out = result(module, data)
    assert out["risk_events"] == [] and "fill_ordering_unknown" in out["reason_codes"]


def test_rejected_order_cannot_have_fills(module):
    data = evidence()
    data["orders"][0]["status"] = "rejected"
    out = result(module, data)
    assert out["risk_events"] == [] and "rejected_order_has_fills" in out["reason_codes"]


@pytest.mark.parametrize("index", [0, 1, 2])
def test_commission_or_realized_income_cannot_precede_its_linked_fill(module, index):
    data = evidence()
    data["income_rows"][index]["timestamp_ms"] = 500
    out = result(module, data)
    assert out["risk_events"] == [] and "income_precedes_linked_fill" in out["reason_codes"]

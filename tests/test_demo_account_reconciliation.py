"""真实 reducer 的离线证据测试；不请求交易所、不拼造运行账户事实。"""
from copy import deepcopy

from demo_account_reconciliation import reconcile_account_risk_evidence


OPEN = 57_600_000  # 北京时间 1970-01-02 00:00:00
NOW = 60_000_000
SCOPE = {"environment": "binance_demo", "account_id": "exclusive-demo",
         "stage_id": "stage-1000"}


def snapshot(timestamp, wallet, unrealized="0", *, equity=None):
    return {**SCOPE, "timestamp_ms": timestamp, "source": "exchange_account_snapshot",
            "source_ref": f"account-response-{timestamp}", "reconciled": True,
            "positions_complete": True, "mark_prices_complete": True,
            "multi_assets_margin": False,
            "asset": "USDT", "wallet_balance_usdt": wallet,
            "unrealized_pnl_usdt": unrealized,
            "net_equity_usdt": wallet if equity is None else equity}


def income(tran_id, kind, amount, timestamp):
    return {"tranId": tran_id, "incomeType": kind, "income": amount,
            "asset": "USDT", "time": timestamp}


def evidence(rows=None):
    return {**SCOPE, "stage_start_ms": 0,
            "stage_opening": snapshot(0, "1000"),
            "day_opening": snapshot(OPEN, "1000"),
            "current": snapshot(NOW, "1000"),
            "income_scan": {**SCOPE, "source": "exchange_income_history",
                "all_income_types": True,
                "start_ms": 1, "end_ms": NOW, "limit": 1000,
                "pages": [{"page": 1, "start_ms": 1, "end_ms": NOW,
                           "source_ref": "income-page-1", "has_more": False,
                           "rows": rows or []}]}}


def reconcile(data):
    return reconcile_account_risk_evidence(data, now_ms=NOW, max_snapshot_age_ms=5_000)


def test_verified_zero_income_history_is_complete_without_subtracting_costs_twice():
    result = reconcile(evidence())
    assert result["data_complete"] is True
    assert result["funding_fee_complete"] is True
    assert result["cumulative_external_net_flow_usdt"] == "0"
    assert result["margin_balance"] == "1000"
    assert result["as_of_ms"] == NOW
    assert result["risk_day_opening"]["timestamp_ms"] == OPEN
    assert result["risk_day_opening"]["net_equity_usdt"] == "1000"
    assert result["reasons"] == []


def test_external_flow_is_excluded_but_funding_commission_and_unrealized_stay_in_equity():
    data = evidence([income(1, "TRANSFER", "100", OPEN - 1),
                     income(2, "TRANSFER", "50", OPEN + 1),
                     income(3, "REALIZED_PNL", "30", OPEN + 2),
                     income(4, "COMMISSION", "-2", OPEN + 3),
                     income(5, "FUNDING_FEE", "-3", OPEN + 4)])
    data["day_opening"] = snapshot(OPEN, "1100")
    data["current"] = snapshot(NOW, "1175", "-10", equity="1165")
    result = reconcile(data)
    assert result["data_complete"] is True
    assert result["margin_balance"] == "1165"
    assert result["cumulative_external_net_flow_usdt"] == "150"
    assert result["risk_day_opening"]["cumulative_external_net_flow_usdt"] == "100"
    assert result["daily_net_pnl_usdt"] == "15"
    assert result["funding_net_usdt"] == "-3"
    assert result["commission_net_usdt"] == "-2"


def test_missing_or_incomplete_income_scan_never_defaults_cashflow_to_zero():
    for change in ("missing_scan", "no_pages", "not_terminal", "missing_page", "window_gap"):
        data = evidence()
        if change == "missing_scan":
            del data["income_scan"]
        elif change == "no_pages":
            data["income_scan"]["pages"] = []
        elif change == "not_terminal":
            data["income_scan"]["pages"][0]["has_more"] = True
        elif change == "missing_page":
            data["income_scan"]["pages"][0]["page"] = 2
        else:
            data["income_scan"]["pages"][0]["start_ms"] = 2
        result = reconcile(data)
        assert result["data_complete"] is False, change
        assert result["funding_fee_complete"] is False, change
        assert result["cumulative_external_net_flow_usdt"] is None, change
        assert result["reasons"], change


def test_complete_pages_are_scanned_in_order_and_reconcile_wallet_delta():
    data = evidence([income(1, "TRANSFER", "10", OPEN + 1)])
    scan = data["income_scan"]
    scan["limit"] = 1
    scan["pages"][0]["has_more"] = True
    scan["pages"].append({"page": 2, "start_ms": 1, "end_ms": NOW,
                          "source_ref": "income-page-2", "has_more": False, "rows": []})
    data["current"] = snapshot(NOW, "1010")
    assert reconcile(data)["data_complete"] is True
    scan["pages"] = list(reversed(scan["pages"]))
    assert reconcile(data)["data_complete"] is False


def test_missing_wallet_event_and_wrong_equity_sum_are_rejected():
    for current in (snapshot(NOW, "999"), snapshot(NOW, "1000", "-2", equity="1000")):
        data = evidence()
        data["current"] = current
        result = reconcile(data)
        assert result["data_complete"] is False
        assert result["daily_net_pnl_usdt"] is None


def test_unknown_income_type_non_usdt_and_duplicate_transactions_block_completeness():
    cases = [[income(1, "UNKNOWN_REWARD", "1", OPEN + 1)],
             [{**income(1, "COMMISSION", "-1", OPEN + 1), "asset": "BNB"}],
             [income(1, "TRANSFER", "1", OPEN + 1)] * 2,
             [income(1, "TRANSFER", "1", NOW + 1)]]
    for rows in cases:
        result = reconcile(evidence(rows))
        assert result["data_complete"] is False
        assert result["funding_fee_complete"] is False
        assert result["cumulative_external_net_flow_usdt"] is None


def test_same_transaction_number_in_different_income_types_is_not_a_duplicate():
    data = evidence([income(1, "REALIZED_PNL", "2", OPEN + 1),
                     income(1, "COMMISSION", "-2", OPEN + 1)])
    assert reconcile(data)["data_complete"] is True


def test_account_stage_environment_and_snapshot_provenance_must_match():
    for section, field, value in (("current", "account_id", "another-account"),
                                  ("day_opening", "stage_id", "another-stage"),
                                  ("income_scan", "environment", "live"),
                                  ("current", "reconciled", False),
                                  ("current", "source_ref", ""),
                                  ("current", "source", "interpolated"),
                                  ("current", "positions_complete", False),
                                  ("current", "mark_prices_complete", False),
                                  ("current", "multi_assets_margin", True),
                                  ("income_scan", "all_income_types", False)):
        data = evidence()
        data[section][field] = value
        assert reconcile(data)["data_complete"] is False


def test_non_midnight_future_stale_or_missing_opening_cannot_be_fabricated():
    cases = []
    data = evidence()
    data["day_opening"]["timestamp_ms"] = OPEN + 1
    cases.append(data)
    data = evidence()
    data["current"]["timestamp_ms"] = NOW + 1
    cases.append(data)
    data = evidence()
    data["current"]["timestamp_ms"] = NOW - 5_001
    cases.append(data)
    data = evidence()
    del data["day_opening"]
    cases.append(data)
    for data in cases:
        result = reconcile(data)
        assert result["data_complete"] is False
        assert result["risk_day_opening"] is None


def test_malformed_finite_money_and_input_types_fail_closed_without_mutation():
    for value in (None, {}, [], True, "NaN", "Infinity", "bad"):
        data = evidence()
        data["current"]["net_equity_usdt"] = value
        before = deepcopy(data)
        result = reconcile(data)
        assert result["data_complete"] is False
        assert result["margin_balance"] is None
        assert data == before
    for data in (None, [], "bad"):
        assert reconcile(data)["data_complete"] is False


def test_observation_fingerprint_is_stable_for_identical_evidence_and_binds_facts():
    data = evidence()
    first = reconcile(data)
    second = reconcile(deepcopy(data))
    assert first["risk_observation_id"] == second["risk_observation_id"]
    data["current"]["unrealized_pnl_usdt"] = "1"
    data["current"]["net_equity_usdt"] = "1001"
    assert reconcile(data)["risk_observation_id"] != first["risk_observation_id"]


def test_current_snapshot_from_previous_day_does_not_establish_todays_risk_opening():
    # 使用真实正数日期，避免 epoch 之前的午夜只因时间戳非法而被挡住。
    shift = 10 * 86_400_000
    data = evidence()
    data["stage_start_ms"] = shift
    data["stage_opening"] = snapshot(shift, "1000")
    data["day_opening"] = snapshot(shift + OPEN, "1000")
    data["current"] = snapshot(shift + OPEN + 86_400_000 - 1, "1000")
    scan = data["income_scan"]
    scan["start_ms"] = shift + 1
    scan["end_ms"] = shift + OPEN + 86_400_000 - 1
    scan["pages"][0].update(start_ms=scan["start_ms"], end_ms=scan["end_ms"])
    result = reconcile_account_risk_evidence(
        data, now_ms=shift + OPEN + 86_400_000, max_snapshot_age_ms=5_000)
    assert result["data_complete"] is False
    assert result["risk_day_opening"] is None


def test_full_terminal_page_requires_followup_and_infinite_request_window_is_rejected():
    data = evidence([income(1, "TRANSFER", "0", OPEN + 1)])
    data["income_scan"]["limit"] = 1
    result = reconcile(data)
    assert result["data_complete"] is False
    assert result["reasons"] == ["income_pagination_incomplete"]

    data = evidence()
    data["stage_start_ms"] = 0
    data["stage_opening"] = snapshot(0, "1000")
    shift = 8 * 86_400_000
    data["day_opening"] = snapshot(shift + OPEN, "1000")
    data["current"] = snapshot(shift + NOW, "1000")
    scan = data["income_scan"]
    scan["end_ms"] = shift + NOW
    scan["pages"][0]["end_ms"] = shift + NOW
    result = reconcile_account_risk_evidence(
        data, now_ms=shift + NOW, max_snapshot_age_ms=5_000)
    assert result["data_complete"] is False
    assert result["reasons"] == ["income_window_requires_chunking"]


def test_boundary_income_belongs_to_opening_and_withdrawals_do_not_create_loss():
    data = evidence([income(1, "TRANSFER", "100", OPEN),
                     income(2, "TRANSFER", "-25", OPEN + 1)])
    data["day_opening"] = snapshot(OPEN, "1100")
    data["current"] = snapshot(NOW, "1075")
    result = reconcile(data)
    assert result["data_complete"] is True
    assert result["risk_day_opening"]["cumulative_external_net_flow_usdt"] == "100"
    assert result["daily_net_pnl_usdt"] == "0"


def test_long_running_stage_requires_contiguous_nonoverlapping_scanned_chunks():
    data = evidence()
    shift = 8 * 86_400_000
    data["day_opening"] = snapshot(shift + OPEN, "1000")
    data["current"] = snapshot(shift + NOW, "1000")
    first = data.pop("income_scan")
    first["end_ms"] = 7 * 86_400_000
    first["pages"][0]["end_ms"] = first["end_ms"]
    second = deepcopy(first)
    second["start_ms"] = first["end_ms"] + 1
    second["end_ms"] = shift + NOW
    second["pages"][0].update(start_ms=second["start_ms"], end_ms=second["end_ms"],
                                  source_ref="income-chunk-2")
    data["income_scans"] = [first, second]
    result = reconcile_account_risk_evidence(
        data, now_ms=shift + NOW, max_snapshot_age_ms=5_000)
    assert result["data_complete"] is True
    for start in (first["end_ms"], first["end_ms"] + 2):
        second["start_ms"] = start
        second["pages"][0]["start_ms"] = start
        result = reconcile_account_risk_evidence(
            data, now_ms=shift + NOW, max_snapshot_age_ms=5_000)
        assert result["data_complete"] is False
        assert result["reasons"] == ["income_chunk_window_incomplete"]


def test_reused_archive_reference_cannot_prove_two_distinct_income_pages():
    data = evidence([income(1, "TRANSFER", "0", OPEN + 1)])
    scan = data["income_scan"]
    scan["limit"] = 1
    scan["pages"][0]["has_more"] = True
    scan["pages"].append({"page": 2, "start_ms": 1, "end_ms": NOW,
                          "source_ref": "income-page-1", "has_more": False, "rows": []})
    result = reconcile(data)
    assert result["data_complete"] is False
    assert result["reasons"] == ["income_page_reference_duplicate"]


def test_page_window_metadata_requires_integer_milliseconds_and_positive_span():
    for field, value in (("start_ms", True), ("end_ms", float(NOW))):
        data = evidence()
        data["income_scan"]["pages"][0][field] = value
        assert reconcile(data)["data_complete"] is False
    data = evidence()
    data["stage_start_ms"] = OPEN
    data["stage_opening"] = snapshot(OPEN, "1000")
    data["current"] = snapshot(OPEN, "1000")
    data["income_scan"].update(start_ms=OPEN + 1, end_ms=OPEN)
    data["income_scan"]["pages"][0].update(start_ms=OPEN + 1, end_ms=OPEN)
    result = reconcile_account_risk_evidence(data, now_ms=OPEN, max_snapshot_age_ms=5_000)
    assert result["data_complete"] is False

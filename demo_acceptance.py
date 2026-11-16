"""按冻结协议裁定 Binance Demo 30 天验收轮次。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
IDENTITY_FIELDS = ("environment", "account_id", "strategy_version_id", "run_id")


def _amount(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("金额必须是有限十进制数")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("金额必须是有限十进制数") from exc
    if not amount.is_finite():
        raise ValueError("金额必须是有限十进制数")
    return amount


def _timestamp(value) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("证据时间必须是带时区的 ISO 时间")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("证据时间必须显式包含时区")
    return parsed.astimezone(timezone.utc)


def _identity(value):
    return value if isinstance(value, str) and value.strip() else None


def _report(evidence, verdict, reasons, criteria, *, data_quality="unknown",
            cost_completeness="unknown"):
    source = evidence if isinstance(evidence, dict) else {}
    try:
        as_of = _timestamp(source.get("as_of")).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, ArithmeticError):
        as_of = None
    reasons = list(dict.fromkeys(reasons))
    return {
        **{key: _identity(source.get(key)) for key in IDENTITY_FIELDS},
        "round_id": _identity(source.get("round_id")),
        "as_of": as_of, "data_quality": data_quality,
        "cost_completeness": cost_completeness,
        "status": verdict, "verdict": verdict,
        "reason": reasons[0] if reasons else "all_acceptance_criteria_met",
        "reasons": reasons, "criteria": criteria,
    }


def _fixed_daily_pnl(row) -> Decimal:
    return (_amount(row["closing_equity_usdt"]) - _amount(row["opening_equity_usdt"])
            - _amount(row["external_net_flow_usdt"]))


def _row_identity_matches(evidence, row):
    return all(_identity(row.get(key)) is not None and row[key] == evidence.get(key)
               for key in IDENTITY_FIELDS)


def _confirmed_boundary_failures(evidence):
    """只把属于本轮精确边界的已知敞口视为确认失败。"""
    if (any(_identity(evidence.get(key)) is None for key in IDENTITY_FIELDS)
            or evidence.get("environment") != "binance_demo"):
        return []
    try:
        start = date.fromisoformat(evidence["start_date_bjt"])
        opening = datetime.combine(start, time.min, BEIJING).astimezone(timezone.utc)
        closing = opening + timedelta(days=30)
        if (_timestamp(evidence.get("window_start_at")) != opening
                or _timestamp(evidence.get("window_end_at")) != closing):
            return []
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return []
    failures = []
    for name, at in (("opening_account_state", opening), ("closing_account_state", closing)):
        state = evidence.get(name)
        if not isinstance(state, dict) or any(state.get(key) != evidence[key] for key in IDENTITY_FIELDS):
            continue
        try:
            if _timestamp(state.get("as_of")) != at:
                continue
        except (TypeError, ValueError, ArithmeticError):
            continue
        failures.extend(f"{name}_{key}_violated" for key in
                        ("flat", "no_open_orders", "no_conditional_orders")
                        if state.get(key) is False)
    return failures


def _proven_daily_risk_failures(evidence, expected_dates, by_date):
    """日末事实可证明触线，完整覆盖仍由风险账本的证据标志保证。"""
    failures = []
    previous_close, adjusted, peak = None, None, None
    for day in expected_dates:
        row = by_date.get(day)
        try:
            if (not isinstance(row, dict) or not _row_identity_matches(evidence, row)
                    or any(row.get(key) is not True for key in
                           ("complete", "costs_complete", "funding_complete"))):
                raise ValueError("风险日事实不完整")
            opening = _amount(row["opening_equity_usdt"])
            closing = _amount(row["closing_equity_usdt"])
            pnl = _fixed_daily_pnl(row)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            previous_close, adjusted, peak = None, None, None
            continue
        if pnl < Decimal("-50"):
            failures.append("daily_loss_exceeded")
        if previous_close is None or previous_close != opening:
            adjusted, peak = opening, opening
        adjusted += pnl
        peak = max(peak, adjusted)
        if peak - adjusted >= Decimal("150"):
            failures.append("drawdown_limit_reached")
        previous_close = closing
    return list(dict.fromkeys(failures))


def _early_daily_risk_failures(evidence):
    """先抽取可归属的已知风险，避免其它证据缺失降级已确认失败。"""
    if evidence.get("environment") != "binance_demo":
        return []
    try:
        start = date.fromisoformat(evidence["start_date_bjt"])
        expected_dates = [start + timedelta(days=offset) for offset in range(30)]
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return []
    rows = evidence.get("daily_records")
    if not isinstance(rows, list):
        return []
    by_date, duplicates = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            day = date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if day in by_date:
            duplicates.add(day)
        by_date[day] = row
    for day in duplicates:
        by_date.pop(day)
    return _proven_daily_risk_failures(evidence, expected_dates, by_date)


def _boundary_reasons(evidence, name, expected_time, expected_equity):
    state = evidence.get(name)
    if not isinstance(state, dict):
        return [f"{name}_incomplete"]
    reasons = []
    if any(_identity(state.get(key)) is None or state.get(key) != evidence.get(key)
           for key in IDENTITY_FIELDS):
        reasons.append(f"{name}_identity_mismatch")
    try:
        if _timestamp(state.get("as_of")) != expected_time:
            reasons.append(f"{name}_time_mismatch")
    except (TypeError, ValueError, ArithmeticError):
        reasons.append(f"{name}_time_invalid")
    try:
        if expected_equity is None or _amount(state.get("net_equity_usdt")) != expected_equity:
            reasons.append(f"{name}_equity_mismatch")
    except (TypeError, ValueError, ArithmeticError):
        reasons.append(f"{name}_equity_invalid")
    for key in ("flat", "no_open_orders", "no_conditional_orders"):
        if state.get(key) is False:
            # 明确违反首尾安全条件是失败；缺失事实只能判为证据不足。
            reasons.append(f"{name}_{key}_violated")
        elif state.get(key) is not True:
            reasons.append(f"{name}_{key}_unknown")
    if state.get("reconciled") is not True:
        reasons.append(f"{name}_reconciliation_incomplete")
    return reasons


def evaluate_acceptance_round(evidence: dict) -> dict:
    """返回 passed、failed 或 insufficient_evidence 及逐项可追溯依据。

    硬风险失败先裁定；任何缺日、费用缺失或成交对账不完整都会阻止通过。
    停机日保留在固定日历窗口内，外部资金流不计为投资收益。
    """
    if not isinstance(evidence, dict):
        return _report(evidence, "insufficient_evidence",
                       ["acceptance_evidence_invalid", "TypeError"], {})

    reasons = []
    raw_hard_failures = evidence.get("confirmed_hard_risk_failures")
    boundary_failures = _confirmed_boundary_failures(evidence) + _early_daily_risk_failures(evidence)
    if not isinstance(raw_hard_failures, list):
        return _report(evidence, "failed" if boundary_failures else "insufficient_evidence",
                       boundary_failures + ["acceptance_evidence_invalid", "TypeError"], {})
    hard_failures = [item for item in raw_hard_failures
                     if isinstance(item, dict) and item.get("confirmed") is True]
    if hard_failures:
        codes = sorted({str(item.get("code") or "confirmed_hard_risk_failure")
                        for item in hard_failures})
        return _report(evidence, "failed", codes, {"hard_risk_failures": codes})
    if boundary_failures:
        return _report(evidence, "failed", boundary_failures, {})
    if any(not isinstance(item, dict) for item in raw_hard_failures):
        return _report(evidence, "insufficient_evidence",
                       ["acceptance_evidence_invalid", "TypeError"], {})

    deterministic_failure = False
    try:
        initial = _amount(evidence["initial_equity_usdt"])
        start = date.fromisoformat(evidence["start_date_bjt"])
        rows = evidence["daily_records"]
        trips = evidence["closed_round_trips"]
        if not isinstance(rows, list) or not isinstance(trips, list):
            raise ValueError("daily_records 和 closed_round_trips 必须是列表")
        if initial != Decimal("1000"):
            reasons.append("starting_equity_mismatch")
            deterministic_failure = True
        if any(_identity(evidence.get(key)) is None for key in IDENTITY_FIELDS):
            reasons.append("round_identity_incomplete")
        elif evidence["environment"] != "binance_demo":
            reasons.append("acceptance_environment_mismatch")
        if evidence.get("risk_evidence_complete") is not True:
            reasons.append("risk_evidence_incomplete")

        expected_start = datetime.combine(start, time.min, BEIJING).astimezone(timezone.utc)
        expected_end = expected_start + timedelta(days=30)
        try:
            window_start = _timestamp(evidence.get("window_start_at"))
            window_end = _timestamp(evidence.get("window_end_at"))
            if window_start != expected_start or window_end != expected_end:
                reasons.append("fixed_acceptance_window_mismatch")
        except (TypeError, ValueError, ArithmeticError):
            reasons.append("fixed_acceptance_window_invalid")
        try:
            if _timestamp(evidence.get("as_of")) < expected_end:
                reasons.append("acceptance_window_not_finished")
        except (TypeError, ValueError, ArithmeticError):
            reasons.append("acceptance_report_time_invalid")

        expected_dates = [start + timedelta(days=offset) for offset in range(30)]
        by_date = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                day = date.fromisoformat(row["date"])
            except (KeyError, TypeError, ValueError):
                continue
            if day in by_date:
                reasons.append("duplicate_calendar_day")
            by_date[day] = row
        if set(by_date) != set(expected_dates) or len(rows) != 30:
            reasons.append("calendar_window_incomplete")
        if any(not _row_identity_matches(evidence, row) for row in by_date.values()):
            reasons.append("daily_record_identity_mismatch")
        missing = [day.isoformat() for day in expected_dates if day not in by_date]
        ordered_rows = [by_date[day] for day in expected_dates if day in by_date]
        if missing:
            reasons.append("calendar_day_missing")
        incomplete_days = [row for row in ordered_rows if row.get("complete") is not True]
        if incomplete_days:
            reasons.append("daily_evidence_incomplete")
        incomplete_cost_days = [row for row in ordered_rows
                                if row.get("costs_complete") is not True
                                or row.get("funding_complete") is not True]
        if incomplete_cost_days:
            reasons.append("daily_cost_evidence_incomplete")
        if not ordered_rows or _amount(ordered_rows[0]["opening_equity_usdt"]) != Decimal("1000"):
            reasons.append("day_one_opening_equity_mismatch")
        if any(_amount(current["opening_equity_usdt"])
               != _amount(previous["closing_equity_usdt"])
               for previous, current in zip(ordered_rows, ordered_rows[1:])):
            reasons.append("daily_equity_chain_incomplete")

        for name, at, equity in (
            ("opening_account_state", expected_start, initial),
            ("closing_account_state", expected_end,
             _amount(by_date[expected_dates[-1]]["closing_equity_usdt"])
             if expected_dates[-1] in by_date else None),
        ):
            reasons.extend(_boundary_reasons(evidence, name, at, equity))

        daily_pnl = [_fixed_daily_pnl(row) for row in ordered_rows]
        net_profit = sum(daily_pnl, Decimal(0))
        winning_days = sum(value > 0 for value in daily_pnl)
        segments = [sum(daily_pnl[offset:offset + 10], Decimal(0))
                    for offset in (0, 10, 20)]
        complete_trips, seen_ids = [], set()
        trip_costs_complete = True
        for trip in trips:
            if not isinstance(trip, dict):
                trip_costs_complete = False
                continue
            if not _row_identity_matches(evidence, trip):
                reasons.append("round_trip_identity_mismatch")
                continue
            if trip.get("costs_included") is not True or trip.get("funding_complete") is not True:
                trip_costs_complete = False
            if trip.get("funding_complete") is not True:
                reasons.append("funding_evidence_incomplete")
            trade_id = _identity(trip.get("trade_id"))
            if trade_id is None:
                continue
            if trade_id in seen_ids:
                reasons.append("duplicate_round_trip_identity")
                continue
            seen_ids.add(trade_id)
            try:
                opened_at, closed_at = _timestamp(trip.get("opened_at")), _timestamp(trip.get("closed_at"))
                if not expected_start <= opened_at <= closed_at <= expected_end:
                    continue
                _amount(trip["net_pnl_usdt"])
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
            if (trip.get("closed") is True
                    and trip.get("fill_reconciliation_complete") is True
                    and trip.get("costs_included") is True
                    and trip.get("funding_complete") is True):
                complete_trips.append(trip)
        trip_count = len(complete_trips)
        if trip_count != len(trips):
            reasons.append("round_trip_evidence_incomplete")
        data_complete = not reasons
        costs_complete = not incomplete_cost_days and trip_costs_complete
        if reasons:
            verdict = "failed" if deterministic_failure else "insufficient_evidence"
        elif (net_profit < Decimal("1500") or trip_count < 100 or winning_days < 18
              or any(value <= 0 for value in segments)):
            verdict = "failed"
            if net_profit < Decimal("1500"):
                reasons.append("net_profit_below_target")
            if trip_count < 100:
                reasons.append("complete_round_trips_below_target")
            if winning_days < 18:
                reasons.append("winning_days_below_target")
            if any(value <= 0 for value in segments):
                reasons.append("ten_day_segment_not_profitable")
        else:
            verdict = "passed"
        return _report(evidence, verdict, reasons, {
            "initial_equity_usdt": str(initial), "calendar_day_count": len(ordered_rows),
            "window_start_bjt": start.isoformat(), "window_end_bjt": expected_dates[-1].isoformat(),
            "window_start_at": expected_start.isoformat().replace("+00:00", "Z"),
            "window_end_at": expected_end.isoformat().replace("+00:00", "Z"),
            "net_profit_usdt": str(net_profit), "complete_round_trip_count": trip_count,
            "winning_day_count": winning_days,
            "ten_day_segment_net_pnl_usdt": [str(value) for value in segments],
            "missing_dates_bjt": missing, "all_daily_evidence_complete": not incomplete_days,
        }, data_quality="complete" if data_complete else "incomplete",
            cost_completeness="complete" if costs_complete else "incomplete")
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        return _report(evidence, "failed" if deterministic_failure else "insufficient_evidence",
                       reasons + ["acceptance_evidence_invalid", type(exc).__name__], {})

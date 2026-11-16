"""将已归档的交易所账户与资金流水事实归并为风控证据。

本模块不下载数据、不推断缺失事件，也不证明调用方的归档来源真实。输入须由
独立的、绑定账户身份的采集与持久化流程提供；source_ref 指向其不可变原文。
只有完整分页、钱包变动及午夜边界全部对账后，输出才能用于新增风险门禁。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
from zoneinfo import ZoneInfo


LOGGER = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")
SCOPE_FIELDS = ("environment", "account_id", "stage_id")
# 未列出的费用/奖励/资产转换不能静默算作策略收益，须先明确政策并冻结版本。
EXTERNAL_TYPES = frozenset({"TRANSFER"})
INTERNAL_TYPES = frozenset({"REALIZED_PNL", "COMMISSION", "FUNDING_FEE",
                            "INSURANCE_CLEAR"})


class EvidenceError(ValueError):
    """只携带无凭据的稳定原因码，避免日志泄露上游响应。"""


def _require(condition, reason):
    if not condition:
        raise EvidenceError(reason)


def _amount(value):
    if isinstance(value, bool):
        raise EvidenceError("amount_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EvidenceError("amount_invalid") from exc
    _require(result.is_finite(), "amount_invalid")
    return result


def _stamp(value):
    _require(isinstance(value, int) and not isinstance(value, bool) and value >= 0,
             "timestamp_invalid")
    return value


def _identity(source, expected):
    _require(isinstance(source, dict), "evidence_section_missing")
    _require(all(source.get(key) == expected[key] for key in SCOPE_FIELDS),
             "account_stage_identity_mismatch")


def _source_ref(value):
    _require(isinstance(value, str) and bool(value.strip()), "source_reference_missing")
    return value


def _snapshot(source, scope):
    _identity(source, scope)
    _require(source.get("source") == "exchange_account_snapshot",
             "snapshot_source_unverified")
    _source_ref(source.get("source_ref"))
    _require(source.get("asset") == "USDT", "snapshot_asset_unsupported")
    _require(source.get("multi_assets_margin") is False, "multi_asset_margin_unverified")
    _require(all(source.get(key) is True for key in
                 ("reconciled", "positions_complete", "mark_prices_complete")),
             "snapshot_reconciliation_incomplete")
    timestamp = _stamp(source.get("timestamp_ms"))
    wallet = _amount(source.get("wallet_balance_usdt"))
    unrealized = _amount(source.get("unrealized_pnl_usdt"))
    equity = _amount(source.get("net_equity_usdt"))
    _require(equity == wallet + unrealized, "snapshot_equity_sum_mismatch")
    return timestamp, wallet, equity


def _income_rows(scan, scope, start_ms, end_ms, seen, source_refs):
    _identity(scan, scope)
    _require(scan.get("source") == "exchange_income_history", "income_source_unverified")
    _require(scan.get("all_income_types") is True, "income_type_filter_unverified")
    _require(_stamp(scan.get("start_ms")) == start_ms
             and _stamp(scan.get("end_ms")) == end_ms, "income_window_mismatch")
    _require(start_ms <= end_ms, "income_window_invalid")
    # 本采集协议固定每个查询窗口不超过七天，长期轮次须提交连续分块。
    _require(end_ms - start_ms < 7 * 86_400_000, "income_window_requires_chunking")
    limit = scan.get("limit")
    _require(isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 1000,
             "income_page_limit_invalid")
    pages = scan.get("pages")
    _require(isinstance(pages, list) and bool(pages), "income_pages_missing")
    rows = []
    for index, page in enumerate(pages, start=1):
        _require(isinstance(page, dict) and page.get("page") == index
                 and not isinstance(page.get("page"), bool), "income_page_sequence_invalid")
        reference = _source_ref(page.get("source_ref"))
        _require(reference not in source_refs, "income_page_reference_duplicate")
        source_refs.add(reference)
        _require(_stamp(page.get("start_ms")) == start_ms
                 and _stamp(page.get("end_ms")) == end_ms,
                 "income_page_window_mismatch")
        values = page.get("rows")
        _require(isinstance(values, list) and len(values) <= limit, "income_page_rows_invalid")
        # 终页必须短于 limit；恰好满页必须再保存一页，不能靠声明 has_more=false 截断。
        more = index < len(pages)
        _require(page.get("has_more") is more
                 and ((more and len(values) == limit) or (not more and len(values) < limit)),
                 "income_pagination_incomplete")
        for row in values:
            _require(isinstance(row, dict), "income_row_invalid")
            kind = row.get("incomeType")
            _require(isinstance(kind, str) and kind in EXTERNAL_TYPES | INTERNAL_TYPES,
                     "income_type_unclassified")
            _require(row.get("asset") == "USDT", "income_asset_unsupported")
            transaction = row.get("tranId")
            _require(isinstance(transaction, (str, int)) and not isinstance(transaction, bool)
                     and bool(str(transaction).strip()), "income_transaction_id_missing")
            # Binance tranId 只在 incomeType 内唯一；相同类型重复页会破坏完整性证明。
            key = (kind, str(transaction))
            _require(key not in seen, "income_transaction_duplicate")
            seen.add(key)
            timestamp = _stamp(row.get("time"))
            _require(start_ms <= timestamp <= end_ms, "income_row_outside_window")
            amount = _amount(row.get("income"))
            rows.append((timestamp, kind, amount))
    return rows


def _fingerprint(value):
    normalized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                            allow_nan=False)
    return hashlib.sha256(normalized.encode()).hexdigest()


def _all_income_rows(evidence, scope, start_ms, end_ms):
    _require(not ("income_scans" in evidence and "income_scan" in evidence),
             "income_scan_form_ambiguous")
    if "income_scans" not in evidence:
        return _income_rows(evidence.get("income_scan"), scope, start_ms, end_ms, set(), set())
    scans = evidence["income_scans"]
    _require(isinstance(scans, list) and bool(scans), "income_chunks_missing")
    cursor, rows, seen, source_refs = start_ms, [], set(), set()
    for scan in scans:
        _require(isinstance(scan, dict), "income_chunk_invalid")
        chunk_start = _stamp(scan.get("start_ms"))
        chunk_end = _stamp(scan.get("end_ms"))
        _require(chunk_start == cursor and chunk_start <= chunk_end <= end_ms,
                 "income_chunk_window_incomplete")
        rows.extend(_income_rows(scan, scope, chunk_start, chunk_end, seen, source_refs))
        cursor = chunk_end + 1
    _require(cursor == end_ms + 1, "income_chunk_window_incomplete")
    return rows


def reconcile_account_risk_evidence(evidence, *, now_ms, max_snapshot_age_ms):
    """返回与执行服务兼容的净权益证据，任何缺口均保持 data_complete=false。

    资金流水查询区间为 (阶段起点, 当前权益时刻]，午夜快照包含该时刻已确认事件。
    从阶段/午夜钱包到当前钱包均须与全部流水精确相等。手续费、资金费已在权益中，
    此处只核验与归因，不再次从净权益扣除；外部净入金才从收益基线剔除。
    """
    result = {"margin_balance": None, "cumulative_external_net_flow_usdt": None,
              "funding_fee_complete": False, "data_complete": False,
              "risk_day_opening": None, "daily_net_pnl_usdt": None,
              "funding_net_usdt": None, "commission_net_usdt": None,
              "risk_observation_id": None, "as_of_ms": None, "reasons": [],
              "data_quality": "incomplete", "cost_completeness": "unknown"}
    try:
        now = _stamp(now_ms)
        age_limit = _stamp(max_snapshot_age_ms)
        _require(isinstance(evidence, dict), "account_evidence_invalid")
        scope = {key: evidence.get(key) for key in SCOPE_FIELDS}
        _require(all(isinstance(value, str) and bool(value.strip()) for value in scope.values()),
                 "account_stage_identity_missing")
        _require(scope["environment"] == "binance_demo", "environment_unsupported")
        start_ms = _stamp(evidence.get("stage_start_ms"))
        start_ts, start_wallet, _ = _snapshot(evidence.get("stage_opening"), scope)
        opening_ts, opening_wallet, opening_equity = _snapshot(evidence.get("day_opening"), scope)
        current_ts, current_wallet, current_equity = _snapshot(evidence.get("current"), scope)
        _require(start_ts == start_ms and start_ms <= opening_ts <= current_ts <= now,
                 "snapshot_time_order_invalid")
        _require(now - current_ts <= age_limit, "account_snapshot_stale")
        current_day = datetime.fromtimestamp(current_ts / 1000, timezone.utc).astimezone(BEIJING)
        report_day = datetime.fromtimestamp(now / 1000, timezone.utc).astimezone(BEIJING)
        _require(current_day.date() == report_day.date(), "current_beijing_day_snapshot_missing")
        midnight = current_day.replace(hour=0, minute=0, second=0, microsecond=0)
        _require(opening_ts == int(midnight.timestamp() * 1000),
                 "beijing_midnight_snapshot_missing")
        rows = _all_income_rows(evidence, scope, start_ms + 1, current_ts)
        total_delta = sum((amount for _, _, amount in rows), Decimal(0))
        opening_delta = sum((amount for ts, _, amount in rows if ts <= opening_ts), Decimal(0))
        _require(current_wallet == start_wallet + total_delta
                 and opening_wallet == start_wallet + opening_delta,
                 "wallet_cashflow_reconciliation_mismatch")
        external = sum((amount for _, kind, amount in rows if kind in EXTERNAL_TYPES), Decimal(0))
        opening_external = sum((amount for ts, kind, amount in rows
                                if ts <= opening_ts and kind in EXTERNAL_TYPES), Decimal(0))
        fingerprint = _fingerprint(evidence)
        opening = {"timestamp_ms": opening_ts, "net_equity_usdt": str(opening_equity),
                   "cumulative_external_net_flow_usdt": str(opening_external),
                   "data_complete": True,
                   "event_id": "day-opening:" + _fingerprint({
                       **scope, "timestamp_ms": opening_ts, "net_equity_usdt": str(opening_equity),
                       "cumulative_external_net_flow_usdt": str(opening_external)})}
        result.update(margin_balance=str(current_equity), as_of_ms=current_ts,
                      cumulative_external_net_flow_usdt=str(external),
                      funding_fee_complete=True, data_complete=True,
                      risk_day_opening=opening, risk_observation_id="account-equity:" + fingerprint,
                      daily_net_pnl_usdt=str(current_equity - opening_equity
                                             - (external - opening_external)),
                      funding_net_usdt=str(sum((amount for _, kind, amount in rows
                                               if kind == "FUNDING_FEE"), Decimal(0))),
                      commission_net_usdt=str(sum((amount for _, kind, amount in rows
                                                  if kind == "COMMISSION"), Decimal(0))),
                      data_quality="complete", cost_completeness="reconciled")
        LOGGER.info("Demo 账户净权益对账完成：账户=%s 阶段=%s 时间毫秒=%s 净权益=%s "
                    "累计外部净流=%s 日净收益=%s 流水数=%s 证据指纹=%s",
                    scope["account_id"], scope["stage_id"], current_ts, current_equity,
                    external, result["daily_net_pnl_usdt"], len(rows), fingerprint)
    except EvidenceError as exc:
        result["reasons"] = [str(exc)]
    except (TypeError, ValueError, KeyError, ArithmeticError, OverflowError):
        result["reasons"] = ["account_evidence_invalid"]
    if not result["data_complete"]:
        LOGGER.warning("Demo 账户净权益证据不完整：原因=%s；禁止新增风险，不能将未知资金流记为零",
                       result["reasons"][0])
    return result

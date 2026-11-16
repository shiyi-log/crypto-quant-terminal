"""可信内部选择器到不可变验收证据的只读服务；不授予执行资格。"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re
import threading
import time as system_time
from zoneinfo import ZoneInfo

from demo_acceptance import evaluate_acceptance_round
from demo_evidence_store import DemoEvidenceStore, HASHES
from demo_round_trip_costs import reconcile_round_trip_costs

SCOPE = ("environment", "account_id", "stage_id", "strategy_version_id", "run_id")
TYPED = ("account_snapshot", "normal_orders", "conditional_orders", "fills", "cashflows")
SHA = re.compile(r"[a-f0-9]{64}")
ID = re.compile(r"[A-Za-z0-9_./:+-]{1,256}")
DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:[.][0-9]+)?")
BEIJING = ZoneInfo("Asia/Shanghai")


class EvidenceUnavailable(ValueError):
    """固定原因码；不携带来源异常或连接信息。"""


def _safe(value):
    return (
        isinstance(value, str) and ID.fullmatch(value) and "://" not in value and "@" not in value
    )


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _stamp(value):
    if not isinstance(value, str):
        raise EvidenceUnavailable("projection_time_invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise EvidenceUnavailable("projection_time_invalid") from None


def _amount(value):
    if not isinstance(value, str) or len(value) > 302 or not DECIMAL.fullmatch(value):
        raise EvidenceUnavailable("projection_amount_invalid")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise EvidenceUnavailable("projection_amount_invalid") from None
    if not result.is_finite() or len(result.as_tuple().digits) > 300:
        raise EvidenceUnavailable("projection_amount_invalid")
    return result


class DemoAcceptanceService:
    """所有绑定与回调由服务端组合根提供，evaluate 不接受请求正文。"""

    def __init__(
        self,
        archive,
        *,
        scope,
        identity_hashes,
        round_id,
        start_date_bjt,
        bundle_selector=None,
        semantic_validator=None,
        clock_ms=None,
    ):
        if (
            not isinstance(archive, DemoEvidenceStore)
            or type(scope) is not dict
            or set(scope) != set(SCOPE)
            or scope.get("environment") != "binance_demo"
            or any(not _safe(scope.get(key)) for key in SCOPE)
            or (archive.environment, archive.account_id, archive.stage_id)
            != (scope["environment"], scope["account_id"], scope["stage_id"])
            or not _safe(round_id)
        ):
            raise ValueError("验收内部身份绑定无效")
        if (
            type(identity_hashes) is not dict
            or set(identity_hashes) != set(HASHES)
            or any(
                not isinstance(item, str) or not SHA.fullmatch(item)
                for item in identity_hashes.values()
            )
        ):
            raise ValueError("验收冻结哈希无效")
        try:
            start = date.fromisoformat(start_date_bjt)
            if start.isoformat() != start_date_bjt:
                raise ValueError
            opening = datetime.combine(start, time.min, BEIJING).astimezone(timezone.utc)
            closing = opening + timedelta(days=30)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("验收固定日历无效") from None
        self.archive = archive
        self.scope, self.hashes = deepcopy(scope), deepcopy(identity_hashes)
        self.round_id, self.start_date = round_id, start
        self.opening, self.closing = opening, closing
        self.bundle_selector, self.semantic_validator = bundle_selector, semantic_validator
        self.clock_ms = clock_ms or (lambda: int(system_time.time() * 1000))
        self._read_snapshot = None
        self._completion_snapshot = None
        self._projection_source_refs = None
        self._evaluate_lock = threading.RLock()

    def _binding(self):
        return {
            "scope": deepcopy(self.scope),
            "identity_hashes": deepcopy(self.hashes),
            "round_id": self.round_id,
            "window_start_at": _iso(self.opening),
            "window_end_at": _iso(self.closing),
        }

    def _read(self, ref, kind, *, before=None, record_hash=None):
        if not _safe(ref):
            raise EvidenceUnavailable("archive_record_unverified")
        try:
            row = (self._read_snapshot or {}).get(ref)
            if row is None:
                raise ValueError('reference missing')
            row = deepcopy(row)
        except Exception:
            raise EvidenceUnavailable("archive_record_unverified") from None
        if (
            type(row) is not dict
            or row.get("committed") is not True
            or row.get("ref") != ref
            or row.get("kind") != kind
            or row.get("scope") != self.scope
            or type(row.get("payload")) is not dict
            or type(row.get("sequence")) is not int
            or row["sequence"] <= 0
            or (before is not None and row["sequence"] >= before)
            or any(
                not isinstance(row.get(key), str) or not SHA.fullmatch(row[key])
                for key in ("sha256", "record_sha256")
            )
            or (record_hash is not None and row["record_sha256"] != record_hash)
            or (kind != "completion" and row["payload"].get("identity_hashes") != self.hashes)
        ):
            raise EvidenceUnavailable("archive_scope_kind_or_identity_mismatch")
        return row

    def _projection(self, ref, purpose, owner, hashes):
        kind = "boundary" if purpose == "boundary" else "equity_reconciliation"
        row = self._read(ref, kind, before=owner["sequence"], record_hash=hashes[ref])
        payload = row["payload"]
        if (
            payload.get("schema") != "demo-acceptance-projection-v1"
            or payload.get("purpose") != purpose
        ):
            raise EvidenceUnavailable("acceptance_projection_contract_invalid")
        refs, frozen = payload.get("source_refs"), payload.get("source_record_hashes")
        allowed = set(TYPED) | (
            {"opening_boundary", "closing_boundary"} if purpose == "daily" else set()
        )
        if (
            type(refs) is not dict
            or set(refs) != allowed
            or type(frozen) is not dict
            or any(not _safe(value) for value in refs.values())
            or len(refs.values()) != len(set(refs.values()))
            or set(frozen) != set(refs.values())
        ):
            raise EvidenceUnavailable("projection_dependencies_incomplete")
        expected_refs = self._projection_source_refs
        if purpose == "daily":
            expected_refs = {
                **(expected_refs or {}),
                "opening_boundary": refs.get("opening_boundary"),
                "closing_boundary": refs.get("closing_boundary"),
            }
        if expected_refs is not None and refs != expected_refs:
            raise EvidenceUnavailable("projection_typed_sources_mismatch")
        sources = {}
        for role, dependency in refs.items():
            expected = role if role in TYPED else "boundary"
            sources[dependency] = self._read(
                dependency, expected, before=row["sequence"], record_hash=frozen[dependency]
            )
        if not callable(self.semantic_validator):
            raise EvidenceUnavailable("semantic_validator_unavailable")
        try:
            verified = self.semantic_validator(
                deepcopy(row), deepcopy(sources), purpose=purpose, binding=self._binding()
            )
        except Exception:
            raise EvidenceUnavailable("semantic_proof_unverified") from None
        expected = {
            "verified": True,
            "purpose": purpose,
            "scope": self.scope,
            "identity_hashes": self.hashes,
            "record_sha256": row["record_sha256"],
            "source_record_hashes": frozen,
            "window_start_at": _iso(self.opening),
            "window_end_at": _iso(self.closing),
        }
        if (
            type(verified) is not dict
            or verified != expected
            or verified.get("verified") is not True
        ):
            raise EvidenceUnavailable("semantic_proof_unverified")
        return row, sources

    def _identity(self, value):
        if type(value) is not dict or any(
            value.get(key) != item for key, item in self.scope.items()
        ):
            raise EvidenceUnavailable("projection_value_identity_mismatch")

    def _boundary(self, row, allowed_refs=None):
        value = deepcopy(row["payload"].get("value"))
        self._identity(value)
        at = _stamp(value.get("as_of"))
        if (
            at != _stamp(value.get("state_at"))
            or not self.opening <= at <= self.closing
            or at.astimezone(BEIJING).time() != time.min
            or value.get("method") not in {"direct_exchange_asof", "replayed_boundary_projection"}
            or not isinstance(value.get("valuation_policy_hash"), str)
            or not SHA.fullmatch(value["valuation_policy_hash"])
        ):
            raise EvidenceUnavailable("exact_midnight_boundary_unverified")
        if allowed_refs is not None and row["ref"] not in allowed_refs:
            raise EvidenceUnavailable("daily_boundary_not_in_manifest")
        _amount(value.get("net_equity_usdt"))
        for key in ("flat", "no_open_orders", "no_conditional_orders", "reconciled"):
            if type(value.get(key)) is not bool:
                raise EvidenceUnavailable("boundary_reconciliation_unverified")
        return value

    def _daily(self, row, sources, boundary_refs):
        value = deepcopy(row["payload"].get("value"))
        self._identity(value)
        try:
            day = date.fromisoformat(value["date"])
        except (KeyError, TypeError, ValueError):
            raise EvidenceUnavailable("daily_calendar_invalid") from None
        opening = datetime.combine(day, time.min, BEIJING).astimezone(timezone.utc)
        closing = opening + timedelta(days=1)
        if (
            _stamp(value.get("window_start_at")) != opening
            or _stamp(value.get("window_end_at")) != closing
            or not self.opening <= opening < closing <= self.closing
        ):
            raise EvidenceUnavailable("daily_window_invalid")
        for key in ("opening_equity_usdt", "closing_equity_usdt", "external_net_flow_usdt"):
            _amount(value.get(key))
        for key in ("complete", "costs_complete", "funding_complete"):
            if type(value.get(key)) is not bool:
                raise EvidenceUnavailable("daily_completeness_unverified")
        if any(value[key] is not True for key in ("complete", "costs_complete", "funding_complete")):
            raise EvidenceUnavailable("daily_evidence_incomplete")
        for role, at, amount in (
            ("opening_boundary", opening, "opening_equity_usdt"),
            ("closing_boundary", closing, "closing_equity_usdt"),
        ):
            boundary = self._boundary(
                sources[row["payload"]["source_refs"][role]], boundary_refs
            )
            if _stamp(boundary["as_of"]) != at or _amount(boundary["net_equity_usdt"]) != _amount(
                value[amount]
            ):
                raise EvidenceUnavailable("daily_boundary_mismatch")
        return value

    def _completion(self, ref, owner, hashes):
        row = self._read(ref, "completion", before=owner["sequence"], record_hash=hashes[ref])
        value = row["payload"]
        if value.get("correction_required") is not False:
            raise EvidenceUnavailable("completion_cost_correction_pending")
        refs = value.get("source_refs")
        if (not isinstance(refs, list) or not refs or any(not _safe(item) for item in refs)
                or len(refs) != len(set(refs))):
            raise EvidenceUnavailable("completion_cost_sources_unverified")
        actuals = []
        for source in refs:
            source_row = self._read(source, "equity_reconciliation", before=row["sequence"])
            cost, _ = self._projection(source, "cost", row, {source: source_row["record_sha256"]})
            evidence = cost["payload"].get("cost_evidence")
            if type(evidence) is not dict or any(
                evidence.get(key) != item for key, item in {**self.scope, **self.hashes}.items()
            ):
                raise EvidenceUnavailable("completion_frozen_identity_mismatch")
            try:
                fresh = reconcile_round_trip_costs(evidence).to_dict()
            except Exception:
                raise EvidenceUnavailable("completion_cost_reconciliation_mismatch") from None
            trips = [trip for trip in fresh["trips"] if trip["trade_id"] == value.get("trade_id")]
            if (
                len(trips) != 1
                or trips[0].get("complete") is not True
                or trips[0] != value.get("completion")
            ):
                raise EvidenceUnavailable("completion_cost_reconciliation_mismatch")
            actuals.append(trips[0])
            if (evidence.get("start_ms") != int(self.opening.timestamp() * 1000)
                    or evidence.get("end_ms") < int(self.closing.timestamp() * 1000)
                    or evidence.get("as_of_ms") < int(self.closing.timestamp() * 1000)):
                raise EvidenceUnavailable("completion_cost_coverage_incomplete")
        if any(item != actuals[0] for item in actuals):
            raise EvidenceUnavailable("completion_cost_reconciliation_mismatch")
        trip = actuals[0]
        _amount(trip.get("net_pnl_usdt"))
        if (
            not self.opening
            <= _stamp(trip.get("opened_at"))
            <= _stamp(trip.get("closed_at"))
            <= self.closing
        ):
            raise EvidenceUnavailable("completion_outside_calendar_window")
        return trip

    def _base(self, as_of):
        return {
            **self.scope,
            "round_id": self.round_id,
            "as_of": as_of,
            "start_date_bjt": self.start_date.isoformat(),
            "initial_equity_usdt": "1000",
            "window_start_at": _iso(self.opening),
            "window_end_at": _iso(self.closing),
            "daily_records": [],
            "closed_round_trips": [],
            "confirmed_hard_risk_failures": [],
            "risk_evidence_complete": False,
        }

    def _report(self, evidence, errors, bundle=None):
        with localcontext() as context:
            context.prec = 1024
            try:
                report = evaluate_acceptance_round(evidence)
            except Exception:
                report = evaluate_acceptance_round(self._base(evidence.get('as_of')))
                errors = [*errors, 'acceptance_evaluator_unavailable']
        if errors:
            confirmed = any(
                item in report.get('reasons', [])
                for item in (
                    'daily_loss_exceeded',
                    'drawdown_limit_reached',
                    'opening_account_state_flat_violated',
                    'opening_account_state_no_open_orders_violated',
                    'opening_account_state_no_conditional_orders_violated',
                    'closing_account_state_flat_violated',
                    'closing_account_state_no_open_orders_violated',
                    'closing_account_state_no_conditional_orders_violated',
                    'starting_equity_mismatch',
                )
            ) or bool(evidence.get('confirmed_hard_risk_failures'))
            if not confirmed:
                report["status"] = report["verdict"] = "insufficient_evidence"
                report["reason"] = errors[0]
            report["reasons"] = list(dict.fromkeys([*errors, *report["reasons"]]))
            report["data_quality"] = report["cost_completeness"] = "incomplete"
        return {
            **report,
            "stage_id": self.scope["stage_id"],
            "identity_hashes": deepcopy(self.hashes),
            "window_start_at": _iso(self.opening),
            "window_end_at": _iso(self.closing),
            "bundle_ref": bundle,
            "acceptance_eligible": False,
            "production_wired": False,
        }

    def _consistent_snapshot(self):
        """Freeze all account evidence while expensive pure projections run."""
        with self.archive.store._connection() as conn:
            self.archive._lock(conn)
            try:
                rows = self.archive._chain(conn)
            except Exception:
                raise EvidenceUnavailable("archive_record_unverified") from None
            try:
                index = self.archive._completion_index(conn, rows)
            except Exception:
                raise EvidenceUnavailable("completion_history_unverified") from None
            self._read_snapshot = {
                row['ref']: {
                    **deepcopy(row['body']),
                    **self.archive._receipt(row),
                    'previous_hash': row['previous_hash'],
                }
                for row in rows
            }
            self._completion_snapshot = [
                {
                    **deepcopy(item['payload']),
                    **self.archive._receipt(next(row for row in rows if row['ref'] == item['ref'])),
                }
                for item in index
            ]

    def evaluate(self):
        """读取同一账户链快照；无法读取链时返回固定证据错误。"""
        with self._evaluate_lock:
            try:
                self._consistent_snapshot()
                return self._evaluate_snapshot()
            except EvidenceUnavailable as error:
                return self._report(self._base(None), [str(error)])
            except Exception:
                return self._report(self._base(None), ['archive_snapshot_unverified'])
            finally:
                self._read_snapshot = None
                self._completion_snapshot = None
                self._projection_source_refs = None

    def _evaluate_snapshot(self):
        """只读取；不存在调用方 supplied evidence、approval 或 bundle_ref 参数。"""
        try:
            now = self.clock_ms()
            if type(now) is not int or not 0 <= now <= 253402300799000:
                raise ValueError
            as_of = _iso(datetime.fromtimestamp(now / 1000, timezone.utc))
        except Exception:
            return self._report(self._base(None), ["acceptance_clock_unavailable"])
        evidence = self._base(as_of)
        if not callable(self.bundle_selector):
            return self._report(evidence, ["acceptance_selector_unavailable"])
        try:
            selected = self.bundle_selector(**self._binding())
        except Exception:
            return self._report(evidence, ["acceptance_selector_failed"])
        if (
            type(selected) is not dict
            or set(selected) != {"bundle_ref", "manifest_ref"}
            or not _safe(selected.get("bundle_ref"))
            or not _safe(selected.get("manifest_ref"))
        ):
            return self._report(evidence, ["acceptance_selector_contract_invalid"])
        ref, manifest_ref = selected["bundle_ref"], selected["manifest_ref"]
        try:
            bundle = self._read(ref, "acceptance")
            payload = bundle["payload"]
            refs = payload.get("source_refs")
            if (
                payload.get("status") != "evidence_bundle_recorded"
                or type(refs) is not dict
                or set(refs) != set(TYPED)
                or len(set(refs.values())) != len(TYPED)
                or payload.get("identity_hashes") != self.hashes
            ):
                raise EvidenceUnavailable("acceptance_bundle_contract_invalid")
            for kind, source in refs.items():
                self._read(source, kind, before=bundle["sequence"])
            manifest = self._read(manifest_ref, "account_snapshot")
            if manifest["sequence"] >= bundle["sequence"]:
                raise EvidenceUnavailable("acceptance_manifest_order_invalid")
            value = manifest["payload"]
            if (
                value.get("schema") != "demo-acceptance-manifest-v1"
                or value.get("round_id") != self.round_id
                or value.get("start_date_bjt") != self.start_date.isoformat()
                or value.get("identity_hashes") != self.hashes
            ):
                raise EvidenceUnavailable("acceptance_manifest_unavailable")
            projection_sources = value.get("projection_source_refs")
            if (
                type(projection_sources) is not dict
                or set(projection_sources) != set(TYPED)
                or any(not _safe(item) for item in projection_sources.values())
                or len(set(projection_sources.values())) != len(TYPED)
                or projection_sources != refs
            ):
                raise EvidenceUnavailable("acceptance_manifest_sources_invalid")
            self._projection_source_refs = refs
            groups = [
                value.get("boundary_refs"),
                value.get("daily_refs"),
                value.get("completion_refs"),
            ]
            if any(not isinstance(items, list) for items in groups):
                raise EvidenceUnavailable("acceptance_manifest_contract_invalid")
            if len(value.get("boundary_refs", [])) != 31 or len(value.get("daily_refs", [])) != 30:
                errors = ['calendar_manifest_incomplete']
            else:
                errors = []
            all_refs = [item for group in groups for item in group] + [value.get("risk_ref")]
            hashes = value.get("projection_record_hashes")
            if (
                len(all_refs) > 100_000
                or any(not _safe(item) for item in all_refs)
                or len(all_refs) != len(set(all_refs))
                or type(hashes) is not dict
                or set(hashes) != set(all_refs)
                or any(
                    not isinstance(item, str) or not SHA.fullmatch(item) for item in hashes.values()
                )
            ):
                raise EvidenceUnavailable("acceptance_manifest_contract_invalid")
        except EvidenceUnavailable as error:
            return self._report(evidence, [str(error)])
        except Exception:
            return self._report(evidence, ["acceptance_bundle_contract_invalid"])
        boundaries = {}
        boundary_refs = set(value["boundary_refs"])
        for boundary_ref in value["boundary_refs"]:
            try:
                row, _ = self._projection(boundary_ref, "boundary", manifest, hashes)
                boundary = self._boundary(row, boundary_refs)
                at = _stamp(boundary["as_of"])
                if at in boundaries:
                    raise EvidenceUnavailable("duplicate_calendar_boundary")
                boundaries[at] = boundary
                if at == self.opening:
                    evidence["opening_account_state"] = boundary
                if at == self.closing:
                    evidence["closing_account_state"] = boundary
            except EvidenceUnavailable as error:
                errors.append(str(error))
            except Exception:
                errors.append('acceptance_boundary_projection_unverified')
        if set(boundaries) != {self.opening + timedelta(days=index) for index in range(31)}:
            errors.append("calendar_boundaries_incomplete")
        for daily_ref in value["daily_refs"]:
            try:
                row, sources = self._projection(daily_ref, "daily", manifest, hashes)
                evidence["daily_records"].append(self._daily(row, sources, boundary_refs))
            except EvidenceUnavailable as error:
                errors.append(str(error))
            except Exception:
                errors.append('acceptance_daily_projection_unverified')
        try:
            row, _ = self._projection(value["risk_ref"], "risk", manifest, hashes)
            risk = row["payload"].get("value")
            if (
                type(risk) is not dict
                or type(risk.get("complete")) is not bool
                or _stamp(risk.get("window_start_at")) != self.opening
                or _stamp(risk.get("window_end_at")) != self.closing
                or not isinstance(risk.get("hard_failures"), list)
            ):
                raise EvidenceUnavailable("risk_window_unverified")
            evidence["risk_evidence_complete"] = risk["complete"]
            self._identity(risk)
            if risk["complete"] is not True:
                errors.append('risk_evidence_incomplete')
            for item in risk["hard_failures"]:
                if (
                    type(item) is not dict
                    or item.get("confirmed") is not True
                    or not _safe(item.get("code"))
                ):
                    errors.append("risk_failure_record_unverified")
                else:
                    evidence["confirmed_hard_risk_failures"].append(
                        {"confirmed": True, "code": item["code"]}
                    )
        except EvidenceUnavailable as error:
            errors.append(str(error))
        except Exception:
            errors.append('acceptance_risk_projection_unverified')
        try:
            history = self._completion_snapshot or []
            current = [item for item in history if item.get("scope") == self.scope]
            if {item["ref"] for item in current} != set(value["completion_refs"]):
                errors.append("completion_history_incomplete")
            if any(item.get("correction_required") is not False for item in current):
                errors.append("completion_cost_correction_pending")
            trade_ids = [item.get('trade_id') for item in current if isinstance(item.get('trade_id'), str)]
            if len(trade_ids) != len(set(trade_ids)):
                errors.append('duplicate_round_trip_identity')
        except Exception:
            errors.append("completion_history_unverified")
        for completion_ref in value["completion_refs"]:
            try:
                evidence["closed_round_trips"].append(
                    self._completion(completion_ref, manifest, hashes)
                )
            except EvidenceUnavailable as error:
                errors.append(str(error))
            except Exception:
                errors.append('completion_cost_reconciliation_mismatch')
        return self._report(evidence, errors, ref)

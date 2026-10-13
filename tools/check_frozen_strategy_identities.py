#!/usr/bin/env python3
"""Fail closed before merging/deploying code that cannot resume frozen accounts.

Production example (includes the separate 24-account execution experiment)::

    .venv/bin/python3 tools/check_frozen_strategy_identities.py --risk-execution-2x2

``--profile production`` matches the deployed factorial/Fnew/paired population;
``--profile lab-default`` matches the lab CLI defaults. Every population flag is
explicit in the report. Archived accounts outside that population are reported
as skipped, never presented as validated. New identities are validated without
creating them; use ``--require-existing`` if an entirely persisted population is
required. ``--ci`` runs fixed historical contracts without connecting to a DB.

The database path deliberately avoids DataStore: its initialization can run DDL.
This tool uses one repeatable-read, read-only PostgreSQL transaction and SELECTs
only. It neither writes local audit files nor persists reports; ``--json`` emits
a report on stdout for a separately authorized audit publisher.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

METHOD = "frozen-strategy-identity-preflight-v1"
# Independently generated with all nine strategy/fee/factory modules AND the
# research source document loaded from the immutable compatible commit 87a9aaf.
# Do not regenerate these from the candidate code being checked. An intentional
# replacement population needs new account identities and a reviewed baseline.
CI_BASELINE_COMMIT = "87a9aaf68773984edad6ad3012286fc138f4feb7"
CI_IDENTITY_ANCHORS = {
    "legacy": (29, "b91be7b7b4129fe2a5358cc49c6ea960cd9c3c218b4d23c8ac96240ec4d3331b"),
    "factorial": (261, "8d1a86ce9295fc60a300807d66aaf0358373fe4ba242ffac998678575c192725"),
    "fnew": (45, "82dc236d762f1e355edec3f6ad7c1baaa8dd519a117aa32fc092f6ca7fd7c4f2"),
    "risk_execution_2x2": (24, "97405c288c1225f1a77d9b8df38f7e510b81fdddbcf3b0b41a6f4e980b6fb631"),
}
PROFILES = {
    "production": {"factorial": True, "fnew": True,
                   "fnew_paired_execution": True, "risk_execution_2x2": False,
                   "cost_adjusted": False},
    "lab-default": {"factorial": True, "fnew": True,
                    "fnew_paired_execution": False, "risk_execution_2x2": False,
                    "cost_adjusted": False},
}


def population_flags(profile="production", **overrides):
    flags = dict(PROFILES[profile])
    unknown = set(overrides) - set(flags)
    if unknown:
        raise ValueError("unsupported population flag")
    flags.update({key: value for key, value in overrides.items() if value is not None})
    if any(not isinstance(value, bool) for value in flags.values()):
        raise ValueError("population flags must be boolean")
    if flags["fnew_paired_execution"] and not flags["fnew"]:
        raise ValueError("paired execution requires the Fnew population")
    return flags


def population_group(spec):
    strategy_id = str(spec.get("strategy_id", ""))
    if "_2x2_" in strategy_id:
        return "risk_execution_2x2"
    if strategy_id.endswith("_Cost4bp"):
        return "cost_adjusted"
    if spec.get("fnew_design_version"):
        return "fnew"
    if spec.get("factorial_design_version"):
        return "factorial"
    return "legacy"


def build_requested_specs(flags):
    """Use the same factories and order as strategy_lab_service.main()."""
    from strategy_lab_service import (load_2x2_specs, load_cost_adjusted_specs,
                                      load_specs, normalize_account_spec)

    requested = load_specs()
    if flags["cost_adjusted"]:
        requested += load_cost_adjusted_specs()
    if flags["risk_execution_2x2"]:
        requested += load_2x2_specs(activate=True)
    if flags["factorial"]:
        from strategy_factorial import load_factorial_specs
        requested += load_factorial_specs(normalizer=normalize_account_spec)
    if flags["fnew"]:
        from strategy_fnew import load_fnew_specs
        requested += load_fnew_specs(
            paired_execution_enabled=flags["fnew_paired_execution"])
    ids = [spec["strategy_id"] for spec in requested]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("empty or colliding requested account identities")
    return requested


def read_frozen_rows(dsn, schema="quant"):
    """Take a consistent SELECT-only snapshot; never initialize a DataStore."""
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    if not dsn:
        raise ValueError("database_not_configured")
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL statement_timeout TO '30s'")
            conn.execute("SET LOCAL lock_timeout TO '5s'")
            return conn.execute(sql.SQL("""SELECT strategy_id, definition, rule_hash,
                execution_hash FROM {}.strategy_sim_accounts ORDER BY strategy_id""")
                .format(sql.Identifier(schema))).fetchall()


class _SnapshotStore:
    """Allow the production resume function to read an already-read snapshot."""

    def __init__(self, rows):
        self.rows = rows

    @contextmanager
    def _connection(self):
        yield self

    def execute(self, statement, params):
        if not statement.lstrip().upper().startswith("SELECT"):
            raise AssertionError("resume audit attempted a write")
        ids = set(params[0])
        self.result = [row for row in self.rows if row["strategy_id"] in ids]
        return self

    def fetchall(self):
        return copy.deepcopy(self.result)


def _exception_code(exc):
    """Return bounded diagnostic codes, never DB errors or arbitrary messages."""
    message = str(exc).lower()
    for token, code in (
        ("canonical signal rule hash mismatch", "canonical_signal_hash_mismatch"),
        ("persisted frozen strategy identity mismatch", "persisted_identity_mismatch"),
        ("rule hash mismatch", "rule_hash_mismatch"),
        ("new strategy identity", "rule_hash_mismatch"),
    ):
        if token in message:
            return code
    return "identity_validation_failed"


def _check_definition(spec, row=None):
    from strategy_lab_service import normalize_account_spec
    from strategy_simulation import execution_identity

    normalized = normalize_account_spec(copy.deepcopy(spec))
    identity = execution_identity(copy.deepcopy(spec))
    if normalized["rule_hash"] != identity["rule_hash"]:
        return "rule_execution_hash_disagreement"
    if row is not None:
        if spec.get("strategy_id") != row["strategy_id"]:
            return "persisted_account_id_mismatch"
        if normalized["rule_hash"] != row["rule_hash"]:
            return "stored_rule_hash_mismatch"
        if identity["execution_hash"] != row["execution_hash"]:
            return "stored_execution_hash_mismatch"
    # Both factorial and 2x2 adapters recompute their canonical signal hash.
    # Check every supplied alias too, so an unused/contradictory alias cannot
    # be mistaken for successful verification of that persisted evidence.
    canonical = normalized.get("signal_rule_hash", normalized["rule_hash"])
    for field in ("signal_rule_hash", "canonical_signal_rule_hash"):
        if spec.get(field) is not None and spec[field] != canonical:
            return "canonical_signal_hash_mismatch"
    return None


def audit_frozen_rows(requested, rows, *, profile="production", flags=None,
                      require_existing=False):
    """Collect every selected failure; never mutate definitions or the store."""
    from strategy_lab_service import resume_frozen_specs

    requested = copy.deepcopy(requested)
    rows = copy.deepcopy(rows)
    ids = [spec["strategy_id"] for spec in requested]
    selected_ids = set(ids)
    failures = []
    duplicates = [sid for sid, count in Counter(ids).items() if count > 1]
    for sid in duplicates:
        failures.append({"strategy_id": sid, "code": "duplicate_requested_identity"})
    row_ids = [row["strategy_id"] for row in rows]
    for sid, count in Counter(row_ids).items():
        if count > 1 and sid in selected_ids:
            failures.append({"strategy_id": sid, "code": "duplicate_persisted_identity"})
    stored = {row["strategy_id"]: row for row in rows}
    checked, new = [], []
    for request in requested:
        sid = request["strategy_id"]
        row = stored.get(sid)
        if row is None:
            new.append(sid)
            spec = request
            if require_existing:
                failures.append({"strategy_id": sid, "code": "required_account_not_persisted"})
        else:
            checked.append(sid)
            spec = row["definition"]
        try:
            code = _check_definition(spec, row)
        except Exception as exc:
            code = _exception_code(exc)
        if code:
            failures.append({"strategy_id": sid, "code": code})
    # Verify the exact restart contract too. This is a SELECT against an
    # in-memory snapshot, so resume_frozen_specs cannot initialize or write DB.
    deferred = []
    resume_ok = False
    try:
        resumed, deferred = resume_frozen_specs(_SnapshotStore(rows), requested)
        resume_ok = [spec["strategy_id"] for spec in resumed] == ids
        if not resume_ok:
            failures.append({"strategy_id": None, "code": "resume_population_mismatch"})
    except Exception as exc:
        failures.append({"strategy_id": None, "code": "resume_" + _exception_code(exc)})
    skipped = [{"strategy_id": row["strategy_id"],
                "group": population_group(row["definition"] if
                                          isinstance(row.get("definition"), dict) else row),
                "reason": "outside_requested_lab_population"}
               for row in rows if row["strategy_id"] not in selected_ids]
    return {
        "method": METHOD, "ok": not failures, "mode": "postgresql_read_only",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile, "flags": dict(flags or {}),
        "read_only": True, "resume_contract_passed": resume_ok,
        "population": {"requested": len(requested), "stored_total": len(rows),
                       "selected_frozen": len(checked), "new_not_persisted": len(new),
                       "skipped": len(skipped),
                       "requested_by_group": dict(Counter(map(population_group, requested))),
                       "skipped_by_group": dict(Counter(item["group"] for item in skipped))},
        "selected_strategy_ids": checked, "new_not_persisted": new,
        "identity_changes_deferred": deferred, "skipped": skipped,
        "failures": failures,
    }


def canonical_fixture():
    """Fixed historical s07 factorial anchor, verified against HEAD 87a9aaf.

    The literal inner hash is essential: generating all expected hashes with
    the new implementation would miss the canonical mismatch that blocked a
    real lab restart. These are account semantics, not market/outcome data.
    """
    return {
        "strategy_id": "s07_surge_continuation_B15m_R50_k1.5_100x_maker",
        "canonical_strategy_id": "s07_surge_continuation",
        "factorial_design_version": "atr-risk-full-factorial-v1",
        "risk_sizing_mode": "entry_atr", "max_loss_per_trade_usdt": 50,
        "params": {"H": 4, "I": 0.3, "volume_window": 20,
                   "min_volume_multiple": 0.6, "atr_window": 14,
                   "risk_stop_atr_mult": 1.5},
        "timeframe": "15m", "min_warmup_bars": 100,
        "universe": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
        "execution_universe": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
        "research_universe": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
        "data_universe": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
        "position_budget": {"leverage": 100}, "execution_enabled": True,
        "execution_blockers": [], "blocked_execution_reason": None,
        "signal_rule_hash": "0625c492b19ca997b4ba7ae2b5aaffaf5d669f3b692f1d4e5d39f66ac73c3120",
        "rule_hash": "72aca420f080c4dcba8378cdf0ef116bb2e13e2e9ab5c058ed8fa8f2bc1384bb",
    }


def check_ci_population_anchors():
    """Compare every generated identity to fixed compatible-population hashes."""
    from strategy_simulation import execution_identity

    requested = build_requested_specs(population_flags(risk_execution_2x2=True))
    groups = {name: [] for name in CI_IDENTITY_ANCHORS}
    for spec in requested:
        identity = execution_identity(spec)
        groups[population_group(spec)].append({
            "strategy_id": spec["strategy_id"], "rule_hash": identity["rule_hash"],
            "canonical_signal_hash": spec.get("signal_rule_hash", spec["rule_hash"]),
            "execution_hash": identity["execution_hash"],
        })
    results = {}
    for group, rows in groups.items():
        rows.sort(key=lambda row: row["strategy_id"])
        encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        actual_hash = hashlib.sha256(encoded).hexdigest()
        expected_count, expected_hash = CI_IDENTITY_ANCHORS[group]
        results[group] = {"count": len(rows), "expected_count": expected_count,
                          "identity_digest": actual_hash, "expected_digest": expected_hash,
                          "passed": len(rows) == expected_count and actual_hash == expected_hash}
    return results


def run_ci_contracts():
    """Pure fixtures/contracts only: no credentials, network or PostgreSQL."""
    from strategy_lab_service import normalize_account_spec

    fixture = canonical_fixture()
    row = {"strategy_id": fixture["strategy_id"], "definition": fixture,
           "rule_hash": fixture["rule_hash"],
           "execution_hash": "855b27da21fa3526d2993095dad4a68ec6d3ddb7d6daba68c0235755a0fd0fb9"}
    # A normalized request is what the actual lab sends to resume_frozen_specs.
    try:
        request = {**fixture, **normalize_account_spec(fixture)}
        anchor = audit_frozen_rows([request], [row])
    except Exception as exc:
        anchor = {"ok": False, "failures": [{"strategy_id": fixture["strategy_id"],
                                             "code": _exception_code(exc)}]}
    try:
        population_anchors = check_ci_population_anchors()
        populations_ok = all(item["passed"] for item in population_anchors.values())
    except Exception as exc:
        population_anchors = {"build_failure": {"passed": False, "code": _exception_code(exc)}}
        populations_ok = False
    names = (
        "tests.test_strategy_frozen_resume.FrozenStrategyResumeTests",
        "tests.test_strategy_factorial_signals.FactorialIdentityTests",
        "tests.test_strategy_lab_2x2_loader.StrategyLab2x2LoaderTests",
        "tests.test_strategy_fnew.FnewDefinitionTests",
        "tests.test_strategy_fee_schedule.StrategyFeeScheduleTests",
    )
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        suite = unittest.TestLoader().loadTestsFromNames(names)
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    # Raw tracebacks may include arbitrary exception strings. Test identifiers
    # are enough to locate a failure; rerun its normal unittest command locally.
    failed_tests = [test.id() for test, _ in (*result.failures, *result.errors)]
    return {"method": METHOD, "mode": "ci_offline", "read_only": True,
            "ok": anchor["ok"] and populations_ok and result.wasSuccessful() and not result.skipped,
            "baseline_commit": CI_BASELINE_COMMIT, "population_anchors": population_anchors,
            "canonical_anchor_passed": anchor["ok"],
            "anchor_failures": anchor["failures"], "tests_run": result.testsRun,
            "failed_tests": failed_tests, "skipped_tests": len(result.skipped),
            "skipped_test_ids": [test.id() for test, _ in result.skipped],
            "database_queried": False, "contracts": list(names)}


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ci", action="store_true", help="run DB-free frozen contracts")
    parser.add_argument("--json", action="store_true", help="report on stdout as JSON")
    parser.add_argument("--profile", choices=tuple(PROFILES), default="production")
    for flag in PROFILES["production"]:
        parser.add_argument("--" + flag.replace("_", "-"),
                            action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--require-existing", action="store_true")
    parser.add_argument("--schema", default="quant")
    return parser


def main(argv=None):
    args = make_parser().parse_args(argv)
    stage = "ci_contracts" if args.ci else "population_flags"
    flags = None
    try:
        if args.ci:
            report = run_ci_contracts()
        else:
            flags = population_flags(args.profile, **{
                key: getattr(args, key) for key in PROFILES["production"]})
            stage = "population_build"
            requested = build_requested_specs(flags)
            stage = "database_read"
            from runtime_config import load_environment
            load_environment(ROOT)
            rows = read_frozen_rows(os.getenv("QUANT_DATABASE_URL", ""), args.schema)
            stage = "identity_audit"
            report = audit_frozen_rows(requested, rows, profile=args.profile,
                                       flags=flags, require_existing=args.require_existing)
    except Exception as exc:
        diagnostic = _exception_code(exc) if stage in {"population_build", "identity_audit"} else None
        report = {"method": METHOD, "ok": False, "read_only": True,
                  "mode": "ci_offline" if args.ci else "postgresql_read_only",
                  "profile": args.profile, "flags": dict(flags or {}),
                  "failure_stage": stage,
                  "error_code": diagnostic if diagnostic and diagnostic != "identity_validation_failed"
                                else stage + "_failed",
                  "error_class": type(exc).__name__}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    elif args.ci:
        print(f"frozen identities CI: ok={report['ok']} "
              f"tests={report.get('tests_run', 0)} "
              f"canonical_anchor={report.get('canonical_anchor_passed', False)}")
        for group, anchor in report.get("population_anchors", {}).items():
            print(f"{group}: count={anchor.get('count', 0)} passed={anchor['passed']}")
        for failure in report.get("anchor_failures", []):
            print(f"FAIL {failure['strategy_id']}: {failure['code']}")
        for test_id in report.get("failed_tests", []):
            print(f"FAIL {test_id}")
        for test_id in report.get("skipped_test_ids", []):
            print(f"FAIL skipped_contract_test: {test_id}")
    else:
        population = report.get("population", {})
        print(f"frozen identities preflight: ok={report['ok']} profile={args.profile} "
              f"requested={population.get('requested', 0)} "
              f"selected={population.get('selected_frozen', 0)} "
              f"new={population.get('new_not_persisted', 0)} "
              f"skipped={population.get('skipped', 0)}")
        print("flags=" + json.dumps(report.get("flags", {}), sort_keys=True))
        print("requested_by_group=" + json.dumps(population.get("requested_by_group", {}), sort_keys=True))
        print("skipped_by_group=" + json.dumps(population.get("skipped_by_group", {}), sort_keys=True))
        print(f"identity_changes_deferred={len(report.get('identity_changes_deferred', []))}")
        for failure in report.get("failures", []):
            print(f"FAIL {failure['strategy_id'] or 'resume'}: {failure['code']}")
    if report.get("error_code"):
        print(f"FAIL {report['error_code']} ({report['error_class']})", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

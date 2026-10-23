#!/usr/bin/env python3
"""Run one explicitly authorized, isolated Binance Demo open/close test.

Pause the default Demo runner and stop the auth service before invoking this
operator command. It acquires the same account-level lock and never starts the
strategy runner. Credentials are loaded only through the encrypted provider.
Use the same --session to reconcile an interrupted test; never invent a new
session while an earlier submission or position remains unresolved.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from binance_demo_adapter import BinanceDemoAdapter  # noqa: E402
from data_store import DataStore  # noqa: E402
from demo_api import create_demo_api  # noqa: E402
from demo_smoke_trade import VERSION, _hash, run_smoke_trade  # noqa: E402
from demo_store import DemoStore  # noqa: E402
from runtime_config import load_environment  # noqa: E402

_IDENTITY_VERSION = "manual-smoke-operator-v1"
_RISK_GATE_IDENTITY_FIELDS = ("research_id", "event_id", "boundary_hash", "inputs_hash",
                              "target_binding_hash")


def _expected_target_binding(account_scope):
    import research_ledger

    rule = {"version": VERSION, "purpose": "manual_smoke_test"}
    execution = {"version": VERSION, "max_notional_usdt": "75",
                 "entry_wait_seconds": 15, "reconciliation_seconds": 60,
                 "entry_post_only": False, "exit_reduce_only": True}
    return research_ledger.build_risk_gate_target_binding(account_scope, [{
        "strategy_id": "manual_smoke_test",
        "rule_hash": _hash(rule),
        "execution_hash": _hash(execution),
    }])


def _risk_gate_identity(risk_gate):
    if not isinstance(risk_gate, dict):
        return None
    identity = {key: risk_gate.get(key) for key in _RISK_GATE_IDENTITY_FIELDS}
    if any(not isinstance(value, str) or not value.strip() for value in identity.values()):
        raise ValueError("risk_gate_identity_invalid")
    return identity


def _validate_smoke_identity(saved, session, expected_binding):
    if (not isinstance(saved, dict) or saved.get("version") != _IDENTITY_VERSION
            or saved.get("environment") != "binance_demo" or saved.get("session") != session):
        raise ValueError("manual_smoke_session_identity_mismatch")
    if saved.get("expected_target_binding") != expected_binding:
        raise ValueError("manual_smoke_session_identity_mismatch")
    identity = saved.get("risk_gate_identity")
    gate = saved.get("risk_gate")
    if identity is None:
        if saved.get("research_id") is not None or gate is not None:
            raise ValueError("manual_smoke_session_identity_mismatch")
        return None, None, None
    if (_risk_gate_identity(gate) != identity
            or saved.get("research_id") != identity.get("research_id")):
        raise ValueError("manual_smoke_session_identity_mismatch")
    return saved.get("research_id"), gate, identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--execute", action="store_true",
                        help="Explicitly authorize this one Demo round trip")
    parser.add_argument("--research-id",
                        help="Closed research risk-gate identity required for Demo execution")
    parser.add_argument("--account-scope", required=True,
                        help="verified opaque account scope frozen into the risk gate")
    args = parser.parse_args()
    expected_binding = _expected_target_binding(args.account_scope)
    if not args.execute or not re.fullmatch(r"manual_smoke_[A-Za-z0-9_]{1,80}", args.session):
        parser.error("--execute and a unique manual_smoke_ session are required")
    load_environment(ROOT)
    import os
    auth_dir = Path(os.environ.get("QUANT_AUTH_DIR", str(ROOT / "auth")))
    lock_path = ROOT / "database/runtime/binance_demo_runner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    data = DataStore(root=ROOT)
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"success": False, "errorKind": "demo_account_lock_busy"}))
            return 2
        try:
            ledger = DemoStore(data, virtual_account_key=args.session)
            existing_orders = ledger.runner_orders()
            identity_key = "binance_demo_manual_smoke/" + args.session + ".identity.json"
            saved_identity = data.get_document(identity_key)
            recovery_only = bool(existing_orders or saved_identity is not None)
            if not recovery_only:
                # A fresh session may not hide unresolved work from another
                # session, even when the venue currently appears flat.
                with data._connection() as conn:
                    pending = conn.execute("SELECT COUNT(*) AS n FROM demo_orders WHERE "
                        "environment='binance_demo' AND virtual_account_key<>%s AND "
                        "status IN ('intent','submitting','unknown','open','partially_filled')",
                        (args.session,)).fetchone()["n"]
                    exposure = conn.execute("SELECT COUNT(*) AS n FROM (SELECT f.virtual_account_key,"
                        "f.pair,SUM(CASE WHEN o.side='buy' THEN f.quantity ELSE -f.quantity END) AS qty "
                        "FROM demo_fills f JOIN demo_orders o USING "
                        "(environment,virtual_account_key,client_id) WHERE f.environment='binance_demo' "
                        "AND f.virtual_account_key<>%s GROUP BY f.virtual_account_key,f.pair "
                        "HAVING SUM(CASE WHEN o.side='buy' THEN f.quantity ELSE -f.quantity END)<>0) s",
                        (args.session,)).fetchone()["n"]
                if pending or exposure:
                    print(json.dumps({"success": False, "errorKind": "other_demo_session_unresolved",
                                      "otherPendingOrders": pending, "otherPositionGroups": exposure}))
                    return 2
            risk_gate, risk_gate_identity, research_id = None, None, None
            if saved_identity is not None:
                research_id, risk_gate, risk_gate_identity = _validate_smoke_identity(
                    saved_identity, args.session, expected_binding)
            elif not existing_orders:
                if not isinstance(args.research_id, str) or not args.research_id.strip():
                    print(json.dumps({"success": False, "errorKind": "risk_gate_not_closed"}))
                    return 2
                import research_ledger
                try:
                    risk_gate = research_ledger.require_closed_risk_gate(
                        data, args.research_id, expected_binding=expected_binding)
                    risk_gate_identity = _risk_gate_identity(risk_gate)
                except Exception:
                    print(json.dumps({"success": False, "errorKind": "risk_gate_not_closed"}))
                    return 2
                research_id = args.research_id
                saved_identity = {"version": _IDENTITY_VERSION, "environment": "binance_demo",
                    "session": args.session, "research_id": research_id, "risk_gate": risk_gate,
                    "risk_gate_identity": risk_gate_identity,
                    "expected_target_binding": expected_binding,
                    "credential_fingerprint": None}
            api = create_demo_api(data, auth_dir=auth_dir)
            credentials = api.credentials.load_credentials()
            fingerprint = hashlib.sha256(credentials.api_key.encode()).hexdigest()[:16]
            previous_fingerprint = (saved_identity or {}).get("credential_fingerprint")
            if previous_fingerprint is not None and previous_fingerprint != fingerprint:
                del credentials
                print(json.dumps({"success": False,
                                  "errorKind": "manual_smoke_credential_identity_mismatch"}))
                return 2
            if saved_identity is not None and previous_fingerprint is None:
                saved_identity = {**saved_identity, "credential_fingerprint": fingerprint}
                data.put_document(identity_key, saved_identity)
            adapter = BinanceDemoAdapter(api_key=credentials.api_key,
                                         api_secret=credentials.secret, allow_trading=True)
            del credentials
            report = run_smoke_trade(adapter, ledger, max_notional_usdt="75",
                                     entry_wait_seconds=15, reconciliation_seconds=60,
                                     recovery_only=recovery_only,
                                     risk_gate_proof=risk_gate)
            report["credential_fingerprint"] = fingerprint
            report["research_id"] = research_id
            report["risk_gate"] = risk_gate
            data.put_document("binance_demo_manual_smoke_latest.json", report)
            data.put_document("binance_demo_manual_smoke/" + args.session + ".json", report)
            summary = {key: report.get(key) for key in (
                "success", "environment", "endpoint", "experiment_scope", "virtual_account_key",
                "pair", "plan", "entry_submission_attempts", "exit_submission_attempts",
                "position_flat", "open_orders_zero", "fill_reconciliation_complete",
                "persistence_complete", "commission_by_asset", "commission_complete",
                "round_trips", "errors", "finished_ms")}
            summary["orderCount"] = len(report["orders"])
            summary["fillCount"] = len(report["fills"])
            print(json.dumps(summary, ensure_ascii=False))
            return 0 if report["success"] else 1
        except Exception as exc:
            # Never serialize exception messages, private requests or DSNs.
            print(json.dumps({"success": False, "errorKind": type(exc).__name__,
                              "session": args.session, "recoveryRequired": True}))
            return 2
        finally:
            data.close()
            fcntl.flock(handle, fcntl.LOCK_UN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

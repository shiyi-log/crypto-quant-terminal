#!/usr/bin/env python3
"""Capture actual maker/taker fees on BTC/ETH/TRX in Binance Demo only.

Before execution, pause the default Demo runner, confirm its account is flat,
and stop auth_service.py: ``uv run python manage.py stop auth``. This command
holds the same account-level flock; it never stops or restarts any service.
Restore auth with ``uv run python manage.py start auth`` after all orders and
positions are reconciled. Its strategy entry gate remains paused.

Credentials are loaded only through the encrypted provider. An interrupted
session must be recovered with the SAME --session and configuration, preferably
--recover-only. Never use a new session to bypass an unresolved submission.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from binance_demo_adapter import ALLOWED_PAIRS, BinanceDemoAdapter, DEMO_HOST  # noqa: E402
from data_store import DataStore  # noqa: E402
from demo_api import create_demo_api  # noqa: E402
from demo_fee_capture import run_fee_capture  # noqa: E402
from demo_store import DemoStore  # noqa: E402
from runtime_config import load_environment  # noqa: E402
from tools.run_binance_demo_second_level import (  # noqa: E402
    OperatorPreflightError, ledger_preflight,
)

IDENTITY_VERSION = "demo-fee-capture-operator-v1"


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--session", required=True,
                        help="Unique fee_capture_ ID; reuse it for interrupted-session recovery")
    result.add_argument("--execute", action="store_true",
                        help="Explicitly authorize bounded Demo trades or their protective recovery")
    result.add_argument("--max-notional-usdt", default="75")
    result.add_argument("--maker-ttl-seconds", type=int, default=30)
    result.add_argument("--total-timeout-seconds", type=int, default=600,
                        help="Maximum maker entry window per coin, at most 600 seconds")
    result.add_argument("--reconciliation-seconds", type=int, default=60)
    result.add_argument("--recover-only", action="store_true",
                        help="Reconcile/cancel old orders and protect owned positions; no fresh entry")
    return result


def config_from_args(args):
    if not args.execute:
        raise OperatorPreflightError("explicit_execute_required")
    if not re.fullmatch(r"fee_capture_[A-Za-z0-9_]{1,80}", args.session):
        raise OperatorPreflightError("invalid_fee_capture_session")
    try:
        cap = Decimal(str(args.max_notional_usdt))
        if isinstance(args.max_notional_usdt, bool) or not cap.is_finite() or not 0 < cap <= 75:
            raise ValueError
    except (InvalidOperation, ValueError, TypeError):
        raise OperatorPreflightError("invalid_fee_capture_notional_cap") from None
    for value, lower, upper in ((args.maker_ttl_seconds, 30, 60),
                                (args.total_timeout_seconds, args.maker_ttl_seconds, 600),
                                (args.reconciliation_seconds, 1, 120)):
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise OperatorPreflightError("invalid_fee_capture_timing")
    return {"pairs": list(ALLOWED_PAIRS), "max_notional_usdt": format(cap.normalize(), "f"),
            "maker_ttl_seconds": args.maker_ttl_seconds,
            "total_timeout_seconds": args.total_timeout_seconds,
            "reconciliation_seconds": args.reconciliation_seconds}


def run_operator(args, data, *, auth_dir, clock_ms=None, api_factory=None,
                 adapter_factory=None, ledger_factory=None, capture=None):
    """Caller holds the execution flock; injected dependencies stay offline."""
    config = config_from_args(args)
    now = clock_ms or (lambda: int(time.time() * 1000))
    started_ms = now()
    # Reuse the all-history read-only gate, including terminal fill gaps and
    # per-namespace/pair exposure. A flat venue cannot hide another ledger gap.
    preflight = ledger_preflight(data, [args.session])
    prefix = "binance_demo_fee_capture/" + args.session
    identity_key = prefix + ".identity.json"
    identity = {"version": IDENTITY_VERSION, "environment": "binance_demo",
                "endpoint_host": DEMO_HOST, "session": args.session, "config": config}
    saved = data.get_document(identity_key)
    if saved is not None:
        if not isinstance(saved, dict) or any(saved.get(key) != value for key, value in identity.items()):
            raise OperatorPreflightError("fee_capture_session_identity_mismatch")
    elif preflight["sessionOrderCount"] or args.recover_only:
        raise OperatorPreflightError("fee_capture_session_identity_missing")

    api = (api_factory or create_demo_api)(data, auth_dir=auth_dir)
    credentials = api.credentials.load_credentials()
    # Use the same loaded API key given to the adapter, never a second status
    # read that could race credential replacement. No secret is persisted.
    fingerprint = hashlib.sha256(credentials.api_key.encode()).hexdigest()[:16]
    if saved is not None and saved.get("credential_fingerprint") != fingerprint:
        del credentials
        raise OperatorPreflightError("fee_capture_credential_identity_mismatch")
    if saved is None:
        saved = {**identity, "credential_fingerprint": fingerprint, "started_ms": started_ms}
        data.put_document(identity_key, saved)
    adapter = (adapter_factory or BinanceDemoAdapter)(api_key=credentials.api_key,
        api_secret=credentials.secret, allow_trading=True)
    del credentials
    ledger = (ledger_factory or DemoStore)(data, virtual_account_key=args.session)
    report = (capture or run_fee_capture)(adapter, ledger, **config,
                                         recovery_only=args.recover_only, clock_ms=clock_ms)
    report["credential_fingerprint"] = fingerprint
    report["operator_started_ms"] = started_ms
    report["operator_ledger_preflight"] = preflight
    report_key = prefix + "/reports/" + str(now()) + "_" + uuid.uuid4().hex + ".json"
    report["report_document"] = report_key
    # Preserve each invocation before updating its convenient latest pointer.
    data.put_document(report_key, report)
    data.put_document(prefix + ".json", report)
    return report


def print_report(report):
    """One JSON line per actual fill followed by a redacted report summary."""
    fields = ("pair", "order_id", "client_order_id", "fill_id", "side", "price", "quantity",
              "commission", "commission_asset", "is_maker", "timestamp_ms", "transactTime",
              "order_type", "time_in_force", "post_only", "reduce_only", "rate", "rate_status",
              "quoted_rate", "matches_quote")
    for sample in report.get("samples", []):
        print(json.dumps({"kind": "fill", **{key: sample.get(key) for key in fields}},
                         ensure_ascii=False, allow_nan=False))
    keys = ("success", "environment", "endpoint", "virtual_account_key", "recovery_only",
            "position_flat", "open_orders_zero", "fill_reconciliation_complete",
            "persistence_complete", "recoveryRequired", "fee_summary", "maker_actual_fill_verified",
            "errors", "finished_ms", "report_document")
    summary = {key: report.get(key) for key in keys}
    summary.update(kind="summary", orderCount=len(report.get("orders", [])),
                   fillCount=len(report.get("fills", [])))
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    try:
        config_from_args(args)
    except OperatorPreflightError as exc:
        cli.error(exc.kind)
    load_environment(ROOT)
    auth_dir = Path(os.environ.get("QUANT_AUTH_DIR", str(ROOT / "auth")))
    lock_path = ROOT / "database/runtime/binance_demo_runner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    data = None
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"success": False, "errorKind": "demo_account_lock_busy"}))
            return 2
        try:
            data = DataStore(root=ROOT)
            report = run_operator(args, data, auth_dir=auth_dir)
            print_report(report)
            return 0 if report.get("success") else 1
        except OperatorPreflightError as exc:
            print(json.dumps({"success": False, "errorKind": exc.kind, "session": args.session,
                              **exc.facts}, ensure_ascii=False))
            return 2
        except KeyboardInterrupt:
            print(json.dumps({"success": False, "errorKind": "operator_interrupted",
                              "session": args.session, "recoveryRequired": True}))
            return 130
        except Exception as exc:
            # Never print exception messages, DSNs, signed requests or secrets.
            print(json.dumps({"success": False, "errorKind": type(exc).__name__,
                              "session": args.session, "recoveryRequired": True}))
            return 2
        finally:
            try:
                if data is not None:
                    data.close()
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


if __name__ == "__main__":
    raise SystemExit(main())

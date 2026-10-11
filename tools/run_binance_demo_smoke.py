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
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from binance_demo_adapter import BinanceDemoAdapter  # noqa: E402
from data_store import DataStore  # noqa: E402
from demo_api import create_demo_api  # noqa: E402
from demo_smoke_trade import run_smoke_trade  # noqa: E402
from demo_store import DemoStore  # noqa: E402
from runtime_config import load_environment  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--execute", action="store_true",
                        help="Explicitly authorize this one Demo round trip")
    args = parser.parse_args()
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
            # A fresh session may not hide unknown submissions or attributed
            # exposure left by another session, even when the venue looks flat.
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
            api = create_demo_api(data, auth_dir=auth_dir)
            credentials = api.credentials.load_credentials()
            adapter = BinanceDemoAdapter(api_key=credentials.api_key,
                                         api_secret=credentials.secret, allow_trading=True)
            del credentials
            ledger = DemoStore(data, virtual_account_key=args.session)
            report = run_smoke_trade(adapter, ledger, max_notional_usdt="75",
                                     entry_wait_seconds=15, reconciliation_seconds=60)
            report["credential_fingerprint"] = api.credentials.status()["fingerprint"]
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

#!/usr/bin/env python3
"""Run a finite, explicitly authorized Binance Demo second-level experiment.

The caller must pause other Demo writers and release the account execution lock
before invoking this command. It never stops, restarts or starts a service.
Credentials come only from the existing encrypted application provider. Reuse
the same --session to recover an interrupted experiment; an existing session
can only reconcile/protect its frozen orders and cannot add new signals.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from binance_demo_adapter import BinanceDemoAdapter, DEMO_HOST  # noqa: E402
from data_store import DataStore  # noqa: E402
from demo_api import create_demo_api  # noqa: E402
from demo_second_level_runner import (  # noqa: E402
    SecondLevelConfig, recover_second_level_session, run_second_level_session,
)
from demo_second_level_signals import (  # noqa: E402
    FrozenSecondLevelSignalSource, POLICY_HASH, latest_closed_snapshot,
)
from demo_store import DemoStore  # noqa: E402
from runtime_config import load_environment  # noqa: E402

_CHECKPOINT_VERSION = "second-level-operator-checkpoint-v1"
_PAIR_NAMES = {"BTC": "BTC/USDT:USDT", "ETH": "ETH/USDT:USDT", "TRX": "TRX/USDT:USDT"}


class OperatorPreflightError(Exception):
    """A non-secret operator error; no external exception text is retained."""

    def __init__(self, kind, **facts):
        self.kind, self.facts = kind, facts
        super().__init__(kind)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--session", required=True,
                        help="Caller-chosen immutable experiment ID (1-64 alphanumeric, _ or -)")
    result.add_argument("--execute", action="store_true",
                        help="Explicitly authorize a bounded Demo experiment or its protection recovery")
    result.add_argument("--pair", choices=tuple(_PAIR_NAMES), default="BTC")
    result.add_argument("--max-signals", type=int, default=6)
    result.add_argument("--duration-seconds", type=int, default=600)
    result.add_argument("--max-notional-usdt", default="75",
                        help="Maximum quoted notional per entry, at most 75 USDT; cycles are sequential")
    result.add_argument("--recover-only", action="store_true",
                        help="Require an existing session and reconcile/protect without fresh signals")
    return result


def config_from_args(args):
    if not args.execute:
        raise OperatorPreflightError("explicit_execute_required")
    return SecondLevelConfig(session_id=args.session, max_signals=args.max_signals,
                             max_duration_seconds=args.duration_seconds,
                             max_notional_usdt=args.max_notional_usdt)


def namespaces(config):
    return {arm: "second_level_" + config.session_id + "_" + arm for arm in ("maker", "taker")}


def ledger_preflight(data, allowed_namespaces):
    """Check every other session, including missing fills on terminal orders.

    All-history checks share one read-only snapshot. An order whose reported
    execution is not fully projected into fills cannot be hidden by a zero
    net-fill sum. No credential provider or exchange request precedes this.
    """
    allowed = list(allowed_namespaces)
    with data._connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        pending = conn.execute("""SELECT COUNT(*) AS n FROM demo_orders
            WHERE environment='binance_demo' AND NOT (virtual_account_key=ANY(%s))
            AND status IN ('intent','submitting','unknown','open','partially_filled')""",
            (allowed,)).fetchone()["n"]
        exposure = conn.execute("""SELECT COUNT(*) AS n FROM (
            SELECT f.virtual_account_key,f.pair,
                SUM(CASE WHEN o.side='buy' THEN f.quantity ELSE -f.quantity END) AS qty
            FROM demo_fills f JOIN demo_orders o USING (environment,virtual_account_key,client_id)
            WHERE f.environment='binance_demo' AND NOT (f.virtual_account_key=ANY(%s))
            GROUP BY f.virtual_account_key,f.pair
            HAVING SUM(CASE WHEN o.side='buy' THEN f.quantity ELSE -f.quantity END)<>0) s""",
            (allowed,)).fetchone()["n"]
        fill_gaps = conn.execute("""SELECT COUNT(*) AS n FROM (
            SELECT o.virtual_account_key,o.client_id,o.filled_quantity
            FROM demo_orders o LEFT JOIN demo_fills f USING (environment,virtual_account_key,client_id)
            WHERE o.environment='binance_demo' AND NOT (o.virtual_account_key=ANY(%s))
            GROUP BY o.virtual_account_key,o.client_id,o.filled_quantity
            HAVING COALESCE(SUM(f.quantity),0)<>o.filled_quantity) s""",
            (allowed,)).fetchone()["n"]
        own_orders = conn.execute("""SELECT COUNT(*) AS n FROM demo_orders
            WHERE environment='binance_demo' AND virtual_account_key=ANY(%s)""",
            (allowed,)).fetchone()["n"]
    facts = {"otherPendingOrders": int(pending), "otherPositionGroups": int(exposure),
             "otherFillReconciliationGaps": int(fill_gaps), "sessionOrderCount": int(own_orders)}
    if pending or exposure or fill_gaps:
        raise OperatorPreflightError("other_demo_session_unresolved", **facts)
    return facts


def _checkpoint_identity(config, pair):
    return {"format_version": _CHECKPOINT_VERSION, "environment": "binance_demo",
            "endpoint_host": DEMO_HOST, "session_id": config.session_id, "pair": pair,
            "config": asdict(config), "signal_policy_hash": POLICY_HASH}


class CheckpointedSource:
    """Commit source high-water/arm state before the runner sees a decision."""

    def __init__(self, source, data, key, checkpoint):
        self.source, self.data, self.key = source, data, key
        self.checkpoint = deepcopy(checkpoint)

    @property
    def state(self):
        return self.source.state

    @property
    def metrics(self):
        return self.source.metrics

    @property
    def last_decision(self):
        return self.source.last_decision

    def latest_signal(self, now_ms):
        signal = self.source.latest_signal(now_ms)
        checkpoint = {**self.checkpoint, "state": self.source.state,
                      "last_decision": deepcopy(self.source.last_decision), "checkpoint_ms": now_ms}
        # A failed write raises before any newly emitted signal reaches a
        # reserving/submitting runner. Skipping a consumed signal is permitted.
        self.data.put_document(self.key, checkpoint)
        self.checkpoint = checkpoint
        return signal


def _nonzero_positions(positions):
    count = 0
    for row in positions:
        try:
            value = row["signed_qty"]
            if isinstance(value, bool):
                raise ValueError
            quantity = Decimal(str(value))
            if not quantity.is_finite():
                raise ValueError
        except (InvalidOperation, ValueError, KeyError, TypeError):
            raise OperatorPreflightError("invalid_demo_position_response") from None
        count += int(quantity != 0)
    return count


def _compact_report(report):
    summary = {key: report.get(key) for key in (
        "success", "environment", "endpoint_host", "session_id", "recovery_only",
        "position_flat", "open_orders_zero", "requires_operator_recovery",
        "persistence_complete", "signal_count", "source_poll_count", "source_reason_counts",
        "attempted_entry_count", "attempted_exit_count", "attempt_population", "coverage_population",
        "observed_arms", "accepted_arms", "filled_arms", "observed_maker_distances_bp",
        "errors", "stop_reason", "finished_ms")}
    summary["cycleCount"] = len(report.get("cycles", []))
    summary["cycleResults"] = [{key: cycle.get(key) for key in (
        "arm", "signal_id", "maker_distance_bp", "entry_terminal_status",
        "frozen_entry_quantity", "entry_filled_quantity", "entry_exchange_accepted", "position_flat",
        "open_orders_zero", "fill_reconciliation_complete", "fees", "round_trips")}
        for cycle in report.get("cycles", [])]
    summary["reportDocument"] = report.get("report_document")
    return summary


def run_operator(args, data, *, auth_dir, clock_ms=None, api_factory=None,
                 adapter_factory=None, ledger_factory=None, run_session=None,
                 recover_session=None, snapshot_reader=None, source_factory=None):
    """Caller holds the account lock; dependencies permit fully offline tests."""
    now = clock_ms or (lambda: int(time.time() * 1000))
    operator_started_ms = now()
    config = config_from_args(args)
    pair = _PAIR_NAMES[args.pair]
    names = namespaces(config)
    preflight = ledger_preflight(data, names.values())
    prefix = "binance_demo_second_level/" + config.session_id + "/"
    checkpoint_key = prefix + "source.json"
    saved = data.get_document(checkpoint_key)
    identity = _checkpoint_identity(config, pair)
    source_type = source_factory or FrozenSecondLevelSignalSource
    if saved is not None:
        if not isinstance(saved, dict) or any(saved.get(key) != value for key, value in identity.items()):
            raise OperatorPreflightError("second_level_session_identity_mismatch")
        start = saved.get("started_ms")
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise OperatorPreflightError("invalid_second_level_session_start")
        state = saved.get("state")
        if not isinstance(state, dict) or state.get("started_ms") != start:
            raise OperatorPreflightError("invalid_second_level_source_checkpoint")
        # Strict restoration validates the source version, exact state shape,
        # identity, counters and high-water mark without observing a new input.
        source = source_type(data, pair, state=state, started_ms=start)
        recovery_only = True
        checkpoint = saved
    else:
        if preflight["sessionOrderCount"] or args.recover_only:
            raise OperatorPreflightError("second_level_source_checkpoint_missing")
        start = now()
        source = source_type(data, pair, started_ms=start)
        checkpoint = {**identity, "started_ms": start, "state": source.state,
                      "last_decision": None, "checkpoint_ms": start,
                      "credential_fingerprint": None}
        recovery_only = False
    if not recovery_only:
        observation = (snapshot_reader or latest_closed_snapshot)(data, pair, now())
        if observation.get("quality") != "valid":
            raise OperatorPreflightError("second_level_source_unavailable",
                                         sourceReason=observation.get("reason", "unknown"))
        # Initial identity and source state commit precedes credentials and
        # exchange requests; no session gets a new start or arm schedule later.
        data.put_document(checkpoint_key, checkpoint)
    api = (api_factory or create_demo_api)(data, auth_dir=auth_dir)
    credentials = api.credentials.load_credentials()
    # Compute identity from the SAME loaded key passed to the adapter. A
    # separate status() read could race credential replacement. This matches
    # DemoCredentialStore's non-secret SHA-256/API-key fingerprint algorithm.
    fingerprint = hashlib.sha256(credentials.api_key.encode()).hexdigest()[:16]
    previous_fingerprint = checkpoint.get("credential_fingerprint")
    if previous_fingerprint is not None and previous_fingerprint != fingerprint:
        del credentials
        raise OperatorPreflightError("second_level_credential_identity_mismatch")
    if previous_fingerprint is None:
        if preflight["sessionOrderCount"]:
            del credentials
            raise OperatorPreflightError("second_level_credential_identity_missing")
        checkpoint = {**checkpoint, "credential_fingerprint": fingerprint}
        # This immutable account identity is committed before adapter creation
        # and before the first exchange request, including a recovery lookup.
        data.put_document(checkpoint_key, checkpoint)
    adapter = (adapter_factory or BinanceDemoAdapter)(api_key=credentials.api_key,
        api_secret=credentials.secret, allow_trading=True)
    del credentials
    ledgers = {arm: (ledger_factory or DemoStore)(data, virtual_account_key=name)
               for arm, name in names.items()}
    if not recovery_only:
        if adapter.fetch_position_mode().get("hedged") is not False:
            raise OperatorPreflightError("second_level_requires_one_way_mode")
        positions, orders = adapter.fetch_positions(), adapter.fetch_open_orders()
        nonzero = _nonzero_positions(positions)
        if nonzero or orders:
            raise OperatorPreflightError("demo_account_not_flat", nonzeroPositions=nonzero,
                                         openOrderCount=len(orders))
    wrapped = CheckpointedSource(source, data, checkpoint_key, checkpoint)
    if recovery_only:
        report = (recover_session or recover_second_level_session)(adapter, ledgers, config=config)
    else:
        report = (run_session or run_second_level_session)(adapter, ledgers, wrapped, config=config)
    report["credential_fingerprint"] = fingerprint
    report["operator_started_ms"] = operator_started_ms
    report["source_session_started_ms"] = start
    report["signal_source_state"] = wrapped.state
    report["operator_ledger_preflight"] = preflight
    report["virtual_account_keys"] = names
    report_key = prefix + "reports/" + str(now()) + "_" + uuid.uuid4().hex[:12] + ".json"
    report["report_document"] = report_key
    data.put_document(report_key, report)
    data.put_document(prefix + "report.json", report)
    data.put_document("binance_demo_second_level_latest.json", report)
    return report


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    try:
        config_from_args(args)
    except (OperatorPreflightError, ValueError) as exc:
        cli.error(exc.kind if isinstance(exc, OperatorPreflightError) else "invalid second-level finite configuration")
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
            print(json.dumps(_compact_report(report), ensure_ascii=False))
            return 0 if report.get("success") else 1
        except OperatorPreflightError as exc:
            print(json.dumps({"success": False, "errorKind": exc.kind, "session": args.session,
                              **exc.facts}, ensure_ascii=False))
            return 2
        except Exception as exc:
            # Never serialize external exception text, signatures, requests or
            # DSNs. Existing durable rows are the sole recovery authority.
            print(json.dumps({"success": False, "errorKind": type(exc).__name__,
                              "session": args.session, "recoveryRequired": True}))
            return 2
        finally:
            if data is not None:
                data.close()
            fcntl.flock(handle, fcntl.LOCK_UN)


if __name__ == "__main__":
    raise SystemExit(main())

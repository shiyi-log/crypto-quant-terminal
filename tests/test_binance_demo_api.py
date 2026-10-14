"""Demo HTTP guards, encrypted config lifecycle and Binance payload projection."""

from contextlib import redirect_stderr
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from binance_demo_credentials import DemoCredentials
from demo_api import DemoApi, DemoApiError, MAX_BODY_BYTES, PREFIX, handle_demo_request
from demo_runner_controller import DemoHandoffError


ORIGIN = "http://127.0.0.1:8888"


class DemoHttpTests(unittest.TestCase):
    def handler(self, suffix="", body=None):
        handler = Mock()
        handler.path = PREFIX + suffix
        raw = json.dumps(body if body is not None else {}).encode()
        handler.headers = {"Origin": ORIGIN, "Content-Type": "application/json",
                           "Content-Length": str(len(raw))}
        handler._bearer.return_value = "bearer"
        handler._is_local.return_value = True
        handler._read_body.return_value = raw
        handler._send.side_effect = lambda code, payload: (code, payload)
        return handler

    def route(self, handler, method="POST", username="admin"):
        self.provider = Mock()
        self.provider.return_value.post.return_value = {"ok": True}
        self.provider.return_value.get.return_value = {"ok": True}
        return handle_demo_request(handler, method=method,
                                   verify_token=lambda _: username,
                                   allowed_origins={ORIGIN}, api_provider=self.provider)

    def test_authentication_origin_local_and_size_guard_precede_body_and_database(self):
        for alteration, expected in [("unauth", 401), ("remote", 403), ("origin", 403),
                                     ("oversized", 413), ("length", 400), ("transfer", 400),
                                     ("content", 415)]:
            with self.subTest(alteration=alteration):
                handler = self.handler("/credentials", {"apiKey": "x", "secret": "y"})
                if alteration == "remote":
                    handler._is_local.return_value = False
                if alteration == "origin":
                    handler.headers["Origin"] = "https://foreign.invalid"
                if alteration == "oversized":
                    handler.headers["Content-Length"] = str(MAX_BODY_BYTES + 1)
                if alteration == "length":
                    handler.headers["Content-Length"] = "-1"
                if alteration == "transfer":
                    handler.headers["Transfer-Encoding"] = "chunked"
                if alteration == "content":
                    handler.headers["Content-Type"] = "text/plain"
                code, _ = self.route(handler, username=None if alteration == "unauth" else "admin")
                self.assertEqual(code, expected)
                self.assertTrue(handler.close_connection)
                handler._read_body.assert_not_called()
                self.provider.assert_not_called()

    def test_unknown_fields_sandbox_urls_and_duplicate_json_are_rejected(self):
        for raw in (b'{"apiKey":"x","secret":"y","endpoint":"https://fapi.binance.com"}',
                    b'{"apiKey":"x","secret":"y","sandbox":true}',
                    b'{"apiKey":"x","apiKey":"z","secret":"y"}',
                    b'{"apiKey":"x","secret":NaN}', b'[]', b'broken'):
            with self.subTest(raw=raw):
                handler = self.handler("/credentials")
                handler.headers["Content-Length"] = str(len(raw))
                handler._read_body.return_value = raw
                self.assertEqual(self.route(handler)[0], 400)
                self.provider.assert_not_called()

    def test_start_and_get_queries_have_strict_shapes(self):
        handler = self.handler("/start", {"accountId": "s03_R50_k1.5"})
        self.assertEqual(self.route(handler)[0], 200)
        self.provider.return_value.post.assert_called_once_with(
            "/start", {"accountId": "s03_R50_k1.5"}, actor="admin")
        for suffix in ("/orders?limit=1001", "/orders?limit=2&limit=3", "/status?limit=2",
                       "/orders?endpoint=fapi.binance.com", "/anything"):
            handler = self.handler(suffix)
            handler.headers["Content-Length"] = "0"
            self.assertIn(self.route(handler, method="GET")[0], {400, 404})
            self.provider.assert_not_called()

    def test_error_text_never_returns_exception_message(self):
        handler = self.handler("/check")
        provider = Mock(side_effect=RuntimeError("request contains synthetic-secret-value"))
        with self.assertLogs("demo_api", level="ERROR") as logs:
            code, payload = handle_demo_request(handler, method="POST",
                verify_token=lambda _: "admin", allowed_origins={ORIGIN}, api_provider=provider)
        self.assertEqual(code, 503)
        self.assertNotIn("synthetic-secret-value", json.dumps(payload))
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn("synthetic-secret-value", "\n".join(logs.output))

    def test_flat_handoff_uses_authenticated_local_origin_guard_and_empty_body(self):
        for alteration, expected in (("unauth", 401), ("remote", 403), ("origin", 403),
                                     ("fields", 400)):
            with self.subTest(alteration=alteration):
                handler = self.handler("/yield-flat", {"accountId": "selected"}
                                       if alteration == "fields" else {})
                if alteration == "remote":
                    handler._is_local.return_value = False
                if alteration == "origin":
                    handler.headers["Origin"] = "https://foreign.invalid"
                self.assertEqual(self.route(handler, username=None if alteration == "unauth"
                                            else "admin")[0], expected)
                self.provider.assert_not_called()
        handler = self.handler("/yield-flat")
        self.assertEqual(self.route(handler)[0], 200)
        self.provider.return_value.post.assert_called_once_with("/yield-flat", {}, actor="admin")

    def test_switch_uses_the_same_authentication_origin_and_strict_identity_guard(self):
        for alteration, expected in (("unauth", 401), ("remote", 403), ("origin", 403),
                                     ("extra", 400), ("identity", 400)):
            with self.subTest(alteration=alteration):
                body = {"accountId": "selected"}
                if alteration == "extra":
                    body["flatten"] = True
                if alteration == "identity":
                    body["accountId"] = "../other"
                handler = self.handler("/switch", body)
                if alteration == "remote":
                    handler._is_local.return_value = False
                if alteration == "origin":
                    handler.headers["Origin"] = "https://foreign.invalid"
                self.assertEqual(self.route(handler, username=None if alteration == "unauth"
                                            else "admin")[0], expected)
                self.provider.assert_not_called()
        self.assertEqual(self.route(self.handler("/switch", {"accountId": "selected"}))[0], 200)
        self.provider.return_value.post.assert_called_once_with("/switch", {"accountId": "selected"},
                                                               actor="admin")

    def test_history_session_is_read_only_strict_and_never_a_post_parameter(self):
        key = "demo_strategy_" + "a" * 32
        handler = self.handler("/fills?limit=7&session=" + key)
        handler.headers["Content-Length"] = "0"
        self.assertEqual(self.route(handler, method="GET")[0], 200)
        self.provider.return_value.get.assert_called_once_with("/fills", limit=7, session=key)
        for suffix in ("/fills?session=../other", "/fills?session=manual_smoke_test",
                       "/fills?session=demo_default&session=" + key,
                       "/positions?session=demo_default", "/status?session=demo_default"):
            handler = self.handler(suffix)
            handler.headers["Content-Length"] = "0"
            self.assertEqual(self.route(handler, method="GET")[0], 400)
            self.provider.assert_not_called()
        self.assertEqual(self.route(self.handler("/switch?session=" + key,
                                                {"accountId": "selected"}))[0], 400)
        self.provider.assert_not_called()

    def test_comparison_view_is_strict_and_preserves_history_and_limit(self):
        key = "demo_strategy_" + "a" * 32
        for view in ("signals", "audit"):
            with self.subTest(view=view):
                handler = self.handler("/comparisons?view=" + view + "&limit=7&session=" + key)
                handler.headers["Content-Length"] = "0"
                self.assertEqual(self.route(handler, method="GET")[0], 200)
                self.provider.return_value.get.assert_called_once_with(
                    "/comparisons", limit=7, session=key, view=view)
        for suffix in ("/comparisons?view=", "/comparisons?view=other",
                       "/comparisons?view=signals&view=audit", "/comparisons?view=SIGNALS",
                       "/orders?view=signals", "/status?view=audit",
                       "/comparisons?view=signals&limit=0"):
            with self.subTest(suffix=suffix):
                handler = self.handler(suffix)
                handler.headers["Content-Length"] = "0"
                self.assertEqual(self.route(handler, method="GET")[0], 400)
                self.provider.assert_not_called()
        handler = self.handler("/comparisons")
        handler.headers["Content-Length"] = "0"
        self.assertEqual(self.route(handler, method="GET")[0], 200)
        self.provider.return_value.get.assert_called_once_with("/comparisons", limit=100)


class DemoApiTests(unittest.TestCase):
    def setUp(self):
        self.data = Mock()
        self.data.get_document.return_value = None
        self.credentials = Mock()
        self.credentials.status.return_value = {"configured": True, "fingerprint": "old"}
        self.ledger = Mock()
        self.ledger.metrics.return_value = {"orders_by_status": {}, "terminal_entry_count": 0,
                                           "unfilled_entry_count": 0, "unfilled_entry_ratio": None}
        self.ledger.net_fill_positions.return_value = {}
        self.ledger.list_orders.return_value = []
        self.ledger.get_latest_account_snapshot.return_value = None
        self.ledger.get_latest_position_snapshot.return_value = None
        self.ledger.get_binding.return_value = None
        self.controller = Mock()
        self.controller.status.return_value = {"running": False, "entriesEnabled": False,
                                              "status": "not_started", "controllerError": None}
        self.adapter_factory = Mock()
        self.adapter_factory.return_value.fetch_commission_rate.side_effect = lambda pair: {
            "symbol": pair.split("/")[0] + "USDT",
            "makerCommissionRate": "0.00001200", "takerCommissionRate": "0.00004500"}
        self.api = DemoApi(self.data, self.credentials, self.ledger, self.controller,
                           adapter_factory=self.adapter_factory,
                           account_provider=lambda: [{"accountId": "selected", "eligible": True}])

    def test_status_is_exchange_free_and_preserves_errors(self):
        self.controller.status.return_value = {"running": True, "entriesEnabled": True,
            "status": "reconciliation_error", "controllerError": None, "last_error": "RequestTimeout"}
        result = self.api.get("")
        self.assertEqual(result["runner"]["state"], "reconciliation_error")
        self.assertEqual(result["runner"]["lastError"], "RequestTimeout")
        self.adapter_factory.assert_not_called()
        self.credentials.load_credentials.assert_not_called()

    def test_evidence_get_reads_database_evidence_without_network_or_credentials(self):
        reader = Mock(return_value={"environment": "binance_demo", "questions": {"live": "unknown"}})
        self.ledger.virtual_account_key = "selected-demo-ledger"
        with patch.dict("sys.modules", {"demo_evidence": Mock(read_demo_evidence=reader)}):
            result = self.api.get("/evidence")
        reader.assert_called_once_with(self.data, virtual_account_key="selected-demo-ledger")
        self.assertEqual(result["questions"]["live"], "unknown")
        self.adapter_factory.assert_not_called()
        self.credentials.load_credentials.assert_not_called()

    def test_durable_exposure_unknown_submission_and_worker_block_replacing_credentials(self):
        for condition in ("position", "unknown", "worker"):
            with self.subTest(condition=condition):
                self.ledger.net_fill_positions.return_value = {"BTC/USDT:USDT": "1"} if condition == "position" else {}
                self.ledger.metrics.return_value = {"orders_by_status": {"unknown": 1}} if condition == "unknown" else {"orders_by_status": {}}
                self.controller.status.return_value = {"running": condition == "worker", "entriesEnabled": False,
                                                       "status": "paused_protecting" if condition == "worker" else "not_started"}
                with self.assertRaisesRegex(DemoApiError, "pause_and_reconcile"):
                    self.api.post("/credentials", {"apiKey": "new", "secret": "new"}, actor="admin")
                self.credentials.save_credentials.assert_not_called()

    def test_old_flat_ledger_or_snapshot_remains_bound_to_api_fingerprint(self):
        for kind in ("orders", "account", "positions"):
            with self.subTest(kind=kind):
                self.ledger.list_orders.return_value = [{"closed": True}] if kind == "orders" else []
                self.ledger.get_latest_account_snapshot.return_value = {} if kind == "account" else None
                self.ledger.get_latest_position_snapshot.return_value = {} if kind == "positions" else None
                with self.assertRaisesRegex(DemoApiError, "bound_to_existing"):
                    self.api.post("/credentials", {"apiKey": "new", "secret": "new"}, actor="admin")
                self.credentials.save_credentials.assert_not_called()

    def test_same_key_can_rotate_secret_when_flat_and_status_check_is_invalidated(self):
        key = "synthetic-demo-api-value"
        self.credentials.status.return_value = {"configured": True,
            "fingerprint": hashlib.sha256(key.encode()).hexdigest()[:16]}
        self.api.post("/credentials", {"apiKey": key, "secret": "synthetic-secret-value"}, actor="admin")
        self.credentials.save_credentials.assert_called_once_with(key, "synthetic-secret-value", actor="admin")
        self.data.put_document.assert_called_once_with("binance_demo_connection_check.json", None)

    def test_signed_check_is_readonly_and_snapshots_are_stored(self):
        self.credentials.load_credentials.return_value = DemoCredentials("synthetic-key", "synthetic-secret")
        adapter = self.adapter_factory.return_value
        adapter.fetch_balance.return_value = {"raw": {"assets": [{"asset": "USDT", "walletBalance": "10"}]}}
        adapter.fetch_positions.return_value = [{"raw": {"symbol": "BTCUSDT", "positionAmt": "0"}}]
        result = self.api.post("/check", {}, actor="admin")
        self.assertTrue(result["connected"])
        self.adapter_factory.assert_called_once_with(api_key="synthetic-key", api_secret="synthetic-secret", allow_trading=False)
        self.ledger.append_account_snapshot.assert_called_once()
        self.ledger.append_position_snapshot.assert_called_once()
        self.assertEqual(result["feeVerification"], "requires_actual_fills")
        self.assertEqual(result["commissionRateSource"], "demo_signed_account_commission_rate_quote_not_fill")
        self.assertEqual(result["commissionRateObservedAt"], result["checkedAt"])
        self.assertEqual([row["symbol"] for row in result["commissionRates"]], ["BTCUSDT", "ETHUSDT", "TRXUSDT"])
        self.assertEqual(result["commissionRates"][0]["makerCommissionRate"], "0.00001200")
        self.assertEqual(result["commissionRates"][0]["takerCommissionRate"], "0.00004500")
        self.assertEqual(adapter.fetch_commission_rate.call_count, 3)
        account_payload = self.ledger.append_account_snapshot.call_args.args[0]
        self.assertEqual(account_payload["commissionRates"], result["commissionRates"])
        self.assertEqual(account_payload["commissionRateSource"], result["commissionRateSource"])
        self.assertNotIn("commissionRates", self.ledger.append_position_snapshot.call_args.args[0])
        self.assertNotIn("synthetic-secret", json.dumps(result))

    def test_missing_or_malformed_account_quote_never_reports_success_or_assumed_fee(self):
        self.credentials.load_credentials.return_value = DemoCredentials("synthetic-key", "synthetic-secret")
        adapter = self.adapter_factory.return_value
        adapter.fetch_balance.return_value = {"raw": {"assets": []}}
        adapter.fetch_positions.return_value = []
        valid = {"symbol": "BTCUSDT", "makerCommissionRate": "0.00001200",
                 "takerCommissionRate": "0.00004500"}
        for malformed in (None, {}, {**valid, "symbol": "ETHUSDT"},
                          {**valid, "makerCommissionRate": None},
                          {**valid, "makerCommissionRate": 0.0002},
                          {**valid, "takerCommissionRate": "NaN"},
                          {**valid, "takerCommissionRate": "-0.0005"},
                          {**valid, "takerCommissionRate": "Infinity"},
                          {**valid, "takerCommissionRate": "2"}):
            with self.subTest(malformed=malformed):
                adapter.fetch_commission_rate.side_effect = None
                adapter.fetch_commission_rate.return_value = malformed
                with self.assertRaisesRegex(DemoApiError, "demo_signed_check_failed"):
                    self.api.post("/check", {}, actor="admin")
                stored = self.data.put_document.call_args.args[1]
                self.assertFalse(stored["connected"])
                self.assertNotIn("commissionRates", stored)
                self.assertNotIn("synthetic-secret", json.dumps(stored))
        self.ledger.append_account_snapshot.assert_not_called()
        self.ledger.append_position_snapshot.assert_not_called()

    def test_account_quote_network_error_is_redacted_and_status_get_does_not_retry(self):
        self.credentials.load_credentials.return_value = DemoCredentials("synthetic-key", "synthetic-secret")
        adapter = self.adapter_factory.return_value
        adapter.fetch_balance.return_value = {"raw": {"assets": []}}
        adapter.fetch_positions.return_value = []
        adapter.fetch_commission_rate.side_effect = RuntimeError("synthetic-secret in signed request")
        with self.assertRaisesRegex(DemoApiError, "demo_signed_check_failed"):
            self.api.post("/check", {}, actor="admin")
        stored = self.data.put_document.call_args.args[1]
        self.assertFalse(stored["connected"])
        self.assertNotIn("synthetic-secret", json.dumps(stored))
        self.data.get_document.return_value = stored
        self.assertFalse(self.api.get("")["lastCheck"]["connected"])
        adapter.fetch_commission_rate.assert_called_once()
        self.ledger.append_account_snapshot.assert_not_called()

    def test_raw_and_runner_wrapped_snapshots_keep_binance_fields(self):
        balance = {"raw": {"assets": [{"asset": "USDT", "walletBalance": "123.0000"}],
                           "totalWalletBalance": "123.0000"}}
        positions = [{"raw": {"symbol": "BTCUSDT", "positionAmt": "0.001000"}}]
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                self.ledger.get_latest_account_snapshot.return_value = {
                    "payload": {"balance": balance} if wrapped else balance, "observed_ms": 1}
                self.ledger.get_latest_position_snapshot.return_value = {
                    "payload": {"positions": positions} if wrapped else positions, "observed_ms": 1}
                self.assertEqual(self.api.get("/account")["items"][0]["walletBalance"], "123.0000")
                self.assertEqual(self.api.get("/positions")["items"][0]["positionAmt"], "0.001000")

    def test_start_only_eligible_identity_and_stop_retains_protection_semantics(self):
        with self.assertRaisesRegex(DemoApiError, "not_demo_eligible"):
            self.api.post("/start", {"accountId": "unknown"}, actor="admin")
        self.controller.start.assert_not_called()
        self.controller.start.return_value = None
        self.api.post("/start", {"accountId": "selected"}, actor="admin")
        self.controller.start.assert_called_once_with("selected")
        self.controller.stop.return_value = None
        result = self.api.post("/stop", {}, actor="admin")
        self.assertIn("continue_owned_risk_protection", result["stopPolicy"])

    def test_flat_handoff_pauses_then_closes_without_start_or_credential_changes(self):
        calls = []
        self.controller.stop.side_effect = lambda: calls.append("stop")
        self.controller.close_if_flat.side_effect = lambda: calls.append("close")
        result = self.api.post("/yield-flat", {}, actor="admin")
        self.assertEqual(calls, ["stop", "close"])
        self.assertFalse(result["automaticRestart"])
        self.assertIn("yield_only_when_owned_ledger_flat", result["handoffPolicy"])
        self.controller.start.assert_not_called()
        self.credentials.load_credentials.assert_not_called()
        self.credentials.save_credentials.assert_not_called()
        self.adapter_factory.assert_not_called()

    def test_flat_handoff_refusal_is_a_fixed_conflict_code(self):
        self.controller.close_if_flat.side_effect = DemoHandoffError("demo_worker_busy",
            "request contained synthetic-secret-value")
        with self.assertRaises(DemoApiError) as refused:
            self.api.post("/yield-flat", {}, actor="admin")
        self.assertEqual(refused.exception.status, 409)
        self.assertEqual(refused.exception.code, "demo_worker_busy")
        self.assertNotIn("synthetic-secret-value", str(refused.exception))
        self.controller.start.assert_not_called()

    def test_switch_requires_eligible_target_and_returns_paused_session_policy(self):
        with self.assertRaisesRegex(DemoApiError, "paper_account_not_demo_eligible"):
            self.api.post("/switch", {"accountId": "blocked"}, actor="admin")
        self.controller.switch.assert_not_called()
        self.controller.switch.return_value = {"selectedAccountId": "selected", "entriesEnabled": False}
        result = self.api.post("/switch", {"accountId": "selected"}, actor="admin")
        self.assertFalse(result["automaticRestart"])
        self.assertFalse(result["runner"]["entriesEnabled"])
        self.controller.switch.assert_called_once_with("selected")
        self.controller.start.assert_not_called()
        self.credentials.save_credentials.assert_not_called()

    def test_switch_refusal_returns_fixed_conflict_without_exchange_error_text(self):
        self.controller.switch.side_effect = DemoHandoffError("demo_account_positions_not_flat",
                                                              "synthetic-secret-value")
        with self.assertRaises(DemoApiError) as refused:
            self.api.post("/switch", {"accountId": "selected"}, actor="admin")
        self.assertEqual(refused.exception.status, 409)
        self.assertEqual(refused.exception.code, "demo_account_positions_not_flat")
        self.assertNotIn("synthetic-secret-value", str(refused.exception))

    def test_reads_follow_active_store_but_explicit_history_stays_on_old_session(self):
        key = "demo_strategy_" + "b" * 32
        old, current = self.ledger, Mock()
        old.virtual_account_key = "demo_default"
        current.virtual_account_key = key
        current.metrics.return_value = {"orders_by_status": {"filled": 2}}
        current.net_fill_positions.return_value = {}
        current.list_fills.return_value = []
        old.list_fills.return_value = []
        current.list_strategy_sessions.return_value = [
            {"virtualAccountKey": "demo_default", "strategyId": "old", "forwardBoundaryMs": 100},
            {"virtualAccountKey": key, "strategyId": "selected", "forwardBoundaryMs": 200}]
        self.api._ledger_provider = lambda: current
        self.api.session_factory = Mock(return_value=old)
        self.assertEqual(self.api.get("")["metrics"]["orderCount"], 2)
        self.api.get("/fills")
        current.list_fills.assert_called_once_with(limit=100)
        old.list_fills.assert_not_called()
        self.api.get("/fills", session="demo_default")
        old.list_fills.assert_called_once_with(limit=100)
        self.api.session_factory.assert_called_once_with("demo_default")
        sessions = self.api.get("/sessions")["items"]
        self.assertEqual([item["active"] for item in sessions], [False, True])
        self.credentials.load_credentials.assert_not_called()
        self.adapter_factory.assert_not_called()

    def test_unknown_history_never_constructs_a_ledger_or_contacts_exchange(self):
        self.ledger.list_strategy_sessions.return_value = []
        self.api.session_factory = Mock()
        with self.assertRaisesRegex(DemoApiError, "demo_session_not_found") as refused:
            self.api.get("/evidence", session="demo_strategy_" + "c" * 32)
        self.assertEqual(refused.exception.status, 404)
        self.api.session_factory.assert_not_called()
        self.credentials.load_credentials.assert_not_called()

    def test_comparisons_default_to_full_audit_and_focus_keeps_selected_history(self):
        key = "demo_strategy_" + "b" * 32
        old = Mock()
        self.ledger.virtual_account_key = key
        self.ledger.list_comparisons.return_value = [{"stage": "runner_checkpoint"}]
        self.ledger.list_strategy_sessions.return_value = [{"virtualAccountKey": "demo_default"}]
        old.list_comparisons.return_value = [{"stage": "paper_event"}]
        self.api.session_factory = Mock(return_value=old)
        audit = self.api.get("/comparisons")
        self.assertEqual(audit["view"], "audit")
        self.assertEqual(audit["items"], [{"stage": "runner_checkpoint"}])
        self.ledger.list_comparisons.assert_called_once_with(limit=100, view="audit")
        focused = self.api.get("/comparisons", view="signals", limit=5, session="demo_default")
        self.assertEqual(focused["view"], "signals")
        self.assertEqual(focused["items"], [{"stage": "paper_event"}])
        old.list_comparisons.assert_called_once_with(limit=5, view="signals")
        self.api.session_factory.assert_called_once_with("demo_default")
        self.adapter_factory.assert_not_called()
        self.credentials.load_credentials.assert_not_called()

    def test_invalid_comparison_view_never_reads_database_or_history(self):
        self.api.session_factory = Mock()
        for suffix, view in (("/comparisons", "other"), ("/comparisons", []),
                             ("/comparisons", ""), ("/fills", "signals")):
            with self.subTest(suffix=suffix, view=view):
                with self.assertRaisesRegex(DemoApiError, "invalid_comparison_view"):
                    self.api.get(suffix, view=view, session="demo_default")
        self.ledger.list_comparisons.assert_not_called()
        self.ledger.list_strategy_sessions.assert_not_called()
        self.data._connection.assert_not_called()
        self.api.session_factory.assert_not_called()
        self.adapter_factory.assert_not_called()

    def test_explicit_empty_active_session_can_be_read_before_first_strategy_start(self):
        self.ledger.virtual_account_key = "demo_default"
        self.ledger.list_fills.return_value = []
        self.ledger.list_strategy_sessions.return_value = []
        self.api.session_factory = Mock()
        self.assertEqual(self.api.get("/fills", session="demo_default")["items"], [])
        reader = Mock(return_value={"environment": "binance_demo", "binding": None})
        with patch.dict("sys.modules", {"demo_evidence": Mock(read_demo_evidence=reader)}):
            self.api.get("/evidence", session="demo_default")
        reader.assert_called_once_with(self.data, virtual_account_key="demo_default")
        self.api.session_factory.assert_not_called()
        self.ledger.list_strategy_sessions.assert_not_called()
        self.credentials.load_credentials.assert_not_called()

    def test_saved_account_fingerprint_blocks_key_rotation_through_an_empty_new_ledger(self):
        from demo_runner_controller import STATE_KEY
        self.credentials.status.return_value = {"configured": True,
            "fingerprint": hashlib.sha256(b"new-key").hexdigest()[:16]}
        self.data.get_document.side_effect = lambda key, default=None: (
            {"credentialFingerprint": hashlib.sha256(b"original-key").hexdigest()[:16]}
            if key == STATE_KEY else default)
        with self.assertRaisesRegex(DemoApiError, "demo_ledger_bound_to_existing_api_identity"):
            self.api.post("/credentials", {"apiKey": "new-key", "secret": "new-secret"}, actor="admin")
        self.credentials.save_credentials.assert_not_called()

    def test_api_factory_restores_active_namespace_instead_of_returning_to_default(self):
        from demo_api import create_demo_api
        from demo_runner_controller import STATE_KEY
        key = "demo_strategy_" + "d" * 32
        self.data.get_document.side_effect = lambda name, default=None: (
            {"virtualAccountKey": key, "selectedAccountId": "selected"} if name == STATE_KEY else default)
        with patch("demo_api.DemoCredentialStore"), patch("demo_store.DemoStore") as factory, \
                patch("demo_runner_controller.DemoRunnerController") as controller_factory:
            api = create_demo_api(self.data, auth_dir=Path("/unused-offline-auth"))
        factory.assert_called_once_with(self.data, virtual_account_key=key)
        self.assertIs(api.demo_store, controller_factory.return_value.demo_store)

    def test_retained_account_flock_blocks_credential_rotation_after_worker_failure(self):
        self.controller.status.return_value = {"running": False, "entriesEnabled": False,
            "workerRunning": False, "accountLockOwned": True, "status": "not_started"}
        with self.assertRaisesRegex(DemoApiError, "pause_and_reconcile"):
            self.api.post("/credentials", {"apiKey": "new", "secret": "new"}, actor="admin")
        self.credentials.save_credentials.assert_not_called()

    def test_configuration_cannot_race_readonly_signed_check(self):
        entered, release, save_attempted = threading.Event(), threading.Event(), threading.Event()
        self.credentials.load_credentials.return_value = DemoCredentials("synthetic-key", "synthetic-secret")
        self.credentials.status.return_value = {"configured": True,
            "fingerprint": hashlib.sha256(b"synthetic-key").hexdigest()[:16]}
        adapter = self.adapter_factory.return_value
        def wait_balance():
            entered.set()
            if not release.wait(timeout=2):
                raise AssertionError("check was not released")
            return {"raw": {"assets": []}}
        adapter.fetch_balance.side_effect = wait_balance
        adapter.fetch_positions.return_value = []
        errors = []
        def check():
            try:
                self.api.post("/check", {}, actor="admin")
            except Exception as exc:
                errors.append(exc)
        def save():
            save_attempted.set()
            try:
                self.api.post("/credentials", {"apiKey": "synthetic-key", "secret": "new-synthetic-secret"}, actor="admin")
            except Exception as exc:
                errors.append(exc)
        checker = threading.Thread(target=check)
        saver = threading.Thread(target=save)
        checker.start()
        self.assertTrue(entered.wait(timeout=1))
        saver.start()
        self.assertTrue(save_attempted.wait(timeout=1))
        self.credentials.save_credentials.assert_not_called()
        release.set()
        checker.join(timeout=2)
        saver.join(timeout=2)
        self.assertFalse(checker.is_alive() or saver.is_alive())
        self.assertFalse(errors)
        self.credentials.save_credentials.assert_called_once()


class AuthDemoBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        spec = importlib.util.spec_from_file_location("auth_demo_bridge_test",
            Path(__file__).resolve().parents[1] / "auth_service.py")
        cls.auth = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"QUANT_AUTH_DIR": cls.temporary.name}):
            spec.loader.exec_module(cls.auth)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def handler(self):
        handler = object.__new__(self.auth.Handler)
        handler.path = PREFIX + "/credentials"
        handler.headers = {"Origin": ORIGIN, "Content-Type": "application/json",
                           "Content-Length": str(MAX_BODY_BYTES + 1)}
        handler._bearer = Mock(return_value="test")
        handler._is_local = Mock(return_value=True)
        handler._read_body = Mock(side_effect=AssertionError("must not read oversized body"))
        handler._send = Mock(side_effect=lambda code, payload: (code, payload))
        handler._proxy = Mock(side_effect=AssertionError("Demo must not reach general proxy"))
        return handler

    def test_actual_handler_rejects_before_unbounded_read_and_never_proxies(self):
        with patch.object(self.auth, "verify_token", return_value="admin"), \
                patch.object(self.auth, "get_demo_api") as api:
            self.assertEqual(self.handler().do_POST()[0], 413)
            api.assert_not_called()
        for method in ("do_PUT", "do_PATCH", "do_DELETE"):
            self.assertEqual(getattr(self.handler(), method)()[0], 405)

    def test_demo_logging_drops_query_secrets(self):
        handler = self.handler()
        handler.path += "?secret=synthetic-query-secret"
        captured = io.StringIO()
        with redirect_stderr(captured):
            handler.log_message('"%s" %s %s', "POST " + handler.path, "400", "-")
        self.assertEqual(captured.getvalue(), "[auth] Demo HTTP status=400\n")
        self.assertNotIn("synthetic-query-secret", captured.getvalue())

    def test_demo_options_does_not_read_unbounded_body(self):
        handler = self.handler()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler._cors = Mock()
        handler.do_OPTIONS()
        self.assertTrue(handler.close_connection)
        handler._read_body.assert_not_called()
        handler.send_response.assert_called_once_with(204)


if __name__ == "__main__":
    unittest.main()

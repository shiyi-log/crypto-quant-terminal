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
        code, payload = handle_demo_request(handler, method="POST",
            verify_token=lambda _: "admin", allowed_origins={ORIGIN}, api_provider=provider)
        self.assertEqual(code, 503)
        self.assertNotIn("synthetic-secret-value", json.dumps(payload))


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

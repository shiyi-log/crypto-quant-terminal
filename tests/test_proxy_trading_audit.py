"""交易代理审计与失效研究结果的 HTTP 契约；不连接真实交易服务或数据库。"""

from __future__ import annotations

from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

from runtime_config import load_environment


ROOT = Path(__file__).resolve().parents[1]


class _AuthContractCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="quant_proxy_audit_")
        cls.addClassCleanup(cls.temp.cleanup)
        temp_root = Path(cls.temp.name)
        (temp_root / ".env").write_text("QUANT_BOOTSTRAP_PASSWORD=test-only\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location(
            "quant_proxy_audit_contract_test", ROOT / "auth_service.py"
        )
        assert spec is not None and spec.loader is not None
        cls.auth = importlib.util.module_from_spec(spec)
        # auth_service prepares its signing key on import. Redirect both the
        # environment loader and AUTH_DIR before importing; never load real .env.
        with patch.dict(os.environ, {"QUANT_AUTH_DIR": str(temp_root / "auth")}), \
             patch("runtime_config.load_environment", side_effect=lambda: load_environment(temp_root)):
            spec.loader.exec_module(cls.auth)

    def setUp(self):
        self.handler = object.__new__(self.auth.Handler)
        self.handler.path = "/api/v1/forceexit"
        self.handler.command = "POST"
        self.handler.client_address = ("127.0.0.1", 43210)
        self.handler.headers = {"Content-Type": "application/json"}
        self.handler._bearer = Mock(return_value="test-only-ui-token")
        self.body = {"tradeid": "7", "ordertype": "market"}
        self.handler._read_body = Mock(side_effect=lambda: json.dumps(self.body).encode())
        self.handler._send = Mock(side_effect=self._capture_response)
        self.handler.log_message = Mock()
        self.verify = self.enterContext(patch.object(self.auth, "verify_token", return_value="operator"))

    @staticmethod
    def _capture_response(code, payload=None, raw=None, ctype=None):
        if raw is not None:
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                payload = raw
        return code, payload


class ProxyTradingAuditContractTests(_AuthContractCase):
    def setUp(self):
        super().setUp()
        self.events = []
        self.timeline = []
        self.store = Mock()
        self.store_patch = self.enterContext(patch.object(self.auth, "get_store", return_value=self.store))
        self.token_fn = Mock(return_value="test-only-upstream-token")
        # Keep begin/finish/classification real; only replace their durable SQL
        # append boundary. Assertions inspect the actual sanitised event payload.
        self.append = self.enterContext(
            patch.object(self.auth.trading_audit, "_append_event", side_effect=self._append_event)
        )
        self.urlopen = self.enterContext(
            patch.object(self.auth.urllib.request, "urlopen", side_effect=self._dispatch)
        )
        self.upstream_response = self._response({"status": "Created exit order", "trade_id": 7})

    def _append_event(self, store, *, event_type, request_id, payload):
        self.assertIs(store, self.store)
        self.timeline.append(event_type)
        self.events.append({"event_type": event_type, "request_id": request_id,
                            "payload": deepcopy(payload)})
        suffix = "request" if event_type == "trading_operation_requested" else "result"
        return f"trading-operation:{request_id}:{suffix}"

    def _dispatch(self, request, timeout):
        self.timeline.append("upstream_dispatch")
        self.assertEqual(timeout, 30)
        return self.upstream_response

    @staticmethod
    def _response(payload, status=200):
        response = Mock()
        response.status = status
        response.code = status
        response.getcode.return_value = status
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = json.dumps(payload).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    @staticmethod
    def _http_error(status, payload=None):
        return urllib.error.HTTPError(
            "http://fake.invalid/api/v1/forceexit", status, "test HTTP response", {},
            io.BytesIO(json.dumps(payload or {"detail": "upstream rejected"}).encode()),
        )

    def proxy(self):
        return self.handler._proxy_upstream("http://fake.invalid", self.token_fn, "test upstream")

    def assert_single_audit_pair(self, outcome):
        self.assertEqual([e["event_type"] for e in self.events],
                         ["trading_operation_requested", "trading_operation_result"])
        request, result = self.events
        self.assertEqual(request["request_id"], result["request_id"])
        self.assertEqual(request["payload"]["request_id"], result["payload"]["request_id"])
        self.assertEqual(result["payload"]["outcome"], outcome)
        return request["payload"], result["payload"]

    def test_unauthenticated_mutation_does_not_fetch_token_or_dispatch(self):
        self.verify.return_value = None
        self.assertEqual(self.proxy()[0], 401)
        self.token_fn.assert_not_called()
        self.store_patch.assert_not_called()
        self.append.assert_not_called()
        self.urlopen.assert_not_called()

    def test_request_audit_is_durable_before_dispatch_and_acceptance_is_not_fill(self):
        code, payload = self.proxy()
        self.assertEqual(code, 200)
        self.assertEqual(payload["status"], "Created exit order")
        request, result = self.assert_single_audit_pair("accepted")
        self.assertEqual(self.timeline,
                         ["trading_operation_requested", "upstream_dispatch", "trading_operation_result"])
        self.assertEqual(request["actor"], "operator")
        self.assertEqual(request["source_ip"], "127.0.0.1")
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "/api/v1/forceexit")
        self.assertEqual(result["upstream_status"], 200)
        # The result describes API acceptance only; no synthetic fill/profit.
        self.assertNotIn("filled", result)
        self.assertNotIn("profit", result)
        self.assertNotIn("trade_completed", result)

    def test_audit_database_failure_prevents_all_upstream_dispatch(self):
        self.append.side_effect = self.auth.trading_audit.AuditUnavailable("test persistence unavailable")
        code, _payload = self.proxy()
        self.assertEqual(code, 503)
        self.token_fn.assert_not_called()
        self.urlopen.assert_not_called()
        self.assertEqual(self.events, [])

    def test_result_database_failure_leaves_request_unknown_and_does_not_retry_dispatch(self):
        original_append = self._append_event

        def fail_result(store, *, event_type, request_id, payload):
            if event_type == "trading_operation_result":
                raise self.auth.trading_audit.AuditUnavailable("test result persistence unavailable")
            return original_append(store, event_type=event_type, request_id=request_id, payload=payload)

        self.append.side_effect = fail_result
        with patch.object(self.auth.sys, "stderr", io.StringIO()):
            code, payload = self.proxy()
        self.assertEqual(code, 502)
        self.urlopen.assert_called_once()
        self.assertEqual([e["event_type"] for e in self.events], ["trading_operation_requested"])
        self.assertEqual(payload["request_id"], self.events[0]["request_id"])
        self.assertEqual(self.handler._audit_status, "result_pending")
        self.assertEqual(self.handler._audit_request_id, self.events[0]["request_id"])
        self.assertNotEqual(payload.get("status"), "accepted")
        connection = Mock()
        connection.execute.side_effect = [
            Mock(fetchall=Mock(return_value=[{
                "sequence_id": 1, "event_id": f"trading-operation:{payload['request_id']}:request",
                "payload": self.events[0]["payload"], "created_at": None,
            }])),
            Mock(fetchall=Mock(return_value=[])),
        ]
        context = Mock()
        context.__enter__ = Mock(return_value=connection)
        context.__exit__ = Mock(return_value=False)
        self.store._connection.return_value = context
        operation = self.auth.trading_audit.recent_operations(self.store)[0]
        self.assertEqual(operation["request_id"], payload["request_id"])
        self.assertEqual(operation["outcome"], "unknown")
        self.assertIsNone(operation["result_event_id"])

    def test_missing_upstream_credentials_are_not_forwarded_and_recorded(self):
        self.token_fn.return_value = None
        self.assertEqual(self.proxy()[0], 502)
        _request, result = self.assert_single_audit_pair("not_forwarded")
        self.assertEqual(result["error_code"], "credential_unavailable")
        self.assertIsNone(result["upstream_status"])
        self.urlopen.assert_not_called()

    def test_http_rejection_is_recorded_with_status(self):
        self.urlopen.side_effect = self._http_error(409)
        self.assertEqual(self.proxy()[0], 409)
        _request, result = self.assert_single_audit_pair("rejected")
        self.assertEqual(result["upstream_status"], 409)
        self.urlopen.assert_called_once()

    def test_http_200_business_error_is_rejected_not_accepted(self):
        self.upstream_response = self._response({"status": "Error: trade not found"})
        self.assertEqual(self.proxy()[0], 200)
        _request, result = self.assert_single_audit_pair("rejected")
        self.assertEqual(result["upstream_status"], 200)

    def test_empty_or_malformed_http_200_response_is_unknown(self):
        for raw in (b"", b"not-json"):
            with self.subTest(raw=raw):
                self.events.clear()
                self.timeline.clear()
                self.upstream_response.read.return_value = raw
                self.assertEqual(self.proxy()[0], 200)
                _request, result = self.assert_single_audit_pair("unknown")
                self.assertEqual(result["upstream_status"], 200)
                self.assertEqual(result["error_code"], "unknown_response")

    def test_upstream_server_error_is_unknown_not_claimed_rejected(self):
        self.urlopen.side_effect = self._http_error(503)
        self.assertEqual(self.proxy()[0], 503)
        _request, result = self.assert_single_audit_pair("unknown")
        self.assertEqual(result["upstream_status"], 503)

    def test_timeout_leaves_unknown_outcome_without_automatic_retry(self):
        self.urlopen.side_effect = TimeoutError("test timeout")
        self.assertEqual(self.proxy()[0], 502)
        _request, result = self.assert_single_audit_pair("unknown")
        self.assertIsNone(result["upstream_status"])
        self.urlopen.assert_called_once()

    def test_network_failure_leaves_unknown_outcome(self):
        self.urlopen.side_effect = urllib.error.URLError("test connection closed")
        self.assertEqual(self.proxy()[0], 502)
        _request, result = self.assert_single_audit_pair("unknown")
        self.assertIsNone(result["upstream_status"])
        self.urlopen.assert_called_once()

    def test_401_token_refresh_has_one_request_and_one_final_result(self):
        self.token_fn.side_effect = ["first-test-token", "refreshed-test-token"]
        self.urlopen.side_effect = [self._http_error(401), self.upstream_response]
        self.assertEqual(self.proxy()[0], 200)
        self.assert_single_audit_pair("accepted")
        self.assertEqual(self.urlopen.call_count, 2)
        self.assertEqual(self.token_fn.call_args_list[0].kwargs, {})
        self.assertEqual(self.token_fn.call_args_list[1].kwargs, {"force": True})
        requests = [call.args[0] for call in self.urlopen.call_args_list]
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer first-test-token")
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer refreshed-test-token")

    def test_rejected_refresh_attempt_still_has_only_one_final_result(self):
        self.token_fn.side_effect = ["first-test-token", "refreshed-test-token"]
        self.urlopen.side_effect = [self._http_error(401), self._http_error(403)]
        self.assertEqual(self.proxy()[0], 403)
        _request, result = self.assert_single_audit_pair("rejected")
        self.assertEqual(result["upstream_status"], 403)
        self.assertEqual(self.urlopen.call_count, 2)

    def test_read_only_trade_query_does_not_write_audit(self):
        self.handler.command = "GET"
        self.handler.path = "/api/v1/trade/7"
        self.handler._read_body.return_value = b""
        self.handler._read_body.side_effect = None
        self.assertEqual(self.proxy()[0], 200)
        self.append.assert_not_called()
        self.store_patch.assert_not_called()
        self.urlopen.assert_called_once()

    def test_only_allowlisted_target_is_persisted_not_tokens_or_payload_secrets(self):
        self.body.update({
            "password": "secret-test-password", "access_token": "secret-test-body-token",
            "metadata": {"secret": "secret-test-nested-value"},
        })
        self.handler.headers.update({"Authorization": "Bearer secret-test-header-token",
                                     "X-Forwarded-For": "198.51.100.2"})
        self.upstream_response = self._response({"status": "Created exit order",
                                                "token": "secret-test-response-token"})
        self.assertEqual(self.proxy()[0], 200)
        request, _result = self.assert_single_audit_pair("accepted")
        encoded = json.dumps(self.events)
        for secret in ("secret-test-password", "secret-test-body-token", "secret-test-nested-value",
                       "secret-test-header-token", "secret-test-response-token", "test-only-upstream-token"):
            self.assertNotIn(secret, encoded)
        self.assertNotIn("metadata", request["target"])
        self.assertEqual(request["source_ip"], "127.0.0.1")
        # Sanitisation affects only the audit record, not the forwarded request.
        forwarded = json.loads(self.urlopen.call_args.args[0].data)
        self.assertEqual(forwarded, self.body)


class InvalidatedInspectionContractTests(_AuthContractCase):
    def setUp(self):
        super().setUp()
        self.store = Mock()
        self.store_patch = self.enterContext(patch.object(self.auth, "get_store", return_value=self.store))

    def test_disabled_inspection_cannot_surface_archived_checks_as_current(self):
        archived = {
            "t": "2026-10-10 00:37:25", "all_ok": True, "failed": [],
            "checks": {"C1": {"ok": True, "live_annual": 0.156}},
        }
        unchanged = deepcopy(archived)
        self.store.get_document.return_value = archived
        self.store.read_events.return_value = [{"t": "old", "all_ok": True, "failed": []}]
        code, payload = self.handler._local_auto_iterate()
        self.assertEqual(code, 200)
        self.assertEqual(payload["status"], "legacy_disabled")
        self.assertIs(payload["enabled"], False)
        self.assertIs(payload["realtime"], False)
        self.assertEqual(payload["validity"], "invalidated")
        self.assertEqual(payload["checks"], {})
        self.assertIsNone(payload["all_ok"])
        self.assertEqual(payload["failed"], [])
        self.assertEqual(payload["last_run_at"], archived["t"])
        self.assertEqual(payload["archive"]["status"], "archive_only")
        self.assertEqual(payload["archive"]["validity"], "invalidated")
        self.assertEqual(payload["archive"]["data"], archived)
        self.assertEqual(archived, unchanged)
        self.store.put_document.assert_not_called()

    def test_missing_archive_still_reports_disabled_not_missing_live_service(self):
        self.store.get_document.return_value = None
        self.store.read_events.return_value = []
        code, payload = self.handler._local_auto_iterate()
        self.assertEqual(code, 200)
        self.assertEqual(payload["status"], "legacy_disabled")
        self.assertIsNone(payload["archive"]["data"])
        self.assertEqual(payload["checks"], {})

    def test_inspection_requires_authentication_before_reading_archive(self):
        self.verify.return_value = None
        self.assertEqual(self.handler._local_auto_iterate()[0], 401)
        self.store_patch.assert_not_called()

    def test_legacy_champion_metrics_remain_unknown_even_when_registry_has_constants(self):
        legacy = {
            "id": "live-trend-20-20", "layer": "live_strategy", "status": "champion",
            "metrics": {"research_annual": 0.147, "live_annual": 0.156,
                        "sharpe_live": 0.75, "impl_gap_pct": 0.06, "calmar_live": None},
            "gate": {"passed": True, "criteria": "C1"},
        }
        current = {"id": "new-research", "layer": "ml_model", "status": "challenger",
                   "metrics": {"ic": 0.0353}}
        reg = {"updated": "old", "versions": [legacy, current],
               "champions": {"live_strategy": legacy["id"]}}
        unchanged = deepcopy(reg)
        self.store.get_document.return_value = reg
        self.handler._trial_stats = Mock(return_value={"total": 0})
        self.handler._auto_research_running = Mock(return_value=False)
        code, payload = self.handler._local_model_versions()
        self.assertEqual(code, 200)
        champion = payload["layers"]["live_strategy"]["champion"]
        self.assertEqual(champion["metrics_validity"], "invalidated")
        for key in legacy["metrics"]:
            self.assertIsNone(champion["metrics"][key], key)
        self.assertEqual(champion["legacy_metrics"], legacy["metrics"])
        self.assertIs(champion["gate"]["passed"], False)
        self.assertEqual(champion["gate"]["status"], "invalidated")
        self.assertEqual(payload["layers"]["ml_model"]["challengers"][0]["metrics"], current["metrics"])
        self.assertEqual(reg, unchanged)
        self.store.put_document.assert_not_called()

    def test_invalidated_legacy_gate_cannot_promote_without_explicit_override(self):
        import importlib

        registry = importlib.import_module("bot.model_registry")
        reg = {
            "versions": [{"id": "live-trend-20-20", "layer": "live_strategy",
                          "metrics": {"live_annual": 0.156}, "gate": {"passed": True}}],
            "champions": {},
        }
        self.handler._json_body = Mock(return_value={
            "id": "live-trend-20-20", "note": "测试旧口径禁止直接晋升",
        })
        self.handler._model_registry = Mock(return_value=registry)
        with patch.object(registry, "load", return_value=reg), \
             patch.object(registry, "set_champion") as promote, \
             patch.object(registry, "save") as save:
            code, payload = self.handler._local_promote_version()
        self.assertEqual(code, 409)
        self.assertIs(payload["gate"]["passed"], False)
        self.assertEqual(payload["gate"]["status"], "invalidated")
        promote.assert_not_called()
        save.assert_not_called()
        self.store.put_document.assert_not_called()


if __name__ == "__main__":
    unittest.main()

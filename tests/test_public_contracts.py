"""验证本机免密登录的公开接口；测试账户与密钥均隔离于真实环境。"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import urllib.parse
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


class AutoLoginContractTests(unittest.TestCase):
    """保留免密登录行为，同时保证外部请求不能获取本机账户令牌。"""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="quant_auth_test_")
        cls.addClassCleanup(cls.temp_dir.cleanup)
        spec = importlib.util.spec_from_file_location(
            "quant_auth_contract_test", ROOT / "auth_service.py"
        )
        assert spec is not None and spec.loader is not None
        cls.auth = importlib.util.module_from_spec(spec)
        # 导入会准备签名密钥，必须使用临时目录，不能触碰真实 auth/。
        with patch.dict(os.environ, {"QUANT_AUTH_DIR": cls.temp_dir.name}):
            spec.loader.exec_module(cls.auth)

    def setUp(self):
        # 不启动 HTTP 服务；直接调用实际 Handler 方法并替换存储边界。
        self.handler = object.__new__(self.auth.Handler)
        self.handler.path = "/auth/auto-login"
        self.handler.client_address = ("127.0.0.1", 12345)
        self.handler._read_body = Mock(return_value=b"{}")
        self.handler._send = Mock(side_effect=lambda code, payload: (code, payload))
        self.users = {"users": {self.auth.DEFAULT_USER: {}}}
        self.load_users = self.enterContext(
            patch.object(self.auth, "load_users", return_value=self.users)
        )
        self.make_token = self.enterContext(
            patch.object(self.auth, "make_token", return_value="test-only-token")
        )

    def test_post_route_returns_compatible_token_response(self):
        # 经真实 POST 分发链验证入口仍然可达，而不仅验证内部辅助方法。
        self.handler.do_POST()
        self.handler._send.assert_called_once_with(
            200,
            {
                "access_token": "test-only-token",
                "auto": True,
                "username": self.auth.DEFAULT_USER,
            },
        )
        self.make_token.assert_called_once_with(self.auth.DEFAULT_USER)

    def test_ipv6_loopback_can_use_auto_login(self):
        self.handler.client_address = ("::1", 12345)
        code, _payload = self.handler._auth_post()
        self.assertEqual(code, 200)

    def test_query_string_does_not_break_existing_route(self):
        self.handler.path = "/auth/auto-login?source=login-page"
        code, _payload = self.handler._auth_post()
        self.assertEqual(code, 200)

    def test_remote_client_is_denied_before_reading_users(self):
        self.handler.client_address = ("198.51.100.23", 12345)
        code, payload = self.handler._auth_post()
        self.assertEqual(code, 403)
        self.assertNotIn("access_token", payload)
        self.load_users.assert_not_called()
        self.make_token.assert_not_called()

    def test_forwarded_loopback_header_does_not_allow_remote_client(self):
        self.handler.client_address = ("198.51.100.23", 12345)
        self.handler.headers = {"X-Forwarded-For": "127.0.0.1"}
        code, _payload = self.handler._auth_post()
        self.assertEqual(code, 403)
        self.make_token.assert_not_called()

    def test_missing_default_user_has_no_token(self):
        self.users["users"].clear()
        code, payload = self.handler._auth_post()
        self.assertEqual(code, 404)
        self.assertNotIn("access_token", payload)
        self.make_token.assert_not_called()

    def test_market_stream_discovery_requires_login_and_honors_external_url(self):
        self.handler.path = "/api/locals/market-stream-info"
        self.handler._bearer = Mock(return_value="test-only-token")
        with patch.object(self.auth, "verify_token", return_value=None):
            self.assertEqual(self.handler.do_GET()[0], 401)
        with patch.object(self.auth, "verify_token", return_value="admin"), \
             patch.dict(os.environ, {"QUANT_LIVE_WS_URL": "wss://example.test/market"}):
            code, payload = self.handler.do_GET()
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"url": "wss://example.test/market"})
        self.load_users.assert_not_called()

    def test_research_ledger_requires_login(self):
        self.handler.path = "/api/locals/research-ledger"
        self.handler._bearer = Mock(return_value=None)
        with patch.object(self.auth, "verify_token", return_value=None):
            code, payload = self.handler.do_GET()
        self.assertEqual(code, 401)
        self.assertEqual(payload["detail"], "未登录或登录已过期")

    def test_research_ledger_reads_fixed_source_projection(self):
        self.handler.path = "/api/locals/research-ledger?version_id=demo"
        self.handler._bearer = Mock(return_value="test-only-token")
        expected = {"summary": {"version_count": 1}, "versions": []}
        store = Mock()
        operation_audit = {"summary": {"count": 0}, "operations": []}
        with patch.object(self.auth, "verify_token", return_value="admin"), \
             patch.object(self.auth, "get_store", return_value=store), \
             patch("research_ledger.ledger_payload", return_value=expected) as payload_fn, \
             patch.object(self.auth.trading_audit, "audit_payload", return_value=operation_audit) as audit_fn:
            code, payload = self.handler.do_GET()
        self.assertEqual(code, 200)
        self.assertEqual(payload, {**expected, "operation_audit": operation_audit})
        payload_fn.assert_called_once_with(
            store, version_id="demo", source_id="bot/tradesv3.dryrun.sqlite",
            registry_path=self.auth.os.path.join(self.auth.ROOT, "bot", "user_data", "model_versions.json"),
        )
        audit_fn.assert_called_once_with(store)
        store.get_document.assert_not_called()
        store.read_events.assert_not_called()

    def test_research_ledger_returns_service_unavailable_on_database_error(self):
        self.handler.path = "/api/locals/research-ledger"
        self.handler._bearer = Mock(return_value="test-only-token")
        with patch.object(self.auth, "verify_token", return_value="admin"), \
             patch.object(self.auth, "get_store", side_effect=self.auth.psycopg.OperationalError("offline")):
            code, payload = self.handler.do_GET()
        self.assertEqual(code, 503)
        self.assertEqual(payload["detail"], "研究证据账暂时不可用，请检查同步源")

    def test_research_ledger_returns_service_unavailable_on_missing_database_config(self):
        self.handler.path = "/api/locals/research-ledger"
        self.handler._bearer = Mock(return_value="test-only-token")
        with patch.object(self.auth, "verify_token", return_value="admin"), \
             patch.object(self.auth, "get_store", side_effect=RuntimeError("private database config missing")):
            code, payload = self.handler.do_GET()
        self.assertEqual(code, 503)
        self.assertEqual(payload, {"detail": "研究证据账暂时不可用，请检查同步源"})

    def test_research_ledger_returns_service_unavailable_on_projection_read_failure(self):
        self.handler.path = "/api/locals/research-ledger"
        self.handler._bearer = Mock(return_value="test-only-token")
        with patch.object(self.auth, "verify_token", return_value="admin"), \
             patch.object(self.auth, "get_store", return_value=Mock()), \
             patch("research_ledger.ledger_payload", side_effect=self.auth.psycopg.OperationalError("mirror offline")):
            code, payload = self.handler.do_GET()
        self.assertEqual(code, 503)
        self.assertEqual(payload, {"detail": "研究证据账暂时不可用，请检查同步源"})

    def test_kline_response_does_not_wait_for_database_archive(self):
        self.handler.path = "/api/locals/klines?pair=BTC%2FUSDT%3AUSDT&timeframe=1h&limit=1&fresh=1"
        self.handler._bearer = Mock(return_value="test-only-token")
        import json
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps([[1700000000000, "100", "103", "99", "102", "5"]]).encode()
        cached = {("futures", "BTCUSDT", "1h", 1): (self.auth.time.time(), {"data": [["old"]]})}
        with patch.object(self.auth, "verify_token", return_value="admin"), \
             patch.object(self.auth, "_kline_cache", cached), \
             patch.object(self.auth.urllib.request, "urlopen", return_value=response), \
             patch.object(self.auth, "_kline_archive") as archive, \
             patch.object(self.auth, "get_store", side_effect=AssertionError("HTTP 不能同步访问数据库")):
            archive.offer.return_value = True
            code, payload = self.handler.do_GET()
        self.assertEqual(code, 200)
        self.assertEqual(payload["data"][0][4], 102)
        self.assertEqual(payload["storage"], "queued")
        archive.offer.assert_called_once()


class ForwardPaperPublicContractTests(unittest.TestCase):
    """Keep the forward-paper UI contract read-only and format-compatible."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="quant_forward_contract_")
        cls.addClassCleanup(cls.temp_dir.cleanup)
        spec = importlib.util.spec_from_file_location(
            "quant_auth_forward_contract_test", ROOT / "auth_service.py"
        )
        assert spec is not None and spec.loader is not None
        cls.auth = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"QUANT_AUTH_DIR": cls.temp_dir.name}):
            spec.loader.exec_module(cls.auth)

    def setUp(self):
        self.root = Path(self.temp_dir.name) / "runs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.root_patch = self.enterContext(
            patch.object(self.auth, "FORWARD_PAPER_ROOT", str(self.root))
        )

    def _handler(self, path):
        handler = object.__new__(self.auth.Handler)
        handler.path = path
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {}
        handler._read_body = Mock(return_value=b"")
        handler._bearer = Mock(return_value="test-only-token")
        handler._send = Mock(side_effect=lambda code, payload: (code, payload))
        return handler

    def _get(self, path, *, authenticated=True):
        handler = self._handler(path)
        with patch.object(self.auth, "verify_token", return_value=("admin" if authenticated else None)):
            result = handler.do_GET()
        return result

    def _runner_run(self, run_id="run-current"):
        run_dir = self.root / run_id
        run_dir.mkdir(exist_ok=True)
        manifest = {
            "event_type": "manifest", "run_id": run_id,
            "schema_version": "forward-paper-v1",
            "cost_completeness": {"fee": "modeled", "slippage": "unknown", "funding": "unknown"},
        }
        snapshot = {
            "event_type": "snapshot", "run_id": run_id,
            "data_ready": True, "replay_completed": True,
            "data_fingerprint": "fp-1",
            "candle_through_utc": "2026-10-10T00:00:00+00:00",
        }
        manifest["schema_version"] = "forward-paper-v2"
        (run_dir / "manifest.jsonl").write_text(
            "\n".join(json.dumps(row) for row in (manifest, snapshot)) + "\n",
            encoding="utf-8",
        )
        decisions = [
            {"event_type": "decision", "run_id": run_id, "event_id": f"d{i}", "variant_id": "v1"}
            for i in range(3)
        ]
        fills = [
            {"event_type": "fill", "run_id": run_id, "event_id": "f-entry", "action": "entry"},
            {"event_type": "fill", "run_id": run_id, "event_id": "f-exit", "action": "exit", "profit_abs": 2.5},
        ]
        for name, rows in (("decisions.jsonl", decisions), ("fills.jsonl", fills)):
            (run_dir / name).write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
        (run_dir / "checkpoint.json").write_text(
            json.dumps({"run_id": run_id, "unrealized_pnl": 1.25,
                        "database_ledger": {
                            "enabled": True, "synced_event_count": 7,
                            "sync_error": None,
                        }}), encoding="utf-8"
        )
        return run_dir

    def _multi_variant_run(self, run_id="run-multi", *, data_ready=True,
                           checkpoint_ready=True, replay_completed=True,
                           schema_version="forward-paper-v2"):
        """Build a runner-shaped run with deliberately non-additive variants."""
        run_dir = self.root / run_id
        run_dir.mkdir(exist_ok=True)
        variants = [
            {"variant_id": "base", "rule_hash": "hash-base", "hypothesis": "reference"},
            {"variant_id": "tight-sl", "rule_hash": "hash-tight", "hypothesis": "risk"},
        ]
        manifest = {
            "event_type": "manifest", "run_id": run_id,
            "schema_version": schema_version, "variants": variants,
            "cost_completeness": {"fee": "modeled", "slippage": "unknown", "funding": "unknown"},
        }
        snapshot = {
            "event_type": "snapshot", "run_id": run_id,
            "data_ready": data_ready, "data_fingerprint": "fp-multi",
            "candle_through_utc": "2026-10-10T00:00:00+00:00",
        }
        if replay_completed is not None:
            snapshot["replay_completed"] = replay_completed
        else:
            snapshot.pop("replay_completed", None)
        (run_dir / "manifest.jsonl").write_text(
            "\n".join(json.dumps(row) for row in (manifest, snapshot)) + "\n",
            encoding="utf-8",
        )
        decisions = [
            {"event_type": "decision", "run_id": run_id, "event_id": "d-base",
             "variant_id": "base", "rule_hash": "hash-base"},
            {"event_type": "decision", "run_id": run_id, "event_id": "d-tight",
             "variant_id": "tight-sl", "rule_hash": "hash-tight"},
        ]
        fills = [
            {"event_type": "fill", "run_id": run_id, "event_id": "f-base-entry",
             "variant_id": "base", "action": "entry", "coin": "BTC"},
            {"event_type": "fill", "run_id": run_id, "event_id": "f-base-exit",
             "variant_id": "base", "action": "exit", "coin": "BTC", "profit_abs": 10.0},
            {"event_type": "fill", "run_id": run_id, "event_id": "f-tight-entry",
             "variant_id": "tight-sl", "action": "entry", "coin": "ETH"},
            {"event_type": "fill", "run_id": run_id, "event_id": "f-tight-exit",
             "variant_id": "tight-sl", "action": "exit", "coin": "ETH", "profit_abs": -4.0},
        ]
        for name, rows in (("decisions.jsonl", decisions), ("fills.jsonl", fills)):
            (run_dir / name).write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
        checkpoint = {
            "run_id": run_id, "data_ready": checkpoint_ready,
            "unrealized_pnl": 999.0,
            "database_ledger": {
                "enabled": True, "synced_event_count": 7, "sync_error": None,
            },
            "variant_summaries": {
                "base": {
                    "closed_trade_count": 1,
                    "realized_profit_after_fee_before_unknown_costs": 10.0,
                    "unrealized_pnl_before_unknown_costs": 1.5,
                    "strategy_usable": True,
                },
                "tight-sl": {
                    "closed_trade_count": 1,
                    "realized_profit_after_fee_before_unknown_costs": -4.0,
                    "unrealized_pnl_before_unknown_costs": -2.5,
                    "strategy_usable": True,
                },
            },
        }
        (run_dir / "checkpoint.json").write_text(
            json.dumps(checkpoint), encoding="utf-8"
        )
        return run_dir

    def _canonical_run(self, run_id="run-canonical"):
        run_dir = self.root / run_id
        run_dir.mkdir(exist_ok=True)
        (run_dir / "manifest.json").write_text(json.dumps({
            "event_type": "manifest", "run_id": run_id,
            "schema_version": "forward-paper-v1",
            "cost_completeness": {"fee": "modeled"},
        }), encoding="utf-8")
        events = [
            {"event_type": "decision", "run_id": run_id, "event_id": "d1"},
            {"event_type": "fill", "run_id": run_id, "event_id": "f1", "action": "entry"},
            {"event_type": "close", "run_id": run_id, "event_id": "c1", "pnl": 3.75},
        ]
        (run_dir / "events.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in events), encoding="utf-8"
        )
        (run_dir / "state.json").write_text(json.dumps({
            "run_id": run_id, "data_ready": True,
            "data_fingerprint": "canonical-fp", "unrealized_pnl": 4.0,
        }), encoding="utf-8")
        return run_dir

    def test_requires_login_before_reading_root(self):
        handler = self._handler("/api/locals/forward-paper")
        with patch.object(self.auth, "verify_token", return_value=None), \
             patch.object(self.auth.Path, "resolve", side_effect=AssertionError("must not read files")):
            code, payload = handler.do_GET()
        self.assertEqual(code, 401)
        self.assertEqual(payload, {"detail": "未登录或登录已过期"})

    def test_reads_current_runner_format_and_calculates_summary(self):
        self._runner_run()
        code, payload = self._get("/api/locals/forward-paper?run_id=run-current&limit=2")
        self.assertEqual(code, 200)
        self.assertEqual(payload["run_id"], "run-current")
        self.assertEqual(payload["summary"]["closed_pnl"], 2.5)
        self.assertEqual(payload["summary"]["unrealized_pnl"], 1.25)
        self.assertEqual(payload["counts"], {"decisions": 3, "fills": 2, "closes": 1})
        self.assertEqual(len(payload["decisions"]), 2)
        self.assertTrue(payload["summary"]["data_ready"])
        self.assertEqual(payload["summary"]["database_ledger"]["synced_event_count"], 7)

    def test_returns_variant_scoped_results_without_combining_mutually_exclusive_pnl(self):
        self._multi_variant_run()
        code, payload = self._get("/api/locals/forward-paper?run_id=run-multi")
        self.assertEqual(code, 200)
        self.assertIn("variants", payload)
        self.assertEqual(
            payload["variants"]["base"]["realized_profit_after_fee_before_unknown_costs"],
            10.0,
        )
        self.assertEqual(
            payload["variants"]["tight-sl"]["realized_profit_after_fee_before_unknown_costs"],
            -4.0,
        )
        self.assertEqual(
            payload["variants"]["base"]["unrealized_pnl_before_unknown_costs"], 1.5
        )
        self.assertEqual(
            payload["variants"]["tight-sl"]["unrealized_pnl_before_unknown_costs"], -2.5
        )
        # These variants are alternative paper rules, not one portfolio.  A
        # scalar top-level P&L would invite the UI to report a fictitious sum.
        self.assertNotEqual(payload.get("summary", {}).get("closed_pnl"), 6.0)
        self.assertIsNone(payload["summary"]["unrealized_pnl"])

    def test_latest_not_ready_snapshot_overrides_stale_checkpoint_usability(self):
        self._multi_variant_run(data_ready=False, checkpoint_ready=True)
        code, payload = self._get("/api/locals/forward-paper?run_id=run-multi")
        self.assertEqual(code, 200)
        self.assertFalse(payload["summary"]["data_ready"])
        self.assertFalse(payload["summary"]["strategy_usable"])
        self.assertFalse(payload["variants"]["base"]["strategy_usable"])
        self.assertFalse(payload["variants"]["tight-sl"]["strategy_usable"])
        self.assertFalse(payload["latest_snapshot"]["data_ready"])

    def test_ready_data_without_completed_replay_is_not_strategy_usable(self):
        self._multi_variant_run(
            data_ready=True, checkpoint_ready=True, replay_completed=False
        )
        code, payload = self._get("/api/locals/forward-paper?run_id=run-multi")
        self.assertEqual(code, 200)
        self.assertTrue(payload["summary"]["data_ready"])
        self.assertFalse(payload["summary"]["replay_completed"])
        self.assertFalse(payload["summary"]["strategy_usable"])
        self.assertFalse(payload["variants"]["base"]["strategy_usable"])
        self.assertFalse(payload["variants"]["tight-sl"]["strategy_usable"])

    def test_v2_ready_snapshot_without_replay_marker_is_not_successful(self):
        self._multi_variant_run(
            data_ready=True, checkpoint_ready=True, replay_completed=None
        )
        code, payload = self._get("/api/locals/forward-paper?run_id=run-multi")
        self.assertEqual(code, 200)
        self.assertTrue(payload["summary"]["data_ready"])
        self.assertFalse(payload["summary"]["replay_completed"])
        self.assertFalse(payload["summary"]["strategy_usable"])
        self.assertFalse(payload["last_successful_replay"]["replay_completed"])

    def test_v3_and_future_snapshots_require_explicit_success_over_stale_state(self):
        """Readiness and a saved success must not upgrade a new observation."""
        for schema in ("forward-paper-v3", "forward-paper-v99"):
            for state_name in ("checkpoint.json", "state.json"):
                for index, replay_marker in enumerate((None, False, 1, "true", True)):
                    run_id = f"run-{schema}-{state_name.split('.')[0]}-{index}"
                    with self.subTest(schema=schema, state=state_name,
                                      marker=replay_marker):
                        run_dir = self._multi_variant_run(
                            run_id, schema_version=schema,
                            replay_completed=replay_marker,
                        )
                        saved = json.loads((run_dir / "checkpoint.json").read_text(
                            encoding="utf-8"
                        ))
                        saved["replay_completed"] = True
                        saved["candle_through_utc"] = "2026-10-09T00:00:00+00:00"
                        (run_dir / state_name).write_text(json.dumps(saved), encoding="utf-8")
                        code, payload = self._get(
                            "/api/locals/forward-paper?run_id=" + run_id
                        )
                        successful = replay_marker is True
                        self.assertEqual(code, 200)
                        self.assertTrue(payload["summary"]["data_ready"])
                        self.assertIs(payload["summary"]["replay_completed"], successful)
                        self.assertIs(payload["summary"]["strategy_usable"], successful)
                        self.assertIs(payload["last_successful_replay"]["replay_completed"], successful)
                        for result in payload["variants"].values():
                            self.assertIs(result["strategy_usable"], successful)

    def test_v3_and_future_nested_snapshots_cannot_inherit_saved_replay_success(self):
        for schema in ("forward-paper-v3", "forward-paper-v99"):
            for replay_marker in (None, False, True):
                run_id = f"run-nested-{schema}-{replay_marker}"
                with self.subTest(schema=schema, marker=replay_marker):
                    run_dir = self._canonical_run(run_id)
                    manifest_path = run_dir / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["schema_version"] = schema
                    manifest["latest_snapshot"] = {
                        "data_ready": True, "data_fingerprint": "new-observation",
                    }
                    if replay_marker is not None:
                        manifest["latest_snapshot"]["replay_completed"] = replay_marker
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    state_path = run_dir / "state.json"
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    state["replay_completed"] = True
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    code, payload = self._get(
                        "/api/locals/forward-paper?run_id=" + run_id
                    )
                    self.assertEqual(code, 200)
                    successful = replay_marker is True
                    self.assertIs(payload["summary"]["replay_completed"], successful)
                    self.assertIs(payload["summary"]["strategy_usable"], successful)
                    self.assertIs(payload["last_successful_replay"]["replay_completed"], successful)

    def test_v3_and_future_state_only_runs_require_explicit_replay_marker(self):
        for schema in ("forward-paper-v3", "forward-paper-v99"):
            for state_name in ("state.json", "checkpoint.json"):
                for replay_marker in (None, True):
                    run_id = f"run-state-only-{schema}-{state_name.split('.')[0]}-{replay_marker}"
                    with self.subTest(schema=schema, state=state_name,
                                      marker=replay_marker):
                        run_dir = self._canonical_run(run_id)
                        manifest_path = run_dir / "manifest.json"
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                        manifest["schema_version"] = schema
                        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                        state_path = run_dir / "state.json"
                        state = json.loads(state_path.read_text(encoding="utf-8"))
                        state_path.unlink()
                        if replay_marker is not None:
                            state["replay_completed"] = replay_marker
                        (run_dir / state_name).write_text(json.dumps(state), encoding="utf-8")
                        code, payload = self._get(
                            "/api/locals/forward-paper?run_id=" + run_id
                        )
                        self.assertEqual(code, 200)
                        self.assertTrue(payload["summary"]["data_ready"])
                        self.assertIs(payload["summary"]["replay_completed"], replay_marker is True)
                        self.assertIs(payload["summary"]["strategy_usable"], replay_marker is True)

    def test_v3_rule_and_execution_evidence_survives_both_ledger_formats(self):
        rules = {
            "range_filter": {
                "kind": "adx", "window": 14, "threshold": 25.0,
                "op": "gt", "warmup_bars": 28,
            },
            "take_profit": {"kind": "fixed_pct", "value": 1.0, "atr_window": None},
            "stop_loss": {"kind": "fixed_pct", "value": 0.5, "atr_window": None},
            "execution": {
                "dual_touch": "stop_loss_first", "gap_fill": "execution_open",
                "exit_timing": "next_open", "slot_release": "after_exit_fill_next_open",
            },
        }
        filter_evidence = {
            **rules["range_filter"], "raw_value": None, "ready": False,
            "passed": False, "reason": "indicator_not_ready",
        }
        trigger = {
            "reason": "stop_loss", "candle_utc": "2026-10-10T00:00:00+00:00",
            "trigger_level": 99.5, "observed_price": 99.2,
            "observed_high": 101.5, "observed_low": 99.2,
            "observed_open": 100.1, "observed_close": 100.0,
            "take_profit_level": 101.0, "stop_loss_level": 99.5,
            "dual_touch": True, "dual_touch_policy": "stop_loss_first",
        }
        risk_state = {
            "entry_atr": None, "take_profit_level": 101.0, "stop_loss_level": 99.5,
            "trailing_extreme": 102.0, "trailing_level": None,
        }
        for ledger_format in ("split", "canonical"):
            run_id = f"run-v3-evidence-{ledger_format}"
            with self.subTest(ledger_format=ledger_format):
                run_dir = self._multi_variant_run(run_id, schema_version="forward-paper-v3")
                manifest_path = run_dir / "manifest.jsonl"
                manifest_rows = [json.loads(row) for row in manifest_path.read_text(
                    encoding="utf-8"
                ).splitlines()]
                manifest_rows[0]["variants"][1].update(rules)
                manifest_path.write_text(
                    "".join(json.dumps(row) + "\n" for row in manifest_rows), encoding="utf-8"
                )
                decision = {
                    "event_type": "decision", "run_id": run_id, "event_id": "d-risk",
                    "variant_id": "tight-sl", "rule_hash": "hash-tight",
                    "candidates": [{"coin": "ETH", "decision": "deny",
                                    "range_filter": filter_evidence, "risk_ready": False}],
                    "exits": [{"coin": "BTC", "trigger": trigger}],
                }
                fills = [
                    {"event_type": "fill", "run_id": run_id, "event_id": "f-known",
                     "variant_id": "base", "action": "exit", "profit_abs": 10.0},
                    {"event_type": "fill", "run_id": run_id, "event_id": "f-risk",
                     "variant_id": "tight-sl", "action": "exit", "profit_abs": None,
                     "coin": "BTC", "side": "long", "price": 99.0,
                     "filled_at_utc": "2026-10-10T01:00:00+00:00",
                     "trigger": trigger, "risk_state": risk_state,
                     "decision_event_id": "d-risk"},
                ]
                if ledger_format == "canonical":
                    (run_dir / "events.jsonl").write_text(
                        "".join(json.dumps(row) + "\n" for row in (decision, *fills)),
                        encoding="utf-8",
                    )
                else:
                    (run_dir / "decisions.jsonl").write_text(
                        json.dumps(decision) + "\n", encoding="utf-8"
                    )
                    (run_dir / "fills.jsonl").write_text(
                        "".join(json.dumps(row) + "\n" for row in fills), encoding="utf-8"
                    )
                code, payload = self._get("/api/locals/forward-paper?run_id=" + run_id)
                self.assertEqual(code, 200)
                self.assertEqual(payload["manifest"], manifest_rows[0])
                self.assertEqual(payload["decisions"], [decision])
                self.assertEqual(payload["fills"], fills)
                self.assertEqual(payload["closes"], fills)
                self.assertEqual(payload["variants"]["base"][
                    "realized_profit_after_fee_before_unknown_costs"
                ], 10.0)
                self.assertIsNone(payload["variants"]["tight-sl"][
                    "realized_profit_after_fee_before_unknown_costs"
                ])
                self.assertIsNone(payload["summary"]["closed_pnl"])
                self.assertIsNone(payload["summary"]["cross_variant_closed_pnl"])
                self.assertIsNone(payload["summary"]["unrealized_pnl"])
                self.assertIsNone(payload["summary"]["net_pnl"])

    def test_reads_canonical_manifest_events_and_state(self):
        self._canonical_run()
        code, payload = self._get("/api/locals/forward-paper?run_id=run-canonical")
        self.assertEqual(code, 200)
        self.assertEqual(payload["manifest"]["schema_version"], "forward-paper-v1")
        self.assertEqual(payload["counts"], {"decisions": 1, "fills": 1, "closes": 1})
        self.assertEqual(payload["summary"]["closed_pnl"], 3.75)
        self.assertEqual(payload["summary"]["unrealized_pnl"], 4.0)
        self.assertEqual(payload["summary"]["data_fingerprint"], "canonical-fp")

    def test_manifest_nested_snapshot_controls_successful_replay(self):
        run_dir = self._canonical_run("run-nested-snapshot")
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["latest_snapshot"] = {
            "data_ready": True,
            "replay_completed": True,
            "candle_through_utc": "2026-10-10T00:00:00+00:00",
            "data_fingerprint": "nested-fp",
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        code, payload = self._get(
            "/api/locals/forward-paper?run_id=run-nested-snapshot"
        )
        self.assertEqual(code, 200)
        self.assertTrue(payload["last_successful_replay"]["replay_completed"])
        self.assertEqual(
            payload["last_successful_replay"]["data_fingerprint"], "nested-fp"
        )

        manifest["latest_snapshot"]["replay_completed"] = False
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        code, payload = self._get(
            "/api/locals/forward-paper?run_id=run-nested-snapshot"
        )
        self.assertEqual(code, 200)
        self.assertTrue(payload["latest_snapshot"]["data_ready"])
        self.assertFalse(payload["last_successful_replay"]["replay_completed"])

    def test_unknown_close_pnl_is_not_reported_as_zero(self):
        run_dir = self._canonical_run("run-unknown-pnl")
        events = [
            {"event_type": "close", "run_id": "run-unknown-pnl",
             "event_id": "close-unknown"},
        ]
        (run_dir / "events.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in events), encoding="utf-8"
        )
        code, payload = self._get(
            "/api/locals/forward-paper?run_id=run-unknown-pnl"
        )
        self.assertEqual(code, 200)
        self.assertIsNone(payload["summary"]["closed_pnl"])
        self.assertIsNone(
            payload["variants"].get("base", {}).get(
                "realized_profit_after_fee_before_unknown_costs"
            )
        )

    def test_reads_runner_output_written_directly_at_configured_root(self):
        run_id = "run-root-output"
        manifest = {
            "event_type": "manifest", "run_id": run_id,
            "schema_version": "forward-paper-v1",
        }
        snapshot = {
            "event_type": "snapshot", "run_id": run_id,
            "data_ready": True, "data_fingerprint": "root-fp",
        }
        (self.root / "manifest.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in (manifest, snapshot)),
            encoding="utf-8",
        )
        (self.root / "decisions.jsonl").write_text("", encoding="utf-8")
        (self.root / "fills.jsonl").write_text("", encoding="utf-8")
        (self.root / "checkpoint.json").write_text(json.dumps({
            "run_id": run_id, "data_ready": True,
        }), encoding="utf-8")
        code, payload = self._get(
            "/api/locals/forward-paper?run_id=" + run_id
        )
        self.assertEqual(code, 200)
        self.assertEqual(payload["run_id"], run_id)
        self.assertEqual(payload["summary"]["data_fingerprint"], "root-fp")
        self.assertTrue(payload["summary"]["replay_completed"])
        self.assertTrue(payload["last_successful_replay"]["replay_completed"])

    def test_keeps_counterfactual_variant_pnl_separate(self):
        run_id = "run-multi-variant"
        run_dir = self.root / run_id
        run_dir.mkdir(exist_ok=True)
        (run_dir / "manifest.json").write_text(json.dumps({
            "event_type": "manifest", "run_id": run_id,
            "schema_version": "forward-paper-v2",
            "variants": [{"variant_id": "base"}, {"variant_id": "tight"}],
            "latest_snapshot": {"data_ready": True, "replay_completed": True},
        }), encoding="utf-8")
        events = [
            {"event_type": "close", "run_id": run_id, "variant_id": "base",
             "event_id": "c-base", "pnl": 2.0},
            {"event_type": "close", "run_id": run_id, "variant_id": "tight",
             "event_id": "c-tight", "pnl": -1.0},
        ]
        (run_dir / "events.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in events), encoding="utf-8"
        )
        (run_dir / "state.json").write_text(json.dumps({
            "run_id": run_id, "data_ready": True, "replay_completed": True,
            "variant_summaries": {
                "base": {"unrealized_pnl_before_unknown_costs": 0.5,
                         "strategy_usable": True},
                "tight": {"unrealized_pnl_before_unknown_costs": -0.25,
                          "strategy_usable": True},
            },
        }), encoding="utf-8")
        code, payload = self._get(
            "/api/locals/forward-paper?run_id=" + run_id
        )
        self.assertEqual(code, 200)
        self.assertIsNone(payload["summary"]["closed_pnl"])
        self.assertEqual(payload["summary"]["cross_variant_closed_pnl"], 1.0)
        self.assertEqual(payload["variants"]["base"]["closed_trade_count"], 1)
        self.assertEqual(
            payload["variants"]["base"]["realized_profit_after_fee_before_unknown_costs"],
            2.0,
        )
        self.assertTrue(payload["variants"]["base"]["strategy_usable"])
        self.assertTrue(payload["last_successful_replay"]["replay_completed"])
        self.assertIsNone(payload["summary"]["unrealized_pnl"])
        self.assertEqual(payload["last_successful_replay"]["variants"]["base"]["strategy_usable"], True)

    def test_reads_legacy_canonical_manifest_without_event_type(self):
        run_dir = self._canonical_run("run-legacy-manifest")
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        manifest.pop("event_type")
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        code, payload = self._get(
            "/api/locals/forward-paper?run_id=run-legacy-manifest"
        )
        self.assertEqual(code, 200)
        self.assertEqual(payload["run_id"], "run-legacy-manifest")

    def test_rejects_path_traversal_and_absolute_run_ids(self):
        for run_id in ("../outside", "/tmp/outside", "..\\outside"):
            code, _payload = self._get(
                "/api/locals/forward-paper?run_id=" + urllib.parse.quote(run_id, safe="")
            )
            self.assertEqual(code, 400)

    def test_rejects_run_directory_symlink(self):
        outside = Path(self.temp_dir.name) / "outside"
        outside.mkdir()
        os.symlink(outside, self.root / "run-link")
        code, _payload = self._get("/api/locals/forward-paper?run_id=run-link")
        self.assertEqual(code, 400)

    def test_limit_is_validated_and_truncates_rows(self):
        self._runner_run()
        code, _payload = self._get("/api/locals/forward-paper?run_id=run-current&limit=0")
        self.assertEqual(code, 400)
        code, _payload = self._get("/api/locals/forward-paper?run_id=run-current&limit=101")
        self.assertEqual(code, 400)
        code, payload = self._get("/api/locals/forward-paper?run_id=run-current&limit=1")
        self.assertEqual(code, 200)
        self.assertEqual(payload["limit"], 1)
        self.assertEqual(len(payload["decisions"]), 1)
        self.assertEqual(len(payload["fills"]), 1)
        self.assertEqual(len(payload["closes"]), 1)

    def test_missing_split_ledger_is_not_silently_accepted(self):
        run_dir = self._runner_run()
        (run_dir / "fills.jsonl").unlink()
        code, _payload = self._get("/api/locals/forward-paper?run_id=run-current")
        self.assertEqual(code, 404)

    def test_corrupt_events_and_mismatched_identity_are_server_errors(self):
        run_dir = self._canonical_run("run-bad-json")
        (run_dir / "events.jsonl").write_text("{broken\n", encoding="utf-8")
        code, _payload = self._get("/api/locals/forward-paper?run_id=run-bad-json")
        self.assertEqual(code, 500)

        run_dir = self._canonical_run("run-bad-identity")
        (run_dir / "events.jsonl").write_text(
            json.dumps({"event_type": "decision", "run_id": "other"}) + "\n",
            encoding="utf-8",
        )
        code, _payload = self._get("/api/locals/forward-paper?run_id=run-bad-identity")
        self.assertEqual(code, 500)


if __name__ == "__main__":
    unittest.main()

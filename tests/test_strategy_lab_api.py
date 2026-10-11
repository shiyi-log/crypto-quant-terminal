"""Strategy-account HTTP projection requires auth and cannot drive the trading clock."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import auth_service


class StrategyLabApiTests(unittest.TestCase):
    def handler(self, query=""):
        h = object.__new__(auth_service.Handler)
        h.path = "/api/locals/strategy-lab" + query
        h._bearer = Mock(return_value="test")
        h._read_body = Mock()
        h._send = Mock(side_effect=lambda code, payload: (code, payload))
        return h

    def test_unauthenticated_request_never_reads_database(self):
        with patch.object(auth_service, "verify_token", return_value=None), \
                patch.object(auth_service, "get_store") as store:
            code, _ = self.handler().do_GET()
        self.assertEqual(code, 401)
        store.assert_not_called()

    def test_invalid_parameters_rejected_before_database(self):
        for query in ("?strategy_id=../x", "?limit=0", "?limit=501", "?limit=oops",
                      "?strategy_id=x&strategy_id=y", "?other=x",
                      "?strategy_id=s01.arbitrary", "?strategy_id=s01_orb_breakout_B15m_R50_k9.0_100x_maker"):
            with self.subTest(query=query), \
                    patch.object(auth_service, "verify_token", return_value="admin"), \
                    patch.object(auth_service, "get_store") as store:
                self.assertEqual(self.handler(query).do_GET()[0], 400)
                store.assert_not_called()

    def test_overview_is_readonly_with_explicit_execution_limits(self):
        engine = Mock()
        engine.overview.return_value = {"accounts": [{"strategy_id": "s01"}]}
        with patch.object(auth_service, "verify_token", return_value="admin"), \
                patch.object(auth_service, "get_store", return_value=Mock()), \
                patch("strategy_simulation.StrategySimulation", return_value=engine), \
                patch("strategy_lab_service.read_service_status", return_value={"running": True}):
            code, body = self.handler().do_GET()
        self.assertEqual(code, 200)
        self.assertEqual(body["execution"]["funding"], "unknown")
        self.assertEqual(body["execution"]["fee_rate_per_side"], 0.0002)
        self.assertEqual(body["execution"]["active_variant_suffix"], "_100x_maker")
        self.assertFalse(any(
            item["strategy_id"] == "s31_delta_neutral_carry"
            for item in body["blocked_strategies"]
        ))
        carry = next(item for item in body["rejected_strategies"]
                     if item["strategy_id"] == "s31_delta_neutral_carry")
        self.assertEqual(carry["status"], "rejected")
        self.assertIn("0.08%", carry["reason"])
        engine.ensure_accounts.assert_not_called()
        engine.process_batch.assert_not_called()

    def test_rejected_research_is_separate_from_actual_data_blockers(self):
        specs = {"strategies": [
            {"strategy_id": "s31_delta_neutral_carry", "data_ready": False,
             "hypothesis_status": "rejected", "data_blocker": "过时的数据阻塞原因",
             "rejection_reason": "两腿成本修正后已淘汰"},
            {"strategy_id": "retired_arm", "data_ready": True,
             "arm_status": "REJECTED"},
            {"strategy_id": "awaiting_data", "data_ready": False,
             "data_blocker": "仍缺少有效价格覆盖"},
            {"strategy_id": "ready_research", "data_ready": True},
        ]}
        engine = Mock()
        engine.overview.return_value = {"accounts": []}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "research_multi_strategy" / "strategy_specs.json"
            path.parent.mkdir()
            path.write_text(json.dumps(specs), encoding="utf-8")
            with patch.object(auth_service, "ROOT", root), \
                    patch.object(auth_service, "verify_token", return_value="admin"), \
                    patch.object(auth_service, "get_store", return_value=Mock()), \
                    patch("strategy_simulation.StrategySimulation", return_value=engine), \
                    patch("strategy_lab_service.read_service_status", return_value={}):
                code, body = self.handler().do_GET()
        self.assertEqual(code, 200)
        self.assertEqual([item["strategy_id"] for item in body["blocked_strategies"]],
                         ["awaiting_data"])
        self.assertEqual(body["blocked_strategies"][0]["reason"], "仍缺少有效价格覆盖")
        self.assertEqual([item["strategy_id"] for item in body["rejected_strategies"]],
                         ["s31_delta_neutral_carry", "retired_arm"])
        self.assertEqual(body["rejected_strategies"][0]["reason"], "两腿成本修正后已淘汰")
        self.assertEqual(body["rejected_strategies"][1]["status"], "rejected")
        engine.ensure_accounts.assert_not_called()
        engine.process_batch.assert_not_called()

    def test_account_is_scoped_or_404(self):
        engine = Mock()
        engine.account_detail.return_value = None
        with patch.object(auth_service, "verify_token", return_value="admin"), \
                patch.object(auth_service, "get_store", return_value=Mock()), \
                patch("strategy_simulation.StrategySimulation", return_value=engine):
            self.assertEqual(self.handler("?strategy_id=s01&limit=15").do_GET()[0], 404)
        engine.account_detail.assert_called_once_with("s01", limit=15)

    def test_generated_arm_ids_can_load_detail_without_case_conversion(self):
        from strategy_lab_service import load_specs

        strategy_ids = [item["strategy_id"] for item in load_specs()]
        self.assertTrue(any("_Dgen3_" in value for value in strategy_ids))
        for strategy_id in strategy_ids:
            with self.subTest(strategy_id=strategy_id):
                engine = Mock()
                engine.account_detail.return_value = {"account": {"strategy_id": strategy_id}}
                with patch.object(auth_service, "verify_token", return_value="admin"), \
                        patch.object(auth_service, "get_store", return_value=Mock()), \
                        patch("strategy_simulation.StrategySimulation", return_value=engine), \
                        patch("strategy_lab_service.read_service_status", return_value={"running": True}):
                    code, body = self.handler(f"?strategy_id={strategy_id}").do_GET()
                self.assertEqual(code, 200)
                self.assertEqual(body["account"]["strategy_id"], strategy_id)
                engine.account_detail.assert_called_once_with(strategy_id, limit=100)

    def test_all_factor_ids_with_decimal_k_can_load_detail(self):
        from strategy_factorial import load_factorial_specs

        for spec in load_factorial_specs():
            strategy_id = spec["strategy_id"]
            with self.subTest(strategy_id=strategy_id):
                engine = Mock()
                engine.account_detail.return_value = {"account": {"strategy_id": strategy_id}}
                with patch.object(auth_service, "verify_token", return_value="admin"), \
                        patch.object(auth_service, "get_store", return_value=Mock()), \
                        patch("strategy_simulation.StrategySimulation", return_value=engine):
                    code, body = self.handler(f"?strategy_id={strategy_id}").do_GET()
                self.assertEqual(code, 200)
                self.assertEqual(body["account"]["strategy_id"], strategy_id)
                engine.account_detail.assert_called_once_with(strategy_id, limit=100)


if __name__ == "__main__":
    unittest.main()

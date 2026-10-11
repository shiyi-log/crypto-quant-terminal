"""Actual-fill accounting and frontend recovery invariants, without a network."""
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from demo_execution_service import DemoRunnerConfig, derive_round_trips
from demo_runner_controller import DemoRunnerController, STATE_KEY


def fill(trade_id, side, quantity, timestamp, *, pnl="0", fee="0.1", asset="USDT", **extra):
    return {"exchange_trade_id": str(trade_id), "client_id": "entry" if side == "buy" else "exit",
            "pair": "BTC/USDT:USDT", "side": side, "quantity": quantity,
            "timestamp_ms": timestamp, "realized_pnl": pnl, "commission": fee,
            "commission_asset": asset, "is_maker": side == "buy", **extra}


class ActualFillAccountingTests(unittest.TestCase):
    def trade(self, **exit_fields):
        return derive_round_trips([fill(1, "buy", "2", 1000),
                                   fill(2, "sell", "2", 3500, pnl="4", **exit_fields)])[0]

    def test_actual_net_fee_and_seconds_do_not_use_configured_maker_rate(self):
        row = self.trade(fee="0.7")
        self.assertEqual(row["actual_net_trading_pnl_usdt"], "3.2")
        self.assertEqual(row["hold_seconds"], "2.5")
        self.assertEqual((row["maker_fills"], row["taker_fills"]), (1, 1))
        self.assertTrue(row["trading_pnl_excludes_funding"])
        self.assertFalse(row["live_profitability_verified"])

    def test_missing_fee_asset_realized_pnl_and_conflict_all_keep_net_unknown(self):
        for fields in ({"fee": None}, {"asset": None}, {"pnl": None},
                       {"commission_conflict": True}):
            with self.subTest(fields=fields):
                rows = [fill(1, "buy", "2", 1000), fill(2, "sell", "2", 3500, **fields)]
                self.assertIsNone(derive_round_trips(rows)[0]["actual_net_trading_pnl_usdt"])

    def test_non_usdt_fee_has_no_invented_conversion(self):
        row = self.trade(fee="0.001", asset="BNB")
        self.assertIsNone(row["actual_net_trading_pnl_usdt"])
        self.assertEqual(row["actual_commission_by_asset"]["BNB"], "0.001")

    def test_incomplete_order_fills_cannot_be_called_a_completed_profit_sample(self):
        rows = [fill(1, "buy", "1", 1000), fill(2, "sell", "1", 3500, pnl="4")]
        orders = [{"client_id": "entry", "filled_quantity": "2"},
                  {"client_id": "exit", "filled_quantity": "1"}]
        trade = derive_round_trips(rows, orders)[0]
        self.assertFalse(trade["fill_reconciliation_complete"])
        self.assertIsNone(trade["actual_realized_pnl_usdt"])
        self.assertIsNone(trade["actual_net_trading_pnl_usdt"])

    def test_numeric_same_millisecond_trade_ids_keep_exchange_sequence(self):
        rows = [fill(9, "buy", "1", 1000), fill(10, "sell", "1", 1000)]
        self.assertEqual(derive_round_trips(rows)[0]["exchange_trade_ids"], ["9", "10"])

    def test_partial_exit_is_not_yet_a_round_trip(self):
        self.assertEqual(derive_round_trips([fill(1, "buy", "2", 1000),
                                           fill(2, "sell", "1", 3500)]), [])

    def test_policy_records_demo_cap_without_promising_paper_risk(self):
        policy = DemoRunnerConfig("paper").policy()
        self.assertEqual(policy["max_notional_usdt"], "1000")
        self.assertIn("not_guaranteed", policy["risk_loss_bound"])
        self.assertIn("not_full_portfolio_returns", policy["comparison_scope"])


@dataclass
class Credentials:
    api_key: str = "offline-placeholder-key"
    secret: str = "offline-placeholder-secret"


class DocumentStore:
    def __init__(self):
        self.documents = {}

    def get_document(self, key, default=None):
        return self.documents.get(key, default)

    def put_document(self, key, payload):
        self.documents[key] = payload


class ControllerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data, self.ledger, self.paper = DocumentStore(), Mock(), Mock()
        self.ledger.virtual_account_key = "offline_demo"
        self.ledger.get_binding.return_value = None
        self.ledger.net_fill_positions.return_value = {}
        self.ledger.pending_orders.return_value = []
        self.paper.account.return_value = {"execution": {"risk_sizing_mode": "entry_atr"}}
        self.provider, self.factory = Mock(return_value=Credentials()), Mock()
        self.created = []

        def service_factory(adapter, store, source, config, **kwargs):
            service = Mock()
            service.config, service.policy_hash = config, "policy"
            state = {"running": True, "entries_enabled": False,
                     "forward_boundary_ms": 123, "status": "paused_flat"}
            def start(*, entries_enabled):
                self.assertEqual(self.data.documents[STATE_KEY]["selectedAccountId"], config.strategy_id)
                self.assertFalse(self.data.documents[STATE_KEY]["entriesEnabled"])
                state["entries_enabled"] = entries_enabled
                return state
            service.start.side_effect = start
            service.status.side_effect = lambda: dict(state)
            service.stop.side_effect = lambda **kwargs: state.update(entries_enabled=False)
            self.created.append(service)
            return service

        self.service_patch = patch("demo_runner_controller.DemoExecutionService", side_effect=service_factory)
        self.thread_patch = patch("demo_runner_controller.threading.Thread")
        self.service_patch.start()
        self.thread_patch.start()
        self.addCleanup(self.service_patch.stop)
        self.addCleanup(self.thread_patch.stop)
        self.controller = DemoRunnerController(self.data, self.ledger, self.provider, self.factory,
            paper_source=self.paper, lock_path=Path(self.temp.name) / "demo.lock")
        self.addCleanup(self.controller.close_if_flat)

    def test_human_start_saves_recovery_identity_before_exchange_work(self):
        self.controller.start("paper")
        self.created[0].start.assert_called_once_with(entries_enabled=True)
        self.factory.assert_called_once_with(api_key=Credentials().api_key,
                                            api_secret=Credentials().secret, allow_trading=True)
        saved = json.dumps(self.data.documents)
        self.assertNotIn(Credentials().secret, saved)
        self.assertNotIn(Credentials().api_key, saved)

    def test_restart_can_only_resume_protection_without_new_entries(self):
        self.data.put_document(STATE_KEY, {"selectedAccountId": "paper", "entriesEnabled": True})
        result = self.controller.resume_protection()
        self.created[0].start.assert_called_once_with(entries_enabled=False)
        self.assertFalse(result["entriesEnabled"])
        self.assertTrue(result["recoveryMode"])

    def test_stop_pauses_entries_but_keeps_controller_worker_for_risk(self):
        self.controller.start("paper")
        service = self.controller.service
        self.controller.stop()
        service.stop.assert_called_once_with(flatten=False)
        self.assertIs(self.controller.service, service)

    def test_bound_identity_cannot_be_switched_to_another_paper_wallet(self):
        self.ledger.get_binding.return_value = {"strategy_id": "frozen-paper"}
        with self.assertRaisesRegex(ValueError, "another paper identity"):
            self.controller.start("different-paper")
        self.factory.assert_not_called()

    def test_unavailable_credentials_recovery_is_redacted_and_retains_selection(self):
        self.data.put_document(STATE_KEY, {"selectedAccountId": "paper"})
        self.provider.side_effect = ValueError("credential-secret-must-not-be-shown")
        result = self.controller.resume_protection()
        self.assertEqual(result["selectedAccountId"], "paper")
        self.assertIn("ValueError", result["controllerError"])
        self.assertNotIn("credential-secret", json.dumps(result))
        self.assertFalse(self.data.documents[STATE_KEY]["entriesEnabled"])

    def test_controller_will_not_abandon_owned_exposure_on_shutdown(self):
        self.controller.start("paper")
        self.ledger.net_fill_positions.return_value = {"BTC/USDT:USDT": "0.001"}
        with self.assertRaisesRegex(RuntimeError, "owned exposure"):
            self.controller.close_if_flat()
        self.assertIsNotNone(self.controller.service)
        self.ledger.net_fill_positions.return_value = {}


if __name__ == "__main__":
    unittest.main()

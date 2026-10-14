"""Actual-fill accounting and frontend recovery invariants, without a network."""
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from demo_execution_service import (DemoExecutionService, DemoRunnerConfig,
                                    attempted_entry_outcomes, derive_round_trips,
                                    signal_reference_evidence)
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


class ExecutionEvidenceTests(unittest.TestCase):
    def test_closed_reference_records_original_decimal_and_close_instant(self):
        evidence = signal_reference_evidence({"timestamp": 900_000, "close": "60000.1900",
                                               "extras": {"closed": True}}, "15m")
        self.assertEqual(evidence["signal_reference_price"], "60000.1900")
        self.assertEqual(evidence["reference_timestamp_ms"], 1_800_000)
        self.assertEqual(evidence["reference_source"], "paper_closed_candle_from_live_market")
        self.assertFalse(evidence["live_executable_slippage_verified"])
        self.assertIn("environment_and_latency", evidence["reference_comparison_scope"])

    def test_unavailable_or_unclosed_reference_is_unknown_without_a_market_fallback(self):
        for candle in (None, {}, {"close": "60000.1", "extras": {"closed": False}},
                       {"close": "NaN", "timestamp": 900_000, "extras": {"closed": True}}):
            with self.subTest(candle=candle):
                evidence = signal_reference_evidence(candle, "15m")
                for key in ("signal_reference_price", "reference_timestamp_ms", "reference_source"):
                    self.assertIsNone(evidence[key])

    def test_nonfill_population_excludes_unattempted_intents_and_includes_attempt_rejection(self):
        def order(status, attempts, filled="0", exchange_id=None, reduce=False):
            return {"status": status, "submission_attempts": attempts, "filled_quantity": filled,
                    "exchange_order_id": exchange_id, "reduce_only": reduce}
        evidence = attempted_entry_outcomes([
            order("expired", 0), order("intent", 0), order("unknown", 1),
            order("filled", 1, "1", "100"), order("rejected", 1),
            order("canceled", 0, "0", "101"), order("filled", 1, "1", "102", True)])
        self.assertEqual(evidence["terminal_entry_count"], 3)
        self.assertEqual(evidence["unfilled_entry_count"], 2)
        self.assertEqual(evidence["unfilled_entry_ratio"], 2 / 3)
        self.assertEqual(evidence["unfilled_entry_population"], "terminal_attempted_entries_not_verified_accepted")

    def test_visible_entry_fillability_metrics_classify_rejections_and_nonfills(self):
        def order(status, attempts=1, filled="0", quantity="1", exchange_id=None,
                  reduce=False, audit=None):
            return {"status": status, "submission_attempts": attempts,
                    "filled_quantity": filled, "quantity": quantity,
                    "exchange_order_id": exchange_id, "reduce_only": reduce,
                    "created_ms": 10, "audit_observations": audit or []}

        metrics = attempted_entry_outcomes([
            order("rejected", audit=[{"raw": {"error_kind": "OrderNotFillable"},
                                      "observed_ms": 101}]),
            order("canceled", exchange_id="cancelled", filled="0"),
            order("filled", exchange_id="filled", filled="1"),
            order("partially_filled", exchange_id="partial", filled=".25", quantity="1"),
            order("intent", attempts=0),
        ])
        self.assertEqual(metrics["entry_attempts"], 4)
        self.assertEqual(metrics["entry_submitted"], 3)
        self.assertEqual(metrics["entry_rejected_order_not_fillable"], 1)
        self.assertEqual(metrics["entry_expired_non_fill"], 1)
        self.assertEqual(metrics["entry_filled"], 1)
        self.assertEqual(metrics["entry_partial"], 1)
        self.assertEqual(metrics["rejection_rate"], .25)
        self.assertEqual(metrics["non_fill_rate"], 1 / 3)
        self.assertEqual(metrics["total_entry_failure_rate"], .5)
        self.assertEqual(metrics["last_rejection_ms"], 101)
        self.assertEqual(metrics["last_rejection_kind"], "OrderNotFillable")

    def test_candle_account_and_five_second_poll_do_not_test_seconds_hypothesis(self):
        service = DemoExecutionService(Mock(), Mock(), Mock(), DemoRunnerConfig("paper"))
        self.assertEqual(service.config.poll_interval_ms, 5000)
        self.assertFalse(service.status()["seconds_hypothesis_tested"])
        self.assertEqual(service.status()["experiment_scope"], "selected_candle_strategy_execution_only")

    def test_status_returns_last_snapshot_without_waiting_for_busy_cycle_lock(self):
        service = DemoExecutionService(Mock(), Mock(), Mock(), DemoRunnerConfig("paper"))
        service._status.update(status="observing", running=True)
        with service._mutex:
            service._publish_status_snapshot()
        release = threading.Event()
        held = threading.Event()

        def hold_cycle_lock():
            with service._mutex:
                held.set()
                release.wait(timeout=2)

        worker = threading.Thread(target=hold_cycle_lock)
        worker.start()
        self.assertTrue(held.wait(timeout=1))
        started = time.monotonic()
        snapshot = service.status()
        elapsed = time.monotonic() - started
        release.set()
        worker.join(timeout=1)
        self.assertLess(elapsed, 0.1)
        self.assertEqual(snapshot["status"], "observing")
        self.assertTrue(snapshot["status_snapshot_stale"])
        self.assertTrue(snapshot["status_lock_busy"])


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
        self.ledger.runner_orders.return_value = []
        self.ledger.runner_fills.return_value = []
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
        thread_factory = self.thread_patch.start()
        # The mocked worker finishes when joined; an unspecified Mock result
        # is truthy and would correctly fail the flat-handoff safety guard.
        worker = thread_factory.return_value
        worker.join.side_effect = lambda **kwargs: setattr(worker.is_alive, "return_value", False)
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
        self.data.put_document(STATE_KEY, {"selectedAccountId": "paper", "entriesEnabled": True,
                                          "virtualAccountKey": "offline_demo"})
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
        self.data.put_document(STATE_KEY, {"selectedAccountId": "paper", "virtualAccountKey": "offline_demo"})
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

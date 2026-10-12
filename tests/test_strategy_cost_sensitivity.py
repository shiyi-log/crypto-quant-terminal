"""Fee sensitivity reconciles frozen complete legs and never executes a replay."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tools.strategy_cost_sensitivity import (
    SAMPLE_QUALITY_CUTOFF_MS, SCENARIOS, analyze_snapshot, main, read_database_snapshot,
)


def snapshot(*, factorial=True, side="long", entry=100, exit_price=101,
             quantity=10, opened=None, gross=None, fee=.0002, liquidation=False):
    opened = SAMPLE_QUALITY_CUTOFF_MS + 900_000 if opened is None else opened
    definition = {"strategy_id": "test-cost"}
    if factorial:
        definition["factorial_design_version"] = "atr-risk-full-factorial-v1"
    account = {"strategy_id": "test-cost", "definition": definition,
               "execution": {"fee_rate_per_side": fee}, "rule_hash": "rule-1",
               "execution_hash": "execution-1",
               "state": {"closed_trade_count": 1, "positions": {}}}
    gross = ((1 if side == "long" else -1) * quantity * (exit_price - entry)
             if gross is None else gross)
    fees = quantity * (entry + exit_price) * fee
    trade = {"strategy_id": "test-cost", "trade_id": "trade-1", "pair": "BTC/USDT:USDT",
             "side": side, "quantity": quantity, "entry_price": entry,
             "exit_price": exit_price, "notional": quantity * entry,
             "gross_pnl": gross, "fees": fees, "net_pnl": gross - fees,
             "opened_candle_ms": opened, "closed_candle_ms": opened + 900_000,
             "liquidation": liquidation, "rule_hash": "rule-1"}
    return {"accounts": [account], "trades": [trade]}


class FeeSensitivityTests(unittest.TestCase):
    def test_scenarios_match_the_current_demo_fee_schedule(self):
        self.assertEqual(SCENARIOS["maker"], .0002)
        self.assertEqual(SCENARIOS["taker"], .0004)

    def test_actual_entry_and_exit_notionals_are_charged(self):
        report = analyze_snapshot(snapshot())
        leg = report["accounts"][0]["closed_legs"][0]
        self.assertEqual(leg["entry_notional_usdt"], 1000)
        self.assertEqual(leg["exit_notional_usdt"], 1010)
        self.assertEqual(leg["gross_pnl_usdt"], 10)
        self.assertAlmostEqual(leg["scenarios"]["maker"]["entry_fee_usdt"], .2)
        self.assertAlmostEqual(leg["scenarios"]["maker"]["exit_fee_usdt"], .202)
        self.assertAlmostEqual(leg["scenarios"]["taker"]["net_pnl_usdt"], 9.196)
        self.assertAlmostEqual(leg["scenarios"]["stress"]["net_pnl_usdt"], 7.99)
        self.assertAlmostEqual(leg["breakeven_fee_rate_per_side"], 10 / 2010)

    def test_short_fee_turnover_uses_actual_exit_notional(self):
        result = analyze_snapshot(snapshot(side="short", exit_price=90))
        leg = result["accounts"][0]["closed_legs"][0]
        self.assertEqual(leg["gross_pnl_usdt"], 100)
        self.assertAlmostEqual(leg["scenarios"]["taker"]["fees_usdt"], .76)

    def test_maker_positive_can_flip_at_exact_cost_difference(self):
        report = analyze_snapshot(snapshot(exit_price=100.06))
        leg = report["accounts"][0]["closed_legs"][0]
        maker, taker = leg["scenarios"]["maker"], leg["scenarios"]["taker"]
        self.assertGreater(maker["net_pnl_usdt"], 0)
        self.assertLess(taker["net_pnl_usdt"], 0)
        self.assertTrue(taker["maker_positive_to_nonpositive"])
        self.assertAlmostEqual(maker["net_pnl_usdt"] - taker["net_pnl_usdt"],
                               taker["additional_fee_vs_maker_usdt"])

    def test_liquidation_keeps_recorded_margin_clamp(self):
        result = analyze_snapshot(snapshot(exit_price=80, gross=-10, liquidation=True))
        leg = result["accounts"][0]["closed_legs"][0]
        self.assertEqual(leg["gross_pnl_usdt"], -10)
        self.assertAlmostEqual(leg["scenarios"]["taker"]["net_pnl_usdt"], -10.72)
        self.assertLess(leg["breakeven_fee_rate_per_side"], 0)

    def test_ordinary_gross_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ordinary trade gross"):
            analyze_snapshot(snapshot(exit_price=80, gross=-10))

    def test_before_cutoff_open_after_cutoff_exit_is_ineligible(self):
        report = analyze_snapshot(snapshot(opened=SAMPLE_QUALITY_CUTOFF_MS - 1))
        leg = report["accounts"][0]["closed_legs"][0]
        self.assertEqual(leg["sample_quality"], "stop_definition_defect_period")
        self.assertFalse(leg["eligible_for_factorial_evaluation"])
        self.assertEqual(report["eligible_factorial_closed_legs"]["closed_leg_count"], 0)

    def test_exact_cutoff_factorial_is_eligible_but_legacy_is_not(self):
        for factorial, expected in ((True, 1), (False, 0)):
            with self.subTest(factorial=factorial):
                report = analyze_snapshot(snapshot(factorial=factorial,
                                                    opened=SAMPLE_QUALITY_CUTOFF_MS))
                self.assertEqual(report["eligible_factorial_closed_legs"]["closed_leg_count"], expected)

    def test_populations_are_reported_separately(self):
        factor, legacy = snapshot(), snapshot(factorial=False)
        legacy["accounts"][0]["strategy_id"] = "legacy-cost"
        legacy["accounts"][0]["definition"]["strategy_id"] = "legacy-cost"
        legacy["trades"][0]["strategy_id"] = "legacy-cost"
        report = analyze_snapshot({"accounts": factor["accounts"] + legacy["accounts"],
                                   "trades": factor["trades"] + legacy["trades"]})
        totals = report["population_accounting_totals_not_independent_evidence"]
        self.assertEqual(set(totals), {"factorial", "legacy"})
        self.assertEqual(totals["factorial"]["closed_leg_count"], 1)
        self.assertEqual(totals["legacy"]["closed_leg_count"], 1)

    def test_zero_closed_samples_do_not_imply_zero_expectation(self):
        value = snapshot()
        value["trades"] = []
        value["accounts"][0]["state"]["closed_trade_count"] = 0
        report = analyze_snapshot(value)
        result = report["accounts"][0]
        self.assertEqual(result["evidence_status"], "no_eligible_factorial_samples")
        self.assertIsNone(result["all_complete_closed_legs"]["breakeven_fee_rate_per_side"])
        self.assertIsNone(result["all_complete_closed_legs"]["scenarios"]["maker"]
                          ["mean_net_pnl_per_closed_leg_usdt"])

    def test_truncated_account_detail_is_rejected(self):
        value = snapshot()
        account = value["accounts"][0]
        account.update(closed_trade_count=2, open_positions=[])
        account.pop("state")
        with self.assertRaisesRegex(ValueError, "incomplete closed-trade ledger"):
            analyze_snapshot({"account": account, "trades": value["trades"]})

    def test_complete_account_detail_accepted(self):
        value = snapshot()
        account = value["accounts"][0]
        account.update(closed_trade_count=1, open_positions=[])
        account.pop("state")
        report = analyze_snapshot({"account": account, "trades": value["trades"]})
        self.assertEqual(report["source"]["kind"], "account_detail_export")
        self.assertEqual(report["all_complete_closed_legs"]["closed_leg_count"], 1)

    def test_duplicate_closed_event_rejected(self):
        value = snapshot()
        value["trades"].append(copy.deepcopy(value["trades"][0]))
        value["accounts"][0]["state"]["closed_trade_count"] = 2
        with self.assertRaisesRegex(ValueError, "duplicate closed trade"):
            analyze_snapshot(value)

    def test_missing_paired_leg_fails_closed_count_reconciliation(self):
        value = snapshot()
        value["trades"][0]["group_id"] = "pair-group"
        value["accounts"][0]["state"]["closed_trade_count"] = 2
        with self.assertRaisesRegex(ValueError, "incomplete closed-trade ledger"):
            analyze_snapshot(value)

    def test_closed_leg_in_partly_open_group_is_reported_as_leg(self):
        value = snapshot()
        value["trades"][0]["group_id"] = "pair-group"
        value["accounts"][0]["state"]["positions"] = {
            "ETH/USDT:USDT": {"group_id": "pair-group"}}
        result = analyze_snapshot(value)["accounts"][0]
        self.assertEqual(result["closed_group_ids_with_still_open_legs_count"], 1)
        self.assertEqual(result["excluded_open_position_count"], 1)
        self.assertEqual(result["all_complete_closed_legs"]["closed_leg_count"], 1)

    def test_frozen_fee_and_net_contract_must_reconcile(self):
        for field, change, message in (("fees", .01, "gross/fees/net"),
                                       ("net_pnl", .01, "gross/fees/net"),
                                       ("notional", .01, "entry notional")):
            value = snapshot()
            value["trades"][0][field] += change
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                analyze_snapshot(value)
        value = snapshot()
        value["accounts"][0]["execution"]["fee_rate_per_side"] = .0005
        with self.assertRaisesRegex(ValueError, "frozen fee contract"):
            analyze_snapshot(value)

    def test_rule_identity_mismatch_rejected(self):
        value = snapshot()
        value["trades"][0]["rule_hash"] = "different-rule"
        with self.assertRaisesRegex(ValueError, "frozen account rule hash"):
            analyze_snapshot(value)

    def test_nonfinite_or_incomplete_leg_rejected(self):
        for field, bad in (("quantity", float("nan")), ("entry_price", 0),
                           ("exit_price", None), ("gross_pnl", float("inf")),
                           ("opened_candle_ms", True)):
            value = snapshot()
            value["trades"][0][field] = bad
            with self.subTest(field=field), self.assertRaises(ValueError):
                analyze_snapshot(value)

    def test_database_uses_read_only_snapshot_without_data_store(self):
        conn = MagicMock()
        conn.execute.side_effect = [None, None, None,
            MagicMock(fetchone=lambda: {"snapshot_at": datetime(2026, 10, 11, tzinfo=timezone.utc)}),
            MagicMock(fetchone=lambda: {"last_sequence_id": 123}),
            MagicMock(fetchall=lambda: []), MagicMock(fetchall=lambda: [])]
        with patch("psycopg.connect") as connect, patch.dict("os.environ", {"QUANT_DATABASE_URL": "secret-dsn"}), \
                patch("tools.strategy_cost_sensitivity.load_environment"), \
                patch("data_store.DataStore.initialize") as initialize:
            connect.return_value.__enter__.return_value = conn
            result = read_database_snapshot()
        self.assertIn("READ ONLY", conn.execute.call_args_list[0].args[0])
        self.assertIn("REPEATABLE READ", conn.execute.call_args_list[0].args[0])
        self.assertTrue(connect.call_args.kwargs["autocommit"])
        initialize.assert_not_called()
        trade_sql = conn.execute.call_args_list[-1].args[0]
        self.assertIn("trade_closed", trade_sql)
        self.assertNotIn("LIMIT", trade_sql)
        self.assertNotIn("secret-dsn", json.dumps(result))

    def test_cli_offline_output_has_all_rates_and_no_source_credentials(self):
        value = snapshot()
        value["source"] = {"kind": "export", "dsn": "secret-dsn"}
        with tempfile.TemporaryDirectory() as folder:
            input_path, output_path = Path(folder) / "input.json", Path(folder) / "output.json"
            input_path.write_text(json.dumps(value), encoding="utf-8")
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                result = main(["--input", str(input_path), "--output", str(output_path)])
            report = json.loads(output_path.read_text())
            self.assertEqual(result, 0)
            self.assertEqual(report["scenario_rates_per_side"], SCENARIOS)
            self.assertNotIn("secret-dsn", output.getvalue() + output_path.read_text())
            self.assertEqual(output_path.stat().st_mode & 0o777, 0o600)

    def test_analysis_does_not_mutate_frozen_input(self):
        value = snapshot()
        original = copy.deepcopy(value)
        analyze_snapshot(value)
        self.assertEqual(value, original)


if __name__ == "__main__":
    unittest.main()

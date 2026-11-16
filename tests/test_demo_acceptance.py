from datetime import date, timedelta
from decimal import Decimal
import unittest

from demo_acceptance import evaluate_acceptance_round


def valid_round():
    start = date(2026, 1, 1)
    identity = {"environment": "binance_demo", "account_id": "demo-account",
                "strategy_version_id": "strategy-v1", "run_id": "demo-acceptance-001"}
    days = []
    for offset in range(30):
        day = start + timedelta(days=offset)
        # 完整窗口成本后净利润恰好 1500 USDT，外部资金流另记。
        days.append({
            **identity,
            "date": day.isoformat(),
            "opening_equity_usdt": "1000" if offset == 0 else str(1000 + 50 * offset),
            "closing_equity_usdt": str(1050 + 50 * offset),
            "external_net_flow_usdt": "0",
            "complete": True,
            "costs_complete": True,
            "funding_complete": True,
            "operating_state": "running",
        })
    trips = [{**identity, "trade_id": f"trip-{index:03d}", "opened_at": "2026-01-01T00:00:00+08:00",
              "closed_at": "2026-01-01T01:00:00+08:00", "closed": True, "fill_reconciliation_complete": True,
              "net_pnl_usdt": "15", "costs_included": True,
              "funding_complete": True}
             for index in range(100)]
    return {
        "round_id": "demo-acceptance-001",
        "environment": "binance_demo",
        "account_id": "demo-account",
        "strategy_version_id": "strategy-v1",
        "run_id": "demo-acceptance-001",
        "as_of": "2026-01-31T00:00:00+08:00",
        "window_start_at": "2026-01-01T00:00:00+08:00",
        "window_end_at": "2026-01-31T00:00:00+08:00",
        "opening_account_state": {
            "environment": "binance_demo", "account_id": "demo-account",
            "strategy_version_id": "strategy-v1", "run_id": "demo-acceptance-001",
            "as_of": "2026-01-01T00:00:00+08:00", "net_equity_usdt": "1000",
            "flat": True, "no_open_orders": True, "no_conditional_orders": True,
            "reconciled": True,
        },
        "closing_account_state": {
            "environment": "binance_demo", "account_id": "demo-account",
            "strategy_version_id": "strategy-v1", "run_id": "demo-acceptance-001",
            "as_of": "2026-01-31T00:00:00+08:00", "net_equity_usdt": "2500",
            "flat": True, "no_open_orders": True, "no_conditional_orders": True,
            "reconciled": True,
        },
        "start_date_bjt": start.isoformat(),
        "initial_equity_usdt": "1000",
        "daily_records": days,
        "closed_round_trips": trips,
        "confirmed_hard_risk_failures": [],
        "risk_evidence_complete": True,
    }


class DemoAcceptanceTests(unittest.TestCase):
    def test_passes_only_after_all_frozen_30_day_criteria_are_met(self):
        report = evaluate_acceptance_round(valid_round())
        self.assertEqual(report["verdict"], "passed")
        self.assertEqual(report["criteria"]["net_profit_usdt"], "1500")
        self.assertEqual(report["criteria"]["complete_round_trip_count"], 100)
        self.assertEqual(report["criteria"]["winning_day_count"], 30)
        self.assertEqual(report["criteria"]["ten_day_segment_net_pnl_usdt"],
                         ["500", "500", "500"])

    def test_external_deposits_are_removed_from_net_profit(self):
        evidence = valid_round()
        evidence["daily_records"][4]["closing_equity_usdt"] = "1600"
        evidence["daily_records"][4]["external_net_flow_usdt"] = "350"
        for later in evidence["daily_records"][5:]:
            later["opening_equity_usdt"] = str(Decimal(later["opening_equity_usdt"]) + 350)
            later["closing_equity_usdt"] = str(Decimal(later["closing_equity_usdt"]) + 350)
        evidence["closing_account_state"]["net_equity_usdt"] = "2850"
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["criteria"]["net_profit_usdt"], "1500")
        self.assertEqual(report["verdict"], "passed")

    def test_a_downtime_day_remains_in_the_fixed_window(self):
        evidence = valid_round()
        row = evidence["daily_records"][12]
        row["operating_state"] = "stopped"
        row["closing_equity_usdt"] = row["opening_equity_usdt"]
        for later in evidence["daily_records"][13:]:
            later["opening_equity_usdt"] = str(Decimal(later["opening_equity_usdt"]) - 50)
            later["closing_equity_usdt"] = str(Decimal(later["closing_equity_usdt"]) - 50)
        evidence["closing_account_state"]["net_equity_usdt"] = "2450"
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["criteria"]["calendar_day_count"], 30)
        self.assertEqual(report["criteria"]["net_profit_usdt"], "1450")
        self.assertEqual(report["verdict"], "failed")

    def test_missing_or_incomplete_day_is_insufficient_not_removed_from_window(self):
        evidence = valid_round()
        evidence["daily_records"].pop(8)
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "insufficient_evidence")
        self.assertIn("calendar_window_incomplete", report["reasons"])

        evidence = valid_round()
        evidence["daily_records"][8]["complete"] = False
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "insufficient_evidence")

    def test_requires_18_winning_days_and_three_profitable_fixed_segments(self):
        evidence = valid_round()
        for index in range(13):
            evidence["daily_records"][index]["opening_equity_usdt"] = "1000"
            evidence["daily_records"][index]["closing_equity_usdt"] = "1000"
        for row in evidence["daily_records"][13:]:
            row["opening_equity_usdt"] = str(Decimal(row["opening_equity_usdt"]) - 650)
            row["closing_equity_usdt"] = str(Decimal(row["closing_equity_usdt"]) - 650)
        evidence["closing_account_state"]["net_equity_usdt"] = "1850"
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "failed")
        self.assertLess(report["criteria"]["winning_day_count"], 18)
        self.assertFalse(all(Decimal(value) > 0 for value in report["criteria"]["ten_day_segment_net_pnl_usdt"]))

    def test_requires_100_complete_fee_inclusive_round_trips(self):
        evidence = valid_round()
        evidence["closed_round_trips"] = evidence["closed_round_trips"][:99]
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "failed")
        self.assertEqual(report["criteria"]["complete_round_trip_count"], 99)

        evidence = valid_round()
        evidence["closed_round_trips"][0]["costs_included"] = False
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "insufficient_evidence")

    def test_confirmed_hard_risk_failure_precedes_incomplete_evidence(self):
        evidence = valid_round()
        evidence["daily_records"].pop()
        evidence["confirmed_hard_risk_failures"] = [
            {"code": "daily_loss_limit_exceeded", "confirmed": True},
            "malformed",
        ]
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "failed")
        self.assertEqual(report["reasons"], ["daily_loss_limit_exceeded"])

    def test_1000_usdt_starting_equity_and_30_consecutive_bjt_days_are_fixed(self):
        evidence = valid_round()
        evidence["initial_equity_usdt"] = "999"
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "failed")
        self.assertIn("starting_equity_mismatch", report["reasons"])

        evidence = valid_round()
        evidence["daily_records"][5]["date"] = "2026-02-01"
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "insufficient_evidence")

    def test_daily_records_may_arrive_out_of_order_but_dates_must_be_unique(self):
        evidence = valid_round()
        evidence["daily_records"].reverse()
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "passed")

        evidence = valid_round()
        evidence["daily_records"][0]["date"] = evidence["daily_records"][1]["date"]
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "insufficient_evidence")
        self.assertIn("duplicate_calendar_day", report["reasons"])

    def test_missing_fee_or_funding_completeness_is_insufficient(self):
        evidence = valid_round()
        evidence["daily_records"][3].pop("funding_complete")
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "insufficient_evidence")
        self.assertIn("daily_cost_evidence_incomplete", report["reasons"])

        evidence = valid_round()
        evidence["closed_round_trips"][0].pop("funding_complete")
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "insufficient_evidence")
        self.assertIn("funding_evidence_incomplete", report["reasons"])

        evidence = valid_round()
        evidence["closed_round_trips"][0]["net_pnl_usdt"] = "NaN"
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "insufficient_evidence")
        self.assertIn("round_trip_evidence_incomplete", report["reasons"])

    def test_daily_equity_baselines_must_be_contiguous(self):
        evidence = valid_round()
        evidence["daily_records"][1]["opening_equity_usdt"] = "1000"
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "insufficient_evidence")
        self.assertIn("daily_equity_chain_incomplete", report["reasons"])

    def test_malformed_container_is_insufficient_evidence(self):
        for value in (None, {}, "not-a-list"):
            evidence = valid_round()
            evidence["daily_records"] = value
            report = evaluate_acceptance_round(evidence)
            self.assertEqual(report["verdict"], "insufficient_evidence")
            self.assertIn("acceptance_evidence_invalid", report["reasons"])

        evidence = valid_round()
        evidence["confirmed_hard_risk_failures"] = None
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "insufficient_evidence")
        self.assertIn("acceptance_evidence_invalid", report["reasons"])

        evidence = valid_round()
        evidence["confirmed_hard_risk_failures"] = ["malformed"]
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "insufficient_evidence")
        self.assertIn("acceptance_evidence_invalid", report["reasons"])


    def test_duplicate_trade_identity_cannot_supply_the_hundredth_trade(self):
        evidence = valid_round()
        evidence["closed_round_trips"][-1] = dict(evidence["closed_round_trips"][0])
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "insufficient_evidence")
        self.assertIn("duplicate_round_trip_identity", report["reasons"])
        self.assertEqual(report["criteria"]["complete_round_trip_count"], 99)

    def test_missing_trade_identity_or_out_of_window_trade_is_not_counted(self):
        for changes in ({"trade_id": None}, {"closed_at": "2026-02-01T00:00:00+08:00"},
                        {"opened_at": "2025-12-31T23:59:59+08:00"},
                        {"opened_at": "2026-01-01T00:00:00"}):
            with self.subTest(changes=changes):
                evidence = valid_round()
                evidence["closed_round_trips"][0].update(changes)
                report = evaluate_acceptance_round(evidence)
                self.assertEqual(report["verdict"], "insufficient_evidence")
                self.assertEqual(report["criteria"]["complete_round_trip_count"], 99)

    def test_missing_boundary_fact_prevents_passing_and_known_exposure_fails(self):
        for boundary in ("opening_account_state", "closing_account_state"):
            for key in ("flat", "no_open_orders", "no_conditional_orders", "reconciled"):
                with self.subTest(boundary=boundary, key=key):
                    evidence = valid_round()
                    evidence[boundary].pop(key)
                    report = evaluate_acceptance_round(evidence)
                    self.assertEqual(report["verdict"], "insufficient_evidence")
                    evidence[boundary][key] = False
                    report = evaluate_acceptance_round(evidence)
                    self.assertEqual(report["verdict"], "failed" if key != "reconciled" else "insufficient_evidence")

    def test_boundary_identity_timestamp_and_equity_must_match_round(self):
        for changes in ({"account_id": "another-account"},
                        {"strategy_version_id": "strategy-v2"},
                        {"run_id": "another-run"}, {"environment": "live"},
                        {"as_of": "2026-01-30T23:59:59+08:00"},
                        {"net_equity_usdt": "9999"}):
            with self.subTest(changes=changes):
                evidence = valid_round()
                evidence["closing_account_state"].update(changes)
                report = evaluate_acceptance_round(evidence)
                self.assertEqual(report["verdict"], "insufficient_evidence")

    def test_fixed_window_and_report_time_cannot_be_shortened_or_naive(self):
        for key, value in (("window_end_at", "2026-01-30T23:59:59+08:00"),
                           ("window_start_at", "2026-01-01T01:00:00+08:00"),
                           ("window_end_at", "2026-01-31T00:00:00"),
                           ("as_of", "2026-01-30T23:59:59+08:00")):
            with self.subTest(key=key, value=value):
                evidence = valid_round()
                evidence[key] = value
                self.assertEqual(evaluate_acceptance_round(evidence)["verdict"], "insufficient_evidence")

    def test_all_responses_have_identity_quality_and_status_contract(self):
        evidence = valid_round()
        for candidate in (evidence, {}, None, {**evidence, "confirmed_hard_risk_failures": [
                {"code": "daily_loss_exceeded", "confirmed": True}]}):
            report = evaluate_acceptance_round(candidate)
            self.assertTrue({"environment", "account_id", "strategy_version_id", "run_id",
                "as_of", "data_quality", "cost_completeness", "status", "reason"} <= report.keys())
            self.assertEqual(report["status"], report["verdict"])
        passed = evaluate_acceptance_round(evidence)
        self.assertEqual(passed["account_id"], "demo-account")
        self.assertEqual(passed["data_quality"], "complete")
        self.assertEqual(passed["cost_completeness"], "complete")
        self.assertEqual(passed["reason"], "all_acceptance_criteria_met")
        self.assertEqual(passed["as_of"], "2026-01-30T16:00:00Z")

    def test_missing_round_identity_never_passes(self):
        for key in ("environment", "account_id", "strategy_version_id", "run_id", "as_of"):
            evidence = valid_round()
            evidence.pop(key)
            self.assertEqual(evaluate_acceptance_round(evidence)["verdict"], "insufficient_evidence")

    def test_daily_equity_proves_hard_loss_even_if_the_failure_list_is_empty(self):
        evidence = valid_round()
        evidence["daily_records"][0]["closing_equity_usdt"] = "940"
        evidence["daily_records"][1]["opening_equity_usdt"] = "940"
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "failed")
        self.assertIn("daily_loss_exceeded", report["reasons"])
        evidence["daily_records"][5].pop("external_net_flow_usdt")
        self.assertEqual(evaluate_acceptance_round(evidence)["verdict"], "failed")
        evidence["confirmed_hard_risk_failures"] = None
        self.assertEqual(evaluate_acceptance_round(evidence)["verdict"], "failed")

    def test_daily_equity_proves_peak_drawdown_at_the_boundary(self):
        evidence = valid_round()
        for index, closing in enumerate(("1200", "1150", "1100", "1050")):
            evidence["daily_records"][index]["closing_equity_usdt"] = closing
            if index:
                evidence["daily_records"][index]["opening_equity_usdt"] = (
                    evidence["daily_records"][index - 1]["closing_equity_usdt"])
        evidence["daily_records"][4]["opening_equity_usdt"] = "1050"
        report = evaluate_acceptance_round(evidence)
        self.assertEqual(report["verdict"], "failed")
        self.assertIn("drawdown_limit_reached", report["reasons"])

    def test_boundary_failure_is_confirmed_before_unrelated_bad_daily_evidence(self):
        evidence = valid_round()
        evidence["closing_account_state"]["flat"] = False
        evidence["daily_records"] = None
        self.assertEqual(evaluate_acceptance_round(evidence)["verdict"], "failed")
        for change in ({"account_id": "another-account"},
                       {"as_of": "2026-02-01T00:00:00+08:00"}):
            with self.subTest(change=change):
                evidence = valid_round()
                evidence["closing_account_state"].update(flat=False, **change)
                self.assertEqual(evaluate_acceptance_round(evidence)["verdict"],
                                 "insufficient_evidence")

    def test_risk_coverage_must_be_explicit_even_when_the_failure_list_is_empty(self):
        for key in ("confirmed_hard_risk_failures", "risk_evidence_complete"):
            evidence = valid_round()
            evidence.pop(key)
            report = evaluate_acceptance_round(evidence)
            self.assertEqual(report["verdict"], "insufficient_evidence")
            self.assertNotEqual(report["data_quality"], "complete")

    def test_rows_with_conflicting_frozen_identity_never_pass(self):
        for collection in ("daily_records", "closed_round_trips"):
            for field in ("account_id", "strategy_version_id", "run_id", "environment"):
                evidence = valid_round()
                evidence[collection][0][field] = "another-identity"
                self.assertEqual(evaluate_acceptance_round(evidence)["verdict"],
                                 "insufficient_evidence")
                evidence[collection][0].pop(field)
                self.assertEqual(evaluate_acceptance_round(evidence)["verdict"],
                                 "insufficient_evidence")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.run_r11_forward_paper_t027 import DAY_MS, INITIAL_EQUITY, MARGIN_PER_POSITION_USD, MAX_ACTIVE_POSITIONS
from tools import run_r11_forward_paper_t032 as t032
from tools.run_r11_forward_paper_t032 import run
from tools.run_r11_conditional import live_entry_gate


class T032DualTrackTests(unittest.TestCase):
    def snapshot(self, ts: int = 1_792_886_400_000) -> dict:
        return {
            "timestamp": ts,
            "timestamp_iso": "2026-10-25T00:00:00Z",
            "scheduled_decision_ts": ts,
            "schedule_status": "on_boundary",
            "selected_expiry_ts": ts + 30 * DAY_MS,
            "selected_expiry_iso": "2026-11-24T00:00:00Z",
            "strike": 80_000.0,
            "spot_index": 80_000.0,
            "straddle_premium_bid_usd": 7_000.0,
            "straddle_premium_ask_usd": 7_200.0,
            "straddle_premium_mark_usd": 7_100.0,
            "straddle_bid_ask_width_usd": 200.0,
            "straddle_bid_ask_width_pct_of_bid": 2.857,
            "straddle_mark_iv": 37.0,
            "target_hedge_btc": 0.01,
            "perp": {"mark_price": 80_000.0},
            "paper_premium_bid_usd": 700.0,
            "paper_premium_ask_usd": 720.0,
            "paper_premium_mark_usd": 710.0,
            "paper_unit": {
                "target_quantity_per_leg": 0.1,
                "min_trade_amount_per_leg": 0.1,
                "executable_quantity_per_leg": 0.1,
                "actual_notional_usdt_per_leg": 8_000.0,
                "actual_straddle_notional_usdt": 16_000.0,
                "notional_residual_usdt_per_leg": 0.0,
            },
            "legs": {"call": {"instrument_name": "C"}, "put": {"instrument_name": "P"}},
            "execution_evidence": "forward_paper",
        }

    def run_with_gate(self, out: Path, gate: dict, ts: int = 1_792_886_400_000, *, dry_run_lifecycle: bool = False) -> dict:
        snap = self.snapshot(ts)
        with patch("tools.run_r11_forward_paper_t027.fetch_snapshot", return_value=(snap, [])), \
             patch("tools.run_r11_forward_paper_t027.transform_snapshot", side_effect=lambda x: x), \
             patch("tools.run_r11_conditional.live_entry_gate", return_value=gate):
            return run(out, now_ms=snap["timestamp"], daily=True, record_entry=True, dry_run_lifecycle=dry_run_lifecycle)

    def events(self, path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_low_iv_skip_and_unconditional_baseline_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_with_gate(Path(directory), {
                "entry_dvol_1h": 0.37,
                "trailing_252d_median": 0.42,
                "decision": "skipped_low_iv",
                "history_rows": 300,
            })
            conditional = self.events(Path(directory) / "conditional_ledger_events.jsonl")
            unconditional = self.events(Path(directory) / "unconditional_ledger_events.jsonl")
            self.assertEqual([event["event"] for event in conditional], ["snapshot", "skipped_low_iv"])
            self.assertFalse(any(event["event"] == "fill" for event in conditional))
            self.assertTrue(any(event["event"] == "fill" for event in unconditional))
            self.assertEqual(report["conditional"]["decision"], "skipped_low_iv")
            self.assertEqual(report["conditional"]["observations"], 1)
            self.assertEqual(report["conditional"]["live_observations"], 1)
            self.assertEqual(report["conditional"]["active_positions"], 0)
            self.assertEqual(report["unconditional"]["active_positions"], 1)
            self.assertTrue((Path(directory) / "conditional_equity_state.json").exists())
            self.assertTrue((Path(directory) / "unconditional_equity_state.json").exists())
            self.assertTrue((Path(directory) / "FORWARD_STATUS.md").exists())
            self.assertIn("observations: `1 / 20`", (Path(directory) / "FORWARD_STATUS.md").read_text())

    def test_high_iv_entry_in_both_tracks_and_equity_report(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_with_gate(Path(directory), {
                "entry_dvol_1h": 0.45,
                "trailing_252d_median": 0.42,
                "decision": "enter",
                "history_rows": 300,
            })
            for prefix in ("conditional_", "unconditional_"):
                events = self.events(Path(directory) / f"{prefix}ledger_events.jsonl")
                self.assertEqual([event["event"] for event in events], ["snapshot", "risk_gate", "fill"])
                self.assertEqual(json.loads((Path(directory) / f"{prefix}equity_state.json").read_text())["positions"].__len__(), 1)
            self.assertEqual(report["conditional"]["decision"], "enter")
            self.assertEqual(report["conditional"]["active_positions"], 1)
            self.assertEqual(report["unconditional"]["active_positions"], 1)
            self.assertEqual(report["conditional"]["entry_dvol_1h"], 0.45)
            self.assertEqual(report["conditional"]["trailing_252d_median"], 0.42)

    def test_gap_log_records_missing_utc_dates_once(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            gate = {"entry_dvol_1h": 0.37, "trailing_252d_median": 0.42, "decision": "skipped_low_iv", "history_rows": 300}
            self.run_with_gate(out, gate, 1_792_886_400_000)
            # The next snapshot selects a later expiry. Value the existing
            # position with its own synthetic quote rather than query an exchange.
            with patch("tools.run_r11_forward_paper_t027.fetch_expiry_premiums",
                       return_value=(710.0, 720.0)) as expiry_quotes:
                report = self.run_with_gate(out, gate, 1_792_886_400_000 + 3 * DAY_MS)
            expiry_quotes.assert_called_once_with(
                1_792_886_400_000 + 30 * DAY_MS, 80_000.0,
                1_792_886_400_000 + 3 * DAY_MS, out, 80_000.0,
            )
            gaps = self.events(out / "forward_gaps.jsonl")
            status = (out / "FORWARD_STATUS.md").read_text()
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["missed_dates"], ["2026-10-26", "2026-10-27"])
        self.assertEqual(gaps[0]["gap_days"], 2)
        self.assertEqual(gaps[0]["reason"], "watchdog_down")
        self.assertEqual(report["forward_gap_count"], 1)
        self.assertIn("forward gap count: `1`", status)

    def test_dry_run_does_not_change_live_state_or_status(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            gate = {"entry_dvol_1h": 0.37, "trailing_252d_median": 0.42, "decision": "skipped_low_iv", "history_rows": 300}
            self.run_with_gate(out, gate)
            live_files = [
                "conditional_snapshots.jsonl", "conditional_ledger_events.jsonl", "conditional_equity_daily.jsonl",
                "conditional_equity_state.json", "unconditional_snapshots.jsonl", "unconditional_ledger_events.jsonl",
                "unconditional_equity_daily.jsonl", "unconditional_equity_state.json", "FORWARD_STATUS.md",
            ]
            before = {name: hashlib.sha256((out / name).read_bytes()).hexdigest() for name in live_files}
            self.run_with_gate(out, gate, dry_run_lifecycle=True)
            after = {name: hashlib.sha256((out / name).read_bytes()).hexdigest() for name in live_files}
            self.assertEqual(before, after)
            self.assertTrue((out / "dryrun_FORWARD_STATUS.md").exists())

    def test_same_timestamp_is_idempotent_for_both_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            gate = {"entry_dvol_1h": 0.37, "trailing_252d_median": 0.42, "decision": "skipped_low_iv", "history_rows": 300}
            first = self.run_with_gate(out, gate)
            first_cond = (out / "conditional_ledger_events.jsonl").read_text()
            first_uncond = (out / "unconditional_ledger_events.jsonl").read_text()
            first_cond_eq = (out / "conditional_equity_daily.jsonl").read_text()
            first_uncond_eq = (out / "unconditional_equity_daily.jsonl").read_text()
            second = self.run_with_gate(out, gate)
            self.assertEqual(first["conditional"]["observations"], second["conditional"]["observations"])
            self.assertEqual(first_cond, (out / "conditional_ledger_events.jsonl").read_text())
            self.assertEqual(first_uncond, (out / "unconditional_ledger_events.jsonl").read_text())
            self.assertEqual(first_cond_eq, (out / "conditional_equity_daily.jsonl").read_text())
            self.assertEqual(first_uncond_eq, (out / "unconditional_equity_daily.jsonl").read_text())

    def test_condition_gate_uses_normalized_utc_boundary_timestamp(self):
        boundary = 1_792_886_400_000
        quote_ts = boundary + 2_000
        snap = self.snapshot(quote_ts)
        snap["scheduled_decision_ts"] = boundary
        seen: list[int] = []

        def gate(ts: int, output: Path) -> dict:
            seen.append(ts)
            return {"entry_dvol_1h": 0.37, "trailing_252d_median": 0.42, "decision": "skipped_low_iv", "history_rows": 300}

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch("tools.run_r11_forward_paper_t027.fetch_snapshot", return_value=(snap, [])), \
                 patch("tools.run_r11_forward_paper_t027.transform_snapshot", side_effect=lambda x: x), \
                 patch("tools.run_r11_conditional.live_entry_gate", side_effect=gate):
                run(out, now_ms=quote_ts, daily=True, record_entry=True)
        self.assertEqual(seen, [boundary])

    def test_capacity_is_enforced_independently_per_track(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            ts = 1_792_886_400_000
            positions = {
                f"existing-{index}": {
                    "position_id": f"existing-{index}",
                    "entry_ts": ts - index,
                    "expiry_ts": ts + 30 * DAY_MS,
                    "entry_premium_usd": 700.0,
                    "last_mark_premium_usd": 700.0,
                    "quantity_per_leg": 0.1,
                }
                for index in range(MAX_ACTIVE_POSITIONS)
            }
            (out / "conditional_equity_state.json").write_text(json.dumps({
                "initial_equity": INITIAL_EQUITY,
                "cash_equity": INITIAL_EQUITY,
                "positions": positions,
                "last_equity": INITIAL_EQUITY,
                "last_timestamp": None,
            }))
            report = self.run_with_gate(out, {
                "entry_dvol_1h": 0.45,
                "trailing_252d_median": 0.42,
                "decision": "enter",
                "history_rows": 300,
            }, ts)
            conditional_events = self.events(out / "conditional_ledger_events.jsonl")
            unconditional_events = self.events(out / "unconditional_ledger_events.jsonl")
            blocked = [event for event in conditional_events if event["event"] == "capacity_blocked"]
            self.assertEqual(len(blocked), 1)
            self.assertEqual(blocked[0]["cap"], MAX_ACTIVE_POSITIONS)
            self.assertEqual(blocked[0]["active"], MAX_ACTIVE_POSITIONS)
            self.assertAlmostEqual(blocked[0]["margin_per_position_usd"], MARGIN_PER_POSITION_USD)
            self.assertFalse(any(event["event"] == "fill" for event in conditional_events))
            self.assertTrue(any(event["event"] == "fill" for event in unconditional_events))
            self.assertEqual(report["conditional"]["active_positions"], MAX_ACTIVE_POSITIONS)
            self.assertEqual(report["unconditional"]["active_positions"], 1)
            self.assertEqual(report["conditional"]["position_cap"], MAX_ACTIVE_POSITIONS)
            self.assertEqual(report["unconditional"]["position_cap"], MAX_ACTIVE_POSITIONS)


class T033ForwardMetricsTests(unittest.TestCase):
    def test_metrics_and_rejection_gate_start_at_twenty_live_observations(self):
        daily_rows = [
            {"timestamp": 1_792_886_400_000 + index * DAY_MS, "equity": 50_000 + index,
             "run_mode": "real_boundary"}
            for index in range(20)
        ]
        metrics = getattr(t032, "_performance_metrics", None)
        self.assertIsNotNone(metrics)
        result = metrics(daily_rows)
        self.assertEqual(result["observations"], 20)
        self.assertIsNotNone(result["annualized_return"])
        self.assertIsNotNone(result["sharpe"])
        self.assertIsNotNone(result["max_drawdown"])
        self.assertIsNotNone(result["cvar99"])
        self.assertEqual(result["gate"], "rejected")
        self.assertLess(result["annualized_return"], 0.057)

    def test_metrics_are_not_emitted_before_twenty_live_observations(self):
        rows = [{"timestamp": index * DAY_MS, "equity": 50_000, "run_mode": "real_boundary"} for index in range(19)]
        metrics = getattr(t032, "_performance_metrics", None)
        self.assertIsNotNone(metrics)
        self.assertIsNone(metrics(rows))

    def test_status_includes_live_state_and_twenty_observation_gate_metrics(self):
        rows = [{"timestamp": index * DAY_MS, "equity": 50_000 + index, "run_mode": "real_boundary"} for index in range(20)]
        metrics = t032._performance_metrics(rows)
        report = {
            "run_mode": "real_boundary",
            "forward_gap_count": 1,
            "forward_gaps": [],
            "conditional": {
                "observations": 20, "decision": "enter", "entry_dvol_1h": 0.45,
                "trailing_252d_median": 0.42, "equity": 50_019.0,
                "cumulative_pnl_usd": 19.0, "active_positions": 1,
                "observed_spread_pct": 2.25, "performance": metrics,
            },
            "unconditional": {
                "observations": 20, "decision": "unconditional", "entry_dvol_1h": None,
                "trailing_252d_median": None, "equity": 50_010.0,
                "cumulative_pnl_usd": 10.0, "active_positions": 2,
                "observed_spread_pct": 2.5, "performance": metrics,
            },
        }
        status = t032._render_forward_status(report)
        self.assertIn("observations: `20 / 20`", status)
        self.assertIn("cumulative PnL: `19.0` USDT", status)
        self.assertIn("active positions: `1`", status)
        self.assertIn("annualized:", status)
        self.assertIn("Sharpe:", status)
        self.assertIn("maxDD:", status)
        self.assertIn("CVaR99:", status)
        self.assertIn("gate: `rejected`", status)


class T032NoLookaheadTests(unittest.TestCase):
    def test_252_hour_cache_cannot_satisfy_252_day_window(self):
        now = 1_792_886_400_000
        hourly = [[now - (252 - i) * 3_600_000, 0.0, 0.0, 0.0, 0.40] for i in range(252)]
        payload = json.dumps({"result": {"data": hourly + [[now, 0.0, 0.0, 0.0, 0.39]]}}).encode()
        with tempfile.TemporaryDirectory() as directory:
            gate = live_entry_gate(now, Path(directory), fetcher=lambda _url: payload)
            history = [json.loads(line) for line in (Path(directory) / "conditional_dvol_history.jsonl").read_text().splitlines() if line.strip()]
        self.assertEqual(gate["history_spacing_ms"], DAY_MS)
        self.assertLess(gate["history_span_days"], 252)
        self.assertLess(gate["history_rows"], 252)
        self.assertEqual(gate["decision"], "unknown")
        self.assertTrue(all(row["timestamp"] % DAY_MS == 0 for row in history))

    def test_current_iv_is_excluded_from_trailing_median(self):
        now = 1_792_886_400_000
        prior = [[now - (252 - i) * DAY_MS, 0.0, 0.0, 0.0, 0.40] for i in range(252)]
        payload = json.dumps({"result": {"data": prior + [[now, 0.0, 0.0, 0.0, 0.39]]}}).encode()
        with tempfile.TemporaryDirectory() as directory:
            gate = live_entry_gate(now, Path(directory), fetcher=lambda _url: payload)
        self.assertEqual(gate["entry_dvol_1h"], 0.39)
        self.assertEqual(gate["trailing_252d_median"], 0.40)
        self.assertEqual(gate["decision"], "skipped_low_iv")
        self.assertEqual(gate["history_spacing_ms"], DAY_MS)
        self.assertGreaterEqual(gate["history_span_days"], 252)

    def test_t023_scale_current_iv_is_below_daily_median(self):
        now = 1_788_134_400_000  # 2026-08-31T00:00:00Z
        prior = [[now - (252 - i) * DAY_MS, 0.0, 0.0, 0.0, 0.4173] for i in range(252)]
        payload = json.dumps({"result": {"data": prior + [[now, 0.0, 0.0, 0.0, 0.3678]]}}).encode()
        with tempfile.TemporaryDirectory() as directory:
            gate = live_entry_gate(now, Path(directory), fetcher=lambda _url: payload)
        self.assertAlmostEqual(gate["entry_dvol_1h"], 0.3678)
        self.assertAlmostEqual(gate["trailing_252d_median"], 0.4173)
        self.assertEqual(gate["decision"], "skipped_low_iv")
        self.assertGreaterEqual(gate["history_span_days"], 252)


if __name__ == "__main__":
    unittest.main()

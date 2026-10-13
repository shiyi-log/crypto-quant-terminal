"""Offline tests for the read-only candle integrity audit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.check_candle_integrity import (
    compact_status,
    fetch_rest_klines,
    inspect_dataset,
    inspect_snapshot,
    main,
    reconcile_rest,
    run_watch,
    timeframe_milliseconds,
)


def row(timestamp: int, *, complete: bool = True, flow: float = 0.2,
        flow_base: float = -0.8, flow_q: float | None = None) -> dict:
    extras = {"quote_volume": 100.0, "num_trades": 10,
              "taker_buy_base": 1.0, "taker_buy_quote": 60.0,
              "flow_imbal": flow, "flow_imbal_base": flow_base}
    if flow_q is not None:
        extras["flow_imbal_q"] = flow_q
    if not complete:
        extras.pop("num_trades")
        extras.pop("flow_imbal")
        extras.pop("flow_imbal_base")
    return {"timestamp": timestamp, "volume": 10.0, "extras": extras}


def dataset(rows: list[dict]) -> dict:
    return {
        "exchange": "binance", "market": "futures",
        "pair": "BTC/USDT:USDT", "timeframe": "5m",
        "candle_type": "futures", "rows": rows,
    }


class CandleIntegrityTests(unittest.TestCase):
    def test_timeframe_and_field_coverage_detect_historical_gap(self):
        self.assertEqual(timeframe_milliseconds("5m"), 300_000)
        report = inspect_dataset(dataset([
            row(0, complete=False), row(300_000), row(900_000), row(900_000),
        ]))
        self.assertFalse(report["ok"])
        self.assertIn("field_coverage:num_trades", report["errors"])
        self.assertIn("field_coverage:flow_imbal", report["errors"])
        self.assertIn("timestamp_gaps", report["errors"])
        self.assertIn("duplicate_timestamps", report["errors"])
        self.assertEqual(report["gap_count"], 1)
        self.assertEqual(report["gaps"][0]["missing_bars"], 1)
        self.assertEqual(report["fields"]["quote_volume"]["coverage"], 1.0)

    def test_complete_contiguous_snapshot_passes_without_rest_request(self):
        payload = {"datasets": [dataset([row(0), row(300_000), row(600_000)])]}
        report = inspect_snapshot(payload)
        self.assertTrue(report["ok"])
        self.assertEqual(report["error_count"], 0)
        self.assertEqual(report["datasets"][0]["rest_reconciliation"]["status"], "not_requested")

    def test_contiguous_but_stopped_tail_is_reported_when_freshness_is_requested(self):
        payload = {"datasets": [dataset([row(0), row(300_000), row(600_000)])]}
        report = inspect_snapshot(payload, max_age_bars=1, as_of_ms=1_500_000)
        self.assertFalse(report["ok"])
        self.assertIn("stale_tail", report["datasets"][0]["errors"])
        self.assertEqual(report["datasets"][0]["tail"]["age_bars"], 2.0)
        fresh = inspect_snapshot(payload, max_age_bars=2, as_of_ms=1_500_000)
        self.assertTrue(fresh["ok"])

    def test_rest_reconciliation_reports_missing_and_field_mismatch(self):
        current = dataset([row(0), row(300_000)])
        result = reconcile_rest(current, rest_rows=[
            {"timestamp": 0, "num_trades": 11, "quote_volume": 100.0,
             "taker_buy_base": 1.0, "taker_buy_quote": 60.0},
            {"timestamp": 600_000, "num_trades": 10, "quote_volume": 100.0,
             "taker_buy_base": 1.0, "taker_buy_quote": 60.0},
        ])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["compared_count"], 1)
        self.assertEqual(result["missing_local_count"], 1)
        self.assertEqual(result["mismatch_count"], 1)
        self.assertEqual(result["mismatches"][0]["field"], "num_trades")

    def test_flow_imbal_formula_mismatch_is_reported(self):
        broken = row(0)
        broken["extras"]["flow_imbal"] = 0.9
        report = inspect_dataset(dataset([broken]))
        self.assertFalse(report["ok"])
        self.assertEqual(report["flow_formula"]["status"], "failed")
        self.assertIn("flow_imbal_formula", report["errors"])

    def test_quote_canonical_and_base_formula_pass_separately(self):
        report = inspect_dataset(dataset([row(0)]))
        self.assertTrue(report["ok"])
        self.assertEqual(report["flow_formula"]["canonical"], "flow_imbal")
        self.assertEqual(report["flow_formula"]["formulas"]["flow_imbal"]["status"], "passed")
        self.assertEqual(report["flow_formula"]["formulas"]["flow_imbal_base"]["status"], "passed")

    def test_base_formula_mismatch_is_reported(self):
        broken = row(0, flow_base=0.5)
        report = inspect_dataset(dataset([broken]))
        self.assertFalse(report["ok"])
        self.assertIn("flow_imbal_base_formula", report["errors"])
        self.assertEqual(report["flow_formula"]["formulas"]["flow_imbal_base"]["status"], "failed")

    def test_quote_alias_mismatch_is_reported_when_present(self):
        broken = row(0, flow_q=0.9)
        report = inspect_dataset(dataset([broken]))
        self.assertFalse(report["ok"])
        self.assertIn("flow_imbal_q_formula", report["errors"])

    def test_empty_snapshot_is_not_a_green_result(self):
        report = inspect_snapshot({"datasets": []})
        self.assertFalse(report["ok"])
        self.assertEqual(report["error_count"], 1)
        self.assertEqual(report["errors"], ["empty_snapshot"])

    def test_empty_dataset_is_not_a_green_result(self):
        report = inspect_dataset(dataset([]))
        self.assertFalse(report["ok"])
        self.assertIn("empty_dataset", report["errors"])

    def test_rest_fetcher_parses_binance_kline_shape_without_network(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps([[0, "1", "2", "0.5", "1.5", "10", "100", "99.5", 12,
                                    "5", "49.75", "0"]]).encode()

        seen = {}

        def opener(request, timeout):
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            return Response()

        rows = fetch_rest_klines("BTC/USDT:USDT", "5m", limit=1, now_ms=200, opener=opener)
        self.assertEqual(rows[0]["timestamp"], 0)
        self.assertEqual(rows[0]["num_trades"], 12)
        self.assertEqual(rows[0]["quote_volume"], 99.5)
        self.assertIn("symbol=BTCUSDT", seen["url"])
        self.assertEqual(seen["timeout"], 15)

    def test_rest_fetcher_excludes_currently_forming_kline(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps([
                    [0, "1", "2", "0.5", "1.5", "10", 99, "100", 1, "5", "50", "0"],
                    [300_000, "1", "2", "0.5", "1.5", "10", 399_999, "100", 2, "5", "50", "0"],
                ]).encode()

        rows = fetch_rest_klines(
            "BTC/USDT:USDT", "5m", limit=1, now_ms=300_000,
            opener=lambda _request, timeout: Response(),
        )
        self.assertEqual([item["timestamp"] for item in rows], [0])

    def test_cli_offline_report_returns_nonzero_for_pre_fix_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "snapshot.json"
            output = root / "report.json"
            source.write_text(json.dumps({"datasets": [dataset([
                row(0, complete=False), row(300_000), row(900_000),
            ])]}), encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                result = main(["--input", str(source), "--output", str(output)])
            self.assertEqual(result, 1)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(report["ok"])
            self.assertGreater(report["error_count"], 0)

    def test_zero_denominator_bars_do_not_fail_derived_field_coverage(self):
        zero_trade = {"timestamp": 0, "volume": 0.0,
                      "extras": {"quote_volume": 0.0, "num_trades": 0,
                                 "taker_buy_base": 0.0, "taker_buy_quote": 0.0}}
        report = inspect_dataset(dataset([zero_trade, row(300_000)]))
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["fields"]["flow_imbal"]["denominator"], 1)
        self.assertEqual(report["fields"]["flow_imbal"]["undefined"], 1)
        self.assertEqual(report["fields"]["quote_volume"]["coverage"], 1.0)

    def test_absent_denominator_still_fails_derived_field_coverage(self):
        broken = {"timestamp": 0, "volume": 10.0, "extras": {"num_trades": 1}}
        report = inspect_dataset(dataset([broken, row(300_000)]))
        self.assertIn("field_coverage:flow_imbal", report["errors"])
        self.assertIn("field_coverage:quote_volume", report["errors"])

    def test_formula_check_can_be_skipped_for_coverage_watchdogs(self):
        payload = {"datasets": [dataset([row(0), row(300_000)])]}
        skipped = inspect_snapshot(payload, formula_check=False)
        self.assertTrue(skipped["ok"])
        self.assertEqual(skipped["datasets"][0]["flow_formula"]["status"], "not_requested")
        self.assertFalse(skipped["policy"]["formula_check"])
        checked = inspect_snapshot(payload)
        self.assertEqual(checked["datasets"][0]["flow_formula"]["status"], "passed")

    def test_compact_status_projects_identity_and_errors(self):
        report = inspect_snapshot({"datasets": [dataset([row(0), row(300_000)])]})
        status = compact_status(report, cycles=3, interval=300.0)
        self.assertTrue(status["ok"])
        self.assertEqual(status["cycles"], 3)
        self.assertEqual(status["interval_seconds"], 300.0)
        self.assertEqual(status["datasets"][0]["identity"],
                         ["binance", "futures", "BTC/USDT:USDT", "5m", "futures"])
        self.assertTrue(status["datasets"][0]["tail_passes"] is None)
        self.assertIn("pid", status)

    def test_watch_writes_status_each_cycle_and_survives_an_audit_error(self):
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "status.json"
            seen: list[dict] = []
            calls = []

            def audit():
                if status_path.exists():
                    seen.append(json.loads(status_path.read_text(encoding="utf-8")))
                calls.append(len(calls))
                if len(calls) == 2:
                    raise RuntimeError("database unavailable")
                return inspect_snapshot({"datasets": [dataset([row(0), row(300_000)])]})

            args = argparse.Namespace(interval=0.001, status_document=status_path)
            slept = []
            result = run_watch(args, dsn="", cycles=2, audit=audit,
                               sleep=lambda seconds: slept.append(seconds))
            status = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 2)
        # The first cycle published a healthy status; the second cycle's failure
        # replaced it and stayed explicit instead of aborting the watchdog.
        self.assertTrue(seen[0]["ok"])
        self.assertEqual(seen[0]["cycles"], 1)
        self.assertFalse(status["ok"])
        self.assertEqual(status["last_error"], "RuntimeError")
        self.assertEqual(status["cycles"], 2)
        self.assertEqual(slept, [0.001])

    def test_watch_rejects_non_positive_interval(self):
        args = argparse.Namespace(interval=0.0, status_document=None)
        with self.assertRaises(ValueError):
            run_watch(args, dsn="", cycles=1, audit=lambda: {}, sleep=lambda _s: None)


if __name__ == "__main__":
    unittest.main()

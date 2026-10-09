"""Contract checks for the offline paper-runner evidence artifacts."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

import paper_dryrun as paper  # noqa: E402


def known_loss_fixture():
    index = pd.date_range("2025-01-01", periods=70, freq="h", tz="UTC")
    close = np.full(len(index), 100.0)
    close[25] = 110.0
    close[26:48] = np.linspace(109.0, 80.0, 22)
    close[48:] = 80.0
    open_price = np.r_[100.0, close[:-1]]
    open_price[26] = 110.0
    return {"LOSS/USDT:USDT": pd.DataFrame(
        {"open": open_price, "close": close}, index=index)}


class PaperDryrunContractTests(unittest.TestCase):
    def test_variant_hash_is_canonical_and_tracks_executable_rules(self):
        first = {"variant_id": "base", "hypothesis": "reference",
                 "chan_entry": 20, "chan_exit": 20}
        reordered = {"chan_exit": 20, "chan_entry": 20,
                     "hypothesis": "reference", "variant_id": "base"}
        renamed_hypothesis = {**first, "hypothesis": "different explanatory wording"}
        changed = {**first, "chan_exit": 21}

        a = paper.freeze_variants([first])[0]
        b = paper.freeze_variants([reordered])[0]
        narrative_only = paper.freeze_variants([renamed_hypothesis])[0]
        c = paper.freeze_variants([changed])[0]
        self.assertEqual(a["rule_hash"], b["rule_hash"])
        self.assertEqual(a["rule_hash"], narrative_only["rule_hash"],
                         "rule hashes cover executable rules, not labels or hypothesis prose")
        self.assertNotEqual(a["rule_hash"], c["rule_hash"])

    def test_variant_ids_must_be_unique_and_periods_valid(self):
        variant = {"variant_id": "same", "chan_entry": 20, "chan_exit": 20}
        with self.assertRaisesRegex(ValueError, "duplicate variant_id"):
            paper.freeze_variants([variant, variant])
        with self.assertRaisesRegex(ValueError, "periods"):
            paper.freeze_variants([{"variant_id": "bad", "chan_entry": 1}])
        with self.assertRaisesRegex(ValueError, "non-empty"):
            paper.freeze_variants([{"variant_id": ".."}])

    def test_batch_rejects_non_finite_risk_parameters(self):
        data = known_loss_fixture()
        variants = [{"variant_id": "base"}]
        for kwargs in ({"exposure": float("nan")},
                       {"cost_one": float("nan")},
                       {"wallet": float("inf")}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    paper.run_batch(data, variants, tmp, **kwargs)

    def test_artifact_stems_are_collision_free(self):
        variants = paper.freeze_variants([
            {"variant_id": "a/b"},
            {"variant_id": "a_b"},
        ])
        stems = paper._artifact_stems(variants)
        self.assertEqual(len(stems), 2)
        self.assertEqual(len(set(stems.values())), 2)

    def test_data_fingerprint_is_order_independent_and_content_sensitive(self):
        data = known_loss_fixture()
        equivalent = {"ZZZ/USDT:USDT": data["LOSS/USDT:USDT"],
                      "AAA/USDT:USDT": data["LOSS/USDT:USDT"].copy()}
        same = {"AAA/USDT:USDT": data["LOSS/USDT:USDT"].copy(),
                "ZZZ/USDT:USDT": data["LOSS/USDT:USDT"].copy()}
        self.assertEqual(paper.data_fingerprint(equivalent), paper.data_fingerprint(same))

        changed = {"LOSS/USDT:USDT": data["LOSS/USDT:USDT"].copy()}
        changed["LOSS/USDT:USDT"].iloc[30, changed["LOSS/USDT:USDT"].columns.get_loc("close")] += 0.01
        self.assertNotEqual(paper.data_fingerprint(data), paper.data_fingerprint(changed))

    def test_data_rejects_duplicate_candles_and_missing_price_columns(self):
        frame = known_loss_fixture()["LOSS/USDT:USDT"]
        duplicate = pd.concat([frame, frame.iloc[[0]]])
        with self.assertRaisesRegex(ValueError, "duplicate candle"):
            paper.normalize_data({"LOSS/USDT:USDT": duplicate})
        with self.assertRaisesRegex(ValueError, "open and close"):
            paper.normalize_data({"LOSS/USDT:USDT": frame[["close"]]})
        with self.assertRaisesRegex(ValueError, "timestamps"):
            paper.normalize_data({"LOSS/USDT:USDT": frame.iloc[0:0]})

    def test_partial_signal_candle_readiness_blocks_all_new_entries(self):
        index = pd.date_range("2025-01-01", periods=80, freq="h", tz="UTC")
        close = np.full(len(index), 100.0)
        close[30:] = 110.0
        open_price = np.r_[100.0, close[:-1]]
        candidate = pd.DataFrame({"open": open_price, "close": close}, index=index)
        missing_peer = pd.DataFrame({"open": 100.0, "close": 100.0}, index=index)
        missing_peer.loc[index[30], "close"] = np.nan
        events = []

        paper.engine.run_v2(
            {"AAA/USDT:USDT": candidate, "BBB/USDT:USDT": missing_peer},
            top_n=1, max_open=1, chan_entry=20, chan_exit=20, event_sink=events,
        )

        execution_time = index[31].isoformat()
        decision = next(event for event in events
                        if event["event_type"] == "decision"
                        and event["execution_at_utc"] == execution_time)
        self.assertFalse(decision["data_ready"])
        self.assertEqual(decision["missing"], ["BBB"])
        self.assertEqual(decision["decision_reason"], "data_not_ready")
        self.assertFalse(any(event.get("event_type") == "fill"
                             and event.get("action") == "entry"
                             and event.get("filled_at_utc") == execution_time
                             for event in events))

    def test_negative_control_is_a_real_deterministic_engine_loss(self):
        first = paper.run_negative_control()
        second = paper.run_negative_control()
        self.assertTrue(first["passed"], first)
        self.assertTrue(first["expected_negative"])
        self.assertGreater(first["closed_trade_count"], 0)
        self.assertLess(first["realized_after_fee"], 0)
        self.assertEqual(first, second)

    def test_pairing_reports_unmatched_trades_and_correction(self):
        left = pd.DataFrame([{
            "pair": "AAA", "open_date": pd.Timestamp("2025-01-01"),
            "is_short": False, "profit_abs": 2.0,
        }, {
            "pair": "AAA", "open_date": pd.Timestamp("2025-01-02"),
            "is_short": False, "profit_abs": 1.0,
        }])
        right = pd.DataFrame([{
            "pair": "AAA", "open_date": pd.Timestamp("2025-01-01"),
            "is_short": False, "profit_abs": 1.0,
        }, {
            "pair": "BBB", "open_date": pd.Timestamp("2025-01-03"),
            "is_short": False, "profit_abs": 9.0,
        }])
        variants = paper.freeze_variants([{"variant_id": "left"}, {"variant_id": "right"}])
        row = paper._paired_comparisons({"left": left, "right": right}, variants)[0]
        self.assertEqual(row["matched_count"], 1)
        self.assertEqual(row["left_unmatched_count"], 1)
        self.assertEqual(row["right_unmatched_count"], 1)
        self.assertEqual(row["mean_profit_difference"], 1.0)
        self.assertIsNone(row["p_value"], "one pair cannot support a variance estimate")
        self.assertEqual(row["bonferroni_comparisons"], 1)

    def test_batch_manifest_events_and_results_are_idempotent(self):
        data = known_loss_fixture()
        variants = [
            {"variant_id": "base", "hypothesis": "frozen reference",
             "chan_entry": 20, "chan_exit": 20},
            {"variant_id": "entry-21", "hypothesis": "one-bar sensitivity",
             "chan_entry": 21, "chan_exit": 20},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            first = paper.run_batch(data, variants, tmp, top_n=1, max_open=1,
                                    exposure=0.30, wallet=1_000.0)
            out = Path(first["output_dir"])
            before = {path.name: path.read_bytes() for path in out.iterdir()}
            second = paper.run_batch(data, variants, tmp, top_n=1, max_open=1,
                                     exposure=0.30, wallet=1_000.0)
            after = {path.name: path.read_bytes() for path in out.iterdir()}
            self.assertEqual(first["manifest"]["run_id"], second["manifest"]["run_id"])
            self.assertEqual(before, after, "same immutable inputs must reproduce byte-identical artifacts")

            manifest = json.loads((out / "manifest.json").read_text())
            self.assertEqual(manifest["engine"], "event_backtest.run_v2")
            self.assertEqual(manifest["data_fingerprint"], paper.data_fingerprint(data))
            self.assertEqual(manifest["cost_completeness"]["slippage"], "unknown")
            self.assertEqual(manifest["cost_completeness"]["funding"], "unknown")
            base_variant = next(item for item in manifest["variants"]
                                if item["variant_id"] == "base")
            self.assertEqual(len(base_variant["rule_hash"]), 64)
            self.assertTrue((out / "comparisons.json").exists())
            self.assertIn("common_window_utc", manifest)

            events = [json.loads(line) for line in
                      (out / "base.events.jsonl").read_text().splitlines()]
            self.assertGreater(len(events), 0)
            self.assertEqual(len({event["event_id"] for event in events}), len(events))
            decisions = [event for event in events if event["event_type"] == "decision"]
            fills = [event for event in events if event["event_type"] == "fill"]
            self.assertEqual(len(decisions), len(data["LOSS/USDT:USDT"]))
            self.assertTrue(all("candidates" in event and "held_before" in event
                                for event in decisions))
            self.assertTrue(all("actual_fill" not in candidate
                                for event in decisions for candidate in event["candidates"]))
            self.assertTrue(all(event["rule_hash"] == base_variant["rule_hash"]
                                for event in events))
            entry_fills = [event for event in fills if event["action"] == "entry"]
            exit_fills = [event for event in fills if event["action"] == "exit"]
            self.assertTrue(entry_fills)
            self.assertTrue(exit_fills)
            positions = {event["execution_at_utc"]: event["ordinal"] for event in decisions}
            for fill in fills:
                decision_ordinal = positions[fill["filled_at_utc"]]
                self.assertLess(decision_ordinal, fill["ordinal"],
                                "the decision record must precede its same-open fill in ledger order")

            summary = json.loads((out / "summary.json").read_text())
            control = summary["negative_control"]
            self.assertTrue(control["passed"], control)
            self.assertLess(control["realized_after_fee"], 0)
            row = summary["variants"]["base"]
            self.assertIsNone(row["net_profit"], "unknown funding/slippage forbids a net-profit claim")
            self.assertEqual(len(summary["comparisons"]), 1)

    def test_conflicting_immutable_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = paper.run_batch(known_loss_fixture(),
                                     [{"variant_id": "base", "chan_entry": 20,
                                       "chan_exit": 20}], tmp, top_n=1, max_open=1,
                                     wallet=1_000.0)
            manifest_path = Path(result["output_dir"]) / "manifest.json"
            manifest_path.write_text("tampered\n")
            with self.assertRaisesRegex(ValueError, "immutable artifact conflict"):
                paper.run_batch(known_loss_fixture(),
                                [{"variant_id": "base", "chan_entry": 20,
                                  "chan_exit": 20}], tmp, top_n=1, max_open=1,
                                wallet=1_000.0)


if __name__ == "__main__":
    unittest.main()

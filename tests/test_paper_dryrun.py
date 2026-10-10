"""Contract checks for the offline paper-runner evidence artifacts."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
    def test_minimal_variants_receive_explicit_v2_rules_and_refreeze(self):
        minimal = paper.freeze_variants([{"variant_id": "base"}])[0]
        explicit = paper.freeze_variants([{"variant_id": "base", "chan_entry": 20, "chan_exit": 20,
                                          "range_filter": {"kind": "none"},
                                          "take_profit": {"kind": "none"},
                                          "stop_loss": {"kind": "none"},
                                          "execution": dict(paper.EXECUTION_DEFAULTS)}])[0]
        self.assertEqual(paper.SCHEMA_VERSION, "paper-dryrun-v2")
        self.assertEqual(minimal, explicit)
        self.assertEqual(paper.freeze_variants([minimal]), [minimal])
        self.assertEqual(minimal["range_filter"], {"kind": "none", "window": 20, "threshold": 0.0,
                                                  "op": "gt", "warmup_bars": 20})
        self.assertEqual(minimal["take_profit"], {"kind": "none", "value": 0.0, "atr_window": None})
        self.assertEqual(minimal["stop_loss"], minimal["take_profit"])
        self.assertEqual(minimal["execution"], paper.EXECUTION_DEFAULTS)
        legacy_hash = paper.sha256(paper.canonical_json({"chan_entry": 20, "chan_exit": 20}))
        with self.assertRaisesRegex(ValueError, "rule_hash"):
            paper.freeze_variants([{**minimal, "rule_hash": legacy_hash}])
        edited = {**minimal, "take_profit": {"kind": "fixed_pct", "value": 0.5}}
        with self.assertRaisesRegex(ValueError, "rule_hash"):
            paper.freeze_variants([edited])

    def test_nested_executable_fields_and_execution_policy_are_hashed(self):
        rule = {"variant_id": "base", "range_filter": {"kind": "channel_width_pct", "window": 20,
                "threshold": 0.5, "op": "gt", "warmup_bars": 20},
                "take_profit": {"kind": "atr_multiple", "value": 2, "atr_window": 14},
                "stop_loss": {"kind": "fixed_pct", "value": 0.5}}
        base = paper.freeze_variants([rule])[0]
        for field, change in (("range_filter", {"kind": "realized_vol_pct"}),
                              ("range_filter", {"window": 21}),
                              ("range_filter", {"threshold": 0.6}),
                              ("range_filter", {"op": "lt"}),
                              ("range_filter", {"warmup_bars": 21}),
                              ("take_profit", {"value": 3}),
                              ("take_profit", {"atr_window": 15}),
                              ("stop_loss", {"value": 0.6})):
            with self.subTest(field=field, change=change):
                changed = {**rule, field: {**rule[field], **change}}
                self.assertNotEqual(base["rule_hash"], paper.freeze_variants([changed])[0]["rule_hash"])
        equivalent = {**rule, "variant_id": "renamed", "hypothesis": "new wording",
                      "take_profit": {"kind": "atr_multiple", "value": 2.0, "atr_window": 14}}
        self.assertEqual(base["rule_hash"], paper.freeze_variants([equivalent])[0]["rule_hash"])
        expected_payload = {key: value for key, value in base.items()
                            if key not in {"variant_id", "hypothesis", "rule_hash"}}
        expected_payload.update(schema_version="paper-dryrun-v2", percentage_unit="percentage_points")
        self.assertEqual(base["rule_hash"], paper.sha256(paper.canonical_json(expected_payload)))
        without_execution = {key: value for key, value in expected_payload.items() if key != "execution"}
        self.assertNotEqual(base["rule_hash"], paper.sha256(paper.canonical_json(without_execution)))
        self.assertEqual(base["stop_loss"]["value"], 0.5, "0.5 freezes as 0.5 percentage points")

    def test_schema_rejects_unknown_nested_fields_and_policy_values(self):
        for field in ("range_filter", "take_profit", "stop_loss", "execution"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "unsupported"):
                paper.freeze_variants([{"variant_id": "bad", field: {"typo": 1}}])
        with self.assertRaisesRegex(ValueError, "unsupported"):
            paper.freeze_variants([{"variant_id": "bad", "unknown": 1}])
        for field in ("range_filter", "take_profit", "stop_loss", "execution"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "object"):
                paper.freeze_variants([{"variant_id": "bad", field: None}])
        for field in paper.EXECUTION_DEFAULTS:
            with self.subTest(policy=field), self.assertRaisesRegex(ValueError, "currently supports only"):
                paper.freeze_variants([{"variant_id": "bad", "execution": {field: "unfrozen-policy"}}])

    def test_schema_rejects_bool_fractional_and_string_integer_fields(self):
        integer_paths = [("chan_entry", None), ("chan_exit", None),
                         ("range_filter", "window"), ("range_filter", "warmup_bars"),
                         ("take_profit", "atr_window"), ("stop_loss", "atr_window")]
        for field, nested in integer_paths:
            for value in (True, False, 20.9, 20.0, "20", float("nan"), float("inf")):
                item = {"variant_id": "bad"}
                if nested is None:
                    item[field] = value
                elif field == "range_filter":
                    item[field] = {nested: value}
                else:
                    item[field] = {"kind": "atr_multiple", "value": 1.0, nested: value}
                with self.subTest(field=field, nested=nested, value=value), self.assertRaises(ValueError):
                    paper.freeze_variants([item])

    def test_active_rules_require_finite_positive_values_and_valid_kind(self):
        for field, kind, scalar in (("range_filter", "adx", "threshold"),
                                    ("take_profit", "fixed_pct", "value"),
                                    ("stop_loss", "fixed_pct", "value")):
            for value in (True, False, "0.5", 0, -0.5, float("nan"), float("inf"), -float("inf")):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    paper.freeze_variants([{"variant_id": "bad", field: {"kind": kind, scalar: value}}])
            with self.subTest(field=field), self.assertRaises(ValueError):
                paper.freeze_variants([{"variant_id": "bad", field: {"kind": kind}}])
        invalid = [
            {"range_filter": {"kind": "unknown"}},
            {"range_filter": {"kind": "adx", "threshold": 25, "op": "gte"}},
            {"range_filter": {"kind": "none", "threshold": 1}},
            {"take_profit": {"kind": "fixed_pct", "value": 1, "atr_window": 14}},
            {"take_profit": {"kind": "atr_multiple", "value": 2}},
            {"stop_loss": {"kind": "trailing_pct", "value": 1}},
            {"stop_loss": {"kind": "none", "value": 1}},
            {"variant_id": 20}, {"variant_id": "  "}, {"hypothesis": 20},
        ]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                paper.freeze_variants([{**{"variant_id": "bad"}, **raw}])

    def test_default_variants_are_thirteen_fixed_prospective_rules(self):
        variants = paper.freeze_variants(paper.DEFAULT_VARIANTS)
        self.assertEqual(len(variants), 13)
        self.assertTrue(all(item["chan_entry"] == item["chan_exit"] == 20 for item in variants))
        kinds = {item["range_filter"]["kind"] for item in variants}
        self.assertEqual(kinds, {"none", "adx", "channel_width_pct", "realized_vol_pct"})
        self.assertTrue(any(item["take_profit"]["kind"] == "trailing_pct" for item in variants))
        self.assertTrue(any(item["take_profit"]["kind"] == "atr_multiple" for item in variants))
        self.assertEqual(len({item["rule_hash"] for item in variants}), 13)

    def test_data_fingerprint_and_normalization_preserve_ohlc(self):
        legacy = known_loss_fixture()
        rich = {symbol: frame.assign(high=np.maximum(frame["open"], frame["close"]) + 1,
                                     low=np.minimum(frame["open"], frame["close"]) - 1)
                for symbol, frame in legacy.items()}
        normalized = paper.normalize_data(rich)
        self.assertEqual(list(normalized["LOSS/USDT:USDT"].columns), ["open", "high", "low", "close"])
        self.assertNotEqual(paper.data_fingerprint(legacy), paper.data_fingerprint(rich))
        for column in ("high", "low"):
            changed = {symbol: frame.copy() for symbol, frame in rich.items()}
            changed["LOSS/USDT:USDT"].loc[changed["LOSS/USDT:USDT"].index[30], column] += 0.01
            with self.subTest(column=column):
                self.assertNotEqual(paper.data_fingerprint(rich), paper.data_fingerprint(changed))
        reordered = {symbol: frame[["close", "low", "open", "high"]] for symbol, frame in rich.items()}
        self.assertEqual(paper.data_fingerprint(rich), paper.data_fingerprint(reordered))

    def test_feather_loader_retains_ohlc(self):
        frame = known_loss_fixture()["LOSS/USDT:USDT"].copy()
        frame["high"] = frame[["open", "close"]].max(axis=1) + 1
        frame["low"] = frame[["open", "close"]].min(axis=1) - 1
        feather = frame.rename_axis("date").reset_index()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(paper.pd, "read_feather", return_value=feather):
            (Path(tmp) / "LOSS_USDT_USDT-1h-futures.feather").touch()
            loaded = paper.load_feather_1h(tmp, ["LOSS"])
        self.assertEqual(list(loaded["LOSS"].columns), ["open", "high", "low", "close"])
        self.assertEqual(loaded["LOSS"]["high"].iloc[25], feather["high"].iloc[25])

    def test_engine_fingerprint_includes_executable_helper_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine_path = Path(tmp) / "event_backtest.py"
            rules_path = Path(tmp) / "paper_rules.py"
            engine_path.write_text("engine-v1\n")
            rules_path.write_text("rules-v1\n")
            with mock.patch.object(paper.engine, "__file__", str(engine_path)), mock.patch.object(paper, "HERE", Path(tmp)):
                before = paper.engine_fingerprint()
                rules_path.write_text("rules-v2\n")
                self.assertNotEqual(before, paper.engine_fingerprint())

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

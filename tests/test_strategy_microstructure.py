"""Causal input, entry-only gating and paired-population contracts."""

import copy
import json
import unittest

import strategy_signals as core
from strategy_factorial import ALLOWED_RESEARCH_PAIRS, BTC
from strategy_microstructure import (
    BAR_MS,
    EXPERIMENT_VERSION,
    FEATURE_METHOD_VERSION,
    build_microstructure_specs,
    evaluate_microstructure,
    microstructure_rule_hash,
    normalize_microstructure_spec,
    parse_microstructure_identity,
)


def spec_for(group="filter", risk=50, k=1.5):
    return next(spec for spec in build_microstructure_specs()
                if spec["microstructure_group"] == group
                and spec["max_loss_per_trade_usdt"] == risk
                and spec["params"]["risk_stop_atr_mult"] == k)


def candles(close=102, count=116):
    rows = [{"timestamp": i * BAR_MS, "open": 100.0,
             "close": 100.0, "high": 101.0, "low": 99.0,
             "volume": 100.0, "extras": {"closed": True}}
            for i in range(count)]
    rows[-1].update(close=close, high=max(101, close), low=min(99, close))
    return rows


def observation(rows, lag=30_000):
    return rows[-1]["timestamp"] + BAR_MS + lag


def features(rows, obi=0.2, spread=0.0001):
    stamp = rows[-1]["timestamp"]
    return {pair: {"feature_key": f"orderbook/features/{pair}/{stamp}",
                   "source_hash": "immutable-snapshot-sequence-hash",
                   "method_version": FEATURE_METHOD_VERSION,
                   "exchange": "binance", "market": "futures",
                   "pair": pair, "timeframe": "1h", "candle_ms": stamp,
                   "cutoff_ms": stamp + BAR_MS, "received_cutoff_ms": stamp + BAR_MS,
                   "observed_at_ms": stamp + BAR_MS + 15_000,
                   "published_at_ms": stamp + BAR_MS + 16_000,
                   "forward_eligible": True,
                   "quality": {"eligible": True, "status": "ready",
                               "coverage_ratio": 0.99, "minute_bins": 60,
                               "largest_gap_ms": 1000, "age_at_close_ms": 100,
                               "valid_snapshots": 3600},
                   "features": {"obi_5_time_mean": obi,
                                "relative_spread_time_mean": spread,
                                "lagged_obi_mid_directional_consistency": 0.54}}
            for pair in ALLOWED_RESEARCH_PAIRS}


def evaluate(spec, rows, records=None, positions=None, lag=30_000):
    return evaluate_microstructure(
        spec, {pair: rows for pair in ALLOWED_RESEARCH_PAIRS},
        positions=positions, as_of_utc=observation(rows, lag), book_features=records)


class MicrostructureIdentityTests(unittest.TestCase):
    def test_all_18_definitions_are_distinct_and_idempotent(self):
        definitions = build_microstructure_specs()
        self.assertEqual(len(definitions), 18)
        self.assertEqual(len({spec["strategy_id"] for spec in definitions}), 18)
        self.assertEqual(len({spec["rule_hash"] for spec in definitions}), 18)
        for spec in definitions:
            with self.subTest(identity=spec["strategy_id"]):
                self.assertLessEqual(len(spec["strategy_id"]), 80)
                self.assertEqual(normalize_microstructure_spec(spec), spec)
                self.assertEqual(microstructure_rule_hash(spec), spec["rule_hash"])
                self.assertEqual(spec["sample_population"], EXPERIMENT_VERSION)
                self.assertNotIn("factorial_design_version", spec)

    def test_each_cell_has_identical_canonical_rules_in_both_groups(self):
        for risk in (50, 200, 1000):
            for k in (1.0, 1.5, 2.5):
                control, filtered = spec_for("control", risk, k), spec_for("filter", risk, k)
                self.assertEqual(control["signal_rule_hash"], filtered["signal_rule_hash"])
                for key in ("params", "timeframe", "execution_universe", "position_budget",
                            "fee_rate_per_side", "initial_capital", "max_loss_per_trade_usdt"):
                    self.assertEqual(control[key], filtered[key])
        baseline = build_microstructure_specs()
        baseline[0]["params"]["H"] = 1
        self.assertEqual(baseline[1]["params"]["H"], 72)
        self.assertEqual(build_microstructure_specs()[0]["params"]["H"], 72)

    def test_parser_rejects_unrecognized_groups_widths_risks_and_suffixes(self):
        valid = spec_for()["strategy_id"]
        for value in (None, 1, valid + ".", valid.replace("filter", "control_extra"),
                      valid.replace("R50", "R51"), valid.replace("k1.5", "k1.50"),
                      valid.replace("k1.5", "k1"), valid.replace("s37", "s38"),
                      valid.replace("maker", "taker")):
            with self.subTest(value=value):
                self.assertIsNone(parse_microstructure_identity(value))

    def test_frozen_hash_or_contract_prevents_silent_rule_changes(self):
        original = spec_for()
        mutations = {"microstructure_group": "control", "feature_method_version": "v2",
                     "sample_population": "legacy", "book_gate": {"obi_depth": 1},
                     "feature_quality_policy": {}, "risk_sizing_mode": "fixed_pct",
                     "max_loss_per_trade_usdt": 200, "timeframe": "15m",
                     "data_universe": [BTC], "execution_universe": [BTC],
                     "fee_rate_per_side": 0.0005, "execution_enabled": False,
                     "forward_hypothesis": False,
                     "params": {**original["params"], "H": 1},
                     "params_frozen": {**original["params"], "risk_stop_atr_mult": 2.5},
                     "signal_rule_hash": "inconsistent", "fill_model": "market",
                     "rule_hash": "invalid"}
        for key, value in mutations.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                normalize_microstructure_spec({**original, key: value})


class MicrostructureSignalTests(unittest.TestCase):
    def test_matching_book_passes_symmetric_entry_on_every_risk_cell(self):
        for risk in (50, 200, 1000):
            for k in (1.0, 1.5, 2.5):
                for side in (-1, 1):
                    with self.subTest(risk=risk, k=k, side=side):
                        rows = candles(100 + side * 2)
                        records = features(rows, obi=side * 0.1, spread=0.0002)
                        control = evaluate(spec_for("control", risk, k), rows, records)
                        filtered = evaluate(spec_for("filter", risk, k), rows, records)
                        self.assertTrue(filtered["ready"])
                        self.assertNotEqual(control["rule_hash"], filtered["rule_hash"])
                        for pair in ALLOWED_RESEARCH_PAIRS:
                            cs, fs = control["signals"][pair], filtered["signals"][pair]
                            self.assertEqual(cs["target_side"], side)
                            self.assertEqual(fs["target_side"], side)
                            self.assertEqual(cs["evidence"]["atr"], fs["evidence"]["atr"])
                            audit = fs["evidence"]["orderbook"]
                            self.assertTrue(audit["gate_pass"])
                            self.assertTrue(audit["paired_subsample_eligible"])

    def test_filter_rejects_wrong_direction_weak_obi_or_wide_spread_only_for_entry(self):
        rows = candles()
        for obi, spread in ((-0.2, 0.0001), (0.09999, 0.0001), (0.2, 0.00020001)):
            with self.subTest(obi=obi, spread=spread):
                records = features(rows, obi, spread)
                control = evaluate(spec_for("control"), rows, records)
                filtered = evaluate(spec_for(), rows, records)
                self.assertEqual(control["signals"][BTC]["target_side"], 1)
                self.assertIsNone(filtered["signals"][BTC]["target_side"])
                self.assertEqual(filtered["signals"][BTC]["reason"], "orderbook_confirmation_not_met")
                self.assertTrue(filtered["signals"][BTC]["evidence"]["orderbook"]["paired_subsample_eligible"])

    def test_lag_directional_consistency_is_diagnostic_only(self):
        rows = candles()
        records = features(rows)
        records[BTC]["features"]["lagged_obi_mid_directional_consistency"] = 0
        self.assertEqual(evaluate(spec_for(), rows, records)["signals"][BTC]["target_side"], 1)
        records[BTC]["features"]["lagged_obi_mid_directional_consistency"] = 1
        self.assertEqual(evaluate(spec_for(), rows, records)["signals"][BTC]["target_side"], 1)

    def test_just_published_after_observation_is_retried_without_using_future_input(self):
        rows = candles()
        records = features(rows)
        records[BTC]["published_at_ms"] = observation(rows) + 1
        waiting = evaluate(spec_for(), rows, records)
        self.assertFalse(waiting["ready"])
        self.assertTrue(waiting["feature_waiting"])
        self.assertIsNone(waiting["signals"][BTC]["target_side"])
        self.assertFalse(waiting["signals"][BTC]["evidence"]["orderbook"]["feature_validity"])
        retried = evaluate(spec_for(), rows, records, lag=30_001)
        self.assertTrue(retried["ready"])
        self.assertEqual(retried["signals"][BTC]["target_side"], 1)

    def test_missing_feature_waits_then_fails_closed_without_harming_control(self):
        rows = candles()
        for group in ("control", "filter"):
            waiting = evaluate(spec_for(group), rows, lag=15_000)
            self.assertFalse(waiting["ready"])
            self.assertTrue(waiting["feature_waiting"])
            self.assertEqual(waiting["warmup"]["reason"], "waiting_published_orderbook_feature")
        filtered = evaluate(spec_for(), rows, lag=90_000)
        self.assertTrue(filtered["ready"])
        self.assertFalse(filtered["feature_waiting"])
        self.assertIsNone(filtered["signals"][BTC]["target_side"])
        control = evaluate(spec_for("control"), rows, lag=90_000)
        self.assertTrue(control["ready"])
        self.assertEqual(control["signals"][BTC]["target_side"], 1)
        audit = control["signals"][BTC]["evidence"]["orderbook"]
        self.assertFalse(audit["paired_subsample_eligible"])
        self.assertFalse(audit["feature_validity"])

    def test_control_and_filter_publish_waits_end_on_the_same_snapshot(self):
        rows = candles()
        records = features(rows)
        for group in ("control", "filter"):
            pending = evaluate(spec_for(group), rows, records, lag=15_000)
            self.assertFalse(pending["ready"])
            self.assertTrue(pending["feature_waiting"])
            accepted = evaluate(spec_for(group), rows, records, lag=16_000)
            self.assertTrue(accepted["ready"])
            self.assertFalse(accepted["feature_waiting"])
            self.assertEqual(accepted["signals"][BTC]["target_side"], 1)
            self.assertTrue(accepted["signals"][BTC]["evidence"]["orderbook"]["paired_subsample_eligible"])

    def test_book_availability_cannot_suppress_any_frozen_atr_or_holding_exit(self):
        for risk in (50, 200, 1000):
            for k in (1.0, 1.5, 2.5):
                for side in (-1, 1):
                    rows = candles(100 - side * k)
                    pos = {BTC: {"side": side, "entry_price": 100,
                                 "opened_candle_ms": rows[-1]["timestamp"], "entry_atr": 1}}
                    for records in (None, features(rows, obi=-side * 0.2)):
                        with self.subTest(risk=risk, k=k, side=side, records=bool(records)):
                            result = evaluate(spec_for("filter", risk, k), rows, records, pos)
                            self.assertTrue(result["ready"])
                            self.assertFalse(result["feature_waiting"])
                            signal = result["signals"][BTC]
                            self.assertEqual(signal["target_side"], 0)
                            self.assertEqual(signal["reason"], "stop_loss_close_confirmed")
                            self.assertEqual(signal["evidence"]["stop_level"], 100 - side * k)
        rows = candles()
        pos = {BTC: {"side": 1, "entry_price": 102, "opened_candle_ms": 0, "entry_atr": 3}}
        result = evaluate(spec_for(), rows, positions=pos)
        self.assertEqual(result["signals"][BTC]["reason"], "holding_period_elapsed")
        self.assertEqual(result["signals"][BTC]["target_side"], 0)

    def test_ineligible_wrong_bar_future_and_expired_features_refuse_new_entries(self):
        rows = candles()
        records = features(rows)
        changes = [{"forward_eligible": False}, {"method_version": "unknown"},
                   {"exchange": "other"}, {"market": "spot"},
                   {"pair": "SOL/USDT:USDT"}, {"timeframe": "15m"},
                   {"candle_ms": records[BTC]["candle_ms"] - BAR_MS},
                   {"cutoff_ms": records[BTC]["cutoff_ms"] + 1},
                   {"received_cutoff_ms": records[BTC]["cutoff_ms"] + 1},
                   {"feature_key": ""}, {"source_hash": ""},
                   {"published_at_ms": observation(rows) + 1},
                   {"observed_at_ms": records[BTC]["cutoff_ms"] - 1}]
        for change in changes:
            invalid = copy.deepcopy(records)
            invalid[BTC].update(change)
            with self.subTest(change=change):
                result = evaluate(spec_for(), rows, invalid)
                self.assertIsNone(result["signals"][BTC]["target_side"])
                self.assertFalse(result["signals"][BTC]["evidence"]["orderbook"]["paired_subsample_eligible"])
                self.assertEqual(result["signals"][ALLOWED_RESEARCH_PAIRS[1]]["target_side"], 1)
        expired = copy.deepcopy(records)
        expired[BTC]["published_at_ms"] = records[BTC]["cutoff_ms"] + 90_001
        result = evaluate(spec_for(), rows, expired, lag=100_000)
        self.assertIsNone(result["signals"][BTC]["target_side"])
        self.assertEqual(result["signals"][BTC]["evidence"]["orderbook"]["gate_reason"],
                         "orderbook_feature_expired")

    def test_quality_flag_alone_does_not_override_missing_or_failed_quality_metrics(self):
        rows = candles()
        for key, value in (("coverage_ratio", 0.949), ("minute_bins", 54),
                           ("minute_bins", 61), ("largest_gap_ms", 30_001),
                           ("age_at_close_ms", 3001), ("valid_snapshots", 99),
                           ("valid_snapshots", True), ("status", "unknown"),
                           ("eligible", 1), ("coverage_ratio", None)):
            invalid = features(rows)
            invalid[BTC]["quality"][key] = value
            with self.subTest(key=key, value=value):
                result = evaluate(spec_for(), rows, invalid)
                self.assertIsNone(result["signals"][BTC]["target_side"])
                self.assertEqual(result["signals"][BTC]["reason"], "orderbook_feature_quality_unavailable")

    def test_nonfinite_feature_audit_is_json_safe_and_observation_clock_required(self):
        rows = candles()
        for key in ("obi_5_time_mean", "relative_spread_time_mean"):
            records = features(rows)
            records[BTC]["features"][key] = float("nan")
            result = evaluate(spec_for(), rows, records)
            self.assertIsNone(result["signals"][BTC]["target_side"])
            json.dumps(result, allow_nan=False)
        result = evaluate_microstructure(spec_for(), {pair: rows for pair in ALLOWED_RESEARCH_PAIRS},
                                         book_features=features(rows))
        self.assertEqual(result["signals"][BTC]["reason"], "orderbook_observation_time_unavailable")

    def test_control_is_exact_canonical_target_reason_and_risk_evidence(self):
        rows = candles()
        spec = spec_for("control")
        canonical = {**spec, "strategy_id": spec["canonical_strategy_id"]}
        canonical.pop("rule_hash")
        # _inner_spec freezes the common baseline metadata; name has no
        # signal effect, so this independently invokes the public core API.
        canonical["name"] = "independent-canonical-reference"
        expected = core.evaluate(canonical, {pair: rows for pair in ALLOWED_RESEARCH_PAIRS},
                                 as_of_utc=observation(rows))
        control = evaluate(spec, rows)
        for pair in ALLOWED_RESEARCH_PAIRS:
            reference, actual = expected["signals"][pair], control["signals"][pair]
            self.assertEqual(actual["target_side"], reference["target_side"])
            self.assertEqual(actual["reason"], reference["reason"])
            self.assertEqual({key: value for key, value in actual["evidence"].items()
                              if key != "orderbook"}, reference["evidence"])

    def test_actual_hourly_feature_computation_contract_is_consumed(self):
        from orderbook_features import compute_hour_features
        rows = candles()
        stamp, cutoff = rows[-1]["timestamp"], rows[-1]["timestamp"] + BAR_MS
        snapshots = [{"timestamp": t, "last_update_id": str(t),
                      "bids": [[100 - .005 * (i + 1), 3] for i in range(20)],
                      "asks": [[100 + .005 * (i + 1), 1] for i in range(20)],
                      "received_at": t + 5, "received_timestamp": t + 5}
                     for t in range(stamp, cutoff, 1000)]
        record = compute_hour_features(snapshots, pair=BTC, bar_open_ms=stamp,
                                       observed_at_ms=cutoff + 15_000, forward_start_ms=stamp)
        record["published_at_ms"] = cutoff + 16_000
        self.assertTrue(record["quality"]["eligible"])
        result = evaluate(spec_for(), rows, {**features(rows), BTC: record})
        audit = result["signals"][BTC]["evidence"]["orderbook"]
        self.assertTrue(audit["feature_validity"])
        self.assertTrue(audit["gate_pass"])
        self.assertEqual(result["signals"][BTC]["target_side"], 1)


if __name__ == "__main__":
    unittest.main()

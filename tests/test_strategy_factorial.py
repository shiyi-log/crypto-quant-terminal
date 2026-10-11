"""The factorial population is separate, complete, and explicitly constrained."""

import copy
import json
import unittest
from collections import Counter

from strategy_factorial import (
    ALLOWED_RESEARCH_PAIRS, BTC, ETH, RISK_BUDGETS, SPECS_PATH, STOP_WIDTHS,
    build_factorial_specs, load_factorial_specs, parse_factorial_identity,
)


class FactorialDefinitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(SPECS_PATH.read_text(encoding="utf-8"))
        cls.specs = build_factorial_specs(cls.document)

    def account(self, number, risk="R50", k=1.0):
        return next(spec for spec in self.specs
                    if spec["canonical_strategy_id"].startswith(f"s{number:02}_")
                    and spec["factorial_factors"]["risk_budget"] == risk
                    and spec["factorial_factors"]["stop_width_k"] == k)

    def test_real_research_snapshot_contains_exactly_nine_accounts_per_retained_strategy(self):
        self.assertEqual(len(self.specs), 261)
        self.assertEqual(len({spec["strategy_id"] for spec in self.specs}), 261)
        counts = Counter(spec["canonical_strategy_id"] for spec in self.specs)
        self.assertEqual(len(counts), 29)
        self.assertEqual(set(counts.values()), {9})
        self.assertFalse(any(sid.startswith(("s15_", "s31_")) for sid in counts))
        for spec in self.specs:
            with self.subTest(strategy_id=spec["strategy_id"]):
                parsed = parse_factorial_identity(spec["strategy_id"])
                self.assertEqual(parsed["canonical_strategy_id"], spec["variant_of"])
                self.assertEqual(parsed["max_loss_per_trade_usdt"],
                                 spec["max_loss_per_trade_usdt"])
                self.assertEqual(parsed["risk_stop_atr_mult"],
                                 spec["params"]["risk_stop_atr_mult"])
                self.assertEqual(spec["position_budget"]["leverage"], 100)

    def test_r_and_k_form_full_cartesian_product_for_each_mechanism(self):
        expected = {(risk, k) for risk, _ in RISK_BUDGETS for k in STOP_WIDTHS}
        for canonical in {spec["canonical_strategy_id"] for spec in self.specs}:
            with self.subTest(canonical=canonical):
                selected = [spec for spec in self.specs
                            if spec["canonical_strategy_id"] == canonical]
                self.assertEqual({(spec["factorial_factors"]["risk_budget"],
                                   spec["params"]["risk_stop_atr_mult"])
                                  for spec in selected}, expected)
                self.assertEqual({spec["risk_sizing_mode"] for spec in selected},
                                 {"entry_atr"})
                self.assertTrue(all(spec["min_warmup_bars"] >=
                                    spec["params"]["atr_window"] + 2 for spec in selected))
        for k in STOP_WIDTHS:
            spec = self.account(8, k=k)
            self.assertEqual(spec["params"]["stop_atr_mult"], k)
            self.assertEqual(spec["params"]["risk_stop_atr_mult"], k)

    def test_independent_strategy_data_universe_obeys_timeframe_and_three_coin_scope(self):
        for spec in self.specs:
            with self.subTest(strategy_id=spec["strategy_id"]):
                self.assertTrue(set(spec["execution_universe"]) <= set(ALLOWED_RESEARCH_PAIRS))
                self.assertTrue(set(spec["data_universe"]) <= set(ALLOWED_RESEARCH_PAIRS))
                if spec["timeframe"] == "15m":
                    self.assertTrue(set(spec["execution_universe"]) <= {BTC, ETH})
        self.assertEqual(self.account(14)["execution_universe"], list(ALLOWED_RESEARCH_PAIRS))
        for number in (13, 27):
            spec = self.account(number)
            self.assertEqual(spec["research_universe"], list(ALLOWED_RESEARCH_PAIRS))
            self.assertTrue(any(pair not in ALLOWED_RESEARCH_PAIRS
                                for pair in spec["source_original_universe"]))

    def test_cross_asset_trade_legs_and_blockers_are_explicit(self):
        self.assertEqual(self.account(4)["execution_universe"], [BTC, ETH])
        self.assertEqual(self.account(10)["execution_universe"], [ETH])
        self.assertEqual(self.account(10)["research_universe"], [BTC, ETH])
        self.assertEqual(self.account(19)["execution_universe"], [BTC])
        self.assertEqual(self.account(30)["execution_universe"], [BTC, ETH])
        for number in (4, 19, 24):
            spec = self.account(number)
            self.assertIs(spec["execution_enabled"], False)
            self.assertTrue(spec["blocked_execution_reason"])
            self.assertIn(spec["blocked_execution_reason"], spec["execution_blockers"])
        for number in (19, 24):
            spec = self.account(number)
            self.assertIn("paused_research_context_required", spec["execution_blockers"])
            self.assertEqual(len(spec["research_universe"]), 20)
            self.assertEqual(spec["data_universe"], list(ALLOWED_RESEARCH_PAIRS))
        self.assertIn("cross_sectional_portfolio_execution_unimplemented",
                      self.account(24)["execution_blockers"])
        for number in (10, 30):
            spec = self.account(number)
            self.assertIs(spec["execution_enabled"], True)
            self.assertIsNone(spec["blocked_execution_reason"])
            self.assertEqual(spec["execution_blockers"], [])
        self.assertEqual(sum(spec["execution_enabled"] for spec in self.specs), 234)
        self.assertEqual(sum(not spec["execution_enabled"] for spec in self.specs), 27)

    def test_s07_keep_and_legacy_state_or_hash_are_never_reused(self):
        document = copy.deepcopy(self.document)
        selected = next(item for item in document["strategies"]
                        if item["strategy_id"].startswith("s07_"))
        selected.update(rule_hash="old-rule", execution_hash="old-execution",
                        state={"positions": {BTC: {"entry_price": 100}}})
        before = copy.deepcopy(document)
        specs = build_factorial_specs(document)
        self.assertEqual(document, before)
        s07_specs = [spec for spec in specs if spec["variant_of"].startswith("s07_")]
        self.assertEqual(len(s07_specs), 9)
        for spec in s07_specs:
            self.assertEqual(spec["archive_status"], "keep")
            self.assertIs(spec["execution_enabled"], True)
            self.assertNotIn("rule_hash", spec)
            self.assertNotIn("execution_hash", spec)
            self.assertNotIn("state", spec)
            self.assertEqual(spec["legacy_samples_policy"],
                             "preserve_separate_from_factorial_evaluation")

    def test_accounts_and_research_are_deeply_independent(self):
        document = copy.deepcopy(self.document)
        before = copy.deepcopy(document)
        specs = build_factorial_specs(document)
        first, other = specs[0], copy.deepcopy(specs[1])
        first["params"]["atr_window"] = 999
        first["params_frozen"]["atr_window"] = 999
        first["position_budget"]["max_positions"] = 999
        first["execution_universe"].append("FAKE/USDT:USDT")
        first["factorial_factors"]["risk_budget"] = "FAKE"
        self.assertEqual(specs[1], other)
        self.assertEqual(document, before)
        self.assertNotEqual(first["params"], specs[1]["params"])

    def test_normalization_is_injected_and_preserves_execution_metadata(self):
        observed = []

        def normalizer(spec):
            observed.append(spec["strategy_id"])
            return {"strategy_id": spec["strategy_id"], "rule_hash": "verified-rule"}

        specs = build_factorial_specs(self.document, normalizer=normalizer)
        self.assertEqual(len(observed), 261)
        self.assertTrue(all(spec["rule_hash"] == "verified-rule" for spec in specs))
        self.assertEqual(specs[0]["max_loss_per_trade_usdt"], 50)
        self.assertEqual(specs[0]["execution_universe"], [BTC, ETH])
        with self.assertRaisesRegex(ValueError, "changed factorial account identity"):
            build_factorial_specs(self.document,
                                  normalizer=lambda spec: {"strategy_id": "other"})
        with self.assertRaisesRegex(ValueError, "changed factorial risk contract"):
            build_factorial_specs(self.document, normalizer=lambda spec: {
                "strategy_id": spec["strategy_id"], "max_loss_per_trade_usdt": 999})
        with self.assertRaisesRegex(ValueError, "removed an unsupported execution blocker"):
            build_factorial_specs(self.document, normalizer=lambda spec: {
                "strategy_id": spec["strategy_id"], "execution_enabled": True})

    def test_malformed_population_and_descriptive_pairs_fail_closed(self):
        variants = []
        missing = copy.deepcopy(self.document)
        missing["strategies"] = [item for item in missing["strategies"]
                                 if not item["strategy_id"].startswith("s07_")]
        variants.append((missing, "all 29"))
        duplicate = copy.deepcopy(self.document)
        duplicate["strategies"].append(copy.deepcopy(duplicate["strategies"][0]))
        variants.append((duplicate, "duplicate strategy"))
        descriptive = copy.deepcopy(self.document)
        descriptive["strategies"][0]["universe"] = ["BTC（交易）"]
        variants.append((descriptive, "invalid exchange pair"))
        invalid_atr = copy.deepcopy(self.document)
        invalid_atr["strategies"][0]["params_frozen"]["atr_window"] = True
        variants.append((invalid_atr, "positive integer"))
        mismatched_risk = copy.deepcopy(self.document)
        mismatched_risk["risk_budget_design"]["full_factorial"]["axes"]["risk_budget"][
            "max_loss_usdt"]["R50"] = 500
        variants.append((mismatched_risk, "declared factorial axes"))
        for document, error in variants:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                build_factorial_specs(document)

    def test_actual_adapter_normalization_is_idempotent_for_all_261_and_preserves_metadata(self):
        from strategy_factorial_signals import normalize_factorial_spec

        document = copy.deepcopy(self.document)
        document["strategies"][0]["source_annotations"] = {"review": "preserve this evidence"}
        specs = build_factorial_specs(document, normalizer=normalize_factorial_spec)
        self.assertEqual(len(specs), 261)
        self.assertEqual(sum(spec["execution_enabled"] for spec in specs), 234)
        self.assertEqual(specs[0]["source_annotations"], {"review": "preserve this evidence"})
        self.assertEqual(specs[0]["source_original_universe"], [BTC, ETH])
        for spec in specs:
            with self.subTest(strategy_id=spec["strategy_id"]):
                self.assertEqual(len(spec["rule_hash"]), 64)
                self.assertEqual(len(spec["signal_rule_hash"]), 64)
                normalized = normalize_factorial_spec(spec)
                self.assertEqual(normalized["rule_hash"], spec["rule_hash"])
                self.assertEqual(normalized["signal_rule_hash"], spec["signal_rule_hash"])
                self.assertEqual(normalized["params"], spec["params"])
                self.assertEqual(normalized["execution_universe"], spec["execution_universe"])

    def test_parser_does_not_treat_legacy_or_ambiguous_suffixes_as_new_accounts(self):
        for strategy_id in ("s01_orb_breakout_B15m_100x_maker",
                            "s01_orb_breakout_B15m_R50_k1_100x_maker",
                            "s01_orb_breakout_B15m_R51_k1.0_100x_maker",
                            "s01_orb_breakout_B15m_R50_k1.0_100x_maker_extra"):
            with self.subTest(strategy_id=strategy_id):
                self.assertIsNone(parse_factorial_identity(strategy_id))
        for strategy_id in ("s01_orb_breakout_Cgen2_R50_k1.0_100x_maker",
                            "s15_rejected_Cgen2_R50_k1.0_100x_maker"):
            with self.subTest(strategy_id=strategy_id), self.assertRaises(ValueError):
                parse_factorial_identity(strategy_id)
        self.assertEqual(len(load_factorial_specs()), 261)


if __name__ == "__main__":
    unittest.main()

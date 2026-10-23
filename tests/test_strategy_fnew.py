"""Fnew contracts separate hypotheses, markets, cost and frozen identities."""

import copy
import hashlib
import json
import math
import unittest
from collections import Counter

from strategy_fnew import (
    BTC, ETH, TRX, FNEW_CANONICAL_IDS, FNEW_DESIGN_VERSION,
    FNEW_PAIR_EXECUTION_POLICY, PAIR_BLOCKER, RISK_BUDGETS, SPECS_PATH,
    STOP_WIDTHS, build_fnew_specs, fnew_rule_hash, load_fnew_specs,
    normalize_fnew_spec, parse_fnew_identity,
)
from strategy_fee_schedule import DEMO_FEE_MODEL_VERSION


class FnewDefinitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(SPECS_PATH.read_text(encoding="utf-8"))
        cls.specs = build_fnew_specs(cls.document)

    def spec(self, number, risk="R50", k=1.5):
        return copy.deepcopy(next(spec for spec in self.specs
            if spec["canonical_strategy_id"].startswith(f"s{number}_")
            and spec["fnew_factors"]["risk_budget"] == risk
            and spec["params"]["risk_stop_atr_mult"] == k))

    def test_full_population_is_isolated_45_distinct_accounts(self):
        self.assertEqual(len(self.specs), 45)
        self.assertEqual(len({spec["strategy_id"] for spec in self.specs}), 45)
        self.assertEqual(Counter(spec["canonical_strategy_id"] for spec in self.specs),
                         Counter({canonical: 9 for canonical in FNEW_CANONICAL_IDS}))
        expected_axes = {(risk, k) for risk, _ in RISK_BUDGETS for k in STOP_WIDTHS}
        for canonical in FNEW_CANONICAL_IDS:
            actual = {(spec["fnew_factors"]["risk_budget"],
                       spec["params"]["risk_stop_atr_mult"])
                      for spec in self.specs if spec["canonical_strategy_id"] == canonical}
            self.assertEqual(actual, expected_axes)
        for spec in self.specs:
            with self.subTest(strategy=spec["strategy_id"]):
                self.assertLessEqual(len(spec["strategy_id"]), 80)
                self.assertEqual(spec["sample_population"], FNEW_DESIGN_VERSION)
                self.assertIn("_Fnew_Demo_", spec["strategy_id"])
                self.assertEqual(spec["fee_model_version"], DEMO_FEE_MODEL_VERSION)
                self.assertNotIn("factorial_design_version", spec)
                self.assertNotIn("factorial_factors", spec)
                self.assertTrue(spec["forward_hypothesis"])
                self.assertTrue(spec["not_profit_claim"])
                self.assertEqual(spec["archive_status"], "keep")

    def test_only_s33_is_taker_and_fee_enters_each_economic_contract(self):
        for spec in self.specs:
            identity = parse_fnew_identity(spec["strategy_id"])
            taker = spec["canonical_strategy_id"].startswith("s33_")
            self.assertEqual(identity["fee_convention"], "taker" if taker else "maker")
            self.assertEqual(spec["fee_rate_per_side"], .0004 if taker else .0002)
            self.assertEqual(identity["fee_model_version"], DEMO_FEE_MODEL_VERSION)
            self.assertEqual(identity["max_loss_per_trade_usdt"], spec["max_loss_per_trade_usdt"])
            self.assertEqual(identity["risk_stop_atr_mult"], spec["params"]["risk_stop_atr_mult"])
        for number, fee in ((33, .0002), (34, .0005)):
            changed = self.spec(number)
            changed["fee_rate_per_side"] = fee
            changed.pop("rule_hash")
            with self.assertRaisesRegex(ValueError, "risk budget or fee"):
                normalize_fnew_spec(changed)

    def test_pair_accounts_default_blocked_and_budget_is_group_scope(self):
        self.assertEqual(sum(spec["execution_enabled"] for spec in self.specs), 27)
        for number in (32, 36):
            spec = self.spec(number)
            self.assertFalse(spec["execution_enabled"])
            self.assertEqual(spec["execution_blockers"], [PAIR_BLOCKER])
            self.assertEqual(spec["blocked_execution_reason"], PAIR_BLOCKER)
            self.assertEqual(spec["risk_sizing_mode"], "entry_pair_atr")
            self.assertEqual(spec["risk_budget_scope"], "atomic_pair_portfolio")
            self.assertEqual(spec["paired_execution_policy"], FNEW_PAIR_EXECUTION_POLICY)
            self.assertIn("entry_fee_rate_per_side+exit_fee_rate_per_side",
                          spec["paired_notional_formula"])
            self.assertEqual(spec["pair_trade_sample_unit"], "one_atomic_group_not_two_independent_legs")
        for number in (33, 34, 35):
            self.assertEqual(self.spec(number)["risk_sizing_mode"], "entry_atr")
            self.assertEqual(self.spec(number)["risk_budget_scope"], "per_trade_leg")

    def test_pair_first_creation_opt_in_changes_hash_and_cannot_rewrite_frozen_account(self):
        enabled = build_fnew_specs(self.document, paired_execution_enabled=True)
        self.assertEqual(sum(spec["execution_enabled"] for spec in enabled), 45)
        enabled_by_id = {spec["strategy_id"]: spec for spec in enabled}
        for number in (32, 36):
            blocked = self.spec(number)
            allowed = enabled_by_id[blocked["strategy_id"]]
            self.assertNotEqual(blocked["rule_hash"], allowed["rule_hash"])
            self.assertEqual(allowed["execution_blockers"], [])
            changed = copy.deepcopy(blocked)
            changed.update(execution_enabled=True, execution_blockers=[], blocked_execution_reason=None)
            with self.assertRaisesRegex(ValueError, "rule hash mismatch"):
                normalize_fnew_spec(changed)
        self.assertEqual(self.spec(33)["rule_hash"],
                         enabled_by_id[self.spec(33)["strategy_id"]]["rule_hash"])

    def test_spot_is_a_separate_market_and_cannot_be_replaced_with_futures(self):
        spec = self.spec(33)
        self.assertEqual(spec["data_feeds"]["futures"]["pairs"], [BTC, ETH, TRX])
        self.assertEqual(spec["data_feeds"]["spot"]["market"], "spot")
        self.assertEqual(spec["data_feeds"]["spot"]["pairs"],
                         ["BTC/USDT", "ETH/USDT", "TRX/USDT"])
        self.assertEqual(spec["data_feeds"]["spot"]["pair_map"][TRX], "TRX/USDT")
        for mutation in ("market", "pair", "remove"):
            changed = copy.deepcopy(spec)
            changed.pop("rule_hash")
            if mutation == "market":
                changed["data_feeds"]["spot"]["market"] = "futures"
            elif mutation == "pair":
                changed["data_feeds"]["spot"]["pairs"] = [BTC, ETH, TRX]
            else:
                changed["data_feeds"].pop("spot")
            with self.assertRaisesRegex(ValueError, "market-separated"):
                normalize_fnew_spec(changed)
        for number in (32, 34, 35, 36):
            self.assertEqual(set(self.spec(number)["data_feeds"]), {"futures"})

    def test_assets_and_missing_math_defaults_are_explicit_and_hash_bound(self):
        for number in (32, 33, 34):
            self.assertEqual(self.spec(number)["execution_universe"], [BTC, ETH, TRX])
        for number in (35, 36):
            self.assertEqual(self.spec(number)["execution_universe"], [BTC, ETH])
        s32, s33, s34, s36 = (self.spec(n) for n in (32, 33, 34, 36))
        self.assertEqual(s32["params"]["adf_autolag"], "AIC")
        self.assertEqual(s32["params"]["adf_maxlag"], 5)
        self.assertTrue(s32["params"]["positive_beta_required"])
        self.assertEqual(s33["params"]["basis_delta_lag"], 1)
        self.assertEqual(s33["params"]["H"], 24)
        self.assertTrue(s34["params"]["force_flat_at_day_end"])
        self.assertEqual(s34["params"]["anchor_timezone"], "UTC")
        self.assertEqual(s36["params"]["rho_compare_lag"], 168)
        self.assertEqual(s36["params"]["rho_exit_mode"], "rise_from_entry_rho")
        self.assertEqual(s36["min_warmup_bars"], 337)
        self.assertEqual(s36["source_min_warmup_bars"], 180)
        changed = copy.deepcopy(s33)
        changed["params"]["basis_delta_lag"] = 2
        changed["params_frozen"]["basis_delta_lag"] = 2
        with self.assertRaisesRegex(ValueError, "rule hash mismatch"):
            normalize_fnew_spec(changed)

    def test_leverage_map_accepts_compact_symbols_and_persists_canonical_keys(self):
        spec = self.spec(34)
        spec.pop("rule_hash")
        spec["position_budget"]["leverage_by_symbol"] = {
            "BTCUSDT": 100,
            "ETHUSDT": 75,
            "TRXUSDT": 25,
        }
        normalized = normalize_fnew_spec(spec)
        self.assertEqual(
            set(normalized["position_budget"]["leverage_by_symbol"]),
            {BTC, ETH, TRX},
        )
        self.assertEqual(normalized["position_budget"]["leverage_by_symbol"][ETH], 75.0)

    def test_leverage_map_rejects_alias_collisions_after_normalization(self):
        spec = self.spec(34)
        spec.pop("rule_hash")
        spec["position_budget"]["leverage_by_symbol"] = {
            "BTCUSDT": 100,
            BTC: 100,
            "ETHUSDT": 75,
            "TRXUSDT": 25,
        }
        with self.assertRaisesRegex(ValueError, "duplicate leverage_by_symbol key"):
            normalize_fnew_spec(spec)

    def test_economic_hash_ignores_prose_but_rejects_risk_feed_and_capital_changes(self):
        spec = self.spec(34)
        changed = copy.deepcopy(spec)
        changed.update(name="new title", external_apy_claim="not evidence", evidence_source="other source")
        self.assertEqual(fnew_rule_hash(changed), spec["rule_hash"])
        changed["initial_capital"] = 20000
        with self.assertRaisesRegex(ValueError, "rule hash mismatch"):
            normalize_fnew_spec(changed)
        changed = copy.deepcopy(spec)
        changed["risk_stop_atr_mult"] = 999
        with self.assertRaisesRegex(ValueError, "top-level risk_stop"):
            normalize_fnew_spec(changed)
        changed.pop("risk_stop_atr_mult")
        changed["max_loss_per_trade_usdt"] = 100
        with self.assertRaisesRegex(ValueError, "risk budget or fee"):
            normalize_fnew_spec(changed)

    def test_factory_only_reads_selected_candidates_and_preserves_input_and_legacy_state(self):
        document = copy.deepcopy(self.document)
        old = copy.deepcopy(document["strategies"])
        document["strategy_proposals_by_dsh"]["candidates"].append(copy.deepcopy(old[0]))
        document["strategy_proposals_by_dsh"]["candidates"][0].update(
            rule_hash="legacy-rule", execution_hash="legacy-execution", state={"cash": 123},
            definition={"bad": "old"})
        before = copy.deepcopy(document)
        specs = build_fnew_specs(document)
        self.assertEqual(document, before)
        self.assertEqual(document["strategies"], old)
        for spec in specs:
            self.assertNotEqual(spec["rule_hash"], "legacy-rule")
            self.assertNotIn("execution_hash", spec)
            self.assertNotIn("state", spec)
            self.assertNotIn("definition", spec)
        specs[0]["params"]["atr_window"] = 999
        self.assertNotEqual(specs[1]["params"]["atr_window"], 999)
        self.assertEqual(document, before)

    def test_malformed_population_and_invalid_parameters_fail_closed(self):
        candidates = self.document["strategy_proposals_by_dsh"]["candidates"]
        missing = [item for item in candidates if item["strategy_id"] != FNEW_CANONICAL_IDS[4]]
        duplicate = next(item for item in candidates if item["strategy_id"] == FNEW_CANONICAL_IDS[0])
        for items, message in ((missing, "all five"),
                               (candidates + [duplicate], "duplicate")):
            doc = copy.deepcopy(self.document)
            doc["strategy_proposals_by_dsh"]["candidates"] = copy.deepcopy(items)
            with self.assertRaisesRegex(ValueError, message):
                build_fnew_specs(doc)
        for key, value in (("atr_window", True), ("atr_window", 1.2),
                           ("k_sigma", math.nan), ("sigma_window", 1),
                           ("unused_parameter", 3)):
            spec = self.spec(33)
            spec.pop("rule_hash")
            spec["params"][key] = value
            spec["params_frozen"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                normalize_fnew_spec(spec)
        spec = self.spec(35)
        spec["universe"] = [BTC, ETH, TRX]
        with self.assertRaisesRegex(ValueError, "asset universe"):
            normalize_fnew_spec(spec)

    def test_all_normalized_definitions_are_idempotent_and_sparse_id_gets_correct_k(self):
        for spec in self.specs:
            self.assertEqual(normalize_fnew_spec(spec), spec)
            self.assertEqual(fnew_rule_hash(spec), spec["rule_hash"])
        sparse = normalize_fnew_spec({"strategy_id": self.spec(33, k=1.0)["strategy_id"]})
        self.assertEqual(sparse["params"]["risk_stop_atr_mult"], 1.0)
        self.assertEqual(sparse["fee_rate_per_side"], .0004)
        self.assertEqual(len(load_fnew_specs()), 45)
        numeric_equivalent = self.spec(34)
        numeric_equivalent["position_budget"]["leverage"] = 100.0
        numeric_equivalent["position_budget"]["max_positions"] = 2.0
        self.assertEqual(fnew_rule_hash(numeric_equivalent), self.spec(34)["rule_hash"])
        numeric_equivalent["position_budget"]["max_per_coin"] = True
        with self.assertRaises(ValueError):
            normalize_fnew_spec(numeric_equivalent)

    def test_parser_separates_populations_and_rejects_wrong_fee_identity(self):
        for strategy_id in ("s01_orb_breakout_B15m_R50_k1.5_100x_maker",
                            "s33_basis_momentum_1h_Fnew_R50_k1_100x_taker",
                            "s33_basis_momentum_1h_Fnew_R55_k1.5_100x_taker",
                            "s33_basis_momentum_1h_Fnew_R50_k1.5_100x_taker_more"):
            self.assertIsNone(parse_fnew_identity(strategy_id))
        for strategy_id in ("s33_basis_momentum_1h_Fnew_R50_k1.5_100x_maker",
                            "s34_vwap_anchor_reversion_15m_Fnew_R50_k1.5_100x_taker",
                            "s32_unknown_Fnew_R50_k1.5_100x_maker"):
            with self.assertRaises(ValueError):
                parse_fnew_identity(strategy_id)

    def test_all_45_legacy_normalized_definitions_retain_original_values_and_hashes(self):
        # Captured from the original normalizer before Demo-table support. This
        # covers every emitted normalized field, not only the fee and rule hash.
        legacy = []
        for canonical in FNEW_CANONICAL_IDS:
            fee_mode = "taker" if canonical.startswith("s33_") else "maker"
            for risk, _ in RISK_BUDGETS:
                for k in STOP_WIDTHS:
                    strategy_id = f"{canonical}_Fnew_{risk}_k{k:.1f}_100x_{fee_mode}"
                    spec = normalize_fnew_spec({"strategy_id": strategy_id})
                    self.assertNotIn("fee_model_version", spec)
                    self.assertEqual(spec["fee_rate_per_side"], .0005 if fee_mode == "taker" else .0002)
                    self.assertEqual(normalize_fnew_spec(spec), spec)
                    legacy.append(spec)
        encoded = json.dumps(legacy, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(),
                         "82b8a8cd12c035215c668cd2780d51fa3c648a84db0b84a4def1cc57331c4d5f")
        s33 = next(spec for spec in legacy if spec["strategy_id"] ==
                   "s33_basis_momentum_1h_Fnew_R50_k1.5_100x_taker")
        self.assertEqual(s33["rule_hash"],
                         "107f51b4a3c783fd04302da4605c7db2835768abfa80a82377a2393a553b08fa")

    def test_fee_table_changes_require_demo_identity_even_for_unchanged_maker_rate(self):
        for number in (33, 34):
            demo = self.spec(number)
            legacy_id = demo["strategy_id"].replace("_Fnew_Demo_", "_Fnew_")
            legacy = normalize_fnew_spec({"strategy_id": legacy_id})
            self.assertNotEqual(demo["strategy_id"], legacy["strategy_id"])
            self.assertNotEqual(demo["rule_hash"], legacy["rule_hash"])
            relabeled = copy.deepcopy(legacy)
            relabeled["fee_model_version"] = DEMO_FEE_MODEL_VERSION
            relabeled.pop("rule_hash")
            with self.assertRaisesRegex(ValueError, "Demo-marked strategy identity"):
                normalize_fnew_spec(relabeled)
        changed_legacy_taker = normalize_fnew_spec({"strategy_id":
            "s33_basis_momentum_1h_Fnew_R50_k1.5_100x_taker"})
        changed_legacy_taker.update(fee_rate_per_side=.0004)
        changed_legacy_taker.pop("rule_hash")
        with self.assertRaisesRegex(ValueError, "risk budget or fee"):
            normalize_fnew_spec(changed_legacy_taker)

    def test_demo_fee_model_version_and_rate_are_frozen_together(self):
        for version in (None, "future-fee-model", ""):
            changed = self.spec(33)
            changed.pop("rule_hash")
            changed["fee_model_version"] = version
            with self.assertRaisesRegex(ValueError, "fee_model_version"):
                normalize_fnew_spec(changed)
        changed = self.spec(33)
        changed.pop("rule_hash")
        changed["fee_rate_per_side"] = .0005
        with self.assertRaisesRegex(ValueError, "risk budget or fee"):
            normalize_fnew_spec(changed)


if __name__ == "__main__":
    unittest.main()

"""Strategy-lab loading contracts for the opt-in 2x2 execution population."""

from collections import defaultdict
import unittest

from strategy_2x2_scope import ARMS, FOCUS_STRATEGY_IDS
from strategy_lab_service import load_2x2_specs, load_specs
from strategy_simulation import execution_identity


class StrategyLab2x2LoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.legacy = load_specs()
        cls.disabled = load_2x2_specs()
        cls.enabled = load_2x2_specs(activate=True)

    def test_loading_is_exactly_24_and_disabled_by_default(self):
        self.assertEqual(len(self.disabled), len(FOCUS_STRATEGY_IDS) * len(ARMS))
        self.assertTrue(all(spec["execution_enabled"] is False for spec in self.disabled))
        self.assertTrue(all(spec["blocked_execution_reason"] == "manual_activation_required"
                            for spec in self.disabled))

    def test_activation_is_explicit_and_does_not_change_identity_fields(self):
        self.assertEqual(
            [spec["strategy_id"] for spec in self.disabled],
            [spec["strategy_id"] for spec in self.enabled],
        )
        self.assertTrue(all(spec["execution_enabled"] is True for spec in self.enabled))
        self.assertTrue(all("blocked_execution_reason" not in spec for spec in self.enabled))
        for disabled, enabled in zip(self.disabled, self.enabled):
            with self.subTest(strategy_id=disabled["strategy_id"]):
                self.assertEqual(disabled["canonical_signal_rule_hash"],
                                 enabled["canonical_signal_rule_hash"])
                self.assertEqual(disabled["execution_override"], enabled["execution_override"])

    def test_new_ids_do_not_collide_with_frozen_population(self):
        frozen_ids = {spec["strategy_id"] for spec in self.legacy}
        candidate_ids = [spec["strategy_id"] for spec in self.disabled]
        self.assertEqual(len(candidate_ids), len(set(candidate_ids)))
        self.assertFalse(frozen_ids.intersection(candidate_ids))
        self.assertTrue(all("_2x2_" in strategy_id for strategy_id in candidate_ids))

    def test_each_family_preserves_signal_hash_and_separates_execution_hash(self):
        by_family = defaultdict(list)
        for spec in self.enabled:
            by_family[spec["variant_of"]].append(spec)
        self.assertEqual(set(by_family), set(FOCUS_STRATEGY_IDS))
        for family, specs in by_family.items():
            with self.subTest(family=family):
                self.assertEqual(len(specs), len(ARMS))
                identities = [execution_identity(spec) for spec in specs]
                self.assertEqual(len({identity["rule_hash"] for identity in identities}), 1)
                self.assertEqual(len({identity["execution_hash"] for identity in identities}),
                                 len(ARMS))
                self.assertEqual({identity["execution"]["leverage"] for identity in identities},
                                 {30, 100})
                self.assertEqual(
                    {identity["execution"]["stop_execution_mode"] for identity in identities},
                    {"close_confirmed", "intrabar"},
                )

    def test_disabled_specs_remain_blocked_at_evaluation_boundary(self):
        from strategy_lab_service import evaluate_account

        for spec in self.disabled:
            with self.subTest(strategy_id=spec["strategy_id"]):
                result = evaluate_account(spec, {})
                self.assertFalse(result["ready"])
                self.assertEqual(result["warmup"]["reason"], "manual_activation_required")


if __name__ == "__main__":
    unittest.main()

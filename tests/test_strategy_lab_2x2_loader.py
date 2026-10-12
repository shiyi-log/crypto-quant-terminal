"""The strategy-lab 2x2 population is explicit and identity-safe."""

import unittest

from strategy_2x2_scope import ARMS, FOCUS_STRATEGY_IDS
from strategy_lab_service import load_2x2_specs, load_specs, normalize_account_spec
from strategy_simulation import execution_identity


class StrategyLab2x2LoaderTests(unittest.TestCase):
    def test_default_population_does_not_include_2x2_accounts(self):
        specs = load_specs()
        self.assertFalse(any("_2x2_" in spec["strategy_id"] for spec in specs))

    def test_loader_returns_exactly_24_disabled_candidates_by_default(self):
        specs = load_2x2_specs()
        self.assertEqual(len(specs), len(FOCUS_STRATEGY_IDS) * len(ARMS))
        self.assertTrue(all(spec["execution_enabled"] is False for spec in specs))
        self.assertTrue(all(spec["blocked_execution_reason"] == "manual_activation_required"
                            for spec in specs))

    def test_opt_in_activates_only_the_new_24_identities(self):
        specs = load_2x2_specs(activate=True)
        self.assertEqual(len(specs), 24)
        self.assertTrue(all(spec["execution_enabled"] is True for spec in specs))
        self.assertTrue(all("blocked_execution_reason" not in spec for spec in specs))
        self.assertEqual(len({spec["strategy_id"] for spec in specs}), 24)

        for family in FOCUS_STRATEGY_IDS:
            rows = [spec for spec in specs if spec["variant_of"] == family]
            self.assertEqual(len(rows), 4)
            identities = [execution_identity(spec) for spec in rows]
            self.assertEqual(len({identity["rule_hash"] for identity in identities}), 1)
            self.assertEqual(len({identity["execution_hash"] for identity in identities}), 4)
            self.assertEqual({identity["execution"]["leverage"] for identity in identities},
                             {30, 100})
            self.assertEqual({identity["execution"]["stop_execution_mode"]
                              for identity in identities}, {"close_confirmed", "intrabar"})
            # Validate the same contract used by account creation, without
            # touching a store or creating an account as part of this test.
            for spec in rows:
                normalized = normalize_account_spec(spec)
                self.assertEqual(normalized["rule_hash"], spec["canonical_signal_rule_hash"])


if __name__ == "__main__":
    unittest.main()

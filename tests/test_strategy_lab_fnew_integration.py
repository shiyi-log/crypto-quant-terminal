"""Real PostgreSQL integration for the Fnew population in the strategy lab.

Runs only against an isolated ``quant_test`` schema.  It proves the piece that
unit tests with mocks cannot: the Fnew accounts really can be created beside
the frozen legacy population, their identities are stable across restarts, and
enabling paired execution produces a distinct identity rather than mutating a
blocked one.
"""

import os
import unittest
import uuid

from strategy_fnew import load_fnew_specs
from strategy_lab_service import load_specs
from strategy_simulation import StrategySimulation, execution_identity

TEST_URL = os.getenv("QUANT_TEST_DATABASE_URL")
BOUNDARY_MS = 1_791_796_500_000


@unittest.skipUnless(TEST_URL, "需要 QUANT_TEST_DATABASE_URL 才运行隔离 PostgreSQL 集成测试")
class FnewAccountCreationTests(unittest.TestCase):
    def setUp(self):
        from psycopg.conninfo import conninfo_to_dict

        from data_store import DataStore

        test = conninfo_to_dict(TEST_URL)
        production = conninfo_to_dict(os.environ["QUANT_DATABASE_URL"]) \
            if os.environ.get("QUANT_DATABASE_URL") else {}

        def location(config):
            return tuple(config.get(name, "") for name in ("host", "port", "dbname"))

        name = test.get("dbname", "").lower()
        if not ("test" in name or name == "quant_ci") or (
                production and location(test) == location(production)):
            raise RuntimeError("QUANT_TEST_DATABASE_URL 必须指向独立测试数据库")
        # Every test gets its own schema so account counts cannot leak between
        # them, and the real creation path is exercised on every run.
        self.schema = "quant_test_" + uuid.uuid4().hex
        self.store = DataStore(dsn=TEST_URL, schema=self.schema)
        self.store.initialize()
        self.simulation = StrategySimulation(self.store)

    def tearDown(self):
        import psycopg
        from psycopg import sql

        self.store.close()
        with psycopg.connect(TEST_URL) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def accounts(self):
        with self.store._connection() as conn:
            return conn.execute("""SELECT strategy_id, rule_hash, execution_hash, definition
                FROM strategy_sim_accounts""").fetchall()

    def test_legacy_population_is_untouched_when_fnew_is_added(self):
        legacy = load_specs()
        self.simulation.ensure_accounts(legacy, BOUNDARY_MS)
        before = {row["strategy_id"]: (row["rule_hash"], row["execution_hash"])
                  for row in self.accounts()}
        self.assertEqual(len(before), len(legacy))

        self.simulation.ensure_accounts(load_fnew_specs(), BOUNDARY_MS)
        after = {row["strategy_id"]: (row["rule_hash"], row["execution_hash"])
                 for row in self.accounts()}
        for strategy_id, hashes in before.items():
            self.assertEqual(after[strategy_id], hashes,
                             "adding Fnew must not rewrite a frozen legacy identity")
        self.assertEqual(len(after), len(legacy) + 45)

    def test_fnew_creation_is_idempotent_across_restarts(self):
        specs = load_fnew_specs()
        self.simulation.ensure_accounts(specs, BOUNDARY_MS)
        first = {row["strategy_id"]: (row["rule_hash"], row["execution_hash"])
                 for row in self.accounts()}
        self.simulation.ensure_accounts(load_fnew_specs(), BOUNDARY_MS)
        second = {row["strategy_id"]: (row["rule_hash"], row["execution_hash"])
                  for row in self.accounts()}
        self.assertEqual(first, second)
        self.assertEqual(len(second), 45)

    def test_blocked_paired_accounts_are_created_but_not_executable(self):
        specs = load_fnew_specs()
        self.simulation.ensure_accounts(specs, BOUNDARY_MS)
        rows = {row["strategy_id"]: row for row in self.accounts()}
        blocked = [s for s in specs if s["execution_enabled"] is False]
        self.assertEqual(len(blocked), 18)
        for spec in blocked:
            self.assertIn(spec["strategy_id"], rows)
            self.assertEqual(spec["blocked_execution_reason"],
                             "paired_atr_stop_policy_unimplemented")
        enabled_ids = {s["strategy_id"] for s in specs if s["execution_enabled"]}
        self.assertEqual(len(enabled_ids), 27)

    def test_enabling_paired_execution_is_a_new_identity(self):
        blocked = {spec["strategy_id"]: execution_identity(spec)
                   for spec in load_fnew_specs()}
        enabled = {spec["strategy_id"]: execution_identity(spec)
                   for spec in load_fnew_specs(paired_execution_enabled=True)}
        self.assertEqual(set(blocked), set(enabled))
        differing = [key for key in blocked
                     if blocked[key]["rule_hash"] != enabled[key]["rule_hash"]]
        self.assertEqual(len(differing), 18,
                         "all paired identities must change when the policy is enabled")
        for key in differing:
            # The blocker is bound into the execution identity too, so an
            # account created while blocked can never be silently upgraded.
            self.assertNotEqual(blocked[key]["execution_hash"],
                                enabled[key]["execution_hash"])
            self.assertEqual(blocked[key]["execution"]["execution_enabled"], False)
            self.assertEqual(blocked[key]["execution"]["blocked_execution_reason"],
                             "paired_atr_stop_policy_unimplemented")
            self.assertEqual(enabled[key]["execution"]["execution_enabled"], True)
            self.assertFalse(enabled[key]["execution"].get("blocked_execution_reason"))
        for key in differing:
            # Both states declare the same pair policy; only the blocker differs.
            self.assertEqual(enabled[key]["execution"]["risk_sizing_mode"], "entry_pair_atr")
            self.assertEqual(enabled[key]["execution"]["paired_execution_policy"],
                             "atomic-pair-equal-notional-ohlc-v1")
            self.assertEqual(enabled[key]["execution"]["pair_trade_count_policy"],
                             "completed_pair_is_one_experimental_sample;leg_events_are_audit_only")
            self.assertAlmostEqual(enabled[key]["execution"]["risk_round_trip_fee"], 0.0012)
        for key in set(enabled) - set(differing):
            self.assertEqual(enabled[key]["execution"]["risk_sizing_mode"], "entry_atr")

    def test_single_leg_fnew_accounts_bind_their_own_fee_convention(self):
        specs = {s["canonical_strategy_id"]: s for s in load_fnew_specs()}
        basis = execution_identity(specs["s33_basis_momentum_1h"])
        self.assertEqual(basis["execution"]["risk_sizing_mode"], "entry_atr")
        self.assertEqual(basis["execution"]["fee_rate_per_side"], 0.0004)
        self.assertEqual(basis["execution"]["exit_fee_rate_per_side"], 0.0004)
        self.assertEqual(basis["rule_hash"], specs["s33_basis_momentum_1h"]["rule_hash"])
        maker = execution_identity(specs["s34_vwap_anchor_reversion_15m"])
        self.assertEqual(maker["execution"]["fee_rate_per_side"], 0.0002)
        self.assertEqual(maker["execution"]["exit_fee_rate_per_side"], 0.0004)


if __name__ == "__main__":
    unittest.main()

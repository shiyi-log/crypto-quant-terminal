"""Generation-2 specs must load without mixing research and trade identities."""
import unittest

from strategy_lab_service import StrategyLabService, load_specs
from strategy_simulation import execution_identity


class Generation2IdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.specs = load_specs()

    def test_all_frozen_generation_two_specs_load_as_distinct_accounts(self):
        ids = [spec["strategy_id"] for spec in self.specs]
        self.assertGreaterEqual(len(ids), 20)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(value.endswith("_100x_maker") for value in ids))
        self.assertEqual(len({spec["rule_hash"] for spec in self.specs}), len(ids))

    def test_native_timeframe_is_part_of_execution_identity(self):
        # Account IDs are deliberately variant-scoped (for example the
        # generation-2 arm uses a ``_Cgen2`` suffix).  The research ID is the
        # stable lookup key; the variant suffix itself is part of identity.
        by_research_id = {spec["variant_of"]: spec for spec in self.specs}
        hourly = by_research_id["s14_vol_compression_release"]
        daily = by_research_id["s13_daily_trend_multi_horizon"]
        hourly_identity = execution_identity(hourly)
        daily_identity = execution_identity(daily)
        self.assertEqual(hourly_identity["execution"]["timeframe"], "1h")
        self.assertEqual(daily_identity["execution"]["timeframe"], "1d")
        self.assertEqual(hourly_identity["execution"]["bar_ms"], 3_600_000)
        self.assertEqual(daily_identity["execution"]["bar_ms"], 86_400_000)
        self.assertNotEqual(hourly_identity["execution_hash"], daily_identity["execution_hash"])

    def test_auxiliary_research_universe_does_not_become_an_order_leg(self):
        breadth = next(spec for spec in self.specs
                       if spec["variant_of"] == "s19_multi_coin_breadth")
        self.assertEqual(len(breadth["research_universe"]), 20)
        self.assertIn("SOL/USDT:USDT", breadth["research_universe"])
        self.assertNotIn("SOL/USDT:USDT", breadth["execution_universe"])
        self.assertEqual(tuple(breadth["execution_universe"]),
                         ("BTC/USDT:USDT",))
        # The native multi-timeframe adapter is now active; it is still
        # important that auxiliary symbols remain research-only inputs.
        self.assertEqual(breadth["data_status"], "native_timeframe_adapter_active")

    def test_context_adapter_does_not_expand_execution_legs(self):
        service = StrategyLabService(object(), object(), self.specs, now_ms=86_400_000)
        breadth = next(spec for spec in self.specs
                       if spec["variant_of"] == "s24_cross_sectional_lowvol")
        loaded = {
            "BTC/USDT:USDT": [], "ETH/USDT:USDT": [],
            **{f"COIN{i}/USDT:USDT": [] for i in range(18)},
        }
        evaluated = service._evaluation_spec(breadth, loaded)
        self.assertEqual(tuple(evaluated["execution_universe"]),
                         ("BTC/USDT:USDT", "ETH/USDT:USDT"))
        self.assertEqual(len(evaluated["research_universe"]), 20)
        self.assertEqual(tuple(service._native_trade_candles(breadth, loaded)),
                         ("BTC/USDT:USDT", "ETH/USDT:USDT"))


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import datetime, timezone

from strategy_signals import BTC, ETH, LEGACY_DEFAULTS as DEFAULTS, _single, evaluate, normalize_spec


DAY = 86_400_000


def daily_rows(count=220, *, close_fn=None, volume_fn=None):
    start = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    rows = []
    for index in range(count):
        close = close_fn(index) if close_fn else 100 + index * 0.1
        volume = volume_fn(index) if volume_fn else 100.0
        rows.append({"timestamp": start + index * DAY, "open": close,
                     "high": close + 0.5, "low": close - 0.5,
                     "close": close, "volume": volume,
                     "extras": {"closed": True}})
    return rows


class GenerationThreeSignalTests(unittest.TestCase):
    def test_specs_have_native_daily_timeframe_and_distinct_hashes(self):
        ids = ["s22_dual_confirmation_trend", "s23_triple_confirmation_breakout",
               "s24_cross_sectional_lowvol", "s25_vol_regime_momentum",
               "s26_surge_gated_trend", "s27_long_horizon_mean_reversion",
               "s28_weekly_breakout", "s29_trend_with_trailing",
               "s30_dual_asset_confirmation"]
        normalized = [normalize_spec({"strategy_id": strategy_id}) for strategy_id in ids]
        self.assertTrue(all(spec["timeframe"] == "1d" for spec in normalized))
        self.assertEqual(len({spec["rule_hash"] for spec in normalized}), len(ids))

    def test_dual_confirmation_requires_volume(self):
        rows = daily_rows(volume_fn=lambda index: 200.0 if index == 219 else 100.0)
        side, reason, evidence, _ = _single(
            "s22_dual_confirmation_trend", rows,
            dict(DEFAULTS["s22_dual_confirmation_trend"][0]), None)
        self.assertEqual(side, 1)
        self.assertEqual(reason, "dual_confirmation_trend")
        self.assertGreaterEqual(evidence["volume_ratio"], 1.5)

    def test_long_horizon_reversion_fades_extreme_close(self):
        rows = daily_rows(close_fn=lambda index: 100 + (index % 5) - 2)
        rows[-1]["close"] = 130
        side, reason, evidence, _ = _single(
            "s27_long_horizon_mean_reversion", rows,
            dict(DEFAULTS["s27_long_horizon_mean_reversion"][0]), None)
        self.assertEqual(side, -1)
        self.assertEqual(reason, "long_horizon_mean_reversion")
        self.assertGreaterEqual(evidence["zscore"], 2.5)

    def test_dual_asset_confirmation_requires_matching_states(self):
        btc = daily_rows(close_fn=lambda index: 100 + index * 0.1)
        eth = daily_rows(close_fn=lambda index: 100 - index * 0.1)
        result = evaluate({"strategy_id": "s30_dual_asset_confirmation"},
                          {BTC: btc, ETH: eth})
        self.assertTrue(result["ready"])
        self.assertIsNone(result["signals"][BTC]["target_side"])
        self.assertEqual(result["signals"][BTC]["reason"],
                         "dual_asset_confirmation_not_met")

    def test_cross_sectional_strategy_fails_closed_without_twenty_coins(self):
        rows = daily_rows()
        result = evaluate({"strategy_id": "s24_cross_sectional_lowvol"},
                          {BTC: rows, ETH: rows})
        self.assertFalse(result["ready"])
        self.assertEqual(result["warmup"]["reason"], "multi_asset_context_unavailable")


if __name__ == "__main__":
    unittest.main()

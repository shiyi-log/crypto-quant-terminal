import unittest
from datetime import datetime

from strategy_signals import LEGACY_DEFAULTS as DEFAULTS, _single


def rows(count, *, start="2025-01-06T00:00:00+00:00", step_ms=900_000,
         close=100.0, volume=100.0):
    start_ms = int(datetime.fromisoformat(start).timestamp() * 1000)
    return [{"timestamp": start_ms + i * step_ms, "open": close,
             "high": close + 0.5, "low": close - 0.5, "close": close,
             "volume": volume, "extras": {"closed": True}}
            for i in range(count)]


class GenerationTwoSignalTests(unittest.TestCase):
    def test_s14_reports_atr_for_a_compression_breakout(self):
        data = rows(301, step_ms=3_600_000)
        data[-1].update(open=100, high=102, low=99.5, close=101.0)
        params = dict(DEFAULTS["s14_vol_compression_release"][0])
        side, reason, evidence, _ = _single("s14_vol_compression_release", data,
                                           params, None)
        self.assertEqual((side, reason), (1, "compression_release"))
        self.assertGreater(evidence["atr_pct"], 0)
        self.assertEqual(evidence["compression_high"], 100.5)

    def test_s12_volume_surge_then_breakout(self):
        data = rows(220)
        data[200].update(open=100, high=110, low=99, close=100, volume=1_000)
        data[-1].update(open=110, high=112, low=109, close=111)
        side, reason, evidence, _ = _single("s12_surge_expansion_breakout", data,
                                             dict(DEFAULTS["s12_surge_expansion_breakout"][0]), None)
        self.assertEqual((side, reason), (1, "surge_expansion_breakout"))
        self.assertEqual(evidence["surge_root_ms"], data[200]["timestamp"])

    def test_s16_only_enters_at_configured_utc_hour(self):
        data = rows(3, start="2025-01-06T06:00:00+00:00", step_ms=3_600_000)
        data[-1].update(open=100, high=102, low=99, close=101)
        params = dict(DEFAULTS["s16_weekly_seasonality"][0])
        side, reason, _, _ = _single("s16_weekly_seasonality", data, params, None)
        self.assertEqual((side, reason), (1, "seasonality_momentum"))
        data[-1]["timestamp"] += 3_600_000
        side, reason, _, _ = _single("s16_weekly_seasonality", data, params, None)
        self.assertIsNone(side)
        self.assertEqual(reason, "seasonality_window_closed")

    def test_s17_fades_completed_overnight_range(self):
        data = rows(40, start="2025-01-06T00:00:00+00:00", step_ms=900_000)
        for row in data[:32]:
            row.update(high=110, low=90)
        data[-1].update(open=110, high=111, low=109, close=110)
        side, reason, evidence, _ = _single("s17_overnight_range_fade", data,
                                             dict(DEFAULTS["s17_overnight_range_fade"][0]), None)
        self.assertEqual((side, reason), (-1, "overnight_boundary_fade"))
        self.assertEqual((evidence["overnight_high"], evidence["overnight_low"]), (110, 90))

    def test_s18_requires_two_bullish_confirmation_bars(self):
        data = rows(205, start="2025-01-06T00:00:00+00:00", step_ms=3_600_000)
        data[100].update(open=199, high=200, low=198, close=200)
        for row in data[101:]:
            row.update(open=180, high=181, low=179, close=180)
        data[-2].update(open=175, high=183, low=174, close=182)
        data[-1].update(open=182, high=186, low=181, close=183)
        side, reason, evidence, _ = _single("s18_drawdown_recovery", data,
                                             dict(DEFAULTS["s18_drawdown_recovery"][0]), None)
        self.assertEqual((side, reason), (1, "drawdown_recovery"))
        self.assertGreaterEqual(evidence["drawdown"], 0.08)

    def test_s20_uses_completed_day_range(self):
        data = rows(100, start="2025-01-06T00:00:00+00:00", step_ms=900_000)
        for row in data:
            row.update(high=110, low=90, close=100)
        data[-1].update(open=109, high=111, low=108, close=110)
        side, reason, evidence, _ = _single("s20_intraday_range_position", data,
                                             dict(DEFAULTS["s20_intraday_range_position"][0]), None)
        self.assertEqual((side, reason), (1, "intraday_range_position"))
        self.assertGreaterEqual(evidence["range_position"], 0.9)

    def test_s21_requires_cascade_and_reversal_wick(self):
        data = rows(5, start="2025-01-06T00:00:00+00:00", step_ms=900_000)
        for row in data:
            row.update(open=100, high=101, low=99, close=100.6)
        data[-1].update(open=102.4, high=105, low=102, close=103)
        side, reason, evidence, _ = _single("s21_liquidation_cascade_fade", data,
                                             dict(DEFAULTS["s21_liquidation_cascade_fade"][0]), None)
        self.assertEqual((side, reason), (-1, "liquidation_cascade_fade"))
        self.assertGreaterEqual(abs(evidence["cascade_move"]), 0.02)


if __name__ == "__main__":
    unittest.main()

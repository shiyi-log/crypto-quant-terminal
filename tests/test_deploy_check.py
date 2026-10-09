"""Regression tests for deployment-point date alignment."""
import sys
import types
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from bot import deploy_check


class DeploymentDateTests(unittest.TestCase):
    def test_prior_candle_dates_maps_fill_to_previous_candle(self):
        candles = pd.date_range("2026-01-01", periods=3, freq="D")
        data = {"BTC/USDT": pd.DataFrame(index=candles)}

        result = deploy_check.prior_candle_dates(
            data, pd.to_datetime([candles[0], candles[1], candles[2]])
        )

        self.assertTrue(pd.isna(result[0]))
        self.assertEqual(result[1], candles[0])
        self.assertEqual(result[2], candles[1])

    def test_evaluate_deployment_uses_signal_date_score(self):
        signal_day = pd.Timestamp("2026-01-01")
        fill_day = pd.Timestamp("2026-01-02")
        meta = pd.DataFrame({
            "date": [signal_day, fill_day],
            "coin": ["BTC", "BTC"],
            "label": [1, 1],
        })
        pts = pd.DataFrame({
            "date": [fill_day],
            "decision_date": [signal_day],
            "coin": ["BTC"],
            "profit_pct": [2.5],
        })
        observed = {}

        def capture_score(values, returns):
            observed["scores"] = np.asarray(values)
            observed["returns"] = np.asarray(returns)
            return 0.0, 0.0, len(values)

        with patch.object(deploy_check, "_ic_t", side_effect=capture_score):
            result = deploy_check.evaluate_deployment(np.array([0.2, 0.9]), meta, pts,
                                                      log=lambda _: None)

        np.testing.assert_array_equal(observed["scores"], [0.2])
        np.testing.assert_array_equal(observed["returns"], [2.5])
        self.assertEqual(result["n"], 1)

    def test_multi_entry_points_are_marked_as_raw_event_proxies(self):
        event = pd.DataFrame({
            "date": [pd.Timestamp("2026-01-01")],
            "coin": ["BTC"],
            "ret": [0.03],
            "side": [1],
        })
        ml = types.SimpleNamespace(load_ohlcv=lambda: {"BTC/USDT": pd.DataFrame()})
        entry = types.SimpleNamespace(entry_events=lambda data, period: event.copy())

        with patch.dict(sys.modules, {"ml_lab": ml, "ml_entry_trained": entry}):
            points = deploy_check.deployment_points(entries=[20, 30], log=lambda _: None)

        self.assertEqual(len(points), 2)
        self.assertEqual(set(points["sample_kind"]), {"raw_entry_event_proxy"})
        self.assertEqual(set(points["entry_period"]), {20, 30})
        self.assertTrue(points["fill_date"].isna().all())
        pd.testing.assert_series_equal(points["decision_date"], points["date"],
                                       check_names=False)


if __name__ == "__main__":
    unittest.main()

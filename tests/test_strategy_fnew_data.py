"""Fnew loaders keep spot identity, timestamps and validity independent."""
from copy import deepcopy
import unittest

from strategy_fnew_data import PAIR_MAP, fnew_data_status, load_fnew_candles

HOUR = 3_600_000
PERP = "BTC/USDT:USDT"
SPOT = "BTC/USDT"


def row(stamp, close=100, *, closed=True, quote_volume=10):
    return {"timestamp": stamp, "open": close, "high": close + 1,
            "low": close - 1, "close": close, "volume": 1,
            "extras": {"closed": closed, "quote_volume": quote_volume}}


def basis_spec(pairs=(PERP,)):
    return {"canonical_strategy_id": "s33_basis_momentum_1h", "timeframe": "1h",
            "min_warmup_bars": 3, "data_universe": list(pairs),
            "data_feeds": {"futures": {"market": "futures", "timeframe": "1h",
                                       "pairs": list(pairs)},
                           "spot": {"market": "spot", "timeframe": "1h",
                                    "pairs": [PAIR_MAP[pair] for pair in pairs],
                                    "pair_map": {pair: PAIR_MAP[pair] for pair in pairs}}}}


class Store:
    def __init__(self, datasets):
        self.datasets, self.calls = datasets, []

    def get_candles(self, exchange, market, pair, timeframe, candle_type, **kwargs):
        self.calls.append((exchange, market, pair, timeframe, candle_type, kwargs))
        rows = self.datasets.get((market, pair, timeframe), [])
        return [item for item in rows if kwargs.get("end") is None
                or item["timestamp"] <= kwargs["end"]][-kwargs["limit"]:]


class InternalGapRepairTests(unittest.TestCase):
    """A native 1h window that is long and current but has an internal hole must
    still run the five-minute fallback.  Otherwise ``_continuous_suffix``
    truncates the history to the rows after the hole -- the production defect
    behind 57 blocked 1h strategies on 2026-10-16.

    Only the s33 basis path reads through ``_market_rows``, so the regression
    test uses a basis spec and drives both the perpetual and the spot leg.
    """

    @staticmethod
    def _datasets():
        # 1h perpetual: hours 0,1,2,4 -- hour 3 is missing, but both the row
        # count and the newest stamp still satisfy the loader's freshness checks.
        perp = [row(i * HOUR, 100 + i) for i in (0, 1, 2, 4)]
        spot = [row(i * HOUR, 80 + i) for i in (0, 1, 2, 3, 4)]
        fivemin = [{"timestamp": i * 300_000, "open": 100, "high": 101, "low": 99,
                    "close": 100, "volume": 1,
                    "extras": {"closed": True, "quote_volume": 1}}
                   for i in range(0, 60)]
        return {("futures", PERP, "1h"): perp,
                ("futures", PERP, "5m"): fivemin,
                ("spot", SPOT, "1h"): spot}

    def test_internal_native_gap_triggers_the_full_five_minute_fallback(self):
        store = Store(self._datasets())
        spec = basis_spec()
        result = load_fnew_candles(store, spec, limit=3, as_of_ms=5 * HOUR)

        perpetual_5m = [call for call in store.calls
                        if call[1] == "futures" and call[3] == "5m"]
        self.assertTrue(perpetual_5m,
                        "an internal native gap must trigger the 5m fallback")
        # The fallback must read the full window, never only the newer tail.
        self.assertEqual(perpetual_5m[0][5]["limit"], 3 * 12 + 2)

        stamps = [item["timestamp"] for item in result[PERP]]
        self.assertEqual(stamps, [2 * HOUR, 3 * HOUR, 4 * HOUR])
        self.assertIn(3 * HOUR, stamps, "the missing hour must be repaired")


class FnewDataTests(unittest.TestCase):
    def test_two_actual_markets_exactly_align_without_price_substitution(self):
        futures = [row(i * HOUR, 100 + i) for i in range(4)]
        spot = [row(i * HOUR, 80 + i) for i in range(1, 4)]
        store = Store({("futures", PERP, "1h"): futures, ("spot", SPOT, "1h"): spot})
        result = load_fnew_candles(store, basis_spec(), limit=3, as_of_ms=4 * HOUR)
        self.assertEqual([item["timestamp"] for item in result[PERP]], [HOUR, 2 * HOUR, 3 * HOUR])
        self.assertEqual(result[PERP][-1]["close"], 103)
        self.assertEqual(result["spot"][SPOT][-1]["close"], 83)
        self.assertTrue(fnew_data_status(basis_spec(), result, as_of_ms=4 * HOUR)["ready"])
        self.assertEqual([call[:5] for call in store.calls], [
            ("binance", "futures", PERP, "1h", "futures"),
            ("binance", "spot", SPOT, "1h", "spot")])

    def test_stale_spot_does_not_trim_future_clock_or_forward_fill(self):
        store = Store({("futures", PERP, "1h"): [row(i * HOUR) for i in range(4)],
                       ("spot", SPOT, "1h"): [row(i * HOUR, 90) for i in range(3)]})
        result = load_fnew_candles(store, basis_spec(), limit=3, as_of_ms=4 * HOUR)
        self.assertEqual(result[PERP][-1]["timestamp"], 3 * HOUR)
        self.assertEqual(result["spot"][SPOT][-1]["timestamp"], 2 * HOUR)
        status = fnew_data_status(basis_spec(), result, as_of_ms=4 * HOUR)
        self.assertFalse(status["ready"])
        self.assertIn("spot_data_stale", status["by_pair"][PERP]["reasons"])
        self.assertIn("basis_timestamps_not_exactly_synchronized", status["by_pair"][PERP]["reasons"])

    def test_both_markets_stale_even_at_same_stamp_are_not_ready(self):
        store = Store({(market, pair, "1h"): [row(i * HOUR) for i in range(3)]
                       for market, pair in [("futures", PERP), ("spot", SPOT)]})
        result = load_fnew_candles(store, basis_spec(), limit=3, as_of_ms=4 * HOUR)
        status = fnew_data_status(basis_spec(), result, as_of_ms=4 * HOUR)
        self.assertFalse(status["ready"])
        self.assertIn("futures_data_stale", status["by_pair"][PERP]["reasons"])
        self.assertIn("spot_data_stale", status["by_pair"][PERP]["reasons"])

    def test_missing_close_flags_never_become_closed_by_elapsed_time(self):
        unflagged = [row(i * HOUR) for i in range(4)]
        for item in unflagged:
            item["extras"].pop("closed")
        store = Store({("futures", PERP, "1h"): [row(i * HOUR) for i in range(4)],
                       ("spot", SPOT, "1h"): unflagged})
        result = load_fnew_candles(store, basis_spec(), limit=3, as_of_ms=4 * HOUR)
        self.assertEqual(result["spot"][SPOT], [])
        self.assertFalse(fnew_data_status(basis_spec(), result, as_of_ms=4 * HOUR)["ready"])

    def test_closed_future_and_explicit_partial_rows_are_excluded(self):
        rows = [row(i * HOUR) for i in range(5)]
        rows[-2]["extras"]["closed"] = False
        store = Store({(market, pair, "1h"): rows
                       for market, pair in [("futures", PERP), ("spot", SPOT)]})
        result = load_fnew_candles(store, basis_spec(), limit=5, as_of_ms=4 * HOUR)
        self.assertEqual([item["timestamp"] for item in result[PERP]], [0, HOUR, 2 * HOUR])
        self.assertFalse(fnew_data_status(basis_spec(), result, as_of_ms=4 * HOUR)["ready"])

    def test_current_partial_does_not_displace_last_closed_warmup(self):
        rows = [row(i * HOUR) for i in range(4)] + [row(4 * HOUR, closed=False)]
        store = Store({(market, pair, "1h"): rows
                       for market, pair in [("futures", PERP), ("spot", SPOT)]})
        result = load_fnew_candles(store, basis_spec(), limit=3, as_of_ms=4 * HOUR + 20_000)
        self.assertEqual(len(result[PERP]), 3)
        self.assertTrue(fnew_data_status(basis_spec(), result, as_of_ms=4 * HOUR + 20_000)["ready"])

    def test_spot_gap_breaks_signal_warmup_without_shortening_perpetual_risk_input(self):
        store = Store({("futures", PERP, "1h"): [row(i * HOUR) for i in range(6)],
                       ("spot", SPOT, "1h"): [row(i * HOUR) for i in (0, 1, 3, 4, 5)]})
        result = load_fnew_candles(store, basis_spec(), limit=6, as_of_ms=6 * HOUR)
        self.assertEqual([item["timestamp"] for item in result[PERP]],
                         [i * HOUR for i in range(6)])
        self.assertEqual([item["timestamp"] for item in result["spot"][SPOT]],
                         [3 * HOUR, 4 * HOUR, 5 * HOUR])
        self.assertFalse(fnew_data_status(basis_spec(), result, as_of_ms=6 * HOUR,
                                         minimum=4)["ready"])
        self.assertTrue(fnew_data_status(basis_spec(), result, as_of_ms=6 * HOUR,
                                        minimum=3)["ready"])

    def test_complete_source_buckets_can_extend_real_spot_history(self):
        direct = [row(i * HOUR, 80) for i in range(3)]
        source = [row(i * 300_000, 90, quote_volume=9) for i in range(3 * 12, 4 * 12)]
        store = Store({("futures", PERP, "1h"): [row(i * HOUR) for i in range(4)],
                       ("spot", SPOT, "1h"): direct, ("spot", SPOT, "5m"): source})
        result = load_fnew_candles(store, basis_spec(), limit=3, as_of_ms=4 * HOUR)
        self.assertEqual(result["spot"][SPOT][-1]["close"], 90)
        self.assertEqual(result["spot"][SPOT][-1]["extras"]["quote_volume"], 108)
        self.assertTrue(fnew_data_status(basis_spec(), result, as_of_ms=4 * HOUR)["ready"])

    def test_incomplete_source_does_not_fill_missing_spot_hour(self):
        source = [row(i * 300_000, 90) for i in range(3 * 12, 4 * 12) if i != 40]
        store = Store({("futures", PERP, "1h"): [row(i * HOUR) for i in range(4)],
                       ("spot", SPOT, "1h"): [row(i * HOUR) for i in range(3)],
                       ("spot", SPOT, "5m"): source})
        result = load_fnew_candles(store, basis_spec(), limit=3, as_of_ms=4 * HOUR)
        self.assertEqual(result["spot"][SPOT][-1]["timestamp"], 2 * HOUR)

    def test_market_feed_contract_is_not_silently_repaired(self):
        for mutate in [lambda value: value.pop("data_feeds"),
                       lambda value: value["data_feeds"]["spot"].update(market="futures"),
                       lambda value: value["data_feeds"]["spot"]["pair_map"].update({PERP: PERP}),
                       lambda value: value["data_feeds"]["futures"].update(pairs=["SOL/USDT:USDT"]),
                       lambda value: value["data_feeds"]["spot"].update(timeframe="5m")]:
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                spec = deepcopy(basis_spec())
                mutate(spec)
                load_fnew_candles(Store({}), spec)

    def test_regular_fnew_retains_quote_volume_for_vwap(self):
        store = Store({("futures", PERP, "15m"): [row(i * 900_000, quote_volume=321)
                                                    for i in range(4)]})
        spec = {"strategy_id": "s34_vwap_anchor_reversion_15m", "timeframe": "15m",
                "universe": [PERP], "min_warmup_bars": 3}
        result = load_fnew_candles(store, spec, limit=3, as_of_ms=4 * 900_000)
        self.assertEqual(result[PERP][-1]["extras"]["quote_volume"], 321)
        self.assertNotIn("spot", result)
        self.assertTrue(fnew_data_status(spec, result, as_of_ms=4 * 900_000)["ready"])

    def test_short_current_spot_tail_does_not_disable_existing_perpetual_atr_stop(self):
        from strategy_fnew import load_fnew_specs
        from strategy_fnew_signals import evaluate_fnew
        spec = next(item for item in load_fnew_specs()
                    if item["canonical_strategy_id"] == "s33_basis_momentum_1h"
                    and item["max_loss_per_trade_usdt"] == 50
                    and item["params"]["risk_stop_atr_mult"] == 1.5)
        pairs = spec["execution_universe"]
        datasets = {}
        for pair in pairs:
            datasets[("futures", pair, "1h")] = [row(i * HOUR, 90, quote_volume=90)
                                                 for i in range(200)]
            datasets[("spot", PAIR_MAP[pair], "1h")] = [row(i * HOUR, 90, quote_volume=90)
                                                       for i in range(197, 200)]
        result = load_fnew_candles(Store(datasets), spec, as_of_ms=200 * HOUR)
        pair = pairs[0]
        positions = {pair: {"side": 1, "entry_price": 100, "entry_atr": 1,
                            "opened_candle_ms": 195 * HOUR}}
        evaluation = evaluate_fnew(spec, result, positions, as_of_utc=200 * HOUR)
        self.assertEqual(len(result[pair]), 200)
        self.assertFalse(fnew_data_status(spec, result, as_of_ms=200 * HOUR)["ready"])
        signal = evaluation["signals"][pair]
        self.assertEqual(signal["target_side"], 0)
        self.assertEqual(signal["reason"], "stop_loss_close_confirmed")
        self.assertEqual(signal["evidence"]["stop_level"], 98.5)


if __name__ == "__main__":
    unittest.main()

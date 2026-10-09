"""Verify entry ranking with real Freqtrade hooks; no trading process is started."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

try:
    import pandas as pd
    from freqtrade.enums import RunMode
    from freqtrade.strategy import IStrategy
    from freqtrade.strategy.strategy_wrapper import strategy_safe_wrapper
except ImportError:
    # The lightweight backend CI does not require the optional trading engine.
    RunMode = None


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 9, 8, 30, tzinfo=timezone.utc)
CANDLE = datetime(2026, 10, 8, tzinfo=timezone.utc)
PAIR = "BTC/USDT:USDT"
OTHER = "ETH/USDT:USDT"
LOGGER = "freqtrade.loggers.TrendFollowing"


class FakeDP:
    """Model the already-analyzed provider boundary, including delayed symbols."""

    def __init__(self, frames, runmode, whitelist=None):
        self.frames = frames
        self.runmode = runmode
        self.whitelist = list(frames) if whitelist is None else list(whitelist)
        self.reads = []

    def current_whitelist(self):
        return list(self.whitelist)

    def get_analyzed_dataframe(self, pair, timeframe):
        self.reads.append((pair, timeframe))
        frame = self.frames.get(pair)
        if isinstance(frame, Exception):
            raise frame
        return frame, NOW


@unittest.skipUnless(RunMode is not None, "optional Freqtrade dependencies unavailable")
class TrendFollowingEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "quant_trend_following_test", ROOT / "bot/user_data/strategies/TrendFollowing.py"
        )
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        self.clock = self.enterContext(patch.object(self.module, "monotonic", return_value=100.0))
        self.strategy = self.make_strategy()
        # Tests other than the startup boundary run after the 30-second warmup.
        self.clock.return_value = 130.0

    @staticmethod
    def frame(strength=0.5, state=1, date=CANDLE):
        return pd.DataFrame({
            "date": [pd.Timestamp(date)],
            "trend_state": [state],
            "break_strength": [strength],
        })

    def make_strategy(self, mode=None, frames=None, whitelist=None):
        mode = RunMode.DRY_RUN if mode is None else mode
        strategy = self.module.TrendFollowing({"runmode": mode})
        self.assertIsInstance(strategy, IStrategy)
        strategy.dp = FakeDP(
            {PAIR: self.frame()} if frames is None else frames, mode, whitelist
        )
        strategy.bot_start()
        return strategy

    def confirm(self, pair=PAIR, side="long", current_time=NOW, strategy=None):
        strategy = self.strategy if strategy is None else strategy
        return strategy.confirm_trade_entry(
            pair=pair,
            order_type="limit",
            amount=1.0,
            rate=100.0,
            time_in_force="GTC",
            current_time=current_time,
            entry_tag="trend_up" if side == "long" else "trend_dn",
            side=side,
        )

    @staticmethod
    @contextmanager
    def parameter_value(parameter, value):
        # Restore Freqtrade's .value property via its setter; it has no deleter.
        original = parameter.value
        parameter.value = value
        try:
            yield
        finally:
            parameter.value = original

    def test_original_empty_strengths_bypass_is_closed(self):
        with patch.object(self.strategy, "_current_strengths", return_value={}):
            self.assertIs(self.confirm(), False)

    def test_no_whitelist_denies_entry(self):
        self.strategy.dp.whitelist = []
        self.assertIs(self.confirm(), False)
        self.assertFalse(self.strategy._strength_snapshot["ready"])

    def test_missing_frame_cannot_create_partial_ranking_and_recovers_same_candle(self):
        self.strategy.dp.whitelist.append(OTHER)
        self.strategy.dp.frames[OTHER] = None
        self.assertIs(self.confirm(), False)
        self.assertFalse(self.strategy._strength_snapshot["ready"])
        self.assertIn(OTHER, self.strategy._strength_snapshot["missing"])
        # A failed snapshot must not cache BTC's RangeIndex/date as a valid ranking.
        self.strategy.dp.frames[OTHER] = self.frame(strength=0.2)
        self.assertIs(self.confirm(), True)
        self.assertTrue(self.strategy._strength_snapshot["ready"])
        self.assertEqual(set(self.strategy._current_strengths()), {PAIR, OTHER})

    def test_provider_failure_denies_entry_and_recovers(self):
        self.strategy.dp.frames[PAIR] = RuntimeError("analyzed data not ready")
        self.assertIs(self.confirm(), False)
        self.assertFalse(self.strategy._strength_snapshot["ready"])
        self.strategy.dp.frames[PAIR] = self.frame()
        self.assertIs(self.confirm(), True)

    def test_empty_dataframe_denies_entry(self):
        self.strategy.dp.frames[PAIR] = self.frame().iloc[:0]
        self.assertIs(self.confirm(), False)
        self.assertFalse(self.strategy._strength_snapshot["ready"])

    def test_required_columns_missing_fail_closed(self):
        for column in ("date", "trend_state", "break_strength"):
            with self.subTest(column=column):
                self.strategy.bot_loop_start(current_time=NOW)
                self.strategy.dp.frames[PAIR] = self.frame().drop(columns=[column])
                self.assertIs(self.confirm(), False)
                self.assertFalse(self.strategy._strength_snapshot["ready"])
                self.strategy.dp.frames[PAIR] = self.frame()
                self.assertIs(self.confirm(), True)

    def test_same_loop_cache_cannot_hide_missing_first_pair_columns(self):
        for column in ("date", "trend_state", "break_strength"):
            with self.subTest(column=column):
                self.strategy.dp.frames[PAIR] = self.frame()
                self.assertIs(self.confirm(), True)
                self.assertTrue(self.strategy._strength_snapshot["ready"])
                # Change the provider row after a valid cache hit, without a new loop.
                self.strategy.dp.frames[PAIR] = self.frame().drop(columns=[column])
                self.assertIs(self.confirm(), False)
                self.assertFalse(self.strategy._strength_snapshot["ready"])
                self.assertEqual(self.strategy._strength_snapshot["strengths"], {})

    def test_same_loop_cache_cannot_hide_invalid_first_pair_values(self):
        for column, value in (
            ("trend_state", 2), ("trend_state", float("nan")), ("trend_state", "bad"),
            ("break_strength", float("nan")), ("break_strength", float("inf")),
            ("break_strength", "bad"),
        ):
            with self.subTest(column=column, value=value):
                self.strategy.dp.frames[PAIR] = self.frame()
                self.assertIs(self.confirm(), True)
                changed = self.frame()
                changed[column] = [value]
                self.strategy.dp.frames[PAIR] = changed
                self.assertIs(self.confirm(), False)
                self.assertFalse(self.strategy._strength_snapshot["ready"])
                self.assertEqual(self.strategy._strength_snapshot["strengths"], {})

    def test_same_loop_valid_first_pair_state_changes_refresh_direction(self):
        self.assertIs(self.confirm(), True)
        self.strategy.dp.frames[PAIR] = self.frame(state=-1)
        self.assertIs(self.confirm(side="long"), False)
        self.assertIs(self.confirm(side="short"), True)
        self.assertEqual(self.strategy._strength_snapshot["states"][PAIR], -1.0)
        self.strategy.dp.frames[PAIR] = self.frame(state=0)
        self.assertIs(self.confirm(side="short"), False)
        self.assertTrue(self.strategy._strength_snapshot["ready"])
        self.assertEqual(self.strategy._strength_snapshot["strengths"], {})
        self.strategy.dp.frames[PAIR] = self.frame(state=1)
        self.assertIs(self.confirm(side="long"), True)

    def test_same_loop_valid_first_pair_strength_changes_refresh_top_n(self):
        pairs = [PAIR] + [f"COIN{i:02}/USDT:USDT" for i in range(8)]
        self.strategy.dp.whitelist = pairs
        self.strategy.dp.frames = {
            pair: self.frame(strength=9 - i) for i, pair in enumerate(pairs)
        }
        self.assertIs(self.confirm(), True)
        self.strategy.dp.frames[PAIR] = self.frame(strength=0.1)
        self.assertIs(self.confirm(), False)
        self.assertTrue(self.strategy._strength_snapshot["ready"])
        self.assertEqual(self.strategy._strength_snapshot["strengths"][PAIR], 0.1)
        self.strategy.dp.frames[PAIR] = self.frame(strength=9)
        self.assertIs(self.confirm(), True)
        self.assertEqual(self.strategy._strength_snapshot["strengths"][PAIR], 9.0)

    def test_invalid_active_strengths_fail_closed(self):
        for value in (None, float("nan"), float("inf"), float("-inf"), "bad"):
            with self.subTest(value=value):
                self.strategy.bot_loop_start(current_time=NOW)
                self.strategy.dp.frames[PAIR] = self.frame(strength=value)
                self.assertIs(self.confirm(), False)
                self.assertFalse(self.strategy._strength_snapshot["ready"])

    def test_invalid_trend_states_fail_closed(self):
        for value in (None, float("nan"), float("inf"), "bad", 2):
            with self.subTest(value=value):
                self.strategy.bot_loop_start(current_time=NOW)
                self.strategy.dp.frames[PAIR] = self.frame(state=value)
                self.assertIs(self.confirm(), False)
                self.assertFalse(self.strategy._strength_snapshot["ready"])

    def test_stale_unclosed_or_invalid_daily_dates_fail_closed(self):
        for date in (CANDLE - timedelta(days=1), CANDLE + timedelta(days=1), pd.NaT, "bad"):
            with self.subTest(date=date):
                self.strategy.bot_loop_start(current_time=NOW)
                frame = self.frame()
                frame["date"] = [date]
                self.strategy.dp.frames[PAIR] = frame
                self.assertIs(self.confirm(), False)
                self.assertFalse(self.strategy._strength_snapshot["ready"])

    def test_mixed_daily_dates_fail_closed_and_recovery_is_not_cached_out(self):
        self.strategy.dp.whitelist.append(OTHER)
        self.strategy.dp.frames[OTHER] = self.frame(date=CANDLE - timedelta(days=1))
        self.assertIs(self.confirm(), False)
        self.strategy.dp.frames[OTHER] = self.frame(strength=0.2)
        self.assertIs(self.confirm(), True)

    def test_range_index_reuse_does_not_hide_a_new_daily_candle(self):
        self.assertIs(self.confirm(), True)
        next_day = NOW + timedelta(days=1)
        self.strategy.dp.frames[PAIR] = self.frame(
            state=0, date=CANDLE + timedelta(days=1)
        )
        # Both frames have RangeIndex([0]); the actual UTC date must be the key.
        self.assertIs(self.confirm(current_time=next_day), False)
        self.assertTrue(self.strategy._strength_snapshot["ready"])
        self.assertEqual(self.strategy._strength_snapshot["strengths"], {})

    def test_utc_day_boundary_does_not_reuse_a_previously_valid_stale_cache(self):
        self.assertIs(self.confirm(), True)
        self.assertIs(self.confirm(current_time=NOW + timedelta(days=1)), False)
        self.assertFalse(self.strategy._strength_snapshot["ready"])

    def test_beijing_eight_am_is_the_utc_daily_boundary(self):
        beijing = timezone(timedelta(hours=8))
        before_close = datetime(2026, 10, 9, 7, 59, 59, tzinfo=beijing)
        just_closed = datetime(2026, 10, 9, 8, 0, tzinfo=beijing)
        self.assertIs(self.confirm(current_time=before_close), False)
        self.assertIs(self.confirm(current_time=just_closed), True)

    def test_whitelist_changes_recalculate_same_candle(self):
        self.assertIs(self.confirm(), True)
        self.strategy.dp.whitelist.append(OTHER)
        self.strategy.dp.frames[OTHER] = None
        self.assertIs(self.confirm(), False)
        self.strategy.dp.frames[OTHER] = self.frame()
        self.assertIs(self.confirm(pair=OTHER), True)

    def test_parameter_changes_recalculate_same_candle(self):
        for parameter in ("enter_period", "exit_period"):
            with self.subTest(parameter=parameter):
                self.strategy.bot_loop_start(current_time=NOW)
                self.strategy.dp.frames[PAIR] = self.frame()
                self.assertIs(self.confirm(), True)
                self.strategy.dp.frames[PAIR] = self.frame(state=0)
                value = getattr(self.strategy, parameter).value
                with self.parameter_value(getattr(self.strategy, parameter), value + 1):
                    self.assertIs(self.confirm(), False)
                    self.assertTrue(self.strategy._strength_snapshot["ready"])

    def test_new_bot_loop_refreshes_same_candle_snapshot(self):
        self.assertIs(self.confirm(), True)
        self.strategy.dp.frames[PAIR] = RuntimeError("provider temporarily unavailable")
        self.strategy.bot_loop_start(current_time=NOW)
        self.assertIs(self.confirm(), False)
        self.assertFalse(self.strategy._strength_snapshot["ready"])

    def test_complete_pool_without_trends_is_distinct_from_missing_data(self):
        self.strategy.dp.frames[PAIR] = self.frame(state=0)
        self.assertIs(self.confirm(), False)
        snapshot = self.strategy._strength_snapshot
        self.assertTrue(snapshot["ready"])
        self.assertEqual(snapshot["strengths"], {})
        self.assertFalse(snapshot["missing"])
        no_trend_reason = snapshot["reason"]
        self.strategy.bot_loop_start(current_time=NOW)
        self.strategy.dp.frames[PAIR] = None
        self.assertIs(self.confirm(), False)
        self.assertFalse(self.strategy._strength_snapshot["ready"])
        self.assertNotEqual(no_trend_reason, self.strategy._strength_snapshot["reason"])

    def test_top_n_boundary_uses_strengths_and_never_admits_unranked_pair(self):
        pairs = [f"COIN{i:02}/USDT:USDT" for i in range(10)]
        self.strategy.dp.whitelist = pairs
        self.strategy.dp.frames = {
            pair: self.frame(strength=10 - i) for i, pair in enumerate(pairs)
        }
        for i, pair in enumerate(pairs):
            with self.subTest(rank=i + 1):
                self.assertIs(self.confirm(pair=pair), i < 8)
        self.assertIs(self.confirm(pair="UNRANKED/USDT:USDT"), False)
        with self.parameter_value(self.strategy.top_n, 3):
            self.assertIs(self.confirm(pair=pairs[2]), True)
            self.assertIs(self.confirm(pair=pairs[3]), False)

    def test_equal_strength_ranking_is_stable_at_top_n_boundary(self):
        pairs = [f"COIN{i:02}/USDT:USDT" for i in range(10)]
        self.strategy.dp.whitelist = list(pairs)
        self.strategy.dp.frames = {pair: self.frame() for pair in pairs}
        first = [pair for pair in pairs if self.confirm(pair=pair)]
        self.strategy.bot_loop_start(current_time=NOW)
        self.strategy.dp.whitelist.reverse()
        second = [pair for pair in pairs if self.confirm(pair=pair)]
        self.assertEqual(first, pairs[:8])
        self.assertEqual(second, first)

    def test_fewer_candidates_only_admits_existing_ranked_pair(self):
        self.assertIs(self.confirm(), True)
        self.assertIs(self.confirm(pair=OTHER), False)

    def test_side_must_match_snapshot_trend_direction(self):
        for state, allowed_side, rejected_side in ((1, "long", "short"), (-1, "short", "long")):
            with self.subTest(state=state):
                self.strategy.bot_loop_start(current_time=NOW)
                self.strategy.dp.frames[PAIR] = self.frame(state=state)
                self.assertIs(self.confirm(side=allowed_side), True)
                self.assertIs(self.confirm(side=rejected_side), False)
                self.assertIs(self.confirm(side="unknown"), False)

    def test_unexpected_ranking_error_cannot_trigger_freqtrade_default_true(self):
        wrapped = strategy_safe_wrapper(
            self.strategy.confirm_trade_entry, default_retval=True
        )
        for error in (ValueError("bad snapshot"), RuntimeError("provider exploded")):
            with self.subTest(error=error):
                with patch.object(self.strategy, "_current_strengths", side_effect=error):
                    self.assertIs(wrapped(
                        pair=PAIR, order_type="limit", amount=1.0, rate=100.0,
                        time_in_force="GTC", current_time=NOW, entry_tag="trend_up", side="long"
                    ), False)

    def test_startup_guard_in_dry_run_and_live_before_and_at_deadline(self):
        for mode in (RunMode.DRY_RUN, RunMode.LIVE):
            with self.subTest(mode=mode):
                self.clock.return_value = 100.0
                strategy = self.make_strategy(mode=mode)
                self.clock.return_value = 129.999
                self.assertIs(self.confirm(strategy=strategy), False)
                self.clock.return_value = 130.0
                self.assertIs(self.confirm(strategy=strategy), True)

    def test_warmup_elapsed_does_not_bypass_unready_data(self):
        self.clock.return_value = 10000.0
        self.strategy.dp.frames[PAIR] = None
        self.assertIs(self.confirm(), False)

    def test_backtest_and_hyperopt_do_not_wait_or_require_live_pool_synchrony(self):
        for mode in (RunMode.BACKTEST, RunMode.HYPEROPT):
            with self.subTest(mode=mode):
                self.clock.return_value = 100.0
                strategy = self.make_strategy(
                    mode=mode,
                    frames={PAIR: self.frame(date=CANDLE - timedelta(days=20)), OTHER: None},
                )
                # Historical DP supplies a time-sliced subset, unlike a live startup.
                self.assertIs(self.confirm(strategy=strategy), True)

    def test_invalid_first_pair_does_not_discard_valid_historical_slice(self):
        for mode in (RunMode.BACKTEST, RunMode.HYPEROPT, RunMode.DRY_RUN, RunMode.LIVE):
            for column in ("date", "trend_state", "break_strength"):
                for value in (None, "bad"):
                    with self.subTest(mode=mode, column=column, value=value):
                        changed = self.frame()
                        changed[column] = [value]
                        self.clock.return_value = 100.0
                        strategy = self.make_strategy(
                            mode=mode,
                            frames={PAIR: changed, OTHER: self.frame(strength=0.25)},
                        )
                        self.clock.return_value = 130.0
                        historical = mode in (RunMode.BACKTEST, RunMode.HYPEROPT)
                        self.assertIs(self.confirm(pair=OTHER, strategy=strategy), historical)
                        self.assertIn(PAIR, strategy._strength_snapshot["missing"])
                        self.assertIs(strategy._strength_snapshot["ready"], historical)

    def test_ranking_and_decision_logs_show_strength_rank_and_utc_candle(self):
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.assertIs(self.confirm(), True)
            self.assertIs(self.confirm(pair=OTHER), False)
        output = "\n".join(logs.output)
        for value in (PAIR, OTHER, "2026-10-08", "0.5", "rank=1", "top_n=8"):
            self.assertIn(value, output)
        self.assertIn("allowed=True", output)
        self.assertIn("allowed=False", output)
        self.assertIn("reason=", output)

    def test_empty_ranking_is_logged_with_data_readiness(self):
        self.strategy.dp.frames[PAIR] = None
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.assertIs(self.confirm(), False)
        output = "\n".join(logs.output)
        self.assertIn(PAIR, output)
        self.assertIn("ready=False", output)
        self.assertIn("strengths=[]", output)
        self.assertIn("count=0", output)
        self.assertIn("allowed=False", output)

    def test_default_strategy_parameters_remain_unchanged(self):
        self.assertEqual(self.strategy.enter_period.value, 20)
        self.assertEqual(self.strategy.exit_period.value, 20)
        self.assertEqual(self.strategy.top_n.value, 8)
        self.assertEqual(self.strategy.target_exposure.value, 0.30)


if __name__ == "__main__":
    unittest.main()

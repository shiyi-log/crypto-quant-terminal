"""100x paper execution: margin sizing, liquidation, and fee accounting."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from strategy_simulation import (BAR_MS, PAIRS, _mark, apply_closed_batch,
                                  execution_identity, initial_state)


def _rule_hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def _spec(strategy_id: str = "test-leverage") -> dict:
    return {
        "strategy_id": strategy_id,
        "label": strategy_id,
        "timeframe": "5m",
        "universe": list(PAIRS),
        "params": {"n": 3},
        "initial_capital": 10_000,
        "fee_rate_per_side": 0.0005,
        "position_budget": {
            "target_exposure": 0.30,
            "max_positions": 2,
            "max_per_coin": 1,
            "sizing": "剩余预算 / 剩余槽位",
            "leverage": 100,
        },
    }


def _bars(timestamp: int, *, open_price: float = 100.0, high: float | None = None,
          low: float | None = None, close: float | None = None,
          include_eth: bool = True) -> dict:
    high = max(open_price, close if close is not None else open_price) + 0.5 if high is None else high
    low = min(open_price, close if close is not None else open_price) - 0.5 if low is None else low
    close = open_price if close is None else close
    row = {
        "timestamp": timestamp,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": 10,
        "extras": {"closed": True, "live_event_timestamp": timestamp + BAR_MS},
    }
    return {pair: [copy.deepcopy(row)] for pair in (PAIRS if include_eth else PAIRS[:1])}


def _signal(spec: dict, timestamp: int, targets: tuple[int | None, int | None]) -> dict:
    return {
        "strategy_id": spec["strategy_id"],
        "rule_hash": _rule_hash(spec),
        "ready": True,
        "warmup": {"ready": True, "valid_bars": 400, "required_bars": 400},
        "signals": {
            pair: {
                "target_side": side,
                "reason": "test_signal",
                "candle_utc": timestamp,
                "evidence": {},
                "size_multiplier": 1,
            }
            for pair, side in zip(PAIRS, targets)
        },
        "group": None,
    }


class LeveragedTransitionTests(unittest.TestCase):
    def setUp(self):
        self.spec = _spec()
        self.patch = patch("strategy_simulation._rule_hash", side_effect=_rule_hash)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.state = initial_state(self.spec, BAR_MS)

    def step(self, timestamp: int, targets=(None, None), *, state=None, candles=None):
        candles = candles or _bars(timestamp)
        return apply_closed_batch(
            state or self.state,
            self.spec,
            candles,
            _signal(self.spec, timestamp, targets),
            timestamp + BAR_MS + 1000,
            BAR_MS,
        )

    def enter(self, targets=(1, None)):
        pending, _ = self.step(BAR_MS, targets)
        return self.step(2 * BAR_MS, state=pending, candles=_bars(2 * BAR_MS, open_price=100,
                                                                    high=100.5, low=99.5))

    def test_100x_entry_keeps_margin_budget_but_scales_notional(self):
        opened = self.enter((1, -1))[0]
        self.assertEqual(len(opened["positions"]), 2)
        for position in opened["positions"].values():
            with self.subTest(pair=position["pair"]):
                self.assertAlmostEqual(position["quantity"] * position["entry_price"],
                                       position["margin"] * 100)
                self.assertAlmostEqual(position["notional"], position["margin"] * 100)
                self.assertEqual(position["leverage"], 100)
        # Convention A keeps posted margin at 30% of starting equity. Fees are
        # a separate cash cost charged on the leveraged notional.
        self.assertLessEqual(sum(p["margin"] for p in opened["positions"].values()),
                             10_000 * 0.30 + 1e-8)
        self.assertGreater(sum(p["margin"] for p in opened["positions"].values()), 0)
        self.assertEqual(opened["fees_paid"],
                         sum(p["entry_fee"] for p in opened["positions"].values()))
        self.assertGreater(_mark(opened)["gross_notional"], 10_000)

    def test_aggregate_risk_cap_allocates_by_signal_weight(self):
        self.spec = dict(self.spec, strategy_id="test-leverage-aggregate-weighted",
                         aggregate_risk_cap=True)
        state = initial_state(self.spec, BAR_MS)
        weighted = _signal(self.spec, BAR_MS, (1, -1))
        weighted["signals"][PAIRS[0]]["size_multiplier"] = 0.25
        weighted["signals"][PAIRS[1]]["size_multiplier"] = 1.0
        pending, _ = apply_closed_batch(
            state, self.spec, _bars(BAR_MS), weighted,
            2 * BAR_MS + 1000, BAR_MS)
        weighted = _signal(self.spec, 2 * BAR_MS, (None, None))
        opened, _ = apply_closed_batch(
            pending, self.spec, _bars(2 * BAR_MS), weighted,
            3 * BAR_MS + 1000, BAR_MS)

        identity = execution_identity(self.spec)["execution"]
        planned_loss = sum(
            position["notional"] * (identity["risk_stop_pct"] +
                                     identity["risk_round_trip_fee"])
            for position in opened["positions"].values()
        )
        self.assertAlmostEqual(planned_loss,
                               identity["max_loss_per_trade_usdt"], places=8)
        self.assertLess(opened["positions"][PAIRS[0]]["notional"],
                        opened["positions"][PAIRS[1]]["notional"])

    def test_aggregate_risk_cap_rejects_heterogeneous_stop_modes(self):
        for mode, params in (("entry_atr", {"risk_stop_atr_mult": 1.5}),
                             ("structural", {})):
            with self.subTest(mode=mode):
                definition = dict(self.spec, strategy_id=f"test-aggregate-{mode}",
                                  aggregate_risk_cap=True,
                                  risk_sizing_mode=mode, params=params)
                with self.assertRaisesRegex(ValueError, "supports only fixed-stop"):
                    execution_identity(definition)

    def test_long_low_triggers_liquidation_at_bankruptcy_price(self):
        opened, _ = self.enter((1, None))
        position = opened["positions"][PAIRS[0]]
        liq = position["entry_price"] * 0.99
        liquidated, events = self.step(
            3 * BAR_MS,
            state=opened,
            candles=_bars(3 * BAR_MS, open_price=100, high=100.2, low=liq, close=100),
        )
        self.assertNotIn(PAIRS[0], liquidated["positions"])
        self.assertEqual(liquidated["closed_trade_count"], 1)
        trade = next(e["payload"] for e in events if e["event_type"] == "trade_closed")
        self.assertEqual(trade["exit_reason"], "liquidation")
        self.assertAlmostEqual(trade["exit_price"], liq)
        self.assertAlmostEqual(trade["gross_pnl"], -position["margin"], places=6)
        self.assertAlmostEqual(trade["fees"], position["entry_fee"] +
                               position["quantity"] * liq * self.spec["fee_rate_per_side"], places=6)
        self.assertAlmostEqual(_mark(liquidated)["equity"], liquidated["cash"], places=6)

    def test_short_high_triggers_liquidation_and_close_is_not_fill_proxy(self):
        opened, _ = self.enter((-1, None))
        position = opened["positions"][PAIRS[0]]
        liq = position["entry_price"] * 1.01
        liquidated, events = self.step(
            3 * BAR_MS,
            state=opened,
            candles=_bars(3 * BAR_MS, open_price=100, high=liq, low=99.8, close=100),
        )
        self.assertNotIn(PAIRS[0], liquidated["positions"])
        trade = next(e["payload"] for e in events if e["event_type"] == "trade_closed")
        self.assertEqual(trade["exit_reason"], "liquidation")
        self.assertAlmostEqual(trade["exit_price"], liq)
        self.assertAlmostEqual(trade["gross_pnl"], -position["margin"], places=6)

    def test_intrabar_breach_is_required_before_liquidation(self):
        opened, _ = self.enter((1, None))
        position = opened["positions"][PAIRS[0]]
        liq = position["liquidation_price"]
        observed, events = self.step(
            3 * BAR_MS,
            state=opened,
            candles=_bars(3 * BAR_MS, open_price=100, high=100.2,
                          low=liq + 0.01, close=100),
        )
        self.assertIn(PAIRS[0], observed["positions"])
        self.assertFalse(any(e["event_type"] == "liquidation" for e in events))

    def test_maker_fee_is_part_of_new_execution_identity(self):
        maker = copy.deepcopy(self.spec)
        maker["strategy_id"] = "test-leverage_100x_maker"
        maker["fee_rate_per_side"] = 0.0002
        taker = copy.deepcopy(maker)
        taker["strategy_id"] = "test-leverage_100x"
        taker["fee_rate_per_side"] = 0.0005
        self.assertNotEqual(execution_identity(maker)["execution_hash"],
                            execution_identity(taker)["execution_hash"])

    def test_intrabar_stop_variant_is_opt_in_and_preserves_legacy_hash(self):
        legacy = execution_identity(self.spec)
        variant = copy.deepcopy(self.spec)
        variant["strategy_id"] = "test-leverage_100x_intrabar"
        variant["stop_execution_mode"] = "intrabar"
        identity = execution_identity(variant)
        self.assertNotEqual(identity["execution_hash"], legacy["execution_hash"])
        self.assertEqual(legacy["execution"].get("stop_execution_mode"), None)
        self.assertEqual(identity["execution"]["stop_execution_mode"], "intrabar")
        self.assertEqual(identity["execution"]["stop_policy"],
                         "fixed_pct_intrabar_ohlc_stop_first_open_gap")

    def test_intrabar_long_stop_uses_low_and_stop_level(self):
        self.spec["stop_execution_mode"] = "intrabar"
        state = initial_state(self.spec, BAR_MS)
        pending, _ = self.step(BAR_MS, (1, None), state=state)
        opened, _ = self.step(2 * BAR_MS, state=pending,
                               candles=_bars(2 * BAR_MS, open_price=100,
                                             high=100.5, low=99.6, close=100))
        position = opened["positions"][PAIRS[0]]
        self.assertAlmostEqual(position["stop_level"], 99.5)
        stopped, events = self.step(3 * BAR_MS, state=opened,
                                    candles=_bars(3 * BAR_MS, open_price=100,
                                                  high=101, low=99.5, close=100))
        self.assertNotIn(PAIRS[0], stopped["positions"])
        trade = next(item["payload"] for item in events if item["event_type"] == "trade_closed")
        self.assertEqual(trade["exit_reason"], "stop_loss_intrabar")
        self.assertEqual(trade["exit_price"], 99.5)
        filled = next(item["payload"] for item in events if item["event_type"] == "order_filled")
        self.assertEqual(filled["fill_model"], "intrabar_stop_level")

    def test_intrabar_gap_fills_at_open_and_stop_precedes_signal_exit(self):
        self.spec["stop_execution_mode"] = "intrabar"
        pending, _ = self.step(BAR_MS, (1, None), state=initial_state(self.spec, BAR_MS))
        opened, _ = self.step(2 * BAR_MS, state=pending,
                               candles=_bars(2 * BAR_MS, open_price=100,
                                             high=100.5, low=99.6, close=100))
        stopped, events = self.step(3 * BAR_MS, (0, None), state=opened,
                                    candles=_bars(3 * BAR_MS, open_price=99,
                                                  high=100, low=98.5, close=99))
        self.assertNotIn(PAIRS[0], stopped["positions"])
        trades = [item["payload"] for item in events if item["event_type"] == "trade_closed"]
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "stop_loss_intrabar")
        self.assertEqual(trades[0]["exit_price"], 99)
        filled = next(item["payload"] for item in events if item["event_type"] == "order_filled")
        self.assertEqual(filled["fill_model"], "execution_open_stop_gap")

    def test_intrabar_stop_applies_to_entry_candle_before_liquidation(self):
        self.spec["stop_execution_mode"] = "intrabar"
        pending, _ = self.step(BAR_MS, (1, None), state=initial_state(self.spec, BAR_MS))
        # The entry fills at this candle's open.  Its low touches the stop but
        # stays above the 100x liquidation price; the stop must close it first.
        stopped, events = self.step(
            2 * BAR_MS,
            state=pending,
            candles=_bars(2 * BAR_MS, open_price=100, high=100.5, low=99.4, close=100),
        )
        self.assertNotIn(PAIRS[0], stopped["positions"])
        self.assertFalse(any(item["event_type"] == "liquidation" for item in events))
        trade = next(item["payload"] for item in events if item["event_type"] == "trade_closed")
        self.assertEqual(trade["exit_reason"], "stop_loss_intrabar")
        self.assertEqual(trade["exit_price"], 99.5)

    def test_legacy_close_confirmed_variant_ignores_intrabar_stop_touch(self):
        # A low below the proposed fixed stop must not alter the historical
        # close-confirmed execution contract when the opt-in mode is absent.
        pending, _ = self.step(BAR_MS, (1, None), state=initial_state(self.spec, BAR_MS))
        observed, events = self.step(
            2 * BAR_MS,
            state=pending,
            candles=_bars(2 * BAR_MS, open_price=100, high=100.5, low=99.4, close=100),
        )
        self.assertIn(PAIRS[0], observed["positions"])
        self.assertFalse(any(item["event_type"] == "trade_closed" for item in events))

    def test_intrabar_stop_cancels_queued_close_without_double_closing(self):
        self.spec["stop_execution_mode"] = "intrabar"
        pending, _ = self.step(BAR_MS, (1, None), state=initial_state(self.spec, BAR_MS))
        opened, _ = self.step(2 * BAR_MS, state=pending,
                               candles=_bars(2 * BAR_MS, open_price=100,
                                             high=100.5, low=99.6, close=100))
        exit_pending, _ = self.step(3 * BAR_MS, (0, None), state=opened,
                                     candles=_bars(3 * BAR_MS, open_price=100,
                                                   high=100.5, low=99.6, close=100))
        self.assertEqual(len(exit_pending["pending_orders"]), 1)
        stopped, events = self.step(4 * BAR_MS, state=exit_pending,
                                    candles=_bars(4 * BAR_MS, open_price=100,
                                                  high=100.5, low=99.5, close=100))
        self.assertNotIn(PAIRS[0], stopped["positions"])
        self.assertFalse(stopped["pending_orders"])
        self.assertEqual(sum(item["event_type"] == "trade_closed" for item in events), 1)
        self.assertEqual(sum(item["event_type"] == "order_cancelled" for item in events), 1)
        trade = next(item["payload"] for item in events if item["event_type"] == "trade_closed")
        self.assertEqual(trade["exit_reason"], "stop_loss_intrabar")


if __name__ == "__main__":
    unittest.main()

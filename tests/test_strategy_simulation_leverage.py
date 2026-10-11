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


if __name__ == "__main__":
    unittest.main()

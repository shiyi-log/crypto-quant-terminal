"""ATR execution sizing: frozen evidence, immutable policies and entry guards."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from unittest.mock import MagicMock, patch

from strategy_simulation import (PAIRS, SAMPLE_QUALITY_CUTOFF_MS, StrategySimulation, _mark,
                                 apply_closed_batch, execution_identity, initial_state,
                                 sample_annotation)


BAR = 900_000


def _rule_hash(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def _spec(*, risk=50, k=1.5, capital=10_000, fee=.0002, enabled=True):
    value = {"strategy_id": "test-atr-risk", "timeframe": "15m", "universe": list(PAIRS),
             "initial_capital": capital, "fee_rate_per_side": fee,
             "position_budget": {"leverage": 100},
             "params": {"atr_window": 14, "risk_stop_atr_mult": k},
             "risk_sizing_mode": "entry_atr", "max_loss_per_trade_usdt": risk}
    if not enabled:
        value.update(execution_enabled=False, blocked_execution_reason="test_policy_unavailable")
    return value


def _bars(timestamp, price=100):
    row = {"timestamp": timestamp, "open": price, "high": price + .1,
           "low": price - .1, "close": price, "volume": 10, "extras": {"closed": True}}
    return {pair: copy.deepcopy(row) for pair in PAIRS}


def _signal(spec, timestamp, targets=(None, None), *, evidence=None, atomic=False, weight=1):
    evidence = {"atr": .347} if evidence is None else evidence
    return {"strategy_id": spec["strategy_id"], "rule_hash": _rule_hash(spec), "ready": True,
            "warmup": {"ready": True, "valid_bars": 750, "required_bars": 16},
            "signals": {pair: {"target_side": side, "reason": "test_signal",
                               "candle_utc": timestamp, "evidence": copy.deepcopy(evidence),
                               "size_multiplier": weight}
                        for pair, side in zip(PAIRS, targets, strict=True)},
            "group": {"atomic": True, "legs": list(PAIRS)} if atomic else None}


class AtrExecutionTests(unittest.TestCase):
    def setUp(self):
        self.rule_patch = patch("strategy_simulation._rule_hash", side_effect=_rule_hash)
        self.rule_patch.start()
        self.addCleanup(self.rule_patch.stop)

    def step(self, spec, state, timestamp, targets=(None, None), *, evidence=None,
             atomic=False, weight=1, price=100):
        return apply_closed_batch(state, spec, _bars(timestamp, price),
                                  _signal(spec, timestamp, targets, evidence=evidence,
                                          atomic=atomic, weight=weight),
                                  timestamp + BAR + 1000, BAR)

    def enter(self, spec=None, *, atr=.347, direction=1, price=100, weight=1):
        spec = _spec() if spec is None else spec
        pending, _ = self.step(spec, initial_state(spec, BAR), BAR, (direction, None),
                               evidence={"atr": atr}, weight=weight)
        return self.step(spec, pending, 2 * BAR, evidence={"atr": 10}, price=price)

    def test_full_risk_and_stop_grid_sizes_from_frozen_atr(self):
        for risk in (50, 200, 1000):
            for k in (1, 1.5, 2.5):
                with self.subTest(risk=risk, k=k):
                    state, events = self.enter(_spec(risk=risk, k=k))
                    position = state["positions"][PAIRS[0]]
                    risk_notional = risk / (k * .347 / 100 + .0004)
                    expected = min(risk_notional, 150_000)
                    self.assertAlmostEqual(position["notional"], expected)
                    self.assertAlmostEqual(position["margin"], expected / 100)
                    self.assertEqual(position["entry_atr"], .347)
                    self.assertAlmostEqual(position["stop_level"], 100 - k * .347)
                    self.assertAlmostEqual(position["risk_sizing"]["risk_cap_notional"], risk_notional)
                    self.assertLessEqual(position["risk_sizing"]["planned_loss_usdt"], risk + 1e-8)
                    opened = next(event["payload"] for event in events
                                  if event["event_type"] == "position_opened")
                    filled = next(event["payload"] for event in events
                                  if event["event_type"] == "order_filled")
                    self.assertEqual(opened["risk_sizing"], filled["risk_sizing"])

    def test_gap_changes_percentage_and_stop_using_actual_fill_price(self):
        state, _ = self.enter(price=120)
        position = state["positions"][PAIRS[0]]
        self.assertEqual(position["entry_price"], 120)
        self.assertAlmostEqual(position["notional"], 50 / (1.5 * .347 / 120 + .0004))
        self.assertAlmostEqual(position["stop_level"], 120 - 1.5 * .347)
        self.assertEqual(position["risk_sizing"]["atr_signal_candle_ms"], BAR)

    def test_short_stop_is_above_fill_and_same_notional_as_long(self):
        long = self.enter()[0]["positions"][PAIRS[0]]
        short = self.enter(direction=-1)[0]["positions"][PAIRS[0]]
        self.assertAlmostEqual(long["notional"], short["notional"])
        self.assertAlmostEqual(short["stop_level"], 100 + 1.5 * .347)

    def test_new_fee_sensitivity_reserves_effective_roundtrip_fee(self):
        for fee in (.0002, .0005):
            with self.subTest(fee=fee):
                state, _ = self.enter(_spec(fee=fee))
                position = state["positions"][PAIRS[0]]
                self.assertAlmostEqual(position["notional"], 50 / (.005205 + 2 * fee))
                self.assertEqual(position["risk_sizing"]["round_trip_fee"], 2 * fee)

    def test_size_multiplier_scales_below_the_risk_budget(self):
        full = self.enter()[0]["positions"][PAIRS[0]]
        half = self.enter(weight=.5)[0]["positions"][PAIRS[0]]
        self.assertAlmostEqual(half["notional"], full["notional"] / 2)
        self.assertAlmostEqual(half["risk_sizing"]["planned_loss_usdt"], 25)
        zero, events = self.enter(weight=0)
        self.assertFalse(zero["positions"])
        self.assertTrue(any(event["payload"].get("reason") == "zero_sizing" for event in events))

    def test_missing_and_malformed_atr_deny_entry_without_raising(self):
        values = (None, True, 0, -1, float("nan"), float("inf"), "invalid", [], {},
                  {"nested": float("nan")})
        spec = _spec()
        for value in values:
            with self.subTest(value=value):
                state, events = self.step(spec, initial_state(spec, BAR), BAR, (1, None),
                                          evidence={"atr": value})
                self.assertFalse(state["positions"])
                self.assertFalse(state["pending_orders"])
                denied = next(event["payload"] for event in events if event["event_type"] == "entry_denied")
                self.assertEqual(denied["reason"], "entry_atr_unavailable_or_invalid")
                json.dumps(events, allow_nan=False)
        for evidence in ({}, [], "bad"):
            with self.subTest(evidence=evidence):
                state, _ = self.step(spec, initial_state(spec, BAR), BAR, (1, None), evidence=evidence)
                self.assertFalse(state["pending_orders"])

    def test_pending_invalid_atr_rejected_but_same_open_exit_still_fills(self):
        spec = _spec()
        opened, _ = self.enter(spec)
        pending, _ = self.step(spec, opened, 3 * BAR, (0, 1))
        for order in pending["pending_orders"]:
            if order["kind"] == "entry":
                order["evidence"] = {"atr": float("nan")}
        state, events = self.step(spec, pending, 4 * BAR)
        self.assertFalse(state["positions"])
        self.assertEqual(state["closed_trade_count"], 1)
        self.assertTrue(any(event["payload"].get("reason") == "entry_atr_unavailable_or_invalid"
                            for event in events if event["event_type"] == "order_rejected"))

    def test_nonpositive_or_overflowing_stop_rejects_pending_entry(self):
        for atr in (100, 1e308):
            with self.subTest(atr=atr):
                state, events = self.enter(atr=atr)
                self.assertFalse(state["positions"])
                self.assertTrue(any(event["payload"].get("reason") == "entry_atr_unavailable_or_invalid"
                                    for event in events if event["event_type"] == "order_rejected"))

    def test_distinct_pair_atrs_keep_individual_risk_caps(self):
        spec = _spec()
        signal = _signal(spec, BAR, (1, -1))
        signal["signals"][PAIRS[1]]["evidence"] = {"atr": .7}
        pending, _ = apply_closed_batch(initial_state(spec, BAR), spec, _bars(BAR),
                                        signal, 2 * BAR + 1000, BAR)
        state, _ = self.step(spec, pending, 2 * BAR)
        self.assertAlmostEqual(state["positions"][PAIRS[0]]["notional"], 50 / (.005205 + .0004))
        self.assertAlmostEqual(state["positions"][PAIRS[1]]["notional"], 50 / (.0105 + .0004))
        self.assertLessEqual(_mark(state)["margin_used"], 3000)

    def test_high_risk_respects_margin_slots_and_fee_cash_cap(self):
        spec = _spec(risk=1000)
        pending, _ = self.step(spec, initial_state(spec, BAR), BAR, (1, -1), evidence={"atr": .001})
        state, _ = self.step(spec, pending, 2 * BAR)
        self.assertEqual(len(state["positions"]), 2)
        self.assertLessEqual(_mark(state)["margin_used"], 3000)
        self.assertGreaterEqual(state["cash"], 0)
        for position in state["positions"].values():
            self.assertLessEqual(position["margin"], 1500)
            self.assertLessEqual(position["risk_sizing"]["planned_loss_usdt"], 1000)
        crowded = copy.deepcopy(state)
        order = copy.deepcopy(pending["pending_orders"][0])
        order.update(pair="TRX/USDT:USDT", expected_fill_candle_ms=3 * BAR,
                     signal_candle_ms=2 * BAR)
        crowded["pending_orders"] = [order]
        # A missing execution leg is cancelled before it can consume a third slot.
        state, events = self.step(spec, crowded, 3 * BAR)
        self.assertEqual(len(state["positions"]), 2)
        self.assertTrue(any(event["payload"].get("reason") == "fill_bar_missing" for event in events))
        expensive = _spec(risk=1000, fee=.009)
        state, _ = self.enter(expensive, atr=.001)
        self.assertGreaterEqual(state["cash"], 0)
        self.assertLessEqual(state["positions"][PAIRS[0]]["margin"] * 1.9, 10_000)

    def test_exit_releases_slot_before_new_atr_entry(self):
        spec = _spec(risk=1000)
        opened, _ = self.enter(spec, atr=.001)
        pending, _ = self.step(spec, opened, 3 * BAR, (0, -1), evidence={"atr": .001})
        state, events = self.step(spec, pending, 4 * BAR)
        self.assertNotIn(PAIRS[0], state["positions"])
        self.assertIn(PAIRS[1], state["positions"])
        types = [event["event_type"] for event in events]
        self.assertLess(types.index("trade_closed"), types.index("position_opened"))

    def test_closed_trade_keeps_frozen_risk_plan_and_exit_reason(self):
        spec = _spec()
        opened, _ = self.enter(spec)
        position = opened["positions"][PAIRS[0]]
        signal = _signal(spec, 3 * BAR, (0, None), evidence={"atr": 25})
        signal["signals"][PAIRS[0]]["reason"] = "stop_loss_close_confirmed"
        pending, _ = apply_closed_batch(opened, spec, _bars(3 * BAR), signal, 4 * BAR + 1000, BAR)
        state, events = self.step(spec, pending, 4 * BAR)
        trade = next(event["payload"] for event in events if event["event_type"] == "trade_closed")
        self.assertEqual(trade["entry_atr"], .347)
        self.assertEqual(trade["risk_sizing"], position["risk_sizing"])
        self.assertEqual(trade["exit_reason"], "stop_loss_close_confirmed")
        self.assertEqual(state["closed_trade_count"], 1)

    def test_open_bankruptcy_supersedes_pending_long_and_short_stops(self):
        spec = _spec()
        for direction, confirmed_close, next_open in ((1, 99.4, 98), (-1, 100.6, 102)):
            with self.subTest(direction=direction):
                opened, _ = self.enter(spec, direction=direction)
                position = opened["positions"][PAIRS[0]]
                signal = _signal(spec, 3 * BAR, (0, None))
                signal["signals"][PAIRS[0]]["reason"] = "stop_loss_close_confirmed"
                pending, _ = apply_closed_batch(opened, spec, _bars(3 * BAR, confirmed_close),
                                                signal, 4 * BAR + 1000, BAR)
                stop_order = pending["pending_orders"][0]
                state, events = self.step(spec, pending, 4 * BAR, price=next_open)
                trades = [event["payload"] for event in events if event["event_type"] == "trade_closed"]
                self.assertEqual(len(trades), 1)
                trade = trades[0]
                self.assertEqual(trade["exit_reason"], "liquidation")
                self.assertTrue(trade["liquidation"])
                self.assertEqual(trade["exit_price"], position["liquidation_price"])
                self.assertAlmostEqual(trade["gross_pnl"], -position["margin"])
                expected_fees = position["entry_fee"] + position["quantity"] * position["liquidation_price"] * .0002
                self.assertAlmostEqual(trade["net_pnl"], -position["margin"] - expected_fees)
                cancelled = next(event["payload"] for event in events if event["event_type"] == "order_cancelled")
                self.assertEqual(cancelled["paper_order_id"], stop_order["paper_order_id"])
                self.assertEqual(cancelled["reason"], "superseded_by_open_liquidation")
                self.assertEqual(cancelled["superseded_exit_reason"], "stop_loss_close_confirmed")
                self.assertEqual(cancelled["liquidated_trade_id"], position["trade_id"])
                self.assertEqual(state["closed_trade_count"], 1)
                self.assertFalse(state["positions"])
                filled = [event["payload"] for event in events if event["event_type"] == "order_filled"]
                self.assertEqual(len(filled), 1)
                self.assertEqual(filled[0]["fill_model"], "open_bankruptcy_price")
                self.assertEqual(filled[0]["liquidation_phase"], "next_open_bankruptcy")

    def test_open_bankruptcy_settles_held_position_before_new_entry_budget(self):
        spec = _spec(risk=1000)
        opened, _ = self.enter(spec, atr=.001)
        old = opened["positions"][PAIRS[0]]
        pending, _ = self.step(spec, opened, 3 * BAR, (0, 1), evidence={"atr": .001}, price=99.4)
        bars = _bars(4 * BAR, 98)
        bars[PAIRS[1]] = _bars(4 * BAR, 100)[PAIRS[1]]
        state, events = apply_closed_batch(pending, spec, bars, _signal(spec, 4 * BAR),
                                          5 * BAR + 1000, BAR)
        after_forced_equity = 10_000 - old["margin"] - old["entry_fee"] - old["quantity"] * 99 * .0002
        new = state["positions"][PAIRS[1]]
        self.assertAlmostEqual(new["margin"], after_forced_equity * .30 / 2)
        self.assertNotIn(PAIRS[0], state["positions"])
        types = [event["event_type"] for event in events]
        self.assertLess(types.index("trade_closed"), types.index("position_opened"))

    def test_open_bankruptcy_without_pending_exit_survives_gap_or_stale_data(self):
        spec = _spec()
        opened, _ = self.enter(spec)
        for timestamp, observed in ((3 * BAR, 4 * BAR + 90_001), (4 * BAR, 5 * BAR + 1000)):
            with self.subTest(timestamp=timestamp):
                state, events = apply_closed_batch(opened, spec, _bars(timestamp, 98),
                                                  _signal(spec, timestamp), observed, BAR)
                self.assertFalse(state["positions"])
                self.assertEqual(state["closed_trade_count"], 1)
                trade = next(event["payload"] for event in events if event["event_type"] == "trade_closed")
                self.assertEqual(trade["exit_reason"], "liquidation")
                self.assertEqual(trade["exit_price"], 99)

    def test_intrabar_bankruptcy_still_settles_new_and_held_positions(self):
        spec = _spec()
        pending, _ = self.step(spec, initial_state(spec, BAR), BAR, (1, None))
        held, _ = self.enter(spec)
        for timestamp, source in ((2 * BAR, pending), (3 * BAR, held)):
            with self.subTest(timestamp=timestamp):
                bars = _bars(timestamp)
                bars[PAIRS[0]]["low"] = 98
                state, events = apply_closed_batch(source, spec, bars, _signal(spec, timestamp),
                                                  timestamp + BAR + 1000, BAR)
                self.assertFalse(state["positions"])
                liquidation = next(event["payload"] for event in events if event["event_type"] == "liquidation")
                self.assertEqual(liquidation["liquidation_phase"], "intrabar_after_next_open_fills")
                trade = next(event["payload"] for event in events if event["event_type"] == "trade_closed")
                self.assertAlmostEqual(trade["gross_pnl"], -trade["margin"])

    def test_open_bankruptcy_precedence_is_part_of_new_execution_hash(self):
        from strategy_simulation import _hash

        spec = _spec()
        identity = execution_identity(spec)
        self.assertIn("open_bankruptcy_breach_policy", identity["execution"])
        prior_model = copy.deepcopy(identity["execution"])
        prior_model.pop("open_bankruptcy_breach_policy")
        prior_model["liquidation"] = {"enabled": True, "trigger": "closed_candle_low_high",
                                      "settlement": "bankruptcy_price_full_margin_loss"}
        state = initial_state(spec, BAR)
        state["execution_hash"] = _hash(prior_model)
        with self.assertRaisesRegex(ValueError, "new strategy identity"):
            self.step(spec, state, BAR)

    def test_atomic_atr_entries_explicitly_unsupported(self):
        spec = _spec()
        state, events = self.step(spec, initial_state(spec, BAR), BAR, (1, -1), atomic=True)
        self.assertFalse(state["pending_orders"])
        denied = [event["payload"] for event in events if event["event_type"] == "entry_denied"]
        self.assertEqual(len(denied), 2)
        self.assertTrue(all(event["reason"] == "atomic_atr_stop_policy_unsupported" for event in denied))

    def test_disabled_execution_blocks_entries_and_preserves_exits(self):
        spec = _spec(enabled=False)
        state, events = self.step(spec, initial_state(spec, BAR), BAR, (1, None))
        self.assertFalse(state["pending_orders"])
        denied = next(event["payload"] for event in events if event["event_type"] == "entry_denied")
        self.assertEqual(denied["reason"], "execution_disabled")
        self.assertEqual(denied["blocked_execution_reason"], "test_policy_unavailable")
        supported = _spec()
        opened, _ = self.enter(supported)
        # A disabled identity can retain a pre-existing exposure for safe wind-down.
        disabled = initial_state(spec, BAR)
        for key in ("positions", "cash", "fees_paid", "last_candle_ms", "last_candle_fingerprint"):
            disabled[key] = copy.deepcopy(opened[key])
        pending, _ = self.step(spec, disabled, 3 * BAR, (0, None))
        closed, _ = self.step(spec, pending, 4 * BAR)
        self.assertFalse(closed["positions"])
        self.assertEqual(closed["closed_trade_count"], 1)

    def test_disabled_and_atomic_guards_also_reject_already_pending_entries(self):
        supported = _spec()
        pending, _ = self.step(supported, initial_state(supported, BAR), BAR, (1, None))
        for disabled, atomic, reason in ((True, False, "execution_disabled"),
                                         (False, True, "atomic_atr_stop_policy_unsupported")):
            with self.subTest(disabled=disabled, atomic=atomic):
                spec = _spec(enabled=not disabled)
                state = copy.deepcopy(pending)
                identity = execution_identity(spec)
                state.update(rule_hash=identity["rule_hash"], execution_hash=identity["execution_hash"])
                state["pending_orders"][0]["atomic"] = atomic
                state["pending_orders"][0]["evidence"] = {"atr": float("nan")}
                rejected, events = self.step(spec, state, 2 * BAR)
                self.assertFalse(rejected["positions"])
                event = next(item["payload"] for item in events if item["event_type"] == "order_rejected")
                self.assertEqual(event["reason"], reason)
                json.dumps(events, allow_nan=False)

    def test_invalid_pending_atr_can_be_cancelled_on_stale_gap_or_restart(self):
        spec = _spec()
        pending, _ = self.step(spec, initial_state(spec, BAR), BAR, (1, None))
        pending["pending_orders"][0]["evidence"] = {"atr": float("nan")}
        cases = ((2 * BAR, 3 * BAR + 90_001, BAR, "stale_closed_bar"),
                 (3 * BAR, 4 * BAR + 1000, BAR, "gap_cancel"),
                 (2 * BAR, 3 * BAR + 1000, 2 * BAR, "restart_cancel"))
        for timestamp, observed, boundary, reason in cases:
            with self.subTest(reason=reason):
                state, events = apply_closed_batch(pending, spec, _bars(timestamp),
                                                  _signal(spec, timestamp), observed, boundary)
                self.assertFalse(state["positions"])
                cancelled = next(event["payload"] for event in events
                                 if event["event_type"] == "order_cancelled")
                self.assertEqual(cancelled["reason"], reason)
                json.dumps(events, allow_nan=False)

    def test_risk_budget_and_support_status_are_immutable_execution_identity(self):
        spec = _spec()
        state = initial_state(spec, BAR)
        baseline = execution_identity(spec)["execution_hash"]
        for changed in (_spec(risk=200), _spec(k=2.5), _spec(enabled=False)):
            with self.subTest(changed=changed):
                self.assertNotEqual(execution_identity(changed)["execution_hash"], baseline)
                with self.assertRaisesRegex(ValueError, "new strategy identity"):
                    self.step(changed, state, BAR)

    def test_invalid_policy_cannot_silently_fall_back_to_legacy_sizing(self):
        for changes in ({"risk_sizing_mode": "unknown"}, {"params": {}},
                        {"max_loss_per_trade_usdt": 0}, {"params": {"risk_stop_atr_mult": False}},
                        {"execution_enabled": False}, {"execution_enabled": "false"}):
            with self.subTest(changes=changes):
                spec = _spec()
                spec.update(changes)
                with self.assertRaises((TypeError, ValueError)):
                    execution_identity(spec)

    def test_legacy_execution_hash_and_fixed_risk_sizing_stay_unchanged(self):
        legacy = {"strategy_id": "legacy", "timeframe": "15m", "universe": list(PAIRS),
                  "params": {"n": 3}, "initial_capital": 10_000,
                  "fee_rate_per_side": .0002, "position_budget": {"leverage": 100}}
        identity = execution_identity(legacy)
        self.assertEqual(identity["execution_hash"],
                         "fc9fc13bf7c22077484165fdf5bbaaa5c04b7e0ac0b892cf4bfa349b01aa2f54")
        self.assertNotIn("risk_sizing_mode", identity["execution"])
        self.assertNotIn("open_bankruptcy_breach_policy", identity["execution"])
        state, _ = self.enter(legacy, atr=50)
        position = state["positions"][PAIRS[0]]
        self.assertAlmostEqual(position["notional"], 50 / (.005 + .0004))
        self.assertNotIn("risk_sizing", position)

    def test_legacy_pending_exit_gap_preserves_its_existing_settlement(self):
        legacy = {"strategy_id": "legacy", "timeframe": "15m", "universe": list(PAIRS),
                  "params": {"n": 3}, "initial_capital": 10_000,
                  "fee_rate_per_side": .0002, "position_budget": {"leverage": 100}}
        opened, _ = self.enter(legacy)
        margin = opened["positions"][PAIRS[0]]["margin"]
        pending, _ = self.step(legacy, opened, 3 * BAR, (0, None), price=99.4)
        state, events = self.step(legacy, pending, 4 * BAR, price=98)
        trade = next(event["payload"] for event in events if event["event_type"] == "trade_closed")
        self.assertFalse(trade["liquidation"])
        self.assertAlmostEqual(trade["gross_pnl"], -2 * margin)
        self.assertEqual(state["closed_trade_count"], 1)

    def test_filtered_preview_uses_exact_subset_and_excludes_other_accounts(self):
        spec = _spec()
        state = initial_state(spec, BAR)
        row = {"strategy_id": spec["strategy_id"], "definition": spec,
               "rule_hash": state["rule_hash"], "state": state,
               "execution": execution_identity(spec)["execution"]}
        other = copy.deepcopy(row)
        other["strategy_id"] = "unselected-broken-account"
        other["definition"] = {"invalid": "must_not_be_loaded"}
        store = MagicMock()
        store._connection.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = [row, other]
        simulation = StrategySimulation(store)
        candles = _bars(BAR)
        candles["TRX/USDT:USDT"] = copy.deepcopy(candles[PAIRS[0]])
        projected = simulation.preview_positions(candles, 2 * BAR + 1000, BAR,
                                                 timeframe="15m", strategy_ids=[spec["strategy_id"]])
        self.assertEqual(projected, {spec["strategy_id"]: {}})
        self.assertEqual(set(simulation._preview_states), {spec["strategy_id"]})

    def test_sample_population_uses_actual_open_even_when_close_crosses_cutoff(self):
        cutoff = SAMPLE_QUALITY_CUTOFF_MS
        for opened_at, quality, eligible in (
                (cutoff - BAR, "stop_definition_defect_period", False),
                (cutoff, "factorial_forward", True)):
            with self.subTest(opened_at=opened_at):
                spec = dict(_spec(), factorial_design_version="atr-risk-full-factorial-v1")
                boundary = opened_at - BAR
                pending, _ = self.step(spec, initial_state(spec, boundary), boundary, (1, None))
                opened, _ = apply_closed_batch(pending, spec, _bars(opened_at),
                                              _signal(spec, opened_at), opened_at + BAR + 1000,
                                              boundary)
                closing, _ = apply_closed_batch(opened, spec, _bars(opened_at + BAR),
                                               _signal(spec, opened_at + BAR, (0, None)),
                                               opened_at + 2 * BAR + 1000, boundary)
                _, events = apply_closed_batch(closing, spec, _bars(opened_at + 2 * BAR),
                                               _signal(spec, opened_at + 2 * BAR),
                                               opened_at + 3 * BAR + 1000, boundary)
                trade = next(event["payload"] for event in events
                             if event["event_type"] == "trade_closed")
                self.assertEqual(trade["opened_candle_ms"], opened_at)
                self.assertGreater(trade["closed_candle_ms"], cutoff)
                self.assertEqual(sample_annotation(spec, trade), {
                    "sample_quality": quality, "eligible_for_factorial_evaluation": eligible})

    def test_post_cutoff_legacy_samples_remain_separate_and_missing_time_is_unknown(self):
        cutoff = SAMPLE_QUALITY_CUTOFF_MS
        legacy = _spec()
        self.assertEqual(sample_annotation(legacy, {"opened_candle_ms": cutoff}), {
            "sample_quality": "legacy_frozen_control", "eligible_for_factorial_evaluation": False})
        self.assertEqual(sample_annotation(legacy, {}), {
            "sample_quality": "unknown", "eligible_for_factorial_evaluation": False})

    def test_api_event_and_trade_annotations_agree_for_pre_cutoff_entry_closed_after_cutoff(self):
        cutoff = SAMPLE_QUALITY_CUTOFF_MS
        spec = dict(_spec(), factorial_design_version="atr-risk-full-factorial-v1")
        state = initial_state(spec, cutoff)
        identity = execution_identity(spec)
        row = {"strategy_id": spec["strategy_id"], "definition": spec, "state": state,
               "execution": identity["execution"], "initial_capital": 10_000,
               "rule_hash": identity["rule_hash"], "execution_hash": identity["execution_hash"]}
        trade = {"trade_id": "cross-cutoff", "opened_candle_ms": cutoff - BAR,
                 "closed_candle_ms": cutoff + BAR, "net_pnl": -1.0}
        event = {"event_id": "closed-event", "event_type": "trade_closed",
                 "candle_ms": cutoff + BAR, "observed_at_ms": cutoff + 2 * BAR + 1000,
                 "rule_hash": identity["rule_hash"], "data_fingerprint": "fixture",
                 "payload": copy.deepcopy(trade)}
        store = MagicMock()
        connection = store._connection.return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = row
        connection.execute.return_value.fetchall.side_effect = [[event], [{"payload": trade}], [], []]
        detail = StrategySimulation(store).account_detail(spec["strategy_id"])
        for record in (detail["trades"][0], detail["events"][0]):
            self.assertEqual(record["sample_quality"], "stop_definition_defect_period")
            self.assertIs(record["eligible_for_factorial_evaluation"], False)
        self.assertNotIn("sample_quality", event)
        self.assertNotIn("sample_quality", trade)


if __name__ == "__main__":
    unittest.main()

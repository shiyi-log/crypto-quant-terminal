"""Atomic pair paper risk: shared budget, conservative contagion and frozen peers."""

import copy
import json
import unittest
from unittest.mock import MagicMock, patch

from strategy_fnew import BTC, ETH, TRX, fnew_rule_hash, load_fnew_specs
from strategy_pair_signals import compute_pair_atr_return
from strategy_simulation import (SAMPLE_QUALITY_CUTOFF_MS, StrategySimulation, _mark,
                                 apply_closed_batch, execution_identity, initial_state,
                                 sample_annotation)


def _tier_snapshot(*, leverages=None):
    leverages = leverages or {BTC: 100, ETH: 100, TRX: 100}
    provenance = {"endpoint": "/fapi/v1/leverageBracket", "http_method": "GET",
                  "security_type": "USER_DATA", "request_id": "pair-test",
                  "signed_at_utc": "2026-10-23T00:00:00Z", "response_sha256": "b" * 64,
                  "environment": "testnet"}
    def schedule(symbol, lev):
        return {"symbol": symbol, "bands": [{"initial_leverage": lev,
                "notional_floor_usdt": "0", "notional_cap_usdt": "1000000",
                "maintenance_margin_ratio": "0.005",
                "cumulative_maintenance_usdt": "0"}], "source": "test",
                "effective_at_utc": "2026-10-23T00:00:00Z",
                "fetched_at_utc": "2026-10-23T00:00:00Z", "account_scope": "pair-test",
                "evidence_sha256": "c" * 64, "verified": True,
                "snapshot_kind": "account_api_snapshot", "notional_cap_inclusive": False,
                "provenance": provenance}
    raw = {"snapshot_hash": "0" * 64, "account_scope": "pair-test",
            "as_of_utc": "2026-10-23T00:00:00Z", "schedules": [
                schedule("BTCUSDT", leverages[BTC]),
                schedule("ETHUSDT", leverages[ETH]),
                schedule("TRXUSDT", leverages[TRX]) ]}
    from core.execution.tier_snapshot import tier_snapshot_hash
    raw["snapshot_hash"] = tier_snapshot_hash(raw)
    return raw


BAR = 3_600_000
START = (SAMPLE_QUALITY_CUTOFF_MS // BAR + 30) * BAR


def bars(timestamp, *, prices=None, legs=(BTC, ETH, TRX)):
    prices = prices or {}
    result = {}
    for pair in legs:
        price = prices.get(pair, 100.0)
        result[pair] = {"timestamp": timestamp, "open": price, "close": price,
                        "high": price + .1, "low": price - .1, "volume": 10,
                        "extras": {"closed": True}}
    return result


def evidence(timestamp, legs=(BTC, ETH), sides=(1, -1), *, width=.1):
    history = {}
    for pair in legs:
        history[pair] = [{"timestamp": timestamp - (14 - index) * BAR,
                          "open": 100.0, "close": 100.0, "high": 100.0 + width,
                          "low": 100.0 - width} for index in range(15)]
    reference = {pair: 100.0 for pair in legs}
    return {"pair_group": "hypothesis:" + "|".join(legs), "pair_legs": list(legs),
            "pair_sides": list(sides), "pair_entry_reference_prices": reference,
            "pair_atr_inputs": history, "pair_atr_window": 14,
            "entry_pair_atr_return": compute_pair_atr_return(history, legs, sides, reference, 14),
            "pair_coefficients": {"intercept": 0.1, "beta": 1.2}, "pair_entry_rho": .4}


def signal(spec, timestamp, targets=None, *, legs=(BTC, ETH), data=None, reason="test_pair"):
    targets = (None, None) if targets is None else targets
    data = evidence(timestamp, legs, (1, -1)) if data is None else data
    values = {pair: {"target_side": None, "candle_utc": timestamp,
                     "reason": "unselected_context", "evidence": {}, "size_multiplier": 1}
              for pair in spec["execution_universe"]}
    for pair, side in zip(legs, targets, strict=True):
        values[pair] = {"target_side": side, "candle_utc": timestamp, "reason": reason,
                        "evidence": copy.deepcopy(data), "size_multiplier": 1}
    return {"strategy_id": spec["strategy_id"], "rule_hash": fnew_rule_hash(spec),
            "ready": True, "warmup": {"ready": True, "valid_bars": 750, "required_bars": 501},
            "signals": values, "group": {"atomic": True, "legs": list(legs),
                                           "pair_group": data.get("pair_group")}}


class PairExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definitions = load_fnew_specs(paired_execution_enabled=True)

    def definition(self, *, canonical="s32", risk=50, k=1.5):
        return copy.deepcopy(next(spec for spec in self.definitions
                                  if spec["strategy_id"].startswith(canonical + "_")
                                  and spec["max_loss_per_trade_usdt"] == risk
                                  and spec["params"]["risk_stop_atr_mult"] == k))

    def step(self, spec, state, timestamp, targets=None, *, candle=None, legs=(BTC, ETH),
             data=None, reason="test_pair", delay=1000, boundary=START):
        return apply_closed_batch(state, spec, bars(timestamp) if candle is None else candle,
                                  signal(spec, timestamp, targets, legs=legs, data=data, reason=reason),
                                  timestamp + BAR + delay, boundary)

    def enter(self, spec=None, *, legs=(BTC, ETH), width=.1, fill_prices=None):
        spec = self.definition() if spec is None else spec
        pending, _ = self.step(spec, initial_state(spec, START), START, (1, -1),
                               legs=legs, data=evidence(START, legs, width=width))
        return self.step(spec, pending, START + BAR, legs=legs,
                         candle=bars(START + BAR, prices=fill_prices))

    def test_pair_budget_is_not_applied_twice_and_grid_is_frozen(self):
        for risk in (50, 200, 1000):
            for k in (1.0, 1.5, 2.5):
                with self.subTest(risk=risk, k=k):
                    spec = self.definition(risk=risk, k=k)
                    state, events = self.enter(spec)
                    a, b = state["positions"][BTC], state["positions"][ETH]
                    n = min(risk / (k * .004 + .0012), 150000 / (1 + .3 * 100 * .0002))
                    self.assertAlmostEqual(a["quantity"] * a["entry_price"], n)
                    self.assertAlmostEqual(b["quantity"] * b["entry_price"], n)
                    self.assertAlmostEqual(a["risk_sizing"]["planned_loss_usdt"], n * (k * .004 + .0012))
                    self.assertLessEqual(a["risk_sizing"]["planned_loss_usdt"], risk + 1e-8)
                    self.assertEqual(a["risk_sizing"], b["risk_sizing"])
                    self.assertEqual(a["group_id"], b["group_id"])
                    self.assertEqual(a["pair_entry_prices"], {BTC: 100, ETH: 100})
                    self.assertEqual(a["entry_pair_coefficients"], {"intercept": .1, "beta": 1.2})
                    self.assertEqual(len([item for item in events if item["event_type"] == "position_opened"]), 2)
                    self.assertLessEqual(_mark(state)["margin_used"], .3 * _mark(state)["equity"] + 1e-8)

    def test_tiered_pair_rejects_all_legs_before_any_cash_mutation(self):
        spec = self.definition()
        spec["liquidation_model"] = "binance_tiered"
        spec["tier_snapshot"] = _tier_snapshot()

        def tiered(pair, entry_price, direction, leverage, notional, tier_provider=None):
            rejected = pair == ETH
            return entry_price * (0.99 if direction == 1 else 1.01), {
                "tier_max_leverage": 75 if rejected else 100,
                "tier_leverage_exceeded": rejected,
                "maintenance_margin_incompatible": False,
                "tier_snapshot_hash": spec["tier_snapshot"]["snapshot_hash"],
                "account_scope": "pair-test",
                "tier_evidence_sha256": "c" * 64,
                "tier_rejection_reason": "tier_leverage_exceeded" if rejected else None,
            }

        with patch("strategy_simulation._tiered_liquidation_price", side_effect=tiered):
            state, events = self.enter(spec)
        self.assertFalse(state["positions"])
        self.assertAlmostEqual(state["cash"], 10_000.0)
        self.assertEqual(state["fees_paid"], 0.0)
        self.assertFalse(any(item["event_type"] in {"order_filled", "position_opened"}
                             for item in events))
        rejected = [item["payload"] for item in events if item["event_type"] == "order_rejected"]
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all(item["reason"] == "tier_leverage_exceeded" for item in rejected))
        self.assertEqual({item["pair"] for item in rejected}, {BTC, ETH})
        for item in rejected:
            with self.subTest(pair=item["pair"]):
                self.assertEqual(item["tier_snapshot_hash"],
                                 spec["tier_snapshot"]["snapshot_hash"])
                self.assertEqual(item["account_scope"], "pair-test")
                self.assertEqual(item["tier_evidence_sha256"], "c" * 64)

    def test_tiered_pair_provider_early_rejection_does_not_claim_unverified_provenance(self):
        spec = self.definition()
        spec["liquidation_model"] = "binance_tiered"
        spec["tier_snapshot"] = _tier_snapshot()
        pending, _ = self.step(spec, initial_state(spec, START), START, (1, -1))
        with patch("strategy_simulation._tier_provider_issue",
                   side_effect=[None, "tier_snapshot_hash_mismatch"]):
            state, events = self.step(spec, pending, START + BAR)

        self.assertFalse(state["positions"])
        rejected = [item["payload"] for item in events
                    if item["event_type"] == "order_rejected"]
        self.assertEqual(len(rejected), 2)
        for item in rejected:
            with self.subTest(pair=item["pair"]):
                self.assertEqual(item["reason"], "tier_snapshot_unavailable")
                self.assertEqual(item["group_rejection_reason"],
                                 "tier_snapshot_hash_mismatch")
                self.assertEqual(item["tier_snapshot_hash"],
                                 spec["tier_snapshot"]["snapshot_hash"])
                self.assertNotIn("account_scope", item)
                self.assertNotIn("tier_evidence_sha256", item)
                self.assertNotIn("liquidation_tier", item)

    def test_tiered_pair_uses_per_leg_leverage_and_margin_with_frozen_snapshot(self):
        spec = self.definition()
        spec["liquidation_model"] = "binance_tiered"
        spec["tier_snapshot"] = _tier_snapshot(leverages={BTC: 100, ETH: 75, TRX: 25})
        spec["position_budget"] = dict(spec["position_budget"],
                                       leverage_by_symbol={BTC: 100, ETH: 75, TRX: 25})
        # The per-symbol matrix is a new executable identity; let Fnew derive
        # its rule hash instead of reusing the frozen scalar-leverage hash.
        spec.pop("rule_hash", None)
        state, events = self.enter(spec, legs=(BTC, ETH))
        self.assertEqual(set(state["positions"]), {BTC, ETH})
        btc, eth = state["positions"][BTC], state["positions"][ETH]
        self.assertEqual(btc["leverage"], 100)
        self.assertEqual(eth["leverage"], 75)
        self.assertAlmostEqual(btc["margin"], btc["notional"] / 100)
        self.assertAlmostEqual(eth["margin"], eth["notional"] / 75)
        self.assertAlmostEqual(btc["notional"], eth["notional"])
        self.assertEqual(btc["liquidation_tier"]["tier_snapshot_hash"],
                         spec["tier_snapshot"]["snapshot_hash"])
        self.assertEqual(eth["liquidation_tier"]["tier_max_leverage"], 75)
        fills = [item["payload"] for item in events if item["event_type"] == "order_filled"]
        self.assertEqual({item["leverage"] for item in fills}, {75, 100})

    def test_tiered_pair_liquidation_settles_each_leg_and_preserves_tier_evidence(self):
        spec = self.definition()
        spec["liquidation_model"] = "binance_tiered"
        spec["tier_snapshot"] = _tier_snapshot(
            leverages={BTC: 100, ETH: 100, TRX: 100})
        opened, _ = self.enter(spec)
        btc_position = opened["positions"][BTC]
        current = bars(START + 2 * BAR)
        current[BTC]["low"] = btc_position["liquidation_price"]
        current[ETH]["high"] = 100.2
        exited, events = self.step(spec, opened, START + 2 * BAR, candle=current)

        self.assertFalse(exited["positions"])
        trades = {item["payload"]["pair"]: item["payload"] for item in events
                  if item["event_type"] == "trade_closed"}
        self.assertEqual(set(trades), {BTC, ETH})
        for pair, position in opened["positions"].items():
            exit_price = trades[pair]["exit_price"]
            expected_gross = position["direction"] * position["quantity"] * (
                exit_price - position["entry_price"])
            with self.subTest(pair=pair):
                self.assertAlmostEqual(trades[pair]["gross_pnl"], expected_gross, places=8)
                self.assertEqual(trades[pair]["liquidation_model"], "binance_tiered")
                self.assertEqual(trades[pair]["tier_snapshot_hash"],
                                 spec["tier_snapshot"]["snapshot_hash"])
                self.assertEqual(trades[pair]["account_scope"], "pair-test")
                self.assertEqual(trades[pair]["tier_evidence_sha256"], "c" * 64)

        liquidation_events = [item["payload"] for item in events
                              if item["event_type"] == "liquidation"]
        self.assertEqual(len(liquidation_events), 1)
        liquidation_payload = liquidation_events[0]
        self.assertEqual(liquidation_payload["tier_snapshot_hash"],
                         spec["tier_snapshot"]["snapshot_hash"])
        self.assertEqual(liquidation_payload["account_scope"], "pair-test")
        self.assertEqual(liquidation_payload["tier_evidence_sha256"], "c" * 64)

        pair_trade = next(item["payload"] for item in events
                          if item["event_type"] == "pair_trade_closed")
        self.assertEqual(len(pair_trade["tier_evidence"]), 2)
        self.assertEqual({entry["pair"] for entry in pair_trade["tier_evidence"]},
                         {BTC, ETH})
        self.assertAlmostEqual(exited["cash"], 10_000 + pair_trade["net_pnl"], places=8)

    def test_actual_next_opens_recompute_atr_and_quantities_keep_equal_notional(self):
        state, _ = self.enter(fill_prices={BTC: 120, ETH: 80})
        a, b = state["positions"][BTC], state["positions"][ETH]
        atr = .2 / 120 + .2 / 80
        self.assertAlmostEqual(a["entry_pair_atr_return"], atr)
        self.assertEqual(a["pair_entry_reference_prices"], {BTC: 100, ETH: 100})
        self.assertEqual(a["pair_entry_prices"], {BTC: 120, ETH: 80})
        self.assertAlmostEqual(a["quantity"] * 120, b["quantity"] * 80)
        self.assertAlmostEqual(a["notional"], 50 / (1.5 * atr + .0012))

    def test_s32_chooses_two_legs_from_three_and_unused_context_may_be_missing(self):
        spec, legs = self.definition(), (ETH, TRX)
        pending, _ = self.step(spec, initial_state(spec, START), START, (1, -1), legs=legs)
        self.assertEqual({order["pair"] for order in pending["pending_orders"]}, set(legs))
        current = bars(START + BAR, legs=legs)
        sig = signal(spec, START + BAR, legs=legs)
        sig["signals"][BTC]["candle_utc"] = START - 5 * BAR
        opened, _ = apply_closed_batch(pending, spec, current, sig, START + 2 * BAR + 1000, START)
        self.assertEqual(set(opened["positions"]), set(legs))

    def test_missing_leg_cancels_both_orders_with_no_naked_exposure(self):
        spec = self.definition()
        pending, _ = self.step(spec, initial_state(spec, START), START, (1, -1))
        state, events = self.step(spec, pending, START + BAR,
                                  candle=bars(START + BAR, legs=(BTC, TRX)))
        self.assertFalse(state["positions"])
        self.assertFalse(state["pending_orders"])
        cancelled = [item["payload"] for item in events if item["event_type"] == "order_cancelled"]
        self.assertEqual(len(cancelled), 2)
        self.assertTrue(all(item["reason"] == "atomic_leg_missing" for item in cancelled))

    def test_incomplete_pending_group_is_cancelled_without_partial_fill(self):
        spec = self.definition()
        pending, _ = self.step(spec, initial_state(spec, START), START, (1, -1))
        pending["pending_orders"].pop()
        state, events = self.step(spec, pending, START + BAR)
        self.assertFalse(state["positions"])
        self.assertIn("atomic_order_group_incomplete", [item["payload"].get("reason") for item in events])

    def test_stale_gap_and_restart_cancel_whole_pending_group(self):
        spec = self.definition()
        pending, _ = self.step(spec, initial_state(spec, START), START, (1, -1))
        for timestamp, delay, boundary, reason in (
                (START + BAR, 90_001, START, "stale_closed_bar"),
                (START + 2 * BAR, 1000, START, "gap_cancel"),
                (START + BAR, 1000, START + BAR, "restart_cancel")):
            with self.subTest(reason=reason):
                state, events = self.step(spec, pending, timestamp, delay=delay, boundary=boundary)
                self.assertFalse(state["positions"])
                self.assertEqual([item["payload"]["reason"] for item in events
                                  if item["event_type"] == "order_cancelled"], [reason, reason])

    def test_close_stop_is_portfolio_based_and_fills_both_next_open(self):
        spec = self.definition()
        state, _ = self.enter(spec)
        first = state["positions"][BTC]
        stopped, events = self.step(spec, state, START + 2 * BAR,
                                    candle=bars(START + 2 * BAR, prices={BTC: 99.65, ETH: 100.35}))
        self.assertEqual(len(stopped["positions"]), 2)
        self.assertEqual(len(stopped["pending_orders"]), 2)
        self.assertTrue(all(order["reason"] == "pair_stop_loss_close_confirmed"
                            for order in stopped["pending_orders"]))
        exited, events = self.step(spec, stopped, START + 3 * BAR,
                                   candle=bars(START + 3 * BAR, prices={BTC: 99.6, ETH: 100.4}))
        self.assertFalse(exited["positions"])
        self.assertEqual(exited["closed_trade_count"], 1)
        pair_trade = next(item["payload"] for item in events if item["event_type"] == "pair_trade_closed")
        self.assertEqual(len(pair_trade["legs"]), 2)
        self.assertAlmostEqual(pair_trade["gross_pnl"], -.008 * first["notional"])
        self.assertAlmostEqual(pair_trade["fees"], sum(leg["fees"] for leg in pair_trade["legs"]))
        self.assertLess(pair_trade["net_pnl"], -50)  # Close confirmation is no strict realized loss cap.

    def test_open_liquidation_closes_peer_at_same_actual_open_before_pending_stop(self):
        spec = self.definition()
        state, _ = self.enter(spec)
        stopped, _ = self.step(spec, state, START + 2 * BAR, (0, 0), reason="stop_loss_close_confirmed")
        exited, events = self.step(spec, stopped, START + 3 * BAR,
                                   candle=bars(START + 3 * BAR, prices={BTC: 98, ETH: 99.8}))
        self.assertFalse(exited["positions"])
        self.assertEqual(exited["closed_trade_count"], 1)
        trades = {item["payload"]["pair"]: item["payload"] for item in events
                  if item["event_type"] == "trade_closed"}
        self.assertTrue(trades[BTC]["liquidation"])
        self.assertEqual(trades[BTC]["exit_price"], 99)
        self.assertFalse(trades[ETH]["liquidation"])
        self.assertEqual(trades[ETH]["exit_price"], 99.8)
        self.assertEqual(trades[ETH]["fill_model"], "synchronized_peer_open_after_leg_liquidation")
        self.assertEqual(len([item for item in events if item["event_type"] == "order_cancelled"]), 2)

    def test_intrabar_liquidation_uses_peer_adverse_extreme_and_never_fakes_open(self):
        spec = self.definition()
        state, _ = self.enter(spec)
        current = bars(START + 2 * BAR)
        current[BTC]["low"] = 98.8
        current[ETH]["high"] = 100.7
        exited, events = self.step(spec, state, START + 2 * BAR, candle=current)
        self.assertFalse(exited["positions"])
        trades = {item["payload"]["pair"]: item["payload"] for item in events
                  if item["event_type"] == "trade_closed"}
        self.assertAlmostEqual(trades[BTC]["gross_pnl"], -state["positions"][BTC]["margin"])
        self.assertEqual(trades[ETH]["exit_price"], 100.7)
        self.assertEqual(trades[ETH]["fill_model"], "conservative_peer_bar_extreme_after_leg_liquidation")
        pair = next(item["payload"] for item in events if item["event_type"] == "pair_trade_closed")
        self.assertEqual(pair["intrabar_peer_timing"], "unknown_conservative_adverse_extreme")

    def test_both_intrabar_liquidations_lose_exact_total_margin_plus_fees(self):
        spec = self.definition()
        state, _ = self.enter(spec)
        current = bars(START + 2 * BAR)
        current[BTC]["low"], current[ETH]["high"] = 98, 102
        exited, events = self.step(spec, state, START + 2 * BAR, candle=current)
        pair = next(item["payload"] for item in events if item["event_type"] == "pair_trade_closed")
        self.assertAlmostEqual(pair["gross_pnl"], -sum(pos["margin"] for pos in state["positions"].values()))
        self.assertAlmostEqual(exited["cash"], 10000 + pair["net_pnl"])

    def test_new_fill_is_exposed_to_intrabar_liquidation_atomically(self):
        spec = self.definition()
        pending, _ = self.step(spec, initial_state(spec, START), START, (1, -1))
        current = bars(START + BAR)
        current[BTC]["low"] = 98
        exited, events = self.step(spec, pending, START + BAR, candle=current)
        self.assertFalse(exited["positions"])
        self.assertEqual(exited["closed_trade_count"], 1)
        types = [item["event_type"] for item in events]
        self.assertLess(types.index("position_opened"), types.index("liquidation"))

    def test_missing_peer_while_held_defers_batch_and_late_matching_peer_recovers_risk(self):
        spec = self.definition()
        state, _ = self.enter(spec)
        immutable = copy.deepcopy(state)
        current = bars(START + 2 * BAR, legs=(BTC, TRX))
        current[BTC]["low"] = 98
        with self.assertRaisesRegex(ValueError, "defer until matching peer arrives"):
            self.step(spec, state, START + 2 * BAR, candle=current)
        self.assertEqual(state, immutable)
        self.assertEqual(state["last_candle_ms"], START + BAR)
        # Late ETH at the same timestamp remains the next unprocessed bar,
        # rather than an immutable fingerprint revision or a lost risk event.
        current[ETH] = bars(START + 2 * BAR)[ETH]
        exited, events = self.step(spec, state, START + 2 * BAR, candle=current)
        self.assertFalse(exited["positions"])
        self.assertEqual(exited["closed_trade_count"], 1)
        self.assertIn("liquidation", [item["event_type"] for item in events])

    def test_second_pair_entry_cannot_overwrite_a_held_pair(self):
        spec = self.definition()
        opened, _ = self.enter(spec)
        refused, events = self.step(spec, opened, START + 2 * BAR, (1, -1), legs=(ETH, TRX))
        self.assertEqual(set(refused["positions"]), {BTC, ETH})
        self.assertFalse(refused["pending_orders"])
        self.assertIn("pair_account_already_occupied", [item["payload"].get("reason") for item in events])

    def test_margin_slots_and_cash_are_caps_below_portfolio_risk(self):
        spec = self.definition(risk=1000)
        state, _ = self.enter(spec, width=.00001)
        self.assertEqual(len(state["positions"]), 2)
        self.assertGreaterEqual(state["cash"], 0)
        self.assertAlmostEqual(_mark(state)["margin_used"], .3 * _mark(state)["equity"])
        for position in state["positions"].values():
            self.assertLess(position["risk_sizing"]["planned_loss_usdt"], 1000)

    def test_bad_pair_metadata_and_future_or_unfrozen_atr_are_denied(self):
        spec = self.definition()
        for alteration in ("missing", "future", "window", "zero", "mismatched"):
            with self.subTest(alteration=alteration):
                data = evidence(START)
                if alteration == "missing":
                    data.pop("pair_atr_inputs")
                elif alteration == "future":
                    data["pair_atr_inputs"][BTC][-1]["timestamp"] += BAR
                elif alteration == "window":
                    data["pair_atr_window"] = 1
                elif alteration == "zero":
                    for values in data["pair_atr_inputs"].values():
                        for row in values:
                            row["low"] = row["high"] = 100
                sig = signal(spec, START, (1, -1), data=data)
                if alteration == "mismatched":
                    sig["signals"][ETH]["evidence"]["pair_group"] = "other"
                refused, events = apply_closed_batch(initial_state(spec, START), spec, bars(START), sig,
                                                      START + BAR + 1000, START)
                self.assertFalse(refused["pending_orders"])
                self.assertIn("entry_pair_atr_unavailable_or_invalid",
                              [item["payload"].get("reason") for item in events])
                json.dumps(events, allow_nan=False)

    def test_per_leg_ohlc_evidence_can_differ_while_pair_risk_metadata_agrees(self):
        spec = self.definition()
        sig = signal(spec, START, (1, -1))
        sig["signals"][BTC]["evidence"]["close"] = 101
        sig["signals"][ETH]["evidence"]["close"] = 99
        pending, _ = apply_closed_batch(initial_state(spec, START), spec, bars(START), sig,
                                        START + BAR + 1000, START)
        self.assertEqual(len(pending["pending_orders"]), 2)

    def test_repeated_batch_is_idempotent_and_revised_bar_is_rejected(self):
        spec = self.definition()
        state, _ = self.enter(spec)
        repeated, events = self.step(spec, state, START + BAR)
        self.assertFalse(events)
        self.assertEqual(repeated, state)
        with self.assertRaisesRegex(ValueError, "processed closed candle changed"):
            self.step(spec, state, START + BAR, candle=bars(START + BAR, prices={BTC: 100.2}))

    def test_inconsistent_frozen_pair_never_creates_single_leg_decision(self):
        spec = self.definition()
        state, _ = self.enter(spec)
        state["positions"][ETH]["group_id"] = "corrupt"
        with self.assertRaisesRegex(ValueError, "metadata is inconsistent"):
            self.step(spec, state, START + 2 * BAR)

    def test_disabled_pair_definitions_do_not_trade(self):
        spec = next(spec for spec in load_fnew_specs() if spec["strategy_id"].startswith("s32_"))
        state, events = self.step(spec, initial_state(spec, START), START, (1, -1))
        self.assertFalse(state["pending_orders"])
        self.assertIn("execution_disabled", [item["payload"].get("reason") for item in events])

    def test_execution_hash_binds_pair_path_and_preserves_old_atr_hash(self):
        legacy = {"strategy_id": "legacy", "timeframe": "15m", "universe": [BTC, ETH],
                  "params": {"n": 3}, "initial_capital": 10000,
                  "fee_rate_per_side": .0002, "position_budget": {"leverage": 100}}
        with patch("strategy_simulation._rule_hash", return_value="legacy"):
            self.assertEqual(execution_identity(legacy)["execution_hash"],
                             "fc9fc13bf7c22077484165fdf5bbaaa5c04b7e0ac0b892cf4bfa349b01aa2f54")
        spec = self.definition()
        model = execution_identity(spec)["execution"]
        self.assertEqual(model["risk_scope"], "atomic_pair_portfolio")
        self.assertIn("direction_adverse_closed_bar_extreme", model["liquidation"]["peer_intrabar_exit"])
        bad = copy.deepcopy(spec)
        bad["paired_execution_policy"] = "unreviewed"
        with self.assertRaisesRegex(ValueError, "paired execution policy"):
            execution_identity(bad)

    def test_s33_taker_population_is_visible_and_hash_dispatch_works(self):
        spec = self.definition(canonical="s33")
        identity, state = execution_identity(spec), initial_state(spec, START)
        self.assertEqual(identity["rule_hash"], fnew_rule_hash(spec))
        self.assertEqual(identity["execution"]["fee_rate_per_side"], .0004)
        row = {"strategy_id": spec["strategy_id"], "definition": spec, "state": state,
               "execution": identity["execution"], "initial_capital": 10000,
               "rule_hash": identity["rule_hash"], "execution_hash": identity["execution_hash"]}
        store = MagicMock()
        store._connection.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = [row]
        account = StrategySimulation(store).overview()["accounts"][0]
        self.assertEqual(account["lifecycle"], "active")
        self.assertEqual(account["sample_population"], spec["sample_population"])
        annotation = sample_annotation(spec, {"opened_candle_ms": START})
        self.assertEqual(annotation["sample_quality"], "fnew_forward")
        self.assertFalse(annotation["eligible_for_factorial_evaluation"])
        self.assertTrue(annotation["eligible_for_population_evaluation"])

    def test_pair_api_exposes_completed_group_samples_and_retains_leg_audit_events(self):
        spec = self.definition()
        opened, _ = self.enter(spec)
        pending, _ = self.step(spec, opened, START + 2 * BAR, (0, 0))
        exited, events = self.step(spec, pending, START + 3 * BAR)
        pair = next(item["payload"] for item in events if item["event_type"] == "pair_trade_closed")
        identity = execution_identity(spec)
        row = {"strategy_id": spec["strategy_id"], "definition": spec, "state": exited,
               "execution": identity["execution"], "initial_capital": 10000,
               "rule_hash": identity["rule_hash"], "execution_hash": identity["execution_hash"]}
        store = MagicMock()
        conn = store._connection.return_value.__enter__.return_value
        conn.execute.return_value.fetchone.return_value = row
        conn.execute.return_value.fetchall.side_effect = [events, [{"payload": pair}], [], []]
        detail = StrategySimulation(store).account_detail(spec["strategy_id"])
        self.assertEqual(len(detail["trades"]), 1)
        self.assertEqual(detail["trades"][0]["sample_unit"], "completed_atomic_pair")
        self.assertEqual(len([event for event in detail["events"] if event["event_type"] == "trade_closed"]), 2)
        queries = [call.args[0] for call in conn.execute.call_args_list]
        self.assertTrue(any("event_type='pair_trade_closed'" in query for query in queries))


if __name__ == "__main__":
    unittest.main()

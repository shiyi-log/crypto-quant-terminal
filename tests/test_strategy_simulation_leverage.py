"""100x paper execution: margin sizing, liquidation, and fee accounting."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from strategy_simulation import (BAR_MS, PAIRS, _mark, _tiered_liquidation_price,
                                  apply_closed_batch, execution_identity, initial_state)


def _tier_snapshot():
    provenance = {"endpoint": "/fapi/v1/leverageBracket", "http_method": "GET",
                  "security_type": "USER_DATA", "request_id": "test-request",
                  "signed_at_utc": "2026-10-23T00:00:00Z", "response_sha256": "b" * 64,
                  "environment": "testnet"}
    def schedule(symbol, bands):
        return {"symbol": symbol, "bands": bands, "source": "test",
                "effective_at_utc": "2026-10-23T00:00:00Z",
                "fetched_at_utc": "2026-10-23T00:00:00Z", "account_scope": "test-account",
                "evidence_sha256": "c" * 64, "verified": True,
                "snapshot_kind": "account_api_snapshot", "notional_cap_inclusive": False,
                "provenance": provenance}
    raw = {"snapshot_hash": "0" * 64, "account_scope": "test-account",
            "as_of_utc": "2026-10-23T00:00:00Z", "schedules": [
                schedule("BTCUSDT", [{"initial_leverage": 125, "notional_floor_usdt": "0",
                                        "notional_cap_usdt": "50000", "maintenance_margin_ratio": "0.004",
                                        "cumulative_maintenance_usdt": "0"}]),
                schedule("ETHUSDT", [{"initial_leverage": 100, "notional_floor_usdt": "0",
                                        "notional_cap_usdt": "10000", "maintenance_margin_ratio": "0.005",
                                        "cumulative_maintenance_usdt": "0"},
                                       {"initial_leverage": 75, "notional_floor_usdt": "10000",
                                        "notional_cap_usdt": "100000", "maintenance_margin_ratio": "0.0065",
                                        "cumulative_maintenance_usdt": "15"}]),
                schedule("TRXUSDT", [{"initial_leverage": 75, "notional_floor_usdt": "0",
                                        "notional_cap_usdt": "5000", "maintenance_margin_ratio": "0.0065",
                                        "cumulative_maintenance_usdt": "0"}]),
            ]}
    from core.execution.tier_snapshot import tier_snapshot_hash
    raw["snapshot_hash"] = tier_snapshot_hash(raw)
    return raw


def _matrix_spec() -> dict:
    spec = _spec("test-leverage-matrix")
    spec["liquidation_model"] = "binance_tiered"
    spec["position_budget"] = dict(spec["position_budget"], leverage_by_symbol={
        PAIRS[0]: 100,
        PAIRS[1]: 75,
    })
    return spec


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
        "tier_snapshot": _tier_snapshot(),
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

    def test_tiered_liquidation_settles_price_derived_gross_and_provenance(self):
        spec = copy.deepcopy(self.spec)
        spec["liquidation_model"] = "binance_tiered"
        state = initial_state(spec, BAR_MS)
        self.spec = spec
        self.state = state
        opened, _ = self.enter((1, None))
        position = opened["positions"][PAIRS[0]]
        liquidation_price = position["liquidation_price"]
        liquidated, events = self.step(
            3 * BAR_MS,
            state=opened,
            candles=_bars(3 * BAR_MS, open_price=100, high=100.2,
                          low=liquidation_price, close=100),
        )

        trade = next(item["payload"] for item in events
                     if item["event_type"] == "trade_closed")
        expected_gross = position["direction"] * position["quantity"] * (
            liquidation_price - position["entry_price"])
        exit_fee = position["quantity"] * liquidation_price * spec["fee_rate_per_side"]
        expected_net = expected_gross - position["entry_fee"] - exit_fee
        self.assertAlmostEqual(trade["gross_pnl"], expected_gross, places=8)
        self.assertNotAlmostEqual(trade["gross_pnl"], -position["margin"], places=8)
        self.assertAlmostEqual(trade["net_pnl"], expected_net, places=8)
        self.assertAlmostEqual(liquidated["cash"], spec["initial_capital"] + expected_net,
                               places=8)

        expected_provenance = {
            "tier_snapshot_hash": spec["tier_snapshot"]["snapshot_hash"],
            "account_scope": "test-account",
            "tier_evidence_sha256": "c" * 64,
        }
        liquidation_event = next(item["payload"] for item in events
                                 if item["event_type"] == "liquidation")
        exit_fill = next(item["payload"] for item in events
                         if item["event_type"] == "order_filled"
                         and item["payload"].get("liquidation"))
        for payload in (liquidation_event, exit_fill, trade):
            with self.subTest(event=payload):
                for key, value in expected_provenance.items():
                    self.assertEqual(payload[key], value)
                self.assertEqual(payload["liquidation_model"], "binance_tiered")

    def test_tiered_model_checks_max_leverage_independently_at_boundaries(self):
        # The ETH tier changes at exactly 10,000 USDT: 9,999.99 remains a
        # legal 100x tier-1 position, while 10,000 enters the 75x tier and
        # must be rejected. The mmr diagnostic is intentionally separate.
        from core.execution.tier_snapshot import load_tier_snapshot
        snapshot = load_tier_snapshot(_tier_snapshot())
        _, legal = _tiered_liquidation_price(
            "ETH/USDT:USDT", 2_000.0, 1, 100, 9_999.99, snapshot)
        _, rejected = _tiered_liquidation_price(
            "ETH/USDT:USDT", 2_000.0, 1, 100, 10_000.0, snapshot)
        self.assertFalse(legal["tier_leverage_exceeded"])
        self.assertIsNone(legal["tier_rejection_reason"])
        self.assertTrue(rejected["tier_leverage_exceeded"])
        self.assertEqual(rejected["tier_rejection_reason"], "tier_leverage_exceeded")

    def test_tiered_model_accepts_boundary_leverage_from_frozen_snapshot(self):
        from core.execution.tier_snapshot import load_tier_snapshot
        snapshot = load_tier_snapshot(_tier_snapshot())
        price, evidence = _tiered_liquidation_price(
            "ETH/USDT:USDT", 2_000.0, 1, 75, 10_000.0, snapshot)
        self.assertGreater(price, 0)
        self.assertFalse(evidence["tier_leverage_exceeded"])
        self.assertFalse(evidence["maintenance_margin_incompatible"])
        self.assertIsNone(evidence["tier_rejection_reason"])
        self.assertEqual(evidence["tier_max_leverage"], 75)

    def test_frozen_snapshot_rejects_unsorted_bands(self):
        from core.execution.tier_snapshot import load_tier_snapshot

        raw = copy.deepcopy(_tier_snapshot())
        # Keep the evidence hash intentionally unchanged: the loader must
        # reject the malformed serialized order before accepting any hash or
        # selecting a tier.  Silently sorting here would make the same bytes
        # produce different execution evidence in different implementations.
        eth = next(item for item in raw["schedules"] if item["symbol"] == "ETHUSDT")
        eth["bands"] = list(reversed(eth["bands"]))
        with self.assertRaisesRegex(ValueError, "ascending notional-floor order"):
            load_tier_snapshot(raw)

    def test_frozen_snapshot_requires_boolean_cap_boundary(self):
        from core.execution.tier_snapshot import load_tier_snapshot

        for malformed in (None, "false", 1):
            with self.subTest(boundary=malformed):
                raw = copy.deepcopy(_tier_snapshot())
                raw["schedules"][0]["notional_cap_inclusive"] = malformed
                with self.assertRaisesRegex(ValueError, "notional_cap_inclusive"):
                    load_tier_snapshot(raw)

    def test_frozen_snapshot_requires_explicit_finite_notional_cap(self):
        from core.execution.tier_snapshot import load_tier_snapshot

        for label, replacement in (
            ("missing", {}),
            ("null", {"notional_cap_usdt": None}),
            ("infinite_string", {"notional_cap_usdt": "Infinity"}),
            ("infinite_decimal", {"notional_cap_usdt": Decimal("Infinity")}),
            ("alias_only", {"notional_upper_usdt": "50000"}),
        ):
            with self.subTest(cap=label):
                raw = copy.deepcopy(_tier_snapshot())
                band = raw["schedules"][0]["bands"][0]
                band.pop("notional_cap_usdt")
                band.update(replacement)
                with self.assertRaisesRegex(ValueError, "notional_cap_usdt.*finite"):
                    load_tier_snapshot(raw)

    def test_tiered_model_uses_cumulative_maintenance_in_liquidation_price(self):
        class Provider:
            snapshot_hash = "a" * 64
            account_scope = "test-account"

            def get_schedule(self, pair):
                return {"evidence_sha256": "c" * 64}

            def get_tier(self, pair, notional):
                return {"notional_floor_usdt": "0", "notional_cap_usdt": "100000",
                        "maintenance_margin_ratio": "0.005", "initial_leverage": 100,
                        "cumulative_maintenance_usdt": "10"}

        price, evidence = _tiered_liquidation_price(
            "BTC/USDT:USDT", 100.0, 1, 100, 10_000.0, Provider())
        # qty=100, cumulative maintenance contributes 0.1 USDT per unit.
        expected = (100.0 - 1.0 - 0.1) / (1 - 0.005)
        self.assertAlmostEqual(price, expected)
        self.assertEqual(evidence["tier_cumulative_maintenance_usdt"], 10.0)

    def test_matrix_requires_frozen_snapshot(self):
        spec = _spec("test-leverage-matrix-no-snapshot")
        spec["liquidation_model"] = "binance_tiered"
        spec["position_budget"] = dict(spec["position_budget"], leverage_by_symbol={
            PAIRS[0]: 100, PAIRS[1]: 75})
        spec.pop("tier_snapshot")
        with self.assertRaisesRegex(ValueError, "frozen tier_snapshot"):
            execution_identity(spec)

    def test_matrix_entry_uses_per_symbol_margin_and_tier_evidence(self):
        spec = _matrix_spec()
        state = initial_state(spec, BAR_MS)
        self.spec = spec
        pending, _ = self.step(BAR_MS, (1, -1), state=state)
        opened, events = self.step(2 * BAR_MS, state=pending)
        self.assertEqual(set(opened["positions"]), set(PAIRS))
        for pair, position in opened["positions"].items():
            expected = 100 if pair == PAIRS[0] else 75
            with self.subTest(pair=pair):
                self.assertEqual(position["leverage"], expected)
                self.assertAlmostEqual(position["margin"],
                                       position["notional"] / expected)
                self.assertEqual(position["liquidation_model"], "binance_tiered")
                self.assertIsNone(position["liquidation_tier"]["tier_rejection_reason"])
                self.assertEqual(position["liquidation_tier"]["tier_snapshot_hash"],
                                 spec["tier_snapshot"]["snapshot_hash"])
        fills = [item["payload"] for item in events if item["event_type"] == "order_filled"]
        self.assertEqual({item["leverage"] for item in fills}, {75, 100})

    def test_matrix_execution_hash_binds_leverage_map_and_snapshot(self):
        first = execution_identity(_matrix_spec())
        changed_leverage = _matrix_spec()
        changed_leverage["position_budget"] = dict(
            changed_leverage["position_budget"],
            leverage_by_symbol={PAIRS[0]: 99, PAIRS[1]: 75})
        changed_snapshot = _matrix_spec()
        changed_snapshot["tier_snapshot"] = copy.deepcopy(_tier_snapshot())
        changed_snapshot["tier_snapshot"]["as_of_utc"] = "2026-10-23T00:01:00Z"
        from core.execution.tier_snapshot import tier_snapshot_hash
        changed_snapshot["tier_snapshot"]["snapshot_hash"] = tier_snapshot_hash(
            changed_snapshot["tier_snapshot"])
        self.assertNotEqual(first["execution_hash"],
                            execution_identity(changed_leverage)["execution_hash"])
        self.assertNotEqual(first["execution_hash"],
                            execution_identity(changed_snapshot)["execution_hash"])

    def test_tiered_model_reports_maintenance_margin_incompatibility(self):
        class Provider:
            snapshot_hash = "a" * 64
            account_scope = "test-account"
            def get_tier(self, pair, notional):
                return {"notional_floor_usdt": "0", "notional_cap_usdt": "100000",
                        "maintenance_margin_ratio": "0.02", "initial_leverage": 100,
                        "cumulative_maintenance_usdt": "0"}
        price, evidence = _tiered_liquidation_price(
            "BTC/USDT:USDT", 100.0, 1, 50, 5_000.0, Provider())
        self.assertFalse(evidence["tier_leverage_exceeded"])
        self.assertTrue(evidence["maintenance_margin_incompatible"])
        self.assertEqual(evidence["tier_rejection_reason"],
                         "maintenance_margin_incompatible")
        self.assertAlmostEqual(price, 100.0 * (1 - 1 / 50))

    def test_tiered_success_records_tier_on_fill_and_position(self):
        self.spec = copy.deepcopy(self.spec)
        self.spec["liquidation_model"] = "binance_tiered"
        self.state = initial_state(self.spec, BAR_MS)
        opened, events = self.enter((1, None))
        filled = next(item["payload"] for item in events
                      if item["event_type"] == "order_filled")
        position = opened["positions"][PAIRS[0]]
        self.assertEqual(filled["liquidation_model"], "binance_tiered")
        self.assertIsInstance(filled["liquidation_tier"], dict)
        self.assertIsNone(filled["liquidation_tier"]["tier_rejection_reason"])
        self.assertEqual(filled["liquidation_tier"]["tier_max_leverage"], 125)
        self.assertEqual(position["liquidation_model"], "binance_tiered")

    def test_non_atomic_tiered_entries_are_rejected_per_order(self):
        spec = copy.deepcopy(self.spec)
        spec["liquidation_model"] = "binance_tiered"
        self.spec = spec
        pending, _ = self.step(BAR_MS, (1, -1), state=initial_state(spec, BAR_MS))
        starting_fees = pending["fees_paid"]

        def tiered(pair, entry_price, direction, leverage, notional, tier_provider=None):
            rejected = pair == PAIRS[1]
            return entry_price * (0.99 if direction == 1 else 1.01), {
                "tier_max_leverage": 75 if rejected else 125,
                "tier_leverage_exceeded": rejected,
                "maintenance_margin_incompatible": False,
                "tier_snapshot_hash": spec["tier_snapshot"]["snapshot_hash"],
                "account_scope": "test-account",
                "tier_evidence_sha256": "c" * 64,
                "tier_rejection_reason": ("tier_leverage_exceeded" if rejected else None),
            }

        with patch("strategy_simulation._tiered_liquidation_price", side_effect=tiered):
            opened, events = self.step(2 * BAR_MS, state=pending)
        self.assertEqual(set(opened["positions"]), {PAIRS[0]})
        rejected = [item["payload"] for item in events
                    if item["event_type"] == "order_rejected"]
        self.assertEqual([item["pair"] for item in rejected], [PAIRS[1]])
        self.assertEqual(rejected[0]["group_rejection_reason"],
                         "tier_leverage_exceeded")
        self.assertEqual(rejected[0]["tier_snapshot_hash"],
                         spec["tier_snapshot"]["snapshot_hash"])
        self.assertEqual(rejected[0]["account_scope"], "test-account")
        self.assertEqual(rejected[0]["tier_evidence_sha256"], "c" * 64)
        filled = [item["payload"] for item in events
                  if item["event_type"] == "order_filled"]
        self.assertEqual([item["pair"] for item in filled], [PAIRS[0]])
        self.assertAlmostEqual(opened["fees_paid"] - starting_fees,
                               opened["positions"][PAIRS[0]]["entry_fee"])

    def test_tiered_entry_rejects_before_cash_or_position_mutation(self):
        spec = copy.deepcopy(self.spec)
        spec["liquidation_model"] = "binance_tiered"
        self.spec = spec
        pending, _ = self.step(BAR_MS, (1, None), state=initial_state(spec, BAR_MS))
        with patch(
            "strategy_simulation._tiered_liquidation_price",
            return_value=(99.0, {
                "tier_max_leverage": 75,
                "tier_leverage_exceeded": True,
                "maintenance_margin_incompatible": False,
                "tier_snapshot_hash": spec["tier_snapshot"]["snapshot_hash"],
                "account_scope": "test-account",
                "tier_evidence_sha256": "c" * 64,
                "tier_rejection_reason": "tier_leverage_exceeded",
            }),
        ):
            refused, events = self.step(2 * BAR_MS, state=pending)
        self.assertFalse(refused["positions"])
        self.assertAlmostEqual(refused["cash"], 10_000.0)
        self.assertEqual(refused["fees_paid"], pending["fees_paid"])
        self.assertFalse(any(item["event_type"] in {"order_filled", "position_opened"}
                             for item in events))
        rejected = [item["payload"] for item in events if item["event_type"] == "order_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["reason"], "tier_leverage_exceeded")
        self.assertEqual(rejected[0]["liquidation_tier"]["tier_max_leverage"], 75)
        self.assertEqual(rejected[0]["tier_snapshot_hash"],
                         spec["tier_snapshot"]["snapshot_hash"])
        self.assertEqual(rejected[0]["account_scope"], "test-account")
        self.assertEqual(rejected[0]["tier_evidence_sha256"], "c" * 64)

    def test_tiered_entry_provider_early_rejection_does_not_claim_unverified_provenance(self):
        spec = copy.deepcopy(self.spec)
        spec["liquidation_model"] = "binance_tiered"
        self.spec = spec
        pending, _ = self.step(BAR_MS, (1, None), state=initial_state(spec, BAR_MS))
        with patch("strategy_simulation._tier_provider_issue",
                   side_effect=[None, "tier_snapshot_hash_mismatch"]):
            refused, events = self.step(2 * BAR_MS, state=pending)

        self.assertFalse(refused["positions"])
        rejected = [item["payload"] for item in events
                    if item["event_type"] == "order_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["reason"], "tier_snapshot_unavailable")
        self.assertEqual(rejected[0]["group_rejection_reason"],
                         "tier_snapshot_hash_mismatch")
        self.assertEqual(rejected[0]["tier_snapshot_hash"],
                         spec["tier_snapshot"]["snapshot_hash"])
        self.assertNotIn("account_scope", rejected[0])
        self.assertNotIn("tier_evidence_sha256", rejected[0])
        self.assertNotIn("liquidation_tier", rejected[0])

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

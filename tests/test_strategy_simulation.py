"""Independent capital, causal proxy fills and immutable PostgreSQL audit facts."""

import copy
import hashlib
import json
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from data_store import DataStore
from runtime_config import load_environment
from strategy_simulation import (BAR_MS, PAIRS, TIMEFRAME_MS, StrategySimulation, _mark,
                                 apply_closed_batch, execution_identity, initial_state)


load_environment()


def rule_hash(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def spec(strategy_id="test-trend"):
    return {"strategy_id": strategy_id, "label": strategy_id, "timeframe": "5m",
            "universe": list(PAIRS), "params": {"n": 3}, "initial_capital": 10_000,
            "fee_rate_per_side": 0.0005}


def bars(timestamp, price=100, close=None, include_eth=True):
    close = price if close is None else close
    # Keep the generic fixture inside the 100x bankruptcy band. Tests that
    # cover liquidation provide an explicit low/high breach.
    row = {"timestamp": timestamp, "open": price, "high": max(price, close) + .5,
           "low": min(price, close) - .5, "close": close, "volume": 10,
           "extras": {"closed": True, "live_event_timestamp": timestamp + BAR_MS}}
    return {pair: [copy.deepcopy(row)] for pair in (PAIRS if include_eth else PAIRS[:1])}


def evaluation(definition, timestamp, targets=(None, None), atomic=False, ready=True):
    return {"strategy_id": definition["strategy_id"], "rule_hash": rule_hash(definition),
            "ready": ready, "warmup": {"ready": ready, "valid_bars": 300, "required_bars": 100},
            "signals": {pair: {"target_side": side, "reason": "test_signal", "candle_utc": timestamp,
                                "evidence": {"atr": 2.5}, "size_multiplier": 1}
                        for pair, side in zip(PAIRS, targets, strict=True)},
            "group": {"atomic": True, "legs": list(PAIRS)} if atomic else None}


class StrategySimulationTransitionTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch("strategy_simulation._rule_hash", side_effect=rule_hash)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.spec = spec()
        self.state = initial_state(self.spec, BAR_MS)

    def step(self, timestamp, targets=(None, None), *, state=None, candle=None, atomic=False,
             boundary=BAR_MS, ready=True, delay=1000):
        return apply_closed_batch(state or self.state, self.spec,
                                  candle or bars(timestamp), evaluation(self.spec, timestamp, targets, atomic, ready),
                                  timestamp + BAR_MS + delay, boundary)

    def enter(self, targets=(1, None), *, atomic=False):
        pending, _ = self.step(BAR_MS, targets, atomic=atomic)
        return self.step(2 * BAR_MS, state=pending, atomic=atomic)

    def test_signal_close_creates_intent_only_next_open_fills(self):
        pending, events = self.step(BAR_MS, (1, None))
        self.assertEqual(pending["positions"], {})
        self.assertEqual(pending["cash"], 10_000)
        self.assertEqual(len(pending["pending_orders"]), 1)
        self.assertEqual([e["event_type"] for e in events], ["decision", "order_submitted", "equity_snapshot"])
        opened, events = self.step(2 * BAR_MS, state=pending, candle=bars(2 * BAR_MS, price=110))
        position = opened["positions"][PAIRS[0]]
        self.assertEqual(position["entry_price"], 110)
        self.assertEqual(position["opened_candle_ms"], 2 * BAR_MS)
        self.assertEqual(position["entry_evidence"], {"atr": 2.5})
        self.assertEqual(position["entry_atr"], 2.5)
        fill = next(e["payload"] for e in events if e["event_type"] == "order_filled")
        self.assertTrue(fill["simulation_fill_model"])
        self.assertIsNone(fill["exchange_order_id"])
        self.assertEqual(fill["fill_model"], "next_closed_bar_open_proxy")

    def test_native_15m_bar_uses_same_risk_cap_and_next_open_fill(self):
        bar_ms = TIMEFRAME_MS["15m"]
        definition = copy.deepcopy(self.spec)
        definition["strategy_id"] = "test-native-15m"
        definition["timeframe"] = "15m"
        state = initial_state(definition, bar_ms)

        def candle_batch(timestamp):
            row = {"timestamp": timestamp, "open": 100.0, "high": 100.5,
                   "low": 99.5, "close": 100.0, "volume": 10,
                   "extras": {"closed": True, "live_event_timestamp": timestamp + bar_ms}}
            return {pair: [copy.deepcopy(row)] for pair in PAIRS}

        def signal(timestamp):
            return {"strategy_id": definition["strategy_id"],
                    "rule_hash": rule_hash(definition), "ready": True,
                    "warmup": {"ready": True, "valid_bars": 400,
                                "required_bars": 400},
                    "signals": {PAIRS[0]: {"target_side": 1, "candle_utc": timestamp,
                                             "reason": "test_signal", "size_multiplier": 1},
                                PAIRS[1]: {"target_side": None, "candle_utc": timestamp,
                                            "reason": "test_signal", "size_multiplier": 1}},
                    "group": None}

        state, _ = apply_closed_batch(state, definition, candle_batch(bar_ms),
                                      signal(bar_ms), 2 * bar_ms + 1000, bar_ms)
        state, events = apply_closed_batch(state, definition, candle_batch(2 * bar_ms),
                                           signal(2 * bar_ms), 3 * bar_ms + 1000, bar_ms)
        position = state["positions"][PAIRS[0]]
        self.assertEqual(state["last_candle_ms"], 2 * bar_ms)
        self.assertAlmostEqual(position["quantity"] * position["entry_price"],
                               position["margin"] * 100)
        self.assertLessEqual(position["notional"] * (0.005 + 0.0004), 50.0 + 1e-8)
        self.assertTrue(any(item["event_type"] == "order_filled" for item in events))

    def test_fees_reduce_equity_at_entry_closed_pnl_only_at_exit(self):
        opened, _ = self.enter()
        position = opened["positions"][PAIRS[0]]
        self.assertAlmostEqual(opened["realized_pnl"], 0)
        self.assertAlmostEqual(_mark(opened)["equity"], 10_000 - position["entry_fee"])
        self.assertLessEqual(_mark(opened)["margin_used"], self.spec["initial_capital"] * .30)
        pending, _ = self.step(3 * BAR_MS, (0, None), state=opened)
        exited, events = self.step(4 * BAR_MS, state=pending, candle=bars(4 * BAR_MS, price=105))
        trade = next(e["payload"] for e in events if e["event_type"] == "trade_closed")
        gross = position["quantity"] * 5
        fees = position["entry_fee"] + position["quantity"] * 105 * .0005
        self.assertAlmostEqual(trade["gross_pnl"], gross)
        self.assertAlmostEqual(trade["net_pnl"], gross - fees)
        self.assertAlmostEqual(exited["cash"], 10_000 + gross - fees)
        self.assertAlmostEqual(exited["realized_pnl"], gross - fees)
        self.assertEqual(exited["closed_trade_count"], 1)
        self.assertFalse(exited["positions"])

    def test_short_pnl_uses_direction_and_same_fee_accounting(self):
        opened, _ = self.enter((-1, None))
        position = opened["positions"][PAIRS[0]]
        pending, _ = self.step(3 * BAR_MS, (0, None), state=opened)
        exited, events = self.step(4 * BAR_MS, state=pending, candle=bars(4 * BAR_MS, price=95))
        trade = next(e["payload"] for e in events if e["event_type"] == "trade_closed")
        self.assertAlmostEqual(trade["gross_pnl"], position["quantity"] * 5)
        self.assertGreater(exited["cash"], 10_000)

    def test_repeated_bar_is_idempotent_even_post_fill_signal_differs(self):
        opened, _ = self.enter()
        repeated, events = self.step(2 * BAR_MS, (0, None), state=opened)
        self.assertEqual(events, [])
        self.assertEqual(repeated["positions"], opened["positions"])
        self.assertEqual(repeated["fees_paid"], opened["fees_paid"])

    def test_candle_revision_and_out_of_order_are_rejected(self):
        opened, _ = self.enter()
        with self.assertRaisesRegex(ValueError, "changed"):
            self.step(2 * BAR_MS, state=opened, candle=bars(2 * BAR_MS, close=101))
        with self.assertRaisesRegex(ValueError, "out-of-order"):
            self.step(BAR_MS, state=opened)

    def test_rule_changes_require_new_identity(self):
        self.spec["params"]["n"] = 4
        with self.assertRaisesRegex(ValueError, "new strategy identity"):
            self.step(BAR_MS)

    def test_signal_hash_or_timestamp_mismatch_is_rejected(self):
        bad = evaluation(self.spec, BAR_MS, (1, None))
        bad["rule_hash"] = "wrong"
        with self.assertRaisesRegex(ValueError, "hash"):
            apply_closed_batch(self.state, self.spec, bars(BAR_MS), bad, 2 * BAR_MS + 1000, BAR_MS)
        bad = evaluation(self.spec, 0, (1, None))
        with self.assertRaisesRegex(ValueError, "timestamp"):
            apply_closed_batch(self.state, self.spec, bars(BAR_MS), bad, 2 * BAR_MS + 1000, BAR_MS)

    def test_restart_cancels_pending_and_does_not_backfill(self):
        pending, _ = self.step(BAR_MS, (1, None))
        result, events = self.step(2 * BAR_MS, state=pending, boundary=2 * BAR_MS)
        self.assertFalse(result["positions"])
        cancel = next(e["payload"] for e in events if e["event_type"] == "order_cancelled")
        self.assertEqual(cancel["reason"], "restart_cancel")
        self.assertEqual(result["cash"], 10_000)

    def test_missing_bar_cancels_pending_instead_of_replay(self):
        pending, _ = self.step(BAR_MS, (1, None))
        result, events = self.step(3 * BAR_MS, state=pending)
        self.assertFalse(result["positions"])
        cancel = next(e["payload"] for e in events if e["event_type"] == "order_cancelled")
        self.assertEqual(cancel["reason"], "gap_cancel")

    def test_history_and_unclosed_candles_cannot_create_orders(self):
        with self.assertRaisesRegex(ValueError, "historical warmup"):
            self.step(0, (1, None))
        values = bars(BAR_MS)
        values[PAIRS[0]][0]["extras"]["closed"] = False
        with self.assertRaisesRegex(ValueError, "explicitly closed"):
            self.step(BAR_MS, (1, None), candle=values)

    def test_stale_bar_cancels_fill_and_denies_new_entry(self):
        pending, _ = self.step(BAR_MS, (1, None))
        result, events = self.step(2 * BAR_MS, (1, None), state=pending, delay=90_001)
        self.assertFalse(result["positions"])
        self.assertFalse(result["pending_orders"])
        self.assertTrue(any(e["event_type"] == "entry_denied" for e in events))

    def test_same_direction_does_not_add_position(self):
        opened, _ = self.enter()
        result, _ = self.step(3 * BAR_MS, (1, None), state=opened)
        self.assertFalse(result["pending_orders"])
        self.assertEqual(result["positions"], opened["positions"])

    def test_reversal_closes_then_opens_at_same_next_open(self):
        opened, _ = self.enter()
        pending, _ = self.step(3 * BAR_MS, (-1, None), state=opened)
        result, events = self.step(4 * BAR_MS, state=pending)
        self.assertEqual(result["closed_trade_count"], 1)
        self.assertEqual(result["positions"][PAIRS[0]]["direction"], -1)
        self.assertEqual(len([e for e in events if e["event_type"] == "order_filled"]), 2)

    def test_atomic_pair_fills_equal_notional_and_within_fee_adjusted_budget(self):
        opened, _ = self.enter((1, -1), atomic=True)
        left, right = [opened["positions"][pair] for pair in PAIRS]
        self.assertAlmostEqual(left["margin"], right["margin"])
        valuation = _mark(opened)
        self.assertLessEqual(valuation["margin_used"], 10_000 * .30 + 1e-8)
        self.assertEqual(len(opened["positions"]), 2)

    def test_aggregate_risk_cap_limits_independent_entries_on_one_candle(self):
        definition = copy.deepcopy(self.spec)
        definition.update(
            strategy_id="test-aggregate-cap",
            fee_model_version="binance-demo-commission-20261012-v1",
            fee_rate_per_side=0.0002,
            exit_fee_rate_per_side=0.0004,
            aggregate_risk_cap=True,
        )
        state = initial_state(definition, BAR_MS)

        def step(timestamp, state):
            return apply_closed_batch(
                state, definition, bars(timestamp),
                evaluation(definition, timestamp, (1, -1)),
                timestamp + BAR_MS + 1000, BAR_MS)

        state, _ = step(BAR_MS, state)
        state, _ = step(2 * BAR_MS, state)
        identity = execution_identity(definition)
        planned_loss = sum(
            position["notional"] * (
                identity["execution"]["risk_stop_pct"]
                + identity["execution"]["risk_round_trip_fee"]
            ) for position in state["positions"].values()
        )
        self.assertLessEqual(planned_loss, 50.0 + 1e-8)
        self.assertAlmostEqual(planned_loss, 50.0, places=8)

    def test_aggregate_cap_is_fail_closed_for_heterogeneous_atr_sizing(self):
        definition = copy.deepcopy(self.spec)
        definition.update(
            strategy_id="test-aggregate-cap-atr",
            params={"n": 3, "risk_stop_atr_mult": 1.5},
            risk_sizing_mode="entry_atr",
            max_loss_per_trade_usdt=50.0,
            aggregate_risk_cap=True,
        )
        with self.assertRaisesRegex(ValueError, "supports only fixed-stop regular"):
            execution_identity(definition)

    def test_atomic_missing_leg_cancels_both_orders(self):
        pending, _ = self.step(BAR_MS, (1, -1), atomic=True)
        result, events = self.step(2 * BAR_MS, state=pending,
                                  candle=bars(2 * BAR_MS, include_eth=False), atomic=True)
        self.assertFalse(result["positions"])
        cancelled = [e for e in events if e["event_type"] == "order_cancelled"]
        self.assertEqual(len(cancelled), 2)
        self.assertTrue(all(e["payload"]["reason"] == "atomic_leg_missing" for e in cancelled))

    def test_invalid_atomic_signal_rolls_back_pure_input(self):
        bad = evaluation(self.spec, BAR_MS, (1, None), atomic=True)
        del bad["signals"][PAIRS[1]]
        before = copy.deepcopy(self.state)
        with self.assertRaisesRegex(ValueError, "both explicit legs"):
            apply_closed_batch(self.state, self.spec, bars(BAR_MS), bad, 2 * BAR_MS + 1000, BAR_MS)
        self.assertEqual(self.state, before)

    def test_accounts_never_share_capital_or_mutate_input(self):
        before = copy.deepcopy(self.state)
        another = initial_state(spec("other"), BAR_MS)
        result, _ = self.enter()
        self.assertNotEqual(result["cash"], another["cash"])
        self.assertEqual(another["cash"], 10_000)
        self.assertEqual(self.state, before)

    def test_invalid_cash_and_inconsistent_account_budget_fail_closed(self):
        broken = copy.deepcopy(self.state)
        broken["cash"] = 9999
        with self.assertRaisesRegex(ValueError, "invalid account capital"):
            self.step(BAR_MS, state=broken)
        self.spec["position_budget"] = {"target_exposure": .15, "max_positions": 2}
        with self.assertRaisesRegex(ValueError, "30% margin exposure"):
            initial_state(self.spec, BAR_MS)

    def test_atomic_single_leg_signal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "opposite directions"):
            self.step(BAR_MS, (1, None), atomic=True)

    def test_budget_exhaustion_rejects_pending_entry_without_overwriting_position(self):
        opened, _ = self.enter()
        position = opened["positions"][PAIRS[0]]
        position["margin"] = 3500
        position["quantity"] = 35
        opened["cash"] = 6500 - position["entry_fee"]
        pending, _ = self.step(3 * BAR_MS, (None, 1), state=opened)
        result, events = self.step(4 * BAR_MS, state=pending)
        self.assertEqual(len(result["positions"]), 1)
        rejected = [e for e in events if e["event_type"] == "order_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["payload"]["reason"], "account_budget_exhausted")

    def test_same_open_exit_releases_budget_before_other_pair_entry(self):
        opened, _ = self.enter((None, 1))
        position = opened["positions"][PAIRS[1]]
        position["margin"], position["quantity"] = 3500, 35
        opened["cash"] = 6500 - position["entry_fee"]
        # Pair iteration submits BTC's entry before ETH's exit. All exits must
        # still release capital first when both are due at the same next open.
        pending, _ = self.step(3 * BAR_MS, (1, 0), state=opened)
        result, _ = self.step(4 * BAR_MS, state=pending)
        self.assertEqual(set(result["positions"]), {PAIRS[0]})
        self.assertEqual(result["closed_trade_count"], 1)


class StrategySimulationWarmupDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.store = MagicMock()
        self.conn = self.store._connection.return_value.__enter__.return_value
        self.state = {"status": "observing", "cash": 9870.0,
                      "positions": {PAIRS[0]: {"side": 1, "quantity": 2.0}},
                      "pending_orders": [{"pair": PAIRS[1], "target_side": -1}],
                      "last_candle_ms": BAR_MS, "observed_boundary_ms": BAR_MS,
                      "warmup": {"ready": False, "valid_bars": 27, "required_bars": 170}}
        self.conn.execute.return_value.fetchone.return_value = {
            "strategy_id": "test-native", "rule_hash": "frozen-rule",
            "state": self.state, "execution": {"execution_universe": list(PAIRS)}}
        self.store._json.side_effect = copy.deepcopy
        self.sim = StrategySimulation(self.store)
        self.signal = {"strategy_id": "test-native", "rule_hash": "frozen-rule", "ready": False,
                       "warmup": {"ready": False, "valid_bars": 46, "required_bars": 170},
                       "signals": {PAIRS[0]: {"target_side": -1}}}

    def test_diagnostic_changes_only_warmup_fields(self):
        before = copy.deepcopy(self.state)
        self.sim.update_warmup({"test-native": self.signal}, 2 * BAR_MS, diagnostic_only=True)
        expected = dict(before, warmup=copy.deepcopy(self.signal["warmup"]),
                        warmup_observed_at_ms=2 * BAR_MS)
        self.assertEqual(self.state, expected)
        self.assertEqual(self.conn.execute.call_count, 2)
        self.assertTrue(self.conn.execute.call_args.args[0].startswith("UPDATE strategy_sim_accounts"))

    def test_unchanged_diagnostic_does_not_write_or_change_observation_time(self):
        self.state["warmup"] = copy.deepcopy(self.signal["warmup"])
        self.state["warmup_observed_at_ms"] = BAR_MS
        before = copy.deepcopy(self.state)
        self.sim.update_warmup({"test-native": self.signal}, 2 * BAR_MS, diagnostic_only=True)
        self.assertEqual(self.state, before)
        self.assertEqual(self.conn.execute.call_count, 1)

    def test_diagnostic_cannot_change_rule_identity(self):
        self.signal["rule_hash"] = "changed-rule"
        before = copy.deepcopy(self.state)
        with self.assertRaisesRegex(ValueError, "warmup rule/strategy"):
            self.sim.update_warmup({"test-native": self.signal}, 2 * BAR_MS, diagnostic_only=True)
        self.assertEqual(self.state, before)
        self.assertEqual(self.conn.execute.call_count, 1)


class StrategySimulation2x2ProjectionTests(unittest.TestCase):
    """Database projections include enabled 2x2 arms without legacy leakage."""

    def setUp(self):
        self.patch = patch("strategy_simulation._rule_hash", side_effect=rule_hash)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.store = MagicMock()
        self.conn = self.store._connection.return_value.__enter__.return_value
        self.store._json.side_effect = copy.deepcopy
        self.base = spec("s05_bollinger_fade")
        self.base["position_budget"] = {"target_exposure": .30,
                                        "max_positions": 2,
                                        "leverage": 100}

    def row(self, strategy_id, *, variant=None, leverage=100, enabled=True,
            archived=False):
        definition = copy.deepcopy(self.base)
        definition["strategy_id"] = strategy_id
        definition["position_budget"]["leverage"] = leverage
        if variant is not None:
            definition.update(execution_variant=variant,
                              stop_execution_mode="intrabar",
                              execution_enabled=enabled,
                              signal_rule_hash=rule_hash(self.base))
            if not enabled:
                definition["blocked_execution_reason"] = "manual_activation_required"
        identity = __import__("strategy_simulation").execution_identity(definition)
        state = initial_state(definition, BAR_MS)
        row = {"strategy_id": strategy_id, "rule_hash": identity["rule_hash"],
               "execution_hash": identity["execution_hash"], "definition": definition,
               "execution": identity["execution"], "initial_capital": 10_000.0,
               "state": state}
        if archived:
            row["strategy_id"] = strategy_id + "_100x"
        return row

    def rows(self):
        return [
            self.row("s05_bollinger_fade_100x_maker"),
            self.row("s05_bollinger_fade_2x2_B_L30_close", variant="risk-execution-2x2-v1",
                     leverage=30),
            self.row("s05_bollinger_fade_2x2_C_L100_intrabar", variant="risk-execution-2x2-v1",
                     leverage=100),
            self.row("s05_bollinger_fade_2x2_disabled_L30_close", variant="risk-execution-2x2-v1",
                     leverage=30, enabled=False),
            self.row("s05_bollinger_fade", archived=True),
        ]

    def test_positions_by_strategy_includes_enabled_2x2_arms(self):
        self.conn.execute.return_value.fetchall.return_value = self.rows()
        result = StrategySimulation(self.store).positions_by_strategy()
        self.assertEqual(set(result), {
            "s05_bollinger_fade_100x_maker",
            "s05_bollinger_fade_2x2_B_L30_close",
            "s05_bollinger_fade_2x2_C_L100_intrabar",
        })

    def test_overview_includes_2x2_and_marks_it_active(self):
        self.conn.execute.return_value.fetchall.return_value = self.rows()
        accounts = StrategySimulation(self.store).overview()["accounts"]
        self.assertEqual({item["strategy_id"] for item in accounts}, {
            "s05_bollinger_fade_100x_maker",
            "s05_bollinger_fade_2x2_B_L30_close",
            "s05_bollinger_fade_2x2_C_L100_intrabar",
        })
        arm = next(item for item in accounts if "_2x2_B_" in item["strategy_id"])
        self.assertEqual(arm["status"], "warming_up")
        self.assertEqual(arm["execution"]["leverage"], 30)

    def test_preview_positions_projects_enabled_2x2_without_persisting(self):
        rows = self.rows()
        self.conn.execute.return_value.fetchall.return_value = rows
        projected_state = copy.deepcopy(rows[1]["state"])
        projected_state["positions"] = {PAIRS[0]: {"direction": 1, "quantity": 1.0}}
        with patch("strategy_simulation.apply_closed_batch",
                   return_value=(projected_state, [])):
            result = StrategySimulation(self.store).preview_positions(
                bars(BAR_MS), 2 * BAR_MS, BAR_MS)
        self.assertIn("s05_bollinger_fade_2x2_B_L30_close", result)
        self.assertIn(PAIRS[0], result["s05_bollinger_fade_2x2_B_L30_close"])
        self.assertNotIn("s05_bollinger_fade_2x2_disabled_L30_close", result)

    def test_disabled_ordinary_account_is_excluded_from_active_projections(self):
        """Retirement flags apply to frozen 100x rows, not only 2x2 arms."""
        rows = self.rows()
        retired = self.row("s14_vol_compression_release_B15m_R50_k1.0_100x_maker")
        retired["execution"] = dict(retired["execution"],
                                     execution_enabled=False,
                                     blocked_execution_reason="strategy_retired")
        rows.append(retired)
        self.conn.execute.return_value.fetchall.return_value = rows

        ids = {item["strategy_id"] for item in StrategySimulation(self.store).overview()["accounts"]}
        self.assertNotIn(retired["strategy_id"], ids)

        positions = StrategySimulation(self.store).positions_by_strategy()
        self.assertNotIn(retired["strategy_id"], positions)

@unittest.skipUnless(os.getenv("QUANT_TEST_DATABASE_URL"), "isolated PostgreSQL test database required")
class StrategySimulationStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import conninfo_to_dict

        test = conninfo_to_dict(os.environ["QUANT_TEST_DATABASE_URL"])
        production = conninfo_to_dict(os.environ.get("QUANT_DATABASE_URL", ""))
        def location(value):
            return tuple(value.get(k, "") for k in ("host", "port", "dbname"))
        if "test" not in test.get("dbname", "") or location(test) == location(production):
            raise RuntimeError("integration tests require an independent test database")
        cls.schema = "quant_strategy_test_" + uuid.uuid4().hex
        cls.store = DataStore(dsn=os.environ["QUANT_TEST_DATABASE_URL"], schema=cls.schema)
        cls.store.initialize()

    @classmethod
    def tearDownClass(cls):
        import psycopg
        from psycopg import sql

        cls.store.close()
        with psycopg.connect(os.environ["QUANT_TEST_DATABASE_URL"]) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        self.patch = patch("strategy_simulation._rule_hash", side_effect=rule_hash)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.spec = spec("test-" + uuid.uuid4().hex)
        with self.store._connection() as conn:
            conn.execute("TRUNCATE strategy_sim_events,strategy_sim_accounts RESTART IDENTITY")
        self.sim = StrategySimulation(self.store, clock_ms=lambda: 2 * BAR_MS)
        self.sim.ensure_accounts([self.spec], BAR_MS)

    def process(self, timestamp, targets=(None, None)):
        self.sim.process_batch(bars(timestamp), {self.spec["strategy_id"]: evaluation(self.spec, timestamp, targets)},
                               timestamp + BAR_MS + 1000, BAR_MS)

    def test_independent_initial_capital_idempotence_and_restored_positions(self):
        other = spec("test-" + uuid.uuid4().hex)
        self.sim.ensure_accounts([other], BAR_MS)
        self.process(BAR_MS, (1, None))
        self.process(2 * BAR_MS)
        detail = self.sim.account_detail(self.spec["strategy_id"])
        self.assertEqual(detail["account"]["open_position_count"], 1)
        self.assertEqual(self.sim.account_detail(other["strategy_id"])["account"]["cash"], 10_000)
        event_count = len(detail["events"])
        self.process(2 * BAR_MS)
        self.assertEqual(len(self.sim.account_detail(self.spec["strategy_id"])["events"]), event_count)
        recovered = StrategySimulation(self.store)
        recovered.ensure_accounts([self.spec], 20 * BAR_MS)
        self.assertEqual(recovered.account_detail(self.spec["strategy_id"])["account"]["open_position_count"], 1)
        self.assertNotEqual(recovered.account_detail(self.spec["strategy_id"])["account"]["cash"], 10_000)

    def test_account_and_event_immutability_at_database_level(self):
        with self.assertRaises(Exception):
            with self.store._connection() as conn:
                conn.execute("UPDATE strategy_sim_accounts SET initial_capital=10001 WHERE strategy_id=%s",
                             (self.spec["strategy_id"],))
        with self.assertRaises(Exception):
            with self.store._connection() as conn:
                conn.execute("DELETE FROM strategy_sim_events WHERE strategy_id=%s", (self.spec["strategy_id"],))
        changed = copy.deepcopy(self.spec)
        changed["params"]["n"] = 4
        with self.assertRaisesRegex(ValueError, "new strategy identity"):
            self.sim.ensure_accounts([changed], BAR_MS)

    def test_warmup_never_creates_orders_or_updates_execution_boundary(self):
        signal = evaluation(self.spec, 0)
        self.sim.update_warmup({self.spec["strategy_id"]: signal}, 2 * BAR_MS)
        detail = self.sim.account_detail(self.spec["strategy_id"])
        self.assertEqual(detail["orders"], [])
        self.assertIsNone(detail["account"]["last_candle_ms"])
        self.assertEqual(detail["account"]["observed_boundary_ms"], BAR_MS)
        self.assertEqual(detail["account"]["status"], "awaiting_new_closed_bar")

    def test_preview_is_read_only_and_new_fill_can_exit_on_same_close(self):
        self.process(BAR_MS, (1, None))
        before = self.sim.account_detail(self.spec["strategy_id"])
        projected = self.sim.preview_positions(bars(2 * BAR_MS), 3 * BAR_MS + 1000, BAR_MS)
        self.assertIn(PAIRS[0], projected[self.spec["strategy_id"]])
        self.assertEqual(self.sim.account_detail(self.spec["strategy_id"]), before)
        self.process(2 * BAR_MS, (0, None))
        detail = self.sim.account_detail(self.spec["strategy_id"])
        self.assertEqual(detail["account"]["open_position_count"], 1)
        self.assertEqual(detail["account"]["pending_order_count"], 1)
        self.process(3 * BAR_MS)
        detail = self.sim.account_detail(self.spec["strategy_id"])
        self.assertEqual(detail["account"]["open_position_count"], 0)
        self.assertEqual(detail["account"]["closed_trade_count"], 1)

    def test_failed_batch_rolls_back_all_events_and_capital(self):
        before = self.sim.account_detail(self.spec["strategy_id"])
        bad = evaluation(self.spec, BAR_MS, (True, None))
        with self.assertRaises(ValueError):
            self.sim.process_batch(bars(BAR_MS), {self.spec["strategy_id"]: bad}, 2 * BAR_MS + 1000, BAR_MS)
        self.assertEqual(self.sim.account_detail(self.spec["strategy_id"]), before)

    def test_preview_input_changes_must_reevaluate_before_commit(self):
        self.process(BAR_MS, (1, None))
        self.sim.preview_positions(bars(2 * BAR_MS), 3 * BAR_MS + 1000, BAR_MS)
        with self.assertRaisesRegex(ValueError, "after fill preview"):
            self.sim.process_batch(bars(2 * BAR_MS, price=110),
                {self.spec["strategy_id"]: evaluation(self.spec, 2 * BAR_MS)},
                3 * BAR_MS + 1000, BAR_MS)
        self.assertEqual(self.sim.account_detail(self.spec["strategy_id"])["account"]["open_position_count"], 0)

    def test_concurrent_duplicate_batches_do_not_charge_fee_twice(self):
        self.process(BAR_MS, (1, None))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.process, 2 * BAR_MS) for _ in range(2)]
            for future in futures:
                future.result()
        detail = self.sim.account_detail(self.spec["strategy_id"])
        self.assertEqual(detail["account"]["open_position_count"], 1)
        fills = [item for item in detail["orders"] if item["status"] == "filled"]
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(detail["account"]["fees_paid"], fills[0]["fee"])

    def test_all_real_specs_evaluate_preview_commit_and_recover_without_history_fills(self):
        from strategy_lab_service import load_specs
        from strategy_signals import evaluate, strategy_rule_hash

        definitions = load_specs()
        # The research catalog is extensible (generation-2/3 strategies are
        # loaded alongside the original set).  Validate the service contract
        # without freezing the test to an obsolete catalog size.
        self.assertGreaterEqual(len(definitions), 10)
        # Native 15m/1h/1d arms are driven by StrategyLabService's own data
        # adapter.  This fixture intentionally contains only synthetic 5m
        # rows, so it must not pretend those accounts are warmed or executable.
        definitions = [item for item in definitions if item.get("timeframe", "5m") == "5m"]
        if not definitions:
            self.assertEqual(definitions, [])
            return
        with self.store._connection() as conn:
            conn.execute("TRUNCATE strategy_sim_events,strategy_sim_accounts RESTART IDENTITY")
        boundary = 750 * BAR_MS
        history = {pair: [] for pair in PAIRS}
        for index in range(750):
            price = 100 + index * .001
            for pair in PAIRS:
                row = bars(index * BAR_MS, price=price, close=price + .001)[pair][0]
                row["extras"].update(taker_buy_base=8, quote_volume=price * 10, num_trades=5)
                history[pair].append(row)
        with patch("strategy_simulation._rule_hash", side_effect=strategy_rule_hash):
            self.sim.ensure_accounts(definitions, boundary)
            warmup = {item["strategy_id"]: evaluate(item, history, positions={}, as_of_utc=boundary)
                      for item in definitions}
            self.assertTrue(all(value["ready"] for value in warmup.values()))
            self.sim.update_warmup(warmup, boundary)
            self.assertTrue(all(account["cash"] == 10_000 and account["open_position_count"] == 0
                                for account in self.sim.overview()["accounts"]))
            self.assertTrue(all(self.sim.account_detail(item["strategy_id"])["orders"] == []
                                for item in definitions))
            for index in range(750, 754):
                timestamp = index * BAR_MS
                observed = timestamp + BAR_MS + 1000
                for pair in PAIRS:
                    row = bars(timestamp, price=100 + index * .001,
                               close=100 + index * .001 + .001)[pair][0]
                    row["extras"].update(taker_buy_base=8, quote_volume=row["close"] * 10, num_trades=5)
                    history[pair].append(row)
                projected = self.sim.preview_positions(history, observed, boundary)
                evaluations = {item["strategy_id"]: evaluate(item, history,
                        positions=projected[item["strategy_id"]], as_of_utc=observed) for item in definitions}
                self.sim.process_batch(history, evaluations, observed, boundary)
            accounts = self.sim.overview()["accounts"]
            self.assertEqual(len(accounts), len(definitions))
            self.assertTrue(all(account["initial_capital"] == 10_000 for account in accounts))
            self.assertTrue(all(account["last_candle_ms"] == 753 * BAR_MS for account in accounts))
            self.assertTrue(any(account["open_position_count"] for account in accounts))
            self.assertTrue(all(account["warmup"]["ready"] for account in accounts))
            for definition in definitions:
                detail = self.sim.account_detail(definition["strategy_id"])
                self.assertTrue(all(order["signal_candle_ms"] >= boundary for order in detail["orders"]))
                self.assertTrue(all(order["exchange_order_id"] is None for order in detail["orders"]))
            restored = StrategySimulation(self.store)
            before = restored.overview()
            restored.ensure_accounts(definitions, boundary + 10 * BAR_MS)
            self.assertEqual(restored.overview(), before)


if __name__ == "__main__":
    unittest.main()

"""Factor populations share input reads without sharing execution progress."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock

from strategy_factorial import BTC, DESIGN_VERSION, ETH, TRX
from strategy_factorial_signals import normalize_factorial_spec
from strategy_lab_service import StrategyLabService
from strategy_signals import LEGACY_DEFAULTS, normalize_spec
from strategy_simulation import StrategySimulation, execution_identity, initial_state


HOUR = 3_600_000
BOUNDARY = 800 * HOUR
PAUSED = "DOGE/USDT:USDT"


def candle(timestamp):
    return {"timestamp": timestamp, "open": 100.0, "high": 100.1,
            "low": 99.9, "close": 100.0, "volume": 100.0,
            "extras": {"closed": True, "taker_buy_base": 50.0}}


def factor_spec(*, risk=50, k=1.5, pairs=(BTC, ETH), canonical="s16_weekly_seasonality",
                timeframe="1h", data_pairs=None):
    number = int(canonical[1:3])
    arm = "B15m" if number <= 11 else "Cgen2" if number <= 21 else "Dgen3"
    params = {**LEGACY_DEFAULTS[canonical][0], "atr_window": 14, "risk_stop_atr_mult": k}
    return normalize_factorial_spec({
        "strategy_id": f"{canonical}_{arm}_R{risk}_k{k:.1f}_100x_maker",
        "canonical_strategy_id": canonical, "factorial_design_version": DESIGN_VERSION,
        "timeframe": timeframe, "universe": list(pairs),
        "execution_universe": list(pairs), "research_universe": list(data_pairs or pairs),
        "data_universe": list(data_pairs or pairs), "params": params,
        "min_warmup_bars": 1, "risk_sizing_mode": "entry_atr",
        "max_loss_per_trade_usdt": risk, "position_budget": {"leverage": 100},
    })


class CandleStore:
    """Read-only in-memory database fixture; every query is observable."""

    def __init__(self):
        self.rows = {
            (pair, "1h"): [candle(index * HOUR) for index in range(51, 801)]
            for pair in (BTC, ETH, TRX)
        }
        self.queries = []

    def get_candles(self, exchange, market, pair, timeframe, candle_type, *, limit, end=None):
        self.queries.append((pair, timeframe))
        if pair == PAUSED:
            raise AssertionError("paused assets must never be fetched")
        rows = self.rows.get((pair, timeframe), [])
        return copy.deepcopy([row for row in rows if end is None or row["timestamp"] <= end][-limit:])


class FactorialServiceTests(unittest.TestCase):
    def service(self, specs, store=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        simulation = Mock()
        simulation.preview_positions.return_value = {}
        return StrategyLabService(store or CandleStore(), simulation, specs,
                                  now_ms=BOUNDARY,
                                  status_path=Path(temporary.name) / "status.json")

    def test_nine_factors_read_each_shared_native_input_once_per_cycle(self):
        specs = [factor_spec(risk=risk, k=k)
                 for risk in (50, 200, 1000) for k in (1.0, 1.5, 2.5)]
        service = self.service(specs)
        now = BOUNDARY + HOUR + 1000
        service.cycle(now)
        self.assertEqual(service.store.queries, [(BTC, "1h"), (ETH, "1h")])
        self.assertEqual(service.simulation.process_batch.call_count, 9)
        self.assertEqual(set(service.native_account_last_processed),
                         {spec["strategy_id"] for spec in specs})

        # A new observation must fetch again; a cached previous cycle cannot
        # hide a newly received close or a newly filled history gap.
        for rows in service.store.rows.values():
            rows.append(candle(BOUNDARY + HOUR))
        service.cycle(now + HOUR)
        self.assertEqual(service.store.queries,
                         [(BTC, "1h"), (ETH, "1h"), (BTC, "1h"), (ETH, "1h")])
        self.assertEqual(service.simulation.process_batch.call_count, 18)
        self.assertTrue(all(stamp == BOUNDARY + HOUR
                            for stamp in service.native_account_last_processed.values()))

    def test_input_cache_separates_data_universe_and_requested_window(self):
        two = factor_spec()
        three = factor_spec(risk=200, pairs=(BTC, ETH, TRX))
        service = self.service([two, three])
        now = BOUNDARY + HOUR + 1000
        first = service.load_strategy_inputs(two, now_ms=now, limit=40)
        second = service.load_strategy_inputs(two, now_ms=now, limit=40)
        self.assertIs(first, second)
        self.assertEqual(service.store.queries, [(BTC, "1h"), (ETH, "1h")])
        third = service.load_strategy_inputs(three, now_ms=now, limit=40)
        self.assertEqual(set(third), {BTC, ETH, TRX})
        longer = service.load_strategy_inputs(two, now_ms=now, limit=50)
        self.assertEqual(len(longer[BTC]), 50)
        self.assertEqual(len(first[BTC]), 40)
        self.assertEqual(len(service.store.queries), 7)

    def test_late_trx_processes_its_account_once_without_replaying_btc_eth(self):
        two = factor_spec()
        three = factor_spec(risk=200, pairs=(BTC, ETH, TRX))
        service = self.service([two, three])
        service.store.rows[(TRX, "1h")].pop()
        now = BOUNDARY + HOUR + 1000
        service.cycle(now)
        first_preview = service.simulation.preview_positions.call_args
        self.assertEqual(set(first_preview.args[0]), {BTC, ETH})
        self.assertEqual(first_preview.kwargs["strategy_ids"], [two["strategy_id"]])
        self.assertEqual(service.native_account_last_processed, {two["strategy_id"]: BOUNDARY})

        service.store.rows[(TRX, "1h")].append(candle(BOUNDARY))
        service.cycle(now + 10_000)
        second_preview = service.simulation.preview_positions.call_args
        self.assertEqual(set(second_preview.args[0]), {BTC, ETH, TRX})
        self.assertEqual(second_preview.kwargs["strategy_ids"], [three["strategy_id"]])
        batches = service.simulation.process_batch.call_args_list
        self.assertEqual([next(iter(call.args[1])) for call in batches],
                         [two["strategy_id"], three["strategy_id"]])
        self.assertEqual([set(call.args[0]) for call in batches],
                         [{BTC, ETH}, {BTC, ETH, TRX}])
        self.assertEqual(service.native_account_last_processed,
                         {two["strategy_id"]: BOUNDARY, three["strategy_id"]: BOUNDARY})
        service.cycle(now + 20_000)
        self.assertEqual(service.simulation.preview_positions.call_count, 2)
        self.assertEqual(service.simulation.process_batch.call_count, 2)

    def test_missing_trx_does_not_project_or_process_an_incomplete_account(self):
        two = factor_spec()
        three = factor_spec(risk=200, pairs=(BTC, ETH, TRX))
        service = self.service([two, three])
        service.store.rows[(TRX, "1h")] = []
        service.cycle(BOUNDARY + HOUR + 1000)
        preview = service.simulation.preview_positions.call_args
        self.assertEqual(preview.kwargs["strategy_ids"], [two["strategy_id"]])
        self.assertEqual(set(preview.args[0]), {BTC, ETH})
        service.simulation.process_batch.assert_called_once()
        self.assertEqual(set(service.simulation.process_batch.call_args.args[1]),
                         {two["strategy_id"]})
        self.assertNotIn(three["strategy_id"], service.native_account_last_processed)

    def test_blocked_and_paused_research_do_not_fetch_data_or_preview_accounts(self):
        blocked = factor_spec(canonical="s04_btc_eth_pairs", timeframe="15m")
        paused = normalize_spec({"strategy_id": "s13_daily_trend_multi_horizon",
                                 "position_budget": {"leverage": 100}})
        service = self.service([blocked, paused])
        self.assertIs(blocked["execution_enabled"], False)
        self.assertEqual(service.load_strategy_inputs(blocked, now_ms=BOUNDARY), {})
        self.assertEqual(service.load_strategy_inputs(paused, now_ms=BOUNDARY), {})
        evaluations = {}
        service._native_bootstrap(evaluations)
        service.cycle(BOUNDARY + HOUR + 1000)
        self.assertEqual(service.store.queries, [])
        self.assertEqual(evaluations[blocked["strategy_id"]]["warmup"]["reason"],
                         "paired_atr_stop_policy_unimplemented")
        self.assertEqual(evaluations[paused["strategy_id"]]["warmup"]["reason"],
                         "paused_research_context_required")
        service.simulation.preview_positions.assert_not_called()
        service.simulation.process_batch.assert_not_called()

    def test_factor_data_universe_ignores_paused_original_research_constituents(self):
        spec = factor_spec()
        spec["source_original_universe"] = [BTC, ETH, PAUSED]
        service = self.service([spec])
        loaded = service.load_strategy_inputs(spec, now_ms=BOUNDARY + HOUR + 1000)
        self.assertEqual(set(loaded), {BTC, ETH})
        self.assertEqual(service.store.queries, [(BTC, "1h"), (ETH, "1h")])

    def test_real_preview_selects_exact_trade_legs_and_skips_unselected_broken_account(self):
        two = factor_spec()
        three = factor_spec(risk=200, pairs=(BTC, ETH, TRX))
        rows = []
        for spec in (two, three):
            state = initial_state(spec, BOUNDARY)
            rows.append({"strategy_id": spec["strategy_id"], "definition": spec,
                         "rule_hash": state["rule_hash"], "state": state,
                         "execution": execution_identity(spec)["execution"]})
        rows.append({"strategy_id": "unselected-account", "definition": {"invalid": True}})
        before = copy.deepcopy(rows)
        store = MagicMock()
        connection = store._connection.return_value.__enter__.return_value
        connection.execute.return_value.fetchall.return_value = rows
        simulation = StrategySimulation(store)
        candles = {pair: [candle(BOUNDARY)] for pair in (BTC, ETH, TRX)}
        projected = simulation.preview_positions(
            candles, BOUNDARY + HOUR + 1000, BOUNDARY, timeframe="1h",
            strategy_ids=[two["strategy_id"], three["strategy_id"]])
        self.assertEqual(projected, {two["strategy_id"]: {}, three["strategy_id"]: {}})
        self.assertEqual(set(simulation._preview_states),
                         {two["strategy_id"], three["strategy_id"]})
        self.assertNotEqual(simulation._preview_states[two["strategy_id"]]["candle_hash"],
                            simulation._preview_states[three["strategy_id"]]["candle_hash"])
        self.assertEqual(rows, before)
        connection.execute.assert_called_once()
        self.assertTrue(connection.execute.call_args.args[0].lstrip().startswith("SELECT"))


if __name__ == "__main__":
    unittest.main()

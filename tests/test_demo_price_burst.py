"""Offline causal and persistence contracts for the Demo burst event source."""
import copy
from contextlib import contextmanager
import threading
import unittest

from demo_price_burst import (
    BAR_MS, BURST_DEFINITION_KEY, BURST_EVENT_PREFIX, BURST_PAIRS,
    BURST_STATE_KEY, BURST_STRATEGY_ID, PriceBurstEventSource,
)


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return copy.deepcopy(self.rows[0]) if self.rows else None

    def fetchall(self):
        return copy.deepcopy(self.rows)


class Connection:
    """A strict SQL fake: unrelated tables (especially future labels) fail."""
    def __init__(self, store):
        self.store = store

    def execute(self, query, params=()):
        query = " ".join(query.split())
        self.store.queries.append(query)
        if query.startswith("SELECT pg_advisory_xact_lock"):
            return Rows([])
        if query.startswith("SELECT payload FROM documents WHERE key=%s"):
            payload = self.store.documents.get(params[0])
            return Rows([] if payload is None else [{"payload": payload}])
        if query.startswith("INSERT INTO documents"):
            key, payload = params
            if key == BURST_STATE_KEY and self.store.fail_checkpoint_writes:
                raise RuntimeError("simulated checkpoint commit failure")
            if "DO NOTHING" not in query or key not in self.store.documents:
                self.store.documents[key] = copy.deepcopy(payload)
            return Rows([])
        if query.startswith("SELECT payload FROM documents WHERE left(key,%s)=%s"):
            length, prefix, after, limit = params
            keys = sorted(key for key in self.store.documents
                          if key[:length] == prefix and key > after)
            return Rows([{"payload": self.store.documents[key]} for key in keys[:limit]])
        if query.startswith("SELECT timestamp,open,high,low,close,volume,extras FROM candles"):
            pair = params[0]
            rows = [row for row in self.store.candles.get(pair, [])
                    if row.get("extras", {}).get("closed") is True
                    and row.get("extras", {}).get("live_event_timestamp") is not None]
            if "timestamp>=%s" in query:
                rows = [row for row in rows if params[1] <= row["timestamp"] <= params[2]]
                return Rows(sorted(rows, key=lambda row: row["timestamp"]))
            rows = [row for row in rows if row["timestamp"] <= params[1]
                    and row["timestamp"] % params[2] == 0]
            if "AND timestamp=%s" in query:
                rows = [row for row in rows if row["timestamp"] == params[3]]
            return Rows(sorted(rows, key=lambda row: row["timestamp"], reverse=True)[:1])
        raise AssertionError("unexpected query: " + query)


class MemoryStore:
    schema = "offline_test"

    def __init__(self):
        self.documents, self.candles, self.queries = {}, {}, []
        self.fail_checkpoint_writes = False
        self.lock = threading.RLock()

    @staticmethod
    def _json(payload):
        return copy.deepcopy(payload)

    @contextmanager
    def _connection(self):
        with self.lock:
            before = copy.deepcopy(self.documents)
            try:
                yield Connection(self)
            except Exception:
                self.documents = before
                raise


def candle(index, close=100, *, high=None, low=None):
    return {"timestamp": index * BAR_MS, "open": 100,
            "high": max(101, close) if high is None else high,
            "low": min(99, close) if low is None else low,
            "close": close, "volume": 10,
            "extras": {"closed": True, "live_event_timestamp": (index + 1) * BAR_MS + 1}}


class BurstTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.pair = BURST_PAIRS[0]
        self.store.candles[self.pair] = [candle(index) for index in range(16)]
        self.now = 15 * BAR_MS + 10  # decision bar 15 is still open at bootstrap
        self.source = PriceBurstEventSource(self.store, clock_ms=lambda: self.now)
        self.source.ensure_account()

    def signal(self, close=101.5, *, previous=None):
        if previous is not None:
            self.store.candles[self.pair][-2] = candle(14, previous)
        self.store.candles[self.pair][-1] = candle(15, close)
        self.now = 16 * BAR_MS + 10
        return self.source.events(BURST_STRATEGY_ID, 0)

    def entry_documents(self):
        return {key: value for key, value in self.store.documents.items()
                if key.startswith(BURST_EVENT_PREFIX)}

    def test_account_read_does_not_bootstrap_or_mutate(self):
        empty = MemoryStore()
        source = PriceBurstEventSource(empty, clock_ms=lambda: self.now)
        with self.assertRaisesRegex(ValueError, "not been bootstrapped"):
            source.account(BURST_STRATEGY_ID)
        self.assertEqual(empty.documents, {})
        self.assertTrue(all(query.startswith("SELECT") for query in empty.queries))
        before = copy.deepcopy(self.store.documents)
        self.source.account(BURST_STRATEGY_ID)
        self.assertEqual(self.store.documents, before)

    def test_account_definition_risk_and_execution_are_frozen_and_honest(self):
        account = self.source.account(BURST_STRATEGY_ID)
        self.assertEqual(account["state"]["positions"], {})
        self.assertFalse(account["definition"]["paper_mirror"])
        self.assertTrue(account["definition"]["not_profit_claim"])
        self.assertEqual(account["definition"]["params"]["move_threshold"], 0.015)
        self.assertEqual(account["execution"]["max_loss_per_trade_usdt"], 50)
        self.assertEqual(account["execution"]["max_hold_ms"], 3 * BAR_MS)
        self.assertEqual(account["execution"]["hold_clock"], "first_actual_entry_fill_ms")
        self.assertEqual(account["execution"]["execution_universe"], list(BURST_PAIRS))
        first = copy.deepcopy(account)
        self.now += BAR_MS
        self.assertEqual(self.source.ensure_account(), first)
        account["definition"]["params"]["move_threshold"] = 0.001
        self.assertEqual(self.source.account(BURST_STRATEGY_ID), first)

    def test_exact_thresholds_inclusive_and_direction_is_continuation(self):
        for close, expected in ((101.5, "buy"), (98.5, "sell")):
            with self.subTest(close=close):
                self.setUp()
                events = self.signal(close)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["payload"]["buy_sell"], expected)
                self.assertEqual(events[0]["payload"]["kind"], "entry")
                self.assertEqual(events[0]["observed_at_ms"], 16 * BAR_MS)
                self.assertEqual(events[0]["candle_ms"], 15 * BAR_MS)
                self.assertEqual(events[0]["payload"]["signal_candle_ms"], 15 * BAR_MS)
                self.assertEqual(events[0]["payload"]["evidence"]["entry_atr"], "2")

    def test_just_inside_threshold_does_not_become_entry(self):
        for close in (101.499999999, 98.500000001, 100):
            with self.subTest(close=close):
                self.setUp()
                self.assertEqual(self.signal(close), [])
                self.assertEqual(self.store.documents[BURST_STATE_KEY]["sequence_id"], 0)
                self.assertEqual(self.store.documents[BURST_STATE_KEY]["pair_checkpoints"][self.pair],
                                 15 * BAR_MS)

    def test_same_direction_episode_does_not_repeat_entry(self):
        self.assertEqual(self.signal(102, previous=101.5), [])
        self.assertEqual(self.store.documents[BURST_STATE_KEY]["observations"][self.pair]["reason"],
                         "no_new_directional_burst")

    def test_opposite_prior_burst_can_cross_into_new_direction(self):
        events = self.signal(101.5, previous=98.5)
        self.assertEqual(events[0]["payload"]["buy_sell"], "buy")
        self.assertEqual(events[0]["payload"]["evidence"]["previous_direction"], "sell")

    def test_open_and_future_candles_cannot_signal_or_change_fingerprint(self):
        self.store.candles[self.pair][-1] = candle(15, 120)
        self.assertEqual(self.source.events(BURST_STRATEGY_ID, 0), [])
        self.store.candles[self.pair].append(candle(16, 1))
        events = self.signal()
        expected = events[0]["data_fingerprint"]
        self.store.candles[self.pair][-1] = candle(16, 99999)
        self.assertEqual(self.source.events(BURST_STRATEGY_ID, 0)[0]["data_fingerprint"], expected)
        self.assertFalse(any("candidate" in query or "outcome" in query for query in self.store.queries))

    def test_explicitly_unclosed_candle_is_not_a_signal(self):
        self.store.candles[self.pair][-1] = candle(15, 120)
        self.store.candles[self.pair][-1]["extras"]["closed"] = False
        self.now = 16 * BAR_MS + 10
        self.assertEqual(self.source.events(BURST_STRATEGY_ID, 0), [])
        self.assertIsNone(self.source.closed_candle(self.pair, "5m", 15 * BAR_MS))

    def test_missing_live_bar_rejects_and_repair_never_retroactively_emits(self):
        missing = self.store.candles[self.pair].pop(5)
        self.assertEqual(self.signal(), [])
        self.assertEqual(self.store.documents[BURST_STATE_KEY]["observations"][self.pair]["reason"],
                         "missing_contiguous_live_candles")
        self.store.candles[self.pair].append(missing)
        self.assertEqual(self.source.events(BURST_STRATEGY_ID, 0), [])

    def test_history_without_live_provenance_cannot_fill_gap(self):
        self.store.candles[self.pair][5]["extras"].pop("live_event_timestamp")
        self.assertEqual(self.signal(), [])

    def test_invalid_price_range_and_zero_atr_deny_entry(self):
        self.store.candles[self.pair][5]["low"] = 101
        self.assertEqual(self.signal(), [])
        self.assertEqual(self.store.documents[BURST_STATE_KEY]["observations"][self.pair]["reason"],
                         "invalid_live_candle")
        self.setUp()
        self.store.candles[self.pair] = [candle(index, high=100, low=100) for index in range(16)]
        self.assertEqual(self.signal(), [])
        self.assertEqual(self.store.documents[BURST_STATE_KEY]["observations"][self.pair]["reason"],
                         "entry_atr_unavailable")

    def test_atr_uses_prior_true_ranges_not_large_decision_wick(self):
        self.store.candles[self.pair][5] = candle(5, high=105, low=95)
        self.store.candles[self.pair][-1] = candle(15, 101.5, high=200)
        self.now = 16 * BAR_MS + 10
        event = self.source.events(BURST_STRATEGY_ID, 0)[0]
        self.assertAlmostEqual(float(event["payload"]["evidence"]["entry_atr"]), (13 * 2 + 10) / 14)

    def test_bootstrap_and_runner_anchor_never_create_preexisting_burst(self):
        self.now = 16 * BAR_MS + 10
        self.store.candles[self.pair][-1] = candle(15, 101.5)
        self.assertEqual(self.source.latest_sequence(BURST_STRATEGY_ID), 0)
        self.assertEqual(self.source.events(BURST_STRATEGY_ID, 0), [])
        self.store.documents = {}
        self.source.ensure_account()
        self.assertEqual(self.source.events(BURST_STRATEGY_ID, 0), [])

    def test_stale_bar_does_not_get_fresh_observation_timestamp(self):
        self.store.candles[self.pair][-1] = candle(15, 101.5)
        self.now = 16 * BAR_MS + 90_001
        self.assertEqual(self.source.events(BURST_STRATEGY_ID, 0), [])
        self.assertEqual(self.store.documents[BURST_STATE_KEY]["observations"][self.pair]["reason"],
                         "closed_candle_stale")

    def test_restart_preserves_ids_sequence_and_original_clock(self):
        first = self.signal()[0]
        self.now += 40_000
        source = PriceBurstEventSource(self.store, clock_ms=lambda: self.now)
        self.assertEqual(source.events(BURST_STRATEGY_ID, 0), [first])
        self.assertEqual(source.events(BURST_STRATEGY_ID, 1), [])
        self.assertEqual(len(self.entry_documents()), 1)
        self.assertEqual(source.latest_sequence(BURST_STRATEGY_ID), 1)
        self.assertEqual(source.events(BURST_STRATEGY_ID, 0), [first])

    def test_new_bar_after_no_signal_checkpoint_can_emit(self):
        self.assertEqual(self.signal(100), [])
        self.store.candles[self.pair].append(candle(16, 101.5))
        self.now = 17 * BAR_MS + 10
        event = self.source.events(BURST_STRATEGY_ID, 0)[0]
        self.assertEqual(event["observed_at_ms"], 17 * BAR_MS)
        self.assertEqual(event["sequence_id"], 1)

    def test_multi_pair_sequences_and_pagination_are_persisted(self):
        for pair in BURST_PAIRS:
            self.store.candles[pair] = [candle(index) for index in range(16)]
        self.source.latest_sequence(BURST_STRATEGY_ID)
        for index, pair in enumerate(BURST_PAIRS):
            self.store.candles[pair][-1] = candle(15, 98.5 if index == 1 else 101.5)
        self.now = 16 * BAR_MS + 10
        events = self.source.events(BURST_STRATEGY_ID, 0)
        self.assertEqual([event["sequence_id"] for event in events], [1, 2, 3])
        self.assertEqual([event["payload"]["pair"] for event in events], list(BURST_PAIRS))
        self.assertEqual(self.source.events(BURST_STRATEGY_ID, 1, limit=1), [events[1]])
        self.assertEqual(len(self.entry_documents()), 3)

    def test_corrupted_frozen_definition_and_hash_are_refused(self):
        for mutate in (
                lambda row: row["definition"]["params"].update(move_threshold=0.001),
                lambda row: row.update(rule_hash="replacement"),
                lambda row: row["execution"].update(leverage=30)):
            with self.subTest(mutate=mutate):
                self.setUp()
                mutate(self.store.documents[BURST_DEFINITION_KEY])
                before = copy.deepcopy(self.store.documents)
                with self.assertRaisesRegex(ValueError, "identity mismatch"):
                    self.source.ensure_account()
                with self.assertRaisesRegex(ValueError, "identity mismatch"):
                    self.source.events(BURST_STRATEGY_ID, 0)
                self.assertEqual(self.store.documents, before)

    def test_missing_or_incompatible_checkpoint_is_not_silently_recreated(self):
        del self.store.documents[BURST_STATE_KEY]
        with self.assertRaisesRegex(ValueError, "checkpoint is missing"):
            self.source.ensure_account()
        self.setUp()
        self.store.documents[BURST_STATE_KEY]["execution_hash"] = "changed"
        with self.assertRaisesRegex(ValueError, "checkpoint identity mismatch"):
            self.source.latest_sequence(BURST_STRATEGY_ID)
        self.setUp()
        del self.store.documents[BURST_DEFINITION_KEY]
        with self.assertRaisesRegex(ValueError, "orphaned"):
            self.source.ensure_account()

    def test_event_and_checkpoint_commit_atomically_after_failure(self):
        before = copy.deepcopy(self.store.documents)
        self.store.fail_checkpoint_writes = True
        with self.assertRaisesRegex(RuntimeError, "checkpoint commit failure"):
            self.signal()
        self.assertEqual(self.store.documents, before)
        self.store.fail_checkpoint_writes = False
        event = self.source.events(BURST_STRATEGY_ID, 0)[0]
        self.assertEqual(event["sequence_id"], 1)
        self.assertEqual(len(self.entry_documents()), 1)

    def test_concurrent_sources_create_one_fact_and_one_sequence(self):
        self.store.candles[self.pair][-1] = candle(15, 101.5)
        self.now = 16 * BAR_MS + 10
        results, errors = [], []

        def call():
            try:
                results.append(PriceBurstEventSource(self.store, clock_ms=lambda: self.now)
                               .events(BURST_STRATEGY_ID, 0))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.entry_documents()), 1)
        self.assertEqual(self.store.documents[BURST_STATE_KEY]["sequence_id"], 1)

    def test_unknown_strategy_invalid_sequence_and_unsupported_candle_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.source.account("s35")
        for after in (-1, True, "0"):
            with self.subTest(after=after), self.assertRaises(ValueError):
                self.source.events(BURST_STRATEGY_ID, after)
        self.assertIsNone(self.source.closed_candle("SOL/USDT:USDT", "5m"))
        self.assertIsNone(self.source.closed_candle(self.pair, "15m"))


if __name__ == "__main__":
    unittest.main()

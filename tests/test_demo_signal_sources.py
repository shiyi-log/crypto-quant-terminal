"""Independent routing and read-only discovery of frozen Demo burst variants."""
import copy
from contextlib import contextmanager
import unittest
from unittest.mock import Mock, patch

from demo_price_burst import (
    BURST_DEFINITION_KEY, BURST_STRATEGY_ID, BURST_V2_DEFINITION_KEY,
    BURST_V2_STRATEGY_ID, PriceBurstEventSource, _identity,
)
from demo_signal_sources import DemoSignalSources


class ReadOnlyStore:
    """Only document SELECTs are allowed; discovery cannot bootstrap state."""

    def __init__(self, documents=None):
        self.documents = copy.deepcopy(documents or {})
        self.reads = []

    def get_document(self, key):
        self.reads.append(("get", key))
        return copy.deepcopy(self.documents.get(key))

    @contextmanager
    def _connection(self):
        yield self

    def execute(self, query, params=()):
        if query != "SELECT payload FROM documents WHERE key=%s":
            raise AssertionError("discovery must remain read only: " + query)
        self.reads.append(("select", params[0]))
        document = self.documents.get(params[0])
        row = None if document is None else {"payload": copy.deepcopy(document)}
        result = Mock()
        result.fetchone.return_value = row
        return result


def frozen_account(strategy_id):
    return {**_identity(strategy_id), "created_ms": 123, "forward_boundary_ms": 123}


class DemoSignalSourcesTests(unittest.TestCase):
    def test_default_construction_is_read_only_and_selects_two_independent_variants(self):
        store = ReadOnlyStore()
        def clock():
            return 456
        sources = DemoSignalSources(store, paper=Mock(), clock_ms=clock)
        self.assertIsInstance(sources.burst, PriceBurstEventSource)
        self.assertIsInstance(sources.burst_v2, PriceBurstEventSource)
        self.assertIsNot(sources.burst, sources.burst_v2)
        self.assertEqual(sources.burst.strategy_id, BURST_STRATEGY_ID)
        self.assertEqual(sources.burst_v2.strategy_id, BURST_V2_STRATEGY_ID)
        self.assertIs(sources.burst.clock_ms, clock)
        self.assertIs(sources.burst_v2.clock_ms, clock)
        self.assertEqual(store.documents, {})
        self.assertEqual(store.reads, [])

    def test_legacy_burst_injection_remains_supported_with_default_v2(self):
        store, burst = ReadOnlyStore(), Mock()
        sources = DemoSignalSources(store, paper=Mock(), burst=burst)
        self.assertIs(sources.burst, burst)
        self.assertIsInstance(sources.burst_v2, PriceBurstEventSource)
        sources.account(BURST_STRATEGY_ID)
        burst.account.assert_called_once_with(BURST_STRATEGY_ID)

    def test_all_strategy_methods_route_exact_ids_without_crossing_variants(self):
        for strategy_id, selected in (
            (BURST_STRATEGY_ID, "burst"),
            (BURST_V2_STRATEGY_ID, "burst_v2"),
            ("s02_dual_thrust", "paper"),
            (BURST_STRATEGY_ID + "_other", "paper"),
            (BURST_V2_STRATEGY_ID + "_other", "paper"),
        ):
            with self.subTest(strategy_id=strategy_id):
                injected = {name: Mock() for name in ("paper", "burst", "burst_v2")}
                sources = DemoSignalSources(ReadOnlyStore(), **injected)
                target = injected[selected]
                self.assertIs(sources.account(strategy_id), target.account.return_value)
                self.assertIs(sources.latest_sequence(strategy_id),
                              target.latest_sequence.return_value)
                self.assertIs(sources.events(strategy_id, 17, limit=8),
                              target.events.return_value)
                target.account.assert_called_once_with(strategy_id)
                target.latest_sequence.assert_called_once_with(strategy_id)
                target.events.assert_called_once_with(strategy_id, 17, 8)
                for name, source in injected.items():
                    if name != selected:
                        source.assert_not_called()
                        self.assertEqual(source.mock_calls, [])

    def test_default_event_limit_is_forwarded(self):
        burst_v2 = Mock()
        sources = DemoSignalSources(ReadOnlyStore(), paper=Mock(), burst=Mock(),
                                    burst_v2=burst_v2)
        sources.events(BURST_V2_STRATEGY_ID, 9)
        burst_v2.events.assert_called_once_with(BURST_V2_STRATEGY_ID, 9, 1000)

    def test_closed_candles_continue_using_shared_paper_reader(self):
        paper, burst, burst_v2 = Mock(), Mock(), Mock()
        sources = DemoSignalSources(ReadOnlyStore(), paper=paper, burst=burst,
                                    burst_v2=burst_v2)
        self.assertIs(sources.closed_candle("BTC/USDT:USDT", "5m", 300000),
                      paper.closed_candle.return_value)
        paper.closed_candle.assert_called_once_with("BTC/USDT:USDT", "5m", 300000)
        self.assertEqual(burst.mock_calls, [])
        self.assertEqual(burst_v2.mock_calls, [])

    def test_discovery_without_definitions_does_not_bootstrap_either_source(self):
        store = ReadOnlyStore()
        sources = DemoSignalSources(store, paper=Mock())
        with patch.object(sources.burst, "ensure_account", side_effect=AssertionError), \
                patch.object(sources.burst_v2, "ensure_account", side_effect=AssertionError):
            self.assertEqual(sources.additional_accounts(), [])
        self.assertEqual(store.documents, {})
        self.assertEqual(store.reads, [("get", BURST_DEFINITION_KEY),
                                      ("get", BURST_V2_DEFINITION_KEY)])

    def assert_discovered_variants(self, variants):
        definitions = {key: frozen_account(strategy_id)
                       for key, strategy_id, _ in variants}
        store = ReadOnlyStore(definitions)
        sources = DemoSignalSources(store, paper=Mock())
        with patch.object(sources.burst, "ensure_account", side_effect=AssertionError), \
                patch.object(sources.burst_v2, "ensure_account", side_effect=AssertionError):
            rows = sources.additional_accounts()
        self.assertEqual([row["accountId"] for row in rows],
                         [strategy_id for _, strategy_id, _ in variants])
        for row, (key, strategy_id, threshold) in zip(rows, variants):
            definition = definitions[key]
            self.assertEqual(row["strategyId"], strategy_id)
            self.assertEqual(row["ruleHash"], definition["rule_hash"])
            self.assertEqual(row["executionHash"], definition["execution_hash"])
            self.assertIn("±" + threshold, row["description"])
            self.assertTrue(row["eligible"])
            self.assertTrue(row["enabled"])
            self.assertIsNone(row["blockedReason"])
            self.assertEqual(row["timeframe"], "5m")
            self.assertEqual(row["source"], "forward_burst_actual_closed_candles")
        self.assertEqual(store.documents, definitions)
        self.assertEqual([key for operation, key in store.reads if operation == "select"],
                         [key for key, _, _ in variants])

    def test_discovery_with_only_v1_definition(self):
        self.assert_discovered_variants([(BURST_DEFINITION_KEY, BURST_STRATEGY_ID, "1.5%")])

    def test_discovery_with_only_v2_definition(self):
        self.assert_discovered_variants([(BURST_V2_DEFINITION_KEY, BURST_V2_STRATEGY_ID, "0.5%")])

    def test_discovery_with_both_definitions_preserves_distinct_hashes(self):
        self.assert_discovered_variants([
            (BURST_DEFINITION_KEY, BURST_STRATEGY_ID, "1.5%"),
            (BURST_V2_DEFINITION_KEY, BURST_V2_STRATEGY_ID, "0.5%"),
        ])
        self.assertNotEqual(_identity()["rule_hash"], _identity(BURST_V2_STRATEGY_ID)["rule_hash"])
        self.assertNotEqual(_identity()["execution_hash"],
                            _identity(BURST_V2_STRATEGY_ID)["execution_hash"])

    def test_discovery_exposes_corrupt_identity_instead_of_hiding_variant(self):
        for key, strategy_id in (
            (BURST_DEFINITION_KEY, BURST_STRATEGY_ID),
            (BURST_V2_DEFINITION_KEY, BURST_V2_STRATEGY_ID),
        ):
            with self.subTest(strategy_id=strategy_id):
                account = frozen_account(strategy_id)
                account["rule_hash"] = "corrupt"
                store = ReadOnlyStore({key: account})
                before = copy.deepcopy(store.documents)
                sources = DemoSignalSources(store, paper=Mock())
                with self.assertRaisesRegex(ValueError, "frozen Demo price-burst identity mismatch"):
                    sources.additional_accounts()
                self.assertEqual(store.documents, before)


if __name__ == "__main__":
    unittest.main()

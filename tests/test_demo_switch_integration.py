"""Real switch modules with isolated quant_test storage and offline HTTP only.

The real adapter is constructed around ExchangeStub, never a network client.
Credential placeholders are held in memory; no private credential store or
production schema is read. Only protection thread startup is mocked.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock

from binance_demo_adapter import BinanceDemoAdapter
from data_store import DataStore
from demo_api import DemoApi, DemoApiError
from demo_runner_controller import DemoRunnerController, STATE_KEY
from demo_store import DemoStore
from runtime_config import load_environment
from tests.test_demo_contract_integration import BTC, NOW, SYMBOL, ExchangeStub, PaperStub

load_environment()
OLD = "offline-old-orb"
NEW = "offline-new-dual-thrust"


class AccountExchangeStub(ExchangeStub):
    """Inject unrelated actual account state without changing ledger fills."""

    def __init__(self):
        super().__init__()
        self.external_positions = None
        self.external_orders = []

    def request(self, path, api, method, params, *, config=None):
        if path == "positionRisk" and self.external_positions is not None:
            self.calls.append((path, api, method, copy.deepcopy(params)))
            return copy.deepcopy(self.external_positions)
        result = super().request(path, api, method, params, config=config)
        if path == "openOrders":
            result += copy.deepcopy(self.external_orders)
        return result


class TwoFrozenPaperSources:
    def __init__(self):
        self.sources = {OLD: PaperStub(), NEW: PaperStub()}
        for strategy_id, source in self.sources.items():
            source.row.update(strategy_id=strategy_id, rule_hash="rule:" + strategy_id,
                              execution_hash="execution:" + strategy_id)
            source.row["definition"]["strategy_id"] = strategy_id

    def account(self, strategy_id):
        return self.sources[strategy_id].account(strategy_id)

    def latest_sequence(self, strategy_id):
        return self.sources[strategy_id].latest_sequence(strategy_id)

    def events(self, strategy_id, after_sequence, limit=1000):
        return self.sources[strategy_id].events(strategy_id, after_sequence, limit)

    def closed_candle(self, pair, timeframe, candle_ms=None):
        return self.sources[OLD].closed_candle(pair, timeframe, candle_ms)

    def signal(self, strategy_id, now, *, kind="entry", side="buy"):
        source = self.sources[strategy_id]
        source.signal(now, kind=kind, side=side)
        source.rows[-1]["rule_hash"] = source.row["rule_hash"]
        source.rows[-1]["event_id"] = strategy_id + ":" + source.rows[-1]["event_id"]

    def eligible_accounts(self):
        return [{"accountId": strategy_id, "eligible": True, "enabled": True,
                 "ruleHash": source.row["rule_hash"], "executionHash": source.row["execution_hash"]}
                for strategy_id, source in self.sources.items()]


class PlaceholderCredentials:
    def __init__(self):
        self.value = SimpleNamespace(api_key="offline-switch-placeholder-key",
                                     secret="offline-switch-placeholder-secret")

    def load_credentials(self):
        return self.value

    def status(self):
        return {"configured": True,
                "fingerprint": hashlib.sha256(self.value.api_key.encode()).hexdigest()[:16]}


@unittest.skipUnless(os.getenv("QUANT_TEST_DATABASE_URL"), "independent quant_test required")
class DemoSwitchIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import conninfo_to_dict
        test = conninfo_to_dict(os.environ["QUANT_TEST_DATABASE_URL"])
        production = conninfo_to_dict(os.environ.get("QUANT_DATABASE_URL", ""))
        def location(info):
            return tuple(info.get(key, "") for key in ("host", "port", "dbname"))
        if test.get("dbname") != "quant_test" or location(test) == location(production):
            raise RuntimeError("switch integration requires independent quant_test")
        cls.schema = "quant_demo_switch_" + uuid.uuid4().hex
        cls.root = tempfile.TemporaryDirectory()
        cls.data = DataStore(root=cls.root.name, dsn=os.environ["QUANT_TEST_DATABASE_URL"],
                             schema=cls.schema).initialize()
        DemoStore(cls.data).ensure_schema()

    @classmethod
    def tearDownClass(cls):
        import psycopg
        from psycopg import sql
        cls.data.close()
        with psycopg.connect(os.environ["QUANT_TEST_DATABASE_URL"]) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))
        cls.root.cleanup()

    def setUp(self):
        with self.data._connection() as conn:
            conn.execute("TRUNCATE demo_fills,demo_orders,demo_positions,demo_account,demo_signal_comparisons")
            conn.execute("DELETE FROM documents WHERE key=%s", (STATE_KEY,))
        self.clock = NOW
        self.client, self.paper = AccountExchangeStub(), TwoFrozenPaperSources()
        self.credentials = PlaceholderCredentials()
        self.old = DemoStore(self.data, "demo_default")
        self.controllers = []
        self.controller = self.new_controller()
        self.api = self.new_api(self.controller)
        self.addCleanup(self.release_test_locks)

    def adapter_factory(self, **kwargs):
        return BinanceDemoAdapter(client=self.client, **kwargs)

    def new_controller(self):
        controller = DemoRunnerController(self.data, DemoStore(self.data, "demo_default"),
            self.credentials.load_credentials, self.adapter_factory, paper_source=self.paper,
            clock_ms=lambda: self.clock,
            lock_path=Path(self.root.name) / "binance_demo_runner.lock")
        controller._start_worker = Mock()
        self.controllers.append(controller)
        return controller

    def new_api(self, controller):
        return DemoApi(self.data, self.credentials, self.old, controller,
                       account_provider=self.paper.eligible_accounts,
                       ledger_provider=lambda: controller.demo_store,
                       adapter_factory=self.adapter_factory)

    def release_test_locks(self):
        # No workers are launched in these tests. Closing the descriptor models
        # a process exit while retaining its durable recovery pointer.
        for controller in self.controllers:
            if controller._file_lock is not None:
                controller._file_lock.close()
                controller._file_lock = None

    def assert_shared_flock(self, controller):
        with controller.lock_path.open("a+") as contender:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertTrue(controller.status()["accountLockOwned"])

    def tick(self, elapsed=1000):
        self.clock += elapsed
        self.client.now = self.clock
        result = self.controller.service.cycle()
        self.assertIsNone(result["last_error"], result)
        return result

    def start_old(self):
        result = self.api.post("/start", {"accountId": OLD}, actor="offline-admin")
        self.assertTrue(result["runner"]["entriesEnabled"])
        self.assertIsNone(result["runner"]["last_error"], result)
        return copy.deepcopy(self.old.get_binding())

    def complete_old_round_trip(self):
        binding = self.start_old()
        self.clock += 1000
        self.client.now = self.clock
        self.paper.signal(OLD, self.clock)
        self.controller.service.cycle()
        entry = self.old.runner_orders()[0]
        self.assertEqual(entry["status"], "open")
        self.client.add_fill(entry["client_id"], entry["quantity"])
        self.tick()
        self.clock += 1000
        self.client.now = self.clock
        self.paper.signal(OLD, self.clock, kind="exit", side="sell")
        self.controller.service.cycle()
        self.tick()
        self.assertEqual(len(self.old.runner_orders()), 2)
        self.assertEqual(len(self.old.runner_fills()), 2)
        self.assertTrue(self.old.account_reconciliation_status()["safeToSwitch"])
        return binding

    def posts(self):
        return [call for call in self.client.calls if call[2] == "POST"]

    def test_real_round_trip_switch_api_history_and_restart_keep_frozen_sessions(self):
        old_binding = self.complete_old_round_trip()
        old_orders = self.old.runner_orders()
        old_fills = self.old.runner_fills()
        count_before_switch = len(self.posts())
        result = self.api.post("/switch", {"accountId": NEW}, actor="offline-admin")
        namespace = result["runner"]["virtualAccountKey"]
        self.assertRegex(namespace, r"^demo_strategy_[0-9a-f]{32}$")
        self.assertFalse(result["runner"]["entriesEnabled"])
        self.assertFalse(result["automaticRestart"])
        self.assert_shared_flock(self.controller)
        self.assertEqual(len(self.posts()), count_before_switch)
        active = self.controller.demo_store
        new_binding = active.get_binding()
        self.assertEqual(new_binding["strategy_id"], NEW)
        self.assertNotEqual(new_binding["rule_hash"], old_binding["rule_hash"])
        self.assertNotEqual(new_binding["execution_hash"], old_binding["execution_hash"])
        self.assertEqual(self.old.get_binding(), old_binding)
        self.assertEqual(self.old.runner_orders(), old_orders)
        self.assertEqual(self.old.runner_fills(), old_fills)
        with self.assertRaises(ValueError):
            self.old.bind_account(new_binding)

        status = self.api.get("/status")
        self.assertEqual(status["runner"]["virtualAccountKey"], namespace)
        self.assertEqual(status["metrics"]["orderCount"], 0)
        self.assertEqual(self.api.get("/orders")["items"], [])
        self.assertEqual(self.api.get("/fills")["items"], [])
        self.assertEqual(self.api.get("/evidence")["questions"]["fees"]["fillCount"], 0)
        self.assertEqual(len(self.api.get("/orders", session="demo_default")["items"]), 2)
        self.assertEqual(len(self.api.get("/fills", session="demo_default")["items"]), 2)
        historical = self.api.get("/evidence", session="demo_default")
        self.assertEqual(historical["questions"]["fees"]["fillCount"], 2)
        self.assertEqual(historical["questions"]["secondsProfit"]["closedRoundTripCount"], 1)
        sessions = self.api.get("/sessions")["items"]
        self.assertEqual({row["virtualAccountKey"] for row in sessions}, {"demo_default", namespace})
        self.assertEqual([row["virtualAccountKey"] for row in sessions if row["active"]], [namespace])

        # A fresh valid signal in a selected-but-paused session cannot submit.
        self.clock += 1000
        self.client.now = self.clock
        self.paper.signal(NEW, self.clock)
        self.controller.service.cycle()
        self.assertEqual(active.runner_orders(), [])
        self.assertEqual(len(self.posts()), count_before_switch)
        self.assertEqual(active.list_comparisons(stage="entry_denied")[0]["payload"]["reason"],
                         "entries_paused_or_read_only")
        self.controller._save()

        # Closing only the test descriptor simulates process exit, rather than
        # yield-flat (which deliberately disables automatic protection restore).
        self.controller._file_lock.close()
        self.controller._file_lock = None
        restarted = self.new_controller()
        resumed = restarted.resume_protection()
        self.assertEqual(resumed["virtualAccountKey"], namespace)
        self.assertEqual(resumed["selectedAccountId"], NEW)
        self.assertFalse(resumed["entriesEnabled"])
        self.assertIsNone(resumed["last_error"], resumed)
        self.assert_shared_flock(restarted)
        restarted_api = self.new_api(restarted)
        self.assertEqual(restarted_api.get("/orders")["items"], [])
        self.assertEqual(restarted_api.get("/evidence")["questions"]["fees"]["fillCount"], 0)
        self.assertEqual(restarted.demo_store.get_binding(), new_binding)
        self.assertEqual(self.old.get_binding(), old_binding)
        self.assertEqual(len(self.posts()), count_before_switch)

    def seed_missing_fills(self, ledger, *, active=False):
        binding = ledger.get_binding()
        if binding is None:
            binding = {"strategy_id": "offline-historical", "signal_id": "binding",
                       "rule_hash": "historical-rule", "execution_hash": "historical-execution",
                       "input_fingerprint": "historical-input", "demo_policy_hash": "historical-policy"}
            ledger.bind_account(binding)
        client_id = "missing_" + uuid.uuid4().hex[:20]
        intent = {**{key: binding[key] for key in ("strategy_id", "rule_hash", "execution_hash", "input_fingerprint")},
                  "signal_id": "missing-fill-signal", "client_id": client_id, "intent_key": "entry",
                  "pair": BTC, "side": "buy", "order_type": "limit", "quantity": ".01", "price": "60000.1",
                  "time_in_force": "GTX", "signal_ms": self.clock, "created_ms": self.clock,
                  "intent_payload": {"demo_policy_hash": binding["demo_policy_hash"]}}
        ledger.reserve_order(intent)
        ledger.claim_submission(client_id, self.clock)
        raw = {"symbol": SYMBOL, "orderId": 999, "clientOrderId": client_id,
               "side": "BUY", "type": "LIMIT", "status": "FILLED", "origQty": ".01",
               "executedQty": ".01", "avgPrice": "60000.1", "price": "60000.1",
               "time": self.clock, "updateTime": self.clock}
        ledger.record_order_observation(client_id, {"raw": raw})
        if active:
            # Return the actual FILLED response while hiding its userTrades.
            # A cancellation acknowledgement cannot manufacture missing fills.
            self.client.orders[client_id] = raw

    def test_incomplete_historical_namespace_fills_block_real_global_gate(self):
        old_binding = self.start_old()
        history = DemoStore(self.data, "demo_strategy_" + uuid.uuid4().hex)
        self.seed_missing_fills(history)
        gate = self.old.account_reconciliation_status()
        self.assertFalse(gate["safeToSwitch"])
        self.assertEqual(gate["fillQuantityGapCount"], 1)
        with self.assertRaises(DemoApiError) as refused:
            self.api.post("/switch", {"accountId": NEW}, actor="offline-admin")
        self.assertEqual(refused.exception.code, "demo_account_reconciliation_unresolved")
        self.assertEqual(self.old.get_binding(), old_binding)
        self.assertEqual(self.controller.demo_store.virtual_account_key, "demo_default")
        self.assertFalse(self.controller.status()["entriesEnabled"])
        self.assert_shared_flock(self.controller)
        self.assertFalse(self.posts())

    def test_incomplete_active_order_fills_cannot_change_frozen_binding(self):
        old_binding = self.start_old()
        self.seed_missing_fills(self.old, active=True)
        with self.assertRaises(DemoApiError) as refused:
            self.api.post("/switch", {"accountId": NEW}, actor="offline-admin")
        self.assertEqual(refused.exception.code, "demo_owned_fills_unresolved")
        self.assertEqual(self.old.get_binding(), old_binding)
        self.assertFalse(self.controller.status()["entriesEnabled"])
        self.assert_shared_flock(self.controller)
        self.assertEqual(self.old.runner_fills(), [])
        self.assertFalse(self.posts())

    def test_real_adapter_fresh_foreign_position_blocks_switch(self):
        old_binding = self.start_old()
        self.client.external_positions = [{"symbol": "SOLUSDT", "positionAmt": ".5",
            "positionSide": "BOTH", "entryPrice": "100", "leverage": "10", "marginType": "isolated"}]
        with self.assertRaises(DemoApiError) as refused:
            self.api.post("/switch", {"accountId": NEW}, actor="offline-admin")
        self.assertEqual(refused.exception.code, "demo_account_positions_not_flat")
        self.assertEqual(self.old.get_binding(), old_binding)
        self.assertEqual(self.old.net_fill_positions(), {})
        self.assertFalse(self.controller.status()["entriesEnabled"])
        self.assert_shared_flock(self.controller)
        self.assertFalse(self.posts())

    def test_real_adapter_fresh_foreign_open_order_blocks_switch(self):
        old_binding = self.start_old()
        self.client.external_orders = [{"symbol": SYMBOL, "orderId": 777,
            "clientOrderId": "external-order", "side": "SELL", "type": "LIMIT", "status": "NEW",
            "origQty": ".01", "executedQty": "0", "avgPrice": "0", "price": "61000",
            "time": self.clock, "updateTime": self.clock, "reduceOnly": False}]
        with self.assertRaises(DemoApiError) as refused:
            self.api.post("/switch", {"accountId": NEW}, actor="offline-admin")
        self.assertEqual(refused.exception.code, "demo_account_open_orders_unresolved")
        self.assertEqual(self.old.get_binding(), old_binding)
        self.assertEqual(self.old.net_fill_positions(), {})
        self.assertFalse(self.controller.status()["entriesEnabled"])
        self.assert_shared_flock(self.controller)
        self.assertFalse(self.posts())


if __name__ == "__main__":
    unittest.main()

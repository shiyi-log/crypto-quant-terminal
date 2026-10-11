"""Production configuration audit contracts without a database dependency."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path

import runtime_config_audit as audit


class Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, store):
        self.store = store

    def execute(self, query, params=()):
        if "SELECT event_id,payload FROM research_ledger_events" in query:
            rows = [row for row in self.store.events.values()
                    if row["source_id"] == params[0] and row["event_type"] == params[1]]
            return Result(rows[-1] if rows else None)
        if "INSERT INTO research_ledger_events" in query:
            event_id, event_type, payload, source_id = params
            if event_id in self.store.events:
                return Result()
            row = {"event_id": event_id, "event_type": event_type,
                   "payload": payload, "source_id": source_id}
            self.store.events[event_id] = row
            return Result({"event_id": event_id})
        if "SELECT event_id,event_type,payload,source_id FROM research_ledger_events" in query:
            return Result(self.store.events.get(params[0]))
        raise AssertionError(query)


class MemoryStore:
    def __init__(self, fail=False):
        self.events = {}
        self.lock = threading.RLock()
        self.fail = fail

    @contextmanager
    def _connection(self):
        if self.fail:
            raise RuntimeError("ledger offline")
        with self.lock:
            before = dict(self.events)
            try:
                yield Connection(self)
            except Exception:
                self.events = before
                raise

    @staticmethod
    def _json(value):
        return value


class RuntimeConfigAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        (self.root / "config-a.json").write_text(
            '{"exchange":{"key":"secret-value"},"dry_run":false}\n', encoding="utf-8")
        (self.root / "config-b.json").write_text('{"enabled":true}\n', encoding="utf-8")

    def test_canonical_hash_ignores_json_formatting_and_key_order(self):
        first = audit.canonical_config_hash("config-a.json", self.root)
        (self.root / "config-a.json").write_text(
            '{\n  "dry_run": false, "exchange": {"key": "secret-value"}\n}', encoding="utf-8")
        second = audit.canonical_config_hash("config-a.json", self.root)
        self.assertEqual(first, second)

    def test_service_hash_is_independent_of_config_argument_order(self):
        left = audit.build_snapshot(root=self.root, service="api",
                                    config_paths=["config-a.json", "config-b.json"],
                                    trigger="startup", observed_at_utc="fixed")
        right = audit.build_snapshot(root=self.root, service="api",
                                     config_paths=["config-b.json", "config-a.json"],
                                     trigger="startup", observed_at_utc="fixed")
        self.assertEqual(left["config_sha256"], right["config_sha256"])
        self.assertEqual(left["config_paths"], ["config-a.json", "config-b.json"])

    def test_append_is_idempotent_and_links_each_changed_hash(self):
        store = MemoryStore()
        first = audit.append_config_snapshot(store, root=self.root, service="api",
                                             config_paths=["config-a.json"], trigger="startup",
                                             observed_at_utc="t1")
        repeat = audit.append_config_snapshot(store, root=self.root, service="api",
                                              config_paths=["config-a.json"], trigger="startup",
                                              observed_at_utc="t2")
        self.assertEqual(first, repeat)
        self.assertEqual(len(store.events), 1)

        (self.root / "config-a.json").write_text('{"version":2}\n', encoding="utf-8")
        second = audit.append_config_snapshot(store, root=self.root, service="api",
                                              config_paths=["config-a.json"], trigger="reload_config")
        second_payload = store.events[second]["payload"]
        self.assertNotEqual(first, second)
        self.assertEqual(second_payload["previous_sha256"],
                         store.events[first]["payload"]["config_sha256"])

        # Returning to a previously seen value is itself a new transition.
        (self.root / "config-a.json").write_text(
            '{"exchange":{"key":"secret-value"},"dry_run":false}\n', encoding="utf-8")
        rollback = audit.append_config_snapshot(store, root=self.root, service="api",
                                                config_paths=["config-a.json"], trigger="reload_config")
        self.assertNotEqual(first, rollback)
        self.assertEqual(store.events[rollback]["payload"]["previous_sha256"],
                         second_payload["config_sha256"])

    def test_payload_contains_no_config_values_or_absolute_paths(self):
        store = MemoryStore()
        event_id = audit.append_config_snapshot(store, root=self.root, service="api",
                                                config_paths=["config-a.json"], trigger="startup")
        payload_text = json.dumps(store.events[event_id]["payload"])
        self.assertNotIn("secret-value", payload_text)
        self.assertNotIn(str(self.root), payload_text)
        self.assertIn("sha256", payload_text)

    def test_rejects_outside_symlink_and_non_json_configuration(self):
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        link = self.root / "linked.json"
        link.symlink_to(outside)
        for path in (outside, link, "../outside.json"):
            with self.subTest(path=path), self.assertRaises(audit.ConfigAuditError):
                audit.canonical_config_hash(path, self.root)

        bad_json = self.root / "bad.json"
        bad_json.write_text('{"number":NaN}', encoding="utf-8")
        with self.assertRaises(audit.ConfigAuditError):
            audit.canonical_config_hash(bad_json, self.root)

    def test_ledger_failure_propagates_for_fail_closed_callers(self):
        with self.assertRaisesRegex(RuntimeError, "ledger offline"):
            audit.append_config_snapshot(MemoryStore(fail=True), root=self.root,
                                         service="api", config_paths=["config-a.json"],
                                         trigger="startup")


if __name__ == "__main__":
    unittest.main()

"""Unit tests for ledger facts without connecting to a trading database."""
import unittest
import threading
import json
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import call, patch

import research_ledger as ledger


class FakeConnection:
    def __init__(self, store):
        self.store = store

    def execute(self, query, params=()):
        if "INSERT INTO research_ledger_events" in query:
            event_id, event_type, payload, source_id = params
            with self.store.event_lock:
                if event_id in self.store.events:
                    return FakeResult(None)
                self.store.sequence += 1
                self.store.events[event_id] = {"event_id": event_id, "event_type": event_type,
                                               "payload": payload, "source_id": source_id,
                                               "created_at": "2026-10-10T00:00:00+00:00",
                                               "sequence_id": self.store.sequence}
                return FakeResult({"event_id": event_id})
        if "WHERE event_id=%s" in query:
            row = self.store.events.get(params[0])
            return FakeResult(row)
        if "FROM research_ledger_events WHERE source_id=%s" in query:
            source_id = params[0]
            rows = [row for row in self.store.events.values() if row["source_id"] == source_id]
            if "event_type = ANY(%s)" in query:
                event_types = params[1]
                rows = [row for row in rows if row["event_type"] in event_types]
            rows.sort(key=lambda row: row["sequence_id"])
            if " LIMIT %s" in query:
                rows = rows[:params[-1]]
            return FakeResult(rows)
        if "FROM research_ledger_events" in query and "source_id LIKE %s" in query:
            event_type, source_pattern = params
            prefix = source_pattern.removesuffix("%")
            rows = [row for row in self.store.events.values()
                    if row["event_type"] == event_type and row["source_id"].startswith(prefix)]
            return FakeResult(sorted(rows, key=lambda row: row["sequence_id"]))
        if "FROM research_ledger_events ORDER BY" in query:
            rows = list(self.store.events.values())
            order = "sequence_id" if "ORDER BY sequence_id" in query else "event_id"
            return FakeResult(sorted(rows, key=lambda row: row[order]))
        if "INSERT INTO research_ledger_sync" in query:
            source_id, mtime, fingerprint = params
            self.store.syncs[source_id] = {"source_mtime_ns": mtime,
                                           "source_fingerprint": fingerprint,
                                           "last_synced_at": "2026-10-10T00:00:00+00:00"}
            return FakeResult(None)
        if "FROM research_ledger_sync" in query:
            return FakeResult(self.store.syncs.get(params[0]))
        raise AssertionError(query)


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows

    def fetchall(self):
        return self.rows


class FakeStore:
    def __init__(self, trades=(), orders=(), registry=None, trials=()):
        self.events = {}
        # RLock lets the transaction snapshot and FakeConnection's individual
        # statements share the same lock without deadlocking the test double.
        self.event_lock = threading.RLock()
        self.sequence = 0
        self.syncs = {}
        self.external = {"trades": list(trades), "orders": list(orders)}
        self.documents = {"model_versions.json": registry or {"versions": []}}
        self.event_mirrors = {"research_trials.jsonl": list(trials)}

    @contextmanager
    def _connection(self):
        # Mirror DataStore._connection(): one append batch is atomic.  The
        # snapshot is intentionally small and test-only, but it catches code
        # that relies on rollback after a conflict in the middle of a batch.
        with self.event_lock:
            events_before = dict(self.events)
            sequence_before = self.sequence
            try:
                yield FakeConnection(self)
            except Exception:
                self.events = events_before
                self.sequence = sequence_before
                raise

    @staticmethod
    def _json(value):
        return value

    def read_external(self, source, table):
        return self.external.get(table, [])

    def get_document(self, key, default=None):
        return self.documents.get(key, default)

    def read_events(self, key, limit=None):
        rows = self.event_mirrors.get(key, [])
        return rows[-limit:] if limit is not None else rows


class ResearchLedgerTests(unittest.TestCase):
    @staticmethod
    def _forward_event(run_id, event_type, event_id, **fields):
        return {"event_type": event_type, "run_id": run_id,
                "event_id": event_id, **fields}

    def test_forward_paper_retries_use_stable_ids_and_preserve_source_id(self):
        store = FakeStore()
        event = self._forward_event("run-1", "decision", "decision-1", pair="BTC/USDT")

        first = ledger.append_forward_paper_events(store, "run-1", [event])
        retry = ledger.append_forward_paper_events(store, "run-1", [event])

        self.assertEqual(first, retry)
        self.assertEqual(len(store.events), 1)
        row = next(iter(store.events.values()))
        self.assertEqual(row["event_type"], "forward_paper_decision")
        self.assertEqual(row["source_id"], "forward-paper/run-1")
        self.assertEqual(row["payload"]["event_id"], "decision-1")

    def test_forward_paper_reused_stable_id_with_changed_content_is_rejected(self):
        store = FakeStore()
        original = self._forward_event("run-1", "fill", "fill-1", price=100)
        ledger.append_forward_paper_events(store, "run-1", [original])

        changed = self._forward_event("run-1", "fill", "fill-1", price=101)
        with self.assertRaisesRegex(ValueError, "immutable forward-paper event conflict"):
            ledger.append_forward_paper_events(store, "run-1", [changed])
        self.assertEqual(len(store.events), 1)

    def test_forward_paper_batch_conflict_rolls_back_prior_inserts(self):
        store = FakeStore()
        existing = self._forward_event("run-1", "decision", "existing", decision="allow")
        ledger.append_forward_paper_events(store, "run-1", [existing])

        changed = self._forward_event("run-1", "decision", "existing", decision="deny")
        new_event = self._forward_event("run-1", "fill", "new-fill", action="entry")

        with self.assertRaisesRegex(ValueError, "immutable forward-paper event conflict"):
            ledger.append_forward_paper_events(store, "run-1", [new_event, changed])

        # The transaction must leave the outbox unchanged: a retry can safely
        # replay the entire batch after the conflicting producer event is fixed.
        self.assertEqual(len(store.events), 1)
        self.assertEqual(next(iter(store.events.values()))["payload"]["event_id"], "existing")

    def test_forward_paper_identity_is_isolated_by_run_and_event_type(self):
        store = FakeStore()
        events = [
            self._forward_event("run-1", "decision", "shared-id", decision="allow"),
            self._forward_event("run-2", "decision", "shared-id", decision="allow"),
            self._forward_event("run-1", "fill", "shared-id", action="entry"),
        ]

        ids = [ledger.append_forward_paper_events(store, event["run_id"], [event])[0]
               for event in events]

        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(len(store.events), 3)

    def test_forward_paper_reads_filter_by_run_type_and_append_order(self):
        store = FakeStore()
        ledger.append_forward_paper_events(store, "run-1", [
            self._forward_event("run-1", "manifest", "m1"),
            self._forward_event("run-1", "decision", "d1"),
            self._forward_event("run-1", "fill", "f1"),
            self._forward_event("run-1", "decision", "d2"),
        ])
        ledger.append_forward_paper_events(store, "run-2", [
            self._forward_event("run-2", "decision", "other-run"),
        ])

        rows = ledger.read_forward_paper_events(store, "run-1", event_types=["decision"], limit=1)

        self.assertEqual([row["payload"]["event_id"] for row in rows], ["d1"])
        self.assertEqual(rows[0]["event_type"], "forward_paper_decision")
        all_rows = ledger.read_forward_paper_events(store, "run-1")
        self.assertEqual([row["payload"]["event_id"] for row in all_rows], ["m1", "d1", "f1", "d2"])
        self.assertEqual(ledger.read_forward_paper_events(store, "run-1", event_types=[]), [])
        self.assertEqual(ledger.read_forward_paper_events(store, "run-1", limit=0), [])

    def test_forward_paper_run_list_uses_latest_manifest_backed_runs(self):
        store = FakeStore()
        ledger.append_forward_paper_events(store, "run-old", [
            self._forward_event("run-old", "manifest", "m-old", schema_version="v1"),
            self._forward_event("run-old", "decision", "orphan-like"),
        ])
        ledger.append_forward_paper_events(store, "run-new", [
            self._forward_event("run-new", "manifest", "m-new", schema_version="v2"),
        ])
        ledger.append_forward_paper_events(store, "run-new", [
            self._forward_event("run-new", "manifest", "m-new-revision", schema_version="v3"),
        ])
        ledger.append_forward_paper_events(store, "run-without-manifest", [
            self._forward_event("run-without-manifest", "decision", "orphan"),
        ])

        runs = ledger.list_forward_paper_runs(store)

        self.assertEqual([run["run_id"] for run in runs], ["run-new", "run-old"])
        self.assertEqual(runs[0]["manifest"]["schema_version"], "v2")
        self.assertEqual(runs[0]["manifest_sequence_id"], 3)
        self.assertEqual(ledger.list_forward_paper_runs(store, limit=1), runs[:1])
        self.assertEqual(ledger.list_forward_paper_runs(store, limit=0), [])
        with self.assertRaisesRegex(ValueError, "limit"):
            ledger.list_forward_paper_runs(store, limit=True)

    def test_forward_paper_rejects_malformed_events_and_filters(self):
        store = FakeStore()
        with self.assertRaisesRegex(ValueError, "run_id mismatch"):
            ledger.append_forward_paper_events(store, "run-1", [
                self._forward_event("run-2", "decision", "d1")
            ])
        with self.assertRaisesRegex(ValueError, "unsupported forward-paper event type"):
            ledger.append_forward_paper_events(store, "run-1", [
                self._forward_event("run-1", [], "d1")
            ])
        with self.assertRaisesRegex(ValueError, "event_id"):
            ledger.append_forward_paper_events(store, "run-1", [
                self._forward_event("run-1", "decision", "")
            ])
        with self.assertRaisesRegex(ValueError, "event type filter"):
            ledger.read_forward_paper_events(store, "run-1", event_types=[{}])

    def test_events_are_idempotent_and_conflicts_are_rejected(self):
        store = FakeStore()
        payload = {"id": "v1", "config": {"seq_len": 30}}
        first = ledger._record_event(store, "model_version", payload)
        self.assertEqual(ledger._record_event(store, "model_version", payload), first)
        self.assertEqual(len(store.events), 1)
        with patch.object(ledger, "_event_id", return_value=first):
            with self.assertRaisesRegex(ValueError, "immutable ledger event conflict"):
                ledger._record_event(store, "model_version", {"id": "v1", "config": {"seq_len": 45}})

    def test_concurrent_duplicate_appends_remain_idempotent(self):
        store = FakeStore()
        payload = {"id": "parallel-v1", "config": {"seq_len": 30}}

        with ThreadPoolExecutor(max_workers=12) as pool:
            event_ids = list(pool.map(
                lambda _: ledger._record_event(store, "model_version", payload), range(40)
            ))

        self.assertEqual(len(set(event_ids)), 1)
        self.assertEqual(len(store.events), 1)

    def test_same_version_projects_latest_appended_event(self):
        store = FakeStore()
        first = {"id": "v1", "config": {"seq_len": 30}}
        later = next(
            {"id": "v1", "config": {"seq_len": seq_len}}
            for seq_len in range(31, 100)
            if ledger._event_id("model_version", {"id": "v1", "config": {"seq_len": seq_len}})
            < ledger._event_id("model_version", first)
        )
        ledger._record_event(store, "model_version", first)
        ledger._record_event(store, "model_version", later)
        entry = ledger.ledger_payload(store)["versions"][0]
        self.assertEqual(entry["config"]["seq_len"], later["config"]["seq_len"])

    def test_champion_registry_and_old_ic_do_not_claim_ml_deployment(self):
        entry = ledger._version_entry({"id": "champ", "layer": "ml_model", "status": "champion",
                                       "source": "auto_research", "metrics": {"ic_period": 0.07}})
        self.assertEqual(entry["registry_status"], "champion")
        self.assertEqual(entry["deployment_status"], "not_deployed")
        self.assertEqual(entry["evaluation_status"], "invalidated")
        self.assertIsNone(entry["effect"]["actual_orders"])
        self.assertIsNone(entry["effect"]["closed_trades"])
        self.assertIsNone(entry["effect"]["realized_profit_abs"])

    def test_only_explicitly_closed_trades_contribute_realized_profit(self):
        store = FakeStore(trades=[
            {"id": 1, "pair": "BTC/USDT", "is_open": 1, "stake_amount": 100, "close_profit_abs": None},
            {"id": 2, "pair": "ETH/USDT", "is_open": 0, "stake_amount": 50, "close_profit_abs": 4.5},
            {"id": 3, "is_open": None, "close_date": "unknown", "close_profit_abs": 8.0},
            {"id": 4, "close_profit_abs": 9.0},
        ])
        payload = ledger.ledger_payload(store)
        self.assertEqual(payload["summary"]["actual_trade_count"], 4)
        self.assertEqual(payload["summary"]["closed_trade_count"], 1)
        self.assertEqual(payload["summary"]["realized_profit_abs"], 4.5)
        self.assertEqual(payload["summary"]["unattributed_trade_count"], 4)
        self.assertIsNone(payload["actual_trades"][0]["realized_profit_abs"])
        self.assertIsNone(payload["actual_trades"][2]["is_open"])
        self.assertIsNone(payload["actual_trades"][2]["close_date"])
        self.assertIsNone(payload["actual_trades"][2]["realized_profit_abs"])
        self.assertIsNone(payload["actual_trades"][3]["is_open"])
        self.assertTrue(all(row["model_id"] is None for row in payload["actual_trades"]))

    def test_latest_details_are_sorted_by_activity_time_not_record_key(self):
        store = FakeStore(
            trades=[
                {"id": "10", "is_open": 0, "open_date": "2026-10-01T00:00:00Z", "close_date": "2026-10-03T00:00:00Z"},
                {"id": "2", "is_open": 0, "open_date": "2026-10-02T00:00:00Z", "close_date": "2026-10-05T00:00:00Z"},
                {"id": "1", "is_open": 1, "open_date": "2026-10-04T00:00:00Z"},
            ],
            orders=[
                {"id": "10", "order_date": "2026-10-03T00:00:00Z"},
                {"id": "2", "order_date": "2026-10-05T00:00:00Z"},
                {"id": "1", "order_date": "2026-10-04T00:00:00Z"},
            ],
        )

        payload = ledger.ledger_payload(store)

        # The latest records are selected by business time and kept
        # chronologically for the UI; trade 1 is newer than trade 10.
        self.assertEqual([row["trade_id"] for row in payload["actual_trades"]], ["10", "1", "2"])
        self.assertEqual([row["order_id"] for row in payload["actual_orders"]], ["10", "1", "2"])

    def test_latest_rows_fall_back_to_numeric_ids_when_timestamps_are_missing(self):
        rows = [{"id": str(value)} for value in (10, 2, 1, 11)]
        latest = ledger._latest_rows(rows, limit=3, time_fields=("order_date",))
        self.assertEqual([row["id"] for row in latest], ["2", "10", "11"])

    def test_no_closed_trade_means_unknown_not_zero_performance(self):
        payload = ledger.ledger_payload(FakeStore(trades=[{"id": 3, "is_open": 1}]))
        self.assertIsNone(payload["summary"]["realized_profit_abs"])
        self.assertEqual(payload["summary"]["closed_trade_count"], 0)

    def test_missing_open_flag_stays_unknown_even_with_close_fields(self):
        projected = ledger._trade_projection({
            "id": 3,
            "close_date": "2026-10-10T00:00:00Z",
            "close_profit_abs": 8.0,
        })
        self.assertIsNone(projected["is_open"])
        self.assertIsNone(projected["close_date"])
        self.assertIsNone(projected["realized_profit_abs"])

    def test_order_projection_uses_freqtrade_foreign_key_columns(self):
        store = FakeStore(orders=[{
            "id": 7,
            "ft_trade_id": 3,
            "ft_pair": "BTC/USDT:USDT",
            "ft_order_side": "buy",
            "ft_amount": 2.0,
            "ft_price": 100.0,
            "status": "closed",
            "filled": 2.0,
            "order_date": "2026-10-10 00:00:00",
        }])
        order = ledger.ledger_payload(store)["actual_orders"][0]
        self.assertEqual(order["order_id"], 7)
        self.assertEqual(order["trade_id"], 3)
        self.assertEqual(order["pair"], "BTC/USDT:USDT")
        self.assertEqual(order["side"], "buy")
        self.assertEqual(order["amount"], 2.0)
        self.assertEqual(order["price"], 100.0)

    def test_order_count_uses_complete_snapshot_when_details_are_limited(self):
        store = FakeStore(orders=[
            {"id": order_id, "ft_pair": "BTC/USDT", "status": "closed"}
            for order_id in range(205)
        ])

        payload = ledger.ledger_payload(store)

        self.assertEqual(payload["summary"]["actual_order_count"], 205)
        self.assertEqual(len(payload["actual_orders"]), 200)
        self.assertEqual(payload["actual_orders"][-1]["order_id"], 204)

    def test_same_round_nine_and_twenty_config_has_no_config_delta(self):
        store = FakeStore()
        for vid, round_number in (("auto-r9-e3c3df7c", 9), ("auto-r20-e3c3df7c", 20)):
            ledger._record_event(store, "model_version", {"id": vid, "round": round_number,
                                  "source": "auto_research", "spec": {"seq_len": 30, "hidden": 64}})
        comparison = ledger.ledger_payload(store)["comparison"]
        self.assertEqual(comparison["config_changes"], [])
        self.assertIn("round is research sequence, not generation", comparison["note"])

    def test_payload_projects_registry_and_trial_mirrors(self):
        store = FakeStore(
            registry={"versions": [{"id": "registry-v1", "round": 1, "status": "champion"}]},
            trials=[{"run_id": "run-2", "key": "cfg", "round": 2,
                     "ic_period": 0.04, "phase": "purge experiment"}],
        )

        payload = ledger.ledger_payload(store)

        self.assertEqual([item["id"] for item in payload["versions"]], ["registry-v1", "trial-run-2-cfg"])
        trial = payload["versions"][1]
        self.assertEqual(trial["direction"], "purge experiment")
        self.assertEqual(trial["reported_metrics"]["ic"], 0.04)
        self.assertEqual(trial["evaluation_status"], "invalidated")
        self.assertEqual(payload["summary"]["version_count"], 2)

    def test_external_read_failures_do_not_become_zero_records(self):
        for failed_table in ("trades", "orders"):
            with self.subTest(table=failed_table):
                store = FakeStore()
                read_external = store.read_external

                def fail_read(source, table):
                    if table == failed_table:
                        raise RuntimeError(f"{table} mirror unavailable")
                    return read_external(source, table)

                with patch.object(store, "read_external", side_effect=fail_read) as read:
                    with self.assertRaisesRegex(RuntimeError, "mirror unavailable"):
                        ledger.ledger_payload(store)
                self.assertIn(
                    call("bot/tradesv3.dryrun.sqlite", failed_table),
                    read.call_args_list,
                )

    def test_synced_events_take_precedence_over_old_registry_and_trial_mirrors(self):
        store = FakeStore(
            registry={"versions": [{"id": "v1", "config": {"seq_len": 30},
                                    "status": "champion"}]},
            trials=[{"run_id": "run-2", "key": "cfg", "config": {"seq_len": 30},
                     "ic_period": 0.07, "phase": "old trial"}],
        )
        ledger._record_event(store, "model_version", {
            "id": "v1", "config": {"seq_len": 45}, "status": "archived",
            "direction": "audited correction",
        })
        ledger._record_event(store, "research_trial", {
            "run_id": "run-2", "key": "cfg", "config": {"seq_len": 45},
            "metrics": {"ic": 0.02}, "phase": "corrected trial",
        })

        versions = {entry["id"]: entry for entry in ledger.ledger_payload(store)["versions"]}

        self.assertEqual(versions["v1"]["config"], {"seq_len": 45})
        self.assertEqual(versions["v1"]["registry_status"], "archived")
        self.assertEqual(versions["v1"]["direction"], "audited correction")
        self.assertEqual(versions["trial-run-2-cfg"]["config"], {"seq_len": 45})
        self.assertEqual(versions["trial-run-2-cfg"]["reported_metrics"]["ic"], 0.02)
        self.assertEqual(versions["trial-run-2-cfg"]["direction"], "corrected trial")

    def test_unseeded_ledger_falls_back_to_reference_lessons_and_directions(self):
        payload = ledger.ledger_payload(FakeStore())
        self.assertEqual(payload["lessons"], ledger._lessons())
        self.assertEqual(payload["directions"], ledger._directions())

    def test_postgres_sync_datetime_is_utc_and_entire_payload_is_json_serializable(self):
        for timestamp in (
            datetime.fromisoformat("2026-10-10T00:00:00+00:00"),
            datetime.fromisoformat("2026-10-10T08:00:00+08:00"),
            datetime.fromisoformat("2026-10-10T00:00:00"),
        ):
            with self.subTest(timestamp=timestamp):
                store = FakeStore()
                store.syncs["bot/tradesv3.dryrun.sqlite"] = {"last_synced_at": timestamp}

                payload = ledger.ledger_payload(store)
                # Exercise the same strict encoder as HTTP, without default=str
                # or the mocked _send used by route-only contract tests.
                response = json.loads(json.dumps(payload, ensure_ascii=False))

                self.assertEqual(response["last_synced_at"], "2026-10-10T00:00:00+00:00")
                self.assertEqual(response["source_freshness"]["last_synced_at"], response["last_synced_at"])

    def test_sync_persists_reference_evidence_and_replay_is_idempotent(self):
        store = FakeStore()
        first = ledger.sync(store, "")
        first_event_ids = set(store.events)
        expected_count = len(ledger._lessons()) + len(ledger._directions())

        self.assertEqual(len(first_event_ids), expected_count)
        self.assertEqual(first["lessons"], ledger._lessons())
        self.assertEqual(first["directions"], ledger._directions())
        self.assertEqual({row["event_type"] for row in store.events.values()},
                         {"research_lesson", "research_direction"})

        replay = ledger.sync(store, "")

        self.assertEqual(set(store.events), first_event_ids)
        self.assertEqual(replay["lessons"], first["lessons"])
        self.assertEqual(replay["directions"], first["directions"])

    def test_new_evidence_revisions_survive_replaying_seed_content(self):
        store = FakeStore()
        seeded = ledger.sync(store, "")
        lesson = {**seeded["lessons"][0], "reason": "reviewed correction", "status": "verified"}
        direction = {**seeded["directions"][0], "next_check": "first prospective checkpoint", "status": "observing"}
        ledger._record_event(store, "research_lesson", lesson)
        ledger._record_event(store, "research_direction", direction)
        revision_event_ids = set(store.events)

        replay = ledger.sync(store, "")

        self.assertEqual(set(store.events), revision_event_ids)
        self.assertEqual({row["id"]: row for row in replay["lessons"]}[lesson["id"]], lesson)
        self.assertEqual({row["id"]: row for row in replay["directions"]}[direction["id"]], direction)
        # The original evidence remains available as a separate immutable event.
        self.assertIn(seeded["lessons"][0], [row["payload"] for row in store.events.values()])
        self.assertIn(seeded["directions"][0], [row["payload"] for row in store.events.values()])

    def test_sync_appends_changed_reference_content_and_keeps_original_events(self):
        store = FakeStore()
        ledger.sync(store, "")
        original_event_ids = set(store.events)
        lessons = ledger._lessons()
        directions = ledger._directions()
        lessons[0] = {**lessons[0], "reason": "updated evidence"}
        directions[0] = {**directions[0], "status": "observing"}

        with patch.object(ledger, "_lessons", return_value=lessons), \
             patch.object(ledger, "_directions", return_value=directions):
            updated = ledger.sync(store, "")
            updated_event_ids = set(store.events)
            ledger.sync(store, "")

        self.assertEqual(len(updated_event_ids - original_event_ids), 2)
        self.assertTrue(original_event_ids <= updated_event_ids)
        self.assertEqual(set(store.events), updated_event_ids)
        self.assertEqual(updated["lessons"], lessons)
        self.assertEqual(updated["directions"], directions)


if __name__ == "__main__":
    unittest.main()

"""Audit contracts use an isolated fake DB; no trading endpoint is contacted."""
import copy
import json
import os
import unittest
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import trading_audit as audit


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, store):
        self.store = store

    def execute(self, query, params=()):
        if "INSERT INTO research_ledger_events" in query:
            event_id, event_type, payload, source_id = params
            if event_id in self.store.events:
                return Result(None)
            self.store.events[event_id] = {
                "event_id": event_id, "event_type": event_type,
                "payload": copy.deepcopy(payload), "source_id": source_id,
                "sequence_id": len(self.store.events) + 1,
                "created_at": "2026-10-10T00:00:00+00:00",
            }
            return Result({"event_id": event_id})
        if "WHERE event_id=%s" in query:
            return Result(self.store.events.get(params[0]))
        if "WHERE event_type=%s" in query:
            kind, limit = params
            rows = [row for row in self.store.events.values() if row["event_type"] == kind]
            return Result(sorted(rows, key=lambda row: row["sequence_id"], reverse=True)[:limit])
        if "WHERE event_id=ANY(%s)" in query:
            return Result([row for row in self.store.events.values() if row["event_id"] in params[0]])
        raise AssertionError(query)


class Store:
    def __init__(self):
        self.events = {}
        self.unavailable = False
        self.commit_failure = False
        self.commits = 0

    @contextmanager
    def _connection(self):
        if self.unavailable:
            raise RuntimeError("private DSN/password must never be persisted")
        before = copy.deepcopy(self.events)
        try:
            yield Connection(self)
            if self.commit_failure:
                raise RuntimeError("commit failed")
            self.commits += 1
        except Exception:
            self.events = before
            raise

    @staticmethod
    def _json(value):
        return value


class TradingAuditTests(unittest.TestCase):
    def begin(self, store, *, method="POST", path="/api/v1/forceexit", payload=None):
        return audit.begin_operation(store, actor="admin", source_ip="127.0.0.1",
                                     method=method, path=path, payload=payload)

    def test_known_routes_aliases_scope_and_read_exclusion(self):
        cases = [
            ("POST", "/api/v1/forceexit", "force_exit"),
            ("POST", "/api/web/v1/forcesell/", "force_exit"),
            ("POST", "/v1/forcebuy", "force_enter"),
            ("POST", "/api/v1/stopbuy", "pause_entries"),
            ("POST", "/api/v1/stopentry", "pause_entries"),
            ("POST", "/api/v1/pause", "pause_entries"),
            ("DELETE", "/api/v1/trades/12", "delete_trade"),
            ("DELETE", "/api/v1/trades/12/open-order", "cancel_open_order"),
            ("POST", "/api/v1/trades/12/reload", "reload_trade"),
            ("DELETE", "/api/v1/locks/2", "lock_remove"),
            ("POST", "/api/v1/locks/delete", "lock_remove"),
            ("POST", "/api/v1/blacklist", "blacklist_add"),
            ("DELETE", "/api/v1/blacklist", "blacklist_remove"),
            ("POST", "/api/v1/new-control", "other_write"),
            ("PATCH", "/api/future/control", "other_write"),
            ("GET", "/api/v1/forceexit", None),
            ("POST", "/api/v1/pair_candles", None),
        ]
        for method, path, expected in cases:
            with self.subTest(method=method, path=path):
                self.assertEqual(audit.classify_action(method, path), expected)

    def test_request_is_committed_before_begin_returns(self):
        store = Store()
        request = self.begin(store, payload=b'{"tradeid":"all","amount":5,"ordertype":"market"}')
        self.assertEqual(store.commits, 1)
        event = store.events[request.event_id]
        self.assertEqual(event["event_type"], audit.REQUEST_EVENT)
        self.assertEqual(event["source_id"], "trading-operation/" + request.request_id)
        self.assertEqual(event["payload"]["target"], {"trade_id": "all", "amount": 5, "order_type": "market"})
        self.assertEqual(event["payload"]["actor"], "admin")

    def test_sensitive_payload_query_fields_and_response_never_persist(self):
        store = Store()
        request = self.begin(store, path="/api/v1/forceenter?token=secret-query", payload={
            "pair": "ADA/USDT:USDT", "side": "long", "stakeamount": 337,
            "price": 0.34, "leverage": 1, "ordertype": "limit",
            "password": "secret-password", "token": "secret-token",
            "entry_tag": "secret-tag", "Authorization": "secret-header",
            "username": "forged-actor",
        })
        audit.finish_operation(store, request, upstream_status=200,
                               response_payload=b'{"status":"accepted","token":"secret-response"}')
        persisted = json.dumps(store.events)
        self.assertNotIn("secret-", persisted)
        self.assertNotIn("forged-actor", persisted)
        self.assertEqual(request.payload["path"], "/api/v1/forceenter")
        self.assertEqual(request.payload["target"]["stake_amount"], 337)

    def test_unknown_path_is_redacted_but_still_audited(self):
        request = self.begin(Store(), path="/api/v1/control/secret-token?password=secret-value")
        self.assertEqual(request.payload["action"], "other_write")
        self.assertEqual(request.payload["path"], "/api/v1/[unclassified]")

    def test_untrusted_business_fields_do_not_break_audit_or_escape_whitelist(self):
        request = self.begin(Store(), path="/api/v1/forceenter", payload={
            "pair": "secret-token", "side": ["long"], "ordertype": {"token": "secret"},
            "leverage": True, "stakeamount": float("nan"), "price": "secret-value",
        })
        self.assertEqual(request.payload["target"], {})
        for payload, state in [(b"{bad-json", "malformed"), (b"null", "non_object"), (None, "empty")]:
            with self.subTest(state=state):
                self.assertEqual(self.begin(Store(), payload=payload).payload["payload_state"], state)

    def test_percent_encoded_trade_id_and_trailing_slash_are_supported(self):
        request = self.begin(Store(), method="DELETE", path="/api/v1/trades/%31%32/")
        self.assertEqual(request.payload["target"], {"trade_id": "12"})
        self.assertEqual(request.payload["path"], "/api/v1/trades/12")

    def test_database_failure_before_forward_can_be_caught_as_503(self):
        for state in ("unavailable", "commit_failure"):
            store = Store()
            setattr(store, state, True)
            with self.subTest(state=state), self.assertRaises(audit.AuditUnavailable):
                self.begin(store)
            self.assertEqual(store.events, {})

    def test_result_failure_leaves_persisted_request_as_unknown(self):
        store = Store()
        request = self.begin(store)
        store.unavailable = True
        with self.assertRaises(audit.AuditUnavailable):
            audit.finish_operation(store, request, upstream_status=200)
        store.unavailable = False
        item = audit.recent_operations(store)[0]
        self.assertEqual(item["outcome"], "unknown")
        self.assertIsNone(item["result_event_id"])

    def test_http_transport_and_business_outcomes_are_distinct(self):
        cases = [
            (200, b'{"status":"accepted"}', None, "accepted", None),
            (200, b'{"status":"Error entering trade"}', None, "rejected", "upstream_business_error"),
            (200, b'{"errors":{"pair":"sensitive-message"}}', None, "unknown", "upstream_partial_error"),
            (200, b"bad-response", None, "unknown", "unknown_response"),
            (200, b"", None, "unknown", "unknown_response"),
            (204, b"", None, "accepted", None),
            (409, None, None, "rejected", "upstream_http_error"),
            (408, None, None, "unknown", None),
            (503, None, "upstream_http_error", "unknown", "upstream_http_error"),
            (None, None, "timeout", "unknown", "timeout"),
            (None, None, "connection_error", "unknown", "connection_error"),
        ]
        for status, response, error, outcome, expected_error in cases:
            store = Store()
            request = self.begin(store)
            with self.subTest(status=status, response=response):
                event_id = audit.finish_operation(store, request, upstream_status=status,
                                                  response_payload=response, error_code=error)
                result = store.events[event_id]["payload"]
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(result["error_code"], expected_error)
                self.assertEqual(result["request_id"], request.request_id)

    def test_accepted_cannot_override_transport_failure_or_http_business_error(self):
        store = Store()
        request = self.begin(store)
        for options in ({}, {"upstream_status": 500}, {"upstream_status": 200, "error_code": "timeout"},
                        {"upstream_status": 200, "response_payload": {"status": "Error"}}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                audit.finish_operation(store, request, outcome="accepted", **options)
        self.assertEqual(len(store.events), 1)

    def test_not_forwarded_and_error_enum(self):
        store = Store()
        request = self.begin(store)
        audit.finish_operation(store, request, outcome="not_forwarded", error_code="credential_unavailable")
        with self.assertRaises(ValueError):
            audit.finish_operation(store, request, error_code="DSN=secret-password")
        self.assertEqual(audit.recent_operations(store)[0]["outcome"], "not_forwarded")

    def test_append_retries_are_idempotent_but_conflicting_result_is_rejected(self):
        store = Store()
        request = self.begin(store)
        with patch.object(audit, "_now", return_value="2026-10-10T12:00:00+00:00"):
            first = audit.finish_operation(store, request, upstream_status=200)
            self.assertEqual(audit.finish_operation(store, request, upstream_status=200), first)
            with self.assertRaises(audit.AuditUnavailable):
                audit.finish_operation(store, request, upstream_status=409)
        self.assertEqual(len(store.events), 2)
        self.assertEqual(store.events[first]["payload"]["outcome"], "accepted")

    def test_recent_requests_keep_missing_results_and_sort_newest_first(self):
        store = Store()
        closed = self.begin(store, payload={"tradeid": 12})
        audit.finish_operation(store, closed, upstream_status=409)
        pending = self.begin(store, payload={"tradeid": 13})
        items = audit.recent_operations(store)
        self.assertEqual([item["request_id"] for item in items], [pending.request_id, closed.request_id])
        self.assertEqual([item["outcome"] for item in items], ["unknown", "rejected"])
        self.assertEqual(len(audit.recent_operations(store, limit=1)), 1)
        self.assertEqual(audit.recent_operations(store, limit=0), [])
        self.assertEqual(audit.audit_payload(store)["coverage"], "authenticated_proxy_only")

    def test_invalid_actor_ip_status_and_limit_are_rejected(self):
        for actor, source_ip in [("", "127.0.0.1"), ("admin\nforged", "127.0.0.1"), ("admin", "forwarded-secret")]:
            with self.assertRaises(ValueError):
                audit.begin_operation(Store(), actor=actor, source_ip=source_ip,
                                      method="POST", path="/api/v1/start")
        request = self.begin(Store())
        for status in [True, 99, 600, "200"]:
            with self.assertRaises(ValueError):
                audit.finish_operation(Store(), request, upstream_status=status)
        for limit in [True, -1, 501, "100"]:
            with self.assertRaises(ValueError):
                audit.recent_operations(Store(), limit=limit)


@unittest.skipUnless(os.getenv("QUANT_TEST_DATABASE_URL"), "requires isolated PostgreSQL test DSN")
class TradingAuditPostgreSQLTests(unittest.TestCase):
    """Each test owns a fresh schema; never touches bot or production tables."""

    def setUp(self):
        import psycopg
        from psycopg import sql
        from data_store import DataStore

        self.psycopg = psycopg
        self.sql = sql
        self.dsn = os.environ["QUANT_TEST_DATABASE_URL"]
        self.schema = "test_trading_audit_" + uuid.uuid4().hex[:16]
        self.store = DataStore(dsn=self.dsn, schema=self.schema, max_connections=2)
        self.addCleanup(self._drop_owned_schema)
        self.store.initialize()

    def _drop_owned_schema(self):
        self.store.close()
        with self.psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(self.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                self.sql.Identifier(self.schema)))

    def begin(self, trade_id):
        return audit.begin_operation(
            self.store, actor="test-account", source_ip="::1", method="POST",
            path="/api/v1/forceexit?token=secret-query", payload=json.dumps({
                "tradeid": trade_id, "amount": 1.25, "ordertype": "market",
                "password": "secret-password", "token": "secret-token",
            }).encode(),
        )

    def test_real_transactions_join_results_and_keep_unfinished_requests_unknown(self):
        complete = self.begin(12)
        audit.finish_operation(self.store, complete, upstream_status=200,
                               response_payload=b'{"status":"accepted","token":"secret-response"}')
        pending = self.begin(13)
        items = audit.recent_operations(self.store)
        self.assertEqual([item["request_id"] for item in items], [pending.request_id, complete.request_id])
        self.assertEqual([item["outcome"] for item in items], ["unknown", "accepted"])
        self.assertIsNone(items[0]["result_event_id"])
        self.assertEqual(items[1]["target"], {"trade_id": "12", "amount": 1.25, "order_type": "market"})
        self.assertEqual(items[1]["actor"], "test-account")
        self.assertEqual(items[1]["source_ip"], "::1")
        self.assertEqual(len(audit.recent_operations(self.store, limit=1)), 1)
        with self.store._connection() as conn:
            rows = conn.execute("SELECT payload::text AS text FROM research_ledger_events").fetchall()
        self.assertNotIn("secret-", json.dumps(rows))

    def test_real_immutable_retry_and_conflict_preserve_prior_result(self):
        request = self.begin("all")
        with patch.object(audit, "_now", return_value="2026-10-10T12:00:00+00:00"):
            result_id = audit.finish_operation(self.store, request, upstream_status=409)
            retry_id = audit.finish_operation(self.store, request, upstream_status=409)
            self.assertEqual(result_id, retry_id)
            with self.assertRaises(audit.AuditUnavailable):
                audit.finish_operation(self.store, request, upstream_status=200)
        with self.store._connection() as conn:
            row = conn.execute("SELECT count(*) AS count FROM research_ledger_events").fetchone()
        self.assertEqual(row["count"], 2)
        self.assertEqual(audit.recent_operations(self.store)[0]["outcome"], "rejected")

    def test_real_transport_failure_is_unknown_and_business_error_is_rejected(self):
        timeout = self.begin(21)
        audit.finish_operation(self.store, timeout, error_code="timeout")
        business_failure = self.begin(22)
        audit.finish_operation(self.store, business_failure, upstream_status=200,
                               response_payload={"status": "Error: trade not found"})
        items = audit.recent_operations(self.store)
        self.assertEqual(items[0]["outcome"], "rejected")
        self.assertEqual(items[1]["outcome"], "unknown")
        self.assertEqual(items[1]["error_code"], "timeout")
        self.assertIsNone(items[1]["upstream_status"])


if __name__ == "__main__":
    unittest.main()

"""Integrity failures remain visible without mixing active and frozen data."""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
from unittest.mock import patch
import uuid

import pytest

from tools import check_candle_integrity as check


def dataset(rows, **changes):
    return {"exchange": "binance", "market": "futures", "pair": "BTC/USDT:USDT",
            "timeframe": "5m", "candle_type": "futures", "rows": rows, **changes}


def row(timestamp, closed=True):
    return {"timestamp": timestamp, "extras": {"closed": closed}}


def runtime():
    assert importlib.util.find_spec("tools.candle_integrity_runtime"), "windowed audit capability missing"
    return importlib.import_module("tools.candle_integrity_runtime")


def test_forming_or_future_bar_cannot_mask_a_stale_final_close():
    report = check.inspect_dataset(dataset([row(0), row(1_200_000, False), row(1_500_000)]),
                                   required_fields=(), formula_check=False,
                                   max_age_bars=1, as_of_ms=1_500_000)
    assert "stale_tail" in report["errors"]
    assert report["tail"]["last_closed_end_ms"] == 300_000
    assert report["latest_observed_timestamp"] == 1_500_000
    assert report["latest_final_closed_timestamp"] == 0


def test_missing_closed_flag_does_not_claim_final_close():
    report = check.inspect_dataset(dataset([{"timestamp": 1_200_000, "extras": {}}]),
                                   required_fields=(), formula_check=False,
                                   max_age_bars=1, as_of_ms=1_500_000)
    assert report["tail"]["passes"] is False
    assert report["tail"]["last_closed_end_ms"] is None


def test_mark_policy_does_not_require_trade_volume_fields_and_frozen_has_no_staleness():
    payload = {"datasets": [dataset([row(0), row(300_000)], candle_type="mark",
                                    policy={"mode": "frozen", "source": "native"})]}
    report = check.inspect_snapshot(payload, max_age_bars=1, as_of_ms=10_000_000)
    assert report["ok"] is True
    mark = report["datasets"][0]
    assert mark["policy"]["mode"] == "frozen"
    assert mark["fields"] == {}
    assert mark["tail"]["status"] == "not_applicable_frozen"


def test_active_policy_keeps_missing_fields_as_failures():
    report = check.inspect_snapshot({"datasets": [dataset([row(0)])]},
                                    max_age_bars=1, as_of_ms=300_000)
    assert report["ok"] is False
    assert "field_coverage:num_trades" in report["datasets"][0]["errors"]


def test_one_dataset_timeout_is_reported_and_next_dataset_is_still_checked():
    module = runtime()
    identities = [dataset([]), dataset([], pair="ETH/USDT:USDT")]
    def read(identity, *, start_ms, end_ms, timeout_seconds):
        if identity["pair"].startswith("BTC"):
            raise TimeoutError("private server detail")
        return [row(600_000), row(900_000)]
    payload = module.collect_windows(identities, read, window_bars=2, as_of_ms=1_200_000,
                                     timeout_seconds=5)
    report = check.inspect_snapshot(payload, required_fields=(), formula_check=False,
                                    max_age_bars=1, as_of_ms=1_200_000)
    assert report["ok"] is False
    assert report["failed_dataset_count"] == 1
    assert report["successful_dataset_count"] == 1
    assert report["datasets"][0]["errors"] == ["dataset_read_failed"]
    assert report["datasets"][0]["read_error"] == "TimeoutError"
    assert report["datasets"][1]["ok"] is True
    assert "private server detail" not in json.dumps(report)


def test_window_bounds_keep_only_requested_recent_interval_and_full_history_is_explicit():
    module = runtime()
    def read(identity, *, start_ms, end_ms, timeout_seconds):
        assert start_ms == 600_000
        assert end_ms == 1_200_000
        assert timeout_seconds == 5
        return [row(600_000), row(900_000)]
    payload = module.collect_windows([dataset([])], read, window_bars=2, as_of_ms=1_200_000,
                                     timeout_seconds=5)
    assert payload["datasets"][0]["check_window"] == {"start_ms": 600_000, "end_ms": 1_200_000,
                                                    "bars": 2, "scope": "recent"}
    def full(identity, *, start_ms, end_ms, timeout_seconds):
        assert start_ms is None
        return [row(0)]
    payload = module.collect_windows([dataset([])], full, window_bars=None, as_of_ms=1_200_000,
                                     timeout_seconds=5)
    assert payload["datasets"][0]["check_window"]["scope"] == "full_history"


def test_missing_start_of_requested_window_is_not_a_complete_dataset():
    item = dataset([row(900_000)], check_window={"start_ms": 600_000, "end_ms": 1_200_000,
                                                 "bars": 2, "scope": "recent"})
    report = check.inspect_dataset(item, required_fields=(), formula_check=False,
                                    as_of_ms=1_200_000, max_age_bars=1)
    assert report["ok"] is False
    assert "window_coverage_missing" in report["errors"]
    assert report["window_coverage"]["expected_closed_rows"] == 2
    assert report["window_coverage"]["missing_closed_rows"] == 1
    assert report["window_coverage"]["continuous_closed_tail"] == 1


def test_frozen_window_bound_is_explicit_and_does_not_follow_mutable_summary():
    module = runtime()
    item = dataset([], last_timestamp=9_000_000,
                   policy={"mode": "frozen", "end_ms": 1_200_000})
    def read(identity, *, start_ms, end_ms, timeout_seconds):
        assert (start_ms, end_ms) == (600_000, 1_200_000)
        return [row(600_000), row(900_000)]
    payload = module.collect_windows([item], read, window_bars=2, as_of_ms=10_000_000)
    assert payload["datasets"][0]["check_window"]["end_ms"] == 1_200_000


def test_atomic_report_write_failure_keeps_previous_json(tmp_path):
    output = tmp_path / "report.json"
    output.write_text('{"old":true}')
    with patch("json.dump", side_effect=OSError("disk failed")):
        with pytest.raises(OSError):
            check._write_json(output, {"new": True})
    assert json.loads(output.read_text()) == {"old": True}


def test_watch_failure_publishes_report_and_retains_previous_success(tmp_path):
    output, status = tmp_path / "report.json", tmp_path / "status.json"
    args = argparse.Namespace(interval=1, status_document=status, output=output)
    calls = []
    def audit():
        calls.append(1)
        if len(calls) == 2:
            raise TimeoutError("private database address")
        return {"ok": True, "dataset_count": 1, "error_count": 0, "datasets": [],
                "generated_at_utc": "2026-11-15T18:00:00+00:00"}
    with patch.object(check, "require_integrity_monitor_gate"):
        check.run_watch(args, dsn="", cycles=2, audit=audit, sleep=lambda _: None)
    report = json.loads(output.read_text())
    latest = json.loads(status.read_text())
    assert report["ok"] is False
    assert report["last_error"] == "TimeoutError"
    assert latest["last_success_at_utc"] == "2026-11-15T18:00:00+00:00"
    assert latest["started_at_utc"]
    assert latest["finished_at_utc"]
    assert "private database address" not in json.dumps(report)


def test_report_publication_failure_cannot_leave_healthy_watch_status(tmp_path):
    output, status = tmp_path / "readonly/report.json", tmp_path / "status.json"
    args = argparse.Namespace(interval=1, status_document=status, output=output)
    writes = check._write_json
    calls = []
    def publish(path, payload):
        if path == output:
            raise OSError("private volume path")
        return writes(path, payload)
    def audit():
        calls.append(1)
        return {"ok": True, "dataset_count": 1, "error_count": 0, "datasets": []}
    with patch.object(check, "require_integrity_monitor_gate"), patch.object(check, "_write_json", publish):
        assert check.run_watch(args, dsn="", cycles=2, audit=audit, sleep=lambda _: None) == 0
    assert len(calls) == 2
    latest = json.loads(status.read_text())
    assert latest["ok"] is False
    assert latest["last_error"] == "report_publication_failed"
    assert "private volume path" not in json.dumps(latest)


def test_misaligned_bar_cannot_be_final_closed_even_when_flag_is_true():
    report = check.inspect_dataset(dataset([row(900_001)]), required_fields=(),
                                   formula_check=False, max_age_bars=1, as_of_ms=1_500_000)
    assert "timestamp_misaligned" in report["errors"]
    assert report["latest_final_closed_timestamp"] is None


@pytest.mark.parametrize("policy", [[], False, {"mdoe": "frozen"}])
def test_invalid_policy_cannot_silently_become_default_active(policy):
    with pytest.raises(ValueError):
        check.inspect_dataset(dataset([row(0)], policy=policy))


def test_bounded_frozen_audit_requires_fixed_end_instead_of_unbounded_scan():
    module = runtime()
    def read(*args, **kwargs):
        raise AssertionError("frozen policy unexpectedly requested all history")
    with pytest.raises(ValueError, match="end_ms"):
        module.collect_windows([dataset([], policy={"mode": "frozen"})], read,
                                window_bars=750, as_of_ms=1_200_000)


@pytest.mark.skipif(not os.getenv("QUANT_TEST_DATABASE_URL"), reason="isolated PostgreSQL required")
def test_database_reader_discovers_direct_inserts_and_reads_exact_identity_windows():
    import psycopg
    from psycopg import sql

    module = runtime()
    dsn = os.environ["QUANT_TEST_DATABASE_URL"]
    schema = "integrity_contract_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        database = conn.execute("SELECT current_database()").fetchone()[0]
        assert database == "quant_test", "integration test requires quant_test"
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            conn.execute(sql.SQL("CREATE TABLE {}.candles (exchange text, market text, pair text, "
                         "timeframe text, candle_type text, timestamp bigint, volume float8, extras jsonb, "
                         "PRIMARY KEY(exchange,market,pair,timeframe,candle_type,timestamp))").format(sql.Identifier(schema)))
            for candle_type in ("futures", "orderflow"):
                for timestamp in (0, 600_000, 900_000):
                    conn.execute(sql.SQL("INSERT INTO {}.candles VALUES "
                                 "('binance','futures','BTC/USDT:USDT','5m',%s,%s,1,%s::jsonb)")
                                 .format(sql.Identifier(schema)), (candle_type, timestamp, '{"closed":true}'))
            result = module.read_database_windows(dsn=dsn, schema=schema, pairs=["BTC/USDT:USDT"],
                                                   timeframes=["5m"], window_bars=2, as_of_ms=1_200_000)
            assert {item["candle_type"] for item in result["datasets"]} == {"futures", "orderflow"}
            assert all([row["timestamp"] for row in item["rows"]] == [600_000, 900_000]
                       for item in result["datasets"])
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

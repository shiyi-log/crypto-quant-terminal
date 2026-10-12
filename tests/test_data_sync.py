"""同步层验证：只读源、市场隔离、增量重试和批次幂等。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sqlite3
import uuid
import zipfile
from contextlib import nullcontext, redirect_stdout
from io import StringIO

import numpy as np
import pandas as pd
import tempfile
import unittest
from unittest.mock import patch

from data_sync import (
    DataSynchronizer, candle_close_timestamp, candle_identity, exclusive_sync, main,
    sqlite_snapshot, temporary_source, timestamp_ms,
)


class MemoryStore:
    """在单元测试中模拟数据库 API，保留行主键和成功源指纹。"""
    def __init__(self):
        self.documents = {}
        self.events = {}
        self.sources = {}
        self.candles = {}
        self.series = {}
        self.external = {}
        self.artifacts = {}
        self.users = None
        self.sync = {}
        self.batch_lengths = []

    def source_state(self, key):
        return self.sources.get(key)

    def record_source(self, key, path, kind, mtime_ns, size, row_count, error=None):
        state = dict(self.sources.get(key, {}))
        if error is None:
            state.update(mtime_ns=mtime_ns, size=size, row_count=row_count)
        state.update(path=path, kind=kind, error=error)
        self.sources[key] = state

    def put_document(self, key, payload, **kwargs):
        self.documents[key] = payload

    def replace_events(self, key, rows, **kwargs):
        self.events[key] = rows
        return len(rows)

    def migrate_users(self, path):
        if self.users is not None:
            return False
        self.users = json.loads(Path(path).read_text())
        return True

    def replace_external_snapshot(self, source, snapshot):
        return sum(self.replace_external(source, table, rows) for table, rows in snapshot.items())

    def replace_external(self, source, table, rows):
        self.external[(source, table)] = rows
        return len(rows)

    def upsert_candles(self, exchange, market, pair, timeframe, candle_type, rows, source_key=None):
        self.batch_lengths.append(len(rows))
        for row in rows:
            self.candles[(exchange, market, pair, timeframe, candle_type, row["timestamp"])] = row
        return len({row["timestamp"] for row in rows})

    def upsert_series(self, source, metric, asset, rows, source_key=None):
        for row in rows:
            self.series[(source, metric, asset, row["timestamp"])] = row
        return len({row["timestamp"] for row in rows})

    def save_artifact(self, key, path, size, mtime_ns, kind, metadata=None):
        self.artifacts[key] = {"size": size, "metadata": metadata}
        return 1

    def set_sync_state(self, state):
        self.sync.update(state)

    def prune_temporary_sources(self):
        stale = [key for key in self.sources if temporary_source(Path(key))]
        for key in stale:
            del self.sources[key]
        return len(stale)




def write_json(root, relative, payload):
    path = root / "bot" / "user_data" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def candle_frame(dates=None):
    dates = dates if dates is not None else pd.to_datetime(["2026-01-01", "2026-01-02"])
    return pd.DataFrame({"date": dates, "open": 10., "high": 12., "low": 9., "close": 11., "volume": "4"})


IDENTITY_CASES = [('binance/BTC_USDT-5m.feather', ('binance', 'spot', 'BTC/USDT', '5m', 'spot')), ('binance/futures/BTC_USDT_USDT-1d-futures.feather', ('binance', 'futures', 'BTC/USDT:USDT', '1d', 'futures')), ('binance/futures/BTC_USDT_USDT-8h-funding_rate.feather', ('binance', 'futures', 'BTC/USDT:USDT', '8h', 'funding_rate')), ('okx/futures/ETH_USDT_USDT-1d-mark.feather', ('okx', 'futures', 'ETH/USDT:USDT', '1d', 'mark')), ('merged/BTC-1d-merged.feather', ('merged', 'futures', 'BTC/USDT:USDT', '1d', 'merged')), ('orderflow/BTC_5m_orderflow.feather', ('binance', 'futures', 'BTC/USDT:USDT', '5m', 'orderflow'))]


class DataSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        (self.project / "bot/user_data").mkdir(parents=True)

    def test_daemon_clock_crossing_deadline_never_sleeps_negative(self):
        handlers = {}
        sleeps = []
        rounds = 0

        def sleep(seconds):
            # The old loop checks 100.99, then samples 101.01 to compute sleep.
            # This reproduces its production ValueError without actually waiting.
            if seconds < 0:
                raise ValueError("sleep length must be non-negative")
            sleeps.append(seconds)

        def run(**_kwargs):
            nonlocal rounds
            rounds += 1
            if rounds == 2:
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            return {"errors": 0}

        with patch("data_sync.exclusive_sync", return_value=nullcontext()), \
             patch("data_store.DataStore") as store, \
             patch("data_sync.DataSynchronizer") as synchronizer, \
             patch("data_sync.signal.signal", side_effect=lambda number, handler:
                   handlers.update({number: handler})), \
             patch("data_sync.time.monotonic", side_effect=[100., 100.99, 101.01, 102.]), \
             patch("data_sync.time.sleep", side_effect=sleep), \
             redirect_stdout(StringIO()):
            synchronizer.return_value.run.side_effect = run
            result = main(["--daemon", "--fast", "--interval", "1",
                           "--root", str(self.project)])

        self.assertEqual(result, 0)
        self.assertEqual(rounds, 2)
        self.assertEqual(len(sleeps), 1)
        self.assertGreater(sleeps[0], 0)
        self.assertLessEqual(sleeps[0], .25)
        store.return_value.initialize.assert_called_once()

    def test_identity_keeps_markets_separate(self):
        for filename, expected in IDENTITY_CASES:
            with self.subTest(filename=filename):
                base = Path("/tmp/data")
                self.assertEqual(candle_identity(base / filename, base), expected)

    def test_identity_rejects_unknown_data_directory(self):
        base = Path("/tmp/data")
        with self.assertRaises(ValueError):
            candle_identity(base / "research_1h/BTC_USDT-1h.feather", base)

    def test_unknown_data_directories_are_skipped_and_reported(self):
        project = self.project
        directory = project / "bot/user_data/data/research_1h"
        directory.mkdir(parents=True)
        candle_frame().to_feather(directory / "BTC_USDT-1h.feather")
        (directory / "notes.json").write_text('{"research": true}', encoding="utf-8")
        (directory / "returns.csv").write_text("date,return\n2026-01-01,0.1\n", encoding="utf-8")

        store = MemoryStore()
        result = DataSynchronizer(store, project).run()

        assert result["errors"] == 0
        assert result["rows"] == 0
        assert result["warnings"] == [
            "忽略未授权行情目录: bot/user_data/data/research_1h"
        ]
        assert store.candles == {}
        assert store.documents == {}
        assert store.events == {}

    def test_temporary_atomic_cache_is_ignored_and_stale_source_error_is_pruned(self):
        cache = self.project / "bot/user_data/cache"
        cache.mkdir(parents=True)
        temporary_cache = cache / "panel_128_seq.npy.tmp.npy"
        temporary_cache.write_bytes(b"interrupted numpy write")
        final_cache = cache / "panel_128_seq.npy"
        np.save(final_cache, np.zeros((1, 2, 3), dtype=np.float32))

        store = MemoryStore()
        temporary_key = "bot/user_data/cache/panel_64_seq.npy.tmp.npy"
        regular_key = "bot/user_data/cache/old-final-cache.npy"
        store.sources[temporary_key] = {"error": "ValueError", "path": temporary_key}
        store.sources[regular_key] = {"error": "OSError", "path": regular_key}

        result = DataSynchronizer(store, self.project).run()

        self.assertTrue(temporary_source(temporary_cache))
        self.assertFalse(temporary_source(final_cache))
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["pruned_sources"], 1)
        self.assertNotIn(temporary_key, store.sources)
        self.assertEqual(store.sources[regular_key]["error"], "OSError")
        self.assertNotIn(temporary_key, store.artifacts)
        self.assertIn("bot/user_data/cache/panel_128_seq.npy", store.artifacts)

    def test_utc_timestamp_and_nat(self):
        timestamps = timestamp_ms(["2026-01-01T08:00:00+08:00", "invalid"])
        assert timestamps.iloc[0] == 1767225600000
        assert pd.isna(timestamps.iloc[1])
        assert timestamp_ms([1767225600000]).iloc[0] == 1767225600000

    def test_datetime_resolution_is_always_milliseconds(self):
        for unit in ("ms", "us", "ns"):
            dates = pd.Series(pd.to_datetime(["2019-09-08"], utc=True)).astype(f"datetime64[{unit}, UTC]")
            self.assertEqual(timestamp_ms(dates).iloc[0], 1567900800000)

    def test_candle_close_boundary_uses_actual_timeframe_and_calendar_months(self):
        opened = int(pd.Timestamp("2026-01-01T00:00:00Z").timestamp() * 1000)
        for timeframe, duration in (("1s", 1000), ("5m", 300_000),
                                    ("15m", 900_000), ("1h", 3_600_000),
                                    ("4h", 14_400_000), ("1d", 86_400_000),
                                    ("1w", 604_800_000)):
            with self.subTest(timeframe=timeframe):
                self.assertEqual(candle_close_timestamp(opened, timeframe), opened + duration)
        february = int(pd.Timestamp("2024-02-01T00:00:00Z").timestamp() * 1000)
        self.assertEqual(candle_close_timestamp(february, "1M"),
                         int(pd.Timestamp("2024-03-01T00:00:00Z").timestamp() * 1000))
        self.assertEqual(candle_close_timestamp(opened, "2M"),
                         int(pd.Timestamp("2026-03-01T00:00:00Z").timestamp() * 1000))
        with self.assertRaises(ValueError):
            candle_close_timestamp(opened, "0m")

    def test_feather_closed_flags_preserve_partial_rows_and_current_candle(self):
        directory = self.project / "bot/user_data/data/binance/futures"
        directory.mkdir(parents=True)
        path = directory / "BTC_USDT_USDT-15m-futures.feather"
        frame = candle_frame(pd.to_datetime(["2026-10-12T15:15:00Z", "2026-10-12T15:30:00Z",
                                            "2026-10-12T15:45:00Z", "2026-10-12T16:00:00Z"]))
        frame["closed"] = [False, None, None, True]
        frame["quote_volume"] = [100., 101., 102., 103.]
        frame.to_feather(path)
        observed = pd.Timestamp("2026-10-12T16:05:19Z").timestamp()
        os.utime(path, (observed, observed))
        store = MemoryStore()
        with patch("data_sync.time.time", return_value=observed):
            result = DataSynchronizer(store, self.project).run()
        self.assertEqual(result["errors"], 0)
        rows = list(store.candles.values())
        self.assertEqual([row["extras"]["closed"] for row in rows], [False, True, True, False])
        self.assertEqual([row["extras"]["quote_volume"] for row in rows], [100., 101., 102., 103.])

    def test_static_feather_tail_stays_partial_after_its_close_boundary(self):
        directory = self.project / "bot/user_data/data/binance"
        directory.mkdir(parents=True)
        path = directory / "BTC_USDT-1h.feather"
        candle_frame(pd.to_datetime(["2026-10-12T14:00:00Z", "2026-10-12T15:00:00Z"])).to_feather(path)
        captured = pd.Timestamp("2026-10-12T15:30:00Z").timestamp()
        os.utime(path, (captured, captured))
        store = MemoryStore()
        sync = DataSynchronizer(store, self.project)
        # Even a forced import after 16:00 must respect the file's 15:30 snapshot.
        with patch("data_sync.time.time", return_value=captured + 7200):
            result = sync.run()
            self.assertEqual(result["rows"], 2)
            self.assertEqual([row["extras"]["closed"] for row in store.candles.values()], [True, False])
            self.assertEqual(sync.run()["skipped"], 1)
            self.assertEqual(sync.run(force=True)["rows"], 2)
            self.assertEqual([row["extras"]["closed"] for row in store.candles.values()], [True, False])

    def test_feather_close_boundary_cannot_be_newer_than_current_utc_time(self):
        directory = self.project / "bot/user_data/data/binance"
        directory.mkdir(parents=True)
        path = directory / "BTC_USDT-1h.feather"
        candle_frame(pd.to_datetime(["2026-10-12T15:00:00Z"])).to_feather(path)
        observed = pd.Timestamp("2026-10-12T15:30:00Z").timestamp()
        os.utime(path, (observed + 7200, observed + 7200))
        store = MemoryStore()
        with patch("data_sync.time.time", return_value=observed):
            self.assertEqual(DataSynchronizer(store, self.project).run()["errors"], 0)
        self.assertIs(next(iter(store.candles.values()))["extras"]["closed"], False)

    def test_json_incremental_nested_docs_exclusions_and_event_retry(self):
        project = self.project
        store = MemoryStore()
        path = write_json(project, "research_summary.json", {"round": 1, "api_key": "PRIVATE"})
        write_json(project, "models/example/backtest_detail.json", {"trades": [1]})
        write_json(project, "config_live.json", {"api_key": "PRIVATE"})
        write_json(project, "research_progress_smoke.json", {"smoke": True})
        write_json(project, "private/hidden.json", {"private": True})
        events = project / "bot/user_data/iteration_history.jsonl"
        events.write_text('{"round": 1}\n', encoding="utf-8")
        sync = DataSynchronizer(store, project)
        first = sync.run(fast=True)
        assert first["files"] == 3
        assert first["rows"] == 3
        assert store.documents["research_summary.json"] == {"round": 1}
        assert "models/example/backtest_detail.json" in store.documents
        assert set(store.documents) == {"research_summary.json", "models/example/backtest_detail.json"}
        second = sync.run(fast=True)
        assert second["files"] == 0 and second["skipped"] == 3
        source_key = "bot/user_data/iteration_history.jsonl"
        successful_stamp = store.sources[source_key]["mtime_ns"]
        events.write_text('{"round": 1}\n{"round":', encoding="utf-8")
        failed = sync.run(fast=True)
        assert failed["errors"] == 1
        assert store.sources[source_key]["mtime_ns"] == successful_stamp
        assert store.events["iteration_history.jsonl"] == [{"round": 1}]
        events.write_text('{"round": 1}\n{"round": 2}\n', encoding="utf-8")
        path.write_text('{"round": 2}', encoding="utf-8")
        recovered = sync.run(fast=True)
        assert recovered["errors"] == 0 and recovered["files"] == 2
        assert len(store.events["iteration_history.jsonl"]) == 2
        assert store.sync["running"] is False

    def test_database_write_failure_does_not_escape_and_failed_source_retries(self):
        path = write_json(self.project, "research_summary.json", {"round": 1})
        store = MemoryStore()
        sync = DataSynchronizer(store, self.project)
        original_record_source = store.record_source
        writes = 0

        def fail_source_record(key, source_path, kind, mtime_ns, size, row_count, error=None):
            nonlocal writes
            writes += 1
            if writes <= 2:
                raise OSError("disk full")
            original_record_source(key, source_path, kind, mtime_ns, size, row_count, error)

        store.record_source = fail_source_record
        with redirect_stdout(StringIO()):
            failed = sync.run(fast=True, force=True)
        self.assertEqual(failed["errors"], 1)
        self.assertEqual(failed["storage_errors"], 1)
        self.assertEqual(failed["status"], "error")
        self.assertEqual(store.documents["research_summary.json"], {"round": 1})

        store.record_source = original_record_source
        path.write_text('{"round": 2}', encoding="utf-8")
        with redirect_stdout(StringIO()):
            recovered = sync.run(fast=True)
        self.assertEqual(recovered["errors"], 0)
        self.assertEqual(recovered["rows"], 1)
        self.assertEqual(store.documents["research_summary.json"], {"round": 2})
        self.assertEqual(store.sync["running"], False)

    def test_sync_state_write_failure_is_reported_without_aborting_scan(self):
        write_json(self.project, "research_summary.json", {"round": 1})
        store = MemoryStore()
        original_set_sync_state = store.set_sync_state
        attempts = 0

        def fail_first_state_write(state):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("disk full")
            original_set_sync_state(state)

        store.set_sync_state = fail_first_state_write
        with redirect_stdout(StringIO()):
            result = DataSynchronizer(store, self.project).run(fast=True)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["storage_errors"], 1)
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["running"])
        self.assertEqual(store.documents["research_summary.json"], {"round": 1})
        self.assertFalse(store.sync.get("running", True))

    def test_final_sync_state_write_failure_clears_success_timestamp(self):
        write_json(self.project, "research_summary.json", {"round": 1})
        store = MemoryStore()
        original_set_sync_state = store.set_sync_state

        def fail_final_state_write(state):
            if state.get("running") is False:
                raise OSError("disk full")
            original_set_sync_state(state)

        store.set_sync_state = fail_final_state_write
        with redirect_stdout(StringIO()):
            result = DataSynchronizer(store, self.project).run(fast=True)
        self.assertEqual(result["status"], "error")
        self.assertGreaterEqual(result["storage_errors"], 1)
        self.assertNotIn("last_success_at", result)

    def test_failed_source_is_retried_when_old_success_fingerprint_is_unchanged(self):
        path = write_json(self.project, "research_summary.json", {"round": 1})
        key = "bot/user_data/research_summary.json"
        store = MemoryStore()
        sync = DataSynchronizer(store, self.project)
        with redirect_stdout(StringIO()):
            sync.run(fast=True)
        successful_stamp = (store.sources[key]["mtime_ns"], store.sources[key]["size"])

        with patch.object(store, "record_source", side_effect=OSError("disk full")):
            with redirect_stdout(StringIO()):
                failed = sync.run(fast=True, force=True)
        self.assertEqual(failed["errors"], 1)
        self.assertEqual(failed["storage_errors"], 1)
        self.assertEqual((store.sources[key]["mtime_ns"], store.sources[key]["size"]), successful_stamp)
        self.assertIsNone(store.sources[key].get("error"))

        with patch.object(store, "put_document", wraps=store.put_document) as write:
            with redirect_stdout(StringIO()):
                recovered = sync.run(fast=True)
        write.assert_called_once()
        self.assertEqual(recovered["errors"], 0)
        self.assertEqual(recovered["files"], 1)
        self.assertEqual(recovered["skipped"], 0)
        self.assertEqual(path.stat().st_mtime_ns, successful_stamp[0])

        with redirect_stdout(StringIO()):
            settled = sync.run(fast=True)
        self.assertEqual(settled["errors"], 0)
        self.assertEqual(settled["files"], 0)
        self.assertEqual(settled["skipped"], 1)

    def test_source_callback_write_failure_is_isolated(self):
        write_json(self.project, "research_summary.json", {"round": 1})
        store = MemoryStore()
        original_put_document = store.put_document

        def fail_document_write(*args, **kwargs):
            raise OSError("disk full")

        store.put_document = fail_document_write
        with redirect_stdout(StringIO()):
            result = DataSynchronizer(store, self.project).run(fast=True)
        key = "bot/user_data/research_summary.json"
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["storage_errors"], 0)
        self.assertEqual(store.sources[key]["error"], "OSError")
        self.assertFalse(result["running"])

        store.put_document = original_put_document
        with redirect_stdout(StringIO()):
            recovered = DataSynchronizer(store, self.project).run(fast=True)
        self.assertEqual(recovered["errors"], 0)
        self.assertEqual(store.documents["research_summary.json"], {"round": 1})

    def test_batch_counts_nonfinite_extras_and_idempotence(self):
        project = self.project
        directory = project / "bot/user_data/data/orderflow"
        directory.mkdir(parents=True)
        frame = candle_frame(pd.date_range("2026-01-01", periods=7, tz="UTC"))
        frame["num_trades"] = [1, 2, 3, 4, 5, 6, 7]
        frame["buy_ratio"] = [np.nan, .5, .7, .6, .8, .7, .6]
        frame.loc[6, "close"] = np.inf
        duplicate = frame.iloc[[0]].copy()
        duplicate["close"] = 11.5
        pd.concat([frame, duplicate], ignore_index=True).to_feather(directory / "BTC_5m_orderflow.feather")
        store = MemoryStore()
        sync = DataSynchronizer(store, project, batch_size=2)
        result = sync.run()
        assert result["rows"] == 6
        assert result["rejected_rows"] == 2
        assert store.batch_lengths == [2, 2, 2]
        assert len(store.candles) == 6
        first = next(iter(store.candles.values()))
        assert first["close"] == 11.5
        assert first["extras"]["buy_ratio"] is None
        assert all(key[:5] == ("binance", "futures", "BTC/USDT:USDT", "5m", "orderflow") for key in store.candles)
        assert sync.run()["rows"] == 0
        assert sync.run(force=True)["rows"] == 6
        assert len(store.candles) == 6

    def test_second_candle_batch_failure_retries_unchanged_source_idempotently(self):
        directory = self.project / "bot/user_data/data/binance"
        directory.mkdir(parents=True)
        path = directory / "BTC_USDT-1h.feather"
        candle_frame(pd.date_range("2026-01-01", periods=4, tz="UTC")).to_feather(path)
        stamp = (path.stat().st_mtime_ns, path.stat().st_size)
        source_key = "bot/user_data/data/binance/BTC_USDT-1h.feather"
        store = MemoryStore()
        sync = DataSynchronizer(store, self.project, batch_size=2)
        original_upsert = store.upsert_candles
        attempts = 0

        def fail_second_batch(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise OSError("disk full")
            return original_upsert(*args, **kwargs)

        with patch.object(store, "upsert_candles", side_effect=fail_second_batch), \
             redirect_stdout(StringIO()):
            failed = sync.run()
            self.assertEqual(failed["status"], "error")
            self.assertEqual(failed["errors"], 1)
            self.assertEqual(failed["files"], 0)
            self.assertEqual(failed["rows"], 0)
            self.assertNotIn("last_success_at", failed)
            self.assertEqual(attempts, 2)
            self.assertEqual(len(store.candles), 2)
            self.assertEqual(store.sources[source_key]["error"], "OSError")
            self.assertEqual(store.sources[source_key].get("row_count", 0), 0)

            # Retry the identical file, including the already committed batch.
            recovered = sync.run()
            self.assertEqual(recovered["status"], "completed")
            self.assertEqual(recovered["errors"], 0)
            self.assertEqual(recovered["files"], 1)
            self.assertEqual(recovered["rows"], 4)
            self.assertEqual(attempts, 4)
            self.assertEqual(len(store.candles), 4)
            self.assertIsNone(store.sources[source_key]["error"])
            self.assertEqual(store.sources[source_key]["row_count"], 4)
            self.assertEqual(store.batch_lengths, [2, 2, 2])

            unchanged = sync.run()
            self.assertEqual(unchanged["rows"], 0)
            self.assertEqual(unchanged["skipped"], 1)
            self.assertEqual(attempts, 4)
            self.assertEqual(len(store.candles), 4)
            self.assertEqual(store.sources[source_key]["row_count"], 4)
        self.assertEqual((path.stat().st_mtime_ns, path.stat().st_size), stamp)

    def test_tabular_artifact_failure_preserves_events_and_retries_unchanged_source(self):
        path = self.project / "bot/user_data/cross_sectional.csv"
        path.write_text("time,net,longs\n2026-01-01,0.25,BTC\n2026-01-02,,ETH\n", encoding="utf-8")
        stamp = (path.stat().st_mtime_ns, path.stat().st_size)
        source_key = "bot/user_data/cross_sectional.csv"
        expected = [
            {"time": "2026-01-01", "net": .25, "longs": "BTC"},
            {"time": "2026-01-02", "net": None, "longs": "ETH"},
        ]
        store = MemoryStore()
        sync = DataSynchronizer(store, self.project)
        original_save_artifact = store.save_artifact
        attempts = 0

        def fail_first_artifact(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("disk full")
            return original_save_artifact(*args, **kwargs)

        with patch.object(store, "save_artifact", side_effect=fail_first_artifact), \
             redirect_stdout(StringIO()):
            failed = sync.run()
            self.assertEqual(failed["status"], "error")
            self.assertEqual(failed["errors"], 1)
            self.assertEqual(failed["files"], 0)
            self.assertEqual(failed["rows"], 0)
            self.assertNotIn("last_success_at", failed)
            self.assertEqual(store.events["cross_sectional.csv"], expected)
            self.assertNotIn(source_key, store.artifacts)
            self.assertEqual(store.sources[source_key]["error"], "OSError")

            recovered = sync.run()
            self.assertEqual(recovered["status"], "completed")
            self.assertEqual(recovered["errors"], 0)
            self.assertEqual(recovered["files"], 1)
            self.assertEqual(recovered["rows"], 2)
            self.assertEqual(store.events, {"cross_sectional.csv": expected})
            self.assertEqual(store.artifacts[source_key]["metadata"],
                             {"rows": 2, "columns": ["time", "net", "longs"]})
            self.assertIsNone(store.sources[source_key]["error"])
            self.assertEqual(store.sources[source_key]["row_count"], 2)
            self.assertEqual(attempts, 2)

            unchanged = sync.run()
            self.assertEqual(unchanged["rows"], 0)
            self.assertEqual(unchanged["skipped"], 1)
            self.assertEqual(attempts, 2)
            self.assertEqual(store.events, {"cross_sectional.csv": expected})
        self.assertEqual((path.stat().st_mtime_ns, path.stat().st_size), stamp)

    def test_funding_can_be_negative_and_spot_not_overwritten(self):
        project = self.project
        data_dir = project / "bot/user_data/data/binance"
        (data_dir / "futures").mkdir(parents=True)
        candle_frame().to_feather(data_dir / "BTC_USDT-1h.feather")
        frame = candle_frame()
        frame[["open", "high", "low", "close"]] = -.001
        frame.to_feather(data_dir / "futures/BTC_USDT_USDT-8h-funding_rate.feather")
        store = MemoryStore()
        assert DataSynchronizer(store, project).run()["rows"] == 4
        assert {key[1] for key in store.candles} == {"spot"}
        assert len(store.series) == 2
        assert all(row["value"] == -.001 for row in store.series.values())

    def test_two_column_funding_feather_negative_and_invalid(self):
        directory = self.project / "bot/user_data/data/binance/futures"
        directory.mkdir(parents=True)
        frame = pd.DataFrame({"date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]),
                              "funding_rate": [-.0002, .0001, np.inf]})
        frame.to_feather(directory / "BTC_USDT_USDT-1h-funding_rate.feather")
        store = MemoryStore()
        result = DataSynchronizer(store, self.project).run()
        assert result["errors"] == 0 and result["rows"] == 2
        assert result["rejected_rows"] == 1
        assert list(row["value"] for row in store.series.values()) == [-.0002, .0001]
        assert all(key[:3] == ("binance", "funding_rate", "BTC/USDT:USDT") for key in store.series)
        assert all(row["extras"] == {"market": "futures", "timeframe": "1h"} for row in store.series.values())

    def test_oi_pickle_series_and_array_index(self):
        project = self.project
        data_dir = project / "bot/user_data/data/orderflow"
        data_dir.mkdir(parents=True)
        pd.DataFrame({"date": pd.to_datetime(["2026-01-01"]), "oi": [8.], "oi_val": [80.]}).to_feather(data_dir / "BTC_5m_oi.feather")
        source_dir = project / "bot/user_data/newsrc"
        source_dir.mkdir()
        pd.Series([65., np.nan], index=pd.to_datetime(["2026-01-01", "2026-01-02"])).to_pickle(source_dir / "dvol_btc.pkl")
        pd.DataFrame({"BTC": [-.0001, np.nan]}, index=pd.to_datetime(["2026-01-01", "2026-01-02"])).to_pickle(source_dir / "funding_binance.pkl")
        # 未知 pickle 不能被反序列化。
        (source_dir / "untrusted.pkl").write_bytes(b"not a pickle")
        cache = project / "bot/user_data/cache"
        cache.mkdir()
        np.save(cache / "features.npy", np.zeros((2, 3), dtype="float32"))
        store = MemoryStore()
        result = DataSynchronizer(store, project).run()
        assert result["errors"] == 0 and result["rows"] == 5
        assert len(store.series) == 4
        assert {key[1] for key in store.series} == {"open_interest", "open_interest_value", "dvol", "funding_rate_archive"}
        assert store.artifacts["bot/user_data/cache/features.npy"]["metadata"]["shape"] == [2, 3]

    def test_second_pickle_series_batch_failure_retries_unchanged_source_idempotently(self):
        directory = self.project / "bot/user_data/newsrc"
        directory.mkdir()
        path = directory / "dvol_btc.pkl"
        pd.Series([65., 66., 67., 68.],
                  index=pd.date_range("2026-01-01", periods=4, tz="UTC")).to_pickle(path)
        stamp = (path.stat().st_mtime_ns, path.stat().st_size)
        source_key = "bot/user_data/newsrc/dvol_btc.pkl"
        store = MemoryStore()
        sync = DataSynchronizer(store, self.project, batch_size=2)
        original_upsert = store.upsert_series
        attempts = 0

        def fail_second_batch(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise OSError("disk full")
            return original_upsert(*args, **kwargs)

        with patch.object(store, "upsert_series", side_effect=fail_second_batch), \
             redirect_stdout(StringIO()):
            failed = sync.run()
            self.assertEqual(failed["status"], "error")
            self.assertEqual(failed["errors"], 1)
            self.assertEqual(failed["storage_errors"], 0)
            self.assertEqual(failed["rows"], 0)
            self.assertEqual(failed["files"], 0)
            self.assertNotIn("last_success_at", failed)
            self.assertEqual(len(store.series), 2)
            self.assertEqual(attempts, 2)
            self.assertEqual(store.sources[source_key]["error"], "OSError")

            recovered = sync.run()
            self.assertEqual(recovered["status"], "completed")
            self.assertEqual(recovered["errors"], 0)
            self.assertEqual(recovered["rows"], 4)
            self.assertEqual(recovered["files"], 1)
            self.assertEqual(attempts, 4)
            self.assertEqual(len(store.series), 4)
            self.assertEqual([row["value"] for row in store.series.values()],
                             [65., 66., 67., 68.])
            self.assertTrue(all(key[:3] == ("deribit", "dvol", "BTC")
                                for key in store.series))
            self.assertIsNone(store.sources[source_key]["error"])
            self.assertEqual(store.sources[source_key]["row_count"], 4)

            unchanged = sync.run()
            self.assertEqual(unchanged["rows"], 0)
            self.assertEqual(unchanged["skipped"], 1)
            self.assertEqual(attempts, 4)
            self.assertEqual(len(store.series), 4)
            self.assertEqual(store.sources[source_key]["row_count"], 4)
        self.assertEqual((path.stat().st_mtime_ns, path.stat().st_size), stamp)

    def test_sqlite_snapshot_write_failure_retries_unchanged_database(self):
        path = self.project / "bot/tradesv3.dryrun.sqlite"
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, pair TEXT)")
            connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, trade_id INTEGER)")
            connection.execute("INSERT INTO trades VALUES (1,'BTC/USDT')")
            connection.execute("INSERT INTO orders VALUES (1,1)")
            connection.commit()
        finally:
            connection.close()
        original_bytes = path.read_bytes()
        stamp = (path.stat().st_mtime_ns, path.stat().st_size)
        source_key = "bot/tradesv3.dryrun.sqlite"
        store = MemoryStore()
        sync = DataSynchronizer(store, self.project)
        original_replace = store.replace_external_snapshot
        attempts = 0

        def fail_first_snapshot(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("disk full")
            return original_replace(*args, **kwargs)

        with patch.object(store, "replace_external_snapshot", side_effect=fail_first_snapshot), \
             redirect_stdout(StringIO()):
            failed = sync.run(fast=True)
            self.assertEqual(failed["status"], "error")
            self.assertEqual(failed["errors"], 1)
            self.assertEqual(failed["storage_errors"], 0)
            self.assertEqual(failed["rows"], 0)
            self.assertEqual(failed["files"], 0)
            self.assertNotIn("last_success_at", failed)
            self.assertEqual(store.external, {})
            self.assertEqual(store.sources[source_key]["error"], "OSError")

            recovered = sync.run(fast=True)
            self.assertEqual(recovered["status"], "completed")
            self.assertEqual(recovered["errors"], 0)
            self.assertEqual(recovered["rows"], 2)
            self.assertEqual(recovered["files"], 1)
            self.assertEqual(store.external, {
                (source_key, "trades"): [{"id": 1, "pair": "BTC/USDT"}],
                (source_key, "orders"): [{"id": 1, "trade_id": 1}],
            })
            self.assertIsNone(store.sources[source_key]["error"])
            self.assertEqual(store.sources[source_key]["row_count"], 2)
            self.assertEqual(attempts, 2)

            unchanged = sync.run(fast=True)
            self.assertEqual(unchanged["rows"], 0)
            self.assertEqual(unchanged["skipped"], 1)
            self.assertEqual(attempts, 2)
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual((path.stat().st_mtime_ns, path.stat().st_size), stamp)

    def test_once_cli_returns_nonzero_for_sync_state_write_failure(self):
        write_json(self.project, "research_summary.json", {"round": 1})
        store = MemoryStore()
        output = StringIO()
        with patch("data_sync.exclusive_sync", return_value=nullcontext()), \
             patch("data_store.DataStore", return_value=store), \
             patch.object(store, "initialize", create=True) as initialize, \
             patch.object(store, "set_sync_state", side_effect=OSError("disk full")), \
             patch("data_sync.signal.signal"), \
             redirect_stdout(output):
            result = main(["--once", "--fast", "--root", str(self.project)])

        self.assertEqual(result, 1)
        initialize.assert_called_once()
        summary = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(summary["status"], "error")
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["storage_errors"], 2)
        self.assertFalse(summary["running"])
        self.assertNotIn("last_success_at", summary)
        self.assertEqual(store.documents["research_summary.json"], {"round": 1})

    def test_sqlite_consistent_readonly_wal_and_users_preserved(self):
        project = self.project
        path = project / "bot/tradesv3.dryrun.sqlite"
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, pair TEXT)")
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, trade_id INTEGER)")
        connection.execute("INSERT INTO trades VALUES (1,'BTC/USDT')")
        connection.execute("INSERT INTO orders VALUES (1,1)")
        connection.commit()
        user_path = project / "auth/users.json"
        user_path.parent.mkdir()
        payload = {"users": {"example": {"salt": "original-salt", "hash": "original-hash"}}}
        user_path.write_text(json.dumps(payload), encoding="utf-8")
        snapshot = sqlite_snapshot(path)
        assert snapshot["trades"][0]["id"] == snapshot["orders"][0]["trade_id"]
        store = MemoryStore()
        sync = DataSynchronizer(store, project)
        assert sync.run(fast=True)["rows"] == 3
        assert store.users == payload
        assert connection.total_changes == 2
        connection.execute("INSERT INTO trades VALUES (2,'ETH/USDT')")
        connection.commit()
        second = sync.run(fast=True)
        assert second["files"] == 1 and second["rows"] == 3
        assert connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2
        connection.close()

    def test_process_lock_and_outside_symlink_rejected(self):
        project = self.project
        tmp_path = self.project
        with exclusive_sync(project):
            with self.assertRaises(RuntimeError):
                with exclusive_sync(project):
                    pass
        outside = tmp_path / "outside.json"
        outside.write_text('{"secret": "unsafe"}')
        (project / "bot/user_data/leaked.json").symlink_to(outside)
        store = MemoryStore()
        assert DataSynchronizer(store, project).run(fast=True)["files"] == 0

    def test_prediction_records_and_zip_member_safety(self):
        project = self.project
        models = project / "bot/user_data/models/demo"
        models.mkdir(parents=True)
        frame = pd.DataFrame({"date": pd.to_datetime(["2026-01-01"]), "prediction": [np.nan], "do_predict": [1]})
        frame.to_feather(models / "prediction.feather")
        results = project / "bot/user_data/backtest_results"
        results.mkdir()
        with zipfile.ZipFile(results / "result.zip", "w") as archive:
            archive.writestr("result.json", '{"strategy": "example", "token": "PRIVATE"}')
            archive.writestr("../escaped.json", '{"escaped": true}')
            archive.writestr("/absolute.json", '{"escaped": true}')
            archive.writestr("config.json", '{"key": "PRIVATE"}')
        store = MemoryStore()
        result = DataSynchronizer(store, project).run()
        assert result["rows"] == 2 and result["errors"] == 0
        events = store.events["models/demo/prediction.feather"]
        assert events == [{"date": "2026-01-01T00:00:00.000", "prediction": None, "do_predict": 1}]
        assert store.documents == {"backtest_results/result.zip/result.json": {"strategy": "example"}}
        assert len(store.artifacts) == 2

    def test_csv_backtest_records(self):
        path = self.project / "bot/user_data/cross_sectional.csv"
        path.write_text("time,net,longs\n2026-01-01,0.25,BTC\n2026-01-02,,ETH\n", encoding="utf-8")
        store = MemoryStore()
        result = DataSynchronizer(store, self.project).run()
        assert result["errors"] == 0 and result["rows"] == 2
        assert store.events["cross_sectional.csv"] == [
            {"time": "2026-01-01", "net": .25, "longs": "BTC"},
            {"time": "2026-01-02", "net": None, "longs": "ETH"},
        ]

    @unittest.skipUnless(os.environ.get("QUANT_TEST_DATABASE_URL"), "未配置隔离 PostgreSQL 测试库")
    def test_postgres_sync_roundtrip_and_row_counts(self):
        project = self.project
        from data_store import DataStore
        from psycopg import connect, sql

        schema = "test_sync_" + uuid.uuid4().hex
        dsn = os.environ["QUANT_TEST_DATABASE_URL"]
        store = DataStore(root=project, dsn=dsn, schema=schema)
        try:
            store.initialize()
            write_json(project, "models/demo/backtest_detail.json", {"value": 42})
            directory = project / "bot/user_data/data/binance/futures"
            directory.mkdir(parents=True)
            candle_frame().to_feather(directory / "BTC_USDT_USDT-1d-futures.feather")
            pd.DataFrame({"date": pd.to_datetime(["2026-01-01"]), "funding_rate": [-.0003]}).to_feather(
                directory / "BTC_USDT_USDT-1h-funding_rate.feather")
            pd.DataFrame({"date": pd.to_datetime(["2026-01-01"]), "prediction": [.7]}).to_feather(
                project / "bot/user_data/models/demo/prediction.feather")
            (project / "bot/user_data/returns.csv").write_text("time,net\n2026-01-01,0.1\n")
            results = project / "bot/user_data/backtest_results"
            results.mkdir()
            with zipfile.ZipFile(results / "result.zip", "w") as archive:
                archive.writestr("result.json", '{"trades": 2}')
            users = project / "auth/users.json"
            users.parent.mkdir()
            original_users = {"users": {"example": {"salt": "original-salt", "hash": "original-hash"}}}
            users.write_text(json.dumps(original_users))
            with sqlite3.connect(project / "bot/tradesv3.dryrun.sqlite") as connection:
                connection.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, pair TEXT)")
                connection.execute("INSERT INTO trades VALUES (1,'BTC/USDT')")
            sync = DataSynchronizer(store, project, batch_size=1)
            result = sync.run()
            assert result["errors"] == 0 and result["rows"] == 9
            assert store.get_document("models/demo/backtest_detail.json") == {"value": 42}
            assert store.get_document("backtest_results/result.zip/result.json") == {"trades": 2}
            assert len(store.get_candles("binance", "futures", "BTC/USDT:USDT", "1d", "futures")) == 2
            assert store.read_events("models/demo/prediction.feather")[0]["prediction"] == .7
            assert store.load_users() == original_users
            assert store.read_external("bot/tradesv3.dryrun.sqlite", "trades") == [{"id": 1, "pair": "BTC/USDT"}]
            assert sync.run()["rows"] == 0
            # 原用户迁移不重复，强制重跑只写其余八条结构化记录。
            assert sync.run(force=True)["rows"] == 8
            with store._connection() as connection:
                assert connection.execute("SELECT COUNT(*) AS n FROM candles").fetchone()["n"] == 2
                assert connection.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 2
                rate = connection.execute("SELECT value FROM series_points WHERE metric='funding_rate'").fetchone()
                assert rate["value"] == -.0003
        finally:
            store.close()
            with connect(dsn, autocommit=True) as connection:
                connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    @unittest.skipUnless(os.environ.get("QUANT_TEST_DATABASE_URL"), "未配置隔离 PostgreSQL 测试库")
    def test_prune_temporary_source_rows_keeps_regular_sources_and_artifacts(self):
        from data_store import DataStore
        from psycopg import connect, sql

        schema = "test_sync_prune_" + uuid.uuid4().hex
        store = DataStore(root=self.project, dsn=os.environ["QUANT_TEST_DATABASE_URL"], schema=schema)
        try:
            store.initialize()
            temporary_key = "bot/user_data/cache/panel_seq.npy.tmp.npy"
            regular_key = "bot/user_data/cache/panel_seq.npy"
            store.record_source(temporary_key, temporary_key, "artifact", 1, 1, 0,
                                error="ValueError")
            store.record_source(regular_key, regular_key, "artifact", 2, 2, 1)
            store.save_artifact(temporary_key, temporary_key, 1, 1, "cache")

            self.assertEqual(store.prune_temporary_sources(), 1)
            self.assertIsNone(store.source_state(temporary_key))
            self.assertIsNone(store.source_state(regular_key)["error"])
            self.assertEqual(store.list_artifacts(temporary_key)[0]["key"], temporary_key)
        finally:
            store.close()
            with connect(os.environ["QUANT_TEST_DATABASE_URL"], autocommit=True) as connection:
                connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    unittest.main()

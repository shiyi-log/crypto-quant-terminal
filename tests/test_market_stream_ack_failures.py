"""确认已提交行情批次时的暂存盘故障不会被误报成数据库失败。"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from market_stream import MarketCollector, StreamConfig, normalize_message
from tests.test_market_stream import MemoryStore, trade


class SpoolAcknowledgementFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = MemoryStore()
        self.collector = MarketCollector(StreamConfig(spool_dir=Path(temporary.name) / "spool"), self.store)
        self.event = normalize_message(trade(), self.collector.config, 100)
        self.path = self.collector.spool.persist([self.event])

    def assert_storage_failure(self):
        self.assertEqual(self.collector.status["state"], "storage_error")
        self.assertTrue(self.collector.stop.is_set())
        self.assertEqual(self.collector.status["database_failures"], 0)
        self.assertEqual(self.collector.status["inflight"], 0)
        self.assertEqual(len(self.store.ticks), 1)
        self.assertNotIn("批次保留", self.collector.status["last_error"])

    async def test_unlink_failure_retains_batch_after_database_commit(self):
        with patch.object(Path, "unlink", side_effect=OSError("暂存盘不可用")):
            with self.assertRaises(OSError):
                await self.collector.flush_spool()
        self.assert_storage_failure()
        self.assertTrue(self.path.exists())

    async def test_directory_sync_failure_reports_storage_error_after_unlink(self):
        with patch.object(self.collector.spool, "_sync_directory", side_effect=OSError("目录同步失败")):
            with self.assertRaises(OSError):
                await self.collector.flush_spool()
        self.assert_storage_failure()
        self.assertFalse(self.path.exists())

    async def test_spool_read_failure_keeps_valid_batch_for_recovery(self):
        original = self.path.read_bytes()
        for error in (OSError("暂存盘读取失败"), PermissionError("暂存盘读取权限不足")):
            with self.subTest(error=type(error).__name__):
                self.collector.stop.clear()
                self.collector.status["state"] = "starting"
                with patch.object(self.collector.spool, "load", side_effect=error):
                    with self.assertRaises(type(error)):
                        await self.collector.flush_spool()
                self.assertEqual(self.collector.status["state"], "storage_error")
                self.assertTrue(self.collector.stop.is_set())
                self.assertEqual(self.collector.status["database_failures"], 0)
                self.assertEqual(self.collector.status["quarantined_batches"], 0)
                self.assertEqual(self.collector.spool.corrupt_files(), [])
                self.assertEqual(self.collector.spool.pending(), [self.path])
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(len(self.store.ticks), 0)
        self.assertTrue(await self.collector.flush_spool())
        self.assertEqual(len(self.store.ticks), 1)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()

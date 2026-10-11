"""本地状态侧车故障：不连接数据库或交易所。"""

import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from market_stream import MarketCollector, StreamConfig


class StatusStore:
    def __init__(self):
        self.documents = {}
        self.failure = None

    def put_document(self, key, payload):
        if self.failure is not None:
            raise self.failure
        self.documents[key] = payload


class LocalSession:
    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class LifecycleWorker:
    def __init__(self):
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def close(self):
        self.closed = True
        return True

    @property
    def status(self):
        return {"running": self.started and not self.closed, "closed": self.closed}


class StatusFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = StatusStore()
        self.config = StreamConfig(spool_dir=Path(self.temporary.name) / "spool")
        self.collector = MarketCollector(self.config, self.store)
        self.sidecar = self.config.spool_dir.parent / "market_stream_status.json"

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_sidecar_write_failure_keeps_previous_status_and_stops(self):
        await self.collector.publish_status()
        previous = self.sidecar.read_bytes()
        previous_document = self.store.documents["market_stream_status.json"]
        self.collector.status["received"] = 10

        def reject_write(path, *args, **kwargs):
            raise OSError("状态盘已满")

        with patch("market_stream.Path.write_text", new=reject_write):
            with self.assertRaisesRegex(OSError, "状态盘已满"):
                await self.collector.status_loop()
        self.assertEqual(self.sidecar.read_bytes(), previous)
        self.assertEqual(self.store.documents["market_stream_status.json"], previous_document)
        self.assertTrue(self.collector.stop.is_set())
        self.assertEqual(self.collector.status["state"], "storage_error")
        self.assertIn("本地状态写入失败", self.collector.status["last_error"])

    async def test_sidecar_replace_failure_retains_valid_previous_file_and_retryable_tmp(self):
        await self.collector.publish_status()
        previous = self.sidecar.read_bytes()
        self.collector.status["received"] = 20
        with patch("market_stream.os.replace", side_effect=OSError("状态重命名失败")):
            with self.assertRaisesRegex(OSError, "状态重命名失败"):
                await self.collector.status_loop()
        self.assertEqual(self.sidecar.read_bytes(), previous)
        temporary = self.sidecar.with_suffix(".tmp")
        self.assertEqual(json.loads(temporary.read_text())["received"], 20)
        self.assertEqual(self.store.documents["market_stream_status.json"]["received"], 0)
        self.assertTrue(self.collector.stop.is_set())
        self.assertEqual(self.collector.status["state"], "storage_error")
        await self.collector.publish_status()
        self.assertFalse(temporary.exists())
        self.assertEqual(json.loads(self.sidecar.read_text())["received"], 20)

    async def test_database_status_failure_keeps_local_status_available(self):
        self.store.failure = ConnectionError("数据库不可用")
        await self.collector.publish_status()
        self.assertEqual(json.loads(self.sidecar.read_text())["state"], "starting")
        self.assertFalse(self.collector.stop.is_set())
        self.assertIsNone(self.collector.status["database_error"])

    async def test_final_status_failure_sets_storage_error_after_workers_stop(self):
        await self.collector.publish_status()
        previous = self.sidecar.read_bytes()

        async def wait_for_stop(*args):
            await self.collector.stop.wait()

        aiohttp = SimpleNamespace(ClientSession=LocalSession, ClientTimeout=lambda **kwargs: None)
        loop = asyncio.get_running_loop()
        with patch.dict("sys.modules", {"aiohttp": aiohttp}), \
             patch.object(loop, "add_signal_handler"), \
             patch.object(self.collector, "writer", new=AsyncMock()), \
             patch.object(self.collector, "status_loop", new=wait_for_stop), \
             patch.object(self.collector, "connection", new=wait_for_stop), \
             patch("market_stream.Path.write_text", side_effect=OSError("最终状态盘已满")):
            await asyncio.wait_for(self.collector._run(), timeout=1)
        self.assertEqual(self.sidecar.read_bytes(), previous)
        self.assertTrue(self.collector.stop.is_set())
        self.assertEqual(self.collector.status["state"], "storage_error")
        self.assertIn("最终状态写入失败", self.collector.status["last_error"])

    async def test_maturity_worker_starts_and_closes_before_final_status(self):
        worker = LifecycleWorker()
        self.collector.maturity_worker = worker
        aiohttp = SimpleNamespace(ClientSession=LocalSession, ClientTimeout=lambda **kwargs: None)
        loop = asyncio.get_running_loop()
        with patch.dict("sys.modules", {"aiohttp": aiohttp}), \
             patch.object(loop, "add_signal_handler"), \
             patch.object(self.collector, "seed_surge_detector", new=AsyncMock()), \
             patch.object(self.collector, "writer", new=AsyncMock()), \
             patch.object(self.collector, "status_loop", new=self._wait_for_stop), \
             patch.object(self.collector, "connection", new=self._wait_for_stop):
            await asyncio.wait_for(self.collector._run(), timeout=1)
        self.assertTrue(worker.started)
        self.assertTrue(worker.closed)
        snapshot = json.loads(self.sidecar.read_text())
        self.assertEqual(snapshot["orderflow_maturity"], {"enabled": True, "running": False, "closed": True})

    async def test_stop_interrupts_detector_seed_before_connections_start(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_seed():
            entered.set()
            await release.wait()

        aiohttp = SimpleNamespace(ClientSession=LocalSession, ClientTimeout=lambda **kwargs: None)
        loop = asyncio.get_running_loop()
        with patch.dict("sys.modules", {"aiohttp": aiohttp}), \
             patch.object(loop, "add_signal_handler"), \
             patch.object(self.collector, "seed_surge_detector", new=blocked_seed), \
             patch.object(self.collector, "writer", new=AsyncMock()), \
             patch.object(self.collector, "publish_status", new=AsyncMock()):
            task = asyncio.create_task(self.collector._run())
            await asyncio.wait_for(entered.wait(), timeout=1)
            self.collector.stop.set()
            await asyncio.wait_for(task, timeout=1)
        self.assertTrue(self.collector.stop.is_set())

    async def test_shutdown_timeout_bounds_maturity_worker_close(self):
        class SlowWorker:
            status = {"running": False, "closed": False}

            def start(self):
                pass

            def close(self):
                time.sleep(0.2)

        self.collector.config = self.config = StreamConfig(
            spool_dir=self.config.spool_dir, shutdown_timeout=0.03,
        )
        self.collector.maturity_worker = SlowWorker()
        aiohttp = SimpleNamespace(ClientSession=LocalSession, ClientTimeout=lambda **kwargs: None)
        loop = asyncio.get_running_loop()
        with patch.dict("sys.modules", {"aiohttp": aiohttp}), \
             patch.object(loop, "add_signal_handler"), \
             patch.object(self.collector, "seed_surge_detector", new=AsyncMock()), \
             patch.object(self.collector, "writer", new=AsyncMock()), \
             patch.object(self.collector, "status_loop", new=self._wait_for_stop), \
             patch.object(self.collector, "connection", new=self._wait_for_stop), \
             patch.object(self.collector, "publish_status", new=AsyncMock()):
            self.collector.stop.set()
            started = time.monotonic()
            await asyncio.wait_for(self.collector._run(), timeout=0.15)
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertEqual(self.collector.status["state"], "storage_error")
        self.assertIn("关闭超时", self.collector.status["last_error"])

    async def _wait_for_stop(self, *args):
        await self.collector.stop.wait()


if __name__ == "__main__":
    unittest.main()

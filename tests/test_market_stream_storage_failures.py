"""真实文件配合低层故障注入，验证暂存文件失败后的可读恢复语义。"""

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from market_stream import FileSpool


def disk_error():
    return OSError(errno.ENOSPC, "测试磁盘已满")


class FaultyHandle:
    """仍向真实文件写入；失败后由真实句柄关闭并刷新已写入的内容。"""

    def __init__(self, handle, stage):
        self.handle = handle
        self.stage = stage
        self.remaining = 32

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def write(self, text):
        if self.stage != "write":
            return self.handle.write(text)
        prefix = text[:self.remaining]
        if prefix:
            self.handle.write(prefix)
            self.remaining -= len(prefix)
        if len(prefix) < len(text):
            raise disk_error()
        return len(prefix)

    def flush(self):
        if self.stage == "flush":
            raise disk_error()
        return self.handle.flush()


@contextmanager
def faulty_temporary_file(stage):
    original_open = Path.open

    def open_file(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path.suffix == ".tmp" and mode == "w":
            return FaultyHandle(handle, stage)
        return handle

    with patch("market_stream.Path.open", open_file):
        yield


@contextmanager
def directory_fsync_failure():
    original_fsync = os.fsync
    failed_descriptors = []

    def fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            failed_descriptors.append(descriptor)
            raise disk_error()
        return original_fsync(descriptor)

    with patch("market_stream.os.fsync", fsync):
        yield failed_descriptors


class FileSpoolStorageFailureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.events = [{
            "kind": "tick", "market": "futures", "pair": "BTC/USDT:USDT",
            "row": {"trade_id": str(trade_id), "timestamp": 1700000000000,
                    "price": "61234.12345678", "quantity": "0.00100000",
                    "buyer_maker": True},
        } for trade_id in (41, 42)]

    def restarted_batch(self, spool, suffix):
        restarted = FileSpool(spool.directory)
        pending = restarted.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].suffix, suffix)
        return restarted, pending[0]

    def assert_descriptors_closed(self, descriptors):
        self.assertTrue(descriptors)
        for descriptor in descriptors:
            with self.assertRaises(OSError) as failure:
                os.fstat(descriptor)
            self.assertEqual(failure.exception.errno, errno.EBADF)

    def test_open_failure_creates_no_batch_and_retry_is_recoverable(self):
        spool = FileSpool(self.directory / "spool")
        with patch("market_stream.Path.open", side_effect=disk_error()):
            with self.assertRaises(OSError):
                spool.persist(self.events)
        self.assertEqual(FileSpool(spool.directory).pending(), [])
        self.assertEqual(list(spool.directory.iterdir()), [])

        spool.persist(self.events)
        restarted, path = self.restarted_batch(spool, ".json")
        self.assertEqual(restarted.load(path), self.events)

    def test_interrupted_write_or_dump_keeps_partial_tmp_for_quarantine(self):
        def interrupted_dump(payload, handle, **kwargs):
            handle.write('{"version":1,"events":[')
            raise disk_error()

        for stage in ("write", "dump"):
            with self.subTest(stage=stage):
                spool = FileSpool(self.directory / stage)
                fault = (faulty_temporary_file("write") if stage == "write" else
                         patch("market_stream.json.dump", interrupted_dump))
                with fault:
                    with self.assertRaises(OSError):
                        spool.persist(self.events)
                restarted, path = self.restarted_batch(spool, ".tmp")
                original = path.read_bytes()
                self.assertTrue(original)
                with self.assertRaises(ValueError):
                    restarted.load(path)

                quarantined = restarted.quarantine(path)
                self.assertEqual(quarantined.read_bytes(), original)
                self.assertEqual(restarted.pending(), [])
                self.assertEqual(restarted.corrupt_files(), [quarantined])
                restarted.persist(self.events)
                recovered, valid = self.restarted_batch(restarted, ".json")
                self.assertEqual(recovered.load(valid), self.events)

    def test_flush_file_fsync_and_replace_failures_keep_complete_replayable_tmp(self):
        for stage in ("flush", "file_fsync", "replace"):
            with self.subTest(stage=stage):
                spool = FileSpool(self.directory / stage)
                if stage == "flush":
                    fault = faulty_temporary_file("flush")
                else:
                    operation = "os.fsync" if stage == "file_fsync" else "os.replace"
                    fault = patch(f"market_stream.{operation}", side_effect=disk_error())
                with fault:
                    with self.assertRaises(OSError):
                        spool.persist(self.events)
                restarted, path = self.restarted_batch(spool, ".tmp")
                self.assertEqual(restarted.load(path), self.events)
                restarted.acknowledge(path)
                self.assertEqual(FileSpool(spool.directory).pending(), [])

    def test_directory_sync_failure_after_rename_keeps_complete_json(self):
        for stage in ("directory_open", "directory_fsync"):
            with self.subTest(stage=stage):
                spool = FileSpool(self.directory / stage)
                fault = (patch("market_stream.os.open", side_effect=disk_error())
                         if stage == "directory_open" else directory_fsync_failure())
                with fault as descriptors:
                    with self.assertRaises(OSError):
                        spool.persist(self.events)
                if stage == "directory_fsync":
                    self.assert_descriptors_closed(descriptors)
                restarted, path = self.restarted_batch(spool, ".json")
                self.assertEqual(restarted.load(path), self.events)
                restarted.acknowledge(path)
                self.assertEqual(restarted.pending(), [])

    def test_acknowledge_unlink_failure_keeps_batch_for_retry(self):
        spool = FileSpool(self.directory / "spool")
        path = spool.persist(self.events)
        original = path.read_bytes()
        with patch("market_stream.Path.unlink", side_effect=disk_error()):
            with self.assertRaises(OSError):
                spool.acknowledge(path)
        restarted, pending = self.restarted_batch(spool, ".json")
        self.assertEqual(pending.read_bytes(), original)
        self.assertEqual(restarted.load(pending), self.events)
        restarted.acknowledge(pending)
        self.assertEqual(restarted.pending(), [])

    def test_acknowledge_directory_fsync_failure_occurs_after_file_is_deleted(self):
        spool = FileSpool(self.directory / "spool")
        path = spool.persist(self.events)
        with directory_fsync_failure() as descriptors:
            with self.assertRaises(OSError):
                spool.acknowledge(path)
        self.assert_descriptors_closed(descriptors)
        self.assertFalse(path.exists())
        self.assertEqual(FileSpool(spool.directory).pending(), [])

    def test_quarantine_replace_failure_keeps_original_bytes_for_retry(self):
        spool = FileSpool(self.directory / "spool")
        path = spool.directory / "interrupted.tmp"
        original = b'{"version":1,"events":['
        path.write_bytes(original)
        with patch("market_stream.os.replace", side_effect=disk_error()):
            with self.assertRaises(OSError):
                spool.quarantine(path)
        restarted, pending = self.restarted_batch(spool, ".tmp")
        self.assertEqual(pending.read_bytes(), original)
        self.assertEqual(restarted.corrupt_files(), [])
        isolated = restarted.quarantine(pending)
        self.assertEqual(isolated.read_bytes(), original)
        self.assertEqual(restarted.pending(), [])

    def test_quarantine_directory_fsync_failure_still_preserves_isolated_bytes(self):
        spool = FileSpool(self.directory / "spool")
        path = spool.directory / "interrupted.tmp"
        original = b'{"version":1,"events":['
        path.write_bytes(original)
        with directory_fsync_failure() as descriptors:
            with self.assertRaises(OSError):
                spool.quarantine(path)
        self.assert_descriptors_closed(descriptors)
        self.assertFalse(path.exists())
        restarted = FileSpool(spool.directory)
        self.assertEqual(restarted.pending(), [])
        isolated = restarted.corrupt_files()
        self.assertEqual(len(isolated), 1)
        self.assertEqual(isolated[0].read_bytes(), original)
        restarted.persist(self.events)
        recovered, valid = self.restarted_batch(restarted, ".json")
        self.assertEqual(recovered.load(valid), self.events)
        self.assertEqual(recovered.corrupt_files(), isolated)


if __name__ == "__main__":
    unittest.main()

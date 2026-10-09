"""实时 K 线的有界内存归档队列，行情发布路径无需等待 PostgreSQL。"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


def _timestamp(value):
    timestamp = int(value)
    if isinstance(value, bool) or timestamp < 0 or float(value) != timestamp:
        raise ValueError("timestamp 必须是非负整数毫秒")
    return timestamp


def _version(row):
    extras = row.get("extras") or {}
    timestamps = [
        _timestamp(extras[name]) for name in ("live_event_timestamp", "live_snapshot_timestamp")
        if extras.get(name) is not None
    ]
    return (max(timestamps, default=-1), extras.get("closed") is True)


@dataclass(frozen=True)
class _Candle:
    row: dict
    source_key: str | None
    revision: int


@dataclass
class _Dataset:
    rows: dict[int, _Candle] = field(default_factory=dict)
    next_attempt: float = 0.0
    failures: int = 0


class CandleArchiveWriter:
    """单个长期 daemon 线程批量归档，首个非空 offer 才启动。

    队列是有限内存：每个数据集最多 2,000 根，失败时保留并退避重试。
    进程骤停仍可能丢失未提交的 K 线更新；它不是 durable tick/orderbook
    采集器的替代品。调用方应检查 offer 的 False 与 status，停机调用 close。
    store_factory 在归档线程里按需调用，不占用行情请求或推送线程。
    """

    MAX_ROWS_PER_DATASET = 2000

    def __init__(self, store_factory: Callable, flush_interval=1.0, max_datasets=128):
        if float(flush_interval) <= 0 or int(max_datasets) < 1:
            raise ValueError("flush_interval 和 max_datasets 必须大于 0")
        self._store_factory = store_factory
        self._flush_interval = float(flush_interval)
        self._max_datasets = int(max_datasets)
        self._condition = threading.Condition()
        self._pending: dict[tuple, _Dataset] = {}
        self._thread: threading.Thread | None = None
        self._store = None
        self._revision = 0
        self._closed = False
        self._close_deadline: float | None = None
        self._failures = 0
        self._last_error: str | None = None
        self._rejected_offers = 0

    def offer(self, exchange, market, pair, timeframe, candle_type, rows, source_key=None):
        """仅在内存合并，容量不足/已关闭返回 False，绝不等待数据库。

        同根以 WS extras.live_event_timestamp / REST live_snapshot_timestamp
        排序；同时间 closed 优先，再取最新 offer。无版本 REST 不覆盖 WS。
        数据库也会校验版本，防止已提交的
        WS 在后续 REST 归档中被覆盖。满队列拒绝整个批次，不丢弃已有根。
        """
        identity = (exchange, market, pair, timeframe, candle_type)
        incoming = {}
        for value in rows:
            row = copy.deepcopy(value)
            timestamp = _timestamp(row["timestamp"])
            row["timestamp"] = timestamp
            version = _version(row)
            if timestamp not in incoming or version >= _version(incoming[timestamp]):
                incoming[timestamp] = row
            if len(incoming) > self.MAX_ROWS_PER_DATASET:
                with self._condition:
                    self._rejected_offers += 1
                return False

        with self._condition:
            if self._closed:
                return False
            if not incoming:
                return True
            dataset = self._pending.get(identity)
            if dataset is None:
                if len(self._pending) >= self._max_datasets:
                    self._rejected_offers += 1
                    return False
                dataset = _Dataset(next_attempt=time.monotonic() + self._flush_interval)
            if len(dataset.rows.keys() | incoming.keys()) > self.MAX_ROWS_PER_DATASET:
                self._rejected_offers += 1
                return False
            for timestamp, row in incoming.items():
                previous = dataset.rows.get(timestamp)
                if previous is not None and _version(row) < _version(previous.row):
                    continue
                self._revision += 1
                dataset.rows[timestamp] = _Candle(row, source_key, self._revision)
            if not dataset.rows:
                return True
            self._pending[identity] = dataset
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="candle-archive", daemon=True,
                )
                self._thread.start()
            self._condition.notify_all()
        return True

    @property
    def status(self):
        with self._condition:
            return {
                "pending_datasets": len(self._pending),
                "pending_rows": sum(len(dataset.rows) for dataset in self._pending.values()),
                "failures": self._failures,
                # 异常消息可能携带连接凭据，只公开异常类别。
                "last_error": self._last_error,
                "rejected_offers": self._rejected_offers,
                "running": self._thread is not None and self._thread.is_alive(),
                "closed": self._closed,
            }

    def close(self, timeout=5.0):
        """停止接收并立即尝试清空，最多等待 timeout 秒；清空成功返回 True。

        数据库调用本身无法从 Python 线程取消，超时后 daemon 可仍在该调用
        中；它返回后会在截止时间退出。未提交数据仍保留在 status 中。
        """
        timeout = max(0.0, float(timeout))
        with self._condition:
            if not self._closed:
                self._closed = True
                now = time.monotonic()
                self._close_deadline = now + timeout
                for dataset in self._pending.values():
                    dataset.next_attempt = min(dataset.next_attempt, now)
                self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        with self._condition:
            return not self._pending and (thread is None or not thread.is_alive())

    def _run(self):
        while True:
            with self._condition:
                now = time.monotonic()
                if self._closed and (not self._pending or now >= self._close_deadline):
                    return
                if not self._pending:
                    self._condition.wait()
                    continue
                identity, dataset = min(
                    self._pending.items(), key=lambda item: item[1].next_attempt,
                )
                wait = dataset.next_attempt - now
                if wait > 0:
                    if self._closed:
                        wait = min(wait, self._close_deadline - now)
                    self._condition.wait(wait)
                    continue
                snapshot = dict(dataset.rows)

            try:
                if self._store is None:
                    self._store = self._store_factory()
                # 不同 source_key 分批提交，避免把一个来源标记应用到整段历史。
                groups = {}
                for timestamp in sorted(snapshot):
                    candle = snapshot[timestamp]
                    groups.setdefault(candle.source_key, []).append(candle.row)
                for source_key, batch in groups.items():
                    self._store.upsert_candles(*identity, batch, source_key=source_key)
            except Exception as error:
                with self._condition:
                    self._failures += 1
                    self._last_error = type(error).__name__
                    dataset.failures += 1
                    dataset.next_attempt = time.monotonic() + min(
                        5.0, 0.1 * 2 ** min(dataset.failures - 1, 6),
                    )
                continue

            with self._condition:
                for timestamp, candle in snapshot.items():
                    current = dataset.rows.get(timestamp)
                    if current is not None and current.revision == candle.revision:
                        del dataset.rows[timestamp]
                if dataset.rows:
                    dataset.failures = 0
                    dataset.next_attempt = time.monotonic() + (
                        0.0 if self._closed else self._flush_interval
                    )
                else:
                    del self._pending[identity]

"""币安公开成交与前 20 档盘口采集，不访问账户、不下单、不启动模型。"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import signal
import threading
import time
from typing import Any
import uuid

LOG = logging.getLogger("market_stream")
ROOT = Path(__file__).resolve().parent


class CollectorAlreadyRunning(RuntimeError):
    """同一运行目录已有行情采集器。"""


class CollectorInstanceLock:
    """用操作系统锁保护暂存和状态文件，进程异常退出后锁自动释放。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self) -> "CollectorInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise CollectorAlreadyRunning(f"同一运行目录已有采集器，第二实例退出：{self.path}") from exc
        except BaseException:
            handle.close()
            raise
        self.handle = handle
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None
        # 不删除锁文件，避免新旧进程对不同 inode 加锁而误以为各自独占。


def normalize_pair(value: str, market: str = "futures") -> str:
    """统一采用历史数据的 CCXT 交易对格式，并校验订阅符号。"""
    value = value.strip().upper()
    if ":" in value:
        raw, settlement = value.split(":", 1)
        if market != "futures" or settlement != "USDT" or not raw.endswith("/USDT"):
            raise ValueError("当前合约采集仅支持 USDT 结算，现货不能含结算后缀")
        value = raw
    if "/" not in value:
        for quote in ("FDUSD", "USDT", "USDC", "TUSD", "BUSD", "BTC", "ETH", "BNB", "EUR", "TRY"):
            if value.endswith(quote) and len(value) > len(quote):
                value = f"{value[:-len(quote)]}/{quote}"
                break
    if not re.fullmatch(r"[A-Z0-9]{1,24}/[A-Z0-9]{1,12}", value):
        raise ValueError(f"无效交易对：{value}")
    if market == "futures":
        if not value.endswith("/USDT"):
            raise ValueError("当前合约采集仅支持 USDⓈ-M 的 USDT 合约")
        return f"{value}:USDT"
    return value


def symbol_for(pair: str) -> str:
    return pair.split(":", 1)[0].replace("/", "").lower()


@dataclass(frozen=True)
class StreamConfig:
    pairs: tuple[str, ...] = ("BTC/USDT:USDT", "ETH/USDT:USDT")
    market: str = "futures"
    queue_size: int = 20_000
    batch_size: int = 1_000
    flush_interval: float = 1.0
    retry_max: float = 30.0
    shutdown_timeout: float = 10.0
    status_interval: float = 2.0
    spool_dir: Path = ROOT / "database/runtime/market_spool"
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.market not in {"futures", "spot"}:
            raise ValueError("market 必须为 futures 或 spot")
        if not self.pairs or self.queue_size < 1 or self.batch_size < 1:
            raise ValueError("交易对、队列容量与批次容量必须有效")
        if min(self.flush_interval, self.retry_max, self.shutdown_timeout, self.status_interval) <= 0:
            raise ValueError("等待时间必须大于 0")

    def connections(self) -> list[tuple[str, str]]:
        # 当前币安合约文档将成交和盘口分置于 market/public 分区。
        symbols = [symbol_for(pair) for pair in self.pairs]
        trades = "/".join(f"{s}@{'aggTrade' if self.market == 'futures' else 'trade'}" for s in symbols)
        books = "/".join(f"{s}@depth20@100ms" for s in symbols)
        if self.market == "futures":
            return [
                ("trades", f"wss://fstream.binance.com/market/stream?streams={trades}"),
                ("orderbooks", f"wss://fstream.binance.com/public/stream?streams={books}"),
            ]
        return [("market", f"wss://stream.binance.com:9443/stream?streams={trades}/{books}")]


def _decimal(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("行情中存在非法价格或数量") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("行情价格和数量必须是非负有限数")
    # 保留交易所原字符串，避免在采集过程提前损失精度。
    return str(value)


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("行情时间戳与 ID 必须为非负整数")
    number = int(value)
    if number < 0 or str(number) != str(value):
        raise ValueError("行情时间戳与 ID 必须为非负整数")
    return number


def normalize_message(message: dict[str, Any], config: StreamConfig, received_ms: int) -> dict[str, Any] | None:
    """规范现货/合约字段；现货部分深度缺少事件时间，明确用接收时间。"""
    payload = message.get("data", message)
    if not isinstance(payload, dict):
        raise ValueError("行情正文必须为对象")
    stream = str(message.get("stream", ""))
    symbol = str(payload.get("s", stream.split("@", 1)[0])).lower()
    mapping = {symbol_for(pair): pair for pair in config.pairs}
    if symbol not in mapping:
        if "result" in payload:
            return None
        raise ValueError(f"收到未订阅符号：{symbol}")
    pair = mapping[symbol]
    extras = {"received_timestamp": received_ms, "raw": payload}
    if payload.get("e") in {"aggTrade", "trade"}:
        aggregate = payload["e"] == "aggTrade"
        maker = payload["m"]
        if not isinstance(maker, bool):
            raise ValueError("buyer_maker 必须是布尔值")
        extras.update(source="binance_aggTrade" if aggregate else "binance_trade", aggregated=aggregate)
        return {"kind": "tick", "market": config.market, "pair": pair, "row": {
            "trade_id": str(_nonnegative_int(payload["a" if aggregate else "t"])),
            "timestamp": _nonnegative_int(payload["T"]), "price": _decimal(payload["p"]),
            "quantity": _decimal(payload["q"]), "buyer_maker": maker, "extras": extras,
        }}
    if "@depth20" in stream:
        # 只处理明确订阅的部分深度，不能把增量深度误当作完整快照。
        bid_values = payload.get("bids", payload.get("b"))
        ask_values = payload.get("asks", payload.get("a"))
        if not isinstance(bid_values, list) or not isinstance(ask_values, list):
            raise ValueError("盘口 bids/asks 必须为数组")
        def levels(values: list[Any]) -> list[list[str]]:
            if any(not isinstance(level, (list, tuple)) or len(level) != 2 for level in values):
                raise ValueError("盘口档位必须包含价格和数量")
            return [[_decimal(p), _decimal(q)] for p, q in values[:20]]
        update_id = payload.get("u", payload.get("lastUpdateId"))
        if update_id is None:
            raise ValueError("盘口缺少更新 ID")
        timestamp = _nonnegative_int(payload.get("T", payload.get("E", received_ms)))
        extras.update(source="binance_partial_depth20", book_type="partial_snapshot", depth=20,
                      timestamp_source="exchange" if "T" in payload or "E" in payload else "received")
        return {"kind": "orderbook", "market": config.market, "pair": pair, "row": {
            "timestamp": timestamp, "last_update_id": str(_nonnegative_int(update_id)),
            "bids": levels(bid_values), "asks": levels(ask_values), "extras": extras,
        }}
    return None


class FileSpool:
    """数据库故障时持久保留批次；事务提交后删除，重播依靠数据库唯一键去重。"""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def persist(self, events: list[dict[str, Any]]) -> Path:
        with self._lock:
            return self._persist(events)

    def _persist(self, events: list[dict[str, Any]]) -> Path:
        path = self.directory / f"{time.time_ns():020d}-{uuid.uuid4().hex}.json"
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"version": 1, "events": events}, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # 文件和目录均落盘，随后数据库写入才可开始。
        self._sync_directory()
        return path

    def pending(self) -> list[Path]:
        with self._lock:
            # 崩溃可能留下一份尚未重命名的 tmp；不忽略它，以便重播或报告损坏。
            return sorted([*self.directory.glob("*.json"), *self.directory.glob("*.tmp")])

    def load(self, path: Path) -> list[dict[str, Any]]:
        with path.open(encoding="utf-8") as handle:
            content = json.load(handle)
        if not isinstance(content, dict) or content.get("version") != 1 or not isinstance(content.get("events"), list):
            raise ValueError(f"暂存批次格式错误：{path.name}")
        for event in content["events"]:
            if not isinstance(event, dict) or event.get("kind") not in {"tick", "orderbook"}:
                raise ValueError(f"暂存事件类型错误：{path.name}")
            if event.get("market") not in {"spot", "futures"} or not isinstance(event.get("pair"), str):
                raise ValueError(f"暂存事件市场或交易对错误：{path.name}")
            row = event.get("row")
            required = {"trade_id", "timestamp", "price", "quantity", "buyer_maker"} if event["kind"] == "tick" else {"timestamp", "last_update_id", "bids", "asks"}
            if not isinstance(row, dict) or not required.issubset(row):
                raise ValueError(f"暂存事件字段缺失：{path.name}")
        return content["events"]

    def quarantine(self, path: Path) -> Path:
        with self._lock:
            target = path.with_name(f"{path.name}.{uuid.uuid4().hex}.corrupt")
            os.replace(path, target)
            self._sync_directory()
            return target

    def corrupt_files(self) -> list[Path]:
        with self._lock:
            return sorted(self.directory.glob("*.corrupt"))

    def acknowledge(self, path: Path) -> None:
        path.unlink()
        self._sync_directory()

    def _sync_directory(self) -> None:
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class MarketCollector:
    def __init__(self, config: StreamConfig, store: Any):
        self.config, self.store = config, store
        self.spool = FileSpool(config.spool_dir)
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=config.queue_size)
        self.stop = asyncio.Event()
        self.status: dict[str, Any] = {
            "enabled": config.enabled, "exchange": "binance", "market": config.market,
            "pairs": list(config.pairs), "transport": "websocket", "state": "starting",
            "trade_type": "aggregated" if config.market == "futures" else "raw",
            "book_type": "partial_snapshot", "depth": 20, "depth_interval_ms": 100,
            "started_at": int(time.time() * 1000), "received": 0, "persisted": 0,
            "replayed": 0, "overflow_spooled": 0, "invalid_messages": 0,
            "dropped": 0, "inflight": 0, "buffer_length": 0, "database_failures": 0, "reconnects": 0,
            "connections": {}, "last_message": None, "last_flush": None,
            "last_error": None, "last_error_at": None, "database_error": None,
            "quarantined_batches": 0, "corrupt_batches": 0, "storage_warning": None,
        }
        self._retry_at = 0.0
        self._failure_streak = 0
        self._status_lock = asyncio.Lock()

    def record_error(self, error: Exception | str) -> None:
        self.status["last_error"] = str(error)
        self.status["last_error_at"] = int(time.time() * 1000)
        LOG.warning("%s", error)

    async def receive(self, message: dict[str, Any]) -> None:
        received_ms = int(time.time() * 1000)
        try:
            event = normalize_message(message, self.config, received_ms)
        except (KeyError, TypeError, ValueError) as exc:
            self.status["invalid_messages"] += 1
            self.status["dropped"] += 1
            self.record_error(f"非法行情消息已拒绝：{exc}")
            return
        if event is None:
            return
        self.status["received"] += 1
        self.status["last_message"] = received_ms
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                # 内存队列满时落盘，不因背压悄悄丢掉行情。
                await asyncio.to_thread(self.spool.persist, [event])
                self.status["overflow_spooled"] += 1
            except OSError as exc:
                self.status["dropped"] += 1
                self.status["state"] = "storage_error"
                self.record_error(f"队列满且暂存磁盘写入失败，停止采集：{exc}")
                self.stop.set()
                raise

    async def persist_batch(self, events: list[dict[str, Any]]) -> None:
        if events:
            await asyncio.to_thread(self.spool.persist, events)

    def _write(self, events: list[dict[str, Any]]) -> None:
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            groups[(event["kind"], event["market"], event["pair"])].append(event["row"])
        for (kind, market, pair), rows in groups.items():
            method = self.store.upsert_ticks if kind == "tick" else self.store.insert_orderbooks
            method("binance", market, pair, rows)

    async def flush_spool(self) -> bool:
        if time.monotonic() < self._retry_at:
            return False
        pending = await asyncio.to_thread(self.spool.pending)
        if not pending:
            return False
        path = pending[0]
        try:
            events = await asyncio.to_thread(self.spool.load, path)
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            # 中断写入产生的残缺 tmp 不能永久堵住整个队列；原文件隔离保留。
            isolated = await asyncio.to_thread(self.spool.quarantine, path)
            self.status["quarantined_batches"] += 1
            self.status["storage_warning"] = "暂存批次损坏已隔离保留，部分行情可能缺失，需要人工检查 .corrupt 文件"
            self.status["last_spool_error"] = f"{isolated.name}: {exc}"
            self.status["data_gap_possible"] = True
            self.record_error(f"损坏批次已隔离，继续有效批次：{isolated.name}；{exc}")
            await self.publish_status()
            return True
        self.status["inflight"] = len(events)
        try:
            # psycopg 是同步接口，放在线程中，不阻塞 WebSocket 的收包循环。
            await asyncio.to_thread(self._write, events)
            await asyncio.to_thread(self.spool.acknowledge, path)
        except Exception as exc:
            self._failure_streak += 1
            self._retry_at = time.monotonic() + min(self.config.retry_max, 2 ** min(self._failure_streak - 1, 10))
            self.status["database_failures"] += 1
            self.status["database_error"] = str(exc)
            self.record_error(f"写入失败，批次保留并退避重试：{exc}")
            return False
        finally:
            self.status["inflight"] = 0
        self._failure_streak = 0
        self._retry_at = 0.0
        self.status["persisted"] += len(events)
        self.status["replayed"] += len(events)
        self.status["last_flush"] = int(time.time() * 1000)
        self.status["database_error"] = None
        return True

    async def writer(self) -> None:
        deadline: float | None = None
        batch: list[dict[str, Any]] = []
        next_flush = time.monotonic() + self.config.flush_interval
        try:
            while True:
                if self.stop.is_set() and deadline is None:
                    deadline = time.monotonic() + self.config.shutdown_timeout
                try:
                    batch.append(await asyncio.wait_for(self.queue.get(), timeout=max(0.001, next_flush - time.monotonic())))
                    while len(batch) < self.config.batch_size:
                        try:
                            batch.append(self.queue.get_nowait())
                        except asyncio.QueueEmpty:
                            break
                except asyncio.TimeoutError:
                    pass
                self.status["buffer_length"] = len(batch)
                if len(batch) < self.config.batch_size and time.monotonic() < next_flush and not self.stop.is_set():
                    continue
                if batch:
                    await self.persist_batch(batch)
                    for _ in batch:
                        self.queue.task_done()
                    batch = []
                    self.status["buffer_length"] = 0
                next_flush = time.monotonic() + self.config.flush_interval
                # 每轮持久化新消息，再重播旧批次，故障时队列仍然能持续排空。
                for _ in range(10):
                    if not await self.flush_spool():
                        break
                if deadline is not None and self.queue.empty():
                    pending = await asyncio.to_thread(self.spool.pending)
                    if not pending or time.monotonic() >= deadline:
                        break
        except Exception as exc:
            self.status["state"] = "storage_error"
            self.record_error(f"暂存层故障，停止采集并保留未确认文件：{exc}")
            self.stop.set()
            raise
        finally:
            # 写盘故障发生在批次持久化之前时，保留失败批次并最后尝试一次。
            remaining = list(batch)
            while not self.queue.empty():
                remaining.append(self.queue.get_nowait())
            if remaining:
                try:
                    await self.persist_batch(remaining)
                except OSError as exc:
                    self.status["dropped"] += len(remaining)
                    self.record_error(f"关闭时仍有 {len(remaining)} 条消息未能落盘：{exc}")
            self.status["buffer_length"] = 0

    async def publish_status(self) -> None:
        # writer 的隔离告警也会主动发布，与定时状态写入串行，防止侧车 tmp 竞争。
        async with self._status_lock:
            await self._publish_status()

    async def _publish_status(self) -> None:
        corrupt = await asyncio.to_thread(self.spool.corrupt_files)
        self.status["corrupt_batches"] = len(corrupt)
        if corrupt:
            self.status["storage_warning"] = "暂存批次损坏已隔离保留，部分行情可能缺失，需要人工检查 .corrupt 文件"
            self.status["data_gap_possible"] = True
        self.status["corrupt_files"] = [path.name for path in corrupt[:100]]
        self.status["corrupt_file_list_truncated"] = len(corrupt) > 100
        snapshot = dict(self.status)
        snapshot["queue_length"] = self.queue.qsize()
        pending = await asyncio.to_thread(self.spool.pending)
        snapshot["spool_batches"] = len(pending)
        snapshot["retry_in_seconds"] = max(0, round(self._retry_at - time.monotonic(), 1))
        snapshot["updated_at"] = int(time.time() * 1000)
        path = self.config.spool_dir.parent / "market_stream_status.json"
        def write_status() -> None:
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, path)
        await asyncio.to_thread(write_status)
        try:
            await asyncio.to_thread(self.store.put_document, "market_stream_status.json", snapshot)
        except Exception as exc:
            # 数据库断开时，本地运行状态仍可供运维读取。
            LOG.debug("状态暂存在本地，数据库未同步：%s", exc)

    async def status_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await self.publish_status()
            except OSError as exc:
                self.status["state"] = "storage_error"
                self.record_error(f"本地状态写入失败，停止采集：{exc}")
                self.stop.set()
                raise
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.config.status_interval)
            except asyncio.TimeoutError:
                pass

    async def connection(self, session: Any, name: str, url: str) -> None:
        import aiohttp
        failures = 0
        while not self.stop.is_set():
            self.status["connections"][name] = "connecting"
            try:
                async with session.ws_connect(url, heartbeat=30, receive_timeout=90, max_msg_size=2 ** 20) as socket:
                    self.status["connections"][name] = "connected"
                    self.status["state"] = "running"
                    async for message in socket:
                        if self.stop.is_set():
                            break
                        if message.type == aiohttp.WSMsgType.TEXT:
                            await self.receive(json.loads(message.data))
                            failures = 0
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            raise ConnectionError(str(socket.exception()))
                    if not self.stop.is_set():
                        raise ConnectionError(f"{name} WebSocket 已断开")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.stop.is_set() and self.status["state"] == "storage_error":
                    break
                failures += 1
                self.status["connections"][name] = "disconnected"
                self.status["reconnects"] += 1
                self.status["state"] = "reconnecting"
                self.record_error(f"{name} 连接失败：{exc}")
                # 重连间隙无法恢复历史盘口，状态明确记录，不伪造行情。
                self.status["data_gap_possible"] = True
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=min(30, 2 ** min(failures - 1, 5)))
                except asyncio.TimeoutError:
                    pass
        self.status["connections"][name] = "stopped"

    async def run(self) -> None:
        # 获取跨进程锁之后才扫描暂存或写状态，第二实例无法读到正在写的 tmp。
        with CollectorInstanceLock(self.config.spool_dir.parent / ".market_stream.lock"):
            await self._run()

    async def _run(self) -> None:
        import aiohttp
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)
        writer = asyncio.create_task(self.writer(), name="market-writer")
        status = asyncio.create_task(self.status_loop(), name="market-status")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=15), trust_env=True) as session:
            connections = [asyncio.create_task(self.connection(session, name, url), name=f"market-{name}")
                           for name, url in self.config.connections()]
            stopped = asyncio.create_task(self.stop.wait())
            try:
                done, _ = await asyncio.wait([stopped, writer, status], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task is not stopped:
                        task.result()
            finally:
                self.stop.set()
                for task in connections:
                    task.cancel()
                await asyncio.gather(*connections, return_exceptions=True)
                await asyncio.gather(writer, status, return_exceptions=True)
                stopped.cancel()
                if self.status["state"] != "storage_error":
                    self.status["state"] = "stopped"
                await self.publish_status()


def parse_config(argv: list[str] | None = None) -> StreamConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=("futures", "spot"), default=os.getenv("QUANT_STREAM_MARKET", "futures"))
    parser.add_argument("--pairs", default=os.getenv("QUANT_STREAM_PAIRS", "BTC/USDT,ETH/USDT"), help="逗号分隔交易对")
    parser.add_argument("--spool-dir", type=Path, default=Path(os.getenv("QUANT_STREAM_SPOOL_DIR", str(ROOT / "database/runtime/market_spool"))))
    arguments = parser.parse_args(argv)
    pairs = tuple(dict.fromkeys(normalize_pair(pair, arguments.market) for pair in arguments.pairs.split(",") if pair.strip()))
    return StreamConfig(pairs=pairs, market=arguments.market, spool_dir=arguments.spool_dir,
                        enabled=os.getenv("QUANT_STREAM_ENABLED", "true").lower() not in {"false", "0", "no"})


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from runtime_config import load_environment
    load_environment(ROOT)
    config = parse_config()
    if not config.enabled:
        LOG.info("QUANT_STREAM_ENABLED 已关闭，采集器未启动")
        return 0
    from data_store import DataStore
    store = DataStore()
    collector = MarketCollector(config, store)
    try:
        asyncio.run(collector.run())
    except CollectorAlreadyRunning as exc:
        LOG.error("%s", exc)
        return 2
    finally:
        store.close()
    return 1 if collector.status["state"] == "storage_error" else 0


if __name__ == "__main__":
    raise SystemExit(main())

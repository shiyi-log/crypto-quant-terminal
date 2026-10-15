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
import shutil
import threading
import time
from typing import Any
import uuid

from orderflow_detector import VolumeSurgeDetector
from orderflow_maturity import OrderflowMaturityWorker

LOG = logging.getLogger("market_stream")
ROOT = Path(__file__).resolve().parent

# Binance WebSocket interval names accepted by the kline endpoint.  The
# collector only subscribes to explicitly configured intervals; keeping this
# allow-list here also prevents malformed spool events from being replayed.
KLINE_INTERVALS = frozenset({
    "1s", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h",
    "8h", "12h", "1d", "3d", "1w", "1M",
})
MAX_DETERMINISTIC_BATCH_WRITE_FAILURES = 5
_DETERMINISTIC_BATCH_ERROR_NAMES = {
    "DataError", "CheckViolation", "ForeignKeyViolation", "NotNullViolation",
    "InvalidTextRepresentation", "NumericValueOutOfRange",
}


def _is_deterministic_batch_write_error(exc: Exception) -> bool:
    return (isinstance(exc, (ValueError, TypeError, KeyError, AttributeError))
            or type(exc).__name__ in _DETERMINISTIC_BATCH_ERROR_NAMES)


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
    status_document_key: str = "market_stream_status.json"
    enabled: bool = True
    disk_warning_pct: float = 15.0
    disk_critical_pct: float = 5.0
    disk_warning_bytes: int = 0
    disk_critical_bytes: int = 0
    # ``None`` preserves the historical market-specific default: futures use
    # aggregate trades while spot uses raw trades.  ``trade`` can be selected
    # explicitly for futures without changing the default deployment.
    trade_stream: str | None = None
    kline_intervals: tuple[str, ...] = ("5m",)
    gap_threshold_ms: int = 60_000

    def __post_init__(self) -> None:
        if self.market not in {"futures", "spot"}:
            raise ValueError("market 必须为 futures 或 spot")
        if self.trade_stream is not None and self.trade_stream not in {"aggTrade", "trade"}:
            raise ValueError("trade_stream 必须为 aggTrade 或 trade")
        if not self.kline_intervals or any(interval not in KLINE_INTERVALS for interval in self.kline_intervals):
            raise ValueError("kline_intervals 包含不支持的 Binance K 线周期")
        if len(set(self.kline_intervals)) != len(self.kline_intervals):
            raise ValueError("kline_intervals 不能包含重复周期")
        if isinstance(self.gap_threshold_ms, bool) or not isinstance(self.gap_threshold_ms, int):
            raise ValueError("gap_threshold_ms 必须为正整数")
        if self.gap_threshold_ms <= 0:
            raise ValueError("gap_threshold_ms 必须为正整数")
        if not self.pairs or self.queue_size < 1 or self.batch_size < 1:
            raise ValueError("交易对、队列容量与批次容量必须有效")
        if min(self.flush_interval, self.retry_max, self.shutdown_timeout, self.status_interval) <= 0:
            raise ValueError("等待时间必须大于 0")
        if not 0 <= self.disk_critical_pct < self.disk_warning_pct <= 100:
            raise ValueError("磁盘容量阈值必须满足 0 <= critical < warning <= 100")
        if self.disk_warning_bytes < 0 or self.disk_critical_bytes < 0:
            raise ValueError("磁盘剩余字节阈值不能为负数")
        if self.disk_critical_bytes and self.disk_warning_bytes and self.disk_critical_bytes > self.disk_warning_bytes:
            raise ValueError("磁盘临界剩余字节阈值不能高于告警阈值")
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.json", self.status_document_key):
            raise ValueError("状态文档键必须是独立 JSON 文件名")

    @property
    def effective_trade_stream(self) -> str:
        return self.trade_stream or ("aggTrade" if self.market == "futures" else "trade")

    def connections(self) -> list[tuple[str, str]]:
        # 当前币安合约文档将成交和盘口分置于 market/public 分区。
        symbols = [symbol_for(pair) for pair in self.pairs]
        trades = "/".join(f"{s}@{self.effective_trade_stream}" for s in symbols)
        klines = "/".join(
            f"{s}@kline_{interval}"
            for s in symbols for interval in self.kline_intervals
        )
        books = "/".join(f"{s}@depth20@100ms" for s in symbols)
        if self.market == "futures":
            return [
                ("trades", f"wss://fstream.binance.com/market/stream?streams={trades}/{klines}"),
                ("orderbooks", f"wss://fstream.binance.com/public/stream?streams={books}"),
            ]
        return [("market", f"wss://stream.binance.com:9443/stream?streams={trades}/{books}/{klines}")]


def _disk_capacity(path: Path, config: StreamConfig) -> dict[str, Any]:
    """读取暂存目录所在分区容量，并根据剩余百分比/字节数分级。"""
    checked_at = int(time.time() * 1000)
    try:
        usage = shutil.disk_usage(path)
        free_pct = (usage.free / usage.total * 100) if usage.total else 0.0
        critical_reasons: list[str] = []
        warning_reasons: list[str] = []
        if free_pct < config.disk_critical_pct:
            critical_reasons.append(f"剩余空间 {free_pct:.2f}% < {config.disk_critical_pct:g}%")
        elif free_pct < config.disk_warning_pct:
            warning_reasons.append(f"剩余空间 {free_pct:.2f}% < {config.disk_warning_pct:g}%")
        if config.disk_critical_bytes and usage.free < config.disk_critical_bytes:
            critical_reasons.append(f"剩余 {usage.free} bytes < {config.disk_critical_bytes} bytes")
        elif config.disk_warning_bytes and usage.free < config.disk_warning_bytes:
            warning_reasons.append(f"剩余 {usage.free} bytes < {config.disk_warning_bytes} bytes")
        if critical_reasons:
            level, reason = "critical", "; ".join(critical_reasons)
        elif warning_reasons:
            level, reason = "warning", "; ".join(warning_reasons)
        else:
            level, reason = "ok", None
        return {
            "disk_total_bytes": int(usage.total),
            "disk_used_bytes": int(usage.used),
            "disk_free_bytes": int(usage.free),
            "disk_free_pct": round(free_pct, 2),
            "disk_alert_level": level,
            "disk_alert_reason": reason,
            "disk_checked_at": checked_at,
        }
    except (OSError, ValueError) as exc:
        return {
            "disk_total_bytes": None,
            "disk_used_bytes": None,
            "disk_free_bytes": None,
            "disk_free_pct": None,
            "disk_alert_level": "unknown",
            "disk_alert_reason": f"无法检查暂存目录磁盘容量：{exc}",
            "disk_checked_at": checked_at,
        }


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


def _timeframe_milliseconds(timeframe: str | None) -> int | None:
    """Return a fixed Binance kline interval in milliseconds.

    Monthly candles are calendar based rather than fixed length, so they are
    intentionally excluded from arithmetic gap detection.
    """
    if not isinstance(timeframe, str):
        return None
    match = re.fullmatch(r"(\d+)([smhdw])", timeframe)
    if not match:
        return None
    amount = int(match.group(1))
    unit_ms = {"s": 1_000, "m": 60_000, "h": 3_600_000,
               "d": 86_400_000, "w": 604_800_000}[match.group(2)]
    return amount * unit_ms


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
    if payload.get("e") == "kline":
        raw = payload.get("k")
        if not isinstance(raw, dict):
            raise ValueError("K 线正文必须为对象")
        payload_symbol = payload.get("s")
        if not isinstance(payload_symbol, str) or not payload_symbol:
            raise ValueError("K 线缺少交易对符号")
        if stream and stream.split("@", 1)[0].lower() != payload_symbol.lower():
            raise ValueError("K 线流与正文交易对符号不一致")
        if raw.get("s") != payload_symbol:
            raise ValueError("K 线与正文交易对符号不一致")
        closed = raw.get("x")
        if not isinstance(closed, bool):
            raise ValueError("K 线收盘标记必须是布尔值")
        interval = raw.get("i")
        if interval not in KLINE_INTERVALS:
            raise ValueError("K 线周期无效")
        if interval not in config.kline_intervals or not closed:
            # 采集器只归档显式配置的收盘 K 线，未收盘更新由交易/图表流负责。
            return None
        timestamp = _nonnegative_int(raw.get("t"))
        event_timestamp = _nonnegative_int(payload.get("E", received_ms))
        def finite_float(value: Any, name: str, *, positive: bool = False) -> float:
            number = Decimal(_decimal(value))
            converted = float(number)
            if not -float("inf") < converted < float("inf"):
                raise ValueError(f"K 线{name}超出有限数范围")
            if positive and (number <= 0 or converted <= 0):
                raise ValueError(f"K 线{name}必须为正数")
            return converted
        candle = {
            "timestamp": timestamp,
            "open": finite_float(raw.get("o"), "价格", positive=True),
            "high": finite_float(raw.get("h"), "价格", positive=True),
            "low": finite_float(raw.get("l"), "价格", positive=True),
            "close": finite_float(raw.get("c"), "价格", positive=True),
            "volume": _decimal(raw.get("v")),
            "closed": True,
        }
        opening, high, low, closing = (Decimal(_decimal(raw[field])) for field in ("o", "h", "l", "c"))
        if not (low <= min(opening, closing) <= max(opening, closing) <= high):
            raise ValueError("K 线最高价和最低价必须覆盖开盘价与收盘价")
        quote_volume = _decimal(raw.get("q"))
        candle["quote_volume"] = finite_float(quote_volume, "成交额")
        base_volume = Decimal(candle["volume"])
        quote_volume_decimal = Decimal(quote_volume)
        if raw.get("n") is not None:
            candle["num_trades"] = _nonnegative_int(raw["n"])
        if raw.get("V") is not None:
            taker_buy_base = _decimal(raw["V"])
            candle["taker_buy_base"] = taker_buy_base
            if base_volume > 0:
                candle["flow_imbal_base"] = float(
                    2 * Decimal(taker_buy_base) / base_volume - 1
                )
        if raw.get("Q") is not None:
            taker_buy_quote = finite_float(raw["Q"], "买方成交额")
            taker_buy_quote_decimal = Decimal(_decimal(raw["Q"]))
            if taker_buy_quote_decimal > quote_volume_decimal:
                raise ValueError("买方成交额不能高于 K 线成交额")
            candle["taker_buy_quote"] = taker_buy_quote
            if quote_volume_decimal > 0:
                quote_imbalance = float(
                    2 * taker_buy_quote_decimal / quote_volume_decimal - 1
                )
                candle["flow_imbal"] = quote_imbalance
                candle["flow_imbal_q"] = quote_imbalance
        extras.update(source=f"binance_kline_{interval}", closed=True,
                      exchange_event_timestamp=event_timestamp)
        return {
            "kind": "kline", "market": config.market, "pair": pair,
            "timeframe": interval, "candle": candle,
            "row": {**candle, "extras": extras},
            "source_timestamp": received_ms,
        }
    if payload.get("e") in {"aggTrade", "trade"}:
        aggregate = payload["e"] == "aggTrade"
        maker = payload["m"]
        if not isinstance(maker, bool):
            raise ValueError("buyer_maker 必须是布尔值")
        original_trade_id = _nonnegative_int(payload["a" if aggregate else "t"])
        extras.update(
            source="binance_aggTrade" if aggregate else "binance_trade",
            aggregated=aggregate,
            stream_kind="aggTrade" if aggregate else "trade",
            original_trade_id=original_trade_id,
        )
        # aggTrade IDs and raw trade IDs are different exchange namespaces.
        # Prefixing raw IDs prevents an explicitly enabled raw stream from
        # overwriting an existing aggregate row in the ticks upsert key.
        # Spot's historical default is raw ``trade`` and keeps its numeric ID
        # for compatibility.  A caller that explicitly selects the raw stream
        # opts into the namespaced ID needed when aggTrade and trade coexist.
        # Spot trade IDs are already isolated by the market column and must
        # retain their historical numeric form, even if a shared environment
        # selects raw trades for the futures collector.
        namespace_raw_id = config.market == "futures" and config.trade_stream == "trade"
        trade_id = (
            str(original_trade_id)
            if aggregate or not namespace_raw_id
            else f"trade:{original_trade_id}"
        )
        return {"kind": "tick", "market": config.market, "pair": pair, "row": {
            "trade_id": trade_id,
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
        self._retry_directory = self.directory / ".retry"
        self._dead_letter_directory = self.directory / ".dead-letter"
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
            if not isinstance(event, dict) or event.get("kind") not in {
                "tick", "orderbook", "kline", "orderflow_surge", "price_candidate",
            }:
                raise ValueError(f"暂存事件类型错误：{path.name}")
            if event.get("market") not in {"spot", "futures"} or not isinstance(event.get("pair"), str):
                raise ValueError(f"暂存事件市场或交易对错误：{path.name}")
            if (event["kind"] == "price_candidate"
                    and (event["market"] != "futures" or event.get("timeframe") != "5m")):
                raise ValueError(f"在线价格爆发候选只接受 futures 5m：{path.name}")
            row = event.get("row")
            required = {
                "tick": {"trade_id", "timestamp", "price", "quantity", "buyer_maker"},
                "orderbook": {"timestamp", "last_update_id", "bids", "asks"},
                "kline": {"timestamp", "open", "high", "low", "close", "volume", "extras"},
                "orderflow_surge": {"event_id", "observation", "method_version", "data_fingerprint"},
                "price_candidate": {"event_id", "observation", "method_version", "data_fingerprint"},
            }[event["kind"]]
            if not isinstance(row, dict) or not required.issubset(row):
                raise ValueError(f"暂存事件字段缺失：{path.name}")
            if event["kind"] == "kline" and event.get("timeframe") not in KLINE_INTERVALS:
                raise ValueError(f"暂存 K 线周期错误：{path.name}")
            if event["kind"] in {"orderflow_surge", "price_candidate"} and not isinstance(row["observation"], dict):
                raise ValueError(f"暂存订单流观测格式错误：{path.name}")
        return content["events"]

    def quarantine(self, path: Path, metadata: dict[str, Any] | None = None) -> Path:
        with self._lock:
            if metadata is None:
                target = path.with_name(f"{path.name}.{uuid.uuid4().hex}.corrupt")
            else:
                self._dead_letter_directory.mkdir(parents=True, exist_ok=True)
                target = self._dead_letter_directory / f"{path.name}.{uuid.uuid4().hex}.dead"
            os.replace(path, target)
            self._sync_directory()
            if target.parent != self.directory:
                self._sync_directory(target.parent)
            self.clear_write_failure(path)
            if metadata is not None:
                self._write_json_atomic(self._dead_letter_directory / f"{target.name}.json",
                                        {**metadata, "batch": path.name,
                                         "quarantined_file": target.name})
            return target

    def corrupt_files(self) -> list[Path]:
        with self._lock:
            return sorted(self.directory.glob("*.corrupt"))

    def acknowledge(self, path: Path) -> None:
        path.unlink()
        self._sync_directory()
        self.clear_write_failure(path)

    def record_write_failure(self, path: Path, error: Exception, *, now_ms: int | None = None) -> dict[str, Any]:
        with self._lock:
            self._retry_directory.mkdir(parents=True, exist_ok=True)
            metadata_path = self._retry_directory / f"{path.name}.json"
            if metadata_path.exists():
                with metadata_path.open(encoding="utf-8") as handle:
                    previous = json.load(handle)
            else:
                previous = {}
            observed_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
            result = {
                "batch": path.name,
                "attempts": int(previous.get("attempts", 0)) + 1,
                "first_failure_ms": int(previous.get("first_failure_ms", observed_ms)),
                "last_failure_ms": observed_ms,
                "error_kind": type(error).__name__,
                "deterministic": _is_deterministic_batch_write_error(error),
            }
            self._write_json_atomic(metadata_path, result)
            return result

    def write_failures(self) -> list[dict[str, Any]]:
        with self._lock:
            records = []
            if not self._retry_directory.exists():
                return records
            for path in sorted(self._retry_directory.glob("*.json")):
                with path.open(encoding="utf-8") as handle:
                    records.append(json.load(handle))
            return records

    def dead_letters(self) -> list[dict[str, Any]]:
        with self._lock:
            records = []
            if not self._dead_letter_directory.exists():
                return records
            for path in sorted(self._dead_letter_directory.glob("*.json")):
                with path.open(encoding="utf-8") as handle:
                    records.append(json.load(handle))
            return records

    def clear_write_failure(self, path: Path) -> None:
        metadata_path = self._retry_directory / f"{path.name}.json"
        try:
            metadata_path.unlink()
        except FileNotFoundError:
            return
        self._sync_directory(self._retry_directory)

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        self._sync_directory(path.parent)

    def _sync_directory(self, directory: Path | None = None) -> None:
        descriptor = os.open(directory or self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class MarketCollector:
    def __init__(self, config: StreamConfig, store: Any,
                 surge_detector: VolumeSurgeDetector | None = None,
                 *, maturity_store_factory: Any | None = None):
        self.config, self.store = config, store
        self.maturity_worker = (OrderflowMaturityWorker(maturity_store_factory)
                                if maturity_store_factory is not None else None)
        self.spool = FileSpool(config.spool_dir)
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=config.queue_size)
        self.stop = asyncio.Event()
        self.status: dict[str, Any] = {
            "enabled": config.enabled, "exchange": "binance", "market": config.market,
            "pairs": list(config.pairs), "transport": "websocket", "state": "starting",
            "trade_type": "aggregated" if config.effective_trade_stream == "aggTrade" else "raw",
            "trade_stream": config.effective_trade_stream,
            "kline_intervals": list(config.kline_intervals),
            "book_type": "partial_snapshot", "depth": 20, "depth_interval_ms": 100,
            "started_at": int(time.time() * 1000), "received": 0, "persisted": 0,
            "replayed": 0, "overflow_spooled": 0, "invalid_messages": 0,
            "dropped": 0, "inflight": 0, "buffer_length": 0, "database_failures": 0, "reconnects": 0,
            "connections": {name: "connecting" for name, _ in config.connections()},
            "gap_threshold_ms": config.gap_threshold_ms,
            "gap_count": 0, "max_gap_ms": 0, "last_gap": None,
            "gap_metrics": {},
            "connection_gap_metrics": {
                name: {"gap_count": 0, "max_gap_ms": 0, "last_gap": None}
                for name, _ in config.connections()
            },
            "data_gap_possible": False,
            "connection_errors": {}, "last_message": None, "last_flush": None,
            "last_error": None, "last_error_at": None, "database_error": None,
            "quarantined_batches": 0, "corrupt_batches": 0, "storage_warning": None,
            "dead_letter_batches": 0, "write_failure_batches": 0,
            "oldest_write_failure_ms": None, "last_dead_letter": None,
        }
        self._retry_at = 0.0
        self._failure_streak = 0
        self._status_lock = asyncio.Lock()
        self.surge_detector = surge_detector or VolumeSurgeDetector()
        self.status.update({
            "surge_observed": 0, "surge_persisted": 0, "surge_pending": 0,
            "surge_failures": 0, "surge_dropped": 0, "surge_last_error": None,
            "price_candidate_observed": 0, "price_candidate_persisted": 0,
            "price_candidate_pending": 0, "price_candidate_failures": 0,
            "price_candidate_dropped": 0, "price_candidate_last_error": None,
            "surge_warmup": {
                pair: {**self.surge_detector.warmup_status(pair, market=config.market),
                       "source": None}
                for pair in config.pairs
            },
        })

    @staticmethod
    def _gap_connection_name(kind: str, connections: dict[str, Any]) -> str:
        """Map an event kind to the connection carrying its messages."""
        if kind == "orderbook":
            return "orderbooks" if "orderbooks" in connections else "market"
        return "trades" if "trades" in connections else "market"

    def record_event_gap(self, event: dict[str, Any]) -> None:
        """Record a large exchange-timestamp gap for one event identity.

        K-lines are compared against their configured candle interval; a
        regular boundary therefore contributes zero gap.  Tick and orderbook
        streams have no fixed cadence, so their full timestamp delta is used.
        Out-of-order and duplicate events do not move the high-water mark.
        """
        kind = event.get("kind")
        if kind not in {"tick", "orderbook", "kline"}:
            return
        row = event.get("row")
        if not isinstance(row, dict):
            return
        try:
            timestamp = _nonnegative_int(row.get("timestamp"))
        except (TypeError, ValueError, OverflowError):
            return
        market = event.get("market")
        pair = event.get("pair")
        if not isinstance(market, str) or not isinstance(pair, str):
            return
        timeframe = event.get("timeframe") if kind == "kline" else None
        key = f"{kind}:{market}:{pair}:{timeframe}" if timeframe else f"{kind}:{market}:{pair}"
        metrics = self.status["gap_metrics"].setdefault(key, {
            "kind": kind, "market": market, "pair": pair,
            "gap_count": 0, "max_gap_ms": 0, "last_gap": None,
            "last_event_timestamp": None,
        })
        if timeframe:
            metrics["timeframe"] = timeframe
        previous = metrics.get("last_event_timestamp")
        if previous is None:
            metrics["last_event_timestamp"] = timestamp
            return
        if timestamp <= previous:
            return
        expected_interval = (
            _timeframe_milliseconds(event.get("timeframe")) if kind == "kline" else 0
        )
        if expected_interval is None:
            metrics["last_event_timestamp"] = timestamp
            return
        observed_interval = timestamp - previous
        gap_ms = max(0, observed_interval - expected_interval)
        metrics["last_event_timestamp"] = timestamp
        if gap_ms < self.config.gap_threshold_ms:
            return
        detail = {
            "kind": kind, "market": market, "pair": pair,
            "previous_event_timestamp": previous,
            "event_timestamp": timestamp,
            "observed_interval_ms": observed_interval,
            "expected_interval_ms": expected_interval,
            "gap_ms": gap_ms,
            "detected_at": int(time.time() * 1000),
        }
        if kind == "kline":
            detail["timeframe"] = event.get("timeframe")
        metrics["gap_count"] += 1
        metrics["max_gap_ms"] = max(metrics["max_gap_ms"], gap_ms)
        metrics["last_gap"] = detail
        self.status["gap_count"] += 1
        self.status["max_gap_ms"] = max(self.status["max_gap_ms"], gap_ms)
        self.status["last_gap"] = detail
        connection = self._gap_connection_name(kind, self.status["connections"])
        connection_metrics = self.status["connection_gap_metrics"].setdefault(connection, {
            "gap_count": 0, "max_gap_ms": 0, "last_gap": None,
        })
        connection_metrics["gap_count"] += 1
        connection_metrics["max_gap_ms"] = max(connection_metrics["max_gap_ms"], gap_ms)
        connection_metrics["last_gap"] = detail
        self.status["data_gap_possible"] = True
        LOG.warning(
            "行情时间缺口 kind=%s market=%s pair=%s gap_ms=%s previous=%s current=%s",
            kind, market, pair, gap_ms, previous, timestamp,
        )

    @staticmethod
    def _surge_observation(event: dict[str, Any]) -> dict[str, Any]:
        """Adapt detector envelopes to DataStore's flat observation contract."""
        observation = event.get("observation")
        if not isinstance(observation, dict):
            raise ValueError("突增检测器事件缺少 observation")
        result = dict(observation)
        result.update({key: event[key] for key in (
            "event_id", "event_type", "method_version", "data_fingerprint",
            "market", "pair", "timeframe", "observed_at", "status",
        ) if key in event})
        result["threshold_level"] = result.pop("threshold_band", result.get("threshold_level"))
        result["threshold_multiple"] = result.pop("threshold", result.get("threshold_multiple"))
        return result

    async def seed_surge_detector(self) -> None:
        """Warm from this collector's own durable closed Binance 5m candles."""
        getter = getattr(self.store, "get_candles", None)
        reader = getattr(self.store, "read_orderflow_surges", None)
        list_observations = getattr(self.store, "list_surge_observations", None)
        now_ms = int(time.time() * 1000)
        for pair in self.config.pairs:
            try:
                if callable(getter):
                    history: dict[int, tuple[Any, str]] = {}
                    rows = await asyncio.to_thread(
                        getter, "binance", self.config.market, pair, "5m",
                        self.config.market, self.surge_detector.window_size + 1,
                    )
                    for row in rows or []:
                        if not isinstance(row, dict):
                            continue
                        extras = row.get("extras")
                        if (not isinstance(extras, dict)
                                or extras.get("closed") is not True
                                or extras.get("live_event_timestamp") is None):
                            continue
                        try:
                            timestamp = _nonnegative_int(row.get("timestamp"))
                        except (TypeError, ValueError, OverflowError):
                            continue
                        if timestamp + 300_000 > now_ms:
                            continue
                        quote_volume = extras.get("quote_volume", row.get("quote_volume"))
                        if quote_volume is not None:
                            history[timestamp] = (
                                quote_volume, "binance_websocket_closed_kline",
                            )
                    if history:
                        ordered = sorted(history.items())
                        suffix: list[tuple[int, Any, str]] = []
                        for timestamp, (volume, source) in reversed(ordered):
                            if suffix and suffix[-1][0] - timestamp != 300_000:
                                break
                            suffix.append((timestamp, volume, source))
                        suffix.reverse()
                        ordered = [(timestamp, volume) for timestamp, volume, _ in suffix]
                        ordered = ordered[-self.surge_detector.window_size:]
                        self.surge_detector.seed(
                            pair, [volume for _, volume in ordered], market=self.config.market,
                            timestamps=[timestamp for timestamp, _ in ordered],
                        )
                        sources = {source for _, _, source in suffix[-len(ordered):]} if ordered else set()
                        self.status["surge_warmup"][pair] = {
                            **self.surge_detector.warmup_status(pair, market=self.config.market),
                            "source": next(iter(sources)) if len(sources) == 1 else (
                                "mixed" if sources else None
                            ),
                        }
                if callable(reader):
                    persisted = await asyncio.to_thread(
                        reader, market=self.config.market, pair=pair, timeframe="5m",
                        limit=self.surge_detector.window_size,
                    )
                elif callable(list_observations):
                    persisted = await asyncio.to_thread(
                        list_observations, pair, "5m", None, self.surge_detector.window_size,
                    )
                else:
                    persisted = []
                self.surge_detector.restore_event_ids(
                    row["event_id"] for row in persisted or []
                    if isinstance(row, dict) and isinstance(row.get("event_id"), str)
                    and row.get("market", self.config.market) == self.config.market
                )
            except Exception as exc:
                # A cold detector is safe; make the missing warm-up visible.
                self.status["surge_last_error"] = str(exc)
                self.record_error(f"突增检测器历史预热失败：{exc}")

    def record_error(self, error: Exception | str) -> None:
        self.status["last_error"] = str(error)
        self.status["last_error_at"] = int(time.time() * 1000)
        LOG.warning("%s", error)

    def connection_recovered(self, name: str) -> None:
        self.status["connections"][name] = "connected"
        recovered_error = self.status["connection_errors"].pop(name, None)
        if recovered_error and self.status["last_error"] == recovered_error:
            self.status["last_error"] = next(reversed(self.status["connection_errors"].values()), None)
            if self.status["last_error"] is None:
                self.status["last_error_at"] = None
        if self.status["state"] != "storage_error":
            self.status["state"] = (
                "running" if all(value == "connected" for value in self.status["connections"].values())
                else "reconnecting"
            )

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
        self.record_event_gap(event)
        self.status["received"] += 1
        self.status["last_message"] = received_ms
        if event["kind"] == "kline":
            surge = None
            candidate = None
            if event["timeframe"] == "5m":
                try:
                    surge = self.surge_detector.process(
                        event["pair"], event["timeframe"], event["candle"],
                        market=event["market"], source_timestamp=event["source_timestamp"],
                    )
                    candidate = getattr(self.surge_detector, "last_candidate", None)
                    self.status["surge_warmup"][event["pair"]] = {
                        **self.surge_detector.warmup_status(
                            event["pair"], market=event["market"]
                        ),
                        "source": self.status["surge_warmup"][event["pair"]].get("source"),
                    }
                except Exception as exc:
                    self.status["surge_last_error"] = str(exc)
                    self.record_error(f"突增检测器处理失败：{exc}")
            # Closed candles and 5m observations are low-frequency facts. Save
            # them together before returning; 1m candles are persisted alone
            # and intentionally never advance the 5m detector.
            extras = event["row"]["extras"]
            extras.update({key: value for key, value in event["candle"].items()
                           if key not in {"timestamp", "open", "high", "low", "close", "volume"}})
            extras["live_event_timestamp"] = extras["exchange_event_timestamp"]
            batch = [event]
            if candidate is not None and event["market"] == "futures":
                batch.append({"kind": "price_candidate", "market": event["market"],
                              "pair": event["pair"], "timeframe": event["timeframe"], "row": candidate})
                self.status["price_candidate_observed"] += 1
                self.status["price_candidate_pending"] += 1
            if surge is not None:
                batch.append({"kind": "orderflow_surge", "market": event["market"],
                              "pair": event["pair"], "timeframe": event["timeframe"], "row": surge})
                self.status["surge_observed"] += 1
                # Count before spooling so a concurrent writer cannot acknowledge
                # the batch before the pending metric is visible.
                self.status["surge_pending"] += 1
            try:
                await self.persist_batch(batch)
            except OSError as exc:
                self.status["dropped"] += len(batch)
                self.status["surge_dropped"] += int(surge is not None)
                self.status["surge_pending"] = max(
                    0, self.status["surge_pending"] - int(surge is not None)
                )
                self.status["price_candidate_dropped"] += int(candidate is not None)
                self.status["price_candidate_pending"] = max(
                    0, self.status["price_candidate_pending"] - int(candidate is not None)
                )
                self.status["state"] = "storage_error"
                self.record_error(f"收盘 K 线与在线订单流观测暂存失败，停止采集：{exc}")
                self.stop.set()
                raise
            return
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
        groups: dict[tuple[str, str, str, str | None], list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            groups[(event["kind"], event["market"], event["pair"], event.get("timeframe"))].append(event["row"])
        for (kind, market, pair, timeframe), rows in groups.items():
            if kind == "kline":
                source_key = (
                    "live:binance-ws-klines"
                    if timeframe == "5m"
                    else f"live:binance-ws-kline-{timeframe}"
                )
                self.store.upsert_candles(
                    "binance", market, pair, timeframe, market, rows,
                    source_key=source_key,
                )
                if market == "futures":
                    self.store.upsert_candles(
                        "binance", market, pair, timeframe, "orderflow", rows,
                        source_key=source_key,
                    )
            elif kind == "orderflow_surge":
                append = getattr(self.store, "append_orderflow_surge", None)
                for event in rows:
                    try:
                        if callable(append):
                            append(event)
                        else:
                            self.store.insert_surge_observation(self._surge_observation(event))
                    except Exception as exc:
                        self.status["surge_failures"] += 1
                        self.status["surge_last_error"] = str(exc)
                        raise
            elif kind == "price_candidate":
                append = getattr(self.store, "append_price_candidate", None)
                if not callable(append):
                    raise RuntimeError("DataStore 缺少 append_price_candidate")
                for event in rows:
                    try:
                        append(event)
                    except Exception as exc:
                        self.status["price_candidate_failures"] += 1
                        self.status["price_candidate_last_error"] = str(exc)
                        raise
            else:
                method = self.store.upsert_ticks if kind == "tick" else self.store.insert_orderbooks
                method("binance", market, pair, rows)
        maturity_worker = getattr(self, "maturity_worker", None)
        if maturity_worker is not None and any(event["kind"] in {
            "kline", "orderflow_surge", "price_candidate",
        } for event in events):
            maturity_worker.notify()

    async def flush_spool(self) -> bool:
        if time.monotonic() < self._retry_at:
            return False
        pending = await asyncio.to_thread(self.spool.pending)
        if not pending:
            return False
        path = pending[0]
        try:
            events = await asyncio.to_thread(self.spool.load, path)
        except OSError as exc:
            # 临时读取故障不代表内容损坏，保留原批次供存储恢复后重播。
            self.status["state"] = "storage_error"
            self.record_error(f"暂存批次读取失败，保留原文件并停止采集：{exc}")
            self.stop.set()
            raise
        except (UnicodeError, ValueError, TypeError) as exc:
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
            try:
                await asyncio.to_thread(self._write, events)
            except Exception as exc:
                failure = await asyncio.to_thread(self.spool.record_write_failure, path, exc)
                self._failure_streak += 1
                self.status["database_failures"] += 1
                self.status["database_error"] = str(exc)
                self.status["write_failure_batches"] = len(await asyncio.to_thread(self.spool.write_failures))
                self.status["oldest_write_failure_ms"] = min(
                    record["first_failure_ms"]
                    for record in await asyncio.to_thread(self.spool.write_failures)
                )
                if (failure["deterministic"]
                        and failure["attempts"] >= MAX_DETERMINISTIC_BATCH_WRITE_FAILURES):
                    isolated = await asyncio.to_thread(
                        self.spool.quarantine, path,
                        {"reason": "deterministic_write_failure" if failure["deterministic"]
                         else "write_failure_threshold_reached",
                         "attempts": failure["attempts"],
                         "error_kind": failure["error_kind"],
                         "first_failure_ms": failure["first_failure_ms"],
                         "last_failure_ms": failure["last_failure_ms"]})
                    self.status["dead_letter_batches"] += 1
                    self.status["last_dead_letter"] = {
                        "file": isolated.name, "reason": "deterministic_write_failure"
                        if failure["deterministic"] else "write_failure_threshold_reached",
                        "attempts": failure["attempts"], "error_kind": failure["error_kind"],
                    }
                    self.status["storage_warning"] = (
                        "确定性写入失败批次已隔离到 dead-letter，需人工检查"
                    )
                    self.status["data_gap_possible"] = True
                    self.record_error(f"写入失败批次已隔离：{isolated.name}；{exc}")
                    await self.publish_status()
                    return True
                if failure["attempts"] >= MAX_DETERMINISTIC_BATCH_WRITE_FAILURES:
                    self.status["state"] = "storage_error"
                    self.status["storage_warning"] = (
                        "同一批次连续写入失败 5 次，已停止采集并保留原始批次；"
                        "请检查数据库/表结构，修复后重启以继续重放"
                    )
                    self.record_error(f"写入连续失败达到阈值，原批次保留且采集已停止：{path.name}；{exc}")
                    self.stop.set()
                    return False
                self._retry_at = time.monotonic() + min(
                    self.config.retry_max, 2 ** min(self._failure_streak - 1, 10)
                )
                self.record_error(f"写入失败，批次保留并退避重试：{exc}")
                return False
            try:
                await asyncio.to_thread(self.spool.acknowledge, path)
            except OSError as exc:
                # 数据库已经提交；删除或目录 fsync 失败属于暂存盘故障。
                # unlink 后 fsync 失败时文件可能已消失，不能声称批次仍保留。
                self.status["state"] = "storage_error"
                self.record_error(f"已提交批次的暂存确认失败，停止采集：{exc}")
                self.stop.set()
                raise
        finally:
            self.status["inflight"] = 0
        self._failure_streak = 0
        self._retry_at = 0.0
        self.status["write_failure_batches"] = len(await asyncio.to_thread(self.spool.write_failures))
        failures = await asyncio.to_thread(self.spool.write_failures)
        self.status["oldest_write_failure_ms"] = (
            min(record["first_failure_ms"] for record in failures) if failures else None
        )
        self.status["persisted"] += len(events)
        self.status["replayed"] += len(events)
        surges = sum(event["kind"] == "orderflow_surge" for event in events)
        candidates = sum(event["kind"] == "price_candidate" for event in events)
        self.status["surge_persisted"] += surges
        self.status["surge_pending"] = max(0, self.status["surge_pending"] - surges)
        if surges:
            self.status["surge_last_error"] = None
        self.status["price_candidate_persisted"] += candidates
        self.status["price_candidate_pending"] = max(
            0, self.status["price_candidate_pending"] - candidates
        )
        if candidates:
            self.status["price_candidate_last_error"] = None
        self.status["last_flush"] = int(time.time() * 1000)
        previous_database_error = self.status["database_error"]
        if (previous_database_error is not None
                and self.status["last_error"] ==
                f"写入失败，批次保留并退避重试：{previous_database_error}"):
            # A successful replay clears only the database error it caused.
            # Keep a connection error that was recorded while the batch was
            # waiting, so recovery does not hide an independent outage.
            connection_errors = self.status["connection_errors"]
            replacement = next(reversed(connection_errors.values()), None)
            self.status["last_error"] = replacement
            self.status["last_error_at"] = (
                int(time.time() * 1000) if replacement is not None else None
            )
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
                except Exception as exc:
                    self.status["dropped"] += len(remaining)
                    self.record_error(f"关闭时仍有 {len(remaining)} 条消息未能落盘：{exc}")
                finally:
                    # queue.get() removes an item even when persistence fails;
                    # acknowledge each removal so queue.join() cannot hang.
                    for _ in remaining:
                        self.queue.task_done()
            self.status["buffer_length"] = 0

    async def publish_status(self) -> None:
        # writer 的隔离告警也会主动发布，与定时状态写入串行，防止侧车 tmp 竞争。
        async with self._status_lock:
            await self._publish_status()

    async def _publish_status(self) -> None:
        corrupt = await asyncio.to_thread(self.spool.corrupt_files)
        dead_letters = await asyncio.to_thread(self.spool.dead_letters)
        write_failures = await asyncio.to_thread(self.spool.write_failures)
        disk_capacity = await asyncio.to_thread(_disk_capacity, self.config.spool_dir, self.config)
        self.status["corrupt_batches"] = len(corrupt)
        self.status["dead_letter_batches"] = len(dead_letters)
        self.status["write_failure_batches"] = len(write_failures)
        self.status["oldest_write_failure_ms"] = (
            min(record["first_failure_ms"] for record in write_failures)
            if write_failures else None
        )
        if corrupt:
            self.status["storage_warning"] = "暂存批次损坏已隔离保留，部分行情可能缺失，需要人工检查 .corrupt 文件"
            self.status["data_gap_possible"] = True
        self.status["corrupt_files"] = [path.name for path in corrupt[:100]]
        self.status["corrupt_file_list_truncated"] = len(corrupt) > 100
        snapshot = dict(self.status)
        snapshot["orderflow_maturity"] = {
            "enabled": self.maturity_worker is not None,
            **(self.maturity_worker.status if self.maturity_worker is not None else {}),
        }
        snapshot.update(disk_capacity)
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
        self.status.update(disk_capacity)
        try:
            await asyncio.to_thread(self.store.put_document, self.config.status_document_key, snapshot)
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
                    self.connection_recovered(name)
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
                error = f"{name} 连接失败：{exc}"
                self.status["connection_errors"][name] = error
                self.record_error(error)
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
        status = None
        connections: list[asyncio.Task[Any]] = []
        stopped = None
        try:
            stopped = asyncio.create_task(self.stop.wait(), name="market-stop")
            seed_task = asyncio.create_task(self.seed_surge_detector(), name="market-seed")
            done, _ = await asyncio.wait((seed_task, stopped), return_when=asyncio.FIRST_COMPLETED)
            if stopped in done:
                seed_task.cancel()
                await asyncio.gather(seed_task, return_exceptions=True)
                return
            await seed_task
            if self.maturity_worker is not None:
                self.maturity_worker.start()
            status = asyncio.create_task(self.status_loop(), name="market-status")
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=15), trust_env=True) as session:
                connections = [asyncio.create_task(self.connection(session, name, url), name=f"market-{name}")
                               for name, url in self.config.connections()]
                done, _ = await asyncio.wait([stopped, writer, status], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task is not stopped:
                        task.result()
        finally:
            self.stop.set()
            for task in connections:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*connections, return_exceptions=True)
            if stopped is not None and not stopped.done():
                stopped.cancel()
                await asyncio.gather(stopped, return_exceptions=True)
            workers = [task for task in (writer, status) if task is not None]
            if workers:
                await asyncio.gather(
                    asyncio.wait_for(asyncio.gather(*workers, return_exceptions=True),
                                     timeout=self.config.shutdown_timeout),
                    return_exceptions=True,
                )
            if self.maturity_worker is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(self.maturity_worker.close),
                        timeout=self.config.shutdown_timeout,
                    )
                except asyncio.TimeoutError:
                    self.status["state"] = "storage_error"
                    self.record_error("订单流成熟度 worker 关闭超时")
            if self.status["state"] != "storage_error":
                self.status["state"] = "stopped"
            try:
                await self.publish_status()
            except OSError as exc:
                self.status["state"] = "storage_error"
                self.record_error(f"最终状态写入失败：{exc}")


def parse_config(argv: list[str] | None = None) -> StreamConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=("futures", "spot"), default=os.getenv("QUANT_STREAM_MARKET", "futures"))
    parser.add_argument("--pairs", default=os.getenv("QUANT_STREAM_PAIRS", "BTC/USDT,ETH/USDT"), help="逗号分隔交易对")
    parser.add_argument("--trade-stream", choices=("aggTrade", "trade"),
                        default=os.getenv("QUANT_STREAM_TRADE_STREAM") or None,
                        help="成交流类型；默认合约 aggTrade、现货 trade")
    parser.add_argument("--kline-intervals",
                        default=os.getenv("QUANT_STREAM_KLINE_INTERVALS", "5m"),
                        help="逗号分隔 Binance K 线周期，默认 5m")
    parser.add_argument("--gap-threshold-ms", type=int,
                        default=int(os.getenv("QUANT_STREAM_GAP_THRESHOLD_MS", "60000")),
                        help="记录行情时间缺口的阈值（毫秒），默认 60000")
    parser.add_argument("--spool-dir", type=Path, default=Path(os.getenv("QUANT_STREAM_SPOOL_DIR", str(ROOT / "database/runtime/market_spool"))))
    parser.add_argument("--status-document-key", default="market_stream_status.json")
    parser.add_argument("--disk-warning-pct", type=float, default=float(os.getenv("QUANT_STREAM_DISK_WARNING_PCT", "15")),
                        help="磁盘剩余百分比低于此值时告警")
    parser.add_argument("--disk-critical-pct", type=float, default=float(os.getenv("QUANT_STREAM_DISK_CRITICAL_PCT", "5")),
                        help="磁盘剩余百分比低于此值时进入临界告警")
    parser.add_argument("--disk-warning-bytes", type=int, default=int(os.getenv("QUANT_STREAM_DISK_WARNING_BYTES", "0")),
                        help="磁盘剩余字节低于此值时告警，0 表示不启用")
    parser.add_argument("--disk-critical-bytes", type=int, default=int(os.getenv("QUANT_STREAM_DISK_CRITICAL_BYTES", "0")),
                        help="磁盘剩余字节低于此值时进入临界告警，0 表示不启用")
    arguments = parser.parse_args(argv)
    pairs = tuple(dict.fromkeys(normalize_pair(pair, arguments.market) for pair in arguments.pairs.split(",") if pair.strip()))
    kline_intervals = tuple(dict.fromkeys(
        interval.strip() for interval in arguments.kline_intervals.split(",") if interval.strip()
    ))
    return StreamConfig(pairs=pairs, market=arguments.market, spool_dir=arguments.spool_dir,
                        status_document_key=arguments.status_document_key,
                        trade_stream=arguments.trade_stream,
                        kline_intervals=kline_intervals,
                        gap_threshold_ms=arguments.gap_threshold_ms,
                        enabled=os.getenv("QUANT_STREAM_ENABLED", "true").lower() not in {"false", "0", "no"},
                        disk_warning_pct=arguments.disk_warning_pct,
                        disk_critical_pct=arguments.disk_critical_pct,
                        disk_warning_bytes=arguments.disk_warning_bytes,
                        disk_critical_bytes=arguments.disk_critical_bytes)


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
    collector = None
    try:
        collector = MarketCollector(config, store,
                                    maturity_store_factory=lambda: DataStore(max_connections=1))
        asyncio.run(collector.run())
    except CollectorAlreadyRunning as exc:
        LOG.error("%s", exc)
        return 2
    except OSError as exc:
        LOG.error("行情采集存储不可用：%s", exc)
        return 1
    finally:
        store.close()
    return 1 if collector is not None and collector.status["state"] == "storage_error" else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Collect public Binance Futures execution evidence at execution resolution.

The collector is deliberately independent from the Demo account and strategy
runner.  It records one-second mark updates, partial depth20 snapshots and
aggregate trades for BTCUSDT/ETHUSDT/TRXUSDT, with one websocket per symbol so
a reconnect on one symbol cannot create a shared three-symbol blind spot.
Raw events are append-only JSONL; status is written atomically for operators.
This process never loads credentials and never submits an order.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import signal
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SYMBOLS = ("BTCUSDT", "ETHUSDT", "TRXUSDT")
MARKET_STREAM_BASE = "wss://fstream.binance.com/market/stream?streams="
PUBLIC_STREAM_BASE = "wss://fstream.binance.com/public/stream?streams="
SCHEMA_VERSION = "account-risk-execution-stream-v1"


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported value: {type(value).__name__}")


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(_canonical(value), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _event_timestamp(payload: dict[str, Any]) -> int | None:
    for key in ("T", "E", "time"):
        value = payload.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def normalize_event(symbol: str, stream: str, payload: dict[str, Any], received_ms: int) -> dict[str, Any] | None:
    event_type = payload.get("e")
    # For mark-price updates Binance's ``T`` field is the next funding time,
    # not the timestamp of this observation.  Use the event time ``E`` so the
    # 1-second continuity audit cannot see an artificial multi-hour gap.
    timestamp = (
        payload.get("E")
        if event_type == "markPriceUpdate"
        else _event_timestamp(payload)
    )
    if not isinstance(timestamp, int) or timestamp < 0:
        timestamp = None
    if timestamp is None:
        return None
    if event_type == "markPriceUpdate":
        price = payload.get("p")
        if not isinstance(price, str):
            return None
        return {
            "kind": "mark",
            "symbol": symbol,
            "timestamp_ms": timestamp,
            "event_time_ms": payload.get("E"),
            "mark_price": price,
            "index_price": payload.get("i"),
            "funding_rate": payload.get("r"),
            "next_funding_time_ms": payload.get("T"),
            "stream": stream,
            "received_ms": received_ms,
        }
    if event_type == "aggTrade":
        price = payload.get("p")
        quantity = payload.get("q")
        trade_id = payload.get("a")
        if not all(isinstance(item, (str, int)) for item in (price, quantity, trade_id)):
            return None
        return {
            "kind": "trade",
            "symbol": symbol,
            "timestamp_ms": timestamp,
            "event_time_ms": payload.get("E"),
            "trade_id": str(trade_id),
            "price": str(price),
            "quantity": str(quantity),
            "buyer_maker": payload.get("m"),
            "stream": stream,
            "received_ms": received_ms,
        }
    if "@depth20" in stream:
        # Futures partial-depth updates use the compact ``b``/``a`` keys;
        # accept the verbose names as well for fixtures and alternate feeds.
        bids = payload.get("b", payload.get("bids"))
        asks = payload.get("a", payload.get("asks"))
        update_id = payload.get("u")
        if not isinstance(bids, list) or not isinstance(asks, list) or update_id is None:
            return None
        return {
            "kind": "book",
            "symbol": symbol,
            "timestamp_ms": timestamp,
            "event_time_ms": payload.get("E"),
            "transaction_time_ms": payload.get("T"),
            "update_id": str(update_id),
            "bids": [[str(level[0]), str(level[1])] for level in bids[:20]
                     if isinstance(level, (list, tuple)) and len(level) == 2],
            "asks": [[str(level[0]), str(level[1])] for level in asks[:20]
                     if isinstance(level, (list, tuple)) and len(level) == 2],
            "stream": stream,
            "received_ms": received_ms,
        }
    return None


class StreamCollector:
    def __init__(self, output: Path, *, reconnect_max_seconds: float = 30.0) -> None:
        self.output = output
        self.reconnect_max_seconds = reconnect_max_seconds
        self.stop = asyncio.Event()
        self.status: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "state": "starting",
            "environment": "binance_public_futures",
            "symbols": list(SYMBOLS),
            "transport": "websocket",
            "streams_per_symbol": ["markPrice@1s", "depth20@100ms", "aggTrade"],
            "started_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "updated_at_utc": None,
            "events_by_symbol": {symbol: {"mark": 0, "book": 0, "trade": 0} for symbol in SYMBOLS},
            "last_event_ms": {symbol: {} for symbol in SYMBOLS},
            "reconnects": {symbol: 0 for symbol in SYMBOLS},
            "connection_state": {
                symbol: {"market": "connecting", "orderbook": "connecting"}
                for symbol in SYMBOLS
            },
            "connection_errors": {symbol: {} for symbol in SYMBOLS},
            "max_gap_ms": {symbol: {"mark": 0, "book": 0, "trade": 0} for symbol in SYMBOLS},
            "data_gap_possible": False,
            "raw_event_count": 0,
            "raw_event_sha256": None,
        }
        self._hash = hashlib.sha256()
        self._lock = asyncio.Lock()
        self._handles: dict[str, Any] = {}

    def _url(self, symbol: str, channel: str = "market") -> str:
        lower = symbol.lower()
        if channel == "market":
            streams = (f"{lower}@markPrice@1s", f"{lower}@aggTrade")
            return MARKET_STREAM_BASE + "/".join(streams)
        if channel == "orderbook":
            return PUBLIC_STREAM_BASE + f"{lower}@depth20@100ms"
        raise ValueError("unknown execution stream channel")

    def _event_path(self, symbol: str) -> Path:
        return self.output / f"{symbol}.jsonl"

    async def _write_event(self, event: dict[str, Any]) -> None:
        symbol = event["symbol"]
        line = json.dumps(event, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n"
        path = self._event_path(symbol)
        def append() -> None:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)
        await asyncio.to_thread(append)
        self._hash.update(line.encode("utf-8"))
        self.status["raw_event_count"] += 1
        self.status["events_by_symbol"][symbol][event["kind"]] += 1
        previous = self.status["last_event_ms"][symbol].get(event["kind"])
        current = event["timestamp_ms"]
        if isinstance(previous, int) and current > previous:
            gap = current - previous
            self.status["max_gap_ms"][symbol][event["kind"]] = max(
                self.status["max_gap_ms"][symbol][event["kind"]], gap,
            )
            if event["kind"] in {"mark", "book"} and gap > 1_000:
                self.status["data_gap_possible"] = True
        self.status["last_event_ms"][symbol][event["kind"]] = current
        self.status["raw_event_sha256"] = self._hash.hexdigest()

    async def _publish_status(self) -> None:
        async with self._lock:
            snapshot = dict(self.status)
            snapshot["updated_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            snapshot["status_sha256"] = canonical_sha256(snapshot)
            path = self.output / "status.json"
            temporary = path.with_suffix(".tmp")
            await asyncio.to_thread(
                temporary.write_text,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                "utf-8",
            )
            await asyncio.to_thread(temporary.replace, path)

    async def _status_loop(self) -> None:
        while not self.stop.is_set():
            await self._publish_status()
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    async def _connection(self, symbol: str, channel: str) -> None:
        import aiohttp
        failures = 0
        while not self.stop.is_set():
            self.status["connection_state"][symbol][channel] = "connecting"
            try:
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
                async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                    async with session.ws_connect(self._url(symbol, channel), heartbeat=20,
                                                  receive_timeout=45, max_msg_size=2 ** 20) as socket:
                        self.status["connection_state"][symbol][channel] = "connected"
                        failures = 0
                        async for message in socket:
                            if self.stop.is_set():
                                break
                            if message.type == aiohttp.WSMsgType.TEXT:
                                wrapper = json.loads(message.data)
                                payload = wrapper.get("data", wrapper)
                                stream = str(wrapper.get("stream", ""))
                                if not isinstance(payload, dict):
                                    continue
                                event = normalize_event(symbol, stream, payload,
                                                        int(time.time() * 1000))
                                if event is not None:
                                    await self._write_event(event)
                            elif message.type == aiohttp.WSMsgType.ERROR:
                                raise ConnectionError(str(socket.exception()))
                        if not self.stop.is_set():
                            raise ConnectionError("websocket closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                self.status["connection_state"][symbol][channel] = "disconnected"
                self.status["reconnects"][symbol] += 1
                self.status["connection_errors"][symbol][channel] = type(exc).__name__
                self.status["data_gap_possible"] = True
                delay = min(self.reconnect_max_seconds, 2 ** min(failures - 1, 5))
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        self.status["connection_state"][symbol][channel] = "stopped"

    async def run(self) -> None:
        self.output.mkdir(parents=True, exist_ok=True)
        self.status["state"] = "running"
        tasks = [
            asyncio.create_task(
                self._connection(symbol, channel),
                name=f"risk-stream-{symbol}-{channel}",
            )
            for symbol in SYMBOLS
            for channel in ("market", "orderbook")
        ]
        status = asyncio.create_task(self._status_loop(), name="risk-stream-status")
        try:
            await self.stop.wait()
        finally:
            for task in tasks + [status]:
                task.cancel()
            await asyncio.gather(*tasks, status, return_exceptions=True)
            self.status["state"] = "stopped"
            await self._publish_status()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "database/runtime/account-risk-execution-stream")
    args = parser.parse_args(argv)
    collector = StreamCollector(args.output)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, collector.stop.set)
    try:
        loop.run_until_complete(collector.run())
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Authenticated, shared Binance kline streams for charts, independent of database writes.

Importing this module does not read authentication secrets or open a database.  The
CLI wires in the existing auth service only after argument parsing.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import inspect
import json
import logging
import math
import os
import re
import time
from typing import Any, Callable

import aiohttp
from aiohttp import web

LOG = logging.getLogger("market_live")
DEFAULT_INTERVALS = frozenset({
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1M",
})
TIMEFRAME_UNITS_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000,
                      "M": 2_592_000_000}
LOCAL_ADDRESSES = {"127.0.0.1", "::1"}
MAX_UPSTREAM_STREAMS = 1024
SERVICE_KEY = web.AppKey("market_live", object)


@dataclass(frozen=True, order=True)
class Subscription:
    pair: str
    timeframe: str
    market: str

    @property
    def stream(self) -> str:
        symbol = self.pair.split(":", 1)[0].replace("/", "").lower()
        return f"{symbol}@kline_{self.timeframe}"


def parse_subscriptions(
    values: Any, intervals: frozenset[str], maximum: int = 100,
) -> frozenset[Subscription]:
    """Require the same CCXT identities used by REST; a colon selects USDT futures."""
    if not isinstance(values, list):
        raise ValueError("subscriptions must be an array")
    result: set[Subscription] = set()
    streams: dict[tuple[str, str], str] = {}
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("each subscription must contain pair and timeframe")
        pair, timeframe = value.get("pair"), value.get("timeframe")
        if not isinstance(pair, str) or not isinstance(timeframe, str):
            raise ValueError("pair and timeframe must be strings")
        pair = pair.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{1,24}/[A-Z0-9]{1,12}(?::[A-Z0-9]{1,12})?", pair):
            raise ValueError("pair must use BASE/QUOTE[:SETTLEMENT] format")
        market = "futures" if ":" in pair else "spot"
        if market == "futures" and not pair.endswith("/USDT:USDT"):
            raise ValueError("only USDT-settled futures are supported")
        if timeframe not in intervals:
            raise ValueError("unsupported timeframe")
        item = Subscription(pair, timeframe, market)
        identity = (market, item.stream)
        if identity in streams and streams[identity] != pair:
            raise ValueError("ambiguous exchange symbol")
        streams[identity] = pair
        result.add(item)
        if len(result) > maximum:
            raise ValueError(f"at most {maximum} subscriptions are allowed")
    return frozenset(result)


def upstream_url(market: str, subscriptions: frozenset[Subscription]) -> str:
    streams = "/".join(sorted(item.stream for item in subscriptions))
    if market == "futures":
        # Current Binance USD-M kline streams belong to the market partition.
        return f"wss://fstream.binance.com/market/stream?streams={streams}"
    if market == "spot":
        return f"wss://stream.binance.com:9443/stream?streams={streams}"
    raise ValueError("unsupported market")


def timeframe_milliseconds(timeframe: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([mhdwM])", timeframe)
    if match is None:
        raise ValueError("unsupported timeframe")
    return int(match.group(1)) * TIMEFRAME_UNITS_MS[match.group(2)]


def previous_candle_timestamp(timestamp: int, timeframe: str) -> int:
    """Monthly Binance candles use UTC calendar boundaries, not a 30-day duration."""
    if timeframe == "1M":
        current = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
        year, month = (current.year, current.month - 1) if current.month > 1 else (
            current.year - 1, 12,
        )
        previous = current.replace(year=year, month=month, day=1,
                                   hour=0, minute=0, second=0, microsecond=0)
        return int(previous.timestamp() * 1000)
    return timestamp - timeframe_milliseconds(timeframe)


def _positive_integer(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid timestamp")
    number = int(value)
    if number <= 0 or str(number) != str(value):
        raise ValueError("invalid timestamp")
    return number


def _number(value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid candle number")
    try:
        decimal = Decimal(str(value))
        number = float(decimal)
    except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
        raise ValueError("invalid candle number") from exc
    if not decimal.is_finite() or not math.isfinite(number) or number < 0:
        raise ValueError("invalid candle number")
    if positive and number <= 0:
        raise ValueError("invalid candle price")
    return number


def normalize_kline(
    message: Any, market: str, subscriptions: frozenset[Subscription], received_ms: int,
) -> tuple[Subscription, dict[str, Any]] | None:
    """Accept authoritative exchange OHLCV only, using k.t as candle identity."""
    if not isinstance(message, dict):
        raise ValueError("invalid exchange message")
    payload = message.get("data", message)
    if not isinstance(payload, dict):
        raise ValueError("invalid exchange payload")
    if payload.get("e") != "kline":
        return None
    raw = payload.get("k")
    if not isinstance(raw, dict):
        raise ValueError("invalid exchange candle")
    symbol, timeframe = payload.get("s"), raw.get("i")
    if not isinstance(symbol, str) or not isinstance(timeframe, str):
        raise ValueError("invalid exchange candle identity")
    stream = f"{symbol.lower()}@kline_{timeframe}"
    if "stream" in message and message["stream"] != stream:
        raise ValueError("exchange stream identity mismatch")
    item = next((s for s in subscriptions if s.market == market and s.stream == stream), None)
    if item is None:
        return None
    if raw.get("s", symbol) != symbol or not isinstance(raw.get("x"), bool):
        raise ValueError("invalid exchange candle identity or close flag")
    candle = {
        "timestamp": _positive_integer(raw.get("t")),
        "open": _number(raw.get("o"), positive=True),
        "high": _number(raw.get("h"), positive=True),
        "low": _number(raw.get("l"), positive=True),
        "close": _number(raw.get("c"), positive=True),
        "volume": _number(raw.get("v")),
        "closed": raw["x"],
    }
    if candle["low"] > min(candle["open"], candle["close"]) or candle["high"] < max(
        candle["open"], candle["close"], candle["low"],
    ):
        raise ValueError("invalid exchange candle range")
    return item, {
        "type": "kline", "pair": item.pair, "timeframe": item.timeframe,
        "candle": candle, "exchange_event_timestamp": _positive_integer(payload.get("E")),
        "received_timestamp": received_ms,
    }


@dataclass
class PendingMessage:
    payload: dict[str, Any]
    received_monotonic: float | None = None


@dataclass(eq=False)
class Client:
    ws: web.WebSocketResponse
    token: str
    subscriptions: frozenset[Subscription] = frozenset()
    pending: dict[tuple[Any, ...], PendingMessage] = field(default_factory=dict)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    overflow: bool = False
    sent: int = 0
    merged: int = 0
    generation: int = 0

    def offer(self, payload: dict[str, Any], received_monotonic: float | None = None) -> None:
        if self.ws.closed or self.overflow:
            return
        if payload["type"] == "kline":
            prefix = ("kline", payload["pair"], payload["timeframe"])
            key = (*prefix, payload["candle"]["timestamp"])
            # Keep the just-closed candle alongside the new current candle.  A
            # client falling further behind reconnects and reloads REST history.
            if key not in self.pending and sum(k[:3] == prefix for k in self.pending) >= 2:
                self.overflow = True
                self.changed.set()
                return
        elif payload["type"] == "status":
            key = ("status", payload["market"])
        else:
            key = (payload["type"],)
        if key in self.pending:
            self.merged += 1
        self.pending[key] = PendingMessage(payload, received_monotonic)
        self.changed.set()


@dataclass
class MarketState:
    market: str
    subscriptions: frozenset[Subscription] = frozenset()
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    state: str = "idle"
    connected: bool = False
    reconnects: int = 0
    received: int = 0
    rejected: int = 0
    last_received_timestamp: int | None = None
    last_error: str | None = None
    task: asyncio.Task | None = None
    ws: Any = None


def _distribution(values: deque[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"samples": 0, "p50": None, "p95": None}
    return {
        "samples": len(ordered),
        "p50": round(ordered[(len(ordered) - 1) // 2], 3),
        "p95": round(ordered[min(len(ordered) - 1, math.ceil(len(ordered) * .95) - 1)], 3),
    }


class MarketLiveService:
    def __init__(
        self, *, verifier: Callable[[str], Any], allowed_origins: set[str] | frozenset[str],
        intervals: frozenset[str] = DEFAULT_INTERVALS,
        upstream_connector: Callable[[str], Any] | None = None, archive: Any = None,
        auth_timeout: float = 10, auth_check_interval: float = 1, flush_interval: float = .025,
        send_timeout: float = 3, retry_initial: float = 1, retry_max: float = 30,
        max_subscriptions: int = 100,
    ):
        if min(auth_timeout, auth_check_interval, flush_interval, send_timeout, retry_initial,
               retry_max) <= 0 or max_subscriptions < 1:
            raise ValueError("timeouts and subscription limit must be positive")
        self.verifier, self.allowed_origins = verifier, frozenset(allowed_origins)
        self.intervals = frozenset(intervals)
        self.upstream_connector, self.archive = upstream_connector, archive
        self.auth_timeout, self.auth_check_interval = auth_timeout, auth_check_interval
        self.flush_interval, self.send_timeout = flush_interval, send_timeout
        self.retry_initial, self.retry_max = retry_initial, retry_max
        self.max_subscriptions = max_subscriptions
        self.clients: set[Client] = set()
        self.markets = {market: MarketState(market) for market in ("futures", "spot")}
        self.latest: dict[Subscription, dict[str, Any]] = {}
        self.versions: dict[Subscription, dict[int, dict[str, Any]]] = {}
        self.receive_latency: deque[float] = deque(maxlen=4096)
        self.fanout_latency: deque[float] = deque(maxlen=4096)
        self.exchange_clock_ahead_samples = 0
        self.sent = 0
        self.slow_clients = 0
        self.archive_rejected = 0
        self.session: aiohttp.ClientSession | None = None
        self.stopping = False

    async def valid_token(self, token: str) -> bool:
        try:
            result = self.verifier(token)
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except Exception:
            # Never log verifier exceptions: they can contain a token or secret.
            return False

    async def start(self, _: web.Application) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(
            total=None, sock_connect=8,
        ), trust_env=True)
        for state in self.markets.values():
            state.task = asyncio.create_task(self._market_worker(state), name=f"klines-{state.market}")

    async def stop(self, _: web.Application) -> None:
        self.stopping = True
        workers = [state.task for state in self.markets.values() if state.task]
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await asyncio.gather(*(
            self._close(client, 1001, "service stopping") for client in tuple(self.clients)
        ), return_exceptions=True)
        if self.session:
            await self.session.close()
        if self.archive is not None:
            try:
                flushed = await asyncio.wait_for(asyncio.to_thread(self.archive.close, 4), timeout=5)
                if flushed is False:
                    LOG.warning("Candle archive stopped with uncommitted pending rows")
            except Exception:
                LOG.warning("Candle archive did not finish within shutdown allowance")

    async def _close(self, client: Client, code: int, reason: str) -> None:
        with suppress(Exception):
            await asyncio.wait_for(client.ws.close(code=code, message=reason.encode()), self.send_timeout)

    def _refresh_subscriptions(self) -> None:
        desired = set().union(*(client.subscriptions for client in self.clients)) if self.clients else set()
        for market, state in self.markets.items():
            subscriptions = frozenset(item for item in desired if item.market == market)
            if subscriptions != state.subscriptions:
                state.subscriptions = subscriptions
                state.changed.set()
        self.latest = {key: value for key, value in self.latest.items() if key in desired}
        self.versions = {key: value for key, value in self.versions.items() if key in desired}

    def _replace_subscriptions(self, client: Client, subscriptions: frozenset[Subscription]) -> None:
        desired = set(subscriptions)
        for other in self.clients:
            if other is not client:
                desired.update(other.subscriptions)
        for market in self.markets:
            identities: dict[str, str] = {}
            for item in desired:
                if item.market != market:
                    continue
                if item.stream in identities and identities[item.stream] != item.pair:
                    raise ValueError("ambiguous exchange symbol across clients")
                identities[item.stream] = item.pair
            if len(identities) > MAX_UPSTREAM_STREAMS:
                raise ValueError("shared upstream subscription capacity reached")
        if client.subscriptions != subscriptions:
            client.generation += 1
        client.subscriptions = subscriptions
        identities = {(item.pair, item.timeframe) for item in subscriptions}
        markets = {item.market for item in subscriptions}
        client.pending = {
            key: value for key, value in client.pending.items()
            if (key[0] == "kline" and key[1:3] in identities)
            or (key[0] == "status" and key[1] in markets)
        }
        self._refresh_subscriptions()
        for market in sorted(markets):
            state = self.markets[market]
            client.offer({
                "type": "status", "state": state.state if state.state != "idle" else "connecting",
                "market": market,
            })

    async def websocket(self, request: web.Request) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if (origin is not None and origin not in self.allowed_origins) or (
            origin is None and request.remote not in LOCAL_ADDRESSES
        ):
            raise web.HTTPForbidden(text="Origin not allowed")
        if request.query:
            raise web.HTTPBadRequest(text="Use the first WebSocket message for authentication")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024, timeout=self.send_timeout)
        await ws.prepare(request)
        client = Client(ws, "")
        sender: asyncio.Task | None = None
        auth_checker: asyncio.Task | None = None
        try:
            try:
                first = await asyncio.wait_for(ws.receive(), timeout=self.auth_timeout)
                auth = json.loads(first.data) if first.type == aiohttp.WSMsgType.TEXT else None
            except (asyncio.TimeoutError, ValueError, TypeError):
                await self._close(client, 1008, "authentication required")
                return ws
            if not isinstance(auth, dict) or auth.get("type") != "auth" or not isinstance(
                auth.get("token"), str,
            ) or len(auth["token"]) > 4096 or not await self.valid_token(auth["token"]):
                await self._close(client, 1008, "authentication failed")
                return ws
            client.token = auth["token"]
            await asyncio.wait_for(ws.send_json({"type": "ready"}), self.send_timeout)
            self.clients.add(client)
            sender = asyncio.create_task(self._send_client(client), name="market-fanout")
            auth_checker = asyncio.create_task(self._check_auth(client), name="market-auth-check")
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    await self._close(client, 1008, "JSON text messages required")
                    break
                if not await self.valid_token(client.token):
                    await self._close(client, 1008, "authentication expired")
                    break
                try:
                    payload = json.loads(message.data)
                    if not isinstance(payload, dict) or payload.get("type") != "subscribe":
                        raise ValueError("expected subscribe message")
                    subscriptions = parse_subscriptions(
                        payload.get("subscriptions"), self.intervals, self.max_subscriptions,
                    )
                    self._replace_subscriptions(client, subscriptions)
                except (ValueError, TypeError) as exc:
                    # Our own validation errors contain no received token or raw input.
                    text = str(exc) if isinstance(exc, ValueError) and not isinstance(
                        exc, json.JSONDecodeError,
                    ) else "invalid JSON message"
                    client.offer({"type": "error", "code": "invalid_subscription", "message": text})
                    continue
        except (asyncio.TimeoutError, ConnectionError, RuntimeError):
            await self._close(client, 1013, "client unavailable")
        finally:
            # aiohttp may cancel a disconnected request during the await below.
            # Release shared subscriptions synchronously before task cleanup.
            self.clients.discard(client)
            client.token = ""
            client.subscriptions = frozenset()
            client.pending.clear()
            self._refresh_subscriptions()
            for task in (sender, auth_checker):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(t for t in (sender, auth_checker) if t), return_exceptions=True)
        return ws

    async def _check_auth(self, client: Client) -> None:
        while not client.ws.closed:
            await asyncio.sleep(self.auth_check_interval)
            if not await self.valid_token(client.token):
                await self._close(client, 1008, "authentication expired")
                return

    async def _send_client(self, client: Client) -> None:
        try:
            while not client.ws.closed:
                await client.changed.wait()
                await asyncio.sleep(self.flush_interval)
                client.changed.clear()
                if client.overflow:
                    self.slow_clients += 1
                    await self._close(client, 1013, "client behind; reload candle history")
                    return
                # A full FIFO snapshot prevents busy symbols from starving quiet ones.
                pending, client.pending = client.pending, {}
                generation = client.generation
                for key, item in pending.items():
                    if generation != client.generation:
                        break
                    if key[0] == "kline" and not any(
                        (s.pair, s.timeframe) == key[1:3] for s in client.subscriptions
                    ):
                        continue
                    if key[0] == "status" and not any(
                        s.market == key[1] for s in client.subscriptions
                    ):
                        continue
                    if not await self.valid_token(client.token):
                        await self._close(client, 1008, "authentication expired")
                        return
                    payload = dict(item.payload)
                    if payload["type"] == "kline":
                        payload["sent_timestamp"] = int(time.time() * 1000)
                    await asyncio.wait_for(client.ws.send_json(payload), self.send_timeout)
                    client.sent += 1
                    self.sent += 1
                    if item.received_monotonic is not None:
                        self.fanout_latency.append((time.monotonic() - item.received_monotonic) * 1000)
        except (asyncio.TimeoutError, ConnectionError, RuntimeError):
            self.slow_clients += 1
            await self._close(client, 1013, "client send timeout")

    def _set_state(self, state: MarketState, value: str) -> None:
        state.state = value
        state.connected = value == "connected"
        payload = {"type": "status", "state": value, "market": state.market}
        for client in self.clients:
            if any(item.market == state.market for item in client.subscriptions):
                client.offer(payload)

    async def _market_worker(self, state: MarketState) -> None:
        retry = 0
        consume: asyncio.Task | None = None
        change: asyncio.Task | None = None
        try:
            while not self.stopping:
                state.changed.clear()
                subscriptions = state.subscriptions
                if not subscriptions:
                    state.state, state.connected = "idle", False
                    await state.changed.wait()
                    retry = 0
                    continue
                self._set_state(state, "connecting" if retry == 0 else "reconnecting")
                consume = asyncio.create_task(self._consume(state, subscriptions))
                received_before = state.received
                change = asyncio.create_task(state.changed.wait())
                done, _ = await asyncio.wait((consume, change), return_when=asyncio.FIRST_COMPLETED)
                if change in done:
                    consume.cancel()
                    await asyncio.gather(consume, return_exceptions=True)
                    retry = 0
                    continue
                change.cancel()
                await asyncio.gather(change, return_exceptions=True)
                try:
                    await consume
                    state.last_error = "upstream disconnected"
                except Exception as exc:
                    # Exception strings can embed the complete connection URL.
                    state.last_error = type(exc).__name__
                state.reconnects += 1
                if state.received > received_before:
                    retry = 0
                self._set_state(state, "reconnecting")
                delay = min(self.retry_max, self.retry_initial * 2 ** min(retry, 10))
                retry += 1
                try:
                    await asyncio.wait_for(state.changed.wait(), timeout=delay)
                    retry = 0
                except asyncio.TimeoutError:
                    pass
        finally:
            for task in (consume, change):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (consume, change) if t), return_exceptions=True)
            state.connected = False

    async def _consume(self, state: MarketState, subscriptions: frozenset[Subscription]) -> None:
        assert self.session is not None
        url = upstream_url(state.market, subscriptions)
        connector = self.upstream_connector or (lambda value: self.session.ws_connect(
            value, heartbeat=20, receive_timeout=45, max_msg_size=1024 * 1024,
        ))
        try:
            async with connector(url) as ws:
                state.ws = ws
                self._set_state(state, "connected")
                async for message in ws:
                    received_ms, received_monotonic = int(time.time() * 1000), time.monotonic()
                    if message.type == aiohttp.WSMsgType.ERROR:
                        raise ConnectionError("upstream failed")
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        normalized = normalize_kline(
                            json.loads(message.data), state.market, subscriptions, received_ms,
                        )
                        if normalized is None:
                            continue
                        item, payload = normalized
                    except (ValueError, TypeError, KeyError, OverflowError):
                        state.rejected += 1
                        continue
                    if item not in state.subscriptions:
                        continue
                    self.accept_event(state, item, payload, received_monotonic)
        finally:
            state.ws = None
            state.connected = False

    def accept_event(
        self, state: MarketState, item: Subscription, payload: dict[str, Any], received_monotonic: float,
    ) -> bool:
        candle, event_ms = payload["candle"], payload["exchange_event_timestamp"]
        timestamp = candle["timestamp"]
        latest = self.latest.get(item)
        versions = self.versions.setdefault(item, {})
        previous = versions.get(timestamp)
        # A previously observed candle may finish just after the next one opens.
        # Validate its own event version, without rolling back the current candle.
        if latest is not None and timestamp < latest["candle"]["timestamp"]:
            # At most the immediately preceding period can be a valid late
            # close.  This bounds replayed/stale data after a long disconnect.
            if timestamp < previous_candle_timestamp(latest["candle"]["timestamp"], item.timeframe):
                state.rejected += 1
                return False
        if previous is not None:
            old = previous["candle"]
            if event_ms < previous["exchange_event_timestamp"]:
                state.rejected += 1
                return False
            if (
                old["closed"] or (
                    event_ms == previous["exchange_event_timestamp"] and not candle["closed"]
                )
            ):
                state.rejected += 1
                return False
        # Binance can deliver the closing update for the previous candle after
        # the first update of the next candle.  The per-candle version check
        # above still rejects stale duplicates; an unseen older candle is
        # valid history and must be archived/fanned out without moving latest.
        versions[timestamp] = payload
        if len(versions) > 2:
            del versions[min(versions)]
        if latest is None or timestamp >= latest["candle"]["timestamp"]:
            self.latest[item] = payload
        state.received += 1
        state.last_received_timestamp = payload["received_timestamp"]
        delay = payload["received_timestamp"] - event_ms
        self.receive_latency.append(delay)
        if delay < 0:
            self.exchange_clock_ahead_samples += 1
        for client in self.clients:
            if item in client.subscriptions:
                client.offer(payload, received_monotonic)
        if self.archive is not None:
            row = {**candle, "extras": {
                "closed": candle["closed"], "live_event_timestamp": event_ms,
                "received_timestamp": payload["received_timestamp"],
            }}
            try:
                if not self.archive.offer(
                    "binance", state.market, item.pair, item.timeframe, state.market, [row],
                    source_key="live:binance-ws-klines",
                ):
                    self.archive_rejected += 1
            except Exception:
                self.archive_rejected += 1
        return True

    async def health(self, _: web.Request) -> web.Response:
        markets = {
            name: {
                "state": state.state, "connected": state.connected,
                "subscriptions": len(state.subscriptions), "reconnects": state.reconnects,
                "received": state.received, "rejected": state.rejected,
                "last_received_timestamp": state.last_received_timestamp,
                "last_error": state.last_error,
            } for name, state in self.markets.items()
        }
        archive_status = None
        if self.archive is not None:
            try:
                archive_status = self.archive.status
                if callable(archive_status):
                    archive_status = archive_status()
                if inspect.isawaitable(archive_status):
                    archive_status = await archive_status
                if not isinstance(archive_status, dict):
                    raise TypeError("archive status must be a mapping")
            except Exception:
                archive_status = {"error": "archive status unavailable"}
        return web.json_response({
            "service": "market_live", "running": not self.stopping,
            "clients": len(self.clients), "markets": markets,
            "exchange_to_receive_latency_ms": {**_distribution(self.receive_latency),
                "clock_skew_possible": self.exchange_clock_ahead_samples > 0,
                "exchange_clock_ahead_samples": self.exchange_clock_ahead_samples,
                "signed": True,
            },
            "receive_to_send_latency_ms": _distribution(self.fanout_latency),
            "fanout": {
                "sent": self.sent, "pending": sum(len(c.pending) for c in self.clients),
                "merged": sum(c.merged for c in self.clients), "slow_clients": self.slow_clients,
                "maximum_pending_per_subscription": 2,
            },
            "archive": archive_status, "archive_rejected": self.archive_rejected,
        })


def create_app(**options: Any) -> web.Application:
    service = MarketLiveService(**options)
    app = web.Application()
    app[SERVICE_KEY] = service
    app.router.add_get("/ws/market", service.websocket)
    app.router.add_get("/health", service.health)
    app.on_startup.append(service.start)
    # Close upgraded sockets before aiohttp waits for active handlers to finish.
    app.on_shutdown.append(service.stop)
    return app


def main() -> None:
    from runtime_config import load_environment
    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default=os.environ.get("QUANT_LIVE_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=os.environ.get("QUANT_LIVE_PORT", "8892"))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    # These imports intentionally happen here, never during unit-test imports.
    import auth_service
    from candle_archive import CandleArchiveWriter
    archive = CandleArchiveWriter(auth_service.get_store)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = create_app(
        verifier=auth_service.verify_token, allowed_origins=auth_service.ALLOWED_ORIGINS,
        intervals=frozenset(auth_service.BINANCE_INTERVALS), archive=archive,
    )
    # An access token is forbidden in the URL; also keep arbitrary queries out of logs.
    web.run_app(app, host=args.bind, port=args.port, access_log=None, shutdown_timeout=5)


if __name__ == "__main__":
    main()

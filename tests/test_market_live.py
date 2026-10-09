"""Real local WebSocket integration tests; no exchange, authentication files or database."""

import asyncio
from contextlib import suppress
import copy
from datetime import datetime, timezone
import json
import time
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from market_live import (
    Client, DEFAULT_INTERVALS, SERVICE_KEY, Subscription, create_app, normalize_kline,
    parse_subscriptions, timeframe_milliseconds, upstream_url,
)


FUTURE = {"pair": "BTC/USDT:USDT", "timeframe": "1m"}
SPOT = {"pair": "BTC/USDT", "timeframe": "1m"}


def kline(*, symbol="BTCUSDT", timeframe="1m", event=None, timestamp=None,
          close="11", closed=False):
    now = int(time.time() * 1000)
    timestamp = timestamp or now // 60000 * 60000
    return {
        "stream": f"{symbol.lower()}@kline_{timeframe}",
        "data": {
            "e": "kline", "E": event or now, "s": symbol,
            "k": {"t": timestamp, "s": symbol, "i": timeframe, "o": "10", "h": "20",
                  "l": "8", "c": close, "v": "123.12345678", "x": closed},
        },
    }


async def eventually(check, timeout=2):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(.005)


async def receive_type(ws, kind, timeout=2, *, predicate=lambda _: True):
    async with asyncio.timeout(timeout):
        while True:
            message = await ws.receive()
            if message.type != aiohttp.WSMsgType.TEXT:
                raise AssertionError(f"expected {kind}, got {message.type}: {message.data}")
            payload = json.loads(message.data)
            if payload.get("type") == kind and predicate(payload):
                return payload


class Archive:
    def __init__(self):
        self.offers = []
        self.accept = True
        self.closed = False

    def offer(self, *args, **kwargs):
        self.offers.append((copy.deepcopy(args), dict(kwargs)))
        return self.accept

    @property
    def status(self):
        return {"pending_rows": len(self.offers), "closed": self.closed}

    def close(self, timeout=4):
        self.closed = True
        return True


class ProtocolTests(unittest.TestCase):
    def test_canonical_pair_timeframe_validation_deduplication_and_limit(self):
        values = [FUTURE, {"pair": " btc/usdt:usdt ", "timeframe": "1m"}, SPOT]
        subscriptions = parse_subscriptions(values, DEFAULT_INTERVALS)
        self.assertEqual(len(subscriptions), 2)
        self.assertEqual({s.market for s in subscriptions}, {"futures", "spot"})
        for values in (
            None, {}, [None], [{"pair": 1, "timeframe": "1m"}],
            [{"pair": "BTCUSDT", "timeframe": "1m"}],
            [{"pair": "BTC/USDC:USDC", "timeframe": "1m"}],
            [{"pair": "BTC/USDT:BTC", "timeframe": "1m"}],
            [{"pair": "../../USDT", "timeframe": "1m"}],
            [{"pair": "BTC/USDT", "timeframe": "1s"}],
            [{"pair": "AB/CD", "timeframe": "1m"}, {"pair": "A/BCD", "timeframe": "1m"}],
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                parse_subscriptions(values, DEFAULT_INTERVALS)
        with self.assertRaises(ValueError):
            parse_subscriptions([
                {"pair": f"C{i}/USDT:USDT", "timeframe": "1m"} for i in range(101)
            ], DEFAULT_INTERVALS)
        # The limit applies after deduplication.
        self.assertEqual(len(parse_subscriptions([FUTURE] * 101, DEFAULT_INTERVALS)), 1)

    def test_official_market_partitioned_kline_endpoints(self):
        futures = parse_subscriptions([FUTURE], DEFAULT_INTERVALS)
        spot = parse_subscriptions([SPOT], DEFAULT_INTERVALS)
        self.assertEqual(upstream_url("futures", futures),
                         "wss://fstream.binance.com/market/stream?streams=btcusdt@kline_1m")
        self.assertEqual(upstream_url("spot", spot),
                         "wss://stream.binance.com:9443/stream?streams=btcusdt@kline_1m")

    def test_timeframe_milliseconds(self):
        self.assertEqual(timeframe_milliseconds("1m"), 60_000)
        self.assertEqual(timeframe_milliseconds("2h"), 7_200_000)
        self.assertEqual(timeframe_milliseconds("1M"), 2_592_000_000)

    def test_authoritative_kline_validation_and_identity(self):
        subscriptions = parse_subscriptions([FUTURE], DEFAULT_INTERVALS)
        item, payload = normalize_kline(kline(), "futures", subscriptions, 123)
        self.assertEqual(item.pair, FUTURE["pair"])
        self.assertEqual(payload["candle"]["volume"], 123.12345678)
        self.assertNotIn("sent_timestamp", payload)
        self.assertIsNone(normalize_kline(kline(symbol="ETHUSDT"), "futures", subscriptions, 123))
        for field, value in (("c", "NaN"), ("v", "-1"), ("o", True), ("h", "9"),
                             ("x", "false"), ("t", 0), ("t", True)):
            message = kline()
            message["data"]["k"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                normalize_kline(message, "futures", subscriptions, 123)
        message = kline()
        message["stream"] = "ethusdt@kline_1m"
        with self.assertRaises(ValueError):
            normalize_kline(message, "futures", subscriptions, 123)


class LocalWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth_valid = True
        self.archive = Archive()
        self.upstreams = {"futures": [], "spot": []}
        self.calls = []
        upstream_app = web.Application()

        async def upstream(request):
            market = request.match_info["market"]
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            self.upstreams[market].append(ws)
            async for _ in ws:
                pass
            return ws

        upstream_app.router.add_get("/{market}", upstream)
        self.upstream_server = TestServer(upstream_app)
        await self.upstream_server.start_server()
        self.http = aiohttp.ClientSession()

        def connector(url):
            self.calls.append(url)
            market = "futures" if "fstream" in url else "spot"
            return self.http.ws_connect(self.upstream_server.make_url(f"/{market}"))

        self.app = create_app(
            verifier=lambda token: token == "test-token" and self.auth_valid,
            allowed_origins={"http://127.0.0.1:8888"},
            upstream_connector=connector, archive=self.archive,
            # Keep the timeout branch covered without making the suite depend on
            # sub-100ms event-loop scheduling under the full test load.
            auth_timeout=.2, auth_check_interval=.02, flush_interval=.02,
            send_timeout=.1, retry_initial=.03, retry_max=.06,
        )
        self.service = self.app[SERVICE_KEY]
        self.server = TestServer(self.app)
        await self.server.start_server()
        self.sockets = []

    async def asyncTearDown(self):
        await asyncio.gather(*(ws.close() for ws in self.sockets), return_exceptions=True)
        await self.server.close()
        await self.http.close()
        await self.upstream_server.close()

    async def connect(self, *, token="test-token", auth=True):
        ws = await self.http.ws_connect(self.server.make_url("/ws/market"))
        self.sockets.append(ws)
        if auth:
            await ws.send_json({"type": "auth", "token": token})
            if token == "test-token":
                self.assertEqual(await ws.receive_json(), {"type": "ready"})
        return ws

    async def subscribe(self, ws, subscriptions, *, market="futures"):
        await ws.send_json({"type": "subscribe", "subscriptions": subscriptions})
        if subscriptions:
            await eventually(lambda: self.upstreams[market] and not self.upstreams[market][-1].closed)
            await receive_type(ws, "status", predicate=lambda p: p["market"] == market and
                               p["state"] == "connected")

    async def publish(self, message, *, market="futures"):
        await self.upstreams[market][-1].send_json(message)

    async def health(self):
        async with self.http.get(self.server.make_url("/health")) as response:
            return await response.json()

    async def test_auth_origin_timeout_token_query_and_expiration(self):
        for path, headers, status in (
            ("/ws/market", {"Origin": "https://untrusted.example"}, 403),
            ("/ws/market?token=test-token", {}, 400),
        ):
            with self.subTest(path=path), self.assertRaises(aiohttp.WSServerHandshakeError) as error:
                await self.http.ws_connect(self.server.make_url(path), headers=headers)
            self.assertEqual(error.exception.status, status)
        invalid = await self.connect(token="bad-secret-token")
        self.assertEqual((await invalid.receive()).type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(invalid.close_code, 1008)
        timed_out = await self.connect(auth=False)
        self.assertEqual((await timed_out.receive()).type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(timed_out.close_code, 1008)
        unauthorized = await self.connect(auth=False)
        await unauthorized.send_json({"type": "subscribe", "subscriptions": [FUTURE]})
        self.assertEqual((await unauthorized.receive()).type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(unauthorized.close_code, 1008)
        self.assertEqual(self.calls, [])
        valid = await self.connect()
        await self.subscribe(valid, [FUTURE])
        self.auth_valid = False
        self.assertEqual((await valid.receive()).type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(valid.close_code, 1008)
        await eventually(lambda: not self.service.clients)
        await eventually(lambda: self.upstreams["futures"][-1].closed)
        health = await self.health()
        self.assertNotIn("token", json.dumps(health))
        self.assertEqual(health["markets"]["futures"]["subscriptions"], 0)

    async def test_invalid_subscription_preserves_existing_subscription(self):
        ws = await self.connect()
        await self.subscribe(ws, [FUTURE])
        for values in (
            [{"pair": "BTC/USDC:USDC", "timeframe": "1m"}],
            [{"pair": "BTC/USDT", "timeframe": "1s"}],
            [{"pair": f"C{i}/USDT:USDT", "timeframe": "1m"} for i in range(101)],
        ):
            await ws.send_json({"type": "subscribe", "subscriptions": values})
            error = await receive_type(ws, "error")
            self.assertEqual(error["code"], "invalid_subscription")
            self.assertEqual(len(self.service.markets["futures"].subscriptions), 1)
        await ws.send_str('{"broken":')
        self.assertEqual((await receive_type(ws, "error"))["message"], "invalid JSON message")
        await self.publish(kline())
        self.assertEqual((await receive_type(ws, "kline"))["pair"], FUTURE["pair"])
        self.assertEqual(len(self.calls), 1)

    async def test_dynamic_subscriptions_shared_between_clients_and_separate_markets(self):
        first, second = await self.connect(), await self.connect()
        await self.subscribe(first, [FUTURE, FUTURE])
        await self.subscribe(second, [FUTURE])
        self.assertEqual(len(self.calls), 1)
        await first.send_json({"type": "subscribe", "subscriptions": []})
        await eventually(lambda: any(not c.subscriptions for c in self.service.clients))
        self.assertEqual(len(self.calls), 1)
        await second.send_json({"type": "subscribe", "subscriptions": [
            FUTURE, {"pair": "ETH/USDT:USDT", "timeframe": "5m"},
        ]})
        await eventually(lambda: len(self.calls) == 2)
        await eventually(lambda: len(self.upstreams["futures"]) == 2)
        self.assertIn("ethusdt@kline_5m", self.calls[-1])
        self.assertEqual(self.calls[-1].count("btcusdt@kline_1m"), 1)
        await self.subscribe(first, [SPOT], market="spot")
        self.assertEqual(len(self.calls), 3)
        await self.publish(kline(), market="spot")
        spot_message = await receive_type(first, "kline")
        self.assertEqual(spot_message["pair"], SPOT["pair"])
        await second.close()
        await eventually(lambda: not self.service.markets["futures"].subscriptions)
        await eventually(lambda: self.upstreams["futures"][-1].closed)
        self.assertFalse(self.upstreams["spot"][-1].closed)
        await first.close()
        await eventually(lambda: not self.service.clients)
        await eventually(lambda: self.upstreams["spot"][-1].closed)

    async def test_cross_client_ambiguous_symbol_rejected(self):
        first, second = await self.connect(), await self.connect()
        await self.subscribe(first, [{"pair": "AB/CD", "timeframe": "1m"}], market="spot")
        await second.send_json({"type": "subscribe", "subscriptions": [
            {"pair": "A/BCD", "timeframe": "1m"},
        ]})
        error = await receive_type(second, "error")
        self.assertIn("ambiguous", error["message"])
        self.assertEqual(len(self.calls), 1)

    async def test_authoritative_fanout_ordering_closed_candles_and_archive(self):
        ws = await self.connect()
        await self.subscribe(ws, [FUTURE])
        start = kline(event=int(time.time() * 1000) + 10000)
        await self.publish(start)
        first = await receive_type(ws, "kline")
        self.assertEqual(set(first), {"type", "pair", "timeframe", "candle",
                                    "exchange_event_timestamp", "received_timestamp", "sent_timestamp"})
        self.assertEqual(first["candle"]["close"], 11)
        self.assertGreaterEqual(first["sent_timestamp"], first["received_timestamp"])
        duplicate = copy.deepcopy(start)
        duplicate["data"]["k"]["c"] = "12"
        await self.publish(duplicate)
        old = copy.deepcopy(start)
        old["data"]["E"] -= 1
        await self.publish(old)
        final = copy.deepcopy(start)
        final["data"]["E"] += 10
        final["data"]["k"].update(c="13", x=True)
        await self.publish(final)
        closed = await receive_type(ws, "kline")
        self.assertTrue(closed["candle"]["closed"])
        regression = copy.deepcopy(final)
        regression["data"]["E"] += 10
        regression["data"]["k"].update(c="14", x=False)
        await self.publish(regression)
        next_candle = copy.deepcopy(regression)
        next_candle["data"]["E"] += 10
        next_candle["data"]["k"]["t"] += 60000
        await self.publish(next_candle)
        current = await receive_type(ws, "kline")
        self.assertFalse(current["candle"]["closed"])
        self.assertGreater(current["candle"]["timestamp"], closed["candle"]["timestamp"])
        await eventually(lambda: self.service.markets["futures"].rejected == 3)
        self.assertEqual(self.service.markets["futures"].received, 3)
        self.assertEqual(len(self.archive.offers), 3)
        args, kwargs = self.archive.offers[-1]
        self.assertEqual(args[:5], ("binance", "futures", FUTURE["pair"], "1m", "futures"))
        self.assertEqual(args[5][0]["extras"]["closed"], False)
        self.assertEqual(kwargs["source_key"], "live:binance-ws-klines")
        health = await self.health()
        self.assertEqual(health["archive"]["pending_rows"], 3)
        self.assertTrue(health["exchange_to_receive_latency_ms"]["clock_skew_possible"])
        self.assertLess(health["exchange_to_receive_latency_ms"]["p50"], 0)
        self.assertGreater(health["receive_to_send_latency_ms"]["samples"], 0)

    async def test_late_final_previous_candle_updates_history_without_current_regression(self):
        ws = await self.connect()
        await self.subscribe(ws, [FUTURE])
        previous = kline()
        await self.publish(previous)
        await receive_type(ws, "kline")
        current = copy.deepcopy(previous)
        current["data"]["E"] += 20
        current["data"]["k"]["t"] += 60000
        current["data"]["k"]["c"] = "12"
        await self.publish(current)
        await receive_type(ws, "kline")
        final = copy.deepcopy(previous)
        final["data"]["E"] += 10
        final["data"]["k"].update(c="13", x=True)
        await self.publish(final)
        received = await receive_type(ws, "kline")
        self.assertTrue(received["candle"]["closed"])
        self.assertEqual(received["candle"]["timestamp"], previous["data"]["k"]["t"])
        subscription = next(iter(self.service.latest))
        self.assertEqual(self.service.latest[subscription]["candle"]["timestamp"],
                         current["data"]["k"]["t"])
        self.assertEqual(len(self.archive.offers), 3)

    async def test_unseen_late_previous_candle_is_archived_without_latest_regression(self):
        ws = await self.connect()
        await self.subscribe(ws, [FUTURE])
        previous = kline()
        current = copy.deepcopy(previous)
        current["data"]["E"] += 20
        current["data"]["k"].update(t=previous["data"]["k"]["t"] + 60000, c="12")
        late_close = copy.deepcopy(previous)
        late_close["data"]["E"] += 10
        late_close["data"]["k"].update(c="13", x=True)

        # The next candle is observed first, then the previous candle's close
        # arrives late after a reconnect or upstream reordering.
        await self.publish(current)
        await receive_type(ws, "kline")
        await self.publish(late_close)
        late = await receive_type(ws, "kline")

        self.assertTrue(late["candle"]["closed"])
        subscription = next(iter(self.service.latest))
        self.assertEqual(self.service.latest[subscription]["candle"]["timestamp"],
                         current["data"]["k"]["t"])
        self.assertEqual(self.service.markets["futures"].received, 2)
        self.assertEqual(self.service.markets["futures"].rejected, 0)
        self.assertEqual(len(self.archive.offers), 2)

    async def assert_monthly_late_close(self, previous_date, current_date):
        def timestamp(value):
            return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)

        ws = await self.connect()
        await self.subscribe(ws, [{"pair": FUTURE["pair"], "timeframe": "1M"}])
        previous = kline(timeframe="1M", timestamp=timestamp(previous_date))
        await self.publish(previous)
        await receive_type(ws, "kline")
        current = copy.deepcopy(previous)
        current["data"]["E"] += 20
        current["data"]["k"].update(t=timestamp(current_date), c="12")
        await self.publish(current)
        await receive_type(ws, "kline")
        final = copy.deepcopy(previous)
        final["data"]["E"] += 10
        final["data"]["k"].update(c="13", x=True)
        await self.publish(final)
        received = await receive_type(ws, "kline")
        self.assertTrue(received["candle"]["closed"])
        self.assertEqual(received["candle"]["timestamp"], timestamp(previous_date))
        subscription = next(iter(self.service.latest))
        self.assertEqual(self.service.latest[subscription]["candle"]["timestamp"],
                         timestamp(current_date))
        self.assertEqual(self.service.markets["futures"].rejected, 0)
        self.assertEqual(len(self.archive.offers), 3)

    async def test_monthly_late_close_after_31_day_month_is_delivered_and_archived(self):
        await self.assert_monthly_late_close("2026-01-01", "2026-02-01")

    async def test_monthly_late_close_after_28_day_month_is_delivered_and_archived(self):
        await self.assert_monthly_late_close("2026-02-01", "2026-03-01")

    async def test_monthly_candle_two_months_behind_is_rejected(self):
        def timestamp(value):
            return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)

        ws = await self.connect()
        await self.subscribe(ws, [{"pair": FUTURE["pair"], "timeframe": "1M"}])
        current = kline(timeframe="1M", timestamp=timestamp("2026-03-01"))
        await self.publish(current)
        await receive_type(ws, "kline")
        stale = copy.deepcopy(current)
        stale["data"]["E"] += 10
        stale["data"]["k"].update(t=timestamp("2026-01-01"), c="13", x=True)
        await self.publish(stale)
        await eventually(lambda: self.service.markets["futures"].rejected == 1)
        subscription = next(iter(self.service.latest))
        self.assertEqual(self.service.latest[subscription]["candle"]["timestamp"],
                         timestamp("2026-03-01"))
        self.assertEqual(self.service.markets["futures"].received, 1)
        self.assertEqual(len(self.archive.offers), 1)

    async def test_health_accepts_archive_status_method(self):
        class MethodStatusArchive:
            def status(self):
                return {"pending_rows": 7, "running": True}

            def close(self, timeout=4):
                return True

        self.service.archive = MethodStatusArchive()
        health = await self.health()
        self.assertEqual(health["archive"], {"pending_rows": 7, "running": True})

    async def test_close_then_next_open_both_survive_coalescing(self):
        ws = await self.connect()
        await self.subscribe(ws, [FUTURE])
        self.service.flush_interval = .06
        previous = kline(closed=True)
        current = copy.deepcopy(previous)
        current["data"]["E"] += 10
        current["data"]["k"].update(t=previous["data"]["k"]["t"] + 60000, x=False)
        await self.publish(previous)
        await self.publish(current)
        first, second = await receive_type(ws, "kline"), await receive_type(ws, "kline")
        self.assertTrue(first["candle"]["closed"])
        self.assertFalse(second["candle"]["closed"])

    async def test_slow_client_is_bounded_without_blocking_another_client(self):
        slow, fast = await self.connect(), await self.connect()
        await self.subscribe(slow, [FUTURE])
        await self.subscribe(fast, [FUTURE])
        await eventually(lambda: len(self.service.clients) == 2)
        slow_port = slow._response.connection.transport.get_extra_info("sockname")[1]
        slow_client = next(c for c in self.service.clients if
                           c.ws._req.transport.get_extra_info("peername")[1] == slow_port)
        blocked = asyncio.Event()
        send_entered = asyncio.Event()
        original = slow_client.ws.send_json

        async def blocked_send(payload, **kwargs):
            if payload["type"] == "kline":
                send_entered.set()
                await blocked.wait()
            await original(payload, **kwargs)

        slow_client.ws.send_json = blocked_send
        initial = kline()
        await self.publish(initial)
        await asyncio.wait_for(send_entered.wait(), 1)
        await receive_type(fast, "kline")
        for i in range(1, 41):
            message = copy.deepcopy(initial)
            message["data"]["E"] += i
            message["data"]["k"]["c"] = str(11 + i / 100)
            await self.publish(message)
        await eventually(lambda: self.service.markets["futures"].received == 41)
        self.assertLessEqual(len(slow_client.pending), 1)
        latest = await receive_type(fast, "kline", predicate=lambda p: p["candle"]["close"] == 11.4)
        self.assertEqual(latest["candle"]["close"], 11.4)
        self.assertGreater(slow_client.merged, 0)
        close = await slow.receive(timeout=1)
        self.assertEqual(close.type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(slow.close_code, 1013)
        blocked.set()
        await eventually(lambda: len(self.service.clients) == 1)
        self.assertGreaterEqual((await self.health())["fanout"]["slow_clients"], 1)

    async def test_falling_three_candles_behind_closes_for_history_reconciliation(self):
        ws = await self.connect()
        await self.subscribe(ws, [FUTURE])
        self.service.flush_interval = .1
        start = kline()
        for i in range(3):
            message = copy.deepcopy(start)
            message["data"]["E"] += i
            message["data"]["k"]["t"] += i * 60000
            await self.publish(message)
        self.assertEqual((await ws.receive(timeout=1)).type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(ws.close_code, 1013)
        self.assertEqual(self.service.slow_clients, 1)

    async def test_reconnect_reports_state_and_receives_real_exchange_data_after_gap(self):
        ws = await self.connect()
        await self.subscribe(ws, [FUTURE])
        original = kline()
        await self.publish(original)
        await receive_type(ws, "kline")
        await self.upstreams["futures"][-1].close()
        state = await receive_type(ws, "status", predicate=lambda p: p["state"] == "reconnecting")
        self.assertEqual(state["market"], "futures")
        await eventually(lambda: len(self.upstreams["futures"]) == 2)
        await receive_type(ws, "status", predicate=lambda p: p["state"] == "connected")
        self.assertEqual(self.service.markets["futures"].received, 1)
        updated = copy.deepcopy(original)
        updated["data"]["E"] += 10
        updated["data"]["k"]["c"] = "12"
        await self.publish(updated)
        self.assertEqual((await receive_type(ws, "kline"))["candle"]["close"], 12)
        self.assertEqual(self.service.markets["futures"].reconnects, 1)

    async def test_archive_rejection_is_visible_but_does_not_delay_fanout(self):
        ws = await self.connect()
        await self.subscribe(ws, [FUTURE])
        self.archive.accept = False
        await self.publish(kline())
        self.assertEqual((await receive_type(ws, "kline"))["candle"]["close"], 11)
        self.assertEqual((await self.health())["archive_rejected"], 1)

    async def test_subscription_switch_discards_pending_previous_candles(self):
        ws = await self.connect()
        await self.subscribe(ws, [FUTURE])
        self.service.flush_interval = .1
        await self.publish(kline())
        await eventually(lambda: self.service.markets["futures"].received == 1)
        await ws.send_json({"type": "subscribe", "subscriptions": [
            {"pair": "ETH/USDT:USDT", "timeframe": "1m"},
        ]})
        await eventually(lambda: len(self.upstreams["futures"]) == 2)
        await self.publish(kline(symbol="ETHUSDT"))
        message = await receive_type(ws, "kline")
        self.assertEqual(message["pair"], "ETH/USDT:USDT")

    async def test_busy_symbol_does_not_starve_quiet_symbol(self):
        ws = await self.connect()
        await self.subscribe(ws, [FUTURE, {"pair": "ETH/USDT:USDT", "timeframe": "1m"}])
        quiet = kline(symbol="ETHUSDT")
        await self.publish(quiet)
        busy = kline()
        for i in range(100):
            message = copy.deepcopy(busy)
            message["data"]["E"] += i
            await self.publish(message)
        received = await receive_type(ws, "kline")
        self.assertEqual(received["pair"], "ETH/USDT:USDT")
        self.assertEqual((await receive_type(ws, "kline"))["pair"], FUTURE["pair"])

    async def test_shutdown_closes_upstreams_clients_session_and_archive(self):
        ws = await self.connect()
        await self.subscribe(ws, [FUTURE])
        await self.server.close()
        with suppress(aiohttp.ClientConnectionError):
            message = await ws.receive(timeout=1)
            self.assertIn(message.type, (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED))
        self.assertTrue(self.service.session.closed)
        self.assertTrue(self.archive.closed)
        self.assertTrue(self.upstreams["futures"][-1].closed)
        self.assertTrue(all(state.task.done() for state in self.service.markets.values()))

    async def test_session_respects_environment_proxy_configuration(self):
        self.assertTrue(self.service.session.trust_env)

    async def test_shared_official_capacity_rejects_without_mutating_active_subscription(self):
        ws = await self.connect()
        await self.subscribe(ws, [FUTURE])
        with patch("market_live.MAX_UPSTREAM_STREAMS", 1):
            await ws.send_json({"type": "subscribe", "subscriptions": [
                FUTURE, {"pair": "ETH/USDT:USDT", "timeframe": "1m"},
            ]})
            self.assertIn("capacity", (await receive_type(ws, "error"))["message"])
        self.assertEqual(len(self.service.markets["futures"].subscriptions), 1)


class PendingTests(unittest.TestCase):
    def test_same_candle_merges_and_pending_is_removed_on_subscription_change(self):
        class Socket:
            closed = False
        client = Client(Socket(), "test-token")
        item = Subscription(FUTURE["pair"], "1m", "futures")
        client.subscriptions = frozenset({item})
        payload = normalize_kline(kline(), "futures", client.subscriptions, 123)[1]
        for i in range(500):
            update = copy.deepcopy(payload)
            update["candle"]["close"] = i
            client.offer(update, 1)
        self.assertEqual(len(client.pending), 1)
        self.assertEqual(client.merged, 499)
        self.assertEqual(next(iter(client.pending.values())).payload["candle"]["close"], 499)


if __name__ == "__main__":
    unittest.main()

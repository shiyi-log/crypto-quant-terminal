"""Offline Demo boundary, actual-fee extraction, and unknown-submit contracts."""

import copy
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import ccxt

from binance_demo_adapter import (
    ADAPTER_VERSION, BinanceDemoAdapter, DemoExchangeRejected, DemoRequestError, DemoSafetyError,
    DemoSubmissionUnknown, _build_client, assert_demo_url, normalize_fill,
    normalize_order, sanitize_response, stable_client_order_id, summarize_fill_fees,
)

BTC = "BTC/USDT:USDT"
ORDER_ID = stable_client_order_id("test", "candle-1", BTC, "buy")


def exchange_info():
    return {"symbols": [{
        "symbol": "BTCUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT",
        "marginAsset": "USDT", "status": "TRADING", "filters": [
            {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100",
             "stepSize": "0.001"},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "100",
             "stepSize": "0.001"},
            {"filterType": "PRICE_FILTER", "minPrice": "0.10", "maxPrice": "1000000",
             "tickSize": "0.10"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ],
    }]}


def order(**updates):
    return {"symbol": "BTCUSDT", "orderId": 123, "clientOrderId": ORDER_ID,
            "side": "BUY", "type": "LIMIT", "status": "PARTIALLY_FILLED",
            "origQty": "0.010", "executedQty": "0.002", "price": "60000.10",
            "avgPrice": "60000.10", "time": 1234, "updateTime": 1235, **updates}


def fill(**updates):
    return {"symbol": "BTCUSDT", "id": 999, "orderId": 123, "side": "BUY",
            "qty": "0.002", "price": "60000.10", "commission": "0.02400004",
            "commissionAsset": "USDT", "maker": True, "time": 1235,
            "realizedPnl": "0", **updates}


class FakeClient:
    id = "binance"
    isSandboxModeEnabled = False
    verbose = False

    def __init__(self):
        self.options = {"enableDemoTrading": True, "maxRetriesOnFailure": 0}
        self.urls = {"api": {"fapiPublic": "https://demo-fapi.binance.com/fapi/v1",
                             "fapiPrivate": "https://demo-fapi.binance.com/fapi/v1",
                             "fapiPrivateV3": "https://demo-fapi.binance.com/fapi/v3"}}
        self.calls = []
        self.configs = []
        self.responses = {}

    def request(self, path, api, method, params, *, config=None):
        self.calls.append((path, api, method, copy.deepcopy(params)))
        self.configs.append(copy.deepcopy(config))
        response = self.responses.get((path, method))
        if isinstance(response, Exception):
            raise response
        if response is not None:
            return copy.deepcopy(response)
        if path == "exchangeInfo":
            return exchange_info()
        if path == "order":
            return order(status="CANCELED" if method == "DELETE" else "PARTIALLY_FILLED")
        if path in {"allOrders", "userTrades", "positionRisk"}:
            return []
        return {}


class DemoAdapterTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.adapter = BinanceDemoAdapter(client=self.client, api_key="offline-key",
                                          api_secret="offline-secret", allow_trading=True,
                                          owned_client_ids=(ORDER_ID,))

    def submit(self, **kwargs):
        return self.adapter.submit_limit_order(BTC, "buy", "0.010", "60000.10", ORDER_ID,
                                               **kwargs)

    def test_stable_id_binds_signal_pair_side_and_role(self):
        self.assertEqual(ORDER_ID, stable_client_order_id("test", "candle-1", BTC, "buy"))
        self.assertNotEqual(ORDER_ID, stable_client_order_id("test", "candle-2", BTC, "buy"))
        self.assertNotEqual(ORDER_ID, stable_client_order_id("test", "candle-1", BTC, "sell"))
        self.assertNotEqual(ORDER_ID, stable_client_order_id("test", "candle-1", BTC, "buy", "exit"))
        self.assertEqual(len(ORDER_ID), 35)

    def test_only_explicit_demo_credentials_and_no_private_request_without_them(self):
        adapter = BinanceDemoAdapter.from_demo_env(
            env={"BINANCE_API_KEY": "do-not-read", "BINANCE_API_SECRET": "do-not-read"},
            client=self.client)
        with self.assertRaisesRegex(DemoSafetyError, "explicit Demo credentials"):
            adapter.fetch_balance()
        self.assertEqual(self.client.calls, [])
        adapter.fetch_exchange_info()  # Public endpoint is credential-free.
        self.assertEqual(len(self.client.calls), 1)

    def test_read_only_is_the_default(self):
        adapter = BinanceDemoAdapter(client=self.client, api_key="offline-key",
                                     api_secret="offline-secret")
        with self.assertRaisesRegex(DemoSafetyError, "read-only"):
            adapter.submit_limit_order(BTC, "buy", "0.01", "60000.1", ORDER_ID)
        with self.assertRaisesRegex(DemoSafetyError, "read-only"):
            adapter.cancel_order(BTC, client_order_id=ORDER_ID)
        self.assertEqual(self.client.calls, [])

    def test_demo_host_https_port_and_path_are_exact(self):
        assert_demo_url("https://demo-fapi.binance.com/fapi/v3/account?signature=private")
        for url in ["http://demo-fapi.binance.com/fapi/v1/order",
                    "https://fapi.binance.com/fapi/v1/order",
                    "https://testnet.binancefuture.com/fapi/v1/order",
                    "https://demo-fapi.binance.com.evil/fapi/v1/order",
                    "https://demo-fapi.binance.com:444/fapi/v1/order",
                    "https://user@demo-fapi.binance.com/fapi/v1/order",
                    "https://demo-api.binance.com/api/v3/order",
                    "https://demo-fapi.binance.com/fapi/v1/../order"]:
            with self.subTest(url=url), self.assertRaises(DemoSafetyError):
                assert_demo_url(url)

    def test_mutating_demo_or_sandbox_config_fails_before_requests(self):
        self.client.urls["api"]["fapiPrivate"] = "https://fapi.binance.com/fapi/v1"
        with self.assertRaises(DemoSafetyError):
            self.adapter.fetch_balance()
        self.assertEqual(self.client.calls, [])
        self.client.urls["api"]["fapiPrivate"] = "https://demo-fapi.binance.com/fapi/v1"
        self.client.isSandboxModeEnabled = True
        with self.assertRaises(DemoSafetyError):
            self.adapter.fetch_balance()
        self.assertEqual(self.client.calls, [])

    def test_api_version_must_match_endpoint_family(self):
        self.client.urls["api"]["fapiPrivateV3"] = "https://demo-fapi.binance.com/fapi/v1"
        with self.assertRaisesRegex(DemoSafetyError, "version"):
            self.adapter.fetch_balance()
        self.assertEqual(self.client.calls, [])

    def test_retry_and_verbose_config_fails_before_requests(self):
        self.client.options["maxRetriesOnFailure"] = 1
        with self.assertRaises(DemoSafetyError):
            self.adapter.fetch_balance()
        self.assertEqual(self.client.calls, [])

    def test_limit_order_uses_demo_perp_gtx_exact_decimals(self):
        result = self.submit()
        self.assertEqual(result["status"], "PARTIALLY_FILLED")
        self.assertEqual(result["filled_qty"], "0.002")
        self.assertEqual(result["raw"], order())
        _, api, method, params = self.client.calls[-1]
        self.assertEqual((api, method), ("fapiPrivate", "POST"))
        self.assertEqual(params, {"symbol": "BTCUSDT", "side": "BUY", "quantity": "0.010",
                                  "price": "60000.10", "type": "LIMIT", "timeInForce": "GTX",
                                  "newClientOrderId": ORDER_ID, "newOrderRespType": "RESULT",
                                  "reduceOnly": "false"})
        self.assertGreaterEqual(result["request_latency_ms"], 0)

    def test_off_grid_and_nonfinite_size_or_price_never_submit(self):
        for qty, price in [("0.0005", "60000.1"), ("0.0015", "60000.1"),
                           ("0.01", "60000.11"), ("nan", "60000.1"),
                           ("0.01", "Infinity"), ("0.001", "0.1")]:
            with self.subTest(qty=qty, price=price), self.assertRaises(DemoSafetyError):
                self.adapter.submit_limit_order(BTC, "buy", qty, price, ORDER_ID)
        self.assertFalse(any(call[2] == "POST" for call in self.client.calls))

    def test_prepare_rounds_quantity_down_and_resting_price_in_side_direction(self):
        buy = self.adapter.prepare_limit_order(BTC, "buy", "0.0109", "60000.19")
        sell = self.adapter.prepare_limit_order(BTC, "sell", "0.0109", "60000.11")
        self.assertEqual(buy, {"quantity": "0.010", "price": "60000.10"})
        self.assertEqual(sell, {"quantity": "0.010", "price": "60000.20"})
        with self.assertRaises(DemoSafetyError):
            self.adapter.prepare_limit_order(BTC, "buy", "0.0009", "60000.19")
        self.assertFalse(any(call[2] == "POST" for call in self.client.calls))

    def test_spot_and_other_symbols_are_rejected(self):
        for pair in ["BTC/USDT", "SOL/USDT:USDT", "BTC/USD:BTC"]:
            with self.subTest(pair=pair), self.assertRaises(DemoSafetyError):
                self.adapter.fetch_trades(pair)
        self.assertEqual(self.client.calls, [])

    def test_market_order_requires_reduce_only_and_no_account_settings(self):
        with self.assertRaises(DemoSafetyError):
            self.adapter.submit_market_order(BTC, "sell", "0.002", ORDER_ID, reduce_only=False)
        self.adapter.submit_market_order(BTC, "sell", "0.002", ORDER_ID)
        params = self.client.calls[-1][3]
        self.assertEqual(params["type"], "MARKET")
        self.assertEqual(params["reduceOnly"], "true")
        self.assertNotIn("price", params)
        self.assertFalse(any(call[0] in {"leverage", "marginType", "positionSide/dual"}
                             for call in self.client.calls))

    def test_timeout_then_order_not_found_never_allows_resubmit(self):
        self.client.responses[("order", "POST")] = ccxt.RequestTimeout(
            "secret signedUrl https://demo-fapi.binance.com?signature=private")
        with self.assertRaises(DemoSubmissionUnknown) as caught:
            self.submit()
        self.assertEqual(caught.exception.error_kind, "RequestTimeout")
        self.assertNotIn("private", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.client.responses[("order", "GET")] = ccxt.OrderNotFound("-2013")
        self.assertIsNone(self.adapter.fetch_order_by_client_id(BTC, ORDER_ID))
        with self.assertRaisesRegex(DemoSafetyError, "already attempted"):
            self.submit()
        self.assertEqual(sum(call[2] == "POST" for call in self.client.calls), 1)

    def test_successful_duplicate_does_not_create_second_order(self):
        self.submit()
        with self.assertRaisesRegex(DemoSafetyError, "already attempted"):
            self.submit()
        self.assertEqual(sum(call[2] == "POST" for call in self.client.calls), 1)

    def test_concurrent_validation_cannot_duplicate_one_client_id(self):
        gate = threading.Barrier(2)
        validate = self.adapter.validate_order

        def concurrent_validate(*args, **kwargs):
            gate.wait(timeout=5)
            return validate(*args, **kwargs)

        def attempt():
            try:
                self.submit()
                return "submitted"
            except DemoSafetyError:
                return "blocked"

        with patch.object(self.adapter, "validate_order", side_effect=concurrent_validate):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(sorted(results), ["blocked", "submitted"])
        self.assertEqual(sum(call[2] == "POST" for call in self.client.calls), 1)

    def test_definite_rejection_is_distinct_from_unknown_but_id_is_never_reused(self):
        self.client.responses[("order", "POST")] = ccxt.InvalidOrder(
            'Binance {"code":-5022,"msg":"GTX rejected"}')
        with self.assertRaises(DemoExchangeRejected) as caught:
            self.submit()
        self.assertEqual(caught.exception.error_kind, "exchange_rejected")
        self.assertEqual(caught.exception.exchange_error_kind, "InvalidOrder")
        self.assertEqual(caught.exception.exchange_code, -5022)
        with self.assertRaises(DemoSafetyError):
            self.submit()

    def test_duplicate_exchange_rejection_does_not_hide_existing_exposure(self):
        self.client.responses[("order", "POST")] = ccxt.InvalidOrder(
            'Binance {"code":-4116,"msg":"duplicated client order ID"}')
        with self.assertRaises(DemoSubmissionUnknown) as caught:
            self.submit()
        self.assertEqual(caught.exception.exchange_code, -4116)
        with self.assertRaises(DemoSafetyError):
            self.submit()

    def test_malformed_submit_response_is_unknown_and_locked(self):
        self.client.responses[("order", "POST")] = order(clientOrderId="another-client")
        with self.assertRaises(DemoSubmissionUnknown):
            self.submit()
        with self.assertRaises(DemoSafetyError):
            self.submit()
        self.assertEqual(sum(call[2] == "POST" for call in self.client.calls), 1)

    def test_lookup_network_failure_is_not_notfound(self):
        self.client.responses[("order", "GET")] = ccxt.NetworkError("sensitive payload")
        with self.assertRaises(DemoRequestError) as caught:
            self.adapter.fetch_order_by_client_id(BTC, ORDER_ID)
        self.assertEqual(caught.exception.error_kind, "NetworkError")
        self.assertNotIn("sensitive", str(caught.exception))

    def test_cancel_verifies_owned_client_and_order_id(self):
        with self.assertRaisesRegex(DemoSafetyError, "does not match"):
            self.adapter.cancel_order(BTC, "external-id", client_order_id=ORDER_ID)
        self.assertFalse(any(call[2] == "DELETE" for call in self.client.calls))
        result = self.adapter.cancel_order(BTC, "123", client_order_id=ORDER_ID)
        self.assertEqual(result["status"], "CANCELED")
        self.assertEqual(self.client.calls[-1][3]["origClientOrderId"], ORDER_ID)

    def test_terminal_or_absent_orders_are_not_canceled(self):
        self.client.responses[("order", "GET")] = order(status="FILLED")
        self.assertEqual(self.adapter.cancel_order(BTC, client_order_id=ORDER_ID)["status"], "FILLED")
        self.client.responses[("order", "GET")] = ccxt.OrderNotFound("not found")
        self.assertIsNone(self.adapter.cancel_order(BTC, client_order_id=ORDER_ID))
        self.assertFalse(any(call[2] == "DELETE" for call in self.client.calls))

    def test_foreign_well_formed_client_id_is_not_cancelable(self):
        foreign = stable_client_order_id("foreign", "candle-1", BTC, "buy")
        self.client.responses[("order", "GET")] = order(clientOrderId=foreign)
        with self.assertRaisesRegex(DemoSafetyError, "owned durable"):
            self.adapter.cancel_order(BTC, client_order_id=foreign)
        self.assertEqual(self.client.calls, [])

    def test_actual_fill_commission_role_and_original_binance_fields(self):
        normalized = normalize_fill(fill(), BTC)
        self.assertEqual(normalized["commission"], "0.02400004")
        self.assertIs(normalized["is_maker"], True)
        self.assertEqual(normalized["raw"], fill())
        fees = summarize_fill_fees([normalized, normalize_fill(fill(id=1000, maker=False), BTC)])
        self.assertEqual(fees["commission_usdt"], "0.04800008")
        self.assertIs(fees["commission_complete"], True)

    def test_mixed_asset_and_missing_fees_remain_unknown(self):
        mixed = [normalize_fill(fill(), BTC),
                 normalize_fill(fill(id=1000, commission="0.001", commissionAsset="BNB"), BTC)]
        fees = summarize_fill_fees(mixed)
        self.assertIsNone(fees["commission_usdt"])
        self.assertEqual(fees["commission_by_asset"]["BNB"], "0.001")
        missing = normalize_fill(fill(commission=None, maker="true"), BTC)
        self.assertIsNone(missing["commission"])
        self.assertIsNone(missing["is_maker"])
        self.assertFalse(summarize_fill_fees([missing])["commission_complete"])
        self.assertIsNone(summarize_fill_fees([])["commission_usdt"])

    def test_account_and_external_positions_are_read_only_and_visible(self):
        self.client.responses[("positionRisk", "GET")] = [
            {"symbol": "BTCUSDT", "positionAmt": "-0.002", "positionSide": "BOTH"},
            {"symbol": "SOLUSDT", "positionAmt": "5", "positionSide": "BOTH"},
        ]
        positions = self.adapter.fetch_positions()
        self.assertEqual(positions[0]["signed_qty"], "-0.002")
        self.assertFalse(positions[1]["allowed_pair"])
        self.assertEqual(positions[1]["symbol"], "SOLUSDT")
        self.assertEqual(positions[1]["pair"], "SOLUSDT")
        self.adapter.fetch_balance()
        self.assertTrue(all(call[2] == "GET" for call in self.client.calls))

    def test_unscoped_open_orders_include_foreign_orders_without_trading_them(self):
        self.client.responses[("openOrders", "GET")] = [order(), order(
            symbol="SOLUSDT", orderId=456, clientOrderId="external")]
        results = self.adapter.fetch_open_orders()
        self.assertEqual(self.client.calls[-1][3], {})
        self.assertEqual(len(results), 2)
        self.assertFalse(results[1]["allowed_pair"])
        self.assertEqual(results[1]["pair"], "SOLUSDT")
        self.assertEqual(results[1]["raw"]["clientOrderId"], "external")

    def test_trades_bounded_interval_and_order_id_are_exact(self):
        self.client.responses[("userTrades", "GET")] = [fill()]
        result = self.adapter.fetch_trades(BTC, since_ms=1000, end_ms=2000, order_id="123")
        self.assertEqual(result[0]["commission"], "0.02400004")
        self.assertEqual(self.client.calls[-1][3], {
            "symbol": "BTCUSDT", "limit": 1000, "startTime": 1000,
            "endTime": 2000, "orderId": "123"})
        for since, end in [(1000, 999), (0, 604800000)]:
            with self.assertRaises(DemoSafetyError):
                self.adapter.fetch_trades(BTC, since_ms=since, end_ms=end)

    def test_fill_cursor_preserves_exchange_ids_without_order_or_time_filter(self):
        self.client.responses[("userTrades", "GET")] = [
            fill(id=1000, orderId=456), fill(id=1001, orderId=123)]
        result = self.adapter.fetch_trades(BTC, from_id=1000)
        self.assertEqual(self.client.calls[-1][3], {
            "symbol": "BTCUSDT", "limit": 1000, "fromId": "1000"})
        self.assertEqual([item["fill_id"] for item in result], ["1000", "1001"])
        self.assertEqual([item["order_id"] for item in result], ["456", "123"])

    def test_direct_requests_use_reviewed_endpoint_rate_limit_costs(self):
        self.adapter.fetch_open_orders()
        self.assertEqual(self.client.configs[-1], {"cost": 1, "noSymbol": 40})
        self.assertEqual(self.adapter.last_call["rate_limit_cost"], 40)
        self.adapter.fetch_open_orders(BTC)
        self.assertEqual(self.adapter.last_call["rate_limit_cost"], 1)
        self.adapter.fetch_balance()
        self.assertEqual(self.client.configs[-1], {"cost": 5})
        self.assertEqual(self.adapter.last_call["rate_limit_cost"], 5)
        self.client.responses[("positionSide/dual", "GET")] = {"dualSidePosition": False}
        self.adapter.fetch_position_mode()
        self.assertEqual(self.client.configs[-1], {"cost": 30})

    def test_exchange_declared_rate_limits_are_preserved_without_inferred_defaults(self):
        raw = exchange_info()
        raw["rateLimits"] = [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE",
                              "intervalNum": 1, "limit": 2400}]
        self.client.responses[("exchangeInfo", "GET")] = raw
        self.adapter.fetch_exchange_info()
        self.assertEqual(self.adapter.declared_rate_limits, raw["rateLimits"])
        self.client.responses[("exchangeInfo", "GET")] = exchange_info()
        self.adapter.fetch_exchange_info()
        self.assertIsNone(self.adapter.declared_rate_limits)

    def test_cursor_conflicting_parameters_fail_before_request(self):
        for params in [{"since_ms": 1000}, {"end_ms": 2000}, {"order_id": "123"}]:
            with self.assertRaises(DemoSafetyError):
                self.adapter.fetch_trades(BTC, from_id=1000, **params)
        for cursor in [-1, True, "invalid", "²", "١٢٣", 12.0]:
            with self.assertRaises(DemoSafetyError):
                self.adapter.fetch_trades(BTC, from_id=cursor)
        self.assertEqual(self.client.calls, [])

    def test_flat_account_position_mode_is_explicit_and_read_only(self):
        self.client.responses[("positionSide/dual", "GET")] = {"dualSidePosition": True}
        result = self.adapter.fetch_position_mode()
        self.assertIs(result["hedged"], True)
        self.assertEqual(result["raw"], {"dualSidePosition": True})
        self.assertEqual(self.client.calls[-1][:3], ("positionSide/dual", "fapiPrivate", "GET"))

    def test_position_mode_unknown_is_not_assumed_one_way(self):
        for raw in [{}, {"dualSidePosition": "false"}, {"dualSidePosition": None}]:
            self.client.responses[("positionSide/dual", "GET")] = raw
            with self.assertRaises(DemoSafetyError):
                self.adapter.fetch_position_mode()

    def test_symbol_configuration_records_actual_leverage_and_margin_without_mutation(self):
        raw = {"symbol": "BTCUSDT", "leverage": 20, "marginType": "CROSSED",
               "isAutoAddMargin": "false", "maxNotionalValue": "1000000"}
        self.client.responses[("symbolConfig", "GET")] = [raw]
        result = self.adapter.fetch_symbol_config(BTC)
        self.assertEqual(result[0]["leverage"], 20)
        self.assertEqual(result[0]["margin_type"], "CROSSED")
        self.assertIs(result[0]["is_auto_add_margin"], False)
        self.assertEqual(result[0]["raw"], raw)
        self.assertEqual(self.client.calls[-1][:3], ("symbolConfig", "fapiPrivate", "GET"))
        self.assertEqual(self.client.calls[-1][3], {"symbol": "BTCUSDT"})

    def test_symbol_configuration_missing_or_wrong_identity_fails(self):
        for raw in [[], [{"symbol": "BTCUSDT", "marginType": "CROSSED"}],
                    [{"symbol": "ETHUSDT", "marginType": "CROSSED", "leverage": 20}]]:
            self.client.responses[("symbolConfig", "GET")] = raw
            with self.assertRaises(DemoSafetyError):
                self.adapter.fetch_symbol_config(BTC)

    def test_safe_raw_never_persists_authentication_material(self):
        result = sanitize_response({"orderId": "123", "apiKey": "secret-value",
                                    "raw": {"headers": {"X-MBX-APIKEY": "private"},
                                            "signature": "signed", "commission": "0.1"},
                                    "requestUrl": "private", "other": "?signature=private"})
        self.assertEqual(result, {"orderId": "123", "raw": {"commission": "0.1"},
                                  "other": "[redacted]"})
        self.assertEqual(sanitize_response({"X-MBX-APIKEY": "private", "raw": {
            "X-MBX-APIKEY": "private", "api_secret": "private", "orderId": "123"}}),
            {"raw": {"orderId": "123"}})

    def test_response_pair_identity_mismatch_fails(self):
        with self.assertRaises(DemoSafetyError):
            normalize_order(order(symbol="ETHUSDT"), BTC)
        with self.assertRaises(DemoSafetyError):
            normalize_fill(fill(symbol="ETHUSDT"), BTC)

    def test_page_parameters_do_not_allow_unbounded_or_ambiguous_requests(self):
        for limit in [0, 1001, True, 1.1]:
            with self.assertRaises(DemoSafetyError):
                self.adapter.fetch_trades(BTC, limit=limit)
        with self.assertRaises(DemoSafetyError):
            self.adapter.fetch_orders(BTC, since_ms=-1)
        self.assertEqual(self.client.calls, [])


class RealClientOfflineGuards(unittest.TestCase):
    def test_constructor_no_network_and_demo_switch_has_no_live_fallback(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("network")):
            client = _build_client("offline-key", "offline-secret")
        self.assertEqual(client.options["enableDemoTrading"], True)
        self.assertFalse(client.isSandboxModeEnabled)
        self.assertEqual(set(client.urls), {"api", "demo"})
        self.assertTrue(all("demo-fapi.binance.com/fapi" in value
                            for value in client.urls["api"].values()))
        self.assertFalse(client.session.trust_env)
        self.assertEqual(client.session.adapters["https://"].max_retries.total, 0)
        self.assertEqual(ADAPTER_VERSION, "binance-demo-usdm-v1")

    def test_sign_rejects_live_spot_and_account_config_paths(self):
        client = _build_client("offline-key", "offline-secret")
        for path, api, method in [("order", "private", "POST"),
                                  ("leverage", "fapiPrivate", "POST"),
                                  ("marginType", "fapiPrivate", "POST"),
                                  ("positionSide/dual", "fapiPrivate", "POST")]:
            with self.subTest(path=path), self.assertRaises(DemoSafetyError):
                client.sign(path, api, method, {})
        client.urls["api"]["fapiPrivate"] = "https://fapi.binance.com/fapi/v1"
        with self.assertRaises(DemoSafetyError):
            client.sign("order", "fapiPrivate", "POST", {"newClientOrderId": ORDER_ID})

    def test_unguarded_real_ccxt_client_cannot_be_injected(self):
        client = ccxt.binance()
        client.enable_demo_trading(True)
        with self.assertRaisesRegex(DemoSafetyError, "unguarded"):
            BinanceDemoAdapter(client=client, api_key="offline-key", api_secret="offline-secret")

    def test_endpoint_specific_retry_options_cannot_repeat_a_post(self):
        client = _build_client("offline-key", "offline-secret")
        client.options["order"] = {"maxRetriesOnFailure": 3}
        with patch.object(client, "fetch", side_effect=ccxt.RequestTimeout("offline")) as fetch:
            with self.assertRaises(ccxt.RequestTimeout):
                client.request("order", "fapiPrivate", "POST", {"newClientOrderId": ORDER_ID})
        self.assertEqual(fetch.call_count, 1)
        self.assertIsNone(client.last_request_headers)
        self.assertIsNone(client.last_request_url)

    def test_callers_cannot_override_reviewed_rate_limit_cost_to_zero(self):
        client = _build_client("offline-key", "offline-secret")
        with patch.object(client, "throttle") as throttle, patch.object(client, "fetch", return_value=[]):
            client.request("openOrders", "fapiPrivate", "GET", {}, config={"cost": 0})
        throttle.assert_called_once_with(40)

    def test_session_routing_mutation_fails_before_transport(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("network")):
            client = _build_client("offline-key", "offline-secret")
            client.session.trust_env = True
            with self.assertRaises(DemoSafetyError):
                client.fetch("https://demo-fapi.binance.com/fapi/v1/exchangeInfo")

    def test_redirects_are_not_followed_or_logged(self):
        class RedirectResponse:
            status_code = 302
        with patch("requests.sessions.Session.request", return_value=RedirectResponse()) as request:
            client = _build_client("offline-key", "offline-secret")
            with self.assertRaises(DemoSafetyError):
                client.fetch("https://demo-fapi.binance.com/fapi/v1/exchangeInfo")
        self.assertIs(request.call_args.kwargs["allow_redirects"], False)
        self.assertEqual(request.call_count, 1)
        self.assertIsNone(client.last_request_headers)


if __name__ == "__main__":
    unittest.main()

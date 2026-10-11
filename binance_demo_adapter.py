"""Fail-closed USDⓈ-M Binance Demo transport; no account configuration changes.

Amounts remain decimal strings.  Fees come from actual user-trades, never an
assumed rate.  An injected client is intended for offline tests; production
clients are always constructed as the guarded CCXT subclass below.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Mapping
from urllib.parse import urlsplit

ADAPTER_VERSION = "binance-demo-usdm-v1"
SUPPORTED_CCXT_VERSION = "4.5.85"
DEMO_HOST = "demo-fapi.binance.com"
ALLOWED_PAIRS = ("BTC/USDT:USDT", "ETH/USDT:USDT", "TRX/USDT:USDT")
PAIR_SYMBOLS = {pair: pair.split("/")[0] + "USDT" for pair in ALLOWED_PAIRS}
SYMBOL_PAIRS = {symbol: pair for pair, symbol in PAIR_SYMBOLS.items()}
_CLIENT_ID = re.compile(r"qd-[a-f0-9]{32}\Z")
_PRIVATE_GET = {
    ("fapiPrivate", "order"), ("fapiPrivate", "allOrders"),
    ("fapiPrivate", "openOrders"), ("fapiPrivate", "userTrades"),
    ("fapiPrivate", "positionSide/dual"), ("fapiPrivate", "symbolConfig"),
    ("fapiPrivateV3", "positionRisk"),
    ("fapiPrivateV3", "account"),
}
# Based on reviewed CCXT 4.5.85 endpoint costs, not an inferred account/IP
# quota. V3 account/positionRisk and unscoped premiumIndex conservatively use
# costs 5/10 (CCXT lists 1), because public official docs were unavailable.
REQUEST_COSTS = {
    ("fapiPublic", "GET", "exchangeInfo"): {"cost": 1},
    ("fapiPublic", "GET", "time"): {"cost": 1},
    ("fapiPublic", "GET", "premiumIndex"): {"cost": 1, "noSymbol": 10},
    ("fapiPrivate", "GET", "order"): {"cost": 1},
    ("fapiPrivate", "GET", "allOrders"): {"cost": 5},
    ("fapiPrivate", "GET", "openOrders"): {"cost": 1, "noSymbol": 40},
    ("fapiPrivate", "GET", "userTrades"): {"cost": 5},
    ("fapiPrivate", "GET", "positionSide/dual"): {"cost": 30},
    ("fapiPrivate", "GET", "symbolConfig"): {"cost": 5},
    ("fapiPrivateV3", "GET", "positionRisk"): {"cost": 5},
    ("fapiPrivateV3", "GET", "account"): {"cost": 5},
    ("fapiPrivate", "POST", "order"): {"cost": 4},
    ("fapiPrivate", "DELETE", "order"): {"cost": 1},
}
_SECRET_KEYS = {"apikey", "secret", "signature", "authorization", "headers",
                "requestheaders", "signedurl", "requesturl", "key", "password",
                "token", "accesskey", "apisecret", "xmbxapikey", "privatekey"}


class DemoSafetyError(ValueError):
    """No network mutation occurred because a local safety condition failed."""

    error_kind = "safety"
    latency_ms = 0


class DemoRequestError(RuntimeError):
    """Redacted request failure; never includes exchange exception messages."""

    def __init__(self, error_kind: str, latency_ms: float = 0,
                 exchange_code: int | None = None):
        self.error_kind = error_kind
        self.latency_ms = latency_ms
        self.exchange_code = exchange_code
        super().__init__(f"Binance Demo request failed ({error_kind})")


class DemoSubmissionUnknown(DemoRequestError):
    """Submission may have reached the exchange.  Reconcile, never resend."""


class DemoExchangeRejected(DemoRequestError):
    """A definite CCXT exchange rejection; its client ID still cannot be reused."""

    def __init__(self, exchange_error_kind: str, latency_ms: float = 0,
                 exchange_code: int | None = None):
        self.exchange_error_kind = exchange_error_kind
        self.exchange_error_type = exchange_error_kind
        super().__init__("exchange_rejected", latency_ms, exchange_code)


def stable_client_order_id(namespace: str, signal_id: str, pair: str, side: str,
                           role: str = "entry") -> str:
    """A durable runner must claim this ID before attempting submission."""
    _symbol(pair)
    if side not in {"buy", "sell"} or not namespace or not signal_id or not role:
        raise DemoSafetyError("invalid stable order identity")
    identity = "\x00".join((ADAPTER_VERSION, namespace, signal_id, pair, side, role))
    return "qd-" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def _symbol(pair: str) -> str:
    if pair not in PAIR_SYMBOLS:
        raise DemoSafetyError("only BTC/ETH/TRX USDT perpetual pairs are permitted")
    return PAIR_SYMBOLS[pair]


def _decimal(value: Any, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise DemoSafetyError("invalid decimal value") from None
    if not result.is_finite() or (positive and result <= 0):
        raise DemoSafetyError("invalid decimal value")
    return result


def _number(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return format(_decimal(value), "f")
    except DemoSafetyError:
        return None


def sanitize_response(value: Any) -> Any:
    """Preserve Binance payload fields, excluding authentication material."""
    if isinstance(value, Mapping):
        return {str(k): sanitize_response(v) for k, v in value.items()
                if re.sub(r"[^a-z]", "", str(k).lower()) not in _SECRET_KEYS}
    if isinstance(value, (list, tuple)):
        return [sanitize_response(item) for item in value]
    if isinstance(value, str) and ("signature=" in value.lower()
                                   or "x-mbx-apikey" in value.lower()):
        return "[redacted]"
    return value


def assert_demo_url(url: str) -> None:
    """Check the final URL without ever including it in an error/log."""
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname == DEMO_HOST
                 and parsed.port in (None, 443) and not parsed.username
                 and not parsed.password
                 and re.fullmatch(r"/fapi/v[123](?:/[A-Za-z0-9]+)*", parsed.path))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise DemoSafetyError("request endpoint is outside Binance USDⓈ-M Demo")


def _assert_api_base(api: str, url: str):
    assert_demo_url(url)
    version = "3" if api.endswith("V3") else "1"
    if url != f"https://{DEMO_HOST}/fapi/v{version}":
        raise DemoSafetyError("Demo API version or base path changed")


def _request_allowed(path: str, api: str, method: str) -> None:
    permitted = ((api == "fapiPublic" and method == "GET"
                  and path in {"exchangeInfo", "time", "premiumIndex"})
                 or (method == "GET" and (api, path) in _PRIVATE_GET)
                 or (api == "fapiPrivate" and path == "order"
                     and method in {"POST", "DELETE"}))
    if not permitted:
        raise DemoSafetyError("unsupported Demo endpoint or account mutation")


def _build_client(api_key: str | None, api_secret: str | None):
    import ccxt

    if ccxt.__version__ != SUPPORTED_CCXT_VERSION:
        raise DemoSafetyError("CCXT version requires an explicit Demo contract review")

    class GuardedBinance(ccxt.binance):
        _quant_demo_transport_guard = ADAPTER_VERSION

        def fetch2(self, path, api="public", method="GET", params=None,
                   headers=None, body=None, config=None):
            _request_allowed(path, api, method)
            # Parameters take precedence over endpoint-specific CCXT options.
            params = {**(params or {}), "maxRetriesOnFailure": 0,
                      "maxRetriesOnFailureDelay": 0}
            reviewed_cost = REQUEST_COSTS[(api, method, path)]
            return super().fetch2(path, api, method, params, headers, body, dict(reviewed_cost))

        def sign(self, path, api="public", method="GET", params=None,
                 headers=None, body=None):
            if self.isSandboxModeEnabled or not self.options.get("enableDemoTrading"):
                raise DemoSafetyError("Demo environment changed")
            _request_allowed(path, api, method)
            _assert_api_base(api, self.urls.get("api", {}).get(api, ""))
            signed = super().sign(path, api, method, params or {}, headers, body)
            assert_demo_url(signed["url"])
            return signed

        def fetch(self, url, method="GET", headers=None, body=None):
            assert_demo_url(url)
            if self.verbose or self.options.get("maxRetriesOnFailure", 0) != 0:
                raise DemoSafetyError("verbose logging and transport retries are prohibited")
            if not self.logger.disabled or not self.verify or not self.validateServerSsl:
                raise DemoSafetyError("Demo logging or TLS guard changed")
            return super().fetch(url, method, headers, body)

        def set_last_request(self, request):
            # CCXT normally retains signed URLs/headers on the client instance.
            self.last_request_headers = self.last_request_body = self.last_request_url = None

    logger = logging.Logger("quant.binance_demo.no_sensitive_logging")
    logger.disabled = True
    client = GuardedBinance({
        "apiKey": api_key or "", "secret": api_secret or "",
        "enableRateLimit": True, "timeout": 10000, "verbose": False,
        "logger": logger, "fetchHistoryCacheSize": 0,
        "enableLastHttpResponse": False, "enableLastJsonResponse": False,
        "enableLastResponseHeaders": False,
        "options": {"defaultType": "future", "maxRetriesOnFailure": 0},
    })
    client.enable_demo_trading(True)
    # Remove live/testnet backups rather than leaving a fallback target around.
    only_fapi = {key: value for key, value in client.urls["api"].items()
                 if key.startswith("fapi")}
    client.urls = {"api": dict(only_fapi), "demo": dict(only_fapi)}
    client.session.trust_env = False
    from requests.adapters import HTTPAdapter
    client.session.mount("https://", HTTPAdapter(max_retries=0))
    original_request = client.session.request

    def guarded_request(method, url, **kwargs):
        assert_demo_url(url)
        if client.session.trust_env or client.session.adapters["https://"].max_retries.total != 0:
            raise DemoSafetyError("Demo session routing or retry guard changed")
        if kwargs.get("proxies"):
            raise DemoSafetyError("Demo proxy routing is prohibited")
        kwargs["allow_redirects"] = False
        response = original_request(method, url, **kwargs)
        if 300 <= response.status_code < 400:
            raise DemoSafetyError("Demo redirects are prohibited")
        return response

    client.session.request = guarded_request
    return client


def normalize_order(raw: Mapping[str, Any], pair: str,
                    latency_ms: float = 0, *, allow_external: bool = False) -> dict[str, Any]:
    expected = raw.get("symbol") if allow_external else _symbol(pair)
    if raw.get("symbol") != expected or not raw.get("orderId"):
        raise DemoSafetyError("invalid Binance Demo order response identity")
    return {
        "order_id": str(raw["orderId"]), "client_id": raw.get("clientOrderId"),
        "allowed_pair": pair in PAIR_SYMBOLS,
        "pair": pair, "side": str(raw.get("side", "")).lower(),
        "type": str(raw.get("type", "")).lower(), "status": raw.get("status"),
        "qty": _number(raw.get("origQty")), "filled_qty": _number(raw.get("executedQty")),
        "price": _number(raw.get("price")), "avg_price": _number(raw.get("avgPrice")),
        "created_ms": raw.get("time", raw.get("updateTime")),
        "updated_ms": raw.get("updateTime", raw.get("time")),
        "request_latency_ms": latency_ms, "raw": sanitize_response(raw),
    }


def normalize_fill(raw: Mapping[str, Any], pair: str) -> dict[str, Any]:
    if raw.get("symbol") != _symbol(pair) or raw.get("id") is None:
        raise DemoSafetyError("invalid Binance Demo fill response identity")
    maker = raw.get("maker", raw.get("isMaker"))
    if not isinstance(maker, bool):
        maker = None
    return {
        "fill_id": str(raw["id"]), "order_id": str(raw.get("orderId", "")),
        "pair": pair, "side": str(raw.get("side", "")).lower(),
        "price": _number(raw.get("price")), "qty": _number(raw.get("qty")),
        "commission": _number(raw.get("commission")),
        "commission_asset": raw.get("commissionAsset"), "is_maker": maker,
        "realized_pnl": _number(raw.get("realizedPnl")), "ts": raw.get("time"),
        "raw": sanitize_response(raw),
    }


def summarize_fill_fees(fills: list[Mapping[str, Any]]) -> dict[str, Any]:
    """No automatic conversion of BNB/other commissions to USDT."""
    totals = defaultdict(Decimal)
    complete = bool(fills)
    for fill in fills:
        amount, asset = _number(fill.get("commission")), fill.get("commission_asset")
        if amount is None or not isinstance(asset, str) or not asset:
            complete = False
        else:
            totals[asset] += Decimal(amount)
    return {
        "commission_by_asset": {asset: format(total, "f") for asset, total in totals.items()},
        "commission_complete": complete,
        "commission_usdt": (format(totals["USDT"], "f")
                            if complete and set(totals) == {"USDT"} else None),
        "fee_source": "binance_demo_user_trades",
    }


class BinanceDemoAdapter:
    """Only explicit Demo credentials; read-only unless allow_trading is true.

    The runner must persist attempt claims across restarts.  This instance also
    rejects any repeated submission, even when a lookup reports OrderNotFound:
    a timeout plus absence from an eventually-consistent query proves nothing.
    """

    def __init__(self, *, client=None, api_key: str | None = None,
                 api_secret: str | None = None, allow_trading: bool = False,
                 owned_client_ids=()):
        if client is not None:
            import ccxt
            if (isinstance(client, ccxt.Exchange)
                    and getattr(client, "_quant_demo_transport_guard", None) != ADAPTER_VERSION):
                raise DemoSafetyError("unguarded CCXT clients cannot be injected")
        self._client = client if client is not None else _build_client(api_key, api_secret)
        self._credentials_ready = bool(api_key and api_secret)
        self.allow_trading = allow_trading is True
        self._attempts: dict[str, str] = {}
        self._request_lock = threading.RLock()
        self._attempt_lock = threading.Lock()
        self._owned_client_ids = set()
        for client_id in owned_client_ids:
            self.authorize_owned_client_id(client_id)
        self._market_rules: dict[str, dict] = {}
        self.declared_rate_limits: list | None = None
        self.last_call: dict[str, Any] | None = None
        self._guard_client()

    def authorize_owned_client_id(self, client_order_id: str):
        """Trust only a runner's already-claimed durable order ledger identity."""
        _check_client_id(client_order_id)
        self._owned_client_ids.add(client_order_id)

    @classmethod
    def from_demo_env(cls, *, env: Mapping[str, str] | None = None, **kwargs):
        # No dotenv load, no real-account config, no other environment key names.
        source = os.environ if env is None else env
        return cls(api_key=source.get("QUANT_BINANCE_DEMO_API_KEY"),
                   api_secret=source.get("QUANT_BINANCE_DEMO_API_SECRET"), **kwargs)

    def _guard_client(self):
        client = self._client
        if (getattr(client, "id", None) != "binance"
                or getattr(client, "isSandboxModeEnabled", False)
                or client.options.get("enableDemoTrading") is not True
                or getattr(client, "verbose", False)
                or client.options.get("maxRetriesOnFailure", 0) != 0):
            raise DemoSafetyError("Demo client identity or transport guard failed")
        for api in ("fapiPublic", "fapiPrivate", "fapiPrivateV3"):
            _assert_api_base(api, client.urls.get("api", {}).get(api, ""))

    def _call(self, path: str, api: str, method: str, params: dict | None = None):
        with self._request_lock:
            return self._call_locked(path, api, method, params)

    def _call_locked(self, path: str, api: str, method: str, params: dict | None = None):
        self._guard_client()
        _request_allowed(path, api, method)
        if api.startswith("fapiPrivate") and not self._credentials_ready:
            raise DemoSafetyError("explicit Demo credentials are required")
        started = time.monotonic()
        config = dict(REQUEST_COSTS[(api, method, path)])
        effective_cost = (config["noSymbol"] if "noSymbol" in config
                          and not (params or {}).get("symbol") else config["cost"])
        try:
            result = self._client.request(path, api, method, dict(params or {}), config=config)
        except Exception as exc:
            elapsed = round((time.monotonic() - started) * 1000, 3)
            kind = type(exc).__name__
            # Numeric exchange codes are safe evidence; never retain the full
            # exception, which may contain a signed URL, request body, or key.
            code_match = re.search(r'"code"\s*:\s*(-?[0-9]+)', str(exc))
            code = int(code_match[1]) if code_match else None
            self.last_call = {"path": path, "api": api, "method": method,
                              "latency_ms": elapsed, "status": "error", "error_kind": kind,
                              "exchange_code": code, "rate_limit_cost": effective_cost}
            raise DemoRequestError(kind, elapsed, code) from None
        self.last_call = {"path": path, "api": api, "method": method,
                          "latency_ms": round((time.monotonic() - started) * 1000, 3),
                          "status": "ok", "adapter_version": ADAPTER_VERSION,
                          "rate_limit_cost": effective_cost}
        return result

    def fetch_exchange_info(self) -> dict:
        raw = self._call("exchangeInfo", "fapiPublic", "GET")
        if not isinstance(raw, dict) or not isinstance(raw.get("symbols"), list):
            raise DemoSafetyError("invalid Demo exchangeInfo response")
        rules = {}
        for item in raw["symbols"]:
            symbol = item.get("symbol")
            if symbol in SYMBOL_PAIRS:
                if (item.get("contractType") != "PERPETUAL" or item.get("quoteAsset") != "USDT"
                        or item.get("marginAsset") != "USDT" or item.get("status") != "TRADING"):
                    raise DemoSafetyError("requested market is not a trading USDT perpetual")
                rules[symbol] = sanitize_response(item)
        self._market_rules = rules
        rate_limits = raw.get("rateLimits")
        self.declared_rate_limits = (sanitize_response(rate_limits)
                                     if isinstance(rate_limits, list) else None)
        return sanitize_response(raw)

    def validate_order(self, pair: str, quantity: Any, price: Any = None,
                       *, market: bool = False) -> tuple[str, str | None]:
        symbol, qty = _symbol(pair), _decimal(quantity, positive=True)
        if symbol not in self._market_rules:
            self.fetch_exchange_info()
        if symbol not in self._market_rules:
            raise DemoSafetyError("Demo market rules unavailable")
        filters = {row["filterType"]: row for row in self._market_rules[symbol]["filters"]}
        lot = filters.get("MARKET_LOT_SIZE") if market else filters.get("LOT_SIZE")
        if lot is None:
            raise DemoSafetyError("Demo lot-size rules unavailable")
        step = _decimal(lot["stepSize"])
        if (qty < _decimal(lot["minQty"]) or qty > _decimal(lot["maxQty"])
                or (step > 0 and qty % step != 0)):
            raise DemoSafetyError("quantity violates Demo exchange precision or size rules")
        if market:
            return format(qty, "f"), None
        px = _decimal(price, positive=True)
        price_filter, notional = filters.get("PRICE_FILTER"), filters.get("MIN_NOTIONAL")
        if price_filter is None or notional is None:
            raise DemoSafetyError("Demo price/notional rules unavailable")
        tick = _decimal(price_filter["tickSize"], positive=True)
        if (px < _decimal(price_filter["minPrice"]) or px > _decimal(price_filter["maxPrice"])
                or px % tick != 0 or qty * px < _decimal(notional["notional"])):
            raise DemoSafetyError("price or notional violates Demo exchange rules")
        return format(qty, "f"), format(px, "f")

    def prepare_limit_order(self, pair: str, side: str, quantity: Any,
                            price: Any) -> dict[str, str]:
        """Round size down and a resting buy/sell price away from crossing."""
        symbol = _symbol(pair)
        if side not in {"buy", "sell"}:
            raise DemoSafetyError("invalid order side")
        qty, px = _decimal(quantity, positive=True), _decimal(price, positive=True)
        if symbol not in self._market_rules:
            self.fetch_exchange_info()
        if symbol not in self._market_rules:
            raise DemoSafetyError("Demo market rules unavailable")
        filters = {row["filterType"]: row for row in self._market_rules[symbol]["filters"]}
        lot, price_filter = filters.get("LOT_SIZE"), filters.get("PRICE_FILTER")
        if lot is None or price_filter is None:
            raise DemoSafetyError("Demo quantization rules unavailable")
        step, tick = _decimal(lot["stepSize"], positive=True), _decimal(price_filter["tickSize"],
                                                                      positive=True)
        qty = (qty / step).to_integral_value(rounding=ROUND_FLOOR) * step
        price_rounding = ROUND_FLOOR if side == "buy" else ROUND_CEILING
        px = (px / tick).to_integral_value(rounding=price_rounding) * tick
        quantity_text, price_text = self.validate_order(pair, qty, px)
        return {"quantity": quantity_text, "price": price_text}

    def fetch_orders(self, pair: str, *, since_ms: int | None = None,
                     limit: int = 1000) -> list[dict]:
        params = {"symbol": _symbol(pair), "limit": _limit(limit)}
        if since_ms is not None:
            params["startTime"] = _timestamp(since_ms)
        raw = self._call("allOrders", "fapiPrivate", "GET", params)
        return [normalize_order(item, pair, self.last_call["latency_ms"]) for item in raw]

    def fetch_order_by_client_id(self, pair: str, client_order_id: str) -> dict | None:
        _check_client_id(client_order_id)
        try:
            raw = self._call("order", "fapiPrivate", "GET", {
                "symbol": _symbol(pair), "origClientOrderId": client_order_id})
        except DemoRequestError as exc:
            if exc.error_kind == "OrderNotFound":
                return None
            raise
        result = normalize_order(raw, pair, self.last_call["latency_ms"])
        if result["client_id"] != client_order_id:
            raise DemoSafetyError("Demo order client identity mismatch")
        if client_order_id in self._attempts:
            self._attempts[client_order_id] = "confirmed"
        return result

    def fetch_open_orders(self, pair: str | None = None) -> list[dict]:
        # Unscoped snapshots expose foreign symbols, so a runner can stop on
        # ownership conflicts rather than accidentally hiding the exposure.
        params = {"symbol": _symbol(pair)} if pair is not None else {}
        raw = self._call("openOrders", "fapiPrivate", "GET", params)
        result = []
        for item in raw:
            symbol = item.get("symbol")
            if not isinstance(symbol, str) or not symbol:
                raise DemoSafetyError("invalid Demo open-order symbol")
            actual_pair = SYMBOL_PAIRS.get(symbol, symbol)
            result.append(normalize_order(item, actual_pair, self.last_call["latency_ms"],
                                           allow_external=actual_pair not in PAIR_SYMBOLS))
        return result

    def fetch_trades(self, pair: str, *, since_ms: int | None = None,
                     end_ms: int | None = None, limit: int = 1000,
                     order_id: str | None = None, from_id: str | int | None = None) -> list[dict]:
        params = {"symbol": _symbol(pair), "limit": _limit(limit)}
        if from_id is not None:
            if since_ms is not None or end_ms is not None or order_id is not None:
                raise DemoSafetyError("fill ID cursor cannot be combined with times or order ID")
            params["fromId"] = _identifier(from_id)
        if since_ms is not None:
            params["startTime"] = _timestamp(since_ms)
        if end_ms is not None:
            params["endTime"] = _timestamp(end_ms)
            if since_ms is not None and (end_ms < since_ms or end_ms - since_ms >= 604800000):
                raise DemoSafetyError("Demo user-trades window must be shorter than seven days")
        if order_id is not None:
            params["orderId"] = _identifier(order_id)
        raw = self._call("userTrades", "fapiPrivate", "GET", params)
        return [normalize_fill(item, pair) for item in raw]

    def fetch_position_mode(self) -> dict:
        raw = self._call("positionSide/dual", "fapiPrivate", "GET")
        mode = raw.get("dualSidePosition") if isinstance(raw, dict) else None
        if not isinstance(mode, bool):
            raise DemoSafetyError("Demo position mode response is incomplete")
        return {"hedged": mode, "dual_side_position": mode, "raw": sanitize_response(raw)}

    def fetch_symbol_config(self, pair: str | None = None) -> list[dict]:
        params = {"symbol": _symbol(pair)} if pair is not None else {}
        raw = self._call("symbolConfig", "fapiPrivate", "GET", params)
        if not isinstance(raw, list) or not raw:
            raise DemoSafetyError("Demo symbol config response is incomplete")
        results = []
        for item in raw:
            symbol = item.get("symbol")
            leverage = item.get("leverage")
            if (not isinstance(symbol, str) or not symbol or isinstance(leverage, bool)
                    or not isinstance(leverage, (str, int))
                    or not re.fullmatch(r"[0-9]+", str(leverage)) or int(leverage) <= 0
                    or item.get("marginType") not in {"CROSSED", "ISOLATED"}):
                raise DemoSafetyError("Demo symbol config response is incomplete")
            if pair is not None and symbol != _symbol(pair):
                raise DemoSafetyError("Demo symbol config identity mismatch")
            auto_margin = item.get("isAutoAddMargin")
            if auto_margin in {"true", "false"}:
                auto_margin = auto_margin == "true"
            elif not isinstance(auto_margin, bool):
                auto_margin = None
            results.append({"pair": SYMBOL_PAIRS.get(symbol, symbol), "symbol": symbol,
                            "allowed_pair": symbol in SYMBOL_PAIRS, "leverage": int(leverage),
                            "margin_type": item["marginType"],
                            "is_auto_add_margin": auto_margin,
                            "max_notional_value": _number(item.get("maxNotionalValue")),
                            "raw": sanitize_response(item)})
        if pair is not None and len(results) != 1:
            raise DemoSafetyError("Demo symbol config requested identity is not unique")
        return results

    def fetch_positions(self) -> list[dict]:
        raw = self._call("positionRisk", "fapiPrivateV3", "GET")
        return [{"pair": SYMBOL_PAIRS.get(item.get("symbol"), item.get("symbol")),
                 "symbol": item.get("symbol"),
                 "allowed_pair": item.get("symbol") in SYMBOL_PAIRS,
                 "signed_qty": _number(item.get("positionAmt")),
                 "position_side": item.get("positionSide"),
                 "entry_price": _number(item.get("entryPrice")),
                 "notional": _number(item.get("notional")),
                 "unrealized_pnl": _number(item.get("unRealizedProfit")),
                 "raw": sanitize_response(item)} for item in raw]

    def fetch_balance(self) -> dict:
        raw = self._call("account", "fapiPrivateV3", "GET")
        return {"wallet_balance": _number(raw.get("totalWalletBalance")),
                "margin_balance": _number(raw.get("totalMarginBalance")),
                "available_balance": _number(raw.get("availableBalance")),
                "assets": sanitize_response(raw.get("assets", [])),
                "positions": sanitize_response(raw.get("positions", [])),
                "raw": sanitize_response(raw)}

    def _submit(self, pair, side, quantity, price, client_order_id, *, market,
                post_only=True, reduce_only=False):
        self._guard_client()
        if not self.allow_trading:
            raise DemoSafetyError("Demo adapter is read-only")
        if not self._credentials_ready:
            raise DemoSafetyError("explicit Demo credentials are required")
        if side not in {"buy", "sell"}:
            raise DemoSafetyError("invalid order side")
        _check_client_id(client_order_id)
        with self._attempt_lock:
            if client_order_id in self._attempts:
                raise DemoSafetyError("client order ID was already attempted; reconcile without resending")
        qty, px = self.validate_order(pair, quantity, price, market=market)
        params = {"symbol": _symbol(pair), "side": side.upper(), "quantity": qty,
                  "type": "MARKET" if market else "LIMIT",
                  "newClientOrderId": client_order_id, "newOrderRespType": "RESULT",
                  "reduceOnly": "true" if reduce_only else "false"}
        if not market:
            params.update(price=px, timeInForce="GTX" if post_only else "GTC")
        with self._attempt_lock:
            # Repeat the test after rule validation, which can issue a public
            # request and let another thread reach the same client ID first.
            if client_order_id in self._attempts:
                raise DemoSafetyError("client order ID was already attempted; reconcile without resending")
            self._attempts[client_order_id] = "unknown"
            self._owned_client_ids.add(client_order_id)
        try:
            raw = self._call("order", "fapiPrivate", "POST", params)
            result = normalize_order(raw, pair, self.last_call["latency_ms"])
            if result["client_id"] != client_order_id:
                raise DemoSafetyError("Demo submission client identity mismatch")
        except Exception as exc:
            kind = exc.error_kind if isinstance(exc, DemoRequestError) else type(exc).__name__
            latency = exc.latency_ms if isinstance(exc, DemoRequestError) else 0
            code = exc.exchange_code if isinstance(exc, DemoRequestError) else None
            definite_type = kind in {"InvalidOrder", "InsufficientFunds", "BadSymbol",
                                     "AuthenticationError", "PermissionDenied", "AccountSuspended",
                                     "ArgumentsRequired", "BadRequest", "OperationRejected"}
            # A duplicate-ID error can mean a previous attempt already exists;
            # retain UNKNOWN until looked up, rather than erasing its exposure.
            if definite_type and code not in {-4111, -4116, -2010}:
                self._attempts[client_order_id] = "rejected"
                raise DemoExchangeRejected(kind, latency, code) from None
            raise DemoSubmissionUnknown(kind, latency, code) from None
        self._attempts[client_order_id] = "confirmed"
        return result

    def submit_limit_order(self, pair: str, side: str, quantity: Any, price: Any,
                           client_order_id: str, *, post_only: bool = True,
                           reduce_only: bool = False) -> dict:
        return self._submit(pair, side, quantity, price, client_order_id,
                            market=False, post_only=post_only, reduce_only=reduce_only)

    def submit_market_order(self, pair: str, side: str, quantity: Any,
                            client_order_id: str, *, reduce_only: bool = True) -> dict:
        if reduce_only is not True:
            raise DemoSafetyError("market orders are only permitted for reduce-only protective exits")
        return self._submit(pair, side, quantity, None, client_order_id,
                            market=True, reduce_only=True)

    def cancel_order(self, pair: str, order_id: str | None = None, *,
                     client_order_id: str) -> dict | None:
        if not self.allow_trading:
            raise DemoSafetyError("Demo adapter is read-only")
        _check_client_id(client_order_id)
        if client_order_id not in self._owned_client_ids:
            raise DemoSafetyError("cancel requires an owned durable order ledger identity")
        observed = self.fetch_order_by_client_id(pair, client_order_id)
        if observed is None:
            return None
        if order_id is not None and str(order_id) != observed["order_id"]:
            raise DemoSafetyError("cancel order ID does not match the owned client ID")
        if observed["status"] in {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}:
            return observed
        raw = self._call("order", "fapiPrivate", "DELETE", {
            "symbol": _symbol(pair), "origClientOrderId": client_order_id})
        result = normalize_order(raw, pair, self.last_call["latency_ms"])
        if result["client_id"] != client_order_id or result["order_id"] != observed["order_id"]:
            raise DemoSafetyError("Demo canceled order identity mismatch")
        return result


def _check_client_id(value: str):
    if not isinstance(value, str) or not _CLIENT_ID.fullmatch(value):
        raise DemoSafetyError("expected a stable adapter client order ID")


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise DemoSafetyError("invalid Demo page limit")
    return value


def _timestamp(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DemoSafetyError("invalid Demo timestamp")
    return value


def _identifier(value: str | int) -> str:
    if (isinstance(value, bool) or not isinstance(value, (str, int))
            or not re.fullmatch(r"[0-9]+", str(value))):
        raise DemoSafetyError("invalid ASCII Binance Demo identifier")
    return str(value)

"""Fail-closed USDⓈ-M Binance Demo transport; no account configuration changes.

Amounts remain decimal strings.  Fees come from actual user-trades, never an
assumed rate.  An injected client is intended for offline tests; production
clients are always constructed as the guarded CCXT subclass below.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Mapping
from urllib.parse import urlsplit

ADAPTER_VERSION = "binance-demo-usdm-v1"
PROTECTION_CAPABILITY_VERSION = "demo-protection-capabilities-v1"
SUPPORTED_CCXT_VERSION = "4.5.85"
DEMO_HOST = "demo-fapi.binance.com"
MAKER_LIMIT_POLICY = "ref_capped_by_book_maker_price_v1"
ALLOWED_PAIRS = ("BTC/USDT:USDT", "ETH/USDT:USDT", "TRX/USDT:USDT")
PAIR_SYMBOLS = {pair: pair.split("/")[0] + "USDT" for pair in ALLOWED_PAIRS}
SYMBOL_PAIRS = {symbol: pair for pair, symbol in PAIR_SYMBOLS.items()}
LOGGER = logging.getLogger(__name__)
_CLIENT_ID = re.compile(r"qd-[a-f0-9]{32}\Z")
_PRIVATE_GET = {
    ("fapiPrivate", "order"), ("fapiPrivate", "allOrders"),
    ("fapiPrivate", "openOrders"), ("fapiPrivate", "userTrades"),
    ("fapiPrivate", "positionSide/dual"), ("fapiPrivate", "symbolConfig"),
    ("fapiPrivate", "commissionRate"),
    ("fapiPrivate", "leverageBracket"),
    ("fapiPrivate", "algoOrder"), ("fapiPrivate", "openAlgoOrders"),
    ("fapiPrivate", "allAlgoOrders"),
    ("fapiPrivate", "income"), ("fapiPrivate", "multiAssetsMargin"),
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
    ("fapiPublic", "GET", "ticker/bookTicker"): {"cost": 2, "noSymbol": 5},
    ("fapiPrivate", "GET", "order"): {"cost": 1},
    ("fapiPrivate", "GET", "allOrders"): {"cost": 5},
    ("fapiPrivate", "GET", "openOrders"): {"cost": 1, "noSymbol": 40},
    ("fapiPrivate", "GET", "userTrades"): {"cost": 5},
    ("fapiPrivate", "GET", "positionSide/dual"): {"cost": 30},
    ("fapiPrivate", "GET", "symbolConfig"): {"cost": 5},
    ("fapiPrivate", "GET", "commissionRate"): {"cost": 20},
    ("fapiPrivate", "GET", "leverageBracket"): {"cost": 1},
    ("fapiPrivateV3", "GET", "positionRisk"): {"cost": 5},
    ("fapiPrivateV3", "GET", "account"): {"cost": 5},
    ("fapiPrivate", "POST", "order/test"): {"cost": 1},
    ("fapiPrivate", "POST", "order"): {"cost": 4},
    ("fapiPrivate", "DELETE", "order"): {"cost": 1},
    ("fapiPrivate", "GET", "algoOrder"): {"cost": 1},
    ("fapiPrivate", "GET", "openAlgoOrders"): {"cost": 1, "noSymbol": 40},
    ("fapiPrivate", "GET", "allAlgoOrders"): {"cost": 5},
    ("fapiPrivate", "POST", "algoOrder"): {"cost": 1},
    ("fapiPrivate", "DELETE", "algoOrder"): {"cost": 1},
    ("fapiPrivate", "GET", "income"): {"cost": 30},
    ("fapiPrivate", "GET", "multiAssetsMargin"): {"cost": 30},
}
_SECRET_KEYS = {"apikey", "secret", "signature", "authorization", "headers",
                "requestheaders", "signedurl", "requesturl", "key", "password",
                "token", "accesskey", "apisecret", "xmbxapikey", "privatekey"}
_ACCOUNT_SNAPSHOT_FIELDS = {
    "feeTier", "canTrade", "canDeposit", "canWithdraw", "updateTime",
    "totalInitialMargin", "totalMaintMargin", "totalWalletBalance",
    "totalUnrealizedProfit", "totalMarginBalance", "totalPositionInitialMargin",
    "totalOpenOrderInitialMargin", "totalCrossWalletBalance", "totalCrossUnPnl",
    "availableBalance", "maxWithdrawAmount", "assets", "positions",
}
_ACCOUNT_ASSET_AMOUNTS = {
    "walletBalance", "unrealizedProfit", "marginBalance", "maintMargin",
    "initialMargin", "positionInitialMargin", "openOrderInitialMargin",
    "crossWalletBalance", "crossUnPnl", "availableBalance", "maxWithdrawAmount",
}
_ACCOUNT_POSITION_AMOUNTS = {
    "initialMargin", "maintMargin", "unrealizedProfit", "positionInitialMargin",
    "openOrderInitialMargin", "leverage", "entryPrice", "breakEvenPrice",
    "maxNotional", "positionAmt", "notional", "isolatedWallet", "bidNotional",
    "askNotional", "isolatedMargin",
}
_ACCOUNT_ASSET_FIELDS = _ACCOUNT_ASSET_AMOUNTS | {"asset", "marginAvailable", "updateTime"}
_ACCOUNT_POSITION_FIELDS = _ACCOUNT_POSITION_AMOUNTS | {
    "symbol", "positionSide", "isolated", "marginType", "updateTime",
}


def _archive_amount(value):
    """归档保留原十进制文本，先拒绝会伪造精度或无穷值的输入。"""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise DemoSafetyError("invalid Demo archived decimal")
    _decimal(value)


def _archive_text(value, *, empty=False):
    if (not isinstance(value, str) or len(value) > 256
            or (not empty and not value) or any(ord(char) < 32 for char in value)):
        raise DemoSafetyError("invalid Demo archived text identity")


def _open_order_rows(raw, *, pair, conditional, strict):
    """快照不丢弃外来订单；归档模式拒绝缺失、浮点或损坏的关键事实。"""
    if not isinstance(raw, list):
        raise DemoSafetyError("invalid Demo open-orders snapshot")
    for row in raw:
        if not isinstance(row, dict):
            raise DemoSafetyError("invalid Demo open-order row")
        symbol = row.get("symbol")
        _archive_text(symbol)
        actual_pair = SYMBOL_PAIRS.get(symbol, symbol)
        if pair is not None and actual_pair != pair:
            raise DemoSafetyError("Demo open-order snapshot scope mismatch")
        if not strict:
            continue
        identity = _identifier(row.get("algoId" if conditional else "orderId"))
        if int(identity) <= 0:
            raise DemoSafetyError("invalid Demo archived order identity")
        for name in (("clientAlgoId", "orderType", "algoStatus") if conditional else
                     ("clientOrderId", "type", "status")):
            _archive_text(row.get(name))
        side = row.get("side")
        if not isinstance(side, str) or side not in {"BUY", "SELL"}:
            raise DemoSafetyError("invalid Demo archived order side")
        required_amounts = (("quantity", "triggerPrice") if conditional else
                            ("origQty", "executedQty", "price"))
        optional_amounts = (("price", "actualPrice", "actualQty") if conditional else
                            ("avgPrice", "stopPrice", "cumQuote", "activatePrice", "priceRate"))
        for name in (*required_amounts, *(name for name in optional_amounts if name in row)):
            _archive_amount(row.get(name))
            if _decimal(row[name]) < 0:
                raise DemoSafetyError("negative Demo archived order amount")
        if not conditional and _decimal(row["executedQty"]) > _decimal(row["origQty"]):
            raise DemoSafetyError("Demo archived executed quantity exceeds order quantity")
        for name in (("createTime", "updateTime", "triggerTime") if conditional else
                     ("time", "updateTime", "goodTillDate")):
            if name in row:
                _timestamp(row[name])
    return raw


def _income_rows(raw, *, since_ms, until_ms, limit):
    if not isinstance(raw, list) or len(raw) > limit:
        raise DemoSafetyError("invalid Demo income response")
    for row in raw:
        if not isinstance(row, dict):
            raise DemoSafetyError("invalid Demo income row")
        _identifier(row.get("tranId"))
        _archive_text(row.get("incomeType"))
        _archive_text(row.get("asset"))
        _archive_text(row.get("symbol"), empty=True)
        _archive_amount(row.get("income"))
        if not since_ms <= _timestamp(row.get("time")) <= until_ms:
            raise DemoSafetyError("Demo income row is outside requested window")
    return sanitize_response(raw)


def _account_snapshot(raw):
    if not isinstance(raw, dict):
        raise DemoSafetyError("invalid Demo account response")
    for name in ("totalWalletBalance", "totalUnrealizedProfit", "totalMarginBalance"):
        _archive_amount(raw.get(name))
    for name in (_ACCOUNT_SNAPSHOT_FIELDS - {
            "feeTier", "canTrade", "canDeposit", "canWithdraw", "updateTime", "assets", "positions"}) & raw.keys():
        _archive_amount(raw[name])
    assets, positions = raw.get("assets"), raw.get("positions")
    if not isinstance(assets, list) or not isinstance(positions, list):
        raise DemoSafetyError("invalid Demo account assets or positions")
    for asset in assets:
        if not isinstance(asset, dict):
            raise DemoSafetyError("invalid Demo account asset")
        _archive_text(asset.get("asset"))
        _archive_amount(asset.get("walletBalance"))
        for name in _ACCOUNT_ASSET_AMOUNTS & asset.keys():
            _archive_amount(asset[name])
    for position in positions:
        if not isinstance(position, dict):
            raise DemoSafetyError("invalid Demo account position")
        _archive_text(position.get("symbol"))
        _archive_amount(position.get("positionAmt"))
        for name in _ACCOUNT_POSITION_AMOUNTS & position.keys():
            _archive_amount(position[name])
    # 只返回账户官方字段，不透传测试替身可能附加的“已对账/精确午夜”声明。
    result = {name: raw[name] for name in raw if name in _ACCOUNT_SNAPSHOT_FIELDS}
    result["assets"] = [{name: row[name] for name in row if name in _ACCOUNT_ASSET_FIELDS}
                        for row in assets]
    result["positions"] = [{name: row[name] for name in row if name in _ACCOUNT_POSITION_FIELDS}
                           for row in positions]
    return sanitize_response(result)


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


def _optional_bool(value: Any) -> bool | None:
    """仅接受交易所明确布尔事实；缺失、数字和损坏字段保持未知。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value in {"true", "false"}:
        return value == "true"
    return None


def _optional_leverage(value: Any) -> int | None:
    if (isinstance(value, bool) or not isinstance(value, (str, int))
            or not re.fullmatch(r"[0-9]+", str(value)) or int(value) <= 0):
        return None
    return int(value)


def _optional_margin_type(value: Any) -> str | None:
    if isinstance(value, str) and value.upper() in {"CROSSED", "ISOLATED"}:
        return value.upper()
    return None


def _bracket_decimal(value: Any, field: str, *, positive: bool = False,
                     nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise DemoSafetyError(f"invalid leverage-bracket {field}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise DemoSafetyError(f"invalid leverage-bracket {field}") from None
    if (not result.is_finite() or (positive and result <= 0)
            or (nonnegative and result < 0)):
        raise DemoSafetyError(f"invalid leverage-bracket {field}")
    return result


def _bracket_integer(value: Any, field: str) -> int:
    number = _bracket_decimal(value, field, positive=True)
    if number != number.to_integral_value():
        raise DemoSafetyError(f"invalid leverage-bracket {field}")
    return int(number)


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
                  and path in {"exchangeInfo", "time", "premiumIndex", "ticker/bookTicker"})
                 or (method == "GET" and (api, path) in _PRIVATE_GET)
                 or (api == "fapiPrivate" and path == "order/test"
                     and method == "POST")
                 or (api == "fapiPrivate" and path in {"order", "algoOrder"}
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
        # Keep only a digest of the response bytes.  CCXT may decode and
        # normalize the payload before the adapter sees it; that normalized
        # object is useful for content identity but cannot stand in for the
        # raw HTTP evidence required by the account-risk gate.
        try:
            raw_bytes = bytes(response.content)
        except (AttributeError, TypeError, ValueError):
            raise DemoSafetyError("Demo response body is not byte-addressable") from None
        client._quant_last_response_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        client._quant_last_response_hash_basis = "raw_http_response_bytes"
        # The leverage-bracket response contains exchange metadata only. Keep
        # the exact body in memory so the caller can place it in a 0600
        # evidence artifact after an explicit read-only capture. It is never
        # logged or included in the adapter's public normalized payload.
        client._quant_last_response_bytes = raw_bytes
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


def normalize_conditional_order(raw: Mapping[str, Any], pair: str,
                                latency_ms: float = 0, *, allow_external=False) -> dict:
    """保留算法单事实；触发或结束永远不等于子订单成交、账户空仓。"""
    if not isinstance(raw, Mapping):
        raise DemoSafetyError("invalid Demo conditional-order response")
    expected = pair if allow_external else _symbol(pair)
    if raw.get("symbol") != expected:
        raise DemoSafetyError("Demo conditional-order symbol mismatch")
    algo_id = _identifier(raw.get("algoId"))
    side = raw.get("side")
    if int(algo_id) <= 0 or not isinstance(side, str) or side not in {"BUY", "SELL"}:
        raise DemoSafetyError("invalid Demo conditional-order identity or side")
    client_id, status = raw.get("clientAlgoId"), raw.get("algoStatus")
    if (not isinstance(client_id, str) or not client_id
            or not isinstance(status, str) or not status):
        raise DemoSafetyError("Demo conditional-order identity or status missing")
    actual_id = raw.get("actualOrderId")
    return {
        "algo_id": algo_id, "client_algo_id": client_id, "algo_status": status,
        "pair": pair, "symbol": raw["symbol"], "allowed_pair": pair in ALLOWED_PAIRS,
        "algo_type": raw.get("algoType"),
        "order_type": raw.get("orderType"), "side": side.lower(),
        "position_side": raw.get("positionSide"),
        "quantity": _number(raw.get("quantity")),
        "trigger_price": _number(raw.get("triggerPrice")),
        "reduce_only": _optional_bool(raw.get("reduceOnly")),
        "close_position": _optional_bool(raw.get("closePosition")),
        "working_type": raw.get("workingType"),
        "price_protect": _optional_bool(raw.get("priceProtect")),
        "created_ms": raw.get("createTime"), "updated_ms": raw.get("updateTime"),
        "trigger_ms": raw.get("triggerTime"),
        "actual_order_id": None if actual_id in (None, "", "0", 0) else _identifier(actual_id),
        "execution_complete": False, "request_latency_ms": latency_ms,
        "raw": sanitize_response(raw),
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

    def protection_capabilities(self) -> dict[str, Any]:
        """报告已有保护接口及其验证边界，不从方法存在推导交易所资格。

        原生条件单提交、查询与取消接口已经实现；受控 Demo 合同、部分
        成交覆盖、安全替换及生产接线尚未验证，正式验收继续不具备资格。
        """
        # 同名方法存在不代表已通过 Demo 原生接口核验、部分成交覆盖和安全
        # 替换验证。当前版本没有这套合同，不能由反射或轮询回退签发资格。
        return {
            "version": PROTECTION_CAPABILITY_VERSION,
            "native_conditional_orders": False,
            "native_stop_loss": False,
            "native_take_profit": False,
            "conditional_order_reconciliation": False,
            "conditional_order_cancel": False,
            "native_contract_verified": False,
            "partial_fill_coverage_verified": False,
            "safe_replacement_verified": False,
            "exchange_native_support": "unknown",
            "reduce_only_market_fallback": callable(getattr(self, "submit_market_order", None)),
            "confirmation_target_ms": 2000,
            "acceptance_eligible": False,
            "reason": "native_conditional_contract_unavailable_polling_fallback_only",
        }


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
        started_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
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
                              "exchange_code": code, "rate_limit_cost": effective_cost,
                              "started_at_utc": started_at_utc}
            raise DemoRequestError(kind, elapsed, code) from None
        self.last_call = {"path": path, "api": api, "method": method,
                          "latency_ms": round((time.monotonic() - started) * 1000, 3),
                          "status": "ok", "adapter_version": ADAPTER_VERSION,
                          "rate_limit_cost": effective_cost,
                          "started_at_utc": started_at_utc}
        return result

    def _read_archive(self, path, api, params, validator, *, with_evidence):
        """响应与请求证据在同一锁内配对，防止并发读取借用另一请求哈希。"""
        if not isinstance(with_evidence, bool):
            raise DemoSafetyError("with_evidence must be boolean")
        with self._request_lock:
            # 旧响应摘要绝不能充作本次传输事实。受守卫的HTTP层会重新填入。
            self._client._quant_last_response_sha256 = None
            self._client._quant_last_response_hash_basis = None
            raw = self._call_locked(path, api, "GET", params)
            data = validator(raw)
            raw_hash = getattr(self._client, "_quant_last_response_sha256", None)
            raw_basis = getattr(self._client, "_quant_last_response_hash_basis", None)
            if (isinstance(raw_hash, str) and re.fullmatch(r"[0-9a-f]{64}", raw_hash)
                    and raw_basis == "raw_http_response_bytes"):
                response_hash, response_basis = raw_hash, raw_basis
            else:
                # 注入的离线替身没有传输字节，规范化摘要明确标注其证据基础。
                try:
                    canonical = json.dumps(sanitize_response(raw), ensure_ascii=False,
                                           sort_keys=True, allow_nan=False, separators=(",", ":"))
                except (TypeError, ValueError):
                    raise DemoSafetyError("invalid Demo archive response encoding") from None
                response_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                response_basis = "canonical_json"
            evidence = {**self.last_call, "request_params": deepcopy(params),
                        "response_sha256": response_hash,
                        "response_hash_basis": response_basis,
                        "received_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
            self.last_call = deepcopy(evidence)
            # 调用方、最近请求与客户端原文不得共享嵌套可变引用。
            return {"data": deepcopy(data), "request_evidence": deepcopy(evidence)} if with_evidence else data

    def fetch_income_window(self, *, since_ms, until_ms, limit=1000, with_evidence=False):
        """读取含端点的全部类型流水；满页不代表已完整，后续采集器需继续分窗。"""
        start, end, page_size = _timestamp(since_ms), _timestamp(until_ms), _limit(limit)
        if end < start or end - start >= 7 * 86400000:
            raise DemoSafetyError("Demo income window must be ordered and shorter than seven days")
        return self._read_archive("income", "fapiPrivate",
            {"startTime": start, "endTime": end, "limit": page_size},
            lambda raw: _income_rows(raw, since_ms=start, until_ms=end, limit=page_size),
            with_evidence=with_evidence)

    def fetch_multi_assets_mode(self, *, with_evidence=False):
        """只读取明确的多资产模式；False也必须来自响应中的真实布尔字段。"""
        def validate(raw):
            if not isinstance(raw, dict) or type(raw.get("multiAssetsMargin")) is not bool:
                raise DemoSafetyError("Demo multi-assets mode is not an explicit boolean")
            return raw["multiAssetsMargin"]
        return self._read_archive("multiAssetsMargin", "fapiPrivate", {}, validate,
                                  with_evidence=with_evidence)

    def fetch_server_time(self, *, with_evidence=False):
        """服务端时间只用于时钟对照，不证明账户余额的精确历史时点。"""
        def validate(raw):
            if not isinstance(raw, dict):
                raise DemoSafetyError("invalid Demo server-time response")
            return _timestamp(raw.get("serverTime"))
        return self._read_archive("time", "fapiPublic", {}, validate, with_evidence=with_evidence)

    def fetch_account_snapshot(self, *, with_evidence=False):
        """保留账户原始字段与金额文本；不填充对账声明、as_of或午夜快照。"""
        return self._read_archive("account", "fapiPrivateV3", {}, _account_snapshot,
                                  with_evidence=with_evidence)

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

    def fetch_book_ticker(self, pair: str) -> dict:
        """Read a single Demo top of book, preserving native decimal strings.

        The optional Binance ``time`` is the exchange timestamp.  Its absence
        stays unknown; request latency never substitutes for quote freshness.
        """
        symbol = _symbol(pair)
        raw = self._call("ticker/bookTicker", "fapiPublic", "GET", {"symbol": symbol})
        if not isinstance(raw, dict) or raw.get("symbol") != symbol:
            raise DemoSafetyError("Demo book-ticker response identity mismatch")
        fields = ("bidPrice", "askPrice", "bidQty", "askQty")
        values = {}
        for field in fields:
            value = raw.get(field)
            if not isinstance(value, str):
                raise DemoSafetyError("Demo book-ticker response is incomplete or invalid")
            values[field] = _decimal(value, positive=True)
        if values["bidPrice"] >= values["askPrice"]:
            raise DemoSafetyError("Demo book-ticker response is crossed or locked")
        timestamp = _timestamp(raw["time"]) if "time" in raw else None
        return {
            "pair": pair, "symbol": symbol,
            "bid_price": raw["bidPrice"], "ask_price": raw["askPrice"],
            "bid_qty": raw["bidQty"], "ask_qty": raw["askQty"],
            "timestamp_ms": timestamp,
            "request_latency_ms": self.last_call["latency_ms"],
            "raw": sanitize_response(raw),
        }

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

    def prepare_maker_limit_order(self, pair: str, side: str, quantity: Any,
                                  reference_price: Any) -> dict[str, Any]:
        """Prepare a resting GTX quote from a fresh top-of-book observation.

        The paper/reference price remains evidence only.  The submitted price
        is moved one tick inside the current book whenever the reference would
        cross the spread, preserving post-only semantics without using a taker
        order.
        """
        book = self.fetch_book_ticker(pair)
        symbol = _symbol(pair)
        if symbol not in self._market_rules:
            self.fetch_exchange_info()
        filters = {row["filterType"]: row for row in self._market_rules[symbol]["filters"]}
        price_filter = filters.get("PRICE_FILTER")
        if price_filter is None:
            raise DemoSafetyError("Demo price precision unavailable")
        tick = _decimal(price_filter["tickSize"], positive=True)
        reference = _decimal(reference_price, positive=True)
        bid, ask = _decimal(book["bid_price"], positive=True), _decimal(book["ask_price"], positive=True)
        if side == "buy":
            maker_limit = min(reference, bid - tick)
            adjusted = reference > bid - tick
        elif side == "sell":
            maker_limit = max(reference, ask + tick)
            adjusted = reference < ask + tick
        else:
            raise DemoSafetyError("invalid order side")
        if maker_limit <= 0:
            raise DemoSafetyError("book does not provide a valid maker price")
        prepared = self.prepare_limit_order(pair, side, quantity, maker_limit)
        submitted = _decimal(prepared["price"], positive=True)
        # Measure the actual, exchange-quantized quote against the paper
        # reference.  Keep it as a decimal string for lossless audit storage.
        deviation = (((reference - submitted) / reference) * Decimal("10000")).quantize(
            Decimal("0.000001"))
        return {
            **prepared,
            "reference_price": format(reference, "f"),
            "bid_price": book["bid_price"],
            "ask_price": book["ask_price"],
            "quote_timestamp_ms": book.get("timestamp_ms"),
            "quote_request_latency_ms": book.get("request_latency_ms"),
            "quote_adjustment_reason": "inside_top_of_book" if adjusted else "reference_already_maker",
            "quote_adjustment_applied": adjusted,
            "limit_deviation_bps": format(deviation.normalize(), "f"),
            "book_at_submit": {
                "bid": book["bid_price"],
                "ask": book["ask_price"],
                "observed_ms": book.get("timestamp_ms"),
            },
            "limit_policy": MAKER_LIMIT_POLICY,
        }

    def prepare_gtx_entry(self, pair: str, side: str, quantity: Any,
                          reference_price: Any) -> dict[str, Any]:
        """Compatibility name for the guarded maker-entry preparation path."""
        return self.prepare_maker_limit_order(pair, side, quantity, reference_price)

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

    def fetch_open_orders(self, pair: str | None = None, *, with_evidence=False) -> list[dict] | dict:
        """默认保留旧列表合同；归档封套与原文在同一请求锁内绑定。"""
        params = {"symbol": _symbol(pair)} if pair is not None else {}
        def validate(raw):
            rows = _open_order_rows(raw, pair=pair, conditional=False, strict=with_evidence)
            result = []
            for item in rows:
                actual_pair = SYMBOL_PAIRS.get(item["symbol"], item["symbol"])
                result.append(normalize_order(item, actual_pair, self.last_call["latency_ms"],
                    allow_external=actual_pair not in PAIR_SYMBOLS))
            return result
        result = self._read_archive("openOrders", "fapiPrivate", params, validate,
                                    with_evidence=with_evidence)
        LOGGER.info("Demo普通挂单只读快照已读取 范围=%s 订单数=%s 请求证据封套=%s；不代表账户对账完成",
                    "全账户" if pair is None else pair,
                    len(result["data"] if with_evidence else result), with_evidence)
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

    def fetch_commission_rate(self, pair: str) -> dict[str, str]:
        """Return native, current Demo account rates; never a modeled fallback."""
        symbol = _symbol(pair)
        raw = self._call("commissionRate", "fapiPrivate", "GET", {"symbol": symbol})
        if not isinstance(raw, dict) or raw.get("symbol") != symbol:
            raise DemoSafetyError("Demo commission-rate response identity mismatch")
        for field in ("makerCommissionRate", "takerCommissionRate"):
            value = raw.get(field)
            if not isinstance(value, str) or _decimal(value) < 0:
                raise DemoSafetyError("Demo commission-rate response is incomplete or invalid")
        # Keep the original decimal text and Binance field names.  Commission
        # rates are a current snapshot; realized fees still come from fills.
        return {field: raw[field] for field in (
            "symbol", "makerCommissionRate", "takerCommissionRate")}

    def fetch_symbol_config(self, pair: str | None = None) -> list[dict]:
        params = {"symbol": _symbol(pair)} if pair is not None else {}
        raw = self._call("symbolConfig", "fapiPrivate", "GET", params)
        if not isinstance(raw, list) or not raw:
            raise DemoSafetyError("Demo symbol config response is incomplete")
        results = []
        for item in raw:
            if not isinstance(item, dict):
                raise DemoSafetyError("Demo symbol config response is incomplete")
            symbol = item.get("symbol")
            leverage = item.get("leverage")
            if (not isinstance(symbol, str) or not symbol or isinstance(leverage, bool)
                    or not isinstance(leverage, (str, int))
                    or not re.fullmatch(r"[0-9]+", str(leverage)) or int(leverage) <= 0
                    or item.get("marginType") not in {"CROSSED", "ISOLATED"}):
                raise DemoSafetyError("Demo symbol config response is incomplete")
            if pair is not None and symbol != _symbol(pair):
                raise DemoSafetyError("Demo symbol config identity mismatch")
            results.append({"pair": SYMBOL_PAIRS.get(symbol, symbol), "symbol": symbol,
                            "allowed_pair": symbol in SYMBOL_PAIRS, "leverage": int(leverage),
                            "margin_type": item["marginType"],
                            "is_auto_add_margin": _optional_bool(item.get("isAutoAddMargin")),
                            "max_notional_value": _number(item.get("maxNotionalValue")),
                            "raw": sanitize_response(item)})
        if pair is not None and len(results) != 1:
            raise DemoSafetyError("Demo symbol config requested identity is not unique")
        return results

    def fetch_leverage_bracket(self) -> dict[str, Any]:
        """Read and validate the account-scoped leverage-bracket snapshot.

        Binance returns every symbol for this USER_DATA endpoint.  The
        execution contract only accepts the three explicitly supported
        USDT-M pairs, and validates their complete, contiguous bracket
        schedules before returning a credential-free evidence object.
        """
        raw = self._call("leverageBracket", "fapiPrivate", "GET")
        if not isinstance(raw, list) or not raw:
            raise DemoSafetyError("Demo leverage-bracket response is incomplete")

        expected_symbols = tuple(PAIR_SYMBOLS[pair] for pair in ALLOWED_PAIRS)
        schedules: dict[str, dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, Mapping):
                raise DemoSafetyError("Demo leverage-bracket response contains an invalid symbol")
            symbol = item.get("symbol")
            if symbol not in expected_symbols:
                continue
            if symbol in schedules:
                raise DemoSafetyError("Demo leverage-bracket response contains duplicate symbols")
            brackets = item.get("brackets")
            if not isinstance(brackets, list) or not brackets:
                raise DemoSafetyError("Demo leverage-bracket schedule is incomplete")
            coefficient = item.get("notionalCoef")
            if coefficient is not None and _bracket_decimal(
                    coefficient, f"{symbol}.notionalCoef", positive=True) != Decimal("1"):
                raise DemoSafetyError("Demo leverage-bracket notional coefficient is unsupported")

            normalized: list[dict[str, Any]] = []
            previous: dict[str, Decimal] | None = None
            for position, bracket in enumerate(brackets, start=1):
                if not isinstance(bracket, Mapping):
                    raise DemoSafetyError("Demo leverage-bracket band is invalid")
                required = ("bracket", "initialLeverage", "notionalCap", "notionalFloor",
                            "maintMarginRatio", "cum")
                if any(field not in bracket for field in required):
                    raise DemoSafetyError("Demo leverage-bracket band is incomplete")
                number = _bracket_integer(bracket["bracket"], f"{symbol}.bracket")
                initial = _bracket_integer(bracket["initialLeverage"],
                                           f"{symbol}.initialLeverage")
                floor = _bracket_decimal(bracket["notionalFloor"],
                                         f"{symbol}.notionalFloor", nonnegative=True)
                cap = _bracket_decimal(bracket["notionalCap"],
                                       f"{symbol}.notionalCap", positive=True)
                mmr = _bracket_decimal(bracket["maintMarginRatio"],
                                       f"{symbol}.maintMarginRatio")
                cumulative = _bracket_decimal(bracket["cum"], f"{symbol}.cum",
                                              nonnegative=True)
                if number != position or cap <= floor or not (Decimal("0") < mmr < Decimal("1")):
                    raise DemoSafetyError("Demo leverage-bracket bands are not ordered")
                if position == 1 and floor != 0:
                    raise DemoSafetyError("Demo leverage-bracket bands must start at zero")
                if previous is not None:
                    if floor != previous["cap"]:
                        raise DemoSafetyError("Demo leverage-bracket bands are not contiguous")
                    previous_offset = previous["cap"] * previous["mmr"] - previous["cum"]
                    current_offset = floor * mmr - cumulative
                    if previous_offset != current_offset:
                        raise DemoSafetyError("Demo leverage-bracket maintenance offsets are discontinuous")
                previous = {"cap": cap, "mmr": mmr, "cum": cumulative}
                normalized.append({
                    "bracket": number,
                    "initialLeverage": initial,
                    "notionalCap": format(cap, "f"),
                    "notionalFloor": format(floor, "f"),
                    "maintMarginRatio": format(mmr, "f"),
                    "cum": format(cumulative, "f"),
                })
            schedules[symbol] = {
                "symbol": symbol,
                "notionalCoef": None if coefficient is None else format(
                    _bracket_decimal(coefficient, f"{symbol}.notionalCoef"), "f"),
                "brackets": normalized,
            }

        missing = [symbol for symbol in expected_symbols if symbol not in schedules]
        if missing:
            raise DemoSafetyError("Demo leverage-bracket response is missing supported symbols")

        selected = [schedules[symbol] for symbol in expected_symbols]
        canonical = json.dumps(selected, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        raw_response_hash = getattr(self._client, "_quant_last_response_sha256", None)
        raw_hash_basis = getattr(self._client, "_quant_last_response_hash_basis", None)
        if (not isinstance(raw_response_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", raw_response_hash)
                or raw_hash_basis != "raw_http_response_bytes"):
            # Injected offline clients do not expose transport bytes.  Keep a
            # deterministic fallback for unit tests, but mark it explicitly
            # so callers cannot mistake it for exchange response evidence.
            response_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            response_hash_basis = "canonical_selected_symbols_json"
        else:
            response_hash = raw_response_hash
            response_hash_basis = raw_hash_basis
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        request_id = f"leverage-bracket-{int(time.time() * 1000)}-{response_hash[:12]}"
        return {
            "environment": "demo",
            "endpoint": "/fapi/v1/leverageBracket",
            "http_method": "GET",
            "security_type": "USER_DATA",
            "request_id": request_id,
            "fetched_at_utc": fetched_at,
            "signed_at_utc": self.last_call.get("started_at_utc"),
            "response_sha256": response_hash,
            "response_hash_basis": response_hash_basis,
            "request_latency_ms": self.last_call["latency_ms"],
            "symbols": selected,
            "raw": sanitize_response(selected),
        }

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
                 # positionRisk 不保证返回完整配置。仅透传该响应明确给出的
                 # 字段；缺失值不能作为逐仓、低杠杆或关闭自动追加的证明。
                 "leverage": _optional_leverage(item.get("leverage")),
                 "margin_type": _optional_margin_type(item.get("marginType")),
                 "is_auto_add_margin": _optional_bool(item.get("isAutoAddMargin")),
                 "raw": sanitize_response(item)} for item in raw]

    def fetch_balance(self) -> dict:
        raw = self._call("account", "fapiPrivateV3", "GET")
        result = {"wallet_balance": _number(raw.get("totalWalletBalance")),
                "margin_balance": _number(raw.get("totalMarginBalance")),
                "available_balance": _number(raw.get("availableBalance")),
                "assets": sanitize_response(raw.get("assets", [])),
                "positions": sanitize_response(raw.get("positions", [])),
                "raw": sanitize_response(raw)}
        # Demo account risk evidence may be supplied by a reviewed account
        # reconciler alongside the Binance account response. Never infer these
        # fields from wallet balance; absent values remain unknown.
        for key in ("cumulative_external_net_flow_usdt", "funding_fee_complete",
                    "data_complete", "risk_observation_id", "risk_day_opening"):
            if key in raw:
                result[key] = raw[key]
        return result

    def test_limit_order(self, pair: str, side: str, quantity: Any, price: Any,
                         *, position_side: str = "BOTH") -> dict:
        """Validate a signed LIMIT/GTC request without sending it to matching.

        Binance's order/test endpoint checks the request but creates no order.
        Success verifies this test request, not real submission, margin checks,
        or fills.  This diagnostic is allowed in read-only mode and neither
        claims a client ID nor changes the ordinary submission gate.
        """
        self._guard_client()
        if not self._credentials_ready:
            raise DemoSafetyError("explicit Demo credentials are required")
        symbol = _symbol(pair)
        if side not in {"buy", "sell"}:
            raise DemoSafetyError("invalid order side")
        if position_side not in ("BOTH", "LONG", "SHORT"):
            raise DemoSafetyError("invalid position side")
        qty, px = self.validate_order(pair, quantity, price)
        raw = self._call("order/test", "fapiPrivate", "POST", {
            "symbol": symbol, "side": side.upper(), "type": "LIMIT",
            "timeInForce": "GTC", "quantity": qty, "price": px,
            "positionSide": position_side,
        })
        # Demo also returns a fully empty order-shaped template.  Accept only
        # the observed zero/empty shape, never an actual order or an unfamiliar
        # response.  Exact types prevent bool False from passing as integer 0.
        empty_template = {
            "orderId": 0, "symbol": "", "status": "", "clientOrderId": "",
            "price": "", "origQty": "", "executedQty": "", "timeInForce": "",
            "type": "", "reduceOnly": False, "closePosition": False, "side": "",
            "stopPrice": "", "priceProtect": False, "origType": "", "updateTime": 0,
        }
        if (not isinstance(raw, dict)
                or (raw and (raw.keys() != empty_template.keys()
                             or any(type(value) is not type(empty_template[field])
                                    or value != empty_template[field]
                                    for field, value in raw.items())))):
            raise DemoSafetyError("invalid Binance Demo test-order response")
        return sanitize_response(raw)

    def _submit(self, pair, side, quantity, price, client_order_id, *, market,
                post_only=True, reduce_only=False, time_in_force=None):
        self._guard_client()
        if not self.allow_trading:
            raise DemoSafetyError("Demo adapter is read-only")
        if not self._credentials_ready:
            raise DemoSafetyError("explicit Demo credentials are required")
        if side not in {"buy", "sell"}:
            raise DemoSafetyError("invalid order side")
        if time_in_force not in (None, "GTX", "GTC", "IOC"):
            raise DemoSafetyError("unsupported Demo time in force")
        if ((market and time_in_force is not None)
                or (not market and post_only and time_in_force not in (None, "GTX"))
                or (not market and not post_only and time_in_force == "GTX")):
            raise DemoSafetyError("Demo time in force conflicts with order semantics")
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
            params.update(price=px, timeInForce=time_in_force or ("GTX" if post_only else "GTC"))
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
            # Post-only matching failures are deterministic exchange
            # rejections: the order was not accepted as a resting order.
            # Keep the client id permanently claimed, but do not turn this
            # into an exposure-unknown lock that blocks every session.
            definite_type = kind in {"InvalidOrder", "OrderNotFillable",
                                     "OrderImmediatelyFillable", "InsufficientFunds", "BadSymbol",
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

    def submit_ioc_entry(self, pair: str, side: str, quantity: Any, limit_price: Any,
                         client_order_id: str) -> dict:
        """Submit a price-bounded LIMIT/IOC entry, possibly partial or unfilled.

        A crossing limit is a taker probe; this is not an unlimited MARKET
        order.  Actual user-trade flags determine maker/taker classification.
        The runner must durably claim the client ID before this single attempt.
        """
        return self._submit(pair, side, quantity, limit_price, client_order_id,
                            market=False, post_only=False, reduce_only=False,
                            time_in_force="IOC")

    def submit_market_order(self, pair: str, side: str, quantity: Any,
                            client_order_id: str, *, reduce_only: bool = True) -> dict:
        if reduce_only is not True:
            raise DemoSafetyError("market orders are only permitted for reduce-only protective exits")
        return self._submit(pair, side, quantity, None, client_order_id,
                            market=True, reduce_only=True)

    def _submit_conditional(self, pair, side, quantity, trigger_price, client_algo_id,
                            *, order_type, working_type, price_protect, reduce_only):
        """只发送一次数量明确的原生退出；调用方必须先持久化并 claim 意图。"""
        self._guard_client()
        if not self.allow_trading:
            raise DemoSafetyError("Demo adapter is read-only")
        if not self._credentials_ready:
            raise DemoSafetyError("explicit Demo credentials are required")
        if (not isinstance(pair, str) or pair not in PAIR_SYMBOLS
                or not isinstance(side, str) or side not in {"buy", "sell"}
                or reduce_only is not True or not isinstance(working_type, str)
                or working_type not in {"MARK_PRICE", "CONTRACT_PRICE"}
                or not isinstance(price_protect, bool)):
            raise DemoSafetyError("invalid native protection semantics")
        if any(isinstance(value, bool) or not isinstance(value, (str, int, Decimal))
               for value in (quantity, trigger_price)):
            raise DemoSafetyError("native protection requires exact Decimal inputs")
        _check_client_id(client_algo_id)
        with self._attempt_lock:
            if client_algo_id in self._attempts:
                raise DemoSafetyError("client order ID was already attempted; reconcile without resending")
        # 保护市价退出不受入口最小名义额限制；数量不能静默取整留下裸露余量。
        qty, _ = self.validate_order(pair, quantity, market=True)
        px = _decimal(trigger_price, positive=True)
        filters = {row["filterType"]: row for row in self._market_rules[_symbol(pair)]["filters"]}
        price_filter = filters.get("PRICE_FILTER")
        if price_filter is None:
            raise DemoSafetyError("Demo trigger-price rules unavailable")
        tick = _decimal(price_filter["tickSize"], positive=True)
        if (px < _decimal(price_filter["minPrice"])
                or px > _decimal(price_filter["maxPrice"]) or px % tick):
            raise DemoSafetyError("trigger price violates Demo exchange rules")
        params = {"algoType": "CONDITIONAL", "symbol": _symbol(pair),
                  "side": side.upper(), "positionSide": "BOTH",
                  "type": order_type, "quantity": qty,
                  "triggerPrice": format(px, "f"), "reduceOnly": "true",
                  "clientAlgoId": client_algo_id, "workingType": working_type,
                  "priceProtect": "true" if price_protect else "false"}
        with self._attempt_lock:
            if client_algo_id in self._attempts:
                raise DemoSafetyError("client order ID was already attempted; reconcile without resending")
            self._attempts[client_algo_id] = "unknown"
            self._owned_client_ids.add(client_algo_id)
        try:
            raw = self._call("algoOrder", "fapiPrivate", "POST", params)
            result = normalize_conditional_order(raw, pair, self.last_call["latency_ms"])
            if (result["client_algo_id"] != client_algo_id
                    or result["algo_type"] != "CONDITIONAL"
                    or result["side"] != side or result["order_type"] != order_type
                    or result["position_side"] != "BOTH"
                    or _decimal(result["quantity"]) != _decimal(qty)
                    or _decimal(result["trigger_price"]) != px
                    or result["reduce_only"] is not True
                    or result["close_position"] is not False
                    or result["working_type"] != working_type
                    or result["price_protect"] is not price_protect
                    or result["algo_status"] not in {"NEW", "TRIGGERING", "TRIGGERED", "FINISHED"}):
                raise DemoSafetyError("Demo native protection acknowledgement mismatch")
        except Exception as exc:
            kind = exc.error_kind if isinstance(exc, DemoRequestError) else type(exc).__name__
            latency = exc.latency_ms if isinstance(exc, DemoRequestError) else 0
            code = exc.exchange_code if isinstance(exc, DemoRequestError) else None
            # 重复客户端身份可能指向已存在的保护，不得作为“没有订单”释放。
            if kind in {"InvalidOrder", "InsufficientFunds", "BadSymbol", "AuthenticationError",
                        "PermissionDenied", "AccountSuspended", "ArgumentsRequired",
                        "BadRequest", "OperationRejected"} and code not in {-4111, -4116, -2010}:
                self._attempts[client_algo_id] = "rejected"
                LOGGER.warning("原生条件保护提交被明确拒绝 币种=%s 方向=%s 类型=%s 客户端=%s 数量=%s 触发价=%s 错误类型=%s 代码=%s",
                               _symbol(pair), side, order_type, client_algo_id, qty, format(px, "f"), kind, code)
                raise DemoExchangeRejected(kind, latency, code) from None
            LOGGER.warning("原生条件保护提交结果未知，保留身份且只允许查询 币种=%s 方向=%s 类型=%s 客户端=%s 数量=%s 触发价=%s 错误类型=%s 代码=%s",
                           _symbol(pair), side, order_type, client_algo_id, qty, format(px, "f"), kind, code)
            raise DemoSubmissionUnknown(kind, latency, code) from None
        self._attempts[client_algo_id] = "confirmed"
        LOGGER.info("原生条件保护提交已确认 币种=%s 方向=%s 类型=%s 客户端=%s 算法单=%s 状态=%s 数量=%s 触发价=%s 触发基准=%s 价格保护=%s；尚未证明退出成交",
                    _symbol(pair), side, order_type, client_algo_id, result["algo_id"],
                    result["algo_status"], qty, format(px, "f"), working_type, price_protect)
        return result

    def submit_stop_market_order(self, pair, side, quantity, trigger_price, client_algo_id,
                                 *, working_type, price_protect, reduce_only=True):
        return self._submit_conditional(pair, side, quantity, trigger_price, client_algo_id,
            order_type="STOP_MARKET", working_type=working_type,
            price_protect=price_protect, reduce_only=reduce_only)

    def submit_take_profit_order(self, pair, side, quantity, trigger_price, client_algo_id,
                                 *, working_type, price_protect, reduce_only=True):
        return self._submit_conditional(pair, side, quantity, trigger_price, client_algo_id,
            order_type="TAKE_PROFIT_MARKET", working_type=working_type,
            price_protect=price_protect, reduce_only=reduce_only)

    def fetch_conditional_order(self, pair, client_algo_id):
        _symbol(pair)
        _check_client_id(client_algo_id)
        try:
            raw = self._call("algoOrder", "fapiPrivate", "GET", {"clientAlgoId": client_algo_id})
        except DemoRequestError as exc:
            if exc.error_kind == "OrderNotFound":
                return None
            raise
        result = normalize_conditional_order(raw, pair, self.last_call["latency_ms"])
        if result["client_algo_id"] != client_algo_id:
            raise DemoSafetyError("Demo conditional-order client identity mismatch")
        if client_algo_id in self._attempts:
            self._attempts[client_algo_id] = "confirmed"
        return result

    def fetch_open_conditional_orders(self, pair=None, *, with_evidence=False):
        """算法单原文与请求证据原子配对；未知保护语义仍保持未知。"""
        params = {} if pair is None else {"symbol": _symbol(pair)}
        def validate(raw):
            rows = _open_order_rows(raw, pair=pair, conditional=True, strict=with_evidence)
            result = []
            for item in rows:
                actual_pair = SYMBOL_PAIRS.get(item["symbol"], item["symbol"])
                result.append(normalize_conditional_order(item, actual_pair, self.last_call["latency_ms"],
                    allow_external=actual_pair not in PAIR_SYMBOLS))
            return result
        result = self._read_archive("openAlgoOrders", "fapiPrivate", params, validate,
                                    with_evidence=with_evidence)
        LOGGER.info("Demo条件挂单只读快照已读取 范围=%s 算法单数=%s 请求证据封套=%s；触发与结束不等于退出成交",
                    "全账户" if pair is None else pair,
                    len(result["data"] if with_evidence else result), with_evidence)
        return result

    def fetch_conditional_child_order(self, pair, client_algo_id):
        """由自有算法单的 actualOrderId 查询真实子单，不假设客户端 ID 相同。"""
        _check_client_id(client_algo_id)
        if client_algo_id not in self._owned_client_ids:
            raise DemoSafetyError("child lookup requires an owned durable order ledger identity")
        parent = self.fetch_conditional_order(pair, client_algo_id)
        if parent is None or parent["actual_order_id"] is None:
            return None
        try:
            raw = self._call("order", "fapiPrivate", "GET",
                {"symbol": _symbol(pair), "orderId": parent["actual_order_id"]})
        except DemoRequestError as exc:
            if exc.error_kind == "OrderNotFound":
                return None
            raise
        result = normalize_order(raw, pair, self.last_call["latency_ms"])
        if (result["order_id"] != parent["actual_order_id"]
                or result["side"] != parent["side"]
                or _optional_bool(raw.get("reduceOnly")) is not True):
            raise DemoSafetyError("Demo conditional child-order identity or reduction mismatch")
        return {**result, "parent_client_algo_id": client_algo_id,
                "parent_algo_id": parent["algo_id"]}

    def fetch_conditional_orders(self, pair, *, since_ms=None, until_ms=None,
                                  from_algo_id=None, limit=1000):
        params = {"symbol": _symbol(pair), "limit": _limit(limit)}
        if since_ms is not None:
            params["startTime"] = _timestamp(since_ms)
        if until_ms is not None:
            params["endTime"] = _timestamp(until_ms)
            if since_ms is None or not 0 < until_ms - since_ms < 7 * 86400000:
                raise DemoSafetyError("Demo conditional history requires a bounded window below seven days")
        if from_algo_id is not None:
            params["algoId"] = _identifier(from_algo_id)
        raw = self._call("allAlgoOrders", "fapiPrivate", "GET", params)
        if not isinstance(raw, list):
            raise DemoSafetyError("invalid Demo conditional-order history")
        return [normalize_conditional_order(item, pair, self.last_call["latency_ms"]) for item in raw]

    def cancel_conditional_order(self, pair, algo_id=None, *, client_algo_id):
        if not self.allow_trading:
            raise DemoSafetyError("Demo adapter is read-only")
        _check_client_id(client_algo_id)
        if client_algo_id not in self._owned_client_ids:
            raise DemoSafetyError("cancel requires an owned durable order ledger identity")
        observed = self.fetch_conditional_order(pair, client_algo_id)
        if observed is None:
            return None
        if algo_id is not None and _identifier(algo_id) != observed["algo_id"]:
            raise DemoSafetyError("cancel algo ID does not match the owned client ID")
        if observed["algo_status"] in {"CANCELED", "EXPIRED", "REJECTED", "FINISHED"}:
            return observed
        raw = self._call("algoOrder", "fapiPrivate", "DELETE", {"clientAlgoId": client_algo_id})
        try:
            valid_code = (isinstance(raw, dict) and
                          ((type(raw.get("code")) is int and raw["code"] == 200)
                           or (type(raw.get("code")) is str and raw["code"] == "200")))
            if (not valid_code or raw.get("clientAlgoId") != client_algo_id
                    or _identifier(raw.get("algoId")) != observed["algo_id"]):
                raise DemoSafetyError("Demo conditional-order cancel acknowledgement mismatch")
        except DemoSafetyError:
            # DELETE 已经发出，不能返回“本地拒绝且没有写入”的 safety 语义。
            LOGGER.warning("原生条件单撤销结果未知，保留风险并继续查询 币种=%s 客户端=%s 算法单=%s",
                           _symbol(pair), client_algo_id, observed["algo_id"])
            raise DemoRequestError("conditional_cancel_unknown", self.last_call["latency_ms"]) from None
        # 命令回执未证明撤销完成；协调器必须再查算法单、子单、成交与持仓。
        LOGGER.info("原生条件单撤销命令已接受，等待事实对账 币种=%s 客户端=%s 算法单=%s 原状态=%s",
                    _symbol(pair), client_algo_id, observed["algo_id"], observed["algo_status"])
        return {**observed, "cancel_acknowledged": True, "cancellation_confirmed": False,
                "cancel_raw": sanitize_response(raw)}

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

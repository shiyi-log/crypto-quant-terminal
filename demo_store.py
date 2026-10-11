"""Isolated, restart-safe audit ledger for official Binance Demo Trading.

Construction never opens a connection. Call ``ensure_schema`` explicitly before
using the ledger. Credentials, request signatures and private HTTP headers are
not accepted as ledger identity or raw exchange evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from data_store import _decimal, _serializable, _timestamp

ENVIRONMENT = "binance_demo"
ENDPOINT_HOST = "demo-fapi.binance.com"
TERMINAL_STATUSES = frozenset({"filled", "canceled", "rejected", "expired"})
PENDING_STATUSES = ("intent", "submitting", "unknown", "open", "partially_filled")
MAX_PAYLOAD_BYTES = 262_144
RAW_FIELDS = frozenset({
    "orderId", "clientOrderId", "symbol", "side", "type", "status", "origQty",
    "executedQty", "cumQty", "cumQuote", "avgPrice", "price", "stopPrice", "time",
    "updateTime", "transactTime", "timeInForce", "reduceOnly", "closePosition",
    "positionSide", "workingType", "priceProtect", "origType", "activatePrice",
    "priceRate", "priceMatch", "selfTradePreventionMode", "goodTillDate", "id", "tradeId",
    "qty", "quoteQty", "commission", "commissionAsset", "realizedPnl", "buyer",
    "maker", "isMaker", "accountAlias", "asset", "walletBalance", "crossWalletBalance",
    "balance", "availableBalance", "maxWithdrawAmount", "marginAvailable",
    "positionAmt", "entryPrice", "breakEvenPrice", "markPrice", "unRealizedProfit",
    "unrealizedProfit", "liquidationPrice", "leverage", "marginType", "isolatedMargin",
    "isolatedWallet", "notional", "isAutoAddMargin", "initialMargin", "maintMargin",
    "totalWalletBalance", "totalUnrealizedProfit", "totalMarginBalance", "canTrade",
    "totalInitialMargin", "totalPositionInitialMargin", "totalOpenOrderInitialMargin",
    "totalCrossWalletBalance", "totalCrossUnPnl", "assets", "positions", "code", "msg",
})
_SECRET_KEYS = re.compile(r"api.?key|secret|signature|authorization|cookie|password|credential|token|request|headers", re.I)
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)


def _text(value, name, maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError(f"invalid {name}")
    return value


def _scrub_text(value):
    def url(match):
        parts = urlsplit(match.group())
        # Never retain signed query strings, userinfo or URL fragments.
        return urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))
    value = _URL.sub(url, value)
    value = re.sub(r"(?i)\b(bearer|basic)\s+\S+", r"\1 [redacted]", value)
    value = re.sub(r'''(?i)(["']?(?:api.?key|secret|signature|token|password|authorization)["']?\s*[=:]\s*)(?:["'][^"']*["']|[^\s&,;}]+)''',
                   r"\1[redacted]", value)
    return value


def scrub_payload(payload):
    """Scrub comparison/snapshot metadata; exchange raw uses a strict allowlist."""
    if isinstance(payload, dict):
        return {str(key): scrub_payload(value) for key, value in payload.items()
                if not _SECRET_KEYS.search(str(key))}
    if isinstance(payload, (list, tuple)):
        return [scrub_payload(value) for value in payload]
    if isinstance(payload, str):
        return _scrub_text(payload)
    if payload is None or isinstance(payload, (int, float, bool)):
        return payload
    return _serializable(payload)


def scrub_exchange_raw(payload):
    """Retain Binance response facts only; drop transport/config/request fields."""
    if isinstance(payload, list):
        return [scrub_exchange_raw(value) for value in payload]
    if not isinstance(payload, dict):
        return {}
    result = {}
    for key, value in payload.items():
        if key in RAW_FIELDS:
            result[key] = (scrub_exchange_raw(value) if isinstance(value, (dict, list)) else scrub_payload(value))
    # CCXT ``info`` holds the original Binance response, never request metadata.
    if isinstance(payload.get("info"), dict):
        result.update(scrub_exchange_raw(payload["info"]))
    return result


def _payload(value, *, exchange=False):
    cleaned = scrub_exchange_raw(value) if exchange else scrub_payload(value)
    encoded = json.dumps(cleaned, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("demo audit payload exceeds 256 KiB")
    return cleaned


def _digest(value):
    return hashlib.sha256(json.dumps(_serializable(value), sort_keys=True, allow_nan=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _number(value, name, *, positive=False, nullable=False):
    if value is None and nullable:
        return None
    result = _decimal(value, name)
    if positive and result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _version(value):
    return -1 if value is None or value == -1 else _timestamp(value)


def _limit(value):
    result = _timestamp(value)
    if not 1 <= result <= 1000:
        raise ValueError("query limit must be between 1 and 1000")
    return result


def _field(payload, snake, camel=None, default=None):
    return payload.get(snake, payload.get(camel, default) if camel else default)


def _symbol(pair):
    return str(pair).replace("/", "").replace(":USDT", "")


class DemoStore:
    """All writes are separate committed transactions using DataStore's pool.

    A virtual account key is a research/session identity, NOT an API credential.
    An externally submitted intent can never be claimed a second time; timeout
    recovery reconciles by its stable client id instead of resubmitting.
    """
    def __init__(self, data_store, virtual_account_key="demo_default", *,
                 environment=ENVIRONMENT, endpoint_host=ENDPOINT_HOST):
        if environment != ENVIRONMENT or endpoint_host != ENDPOINT_HOST:
            raise ValueError("DemoStore accepts only official Binance Demo Futures")
        self.store = data_store
        self.virtual_account_key = _text(virtual_account_key, "virtual_account_key", 128)
        self.environment, self.endpoint_host = environment, endpoint_host

    def ensure_schema(self):
        with self.store._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"demo-schema:{self.store.schema}",))
            conn.execute((Path(__file__).parent / "database" / "demo_schema.sql").read_text(encoding="utf-8"), prepare=False)

    def _identity(self):
        return self.environment, self.virtual_account_key

    def _order(self, conn, client_id, *, lock=False):
        return conn.execute("SELECT * FROM demo_orders WHERE environment=%s AND virtual_account_key=%s AND client_id=%s"
                            + (" FOR UPDATE" if lock else ""), (*self._identity(), client_id)).fetchone()

    def get_order(self, client_id):
        with self.store._connection() as conn:
            row = self._order(conn, _text(client_id, "client_id", 36))
        return _serializable(row)

    def reserve_order(self, intent):
        """Commit a frozen intent before any network call; duplicates reuse it.

        Returns ``(row, created)``. A collision with different immutable intent
        facts raises rather than silently borrowing an unrelated old order.
        """
        if intent.get("environment", self.environment) != self.environment or intent.get("endpoint_host", self.endpoint_host) != self.endpoint_host:
            raise ValueError("wrong Demo environment or endpoint")
        if intent.get("virtual_account_key", self.virtual_account_key) != self.virtual_account_key:
            raise ValueError("virtual account mismatch")
        fixed = {name: _text(intent.get(name), name) for name in
                 ("strategy_id", "signal_id", "rule_hash", "execution_hash", "input_fingerprint")}
        fixed["intent_key"] = _text(intent.get("intent_key", "entry"), "intent_key", 128)
        fixed["pair"] = _text(intent.get("pair", intent.get("symbol")), "pair", 64)
        if fixed["pair"] not in {"BTC/USDT:USDT", "ETH/USDT:USDT", "TRX/USDT:USDT", "BTCUSDT", "ETHUSDT", "TRXUSDT"}:
            raise ValueError("Demo research permits only BTC/ETH/TRX USDT Futures")
        fixed["pair"] = {"BTCUSDT": "BTC/USDT:USDT", "ETHUSDT": "ETH/USDT:USDT",
                         "TRXUSDT": "TRX/USDT:USDT"}.get(fixed["pair"], fixed["pair"])
        fixed["side"] = str(intent.get("side", "")).lower()
        fixed["order_type"] = str(intent.get("order_type", intent.get("type", ""))).lower()
        if fixed["side"] not in {"buy", "sell"} or fixed["order_type"] not in {"limit", "market"}:
            raise ValueError("unsupported side/order type")
        fixed["quantity"] = _number(intent.get("quantity", intent.get("qty")), "quantity", positive=True)
        fixed["price"] = _number(intent.get("price"), "price", positive=True, nullable=True)
        if fixed["order_type"] == "limit" and fixed["price"] is None:
            raise ValueError("limit order requires price")
        fixed["time_in_force"] = intent.get("time_in_force", intent.get("timeInForce"))
        if fixed["time_in_force"] not in {None, "GTX", "GTC", "IOC", "FOK"}:
            raise ValueError("unsupported time in force")
        fixed["reduce_only"] = intent.get("reduce_only", intent.get("reduceOnly", False))
        if not isinstance(fixed["reduce_only"], bool):
            raise ValueError("reduce_only must be boolean")
        fixed["signal_ms"] = _timestamp(intent.get("signal_ms", intent.get("timestamp_ms")))
        client_id = intent.get("client_id") or "demo_" + _digest({**fixed, "account": self.virtual_account_key})[:30]
        client_id = _text(client_id, "client_id", 36)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", client_id):
            raise ValueError("invalid stable Binance client id")
        names = tuple(fixed)
        raw = _payload(intent.get("raw_binance", {}), exchange=True)
        intent_payload = _payload(intent.get("intent_payload", intent.get("raw", {})))
        created_ms = _timestamp(intent.get("created_ms", int(time.time() * 1000)))
        with self.store._connection() as conn:
            row = conn.execute("INSERT INTO demo_orders (environment,virtual_account_key,client_id,endpoint_host,"
                               + ",".join(names) + ",created_ms,raw,intent_payload) VALUES ("
                               + ",".join(["%s"] * (len(names) + 7)) + ") ON CONFLICT DO NOTHING RETURNING *",
                               (*self._identity(), client_id, self.endpoint_host, *(fixed[name] for name in names),
                                created_ms, self.store._json(raw), self.store._json(intent_payload))).fetchone()
            if row is not None:
                return _serializable(row), True
            row = conn.execute("SELECT * FROM demo_orders WHERE environment=%s AND virtual_account_key=%s AND "
                               "(client_id=%s OR (strategy_id=%s AND signal_id=%s AND intent_key=%s)) FOR UPDATE",
                               (*self._identity(), client_id, fixed["strategy_id"], fixed["signal_id"], fixed["intent_key"])).fetchone()
            if (row is None or row["client_id"] != client_id or any(row[name] != fixed[name] for name in names)
                    or row["intent_payload"] != intent_payload):
                raise ValueError("duplicate client/signal identity has different frozen intent")
            return _serializable(row), False

    def claim_submission(self, client_id, claimed_ms=None):
        """Only the winner may call the exchange; crashed claims require lookup."""
        claimed_ms = _timestamp(int(time.time() * 1000) if claimed_ms is None else claimed_ms)
        with self.store._connection() as conn:
            row = conn.execute("UPDATE demo_orders SET status='submitting',submission_attempts=1,submission_claimed_ms=%s,"
                               "updated_at=CURRENT_TIMESTAMP WHERE environment=%s AND virtual_account_key=%s AND client_id=%s "
                               "AND status='intent' AND submission_attempts=0 AND exchange_order_id IS NULL RETURNING client_id",
                               (claimed_ms, *self._identity(), _text(client_id, "client_id", 36))).fetchone()
        return row is not None

    def abandon_unclaimed_order(self, client_id, reason, observed_ms=None):
        """Expire a never-submitted local intent; never imply exchange expiry."""
        observed_ms = _timestamp(int(time.time() * 1000) if observed_ms is None else observed_ms)
        evidence = _payload({"kind": "unclaimed_intent_abandoned", "reason": str(reason),
                             "observed_ms": observed_ms, "exchange_status_observed": False})
        evidence["observation_id"] = _digest(evidence)
        with self.store._connection() as conn:
            row = conn.execute("UPDATE demo_orders SET status='expired',audit_observations=audit_observations || %s,"
                               "updated_at=CURRENT_TIMESTAMP WHERE environment=%s AND virtual_account_key=%s AND client_id=%s "
                               "AND status='intent' AND submission_attempts=0 AND exchange_order_id IS NULL RETURNING client_id",
                               (self.store._json([evidence]), *self._identity(), _text(client_id, "client_id", 36))).fetchone()
        return row is not None

    def mark_submission_unknown(self, client_id, reason, observed_ms=None):
        observed_ms = _timestamp(int(time.time() * 1000) if observed_ms is None else observed_ms)
        evidence = _payload({"kind": "submission_unknown", "reason": str(reason), "observed_ms": observed_ms})
        with self.store._connection() as conn:
            row = self._order(conn, client_id, lock=True)
            if row is None:
                raise ValueError("unknown client id")
            if row["status"] == "submitting":
                conn.execute("UPDATE demo_orders SET status='unknown',audit_observations=audit_observations || %s,"
                             "updated_at=CURRENT_TIMESTAMP WHERE environment=%s AND virtual_account_key=%s AND client_id=%s",
                             (self.store._json([evidence]), *self._identity(), client_id))
        return self.get_order(client_id)

    def append_reconciliation(self, client_id, payload, observed_ms=None):
        """Append a lookup/error finding without rewriting any exchange facts."""
        observed_ms = _timestamp(int(time.time() * 1000) if observed_ms is None else observed_ms)
        evidence = _payload({"kind": "reconciliation", "observed_ms": observed_ms, "payload": payload})
        evidence["observation_id"] = _digest(evidence)
        with self.store._connection() as conn:
            row = self._order(conn, client_id, lock=True)
            if row is None:
                raise ValueError("reconciliation requires persisted intent")
            if not any(item.get("observation_id") == evidence["observation_id"] for item in row["audit_observations"]):
                conn.execute("UPDATE demo_orders SET audit_observations=audit_observations || %s,updated_at=CURRENT_TIMESTAMP "
                             "WHERE environment=%s AND virtual_account_key=%s AND client_id=%s",
                             (self.store._json([evidence]), *self._identity(), client_id))
        return evidence["observation_id"]

    def record_order_observation(self, client_id, observation):
        raw = _payload(observation.get("raw", observation.get("info", observation)), exchange=True)
        status = str(_field(observation, "status", default=raw.get("status", "unknown"))).lower()
        status = {"new": "open", "closed": "filled", "cancelled": "canceled", "partiallyfilled": "partially_filled",
                  "expired_in_match": "expired"}.get(status, status)
        if status not in {*PENDING_STATUSES, *TERMINAL_STATUSES}:
            raise ValueError("unknown exchange order status")
        filled = _number(_field(observation, "filled_quantity", "executedQty", observation.get("filled_qty", observation.get("filled", raw.get("executedQty", 0)))), "filled_quantity")
        if filled < 0:
            raise ValueError("filled quantity cannot be negative")
        average = _number(_field(observation, "average_price", "avgPrice", observation.get("avg_price", observation.get("average", raw.get("avgPrice")))), "average_price", nullable=True)
        if average == 0:
            average = None
        if average is not None and average < 0:
            raise ValueError("average_price cannot be negative")
        order_id = _field(observation, "exchange_order_id", "orderId", observation.get("order_id", observation.get("id", raw.get("orderId"))))
        order_id = None if order_id is None else _text(str(order_id), "exchange_order_id", 128)
        version = _version(_field(observation, "exchange_updated_ms", "updateTime", observation.get("updated_ms", raw.get("updateTime"))))
        evidence = _payload({"kind": "exchange_order", "status": status, "filled_quantity": str(filled),
                             "average_price": str(average) if average is not None else None,
                             "exchange_order_id": order_id, "exchange_updated_ms": version, "raw": raw})
        latency = observation.get("request_latency_ms")
        if latency is not None:
            latency = _number(latency, "request_latency_ms")
            if latency < 0:
                raise ValueError("request latency cannot be negative")
            evidence["request_latency_ms"] = str(latency)
        obs_id = _digest(evidence)
        with self.store._connection() as conn:
            row = self._order(conn, client_id, lock=True)
            if row is None:
                raise ValueError("order observation requires persisted intent")
            echoed = {"client_id": observation.get("client_id", raw.get("clientOrderId")),
                      "pair": observation.get("pair", raw.get("symbol")),
                      "side": observation.get("side", raw.get("side")),
                      "order_type": observation.get("type", raw.get("type")),
                      "quantity": observation.get("qty", raw.get("origQty"))}
            for name, value in echoed.items():
                if value is None:
                    continue
                actual = _symbol(value) if name == "pair" else (
                    _number(value, "origQty") if name == "quantity" else str(value).lower() if name in {"side", "order_type"} else value)
                expected = _symbol(row[name]) if name == "pair" else row[name]
                if actual != expected:
                    raise ValueError(f"exchange order {name} conflicts with persisted intent")
            if row["exchange_order_id"] and order_id and row["exchange_order_id"] != order_id:
                raise ValueError("exchange order id conflicts with persisted intent")
            if filled > row["quantity"]:
                raise ValueError("exchange filled quantity exceeds frozen order quantity")
            if status == "open" and filled > 0:
                status = "partially_filled"
            accepted = version >= row["exchange_updated_ms"] and filled >= row["filled_quantity"]
            if row["status"] in TERMINAL_STATUSES and status != row["status"]:
                accepted = False
            if status in {"intent", "submitting", "unknown"} and row["status"] not in {"intent", "submitting", "unknown"}:
                accepted = False
            if any(item.get("observation_id") == obs_id for item in row["audit_observations"]):
                return _serializable(row)
            audit = {**evidence, "observation_id": obs_id, "projection_accepted": accepted}
            conn.execute("UPDATE demo_orders SET audit_observations=audit_observations || %s,updated_at=CURRENT_TIMESTAMP "
                         "WHERE environment=%s AND virtual_account_key=%s AND client_id=%s",
                         (self.store._json([audit]), *self._identity(), client_id))
            if accepted:
                conn.execute("UPDATE demo_orders SET status=%s,filled_quantity=%s,average_price=COALESCE(%s,average_price),"
                             "exchange_order_id=COALESCE(%s,exchange_order_id),exchange_updated_ms=%s,raw=%s "
                             "WHERE environment=%s AND virtual_account_key=%s AND client_id=%s",
                             (status, filled, average, order_id, version, self.store._json(raw), *self._identity(), client_id))
            result = self._order(conn, client_id)
        return _serializable(result)

    def record_fill(self, client_id, fill):
        raw = _payload(fill.get("raw", fill.get("info", fill)), exchange=True)
        order_id = _field(fill, "exchange_order_id", "orderId", fill.get("order_id", fill.get("order", raw.get("orderId"))))
        trade_id = _field(fill, "exchange_trade_id", "id", fill.get("fill_id", raw.get("id", raw.get("tradeId"))))
        order_id, trade_id = _text(str(order_id), "exchange_order_id", 128), _text(str(trade_id), "exchange_trade_id", 128)
        if order_id == "None" or trade_id == "None":
            raise ValueError("actual fill requires exchange order and trade ids")
        price = _number(fill.get("price", raw.get("price")), "price", positive=True)
        quantity = _number(_field(fill, "quantity", "qty", fill.get("amount", raw.get("qty"))), "quantity", positive=True)
        commission = _number(fill.get("commission", raw.get("commission")), "commission", nullable=True)
        asset = _field(fill, "commission_asset", "commissionAsset", raw.get("commissionAsset"))
        if asset is not None:
            asset = _text(asset, "commission_asset", 32)
        maker = _field(fill, "is_maker", "maker", raw.get("maker", raw.get("isMaker")))
        if maker is not None and not isinstance(maker, bool):
            raise ValueError("maker must be actual boolean or unknown")
        realized = _number(_field(fill, "realized_pnl", "realizedPnl", raw.get("realizedPnl")), "realized_pnl", nullable=True)
        timestamp = _timestamp(_field(fill, "timestamp_ms", "time", fill.get("ts", fill.get("timestamp", raw.get("time")))))
        version = _version(_field(fill, "exchange_updated_ms", "updateTime", raw.get("updateTime", timestamp)))
        with self.store._connection() as conn:
            order = self._order(conn, client_id, lock=True)
            if order is None:
                raise ValueError("actual fill requires persisted order intent")
            if order["exchange_order_id"] and order["exchange_order_id"] != order_id:
                raise ValueError("fill order identity mismatch")
            pair = fill.get("pair", fill.get("symbol", raw.get("symbol", order["pair"])))
            if _symbol(pair) != _symbol(order["pair"]):
                raise ValueError("fill pair differs from order")
            fill_side = fill.get("side", raw.get("side"))
            if fill_side is not None and str(fill_side).lower() != order["side"]:
                raise ValueError("fill side differs from order")
            key = (*self._identity(), order["pair"], order_id, trade_id)
            existing = conn.execute("SELECT * FROM demo_fills WHERE environment=%s AND virtual_account_key=%s AND pair=%s "
                                    "AND exchange_order_id=%s AND exchange_trade_id=%s FOR UPDATE", key).fetchone()
            evidence = _payload({"commission": str(commission) if commission is not None else None,
                                 "commission_asset": asset, "is_maker": maker,
                                 "realized_pnl": str(realized) if realized is not None else None,
                                 "exchange_updated_ms": version, "raw": raw})
            evidence["observation_id"] = _digest(evidence)
            if existing is None:
                total = conn.execute("SELECT COALESCE(SUM(quantity),0) AS quantity FROM demo_fills WHERE environment=%s "
                                     "AND virtual_account_key=%s AND client_id=%s", (*self._identity(), client_id)).fetchone()["quantity"]
                if total + quantity > order["quantity"]:
                    raise ValueError("actual fills exceed frozen order quantity")
                conn.execute("INSERT INTO demo_fills (environment,virtual_account_key,pair,exchange_order_id,exchange_trade_id,"
                             "client_id,price,quantity,commission,commission_asset,is_maker,realized_pnl,timestamp_ms,"
                             "exchange_updated_ms,raw,audit_observations) VALUES (" + ",".join(["%s"] * 16) + ")",
                             (*key, client_id, price, quantity, commission, asset, maker, realized, timestamp, version,
                              self.store._json(raw), self.store._json([evidence])))
            else:
                if any(existing[name] != value for name, value in
                       (("client_id", client_id), ("price", price), ("quantity", quantity), ("timestamp_ms", timestamp))):
                    raise ValueError("fill identity has conflicting immutable execution facts")
                if any(item.get("observation_id") == evidence["observation_id"] for item in existing["audit_observations"]):
                    return _serializable(existing)
                newer = version > existing["exchange_updated_ms"]
                same = version == existing["exchange_updated_ms"]
                def project(name, incoming):
                    known = existing[name]
                    return incoming if incoming is not None and (newer or (same and known is None)) else known
                fee, fee_asset = project("commission", commission), project("commission_asset", asset)
                fee_conflict = existing["commission_conflict"]
                full_fee = commission is not None and asset is not None
                if full_fee and (newer or (same and fee_conflict)):
                    # A coherent new actual observation resolves a prior partial
                    # conflict; never pair a new currency with an old amount.
                    fee, fee_asset, fee_conflict = commission, asset, False
                elif newer:
                    currency_change = (asset is not None and existing["commission_asset"] is not None
                                       and asset != existing["commission_asset"] and commission is None)
                    unlabelled_amount_change = (commission is not None and existing["commission"] is not None
                                               and commission != existing["commission"] and asset is None
                                               and existing["commission_asset"] is not None)
                    if currency_change or unlabelled_amount_change:
                        fee, fee_asset, fee_conflict = existing["commission"], existing["commission_asset"], True
                evidence["projection_commission_conflict"] = fee_conflict
                projected_maker, projected_realized = project("is_maker", maker), project("realized_pnl", realized)
                conn.execute("UPDATE demo_fills SET commission=%s,commission_asset=%s,commission_conflict=%s,is_maker=%s,realized_pnl=%s,"
                             "exchange_updated_ms=GREATEST(exchange_updated_ms,%s),raw=CASE WHEN %s THEN %s ELSE raw END,"
                             "audit_observations=audit_observations || %s,updated_at=CURRENT_TIMESTAMP "
                             "WHERE environment=%s AND virtual_account_key=%s AND pair=%s AND exchange_order_id=%s AND exchange_trade_id=%s",
                             (fee, fee_asset, fee_conflict, projected_maker, projected_realized, version, newer or same,
                              self.store._json(raw), self.store._json([evidence]), *key))
            if order["exchange_order_id"] is None:
                conn.execute("UPDATE demo_orders SET exchange_order_id=%s WHERE environment=%s AND virtual_account_key=%s AND client_id=%s",
                             (order_id, *self._identity(), client_id))
            result = conn.execute("SELECT * FROM demo_fills WHERE environment=%s AND virtual_account_key=%s AND pair=%s "
                                  "AND exchange_order_id=%s AND exchange_trade_id=%s", key).fetchone()
        return _serializable(result)

    def list_orders(self, *, limit=1000, statuses=None):
        query = "SELECT * FROM demo_orders WHERE environment=%s AND virtual_account_key=%s"
        params = list(self._identity())
        if statuses is not None:
            if not statuses or any(status not in {*PENDING_STATUSES, *TERMINAL_STATUSES} for status in statuses):
                raise ValueError("invalid order statuses")
            query += " AND status=ANY(%s)"
            params.append(list(statuses))
        query += " ORDER BY created_ms DESC,client_id"
        if limit is not None:
            query += " LIMIT %s"
            params.append(_limit(limit))
        with self.store._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return _serializable(rows)

    def pending_orders(self, *, limit=1000):
        return self.list_orders(limit=limit, statuses=PENDING_STATUSES)

    def runner_orders(self):
        return self.list_orders(limit=None)

    def list_fills(self, client_id=None, *, limit=1000):
        query = "SELECT f.*,o.side,o.strategy_id,o.signal_id,o.reduce_only FROM demo_fills f JOIN demo_orders o USING "
        query += "(environment,virtual_account_key,client_id) WHERE f.environment=%s AND f.virtual_account_key=%s"
        params = list(self._identity())
        if client_id is not None:
            query += " AND f.client_id=%s"
            params.append(_text(client_id, "client_id", 36))
        query += " ORDER BY timestamp_ms DESC,exchange_trade_id"
        if limit is not None:
            query += " LIMIT %s"
            params.append(_limit(limit))
        with self.store._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return _serializable(rows)

    def runner_fills(self):
        return self.list_fills(limit=None)

    def net_fill_positions(self):
        """All-history quantities, never a truncated UI window or live ledger."""
        with self.store._connection() as conn:
            rows = conn.execute("SELECT f.pair,SUM(CASE WHEN o.side='buy' THEN f.quantity ELSE -f.quantity END) AS quantity "
                                "FROM demo_fills f JOIN demo_orders o USING (environment,virtual_account_key,client_id) "
                                "WHERE f.environment=%s AND f.virtual_account_key=%s GROUP BY f.pair ORDER BY f.pair",
                                self._identity()).fetchall()
        return {row["pair"]: str(row["quantity"]) for row in rows}

    def metrics(self):
        """Counts and asset-separated actual commission; unknown never becomes zero."""
        with self.store._connection() as conn:
            orders = conn.execute("SELECT status,COUNT(*) AS count FROM demo_orders WHERE environment=%s AND "
                                  "virtual_account_key=%s GROUP BY status", self._identity()).fetchall()
            fees = conn.execute("SELECT commission_asset,COUNT(*) AS fill_count,"
                                "CASE WHEN commission_asset IS NOT NULL AND COUNT(commission)=COUNT(*) AND NOT BOOL_OR(commission_conflict) THEN SUM(commission) END AS commission,"
                                "CASE WHEN commission_asset IS NOT NULL THEN SUM(commission) FILTER (WHERE NOT commission_conflict) END AS known_commission_subtotal,"
                                "COUNT(*) FILTER (WHERE commission IS NULL) AS unknown_commission_count,"
                                "COUNT(*) FILTER (WHERE commission_asset IS NULL) AS unknown_commission_asset_count,"
                                "COUNT(*) FILTER (WHERE commission_conflict) AS commission_conflict_count,"
                                "COUNT(*) FILTER (WHERE is_maker IS NULL) AS unknown_maker_count FROM demo_fills "
                                "WHERE environment=%s AND virtual_account_key=%s GROUP BY commission_asset ORDER BY commission_asset",
                                self._identity()).fetchall()
            entry_counts = conn.execute("SELECT COUNT(*) FILTER (WHERE NOT reduce_only AND status IN "
                                        "('filled','canceled','rejected','expired') AND (submission_attempts=1 OR exchange_order_id IS NOT NULL)) AS terminal_entry_count,"
                                        "COUNT(*) FILTER (WHERE NOT reduce_only AND status IN "
                                        "('filled','canceled','rejected','expired') AND filled_quantity=0 AND "
                                        "(submission_attempts=1 OR exchange_order_id IS NOT NULL)) AS unfilled_entry_count "
                                        "FROM demo_orders WHERE environment=%s AND virtual_account_key=%s", self._identity()).fetchone()
            latencies = conn.execute("SELECT audit_observations FROM demo_orders WHERE environment=%s AND virtual_account_key=%s",
                                     self._identity()).fetchall()
        known_latencies = [float(observation["request_latency_ms"]) for row in latencies
                           for observation in row["audit_observations"] if observation.get("request_latency_ms") is not None]
        counts = {row["status"]: row["count"] for row in orders}
        terminal, unfilled = entry_counts["terminal_entry_count"], entry_counts["unfilled_entry_count"]
        missing_amount = sum(row["unknown_commission_count"] for row in fees)
        missing_asset = sum(row["unknown_commission_asset_count"] for row in fees)
        conflicts = sum(row["commission_conflict_count"] for row in fees)
        return {"environment": self.environment, "virtual_account_key": self.virtual_account_key,
                "orders_by_status": counts,
                "actual_commission_by_asset": _serializable(fees),
                "fill_count": sum(row["fill_count"] for row in fees),
                "unknown_commission_count": missing_amount, "unknown_commission_asset_count": missing_asset,
                "commission_conflict_count": conflicts,
                "commission_complete": bool(fees) and missing_amount == 0 and missing_asset == 0 and conflicts == 0,
                "median_latency_ms": statistics.median(known_latencies) if known_latencies else None,
                "terminal_entry_count": terminal, "unfilled_entry_count": unfilled,
                "unfilled_entry_ratio": unfilled / terminal if terminal else None,
                "unresolved_order_count": sum(counts.get(status, 0) for status in PENDING_STATUSES),
                "has_exposure": any(_number(quantity, "net quantity") != 0 for quantity in self.net_fill_positions().values()),
                "execution_scope": "Demo results do not prove live execution or live profitability"}

    def _snapshot(self, table, payload, observed_ms):
        payload = _payload(payload)
        observed_ms = _timestamp(int(time.time() * 1000) if observed_ms is None else observed_ms)
        observation_id = _digest({"table": table, "account": self._identity(), "time": observed_ms, "payload": payload})
        with self.store._connection() as conn:
            conn.execute("INSERT INTO " + table + " (observation_id,environment,virtual_account_key,endpoint_host,observed_ms,payload) "
                         "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                         (observation_id, *self._identity(), self.endpoint_host, observed_ms, self.store._json(payload)))
        return observation_id

    def append_account_snapshot(self, payload, observed_ms=None):
        return self._snapshot("demo_account", payload, observed_ms)

    def append_position_snapshot(self, payload, observed_ms=None):
        return self._snapshot("demo_positions", payload, observed_ms)

    def _latest_snapshot(self, table):
        with self.store._connection() as conn:
            row = conn.execute("SELECT * FROM " + table + " WHERE environment=%s AND virtual_account_key=%s "
                               "ORDER BY observed_ms DESC,observation_id DESC LIMIT 1", self._identity()).fetchone()
        return _serializable(row)

    def get_latest_account_snapshot(self):
        return self._latest_snapshot("demo_account")

    def get_latest_position_snapshot(self):
        return self._latest_snapshot("demo_positions")

    def append_comparison(self, payload, stage, observed_ms=None):
        payload = _payload(payload)
        fields = tuple(_text(payload.get(name), name) for name in
                       ("strategy_id", "signal_id", "rule_hash", "execution_hash", "input_fingerprint"))
        stage = _text(stage, "comparison stage", 64)
        if stage == "binding":
            raise ValueError("reserved binding stage; use bind_account")
        observed_ms = _timestamp(int(time.time() * 1000) if observed_ms is None else observed_ms)
        identity = {"account": self._identity(), "fields": fields, "stage": stage, "time": observed_ms, "payload": payload}
        observation_id = _digest(identity)
        with self.store._connection() as conn:
            conn.execute("INSERT INTO demo_signal_comparisons (observation_id,environment,virtual_account_key,strategy_id,"
                         "signal_id,rule_hash,execution_hash,input_fingerprint,stage,observed_ms,payload) VALUES ("
                         + ",".join(["%s"] * 11) + ") ON CONFLICT DO NOTHING",
                         (observation_id, *self._identity(), *fields, stage, observed_ms, self.store._json(payload)))
        return observation_id

    def list_comparisons(self, *, stage=None, signal_id=None, limit=1000):
        query = "SELECT * FROM demo_signal_comparisons WHERE environment=%s AND virtual_account_key=%s"
        params = list(self._identity())
        for name, value in (("stage", stage), ("signal_id", signal_id)):
            if value is not None:
                query += " AND " + name + "=%s"
                params.append(_text(value, name))
        with self.store._connection() as conn:
            rows = conn.execute(query + " ORDER BY observed_ms DESC,observation_id DESC LIMIT %s", (*params, _limit(limit))).fetchall()
        return _serializable(rows)

    def bind_account(self, binding):
        """Freeze non-secret session/rule identity; never permits rebinding."""
        payload = _payload(binding)
        fields = tuple(_text(payload.get(name), name) for name in
                       ("strategy_id", "signal_id", "rule_hash", "execution_hash", "input_fingerprint"))
        with self.store._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("demo-binding:" + self.virtual_account_key,))
            old = conn.execute("SELECT payload FROM demo_signal_comparisons WHERE environment=%s AND virtual_account_key=%s "
                               "AND stage='binding' LIMIT 1", self._identity()).fetchone()
            if old is not None:
                if old["payload"] != payload:
                    raise ValueError("Demo virtual account is already bound to a different session")
                return old["payload"]
            observed_ms = _timestamp(payload.get("observed_ms", int(time.time() * 1000)))
            conn.execute("INSERT INTO demo_signal_comparisons (observation_id,environment,virtual_account_key,strategy_id,"
                         "signal_id,rule_hash,execution_hash,input_fingerprint,stage,observed_ms,payload) VALUES ("
                         + ",".join(["%s"] * 11) + ")",
                         (_digest({"account": self._identity(), "binding": payload}), *self._identity(), *fields,
                          "binding", observed_ms, self.store._json(payload)))
        return payload

    def get_binding(self):
        rows = self.list_comparisons(stage="binding", limit=1)
        return rows[0]["payload"] if rows else None


def binance_order(row):
    """Return Binance naming, preserving actual raw fields and decimal strings."""
    raw = dict(row.get("raw") or {})
    symbol = row["pair"].replace("/", "").replace(":USDT", "")
    status = {"open": "NEW", "partially_filled": "PARTIALLY_FILLED", "filled": "FILLED",
              "canceled": "CANCELED", "rejected": "REJECTED", "expired": "EXPIRED"}.get(row["status"])
    if row.get("exchange_order_id") is None and "status" not in raw:
        status = None
    fallback = {"orderId": row.get("exchange_order_id"), "clientOrderId": row["client_id"],
            "symbol": symbol, "side": row["side"].upper(), "type": row["order_type"].upper(),
            "status": status, "origQty": str(row["quantity"]), "executedQty": str(row["filled_quantity"]),
            "avgPrice": None if row.get("average_price") is None else str(row["average_price"]),
            "price": None if row.get("price") is None else str(row["price"]), "time": None,
            "updateTime": None if row["exchange_updated_ms"] < 0 else row["exchange_updated_ms"]}
    result = {**fallback, **raw}
    for key in ("origQty", "executedQty", "avgPrice", "price", "stopPrice", "cumQty", "cumQuote"):
        if result.get(key) is not None:
            result[key] = str(result[key])
    result["demoAudit"] = {"environment": row["environment"], "status": row["status"],
                           "strategyId": row["strategy_id"], "signalId": row["signal_id"],
                           "localCreatedMs": row["created_ms"],
                           "exchangeTimeObserved": "time" in raw,
                           "fallbackFields": sorted(set(fallback) - set(raw))}
    return result


def binance_fill(row):
    raw = dict(row.get("raw") or {})
    quote = _number(row["price"], "price") * _number(row["quantity"], "quantity")
    result = {"id": row["exchange_trade_id"], "orderId": row["exchange_order_id"],
            "symbol": row["pair"].replace("/", "").replace(":USDT", ""), "price": str(row["price"]),
            "qty": str(row["quantity"]), "quoteQty": str(quote),
            "commission": None if row.get("commission") is None else str(row["commission"]),
            "commissionAsset": row.get("commission_asset"), "realizedPnl": row.get("realized_pnl"),
            "buyer": None if row.get("side") is None else row["side"] == "buy",
            "maker": row.get("is_maker"), "time": row["timestamp_ms"]}
    # Price/quantity/trade identity are immutable actual fills. Commission and
    # role can be enriched by newer reconciliation; never regress to stale raw.
    for key, value in raw.items():
        if key not in {"commission", "commissionAsset", "maker", "isMaker", "realizedPnl"}:
            result[key] = value
    for key in ("price", "qty", "quoteQty", "commission", "realizedPnl"):
        if result.get(key) is not None:
            result[key] = str(result[key])
    if row.get("commission_conflict"):
        # Preserve the conflicting partial observation as partial. The last
        # coherent historical pair remains independently queryable in audit.
        observations = [item for item in row["audit_observations"]
                        if item.get("exchange_updated_ms") == row["exchange_updated_ms"]]
        partial = observations[-1] if observations else {}
        result["commission"] = partial.get("commission")
        result["commissionAsset"] = partial.get("commission_asset")
        result["demoAudit"] = {"commissionComplete": False, "commissionConflict": True,
                               "lastCoherentCommission": row["commission"],
                               "lastCoherentCommissionAsset": row["commission_asset"]}
    return result

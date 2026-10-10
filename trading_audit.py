"""Immutable audit of authenticated Freqtrade proxy operations.

Call ``begin_operation`` before sending a write upstream. Its transaction must
commit first; ``AuditUnavailable`` means the operation must not be forwarded.
Call ``finish_operation`` once an HTTP response or transport failure is known.
An accepted request is not evidence of an order fill. This module records no
credentials, headers, free text, raw bodies, or exception messages.
"""
from __future__ import annotations

import ipaddress
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


REQUEST_EVENT = "trading_operation_requested"
RESULT_EVENT = "trading_operation_result"
OUTCOMES = frozenset({"accepted", "rejected", "unknown", "not_forwarded"})
ERROR_CODES = frozenset({
    "timeout", "connection_error", "upstream_http_error", "upstream_business_error",
    "upstream_partial_error", "upstream_unavailable", "credential_unavailable",
    "invalid_request", "unknown_response", "audit_result_unavailable",
})
_WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_PAIR = re.compile(r"[A-Z0-9]{1,24}/[A-Z0-9]{1,12}(?::[A-Z0-9]{1,12})?\Z")
_ID = re.compile(r"[0-9]{1,24}\Z")
_PREFIX = re.compile(r"(/api/(?:web/)?v1|/v1)(/.*)?\Z")
_ACTIONS = {
    ("POST", "/forceexit"): "force_exit",
    ("POST", "/forcesell"): "force_exit",
    ("POST", "/forceenter"): "force_enter",
    ("POST", "/forcebuy"): "force_enter",
    ("POST", "/start"): "start",
    ("POST", "/stop"): "stop",
    ("POST", "/pause"): "pause_entries",
    ("POST", "/stopentry"): "pause_entries",
    ("POST", "/stopbuy"): "pause_entries",
    ("POST", "/reload_config"): "reload_config",
    ("POST", "/blacklist"): "blacklist_add",
    ("DELETE", "/blacklist"): "blacklist_remove",
    ("POST", "/locks"): "lock_add",
    ("POST", "/locks/delete"): "lock_remove",
}


class AuditUnavailable(RuntimeError):
    """Audit storage failed. Before forwarding, return 503 and stop."""


@dataclass(frozen=True)
class AuditRequest:
    request_id: str
    event_id: str
    payload: dict[str, Any]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _route(method: str, path: str) -> tuple[str | None, str]:
    method = str(method).upper()
    parsed_path = unquote(urlsplit(path).path).rstrip("/") or "/"
    match = _PREFIX.fullmatch(parsed_path)
    if method not in _WRITES or not match:
        # Proxy paths outside v1 may be introduced in a future upstream. They
        # must not become an unaudited write route, but their text is untrusted.
        if method in _WRITES and parsed_path.startswith("/api/"):
            return "other_write", "/api/[unclassified]"
        return None, ""
    prefix, route = match[1], match[2] or "/"
    if method == "POST" and route == "/pair_candles":
        return None, ""  # Installed Freqtrade implements this POST as a read.
    action = _ACTIONS.get((method, route))
    if action:
        return action, prefix + route
    trade = re.fullmatch(r"/trades/([0-9]{1,24})(/open-order|/reload)?", route)
    if trade:
        suffix = trade[2] or ""
        action = {
            ("DELETE", ""): "delete_trade",
            ("DELETE", "/open-order"): "cancel_open_order",
            ("POST", "/reload"): "reload_trade",
        }.get((method, suffix))
        if action:
            return action, prefix + route
    if method == "DELETE" and re.fullmatch(r"/locks/[0-9]{1,24}", route):
        return "lock_remove", prefix + route
    return "other_write", prefix + "/[unclassified]"


def classify_action(method: str, path: str) -> str | None:
    """Classify a proxy write, including old aliases and unknown write routes."""
    return _route(method, path)[0]


def _body(value: bytes | str | dict | list | None) -> tuple[Any, str]:
    if value is None or value == b"" or value == "":
        return {}, "empty"
    if isinstance(value, (bytes, str)):
        try:
            value = json.loads(value)
        except (ValueError, UnicodeError):
            return {}, "malformed"
    if isinstance(value, dict):
        return value, "object"
    if isinstance(value, list):
        return value, "array"
    return {}, "non_object"


def _identifier(value: Any, *, allow_all: bool = False) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (str, int)):
        text = str(value)
        if _ID.fullmatch(text) or (allow_all and text == "all"):
            return text
    return None


def _finite(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _pair(value: Any) -> str | None:
    return value if isinstance(value, str) and _PAIR.fullmatch(value) else None


def _target(action: str, path: str, payload: Any) -> dict[str, Any]:
    """Extract typed business fields, never an arbitrary subset of a raw body."""
    target: dict[str, Any] = {}
    body = payload if isinstance(payload, dict) else {}
    route_path = unquote(urlsplit(path).path).rstrip("/")
    if action in {"force_enter", "force_exit"}:
        if action == "force_exit":
            value = _identifier(body.get("tradeid"), allow_all=True)
            if value is not None:
                target["trade_id"] = value
        else:
            value = _pair(body.get("pair"))
            if value is not None:
                target["pair"] = value
            if isinstance(body.get("side"), str) and body["side"] in {"long", "short"}:
                target["side"] = body["side"]
        if isinstance(body.get("ordertype"), str) and body["ordertype"] in {"market", "limit"}:
            target["order_type"] = body["ordertype"]
        fields = {"price": "price"}
        fields.update({"amount": "amount"} if action == "force_exit" else {
            "stakeamount": "stake_amount", "leverage": "leverage",
        })
        for field, key in fields.items():
            value = _finite(body.get(field))
            if value is not None:
                target[key] = value
    elif action in {"delete_trade", "cancel_open_order", "reload_trade"}:
        target["trade_id"] = re.search(r"/trades/([0-9]+)", route_path)[1]
    elif action in {"lock_add", "lock_remove"}:
        match = re.search(r"/locks/([0-9]+)$", route_path)
        lock_id = match[1] if match else _identifier(body.get("lockid"))
        if lock_id is not None:
            target["lock_id"] = lock_id
        value = _pair(body.get("pair"))
        if value is not None:
            target["pair"] = value
        if isinstance(body.get("side"), str) and body["side"] in {"long", "short", "*"}:
            target["side"] = body["side"]
        # POST /locks accepts an array, not just an individual object.
        if action == "lock_add" and isinstance(payload, list):
            target["pairs"] = [item["pair"] for item in payload[:100]
                               if isinstance(item, dict) and _pair(item.get("pair"))]
            target["target_count"] = len(payload)
    elif action in {"blacklist_add", "blacklist_remove"}:
        pairs = body.get("blacklist", [])
        if action == "blacklist_remove":
            pairs = parse_qs(urlsplit(path).query).get("pairs_to_delete", [])
        if isinstance(pairs, list):
            target["pairs"] = [value for value in pairs[:100] if _pair(value)]
            target["target_count"] = len(pairs)
    return target


def _event_id(request_id: str, event_type: str) -> str:
    suffix = "request" if event_type == REQUEST_EVENT else "result"
    return f"trading-operation:{request_id}:{suffix}"


def _append_event(store, *, event_type: str, request_id: str,
                  payload: dict[str, Any]) -> str:
    """Commit an immutable event; stable IDs reject conflicting retries."""
    if event_type not in {REQUEST_EVENT, RESULT_EVENT}:
        raise ValueError("unsupported trading audit event type")
    if str(uuid.UUID(request_id)) != request_id:
        raise ValueError("invalid trading audit request ID")
    if payload.get("request_id") != request_id:
        raise ValueError("trading audit request ID mismatch")
    event_id = _event_id(request_id, event_type)
    source_id = f"trading-operation/{request_id}"
    try:
        with store._connection() as conn:
            inserted = conn.execute(
                """INSERT INTO research_ledger_events (event_id,event_type,payload,source_id)
                   VALUES (%s,%s,%s,%s) ON CONFLICT (event_id) DO NOTHING RETURNING event_id""",
                (event_id, event_type, store._json(payload), source_id),
            ).fetchone()
            if inserted is None:
                row = conn.execute(
                    "SELECT event_type,payload,source_id FROM research_ledger_events WHERE event_id=%s",
                    (event_id,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("audit event missing after insert conflict")
                old = (row["event_type"], row["payload"], row["source_id"]) if isinstance(row, dict) else tuple(row)
                if old != (event_type, payload, source_id):
                    raise ValueError("immutable trading audit event conflict")
    except Exception as exc:
        raise AuditUnavailable("交易操作审计暂不可写入，操作结果不得假定成功") from exc
    return event_id


def begin_operation(store, *, actor: str, source_ip: str, method: str,
                    path: str, payload: bytes | str | dict | list | None = None) -> AuditRequest:
    """Persist intent before forwarding. Actor must come from verified session.

    The caller passes the socket peer IP, never X-Forwarded-For. Raw payload is
    only parsed here to extract known business values; it is never persisted.
    Actor identifies an authenticated account, not proof of a natural person.
    """
    if not isinstance(actor, str) or not actor or len(actor) > 128 or any(ord(c) < 32 for c in actor):
        raise ValueError("authenticated actor is required")
    source_ip = str(ipaddress.ip_address(source_ip))
    action, safe_path = _route(method, path)
    if action is None:
        raise ValueError("request is not an audited operation")
    body, payload_state = _body(payload)
    request_id = str(uuid.uuid4())
    record = {
        "request_id": request_id, "requested_at_utc": _now(),
        "actor": actor, "source_ip": source_ip, "method": method.upper(),
        "path": safe_path, "action": action, "target": _target(action, path, body),
        "payload_state": payload_state,
        "upstream_scope": "webserver" if safe_path.startswith("/api/web/") else "bot",
    }
    event_id = _append_event(store, event_type=REQUEST_EVENT, request_id=request_id, payload=record)
    return AuditRequest(request_id, event_id, record)


def _response_outcome(status: int | None, response_payload: Any) -> tuple[str, str | None]:
    if status is None or status >= 500 or status < 200 or 300 <= status < 400 or status == 408:
        return "unknown", None
    if status >= 400:
        return "rejected", "upstream_http_error"
    body, body_state = _body(response_payload)
    if response_payload is not None and (body_state in {"malformed", "non_object"}
            or body_state == "empty" and status not in {204, 205}):
        return "unknown", "unknown_response"
    if isinstance(body, dict):
        if body.get("error") or body.get("detail"):
            return "rejected", "upstream_business_error"
        if body.get("errors"):
            return "unknown", "upstream_partial_error"
        business_status = body.get("status")
        if isinstance(business_status, str) and re.match(r"(?:error|failed|failure|rejected)\b", business_status, re.I):
            return "rejected", "upstream_business_error"
    return "accepted", None


def finish_operation(store, request: AuditRequest, *, upstream_status: int | None = None,
                     outcome: str | None = None, error_code: str | None = None,
                     response_payload: bytes | str | dict | None = None) -> str:
    """Append one linked result; a transport failure stays unknown.

    Response content is inspected only for finite business failure conventions;
    it is never stored. HTTP acceptance cannot prove execution or a fill.
    """
    if not isinstance(request, AuditRequest):
        raise ValueError("an AuditRequest from begin_operation is required")
    if upstream_status is not None and (isinstance(upstream_status, bool)
            or not isinstance(upstream_status, int) or not 100 <= upstream_status <= 599):
        raise ValueError("upstream_status must be an HTTP status or None")
    inferred, inferred_error = _response_outcome(upstream_status, response_payload)
    outcome = outcome or inferred
    if outcome not in OUTCOMES:
        raise ValueError("invalid trading operation outcome")
    if outcome == "accepted" and (inferred != "accepted" or error_code is not None):
        raise ValueError("unknown or failed operation cannot be accepted")
    if outcome == "rejected" and inferred != "rejected":
        raise ValueError("rejection requires an explicit upstream rejection")
    if outcome == "not_forwarded" and upstream_status is not None:
        raise ValueError("not_forwarded cannot include an upstream response")
    if error_code is not None and error_code not in ERROR_CODES:
        raise ValueError("error_code must be a safe enum, never an exception message")
    record = {
        "request_id": request.request_id, "finished_at_utc": _now(),
        "upstream_status": upstream_status, "outcome": outcome,
        "error_code": error_code or inferred_error,
    }
    return _append_event(store, event_type=RESULT_EVENT, request_id=request.request_id, payload=record)


def recent_operations(store, *, limit: int = 100) -> list[dict[str, Any]]:
    """Newest request intents with their result, or unknown if no result exists."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 500:
        raise ValueError("audit limit must be an integer between 0 and 500")
    if limit == 0:
        return []
    with store._connection() as conn:
        rows = conn.execute(
            """SELECT sequence_id,event_id,payload,created_at FROM research_ledger_events
               WHERE event_type=%s ORDER BY sequence_id DESC LIMIT %s""", (REQUEST_EVENT, limit),
        ).fetchall()
        requests = [dict(row) if isinstance(row, dict) else dict(zip(
            ("sequence_id", "event_id", "payload", "created_at"), row)) for row in rows]
        result_ids = [_event_id(row["payload"]["request_id"], RESULT_EVENT) for row in requests]
        result_rows = conn.execute(
            "SELECT event_id,payload FROM research_ledger_events WHERE event_id=ANY(%s)",
            (result_ids,),
        ).fetchall() if result_ids else []
    results = {row["event_id"]: row["payload"] for row in result_rows} if all(
        isinstance(row, dict) for row in result_rows) else {row[0]: row[1] for row in result_rows}
    items = []
    for row in requests:
        request = row["payload"]
        result_id = _event_id(request["request_id"], RESULT_EVENT)
        result = results.get(result_id)
        items.append({
            **request, "sequence_id": row["sequence_id"],
            "request_event_id": row["event_id"], "result_event_id": result_id if result else None,
            "finished_at_utc": result.get("finished_at_utc") if result else None,
            "upstream_status": result.get("upstream_status") if result else None,
            "outcome": result.get("outcome", "unknown") if result else "unknown",
            "error_code": result.get("error_code") if result else None,
        })
    return items


def audit_payload(store, *, limit: int = 100) -> dict[str, Any]:
    return {
        "items": recent_operations(store, limit=limit),
        "coverage": "authenticated_proxy_only",
        "limitations": [
            "仅记录经认证代理的操作；绕过代理直接访问机器人API的请求不在覆盖内。",
            "过去没有审计记录的干预无法补录调用者；客户端连接不构成调用者证据。",
            "操作者为经认证账户名；账户记录不能单独证明操作的自然人身份。",
            "accepted仅表示API接受请求，不表示订单已成交；超时或缺少结果时必须核查订单。",
        ],
    }

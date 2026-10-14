"""Local authenticated HTTP projection for the independent Demo ledger.

GETs never contact the exchange. Only an explicit /check action performs a
read-only signed request; /switch checks the account before changing a paused
session. /start is the sole entry-trading action. The HTTP
guard runs before body reads, DB access, or controller construction.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit

from binance_demo_credentials import DemoCredentialError, DemoCredentialStore
from demo_runner_controller import DemoHandoffError
from demo_store import COMPARISON_VIEWS

PREFIX = "/api/locals/binance-demo"
MAX_BODY_BYTES = 4096
MAX_QUERY_BYTES = 512
_ACCOUNT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,179}\Z")
_SESSION_KEY = re.compile(r"(?:demo_default|demo_strategy_[0-9a-f]{32})\Z")
_GET_PATHS = {"", "/status", "/accounts", "/orders", "/fills", "/positions",
              "/account", "/comparisons", "/evidence", "/sessions", "/strategies"}
_POST_PATHS = {"/credentials", "/check", "/start", "/stop", "/yield-flat", "/switch",
               "/attach", "/adopt-session", "/start-session", "/stop-session"}
logger = logging.getLogger("demo_api")
_HISTORY_PATHS = {"/orders", "/fills", "/comparisons", "/evidence"}
LIMITATIONS = [
    "币安 Demo 的订单簿、定价和执行可能与实盘不同，不能据此自动上线。",
    "手续费以实际成交 commission 为准，缺失字段保留 unknown。",
    "每个会话独立记录真实成交；共享账户同一币同时只允许一个策略持仓或挂单。",
    "账户余额和交易所持仓是共享快照；按策略的交易损益尚未包含资金费。",
]


class DemoApiError(ValueError):
    def __init__(self, code: str, status: int = 400):
        self.code = code
        self.status = status
        super().__init__(code)


def _identity(value):
    if not isinstance(value, str) or not _ACCOUNT_ID.fullmatch(value):
        raise DemoApiError("invalid_account_id")
    return value


def _commission_quote(raw, symbol: str) -> dict:
    """An account quote is separate evidence from actual fill commissions."""
    if not isinstance(raw, dict) or raw.get("symbol") != symbol:
        raise ValueError("invalid Demo commission quote identity")
    result = {"symbol": symbol}
    for field in ("makerCommissionRate", "takerCommissionRate"):
        rate = raw.get(field)
        if not isinstance(rate, str) or not rate or len(rate) > 128:
            raise ValueError("missing Demo commission quote")
        try:
            value = Decimal(rate)
        except InvalidOperation:
            raise ValueError("invalid Demo commission quote") from None
        if not value.is_finite() or not 0 <= value <= 1:
            raise ValueError("invalid Demo commission quote")
        result[field] = rate
    return result


def _strict_json(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DemoApiError("duplicate_request_field")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except DemoApiError:
        raise
    except (UnicodeError, ValueError, TypeError):
        raise DemoApiError("invalid_json_body") from None
    if not isinstance(value, dict):
        raise DemoApiError("json_object_required")
    return value


def handle_demo_request(handler, *, method: str, verify_token, allowed_origins,
                        api_provider):
    """Dispatch before the general proxy; rejects all unknown Demo operations."""
    handler.close_connection = True
    try:
        parsed = urlsplit(handler.path)
        suffix = parsed.path[len(PREFIX):]
        routes = _GET_PATHS if method == "GET" else _POST_PATHS
        if suffix not in routes:
            raise DemoApiError("demo_route_not_found", 404)
        username = verify_token(handler._bearer())
        if not username:
            raise DemoApiError("authentication_required", 401)
        if not handler._is_local():
            raise DemoApiError("demo_requires_local_client", 403)
        if handler.headers.get("Transfer-Encoding"):
            raise DemoApiError("transfer_encoding_not_supported")
        if len(parsed.query) > MAX_QUERY_BYTES:
            raise DemoApiError("query_too_large")
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise DemoApiError("invalid_query_string") from None
        if method == "POST":
            if query:
                raise DemoApiError("unexpected_query_parameter")
            # A non-ambient Bearer token plus exact Origin and JSON-only body
            # blocks browser CSRF; cookie/session credentials are not accepted.
            if handler.headers.get("Origin") not in allowed_origins:
                raise DemoApiError("demo_origin_rejected", 403)
            if handler.headers.get("Content-Type", "").split(";")[0].strip().lower() \
                    != "application/json":
                raise DemoApiError("json_content_type_required", 415)
            raw_length = handler.headers.get("Content-Length", "")
            if not raw_length.isdecimal() or len(raw_length) > 8:
                raise DemoApiError("valid_content_length_required")
            length = int(raw_length)
            if not 2 <= length <= MAX_BODY_BYTES:
                raise DemoApiError("demo_body_too_large" if length > MAX_BODY_BYTES
                                   else "json_object_required", 413 if length > MAX_BODY_BYTES else 400)
            body = _strict_json(handler._read_body())
            expected = ({"apiKey", "secret"} if suffix == "/credentials" else
                        {"accountId"} if suffix in {"/start", "/switch", "/attach"} else
                        {"session"} if suffix in {"/adopt-session", "/start-session", "/stop-session"}
                        else set())
            if set(body) != expected:
                raise DemoApiError("unexpected_or_missing_request_fields")
            if suffix == "/credentials":
                if not isinstance(body["apiKey"], str) or not isinstance(body["secret"], str):
                    raise DemoApiError("invalid_demo_credentials")
            if suffix in {"/start", "/switch", "/attach"}:
                _identity(body["accountId"])
            if suffix in {"/adopt-session", "/start-session", "/stop-session"}:
                if not isinstance(body["session"], str) or not _SESSION_KEY.fullmatch(body["session"]):
                    raise DemoApiError("invalid_demo_session")
            return handler._send(200, api_provider().post(suffix, body, actor=username))
        allowed = {"limit"} if suffix in {"/orders", "/fills", "/comparisons", "/sessions", "/strategies"} else set()
        if suffix in _HISTORY_PATHS:
            allowed.add("session")
        if suffix == "/comparisons":
            allowed.add("view")
        if set(query) - allowed or any(len(values) != 1 for values in query.values()):
            raise DemoApiError("unexpected_query_parameter")
        limit = 100
        if "limit" in query:
            raw_limit = query["limit"][0]
            if not raw_limit.isdecimal() or not 1 <= int(raw_limit) <= 1000:
                raise DemoApiError("invalid_limit")
            limit = int(raw_limit)
        if handler.headers.get("Content-Length", "0") not in {"", "0"}:
            raise DemoApiError("get_request_body_not_supported")
        options = {"limit": limit}
        if "view" in query:
            view = query["view"][0]
            if view not in COMPARISON_VIEWS:
                raise DemoApiError("invalid_comparison_view")
            options["view"] = view
        if "session" in query:
            session = query["session"][0]
            if not _SESSION_KEY.fullmatch(session):
                raise DemoApiError("invalid_demo_session")
            options["session"] = session
        return handler._send(200, api_provider().get(suffix, **options))
    except DemoApiError as exc:
        return handler._send(exc.status, {"detail": exc.code, "errorCode": exc.code})
    except DemoCredentialError as exc:
        return handler._send(409, {"detail": exc.code, "errorCode": exc.code})
    except Exception as exc:
        # Do not serialize traceback, raw CCXT errors, request, DSN or secrets.
        # Only fixed server messages and a class name are exposed.
        logger.error("Demo API request failed error_kind=%s exception_class=%s",
                     type(exc).__name__, exc.__class__.__name__)
        return handler._send(503, {"detail": "demo_service_unavailable",
                                   "errorCode": "demo_service_unavailable",
                                   "errorKind": type(exc).__name__})


def list_paper_accounts(data_store) -> list[dict]:
    with data_store._connection() as conn:
        rows = conn.execute("""SELECT strategy_id,rule_hash,execution_hash,definition,execution
            FROM strategy_sim_accounts ORDER BY strategy_id""").fetchall()
    items = []
    for row in rows:
        spec, execution = row["definition"], row["execution"]
        enabled = spec.get("execution_enabled") is not False and execution.get("execution_enabled") is not False
        eligible = (enabled and execution.get("risk_sizing_mode") == "entry_atr"
                    and not spec.get("paired_atr_stop_policy"))
        reason = spec.get("blocked_execution_reason") if not enabled else (
            None if eligible else "demo_requires_frozen_atr_single_leg_execution")
        items.append({"accountId": row["strategy_id"], "strategyId": row["strategy_id"],
                      "ruleHash": row["rule_hash"], "executionHash": row["execution_hash"],
                      "enabled": enabled, "eligible": eligible, "blockedReason": reason,
                      "description": spec.get("description", spec.get("name", row["strategy_id"])),
                      "timeframe": spec.get("timeframe")})
    return items


class DemoApi:
    def __init__(self, data_store, credential_store, demo_store, controller, *,
                 adapter_factory=None, account_provider=None, ledger_provider=None,
                 session_factory=None):
        self.data_store = data_store
        self.credentials = credential_store
        self._demo_store = demo_store
        self._ledger_provider = ledger_provider
        if session_factory is None:
            from demo_store import DemoStore
            def session_factory(key):
                return DemoStore(data_store, virtual_account_key=key)
        self.session_factory = session_factory
        self.controller = controller
        self.adapter_factory = adapter_factory
        self.account_provider = account_provider or (lambda: list_paper_accounts(data_store))
        self._check_lock = threading.Lock()
        self._mutation_lock = threading.RLock()

    @property
    def demo_store(self):
        return self._ledger_provider() if self._ledger_provider else self._demo_store

    def _runner_status(self):
        status = self.controller.status()
        worker = bool(status.get("workerRunning", status.get("running", False)))
        entries = bool(status.get("entriesEnabled", status.get("entries_enabled", False)))
        native_state = status.get("state", status.get("status", "stopped"))
        state = (native_state if "error" in native_state or native_state.startswith("blocked") else
                 "running" if worker and entries else
                 native_state if worker and native_state.startswith("paused") else
                 "paused_protecting" if worker else native_state)
        metrics = self.demo_store.metrics()
        pending = sum(count for key, count in metrics.get("orders_by_status", {}).items()
                      if key in {"intent", "submitting", "unknown", "open", "partially_filled"})
        has_exposure = any(Decimal(value) != 0 for value in self.demo_store.net_fill_positions().values())
        return {**status, "state": state, "tradingEnabled": entries,
                "workerRunning": worker, "hasExposure": has_exposure,
                "blockedPairs": status.get("blocked_pairs", []),
                "accountLocked": bool(status.get("accountLockOwned") or worker or pending or has_exposure),
                "lastError": status.get("controllerError") or status.get("last_error")}

    def _metrics(self, ledger=None):
        native = (ledger or self.demo_store).metrics()
        counts = native.get("orders_by_status", {})
        if isinstance(counts, list):
            counts = {row["status"]: row["count"] for row in counts}
        return {**native, "orderCount": sum(counts.values()),
                "fillCount": native.get("fill_count"),
                "unfilledOrderCount": native.get("unfilled_entry_count"),
                "terminalEntryCount": native.get("terminal_entry_count"),
                "unfilledEntryCount": native.get("unfilled_entry_count"),
                "unfilledEntryRatio": native.get("unfilled_entry_ratio"),
                "partiallyFilledOrderCount": counts.get("partially_filled", 0),
                "rejectedOrderCount": counts.get("rejected", 0),
                "medianLatencyMs": native.get("median_latency_ms"),
                "entryAttempts": native.get("entry_attempts"),
                "entrySubmitted": native.get("entry_submitted"),
                "entryRejectedOrderNotFillable": native.get("entry_rejected_order_not_fillable"),
                "entryExpiredNonFill": native.get("entry_expired_non_fill"),
                "entryFilled": native.get("entry_filled"),
                "entryPartial": native.get("entry_partial"),
                "rejectionRate": native.get("rejection_rate"),
                "nonFillRate": native.get("non_fill_rate"),
                "totalEntryFailureRate": native.get("total_entry_failure_rate"),
                "lastRejectionMs": native.get("last_rejection_ms"),
                "lastRejectionKind": native.get("last_rejection_kind")}

    def _strategy_summaries(self, limit):
        from demo_execution_service import derive_round_trips

        status = self.controller.status()
        members = {item["virtualAccountKey"]: item
                   for item in status.get("parallelSessions", [])}
        primary = self.demo_store.virtual_account_key
        members.setdefault(primary, {**status, "virtualAccountKey": primary,
                                     "strategyId": status.get("selectedAccountId")})
        sessions = self.demo_store.list_strategy_sessions(limit=limit)
        # Active members remain inspectable even if a UI history limit excludes
        # their older immutable binding.
        by_key = {item["virtualAccountKey"]: item for item in sessions}
        for key, item in members.items():
            by_key.setdefault(key, {"virtualAccountKey": key,
                                   "strategyId": item.get("strategyId", item.get("selectedAccountId")),
                                   "forwardBoundaryMs": item.get("forward_boundary_ms", item.get("forwardBoundaryMs"))})
        items = []
        for key, session in by_key.items():
            ledger = self.demo_store if key == primary else self.session_factory(key)
            state = members.get(key, {})
            # All fills, not a UI page, are necessary to reconstruct independent
            # round trips. Pair exclusivity prevents shared-average P&L mixing.
            orders, fills = ledger.runner_orders(), ledger.runner_fills()
            trades = derive_round_trips(fills, orders)
            net = ledger.net_fill_positions()
            net = {pair: str(qty) for pair, qty in net.items() if Decimal(str(qty)) != 0}
            known = bool(trades) and all(row.get("actual_net_trading_pnl_usdt") is not None
                                         for row in trades)
            metrics = self._metrics(ledger)
            metrics["closedNetTradingPnlUsdt"] = (str(sum((Decimal(row["actual_net_trading_pnl_usdt"])
                                                        for row in trades), Decimal(0)))
                                                 if known else None)
            items.append({**session, "primary": key == primary, "member": key in members,
                          "entriesEnabled": bool(state.get("entriesEnabled", state.get("entries_enabled", False))),
                          "status": state.get("status", "historical"),
                          "state": state.get("state", state.get("status", "historical")),
                          "ownedPositions": net,
                          "ownedPositionRows": [{"pair": pair, "signedQuantity": qty,
                                                 "side": "long" if Decimal(qty) > 0 else "short"}
                                                for pair, qty in net.items()],
                          "metrics": metrics, "roundTrips": trades,
                          "riskAlert": state.get("risk_alert"),
                          "ownershipConflicts": state.get("ownership_conflicts", []),
                          "pnlScope": "exclusive_pair_actual_fills_excludes_funding",
                          "independentWallet": False})
        return {"environment": "binance_demo", "items": items, "limit": limit,
                "sharedAccountPolicy": "one_way_exclusive_pair_no_cross_strategy_netting"}

    def get(self, suffix: str, *, limit=100, session=None, view=None):
        # A GET sees one active pointer, never half of a strategy switch.
        with self._mutation_lock:
            return self._get(suffix, limit=limit, session=session, view=view)

    def _get(self, suffix: str, *, limit=100, session=None, view=None):
        if view is not None and (suffix != "/comparisons" or
                                 not isinstance(view, str) or view not in COMPARISON_VIEWS):
            raise DemoApiError("invalid_comparison_view")
        ledger = self.demo_store
        if session is not None:
            if suffix not in _HISTORY_PATHS or not _SESSION_KEY.fullmatch(session):
                raise DemoApiError("invalid_demo_session")
            if session != ledger.virtual_account_key:
                known = ledger.list_strategy_sessions()
                if not any(item["virtualAccountKey"] == session for item in known):
                    raise DemoApiError("demo_session_not_found", 404)
                ledger = self.session_factory(session)
        if suffix in {"", "/status"}:
            return {"environment": "binance_demo", "endpoint": "demo-fapi.binance.com",
                    "credentials": self.credentials.status(), "runner": self._runner_status(),
                    "accounts": self.account_provider(), "metrics": self._metrics(),
                    "lastCheck": self.data_store.get_document("binance_demo_connection_check.json"),
                    "limitations": LIMITATIONS,
                    "switchSupported": callable(getattr(self.controller, "switch", None)),
                    "parallelSupported": callable(getattr(self.controller, "attach", None))}
        if suffix == "/strategies":
            return self._strategy_summaries(limit)
        if suffix == "/sessions":
            members = {item["virtualAccountKey"]: item
                       for item in self.controller.status().get("parallelSessions", [])}
            items = [{**item, "active": item["virtualAccountKey"] == ledger.virtual_account_key,
                      "member": item["virtualAccountKey"] in members or item["virtualAccountKey"] == ledger.virtual_account_key,
                      "entriesEnabled": bool(members.get(item["virtualAccountKey"], {}).get("entriesEnabled", False))}
                     for item in ledger.list_strategy_sessions(limit=limit)]
            return {"environment": "binance_demo", "items": items, "limit": limit}
        if suffix == "/evidence":
            from demo_evidence import read_demo_evidence
            return read_demo_evidence(self.data_store,
                                      virtual_account_key=ledger.virtual_account_key)
        if suffix == "/accounts":
            items = self.account_provider()
        elif suffix == "/orders":
            from demo_store import binance_order
            items = [binance_order(row) for row in
                     ledger.list_orders(limit=limit)]
        elif suffix == "/fills":
            from demo_store import binance_fill
            items = [binance_fill(row) for row in
                     ledger.list_fills(limit=limit)]
        elif suffix == "/comparisons":
            comparison_view = "audit" if view is None else view
            items = ledger.list_comparisons(limit=limit, view=comparison_view)
            return {"environment": "binance_demo", "items": items, "limit": limit,
                    "view": comparison_view}
        elif suffix in {"/positions", "/account"}:
            snapshot = (ledger.get_latest_position_snapshot() if suffix == "/positions"
                        else ledger.get_latest_account_snapshot())
            if snapshot is None:
                items = []
            else:
                payload = snapshot["payload"]
                if isinstance(payload, dict):
                    payload = payload.get("positions", payload) if suffix == "/positions" else \
                        payload.get("balance", payload)
                raw = payload.get("raw", payload) if isinstance(payload, dict) else payload
                if suffix == "/positions":
                    items = [item.get("raw", item) for item in raw] if isinstance(raw, list) else [raw]
                else:
                    # Binance account response is retained verbatim, including
                    # assets and positions; there is no fabricated account P&L.
                    return {"environment": "binance_demo", "account": raw,
                            "items": raw.get("assets", []) if isinstance(raw, dict) else [],
                            "observedAt": snapshot["observed_ms"]}
        else:
            raise DemoApiError("demo_route_not_found", 404)
        return {"environment": "binance_demo", "items": items, "limit": limit}

    def post(self, suffix: str, body: dict, *, actor: str):
        with self._mutation_lock:
            return self._post(suffix, body, actor=actor)

    def _post(self, suffix: str, body: dict, *, actor: str):
        if suffix == "/credentials":
            state = self._runner_status()
            if state["accountLocked"]:
                raise DemoApiError("pause_and_reconcile_before_replacing_credentials", 409)
            existing = self.credentials.status()
            if existing["configured"]:
                import hashlib
                proposed_fingerprint = hashlib.sha256(body["apiKey"].encode()).hexdigest()[:16]
                from demo_runner_controller import STATE_KEY
                controller_state = self.data_store.get_document(STATE_KEY, {}) or {}
                saved_fingerprint = controller_state.get("credentialFingerprint")
                if saved_fingerprint and proposed_fingerprint != saved_fingerprint:
                    raise DemoApiError("demo_ledger_bound_to_existing_api_identity", 409)
                if (proposed_fingerprint != existing["fingerprint"]
                        and (self.demo_store.list_orders(limit=1)
                             or self.demo_store.get_latest_account_snapshot() is not None
                             or self.demo_store.get_latest_position_snapshot() is not None
                             or self.demo_store.get_binding() is not None)):
                    # A different key may belong to another Binance account;
                    # immutable session history cannot move to that account.
                    raise DemoApiError("demo_ledger_bound_to_existing_api_identity", 409)
            status = self.credentials.save_credentials(body["apiKey"], body["secret"], actor=actor)
            self.data_store.put_document("binance_demo_connection_check.json", None)
            return {"environment": "binance_demo", "credentials": status}
        if suffix == "/check":
            return self.check()
        if suffix in {"/start", "/switch", "/attach"}:
            account_id = _identity(body["accountId"])
            item = next((row for row in self.account_provider() if row["accountId"] == account_id), None)
            if item is None or not item["eligible"]:
                raise DemoApiError("paper_account_not_demo_eligible", 409)
            try:
                result = (self.controller.attach(account_id) if suffix == "/attach" else
                          self.controller.switch(account_id) if suffix == "/switch"
                          else self.controller.start(account_id))
            except DemoHandoffError as exc:
                raise DemoApiError(exc.code, 409) from None
            response = {"environment": "binance_demo", "runner": result or self._runner_status()}
            if suffix == "/switch":
                response.update(automaticRestart=False,
                                switchPolicy="new_paused_session_preserve_history_keep_shared_account_lock")
            elif suffix == "/attach":
                response.update(automaticRestart=False,
                                attachPolicy="new_paused_immutable_session_preserve_existing_members")
            return response
        if suffix in {"/adopt-session", "/start-session", "/stop-session"}:
            session = body.get("session")
            if not isinstance(session, str) or not _SESSION_KEY.fullmatch(session):
                raise DemoApiError("invalid_demo_session")
            action = {"/adopt-session": "adopt_existing_session", "/start-session": "start_session",
                      "/stop-session": "stop_session"}[suffix]
            try:
                result = getattr(self.controller, action)(session)
            except DemoHandoffError as exc:
                raise DemoApiError(exc.code, 409) from None
            return {"environment": "binance_demo", "runner": result or self._runner_status(),
                    "session": session, "automaticRestart": False}
        if suffix == "/stop":
            result = self.controller.stop()
            return {"environment": "binance_demo", "runner": result or self._runner_status(),
                    "stopPolicy": "pause_entries_cancel_entry_orders_continue_owned_risk_protection"}
        if suffix == "/yield-flat":
            try:
                self.controller.stop()
                result = self.controller.close_if_flat()
            except DemoHandoffError as exc:
                raise DemoApiError(exc.code, 409) from None
            return {"environment": "binance_demo", "runner": result or self._runner_status(),
                    "handoffPolicy": "pause_entries_join_worker_yield_only_when_owned_ledger_flat",
                    "automaticRestart": False}
        raise DemoApiError("demo_route_not_found", 404)

    def check(self):
        from binance_demo_adapter import ALLOWED_PAIRS, BinanceDemoAdapter, PAIR_SYMBOLS

        if not self._check_lock.acquire(blocking=False):
            raise DemoApiError("demo_check_in_progress", 409)
        try:
            credentials = self.credentials.load_credentials()
            fingerprint = self.credentials.status()["fingerprint"]
            adapter = (self.adapter_factory or BinanceDemoAdapter)(
                api_key=credentials.api_key, api_secret=credentials.secret, allow_trading=False)
            checked_at = int(time.time() * 1000)
            try:
                balance, positions = adapter.fetch_balance(), adapter.fetch_positions()
                rates = [_commission_quote(adapter.fetch_commission_rate(pair), PAIR_SYMBOLS[pair])
                         for pair in ALLOWED_PAIRS]
                quote_metadata = {"commissionRates": rates,
                    "commissionRateObservedAt": checked_at,
                    "commissionRateSource": "demo_signed_account_commission_rate_quote_not_fill"}
                self.demo_store.append_account_snapshot({"balance": balance,
                    "demoFingerprint": fingerprint, **quote_metadata}, observed_ms=checked_at)
                self.demo_store.append_position_snapshot({"positions": positions,
                    "demoFingerprint": fingerprint}, observed_ms=checked_at)
            except Exception as exc:
                result = {"connected": False, "checkedAt": checked_at,
                          "errorCode": "demo_signed_check_failed", "errorKind": type(exc).__name__}
                self.data_store.put_document("binance_demo_connection_check.json", result)
                raise DemoApiError("demo_signed_check_failed", 502) from None
            result = {"connected": True, "checkedAt": checked_at,
                      "environment": "binance_demo", "endpoint": "demo-fapi.binance.com",
                      "fingerprint": fingerprint,
                      "readOnly": True, "feeVerification": "requires_actual_fills",
                      **quote_metadata}
            self.data_store.put_document("binance_demo_connection_check.json", result)
            return result
        finally:
            self._check_lock.release()


def create_demo_api(data_store, *, auth_dir):
    from demo_store import DemoStore
    from demo_runner_controller import DemoRunnerController
    from demo_signal_sources import DemoSignalSources

    credentials = DemoCredentialStore(data_store, master_key_path=auth_dir / "demo_master.key")
    from demo_runner_controller import STATE_KEY
    saved = data_store.get_document(STATE_KEY, {}) or {}
    namespace = saved.get("virtualAccountKey") or "demo_default"
    if not isinstance(namespace, str) or not _SESSION_KEY.fullmatch(namespace):
        raise DemoApiError("invalid_demo_session")
    ledger = DemoStore(data_store, virtual_account_key=namespace)
    sources = DemoSignalSources(data_store)
    controller = DemoRunnerController(data_store, ledger, credentials.load_credentials,
                                      paper_source=sources)
    return DemoApi(data_store, credentials, ledger, controller,
                   ledger_provider=lambda: controller.demo_store,
                   account_provider=lambda: list_paper_accounts(data_store) + sources.additional_accounts())

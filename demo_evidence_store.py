"""Immutable sanitized Demo evidence, scoped receipts, and once-only cost events.

Storage proves committed bytes and linkage, never exchange authenticity, complete
coverage, execution permission, or acceptance. No network occurs in transactions.
"""

from __future__ import annotations

import copy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from demo_store import DemoStore, _scrub_text

KINDS = frozenset(
    {
        "account_snapshot",
        "positions",
        "normal_orders",
        "conditional_orders",
        "fills",
        "cashflows",
        "clock",
        "boundary",
        "equity_reconciliation",
        "completion",
        "acceptance",
    }
)
HASHES = ("code_hash", "strategy_hash", "execution_hash", "risk_policy_hash", "cost_model_hash")
SCOPE = ("environment", "account_id", "stage_id")
OPTIONAL_SCOPE = ("strategy_version_id", "run_id")
SHA = re.compile(r"[a-f0-9]{64}\Z")
IDENTITY = re.compile(r"[A-Za-z0-9_./:@+-]{1,256}\Z")
SECRET = re.compile(
    r"apikey|apisecret|secret|signature|authorization|cookie|password|credential|token|privatekey|accesskey|headers|signedurl|requesturl|dsn|connectionstring",
    re.I,
)
READ_PATHS = {
    "fapiPublic": {"exchangeInfo", "time", "premiumIndex", "ticker/bookTicker"},
    "fapiPrivate": {
        "order",
        "allOrders",
        "openOrders",
        "userTrades",
        "positionSide/dual",
        "symbolConfig",
        "commissionRate",
        "leverageBracket",
        "algoOrder",
        "openAlgoOrders",
        "allAlgoOrders",
        "income",
        "multiAssetsMargin",
    },
    "fapiPrivateV3": {"positionRisk", "account"},
}
KIND_REQUESTS = {
    "account_snapshot": {
        ("fapiPrivateV3", "account"),
        ("fapiPrivate", "multiAssetsMargin"),
        ("fapiPrivate", "positionSide/dual"),
    },
    "positions": {("fapiPrivateV3", "positionRisk")},
    "normal_orders": {("fapiPrivate", path) for path in ("order", "allOrders", "openOrders")},
    "conditional_orders": {
        ("fapiPrivate", path) for path in ("algoOrder", "allAlgoOrders", "openAlgoOrders")
    },
    "fills": {("fapiPrivate", "userTrades")},
    "cashflows": {("fapiPrivate", "income")},
    "clock": {("fapiPublic", "time")},
}
PARAMS = {
    "symbol",
    "startTime",
    "endTime",
    "limit",
    "fromId",
    "orderId",
    "origClientOrderId",
    "algoId",
    "clientAlgoId",
    "algoType",
}
META = {
    "path",
    "api",
    "method",
    "status",
    "adapter_version",
    "latency_ms",
    "rate_limit_cost",
    "started_at_utc",
    "received_at_utc",
    "request_params",
    "response_sha256",
    "response_hash_basis",
}
ZERO_HASH = "0" * 64
MAX_BYTES = 8 * 1024 * 1024


def _id(value, name):
    if (
        not isinstance(value, str)
        or not IDENTITY.fullmatch(value)
        or "://" in value
        or "@" in value
    ):
        raise ValueError(f"invalid {name}")
    return value


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, allow_nan=False, separators=(",", ":")
    )


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _decimal(value):
    if isinstance(value, bool):
        raise ValueError("invalid exact decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid exact decimal") from None
    if (
        not number.is_finite()
        or len(number.as_tuple().digits) > 256
        or abs(number.as_tuple().exponent) > 256
        or (number and abs(number.adjusted()) > 256)
    ):
        raise ValueError("invalid exact decimal")
    return format(number, "f")


def _sanitize(value, depth=0):
    if depth > 40:
        raise ValueError("evidence nesting too deep")
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or len(key) > 256
                or "://" in key
                or any(ord(char) < 32 for char in key)
                or _scrub_text(key) != key
            ):
                raise ValueError("invalid evidence key")
            if not SECRET.search(re.sub(r"[^a-zA-Z0-9]", "", key)):
                result[key] = _sanitize(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 100000:
            raise ValueError("too many evidence rows")
        return [_sanitize(item, depth + 1) for item in value]
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, str):
        if len(value) > MAX_BYTES:
            raise ValueError("evidence string too large")
        text = _scrub_text(value)
        return re.sub(
            r"(?i)\b(?!https?://)[a-z][a-z0-9+.-]*://[^\s]+", "[redacted-connection]", text
        )
    if value is None or type(value) in (bool, int):
        if type(value) is int and value.bit_length() > 4096:
            raise ValueError("evidence integer too large")
        return value
    raise ValueError("evidence requires exact JSON numbers; float unsupported")


def _request(value):
    if not isinstance(value, dict) or set(value) - META:
        raise ValueError("unsupported request evidence")
    if not value:
        return {}  # Derived/offline facts explicitly lack transport evidence.
    required = {
        "path",
        "api",
        "method",
        "status",
        "request_params",
        "response_sha256",
        "response_hash_basis",
        "started_at_utc",
        "received_at_utc",
    }
    if (
        not required <= set(value)
        or value["method"] != "GET"
        or value["status"] != "ok"
        or value["api"] not in READ_PATHS
        or value["path"] not in READ_PATHS[value["api"]]
        or not isinstance(value["response_sha256"], str)
        or not SHA.fullmatch(value["response_sha256"])
        or value["response_hash_basis"] not in {"raw_http_response_bytes", "canonical_json"}
    ):
        raise ValueError("invalid Demo read request evidence")
    params = value["request_params"]
    if not isinstance(params, dict) or set(params) - PARAMS:
        raise ValueError("unsupported request params")
    for key, item in params.items():
        if key in {"symbol", "origClientOrderId", "clientAlgoId", "algoType"}:
            _id(item, "request parameter")
            if len(item) > 64:
                raise ValueError("invalid request parameter")
        elif type(item) is not int or not 0 <= item < 2**63:
            raise ValueError("invalid request parameter")
    try:
        stamps = [
            datetime.fromisoformat(value[k].replace("Z", "+00:00"))
            for k in ("started_at_utc", "received_at_utc")
        ]
        if any(stamp.tzinfo is None for stamp in stamps) or stamps[1] < stamps[0]:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise ValueError("invalid request timestamps") from None
    result = dict(value)
    if "latency_ms" in result:
        result["latency_ms"] = _decimal(result["latency_ms"])
        if Decimal(result["latency_ms"]) < 0:
            raise ValueError("negative latency")
    if "rate_limit_cost" in result and (
        type(result["rate_limit_cost"]) is not int or result["rate_limit_cost"] < 0
    ):
        raise ValueError("invalid rate limit cost")
    if "adapter_version" in result:
        _id(result["adapter_version"], "adapter version")
    return _sanitize(result)


class DemoEvidenceStore(DemoStore):
    """Explicit schema setup is inherited; all public receipts follow commit."""

    def __init__(self, data_store, account_id, stage_id, *, environment="binance_demo"):
        super().__init__(data_store, "evidence", environment=environment)
        self.account_id = _id(account_id, "account_id")
        self.stage_id = _id(stage_id, "stage_id")

    def _scope(self, scope):
        if not isinstance(scope, dict) or set(scope) - set(SCOPE + OPTIONAL_SCOPE):
            raise ValueError("invalid scope")
        expected = dict(zip(SCOPE, (self.environment, self.account_id, self.stage_id)))
        if any(scope.get(key) != item for key, item in expected.items()):
            raise ValueError("evidence scope mismatch")
        return {key: _id(item, "scope") for key, item in scope.items()}

    def _lock(self, conn):
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"demo-evidence:{self.environment}:{self.account_id}",),
        )

    def _prepare(self, kind, scope, payload, request_evidence, stable_key):
        if kind not in KINDS:
            raise ValueError("unknown evidence kind")
        clean_scope = self._scope(scope)
        body = {
            "version": "demo-evidence-v1",
            "kind": kind,
            "scope": clean_scope,
            "payload": _sanitize(payload),
            "request_evidence": _request(request_evidence),
        }
        metadata = body["request_evidence"]
        if metadata and (metadata["api"], metadata["path"]) not in KIND_REQUESTS.get(kind, set()):
            raise ValueError("request path does not match evidence kind")
        if len(_canonical(body).encode()) > MAX_BYTES:
            raise ValueError("evidence body exceeds 8 MiB")
        digest = _hash(body)
        key = "content:" + digest if stable_key is None else _id(stable_key, "capture key")
        return body, digest, key

    @staticmethod
    def _record_identity(row):
        return {
            key: row[key]
            for key in (
                "environment",
                "account_id",
                "stage_id",
                "account_sequence",
                "ref",
                "kind",
                "capture_key",
                "sha256",
                "previous_hash",
            )
        }

    @staticmethod
    def _receipt(row):
        return {
            "ref": row["ref"],
            "sha256": row["sha256"],
            "record_sha256": row["record_sha256"],
            "sequence": row["account_sequence"],
            "committed": True,
            "kind": row["kind"],
            "scope": copy.deepcopy(row["body"]["scope"]),
        }

    def _chain(self, conn):
        rows = conn.execute(
            "SELECT * FROM demo_evidence_archive WHERE environment=%s AND account_id=%s ORDER BY account_sequence",
            (self.environment, self.account_id),
        ).fetchall()
        previous = ZERO_HASH
        for sequence, row in enumerate(rows, 1):
            body = row["body"]
            try:
                ref = "evidence:" + _hash(
                    {
                        "scope": body["scope"],
                        "capture_key": row["capture_key"],
                        "sha256": row["sha256"],
                    }
                )
                valid = (
                    row["account_sequence"] == sequence
                    and row["previous_hash"] == previous
                    and row["kind"] in KINDS
                    and body["kind"] == row["kind"]
                    and body["version"] == "demo-evidence-v1"
                    and row["ref"] == ref
                    and all(body["scope"].get(key) == row[key] for key in SCOPE)
                    and row["sha256"] == _hash(body)
                    and row["record_sha256"] == _hash(self._record_identity(row))
                )
            except (TypeError, ValueError, KeyError):
                valid = False
            if not valid:
                raise ValueError("evidence chain integrity failure")
            previous = row["record_sha256"]
        return rows

    def _append(self, conn, prepared, rows):
        body, digest, key = prepared
        existing = next(
            (row for row in rows if row["stage_id"] == self.stage_id and row["capture_key"] == key),
            None,
        )
        if existing:
            if existing["sha256"] != digest or existing["body"] != body:
                raise ValueError("immutable evidence capture conflict")
            return existing, False
        row = {
            "environment": self.environment,
            "account_id": self.account_id,
            "stage_id": self.stage_id,
            "account_sequence": len(rows) + 1,
            "kind": body["kind"],
            "capture_key": key,
            "sha256": digest,
            "previous_hash": rows[-1]["record_sha256"] if rows else ZERO_HASH,
            "ref": "evidence:"
            + _hash({"scope": body["scope"], "capture_key": key, "sha256": digest}),
            "body": body,
        }
        row["record_sha256"] = _hash(self._record_identity(row))
        columns = (
            "environment",
            "account_id",
            "stage_id",
            "account_sequence",
            "ref",
            "kind",
            "capture_key",
            "sha256",
            "previous_hash",
            "record_sha256",
        )
        conn.execute(
            "INSERT INTO demo_evidence_archive ("
            + ",".join(columns)
            + ",body) VALUES ("
            + ",".join(["%s"] * 11)
            + ")",
            tuple(row[key] for key in columns) + (self.store._json(body),),
        )
        return row, True

    def append(self, kind, scope, payload, request_evidence, *, stable_key=None):
        if kind in {"completion", "acceptance"}:
            raise ValueError("use typed completion or acceptance API")
        prepared = self._prepare(kind, scope, payload, request_evidence, stable_key)
        with self.store._connection() as conn:
            self._lock(conn)
            row, _ = self._append(conn, prepared, self._chain(conn))
        return self._receipt(row)

    def _find(self, rows, ref, scope=None):
        _id(ref, "evidence ref")
        row = next((item for item in rows if item["ref"] == ref), None)
        if (
            row is None
            or row["stage_id"] != self.stage_id
            or (scope is not None and row["body"]["scope"] != scope)
        ):
            raise ValueError("evidence ref scope mismatch or missing")
        return row

    def read(self, ref):
        with self.store._connection() as conn:
            row = self._find(self._chain(conn), ref)
        return {
            **copy.deepcopy(row["body"]),
            **self._receipt(row),
            "previous_hash": row["previous_hash"],
        }

    def records(self, *, all_stages=False):
        with self.store._connection() as conn:
            rows = self._chain(conn)
        return [
            {
                **copy.deepcopy(row["body"]),
                **self._receipt(row),
                "previous_hash": row["previous_hash"],
            }
            for row in rows
            if all_stages or row["stage_id"] == self.stage_id
        ]

    def _completion_index(self, conn, rows):
        index = conn.execute(
            "SELECT * FROM demo_completion_records WHERE environment=%s AND account_id=%s AND stage_id=%s",
            (self.environment, self.account_id, self.stage_id),
        ).fetchall()
        linked = set()
        for item in index:
            row = self._find(rows, item["ref"])
            payload = row["body"]["payload"]
            if (
                row["kind"] != "completion"
                or payload != item["payload"]
                or any(
                    item[key] != payload.get(key)
                    for key in ("trade_id", "proof_sha256", "correction_required")
                )
            ):
                raise ValueError("completion index integrity failure")
            linked.add(item["ref"])
        expected = {
            row["ref"]
            for row in rows
            if row["stage_id"] == self.stage_id and row["kind"] == "completion"
        }
        if linked != expected:
            raise ValueError("completion index integrity failure")
        return sorted(
            index,
            key=lambda item: next(
                row["account_sequence"] for row in rows if row["ref"] == item["ref"]
            ),
        )

    def previous_completions(self):
        with self.store._connection() as conn:
            self._lock(conn)
            index = self._completion_index(conn, self._chain(conn))
        return {
            item["trade_id"]: item["proof_sha256"]
            for item in index
            if not item["correction_required"]
        }

    def completion_records(self):
        with self.store._connection() as conn:
            self._lock(conn)
            rows = self._chain(conn)
            index = self._completion_index(conn, rows)
        return [
            {**copy.deepcopy(item["payload"]), **self._receipt(self._find(rows, item["ref"]))}
            for item in index
        ]

    def append_completion(self, scope, completion, source_refs, *, stable_key=None):
        from demo_round_trip_costs import reconcile_round_trip_costs

        scope = self._scope(scope)
        incoming = _sanitize(completion)
        if (
            not isinstance(incoming, dict)
            or not isinstance(source_refs, list)
            or not source_refs
            or len(source_refs) != len(set(source_refs))
        ):
            raise ValueError("completion requires immutable source refs")
        for ref in source_refs:
            _id(ref, "source ref")
        with self.store._connection() as conn:
            self._lock(conn)
            rows = self._chain(conn)
            index = self._completion_index(conn, rows)
            previous = {
                item["trade_id"]: item["proof_sha256"]
                for item in index
                if not item["correction_required"]
            }
            actuals, variants = [], []
            for ref in source_refs:
                row = self._find(rows, ref, scope)
                if row["kind"] != "equity_reconciliation" or not isinstance(
                    row["body"]["payload"], dict
                ):
                    raise ValueError("completion source must be typed equity reconciliation")
                source = row["body"]["payload"].get("cost_evidence")
                if not isinstance(source, dict) or any(
                    source.get(key) != item for key, item in scope.items()
                ):
                    raise ValueError("completion source scope mismatch")
                fresh = reconcile_round_trip_costs(source).to_dict()
                trips = [
                    trip for trip in fresh["trips"] if trip["trade_id"] == incoming.get("trade_id")
                ]
                if (
                    len(trips) != 1
                    or trips[0].get("complete") is not True
                    or trips[0].get("risk_event") is None
                ):
                    raise ValueError("completion source incomplete")
                actuals.append(trips[0])
                known = reconcile_round_trip_costs(source, previous_completions=previous).to_dict()
                variants.extend(
                    trip for trip in known["trips"] if trip["trade_id"] == incoming.get("trade_id")
                )
            actual = actuals[0]
            if any(trip != actual for trip in actuals) or incoming not in [actual, *variants]:
                raise ValueError("completion differs from archived cost proof")
            trade, proof = actual["trade_id"], actual["proof_sha256"]
            existing = next(
                (
                    item
                    for item in index
                    if item["trade_id"] == trade and item["proof_sha256"] == proof
                ),
                None,
            )
            correction = trade in previous and previous[trade] != proof
            payload = {
                "trade_id": trade,
                "proof_sha256": proof,
                "completion": actual,
                "source_refs": sorted(source_refs),
                "correction_required": correction,
                "risk_event": None if correction else actual["risk_event"],
                "production_wired": False,
                "acceptance_eligible": False,
            }
            key = stable_key or "completion:" + _hash({"trade_id": trade, "proof_sha256": proof})
            if existing:
                row = self._find(rows, existing["ref"])
                if stable_key is not None and row["capture_key"] != stable_key:
                    raise ValueError("immutable completion capture conflict")
                # Identical economic proof can be observed again under a wider
                # archived scan. The original source remains authoritative.
                created = False
                correction = existing["correction_required"]
            else:
                row, created = self._append(
                    conn, self._prepare("completion", scope, payload, {}, key), rows
                )
                conn.execute(
                    "INSERT INTO demo_completion_records (environment,account_id,stage_id,trade_id,proof_sha256,ref,correction_required,payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        self.environment,
                        self.account_id,
                        self.stage_id,
                        trade,
                        proof,
                        row["ref"],
                        correction,
                        self.store._json(payload),
                    ),
                )
        return {
            **self._receipt(row),
            "new_event": created and not correction,
            "correction_required": correction,
            "risk_event": copy.deepcopy(actual["risk_event"])
            if created and not correction
            else None,
        }

    def append_acceptance_bundle(
        self, scope, identity_hashes, typed_source_refs, *, stable_key=None
    ):
        scope = self._scope(scope)
        if not all(key in scope for key in OPTIONAL_SCOPE):
            raise ValueError("acceptance scope requires strategy version and run")
        if (
            not isinstance(identity_hashes, dict)
            or set(identity_hashes) != set(HASHES)
            or any(
                not isinstance(item, str) or not SHA.fullmatch(item)
                for item in identity_hashes.values()
            )
        ):
            raise ValueError("frozen acceptance identities required")
        required = {"account_snapshot", "normal_orders", "conditional_orders", "fills", "cashflows"}
        if not isinstance(typed_source_refs, dict) or set(typed_source_refs) != required:
            raise ValueError("all typed acceptance source refs required")
        for ref in typed_source_refs.values():
            _id(ref, "source ref")
        with self.store._connection() as conn:
            self._lock(conn)
            rows = self._chain(conn)
            for kind, ref in typed_source_refs.items():
                row = self._find(rows, ref, scope)
                if (
                    row["kind"] != kind
                    or not isinstance(row["body"]["payload"], dict)
                    or row["body"]["payload"].get("identity_hashes") != identity_hashes
                    or any(
                        key in row["body"]["payload"] and row["body"]["payload"][key] != scope[key]
                        for key in SCOPE + OPTIONAL_SCOPE
                    )
                ):
                    raise ValueError("acceptance source kind or frozen identity mismatch")
            payload = {
                "status": "evidence_bundle_recorded",
                "identity_hashes": identity_hashes,
                "source_refs": typed_source_refs,
                "production_wired": False,
                "acceptance_eligible": False,
            }
            row, _ = self._append(
                conn, self._prepare("acceptance", scope, payload, {}, stable_key), rows
            )
        return self._receipt(row)

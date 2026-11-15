"""Bounded read-only dataset collection and crash-safe audit publication."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable

IDENTITY_FIELDS = ("exchange", "market", "pair", "timeframe", "candle_type")


def normalize_policy(dataset: dict[str, Any]) -> dict[str, Any]:
    """Only an explicit inventory entry exempts frozen history from freshness."""
    policy = dataset.get("policy")
    if policy is None:
        policy = {}
    if not isinstance(policy, dict):
        raise ValueError("dataset policy must be an object")
    if set(policy) - {"mode", "source", "required_fields", "max_age_bars", "end_ms"}:
        raise ValueError("unknown dataset policy field")
    mode = policy.get("mode", "active")
    if mode not in {"active", "frozen"}:
        raise ValueError("dataset policy mode must be active or frozen")
    source = policy.get("source", "unspecified")
    if source not in {"native", "resampled", "unspecified"}:
        raise ValueError("dataset source must be native, resampled or unspecified")
    result = {**policy, "mode": mode, "source": source}
    required = result.get("required_fields")
    if required is not None and (not isinstance(required, list)
                                 or any(not isinstance(field, str) or not field for field in required)):
        raise ValueError("required_fields must be a list of field names")
    age = result.get("max_age_bars")
    if age is not None and (isinstance(age, bool) or not isinstance(age, int) or age < 0):
        raise ValueError("dataset max_age_bars must be non-negative")
    if mode == "frozen" and result.get("end_ms") is not None:
        if isinstance(result["end_ms"], bool) or not isinstance(result["end_ms"], int) or result["end_ms"] < 0:
            raise ValueError("frozen end_ms must be non-negative")
    return result


def load_policies(path: Path | None) -> dict[tuple[str, ...], dict[str, Any]]:
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("datasets") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise ValueError("dataset policy must contain a datasets list")
    policies = {}
    for entry in entries:
        if not isinstance(entry, dict) or any(not isinstance(entry.get(key), str)
                                               or not entry[key] for key in IDENTITY_FIELDS):
            raise ValueError("dataset policy identity is incomplete")
        identity = tuple(entry[key] for key in IDENTITY_FIELDS)
        if identity in policies:
            raise ValueError("duplicate dataset policy identity")
        policies[identity] = normalize_policy({"policy": entry.get("policy", {})})
    return policies


def collect_windows(identities: list[dict[str, Any]], read: Callable[..., list[dict[str, Any]]],
                    *, window_bars: int | None = 750, as_of_ms: int | None = None,
                    timeout_seconds: float = 10) -> dict[str, Any]:
    """A timeout becomes one failed dataset and leaves the other checks running."""
    from tools.check_candle_integrity import timeframe_milliseconds

    if window_bars is not None and (isinstance(window_bars, bool)
                                    or not isinstance(window_bars, int) or window_bars < 1):
        raise ValueError("window_bars must be positive or None for full history")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    now = int(datetime.now(timezone.utc).timestamp() * 1000) if as_of_ms is None else as_of_ms
    datasets = []
    for identity in identities:
        item = {key: identity[key] for key in IDENTITY_FIELDS}
        item["policy"] = normalize_policy(identity)
        interval = timeframe_milliseconds(item["timeframe"])
        end = item["policy"].get("end_ms", now) if item["policy"]["mode"] == "frozen" else now
        # A bounded audit must never expand silently into a full-history scan.
        if item["policy"]["mode"] == "frozen" and "end_ms" not in item["policy"] and window_bars is not None:
            raise ValueError("bounded frozen dataset requires end_ms")
        bars = window_bars
        start = None if bars is None else max(0, end // interval * interval - bars * interval)
        item["check_window"] = {"start_ms": start, "end_ms": end, "bars": bars,
                                "scope": "full_history" if bars is None else "recent"}
        item["read_started_at_utc"] = datetime.now(timezone.utc).isoformat()
        try:
            item["rows"] = read(item, start_ms=start, end_ms=end,
                                 timeout_seconds=timeout_seconds)
        except Exception as exc:
            item.update(rows=[], read_error=type(exc).__name__)
        item["read_finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        datasets.append(item)
    return {"datasets": datasets, "snapshot_scope": "independent_dataset_transactions"}


def inspect_window(rows: list[dict[str, Any]], window: dict[str, Any], *,
                   interval: int, as_of_ms: int) -> dict[str, Any] | None:
    """Count expected closed slots, including an entirely missing leading edge."""
    if window.get("start_ms") is None:
        return None
    start = int(window["start_ms"])
    end = min(int(window["end_ms"]), as_of_ms)
    first = ((start + interval - 1) // interval) * interval
    closed_end = end // interval * interval
    expected = max(0, (closed_end - first) // interval)
    present = {row["timestamp"] for row in rows if row["extras"].get("closed") is True
               and row["timestamp"] % interval == 0
               and first <= row["timestamp"] < closed_end}
    tail = 0
    timestamp = closed_end - interval
    while timestamp >= first and timestamp in present:
        tail += 1
        timestamp -= interval
    return {"expected_closed_rows": expected, "observed_closed_rows": len(present),
            "missing_closed_rows": max(0, expected - len(present)),
            "continuous_closed_tail": tail, "start_ms": start, "end_ms": int(window["end_ms"])}


def read_database_windows(*, dsn: str, schema: str, pairs: list[str] | None = None,
                          timeframes: list[str] | None = None, window_bars: int | None = 750,
                          as_of_ms: int | None = None, timeout_seconds: float = 10,
                          policies: dict[tuple[str, ...], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Discover actual identities by indexed keyset seeks, then read windows.

    This avoids depending on a summary table that direct mirror inserts may
    not maintain, and avoids a DISTINCT scan of every historical candle.
    Every dataset has a separate read-only transaction and timeout boundary.
    """
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    policies = policies or {}
    fields = ",".join(IDENTITY_FIELDS)
    predicates = ["exchange='binance'", "market='futures'"]
    params: list[Any] = []
    if pairs:
        predicates.append("pair = ANY(%s)")
        params.append(pairs)
    if timeframes:
        predicates.append("timeframe = ANY(%s)")
        params.append(timeframes)
    where = " AND ".join(predicates)
    after = ",".join("d." + field for field in IDENTITY_FIELDS)
    inventory_query = (
        "WITH RECURSIVE ids AS ((SELECT " + fields + " FROM candles WHERE " + where
        + " ORDER BY " + fields + " LIMIT 1) UNION ALL SELECT n.* FROM ids d CROSS JOIN LATERAL "
        + "(SELECT " + fields + " FROM candles WHERE " + where + " AND (" + fields + ")>("
        + after + ") ORDER BY " + fields + " LIMIT 1) n) SELECT * FROM ids")
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10) as conn:
        def prepare() -> None:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
            conn.execute("SELECT set_config('statement_timeout', %s, true)",
                         (str(max(1, int(timeout_seconds * 1000))),))
        with conn.transaction():
            prepare()
            identities = [dict(row) for row in conn.execute(inventory_query, params + params)]
        known = {tuple(item[key] for key in IDENTITY_FIELDS) for item in identities}
        # Explicit and requested trade datasets remain in scope even if absent.
        expected = set(policies)
        if pairs and timeframes:
            expected |= {("binance", "futures", pair, timeframe, "futures")
                         for pair in pairs for timeframe in timeframes}
        for identity in sorted(expected - known):
            if ((not pairs or identity[2] in pairs)
                    and (not timeframes or identity[3] in timeframes)):
                identities.append(dict(zip(IDENTITY_FIELDS, identity)))
        for item in identities:
            item["policy"] = policies.get(tuple(item[key] for key in IDENTITY_FIELDS), {})

        def read(identity: dict[str, Any], *, start_ms: int | None, end_ms: int,
                 timeout_seconds: float) -> list[dict[str, Any]]:
            conditions = [key + "=%s" for key in IDENTITY_FIELDS] + ["timestamp < %s"]
            values = [identity[key] for key in IDENTITY_FIELDS] + [end_ms]
            if start_ms is not None:
                conditions.append("timestamp >= %s")
                values.append(start_ms)
            query = ("SELECT timestamp,volume,extras FROM candles WHERE "
                     + " AND ".join(conditions) + " ORDER BY timestamp")
            with conn.transaction():
                prepare()
                return [dict(row) for row in conn.execute(query, values)]
        return collect_windows(identities, read, window_bars=window_bars,
                               as_of_ms=as_of_ms, timeout_seconds=timeout_seconds)


def atomic_write_json(path: Path, payload: Any) -> None:
    """An interrupted publication leaves the previous complete JSON visible."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

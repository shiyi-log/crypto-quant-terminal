"""Append-only research/deployment evidence ledger.

The registry is a research record.  The trade mirror is the only source for
actual orders and trades; a registered champion is never treated as deployed
without an explicit deployment event.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from numbers import Real
from pathlib import Path
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _event_id(event_type: str, payload: Any) -> str:
    return hashlib.sha256((_json({"event_type": event_type, "payload": payload})).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: str | os.PathLike[str] | None, default: Any) -> Any:
    if not path or not Path(path).is_file():
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _read_jsonl(path: str | os.PathLike[str] | None) -> list[dict[str, Any]]:
    if not path or not Path(path).is_file():
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"research trial line {line_no} must be an object")
                rows.append(value)
    return rows


def _source_stamp(*paths: str | os.PathLike[str] | None) -> tuple[str, int | None]:
    items = []
    latest = None
    for raw in paths:
        if not raw:
            continue
        p = Path(raw)
        if p.is_file():
            st = p.stat()
            latest = max(latest or 0, st.st_mtime_ns)
            items.append((str(p.resolve()), st.st_mtime_ns, st.st_size))
    return hashlib.sha256(_json(items).encode()).hexdigest(), latest


def _record_event(store, event_type: str, payload: dict[str, Any], source_id: str | None = None) -> str:
    """Insert one immutable event.  Same content is idempotent; same id differs => error."""
    event_id = _event_id(event_type, payload)
    with store._connection() as conn:
        inserted = conn.execute(
            """INSERT INTO research_ledger_events (event_id,event_type,payload,source_id)
               VALUES (%s,%s,%s,%s) ON CONFLICT (event_id) DO NOTHING RETURNING event_id""",
            (event_id, event_type, store._json(payload), source_id),
        ).fetchone()
        if inserted is not None:
            return event_id
        existing = conn.execute(
            "SELECT event_type,payload FROM research_ledger_events WHERE event_id=%s", (event_id,)
        ).fetchone()
        if existing is None:
            raise RuntimeError(f"ledger event disappeared after conflict: {event_id}")
        old = existing["payload"] if isinstance(existing, dict) else existing[1]
        old_type = existing["event_type"] if isinstance(existing, dict) else existing[0]
        if old_type != event_type or _json(old) != _json(payload):
            raise ValueError(f"immutable ledger event conflict: {event_id}")
    return event_id


_FORWARD_PAPER_TYPES = frozenset({"manifest", "snapshot", "decision", "fill", "checkpoint"})


def _forward_paper_db_event_id(run_id: str, event_type: str, stable_event_id: str) -> str:
    """Namespace a producer's stable ID without replacing it in the payload."""
    identity = {"namespace": "forward-paper", "run_id": run_id,
                "event_type": event_type, "stable_event_id": stable_event_id}
    return "forward-paper:" + hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()


def append_forward_paper_events(store, run_id: str,
                                events: list[dict[str, Any]]) -> list[str]:
    """Append immutable forward-paper events using producer-supplied IDs.

    The generic research ledger derives IDs from payload content. Forward-paper
    events already have stable identities, so their database keys must retain
    that identity across retries while remaining isolated by run and event type.
    The whole batch is one transaction; a reused ID with changed content fails.
    """
    if not isinstance(run_id, str) or not run_id or any(ord(char) < 32 for char in run_id):
        raise ValueError("run_id must be a non-empty string without control characters")
    if not isinstance(events, list):
        raise ValueError("events must be a list")

    source_id = f"forward-paper/{run_id}"
    prepared = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("forward-paper events must be objects")
        event_type = event.get("event_type")
        stable_id = event.get("event_id")
        if not isinstance(event_type, str) or event_type not in _FORWARD_PAPER_TYPES:
            raise ValueError(f"unsupported forward-paper event type: {event_type}")
        if event.get("run_id") != run_id:
            raise ValueError("forward-paper event run_id mismatch")
        if not isinstance(stable_id, str) or not stable_id or any(ord(char) < 32 for char in stable_id):
            raise ValueError("forward-paper event_id must be a non-empty stable string")
        database_id = _forward_paper_db_event_id(run_id, event_type, stable_id)
        prepared.append((database_id, f"forward_paper_{event_type}", event, source_id))

    inserted_ids = []
    with store._connection() as conn:
        for database_id, event_type, payload, event_source in prepared:
            inserted = conn.execute(
                """INSERT INTO research_ledger_events (event_id,event_type,payload,source_id)
                   VALUES (%s,%s,%s,%s) ON CONFLICT (event_id) DO NOTHING RETURNING event_id""",
                (database_id, event_type, store._json(payload), event_source),
            ).fetchone()
            if inserted is None:
                existing = conn.execute(
                    """SELECT event_type,payload,source_id FROM research_ledger_events
                       WHERE event_id=%s""", (database_id,),
                ).fetchone()
                if existing is None:
                    raise RuntimeError(f"forward-paper event disappeared after conflict: {database_id}")
                old_type = existing["event_type"] if isinstance(existing, dict) else existing[0]
                old_payload = existing["payload"] if isinstance(existing, dict) else existing[1]
                old_source = existing["source_id"] if isinstance(existing, dict) else existing[2]
                if (old_type != event_type or old_source != event_source
                        or _json(old_payload) != _json(payload)):
                    raise ValueError(f"immutable forward-paper event conflict: {database_id}")
            inserted_ids.append(database_id)
    return inserted_ids


def read_forward_paper_events(store, run_id: str, *,
                              event_types: list[str] | tuple[str, ...] | None = None,
                              limit: int | None = None) -> list[dict[str, Any]]:
    """Read one run's evidence in append order, optionally filtered by type."""
    if not isinstance(run_id, str) or not run_id or any(ord(char) < 32 for char in run_id):
        raise ValueError("run_id must be a non-empty string without control characters")
    if event_types is not None:
        if not isinstance(event_types, (list, tuple)):
            raise ValueError("event_types must be a list or tuple")
        requested_types = list(event_types)
        if any(not isinstance(item, str) or item not in _FORWARD_PAPER_TYPES
               for item in requested_types):
            raise ValueError("unsupported forward-paper event type filter")
        if not requested_types:
            return []
    else:
        requested_types = None
    if limit is not None and (isinstance(limit, bool) or int(limit) != limit or limit < 0):
        raise ValueError("limit must be a non-negative integer")
    if limit == 0:
        return []

    query = """SELECT sequence_id,event_id,event_type,payload,source_id,created_at
        FROM research_ledger_events WHERE source_id=%s"""
    params: list[Any] = [f"forward-paper/{run_id}"]
    if requested_types is not None:
        db_types = [f"forward_paper_{item}" for item in dict.fromkeys(requested_types)]
        query += " AND event_type = ANY(%s)"
        params.append(db_types)
    query += " ORDER BY sequence_id"
    if limit is not None:
        query += " LIMIT %s"
        params.append(int(limit))
    with store._connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) if isinstance(row, dict) else {
        "sequence_id": row[0], "event_id": row[1], "event_type": row[2],
        "payload": row[3], "source_id": row[4], "created_at": row[5],
    } for row in rows]


def list_forward_paper_runs(store, *, limit: int | None = 100) -> list[dict[str, Any]]:
    """List newest manifest-backed runs; orphan activity is not a run."""
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
    ):
        raise ValueError("limit must be a non-negative integer")
    if limit == 0:
        return []

    with store._connection() as conn:
        rows = conn.execute(
            """SELECT source_id,sequence_id,payload,created_at
               FROM research_ledger_events
               WHERE event_type=%s AND source_id LIKE %s
               ORDER BY sequence_id""",
            ("forward_paper_manifest", "forward-paper/%"),
        ).fetchall()

    # The first manifest is the immutable run identity. Retries are already
    # idempotent, but this also handles older databases with duplicate manifests.
    by_source: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row) if isinstance(row, dict) else {
            "source_id": row[0], "sequence_id": row[1],
            "payload": row[2], "created_at": row[3],
        }
        source_id = item["source_id"]
        if not isinstance(source_id, str) or not source_id.startswith("forward-paper/"):
            continue
        run_id = source_id[len("forward-paper/"):]
        payload = item["payload"]
        if not run_id or not isinstance(payload, dict) or payload.get("run_id") != run_id:
            continue
        if source_id not in by_source:
            by_source[source_id] = {
                "run_id": run_id,
                "manifest": payload,
                "manifest_sequence_id": item["sequence_id"],
                "created_at": item["created_at"],
            }

    runs = sorted(by_source.values(), key=lambda item: item["manifest_sequence_id"], reverse=True)
    return runs[:limit] if limit is not None else runs


def _all_events(store) -> list[dict[str, Any]]:
    with store._connection() as conn:
        rows = conn.execute("""SELECT event_id,event_type,payload,source_id,created_at
            FROM research_ledger_events ORDER BY sequence_id""").fetchall()
    out = []
    for row in rows:
        out.append(dict(row) if isinstance(row, dict) else {"event_id": row[0], "event_type": row[1], "payload": row[2], "source_id": row[3], "created_at": row[4]})
    return out


def _version_entry(raw: dict[str, Any], event_type: str = "model_version") -> dict[str, Any]:
    """Normalize the public projection while retaining original evidence."""
    if event_type == "research_trial" and (raw.get("run_id") or raw.get("key")):
        fallback = f"trial-{raw.get('run_id') or 'unknown'}-{raw.get('key') or 'unknown'}"
    else:
        fallback = "trial-" + _event_id("research_trial", raw)[:12] if event_type == "research_trial" else "version-" + _event_id("model_version", raw)[:12]
    vid = str(raw.get("id") or raw.get("version_id") or fallback)
    metrics = raw.get("metrics") or {}
    spec = raw.get("spec") or raw.get("config") or {}
    status = str(raw.get("status") or "challenger")
    legacy_auto_ic = event_type == "research_trial" or raw.get("source") == "auto_research"
    invalid = legacy_auto_ic or bool(raw.get("invalidated")) or str(raw.get("evaluation_status", "")).lower() == "invalidated"
    limitations = list(raw.get("limitations") or [])
    if legacy_auto_ic:
        limitations.append("旧自动 IC 口径已作废；保留原报指标仅供追溯，不用于可用性判断")
    return {
        "id": vid,
        "round": raw.get("round"),
        "layer": raw.get("layer", "ml_model"),
        "config": spec,
        "direction": raw.get("direction") or raw.get("phase") or raw.get("notes") or "",
        "changes": raw.get("changes") or raw.get("history") or [],
        "registry_status": status,
        "deployment_status": "verified" if raw.get("deployment_verified") else ("unverified" if raw.get("deployed") else "not_deployed"),
        "evaluation_status": "invalidated" if invalid else (raw.get("evaluation_status") or "unverified"),
        "reported_metrics": {"ic": metrics.get("ic", metrics.get("ic_period", metrics.get("ic_pool"))), "t": metrics.get("t", metrics.get("t_period", metrics.get("t_pool"))), "q": metrics.get("q", metrics.get("q_value"))},
        "effect": {"actual_orders": None, "closed_trades": None, "realized_profit_abs": None},
        "evidence": raw.get("evidence") or raw.get("artifacts") or [],
        "limitations": limitations or (["历史回测口径已作废"] if invalid else []),
    }


def _trade_rows(store, source_id: str) -> list[dict[str, Any]]:
    # A read failure is unavailable evidence, not evidence of zero trades.
    rows = store.read_external(source_id, "trades")
    return [dict(row) for row in rows if isinstance(row, dict)]


def _order_rows(store, source_id: str) -> list[dict[str, Any]]:
    rows = store.read_external(source_id, "orders")
    return [dict(row) for row in rows if isinstance(row, dict)]


def _timestamp_value(value: Any) -> float | None:
    """Return a comparable UTC timestamp without assigning meaning to junk data."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Real):
        return float(value)
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _id_value(value: Any) -> tuple[int, float | str] | None:
    """Use numeric ids numerically so 10 follows 9, not 1."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return (1, str(value))
    return (0, number)


def _latest_rows(rows: list[dict[str, Any]], *, limit: int = 200, time_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Keep the latest records in chronological order.

    External snapshots are keyed by record id and are not guaranteed to be
    returned in insertion order.  Prefer the latest known activity timestamp,
    then a numeric-aware id, and finally the source position for deterministic
    ordering when an upstream row has no usable metadata.
    """
    if limit <= 0 or not rows:
        return []

    def key(item: tuple[int, dict[str, Any]]) -> tuple[int, float, int, float | str, int]:
        position, row = item
        timestamps = [_timestamp_value(row.get(field)) for field in time_fields]
        valid_timestamps = [value for value in timestamps if value is not None]
        timestamp = max(valid_timestamps) if valid_timestamps else 0.0
        id_value = _id_value(row.get("id", row.get("order_id", row.get("trade_id"))))
        if id_value is None:
            id_value = (2, "")
        # Unknown timestamps are retained as the oldest rows; a usable
        # business timestamp is stronger evidence of recency than source
        # insertion order.
        return (1 if valid_timestamps else 0, timestamp, id_value[0], id_value[1], position)

    ordered = sorted(enumerate(rows), key=key)
    return [row for _, row in ordered[-limit:]]


def _mirrored_version_entries(store) -> list[dict[str, Any]]:
    """Project the latest DataStore mirrors without requiring an explicit sync.

    The immutable ledger events are authoritative for known version ids; these
    mirrors expose previously unseen versions before an explicit ledger sync.
    Stores that only expose the ledger tables can omit the mirror APIs.
    """
    projected: dict[str, dict[str, Any]] = {}
    try:
        registry = store.get_document("model_versions.json", {"versions": []}) or {}
    except (AttributeError, NotImplementedError):
        registry = {}
    raw_versions = registry.get("versions", []) if isinstance(registry, dict) else []
    for raw in raw_versions:
        if isinstance(raw, dict):
            item = _version_entry(raw)
            projected[item["id"]] = item

    try:
        trials = store.read_events("research_trials.jsonl")
    except (AttributeError, NotImplementedError):
        trials = []
    for trial in trials or []:
        if not isinstance(trial, dict):
            continue
        item = _version_entry({
            **trial,
            "layer": "ml_model",
            "status": "trial",
            "metrics": {
                "ic": trial.get("ic_period", trial.get("ic_pool")),
                "t": trial.get("t_period", trial.get("t_pool")),
                "q": trial.get("q_value"),
            },
        }, "research_trial")
        item["direction"] = trial.get("phase") or "研究试验"
        item["evaluation_status"] = trial.get("evaluation_status") or "invalidated"
        item["limitations"] = trial.get("limitations") or [
            "历史试验指标仅作原始记录，未经当前部署口径验证"
        ]
        projected[item["id"]] = item
    return list(projected.values())


def _is_open(value: Any) -> bool | None:
    """Normalize an explicit open flag; ``None`` means state is unknown.

    A missing/invalid flag must not be inferred from ``close_date`` or
    ``close_profit_abs``.  Partial Freqtrade snapshots can contain those
    fields while the row is still incomplete, and treating it as closed would
    turn an unverified result into realized performance.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "open"}:
            return True
        if normalized in {"0", "false", "no", "closed"}:
            return False
    return None


def _trade_projection(row: dict[str, Any], *, external_tags: set[str] | None = None,
                      external_order_count: int = 0, order_tag_audit_unknown: bool = True) -> dict[str, Any]:
    is_open = _is_open(row.get("is_open"))
    closed = is_open is False
    value = _profit_value(row.get("close_profit_abs")) if closed else None
    exit_reason = row.get("exit_reason")
    externally_intervened = bool(external_tags)
    exit_category = "external_intervention" if externally_intervened else _exit_category(exit_reason, is_open)
    if externally_intervened:
        classification_basis = "filled_external_exit_order"
    elif is_open is False:
        classification_basis = "final_trade_exit_reason"
    elif is_open is True:
        classification_basis = "trade_not_closed"
    else:
        classification_basis = "trade_state_unknown"
    return {
        "trade_id": row.get("id", row.get("trade_id")),
        "pair": row.get("pair"),
        "side": "short" if row.get("is_short") else "long",
        "leverage": row.get("leverage", 1.0),
        "stake_amount": row.get("stake_amount"),
        "open_date": row.get("open_date"),
        "close_date": row.get("close_date") if closed else None,
        "is_open": is_open,
        "realized_profit_abs": value,
        "exit_reason": exit_reason,
        "exit_category": exit_category,
        # Exit source and model attribution are different evidence. A known
        # strategy exit does not establish which registered model was running.
        "strategy_eligible": closed and exit_category in _STRATEGY_EXIT_CATEGORIES,
        "externally_intervened": externally_intervened,
        "external_exit_order_count": external_order_count,
        "exit_classification_basis": classification_basis,
        "order_tag_audit_unknown": order_tag_audit_unknown,
        "model_id": None,
        "attribution_status": "unattributed",
    }


_EXIT_CATEGORIES = {
    "exit_signal": "strategy_signal",
    "sell_signal": "strategy_signal",
    "custom_exit": "strategy_signal",
    "custom_sell": "strategy_signal",
    "roi": "strategy_roi",
    "stop_loss": "strategy_risk",
    "stoploss_on_exchange": "strategy_risk",
    "trailing_stop_loss": "strategy_risk",
    "emergency_exit": "execution_emergency",
    "emergency_sell": "execution_emergency",
    "partial_exit": "strategy_adjustment",
    # Liquidation is a strategy risk outcome and cannot be removed merely
    # because it was not a discretionary exit signal.
    "liquidation": "liquidation",
    "force_exit": "external_intervention",
    "force_sell": "external_intervention",
    "sold_on_exchange": "external_exchange_execution",
}
_STRATEGY_EXIT_CATEGORIES = frozenset({
    "strategy_signal", "strategy_roi", "strategy_risk", "execution_emergency",
    "strategy_adjustment", "liquidation",
})

_EXTERNAL_ORDER_TAGS = frozenset({
    "force_exit", "force_sell", "sold_on_exchange",
})


def _exit_category(exit_reason: Any, is_open: bool | None) -> str:
    """Classify only established closed trades; unknown tags remain unknown.

    Freqtrade lets strategies supply arbitrary exit tags, so an unfamiliar
    reason cannot establish either a strategy exit or human intervention.
    Likewise, close fields cannot establish closure when the state is missing.
    """
    if is_open is True:
        return "not_closed"
    if is_open is None or not isinstance(exit_reason, str):
        return "unknown"
    return _EXIT_CATEGORIES.get(exit_reason.strip().lower(), "unknown")


def _order_trade_id(row: dict[str, Any]) -> Any:
    return row.get("trade_id", row.get("ft_trade_id"))


def _order_tag(row: dict[str, Any]) -> str | None:
    value = row.get("ft_order_tag", row.get("order_tag"))
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _order_filled(row: dict[str, Any]) -> bool:
    """Require evidence of execution; an accepted/cancelled request is not enough."""
    value = row.get("filled")
    if isinstance(value, Real) and not isinstance(value, bool):
        return math.isfinite(float(value)) and float(value) > 0
    for field in ("order_filled_date", "order_filled_timestamp", "filled_at"):
        if _timestamp_value(row.get(field)) is not None:
            return True
    return False


def _order_is_exit(row: dict[str, Any], trade: dict[str, Any]) -> bool | None:
    """Require an explicit entry flag or an order side aligned to trade direction."""
    entry_flag = _is_open(row.get("ft_is_entry"))
    side = row.get("ft_order_side") or row.get("side")
    if side in {"stoploss", "stop_loss"}:
        return entry_flag is not True
    is_short = _is_open(trade.get("is_short"))
    inferred = None
    if is_short is not None and side in {"buy", "sell"}:
        inferred = side == ("buy" if is_short else "sell")
    if entry_flag is not None:
        explicit = not entry_flag
        return explicit if inferred is None or inferred == explicit else None
    return inferred


def _external_order_evidence(rows: list[dict[str, Any]],
                             trades: list[dict[str, Any]]) -> tuple[dict[str, set[str]], dict[str, int], set[str]]:
    """Return per-trade filled external tags, counts, and trades with tag audit gaps."""
    tags_by_trade: dict[str, set[str]] = {}
    count_by_trade: dict[str, int] = {}
    trade_by_id = {str(row.get("id", row.get("trade_id"))): row for row in trades}
    seen_trade_ids: set[str] = set()
    unknown_ids: set[str] = set()
    for row in rows:
        raw_trade_id = _order_trade_id(row)
        if raw_trade_id is None:
            continue
        trade_id = str(raw_trade_id)
        seen_trade_ids.add(trade_id)
        # A present null tag still means this order's tag field was inspected;
        # a missing field means the API projection cannot establish coverage.
        if "ft_order_tag" not in row and "order_tag" not in row:
            unknown_ids.add(trade_id)
        is_exit = _order_is_exit(row, trade_by_id.get(trade_id, {}))
        if is_exit is None:
            unknown_ids.add(trade_id)
        tag = _order_tag(row)
        if is_exit is True and tag in _EXTERNAL_ORDER_TAGS and _order_filled(row):
            tags_by_trade.setdefault(trade_id, set()).add(tag)
            count_by_trade[trade_id] = count_by_trade.get(trade_id, 0) + 1
    return tags_by_trade, count_by_trade, unknown_ids | (set(trade_by_id) - seen_trade_ids)


def _profit_value(value: Any) -> float | None:
    """A realized amount must be a finite number, including an explicit zero."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _realized_summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """A partial known sum is disclosed separately from the complete total."""
    values = [row["realized_profit_abs"] for row in trades
              if row["realized_profit_abs"] is not None]
    missing = len(trades) - len(values)
    known = sum(values) if values else None
    return {
        "realized_profit_abs": known if missing == 0 else None,
        "realized_profit_known_abs": known,
        "realized_profit_missing_count": missing,
    }


def _actual_trade_summary(trades: list[dict[str, Any]], *, order_tag_audit_unknown_count: int = 0) -> dict[str, Any]:
    closed = [row for row in trades if row["is_open"] is False]
    groups = {
        "strategy": [row for row in closed if row["strategy_eligible"]],
        "external": [row for row in closed if row["exit_category"] in {
            "external_intervention", "external_exchange_execution",
        }],
        "unknown": [row for row in closed if row["exit_category"] == "unknown"],
    }
    summary = {
        "closed_trade_count": len(closed),
        **_realized_summary(closed),
        "strategy_closed_trade_count": len(groups["strategy"]),
        "external_exit_count": len(groups["external"]),
        "unknown_exit_count": len(groups["unknown"]),
        "unknown_trade_state_count": sum(row["is_open"] is None for row in trades),
        "externally_intervened_trade_count": sum(row["externally_intervened"] for row in trades),
        "partial_exit_audit_unknown_trade_count": order_tag_audit_unknown_count,
    }
    for group, rows in groups.items():
        summary.update({f"{group}_{key}": value
                        for key, value in _realized_summary(rows).items()})
    return summary


def _order_projection(row: dict[str, Any]) -> dict[str, Any]:
    """Project the Freqtrade order row while keeping links to its trade and pair."""
    return {
        "order_id": row.get("order_id", row.get("id")),
        "trade_id": row.get("trade_id", row.get("ft_trade_id")),
        "pair": row.get("pair") or row.get("ft_pair") or row.get("symbol"),
        "side": row.get("side") or row.get("ft_order_side") or (
            "short" if row.get("is_short") else "long"
        ),
        "status": row.get("status"),
        "amount": row.get("amount", row.get("ft_amount")),
        "filled": row.get("filled"),
        "price": row.get("average") or row.get("price") or row.get("ft_price"),
        "order_date": row.get("order_date") or row.get("order_date_created"),
        "model_id": None,
    }


def _evidence_projection(events: list[dict[str, Any]], event_type: str,
                         defaults: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold append-only evidence revisions by id in ledger sequence order."""
    by_id: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["event_type"] != event_type:
            continue
        payload = event["payload"]
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str) or not payload["id"]:
            raise ValueError(f"{event_type} event must contain a nonempty string id")
        by_id[payload["id"]] = dict(payload)
    return list(by_id.values()) if by_id else defaults


def ledger_payload(store, version_id: str | None = None, *, source_id: str = "bot/tradesv3.dryrun.sqlite", registry_path: str | None = None) -> dict[str, Any]:
    events = _all_events(store)
    versions_by_id = {}
    for event in events:
        if event["event_type"] in {"model_version", "research_trial"}:
            entry = _version_entry(event["payload"], event["event_type"])
            versions_by_id[entry["id"]] = entry
    # Synced audit events are authoritative for an existing version.  Mirrors
    # expose previously unseen versions until sync, but their freshness is not
    # established and they must not overwrite an appended correction.
    for entry in _mirrored_version_entries(store):
        versions_by_id.setdefault(entry["id"], entry)
    versions = list(versions_by_id.values())
    if version_id is not None:
        versions = [item for item in versions if item["id"] == version_id]
    versions.sort(key=lambda item: (
        item.get("round") is None,
        item.get("round") if item.get("round") is not None else 0,
        item["id"],
    ))
    by_id = {item["id"]: item for item in versions}
    baseline, candidate = by_id.get("auto-r9-e3c3df7c"), by_id.get("auto-r20-e3c3df7c")
    before = (baseline or {}).get("config") or {}
    after = (candidate or {}).get("config") or {}
    config_changes = [{"key": key, "before": before.get(key), "after": after.get(key)}
                      for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]
    raw_trades = _trade_rows(store, source_id)
    raw_orders = _order_rows(store, source_id)
    external_tags, external_order_counts, audit_unknown_ids = _external_order_evidence(raw_orders, raw_trades)
    trades = []
    for row in raw_trades:
        trade_id = str(row.get("id", row.get("trade_id")))
        trades.append(_trade_projection(
            row,
            external_tags=external_tags.get(trade_id),
            external_order_count=external_order_counts.get(trade_id, 0),
            order_tag_audit_unknown=trade_id in audit_unknown_ids,
        ))
    actual_summary = _actual_trade_summary(
        trades,
        order_tag_audit_unknown_count=sum(
            row["order_tag_audit_unknown"] for row in trades if row["is_open"] is False
        ),
    )
    generated = _now()
    with store._connection() as conn:
        sync_row = conn.execute("SELECT last_synced_at FROM research_ledger_sync WHERE source_id=%s", (source_id,)).fetchone()
    synced = sync_row["last_synced_at"] if sync_row and isinstance(sync_row, dict) else (sync_row[0] if sync_row else None)
    # psycopg returns TIMESTAMPTZ as datetime; the HTTP response must contain
    # JSON primitives and expose one UTC timestamp in both freshness fields.
    if isinstance(synced, datetime):
        synced = (synced if synced.tzinfo else synced.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    latest_trade_rows = _latest_rows(
        raw_trades, time_fields=("open_date", "close_date", "open_date_utc", "close_date_utc")
    )
    latest_order_rows = _latest_rows(
        raw_orders, time_fields=("order_date", "order_date_created", "created_at", "date")
    )
    # Keep the full counts above, but only expose a bounded, chronologically
    # ordered detail list to the UI.  The source mirror may be sorted by an
    # opaque record key, so slicing ``rows[-200:]`` is not a latest-records
    # query.
    latest_trades = []
    for row in latest_trade_rows:
        trade_id = str(row.get("id", row.get("trade_id")))
        latest_trades.append(_trade_projection(
            row,
            external_tags=external_tags.get(trade_id),
            external_order_count=external_order_counts.get(trade_id, 0),
            order_tag_audit_unknown=trade_id in audit_unknown_ids,
        ))
    latest_orders = [_order_projection(row) for row in latest_order_rows]
    lessons = _evidence_projection(events, "research_lesson", _lessons())
    directions = _evidence_projection(events, "research_direction", _directions())
    return {"generated_at": generated, "last_synced_at": synced, "summary": {"version_count": len(versions), "deployed_ml_count": sum(v["layer"] == "ml_model" and v["deployment_status"] == "verified" for v in versions), "actual_trade_count": len(trades), "actual_order_count": len(raw_orders), **actual_summary, "unattributed_trade_count": len(trades)}, "versions": versions, "comparison": {"baseline_id": "auto-r9-e3c3df7c", "candidate_id": "auto-r20-e3c3df7c", "config_changes": config_changes, "note": "round is research sequence, not generation; same configuration does not imply same weights; historical IC evidence invalidated"}, "actual_trades": latest_trades, "actual_orders": latest_orders, "exit_classification_basis": "filled_external_exit_order or final_trade_exit_reason", "exit_classification_limitations": ["只有确认退出方向的已成交订单且带 force_exit/force_sell/sold_on_exchange 标签才会覆盖最终平仓原因；部分人工平仓后再由策略退出时，若订单标签缺失，无法从最终 trade.exit_reason 排除人工干预", "未知 exit_reason 不等于人工干预，且未成交强制请求不计入外部干预"], "lessons": lessons, "directions": directions, "errors": [], "source_id": source_id, "source_freshness": {"source_id": source_id, "trade_rows": len(trades), "order_rows": len(raw_orders), "last_synced_at": synced}}


def _lessons() -> list[dict[str, Any]]:
    return [{"id": "invalidated-backtest", "title": "前视与池化排名污染历史指标", "status": "recorded", "reason": "旧引擎和旧评估口径不可用于生产判定", "evidence": ["P0/P1 audit"], "do_not_repeat": True}, {"id": "champion-not-deployment", "title": "注册 champion 不等于部署", "status": "recorded", "reason": "TrendFollowing 没有 ML 消费路径", "evidence": ["strategy source audit"], "do_not_repeat": True}]


def _directions() -> list[dict[str, Any]]:
    return [{"id": "take-profit-stop-loss", "title": "关键止盈止损点", "status": "planned", "hypothesis": "固定规则可改善净收益且不扩大回撤", "next_check": "同一批真实入场影子对照"}, {"id": "dynamic-leverage", "title": "风险定仓与动态杠杆", "status": "planned", "hypothesis": "敞口不变时波动率定仓改善风险调整收益", "next_check": "强平距离与回撤"}, {"id": "orderbook-impact", "title": "盘口深度与未成交单影响", "status": "planned", "hypothesis": "盘口深度决定实际滑点与容量", "next_check": "候选池决策前后快照"}, {"id": "net-return", "title": "净收益目标", "status": "planned", "hypothesis": "完整扣除可测成本后再判定策略可用性", "next_check": "已平仓净收益与浮盈分开"}]


def sync(store, registry_path: str, trials_path: str | None = None, *, source_id: str = "bot/tradesv3.dryrun.sqlite") -> dict[str, Any]:
    registry = _read_json(registry_path, {})
    versions = registry.get("versions", []) if isinstance(registry, dict) else []
    for version in versions:
        _record_event(store, "model_version", version, source_id)
    for trial in _read_jsonl(trials_path):
        _record_event(store, "research_trial", trial, source_id)
    # Research lessons and the frozen directions are evidence, not just UI
    # copy.  Changed content appends a revision; replaying identical content
    # keeps its original sequence and cannot replace a later correction.
    for lesson in _lessons():
        _record_event(store, "research_lesson", lesson, source_id)
    for direction in _directions():
        _record_event(store, "research_direction", direction, source_id)
    # Keep a source fingerprint separately so a sync can be audited without
    # rewriting any prior event (the projection itself remains append-only).
    fingerprint, mtime = _source_stamp(registry_path, trials_path)
    with store._connection() as conn:
        conn.execute("""INSERT INTO research_ledger_sync
            (source_id,source_mtime_ns,source_fingerprint) VALUES (%s,%s,%s)
            ON CONFLICT (source_id) DO UPDATE SET source_mtime_ns=EXCLUDED.source_mtime_ns,
            source_fingerprint=EXCLUDED.source_fingerprint,last_synced_at=CURRENT_TIMESTAMP""",
                     (source_id, mtime, fingerprint))
    return ledger_payload(store, source_id=source_id, registry_path=registry_path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="sync research/deployment evidence ledger")
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--registry", default="bot/user_data/model_versions.json")
    parser.add_argument("--trials", default="bot/user_data/research_trials.jsonl")
    parser.add_argument("--source-id", default="bot/tradesv3.dryrun.sqlite")
    args = parser.parse_args(argv)
    from data_store import DataStore
    store = DataStore()
    payload = sync(store, args.registry, args.trials, source_id=args.source_id) if args.sync else ledger_payload(store, source_id=args.source_id)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

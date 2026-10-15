#!/usr/bin/env python3
"""Read-only independent verification for the R269 retirement population.

This checker deliberately does not import ``strategy_simulation`` or reuse its
active-account SQL.  It reads the persisted execution JSON and event ledger
directly, then reports whether the five R269 families are disabled and whether
new entry/position events were created in the audit window.

Usage::

    QUANT_DATABASE_URL="$(tools/dburl.sh)" .venv/bin/python3 \
        tools/verify_r269_retirement.py

    # Use a reviewed deployment/retirement boundary instead of the default:
    QUANT_DATABASE_URL="$(tools/dburl.sh)" .venv/bin/python3 \
        tools/verify_r269_retirement.py --since 2026-10-15T13:00:00+08:00

The command is SELECT-only and exits non-zero when a target account is still
enabled, a target entry/position event exists after ``--since``, or an s12 row
is accidentally included in the target population.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

TARGET_FAMILIES = (
    "s14_vol_compression_release",
    "s34_vwap_anchor_reversion",
    "s09_trend_pullback",
    "s03_tsmom_vol_target",
    "s17_overnight_range_fade",
)
EXCLUDED_FAMILY = "s12_surge_expansion_breakout"
# This is an audit-window default only. The implementation/deployment boundary
# should be supplied explicitly with --since when the reviewer has it.
DEFAULT_SINCE = "2026-10-15T00:00:00+08:00"
ENTRY_EVENT_TYPES = frozenset({"position_opened"})
ENTRY_ORDER_TYPES = frozenset({"entry"})


def _database_url() -> str | None:
    value = os.environ.get("QUANT_DATABASE_URL")
    if value:
        return value
    root = Path(__file__).resolve().parents[1]
    for path in (root / ".env", Path("/tmp/bot_env.txt")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("QUANT_DATABASE_URL="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            continue
    return None


def _json(value):
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return json.loads(value)


def _parse_since(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("--since must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _family(strategy_id: str) -> str | None:
    for family in TARGET_FAMILIES:
        if strategy_id.startswith(family):
            return family
    return None


def _row_dicts(cursor):
    columns = [item.name for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def run(database_url: str, since: datetime) -> tuple[dict, int]:
    import psycopg

    report: dict = {
        "since": since.isoformat(),
        "target_families": list(TARGET_FAMILIES),
        "excluded_family": EXCLUDED_FAMILY,
        "read_only": True,
        "families": {},
        "s12_guard": {},
        "problems": [],
    }
    with psycopg.connect(database_url, connect_timeout=10) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL statement_timeout TO '30s'")
            accounts_cursor = conn.execute(
                """
                SELECT strategy_id, execution, state, created_at, updated_at
                  FROM quant.strategy_sim_accounts
                 WHERE strategy_id LIKE 's14_vol_compression_release%'
                    OR strategy_id LIKE 's34_vwap_anchor_reversion%'
                    OR strategy_id LIKE 's09_trend_pullback%'
                    OR strategy_id LIKE 's03_tsmom_vol_target%'
                    OR strategy_id LIKE 's17_overnight_range_fade%'
                 ORDER BY strategy_id
                """
            )
            accounts = _row_dicts(accounts_cursor)
            event_cursor = conn.execute(
                """
                SELECT strategy_id, event_type, candle_ms, created_at, payload
                  FROM quant.strategy_sim_events
                 WHERE created_at >= %s
                   AND (
                        strategy_id LIKE 's14_vol_compression_release%%'
                     OR strategy_id LIKE 's34_vwap_anchor_reversion%%'
                     OR strategy_id LIKE 's09_trend_pullback%%'
                     OR strategy_id LIKE 's03_tsmom_vol_target%%'
                     OR strategy_id LIKE 's17_overnight_range_fade%%'
                     OR strategy_id LIKE 's12_surge_expansion_breakout%%'
                   )
                 ORDER BY created_at, strategy_id, event_type
                """,
                (since,),
            )
            events = _row_dicts(event_cursor)
            s12_cursor = conn.execute(
                """
                SELECT count(*) AS n, min(created_at) AS first_at,
                       max(created_at) AS last_at
                  FROM quant.strategy_sim_accounts
                 WHERE strategy_id LIKE 's12_surge_expansion_breakout%'
                """
            )
            s12_accounts = _row_dicts(s12_cursor)[0]

    by_family: dict[str, list[dict]] = defaultdict(list)
    for row in accounts:
        family = _family(row["strategy_id"])
        if family is not None:
            by_family[family].append(row)

    events_by_family: dict[str, list[dict]] = defaultdict(list)
    for row in events:
        family = _family(row["strategy_id"])
        if family is not None:
            events_by_family[family].append(row)

    for family in TARGET_FAMILIES:
        family_accounts = by_family[family]
        family_events = events_by_family[family]
        enabled_values = Counter()
        for row in family_accounts:
            execution = _json(row["execution"])
            enabled_values[execution.get("execution_enabled", "MISSING")] += 1
        entry_payload_events = [
            row for row in family_events
            if _json(row["payload"]).get("kind") in ENTRY_ORDER_TYPES
        ]
        submitted_entries = [
            row for row in entry_payload_events if row["event_type"] == "order_submitted"
        ]
        filled_entries = [
            row for row in entry_payload_events if row["event_type"] == "order_filled"
        ]
        opened_positions = [
            row for row in family_events if row["event_type"] in ENTRY_EVENT_TYPES
        ]
        account_created = sum(1 for row in family_events if row["event_type"] == "account_created")
        report["families"][family] = {
            "account_count": len(family_accounts),
            "execution_enabled_values": {
                str(key): value for key, value in enabled_values.items()
            },
            "account_statuses": dict(Counter(_json(row["state"]).get("status", "MISSING")
                                              for row in family_accounts)),
            "account_created_events_in_window": account_created,
            "events_in_window": len(family_events),
            "event_types_in_window": dict(Counter(row["event_type"] for row in family_events)),
            "entry_payload_events_in_window": len(entry_payload_events),
            "entry_orders_submitted_in_window": len(submitted_entries),
            "entry_orders_filled_in_window": len(filled_entries),
            "positions_opened_in_window": len(opened_positions),
            "first_event_in_window": min((row["created_at"] for row in family_events), default=None),
            "last_event_in_window": max((row["created_at"] for row in family_events), default=None),
        }
        if any(_json(row["execution"]).get("execution_enabled", "MISSING") is not False
               for row in family_accounts):
            report["problems"].append(f"{family}: execution_enabled is not false for all accounts")
        if submitted_entries or opened_positions:
            report["problems"].append(
                f"{family}: {len(submitted_entries)} entry orders submitted and "
                f"{len(opened_positions)} positions opened at or after --since"
            )

    report["s12_guard"] = {
        "account_count": int(s12_accounts["n"]),
        "first_account_at": s12_accounts["first_at"],
        "last_account_at": s12_accounts["last_at"],
        "included_in_target_query": False,
        "events_seen_in_audit_window": sum(
            1 for row in events if row["strategy_id"].startswith(EXCLUDED_FAMILY)
        ),
    }
    return report, int(bool(report["problems"]))


def _serializable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _serializable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serializable(item) for item in value]
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=DEFAULT_SINCE,
                        help=f"audit boundary (ISO-8601 with timezone; default: {DEFAULT_SINCE})")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    try:
        since = _parse_since(args.since)
    except ValueError as exc:
        parser.error(str(exc))
    url = _database_url()
    if not url:
        print("缺少 QUANT_DATABASE_URL（环境变量、仓库 .env 或 /tmp/bot_env.txt）", file=sys.stderr)
        return 2
    try:
        report, status = run(url, since)
    except Exception as exc:  # bounded diagnostic; do not print credentials/DSN
        print(f"数据库只读核验失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    output = _serializable(report)
    if args.json:
        print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(f"R269 retirement read-only verification · since={output['since']}")
        for family in TARGET_FAMILIES:
            row = output["families"][family]
            print(
                f"{family}: accounts={row['account_count']} "
                f"execution_enabled={row['execution_enabled_values']} "
                f"entry_submitted={row['entry_orders_submitted_in_window']} "
                f"entry_filled={row['entry_orders_filled_in_window']} "
                f"positions_opened={row['positions_opened_in_window']} "
                f"events={row['events_in_window']}"
            )
        print(f"s12 guard: {output['s12_guard']}")
        print(f"problems={len(output['problems'])}")
        for problem in output["problems"]:
            print(f"  FAIL: {problem}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())

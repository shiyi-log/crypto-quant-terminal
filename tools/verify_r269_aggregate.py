#!/usr/bin/env python3
"""Read-only verification of the R259 per-candle aggregate loss limit.

The check intentionally reads the immutable event ledger directly and joins
the account execution contract for each account's own loss cap.  It does not
import the simulator or infer a cap from strategy-family names.

Usage::

    QUANT_DATABASE_URL="$(tools/dburl.sh)" .venv/bin/python3 \
        tools/verify_r269_aggregate.py

The command is SELECT-only.  It exits 0 when no group has every individual
trade within its cap while the group's summed loss exceeds that cap, and 1
when at least one such group remains.  A missing database URL or read error
returns 2.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import datetime as dt
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Iterable


@dataclass(frozen=True)
class Trade:
    strategy_id: str
    opened_candle_ms: int
    net_pnl: float
    max_loss_per_trade_usdt: float


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


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _coerce_trade(row: Any) -> Trade | None:
    """Convert a SQL row to a validated trade without silently coercing nulls."""
    strategy_id = str(row[0]) if row[0] is not None else ""
    opened = _number(row[1])
    net = _number(row[2])
    cap = _number(row[3])
    if not strategy_id or opened is None or net is None or cap is None or cap <= 0:
        return None
    if opened != math.floor(opened):
        return None
    return Trade(strategy_id, int(opened), net, cap)


def summarize(trades: Iterable[Trade]) -> dict[str, Any]:
    """Return R259 counts from validated trade rows.

    The grouping key includes the cap so a malformed account contract cannot
    merge observations with different risk budgets.  A group is an
    ``aggregate_only_breach`` when every member is individually within its
    cap, but the sum of the group's net P&L is below the same cap.
    """
    groups: dict[tuple[str, int, float], list[float]] = defaultdict(list)
    for trade in trades:
        groups[(trade.strategy_id, trade.opened_candle_ms,
                trade.max_loss_per_trade_usdt)].append(trade.net_pnl)

    single_breach = 0
    aggregate_breach = 0
    aggregate_only: list[dict[str, Any]] = []
    worst_aggregate: tuple[float, tuple[str, int, float], list[float]] | None = None
    for key, values in groups.items():
        _, _, cap = key
        has_single_breach = any(value < -cap for value in values)
        total = float(sum(values))
        if has_single_breach:
            single_breach += 1
        if total < -cap:
            aggregate_breach += 1
            if worst_aggregate is None or total < worst_aggregate[0]:
                worst_aggregate = (total, key, values)
        if not has_single_breach and total < -cap:
            aggregate_only.append({
                "strategy_id": key[0],
                "opened_candle_ms": key[1],
                "max_loss_per_trade_usdt": cap,
                "trade_count": len(values),
                "net_pnl_values": list(values),
                "aggregate_net_pnl": total,
            })

    aggregate_only.sort(key=lambda row: row["aggregate_net_pnl"])
    result: dict[str, Any] = {
        "group_count": len(groups),
        "single_breach_count": single_breach,
        "aggregate_breach_count": aggregate_breach,
        "single_compliant_aggregate_breach_count": len(aggregate_only),
        "aggregate_limit_satisfied": not aggregate_only,
        "worst_aggregate_net_pnl": (worst_aggregate[0] if worst_aggregate else None),
        "worst_aggregate_group": (aggregate_only[0] if aggregate_only else None),
        "aggregate_only_examples": aggregate_only[:10],
    }
    return result


def _read_snapshot(database_url: str) -> tuple[list[Trade], dict[str, int]]:
    import psycopg

    query = """
        SELECT e.strategy_id,
               e.payload->>'opened_candle_ms' AS opened_candle_ms,
               e.payload->>'net_pnl' AS net_pnl,
               a.execution->>'max_loss_per_trade_usdt' AS max_loss_per_trade_usdt
          FROM quant.strategy_sim_events AS e
          JOIN quant.strategy_sim_accounts AS a
            ON a.strategy_id = e.strategy_id
         WHERE e.event_type = 'trade_closed'
         ORDER BY e.sequence_id
    """
    counters = Counter()
    rows: list[Trade] = []
    with psycopg.connect(database_url, connect_timeout=10) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL statement_timeout TO '30s'")
            for row in conn.execute(query):
                trade = _coerce_trade(row)
                if trade is None:
                    counters["skipped_invalid_or_missing_fields"] += 1
                else:
                    rows.append(trade)
    return rows, dict(counters)


def _json_default(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    database_url = _database_url()
    if not database_url:
        print("缺少 QUANT_DATABASE_URL（环境变量、仓库 .env 或 /tmp/bot_env.txt）",
              file=sys.stderr)
        return 2
    try:
        trades, counters = _read_snapshot(database_url)
    except Exception as exc:  # bounded diagnostic; never print the DSN
        print(f"数据库只读核验失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    report = {
        "read_only": True,
        "trade_row_count": len(trades),
        "skipped": counters,
        **summarize(trades),
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print("R259 per-candle aggregate loss verification (read-only)")
        print(f"trade_rows={report['trade_row_count']} groups={report['group_count']} "
              f"skipped={report['skipped'].get('skipped_invalid_or_missing_fields', 0)}")
        print(f"single_breaches={report['single_breach_count']} "
              f"aggregate_breaches={report['aggregate_breach_count']} "
              f"single_compliant_aggregate_breaches="
              f"{report['single_compliant_aggregate_breach_count']}")
        print(f"worst_aggregate_net_pnl={report['worst_aggregate_net_pnl']}")
        if report["aggregate_limit_satisfied"]:
            print("PASS: no single-compliant aggregate breach remains")
        else:
            print("FAIL: aggregate loss limit is not satisfied")
            for item in report["aggregate_only_examples"][:3]:
                print("  ", json.dumps(item, ensure_ascii=False, sort_keys=True))
    return 0 if report["aggregate_limit_satisfied"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read-only verification for R269/R270 exit-taker fee evidence.

The check uses persisted ``demo_orders`` and ``demo_fills`` rather than the
simulation implementation.  It treats non-reduce-only orders as entries and
reduce-only orders as exits, then verifies the observed commission rate from
``commission / (price * quantity)``.

No write, migration, order submission, or process control is performed.

Usage::

    QUANT_DATABASE_URL="$(tools/dburl.sh)" \
        .venv/bin/python3 tools/verify_r269_fee.py
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path


MAKER_RATE = Decimal("0.0002")
TAKER_RATE = Decimal("0.0004")
RATE_TOLERANCE = Decimal("0.00000001")
STRATEGY_PREFIX = "demo_strategy_%"


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


def _rate(commission, price, quantity) -> Decimal | None:
    if commission is None or price is None or quantity is None:
        return None
    notional = Decimal(price) * Decimal(quantity)
    if notional == 0:
        return None
    return Decimal(commission) / notional


def _summarize(rows: list[dict], *, label: str) -> dict:
    by_role = {"entry": [], "exit": []}
    for row in rows:
        role = "exit" if row["reduce_only"] else "entry"
        by_role[role].append(row)

    print(f"[{label}] fills={len(rows)}")
    summary = {}
    for role, role_rows in by_role.items():
        expected = MAKER_RATE if role == "entry" else TAKER_RATE
        rates = [row["rate"] for row in role_rows if row["rate"] is not None]
        maker = sum(row["is_maker"] is True for row in role_rows)
        taker = sum(row["is_maker"] is False for row in role_rows)
        mismatches = [
            row for row in role_rows
            if row["rate"] is None or abs(row["rate"] - expected) > RATE_TOLERANCE
        ]
        turnover = sum((row["notional"] for row in role_rows), Decimal(0))
        actual_fees = sum((row["commission"] for row in role_rows), Decimal(0))
        modeled_fees = turnover * MAKER_RATE
        summary[role] = {
            "count": len(role_rows),
            "maker": maker,
            "taker": taker,
            "turnover": turnover,
            "actual_fees": actual_fees,
            "maker_model_fees": modeled_fees,
            "mismatches": len(mismatches),
        }
        print(
            f"  {role}: n={len(role_rows)} maker={maker} taker={taker} "
            f"turnover={turnover} actual_fees={actual_fees} "
            f"expected_role_rate={expected} mismatches={len(mismatches)}"
        )
        if rates:
            print(f"    rate_min={min(rates)} rate_max={max(rates)}")
        for row in mismatches[:5]:
            print(
                "    mismatch: "
                f"account={row['account']} client={row['client_id']} "
                f"reduce_only={row['reduce_only']} maker={row['is_maker']} "
                f"rate={row['rate']}"
            )
    return summary


def _load_rows(conn, account_pattern: str) -> list[dict]:
    result = conn.execute(
        """
        SELECT f.virtual_account_key, f.client_id, o.reduce_only, o.status,
               f.price, f.quantity, f.commission, f.commission_asset,
               f.is_maker, f.timestamp_ms
        FROM quant.demo_fills AS f
        JOIN quant.demo_orders AS o
          USING (environment, virtual_account_key, client_id)
        WHERE f.virtual_account_key LIKE %s
          AND o.status IN ('filled', 'canceled')
        ORDER BY f.timestamp_ms, f.virtual_account_key, f.client_id
        """,
        (account_pattern,),
    )
    rows = []
    for record in result.fetchall():
        account, client_id, reduce_only, status, price, quantity, commission, asset, is_maker, timestamp_ms = record
        rows.append(
            {
                "account": account,
                "client_id": client_id,
                "reduce_only": bool(reduce_only),
                "status": status,
                "price": price,
                "quantity": quantity,
                "notional": Decimal(price) * Decimal(quantity),
                "commission": Decimal(commission) if commission is not None else Decimal(0),
                "commission_asset": asset,
                "is_maker": is_maker,
                "timestamp_ms": timestamp_ms,
                "rate": _rate(commission, price, quantity),
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--account-pattern",
        default=STRATEGY_PREFIX,
        help="SQL LIKE pattern (default: demo_strategy_%%)",
    )
    args = parser.parse_args(argv)

    dsn = _database_url()
    if not dsn:
        print("missing QUANT_DATABASE_URL", file=sys.stderr)
        return 2
    try:
        import psycopg
    except ImportError as exc:
        print(f"psycopg unavailable: {exc}", file=sys.stderr)
        return 2

    with psycopg.connect(dsn, connect_timeout=10) as conn:
        rows = _load_rows(conn, args.account_pattern)
    if not rows:
        print("no matching fills")
        return 1

    print(f"account_pattern={args.account_pattern} joined_fills={len(rows)}")
    summary = _summarize(rows, label="all matching strategy accounts")

    entries = summary["entry"]
    exits = summary["exit"]
    entry_turnover = entries["turnover"]
    exit_turnover = exits["turnover"]
    modeled_total = (entry_turnover + exit_turnover) * MAKER_RATE
    actual_total = entries["actual_fees"] + exits["actual_fees"]
    fee_delta = modeled_total - actual_total
    expected_rate_delta = (MAKER_RATE + TAKER_RATE) - (MAKER_RATE + MAKER_RATE)
    print("fee_contract:")
    print(f"  current_two_maker_rate={MAKER_RATE + MAKER_RATE}")
    print(f"  observed_maker_entry_plus_taker_exit={MAKER_RATE + TAKER_RATE}")
    print(f"  expected_normalized_delta={-expected_rate_delta} (P&L shift)")
    print(f"  modeled_fees_at_two_maker={modeled_total}")
    print(f"  observed_fees_from_fills={actual_total}")
    print(f"  modeled_minus_observed={fee_delta}")
    print(
        "  observed_role_contract_ok="
        f"{entries['maker'] == entries['count'] and exits['taker'] == exits['count']}"
    )

    problems = []
    if entries["count"] and entries["maker"] != entries["count"]:
        problems.append("not every entry fill is maker")
    if exits["count"] and exits["taker"] != exits["count"]:
        problems.append("not every exit fill is taker")
    if entries["mismatches"] or exits["mismatches"]:
        problems.append("one or more role rates differ from 0.0002/0.0004")
    if not entry_turnover or not exit_turnover:
        problems.append("missing one side of a round trip")
    if problems:
        print("result=FAIL")
        for problem in problems:
            print(f"  problem={problem}")
        return 1
    print("result=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

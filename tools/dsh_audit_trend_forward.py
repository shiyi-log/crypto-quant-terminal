#!/usr/bin/env python3
"""Read-only audit of the TrendFollowing dry-run forward database."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def audit(db_path: Path) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "select pair,is_open,open_date,close_date,close_profit_abs,close_profit,exit_reason "
        "from trades order by id"
    )]
    con.close()
    closed = [r for r in rows if not r["is_open"]]
    open_rows = [r for r in rows if r["is_open"]]
    pnls = [float(r["close_profit_abs"] or 0.0) for r in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "db": str(db_path),
        "closed_trades": len(closed),
        "open_trades": len(open_rows),
        "closed_pnl_usdt": round(sum(pnls), 2),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed), 4) if closed else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses else None,
        "max_drawdown_closed_usdt": round(max_dd, 2),
        "open_pairs": [r["pair"] for r in open_rows],
        "last_closed": closed[-1]["close_date"] if closed else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path("bot/tradesv3.dryrun.sqlite"))
    args = ap.parse_args()
    print(json.dumps(audit(args.db), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

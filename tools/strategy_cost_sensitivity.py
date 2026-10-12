#!/usr/bin/env python3
"""Read-only fee repricing of complete frozen simulation trade ledgers.

This is a fixed-fill, fixed-size audit, not an execution replay.  PostgreSQL
reads use one repeatable-read, read-only transaction without DataStore schema
initialization.  An API detail/export may also be audited, but a truncated
closed-trade list is rejected instead of silently treated as a full sample.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime_config import load_environment
from strategy_simulation import SAMPLE_QUALITY_CUTOFF_MS, sample_annotation


SCENARIOS = {"maker": 0.0002, "taker": 0.0004, "stress": 0.001}
METHOD = "fixed_fills_fixed_quantity_fee_repricing_v1"
LIMITATIONS = [
    "Taker 0.0004 is verified against Demo actual fills; maker 0.0002 is an account quote without an actual maker fill.",
    "Fill prices, quantities, timestamps and modeled gross PnL are frozen.",
    "Higher fees do not resize ATR risk, change cash constraints or replay liquidation.",
    "The next-open proxy cannot establish maker/taker role or limit-order non-fill risk.",
    "Funding, slippage, liquidation surcharges and currently open positions are excluded.",
    "A closed leg is not an independent strategy sample; paired legs and factor accounts are correlated.",
    "Population totals are accounting summaries and cannot be pooled as profitability evidence.",
    "No fee result automatically archives or certifies a strategy.",
]


def number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite numeric data")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite numeric data") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{name} must be finite{' positive' if positive else ''} numeric data")
    return result


def count(value: Any, name: str) -> int:
    result = number(value, name)
    if result < 0 or int(result) != result:
        raise ValueError(f"{name} must be a nonnegative integer")
    return int(result)


def equal_money(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-7)


def normalize_snapshot(value: dict) -> dict:
    """Accept our database snapshot or the current account_detail API shape."""
    if not isinstance(value, dict):
        raise ValueError("input must be a snapshot or account_detail object")
    if "account" in value:
        accounts = [value["account"]]
        trades = value.get("trades", [])
        source = {"kind": "account_detail_export", "truncation_checked": True}
    else:
        accounts, trades = value.get("accounts"), value.get("trades")
        supplied_source = value.get("source", {})
        if not isinstance(supplied_source, dict):
            raise ValueError("snapshot source must be an object")
        source = {key: supplied_source[key] for key in (
            "kind", "snapshot_at_utc", "last_ledger_sequence_id",
            "closed_trade_events_read_without_limit") if key in supplied_source}
        source.setdefault("kind", "snapshot_export")
    if not isinstance(accounts, list) or not isinstance(trades, list):
        raise ValueError("input requires accounts and trades lists")
    normalized = {}
    for account in accounts:
        if not isinstance(account, dict) or not account.get("strategy_id"):
            raise ValueError("account requires a strategy_id")
        identity = account["strategy_id"]
        if identity in normalized:
            raise ValueError("duplicate account identity")
        state = account.get("state", account)
        if not isinstance(state, dict):
            raise ValueError("account state must be an object")
        closed_count = count(state.get("closed_trade_count"), "closed_trade_count")
        definition, execution = account.get("definition"), account.get("execution")
        if not isinstance(definition, dict) or not isinstance(execution, dict):
            raise ValueError("account requires frozen definition and execution")
        if definition.get("strategy_id") != identity:
            raise ValueError("definition identity differs from account identity")
        rate = number(execution.get("fee_rate_per_side"), "frozen fee_rate_per_side")
        if rate < 0 or rate >= 0.01:
            raise ValueError("frozen fee_rate_per_side must be in [0, .01)")
        normalized[identity] = dict(account, state=state, closed_trade_count=closed_count,
                                    definition=definition,
                                    execution=dict(execution, fee_rate_per_side=rate))
    grouped = defaultdict(list)
    for event in trades:
        if not isinstance(event, dict):
            raise ValueError("each closed trade must be an object")
        if "payload" in event:
            if event.get("event_type", "trade_closed") != "trade_closed":
                raise ValueError("only trade_closed events are accepted")
            payload = event["payload"]
            if not isinstance(payload, dict):
                raise ValueError("trade payload must be an object")
            if event.get("strategy_id") != payload.get("strategy_id"):
                raise ValueError("event identity differs from trade identity")
        else:
            payload = event
        identity = payload.get("strategy_id")
        if identity not in normalized:
            raise ValueError("trade has no frozen account identity")
        if ("payload" in event and event.get("rule_hash") is not None
                and event["rule_hash"] != payload.get("rule_hash")):
            raise ValueError("event rule hash differs from trade rule hash")
        grouped[identity].append(payload)
    for identity, account in normalized.items():
        if len(grouped[identity]) != account["closed_trade_count"]:
            raise ValueError(f"incomplete closed-trade ledger for {identity}; export all closed legs")
    return {"source": source, "accounts": normalized, "trades": grouped}


def reprice_leg(account: dict, trade: dict, scenarios: dict[str, float]) -> dict:
    """Charge both actual notionals; retain the engine's frozen liquidation PnL."""
    if not trade.get("trade_id") or not trade.get("pair"):
        raise ValueError("closed trade requires trade_id and pair")
    if not account.get("rule_hash") or trade.get("rule_hash") != account["rule_hash"]:
        raise ValueError("trade rule hash differs from frozen account rule hash")
    quantity = number(trade.get("quantity"), "quantity", positive=True)
    entry = number(trade.get("entry_price"), "entry_price", positive=True)
    exit_price = number(trade.get("exit_price"), "exit_price", positive=True)
    opened = count(trade.get("opened_candle_ms"), "opened_candle_ms")
    closed = count(trade.get("closed_candle_ms"), "closed_candle_ms")
    if closed < opened:
        raise ValueError("closed trade ends before it opens")
    entry_notional, exit_notional = quantity * entry, quantity * exit_price
    turnover = entry_notional + exit_notional
    if not all(math.isfinite(v) for v in (entry_notional, exit_notional, turnover)):
        raise ValueError("trade notional overflow")
    if "notional" in trade and not equal_money(number(trade["notional"], "notional"),
                                               entry_notional):
        raise ValueError("entry notional does not reconcile to quantity * entry price")
    gross = number(trade.get("gross_pnl"), "gross_pnl")
    recorded_fees = number(trade.get("fees"), "fees")
    recorded_net = number(trade.get("net_pnl"), "net_pnl")
    if recorded_fees < 0 or not equal_money(recorded_net, gross - recorded_fees):
        raise ValueError("recorded gross/fees/net do not reconcile")
    frozen_rate = account["execution"]["fee_rate_per_side"]
    if not equal_money(recorded_fees, turnover * frozen_rate):
        raise ValueError("recorded fees do not reconcile to the frozen fee contract")
    side = trade.get("side")
    if side not in ("long", "short"):
        raise ValueError("closed trade requires long/short side")
    liquidation = trade.get("liquidation", False)
    if not isinstance(liquidation, bool):
        raise ValueError("liquidation must be a boolean")
    # The engine deliberately clamps gross PnL to posted margin on liquidation.
    if not liquidation:
        calculated = (1 if side == "long" else -1) * quantity * (exit_price - entry)
        if not equal_money(gross, calculated):
            raise ValueError("ordinary trade gross does not reconcile to its fills")
    prices = {}
    maker_net = gross - turnover * SCENARIOS["maker"]
    for label, rate in scenarios.items():
        fees = turnover * rate
        prices[label] = {"fee_rate_per_side": rate, "entry_fee_usdt": entry_notional * rate,
                         "exit_fee_usdt": exit_notional * rate, "fees_usdt": fees,
                         "net_pnl_usdt": gross - fees,
                         "additional_fee_vs_maker_usdt": turnover * (rate - SCENARIOS["maker"]),
                         "maker_positive_to_nonpositive": maker_net > 0 and gross - fees <= 0}
    return {"trade_id": trade["trade_id"], "pair": trade["pair"], "side": side,
            "group_id": trade.get("group_id"), "quantity": quantity,
            "entry_price": entry, "exit_price": exit_price,
            "entry_notional_usdt": entry_notional, "exit_notional_usdt": exit_notional,
            "fee_turnover_usdt": turnover, "gross_pnl_usdt": gross,
            "recorded_fees_usdt": recorded_fees, "recorded_net_pnl_usdt": recorded_net,
            "opened_candle_ms": opened, "closed_candle_ms": closed,
            "liquidation": liquidation, "exit_reason": trade.get("exit_reason"),
            "breakeven_fee_rate_per_side": gross / turnover,
            **sample_annotation(account["definition"], trade), "scenarios": prices}


def aggregate(legs: list[dict], scenarios: dict[str, float]) -> dict:
    gross = math.fsum(leg["gross_pnl_usdt"] for leg in legs)
    turnover = math.fsum(leg["fee_turnover_usdt"] for leg in legs)
    result = {"closed_leg_count": len(legs), "gross_pnl_usdt": gross,
              "entry_notional_usdt": math.fsum(leg["entry_notional_usdt"] for leg in legs),
              "exit_notional_usdt": math.fsum(leg["exit_notional_usdt"] for leg in legs),
              "fee_turnover_usdt": turnover,
              "breakeven_fee_rate_per_side": gross / turnover if turnover else None,
              "scenarios": {}}
    for label, rate in scenarios.items():
        fees = math.fsum(leg["scenarios"][label]["fees_usdt"] for leg in legs)
        net = gross - fees
        result["scenarios"][label] = {
            "fee_rate_per_side": rate, "fees_usdt": fees, "net_pnl_usdt": net,
            "mean_net_pnl_per_closed_leg_usdt": net / len(legs) if legs else None,
            "additional_fee_vs_maker_usdt": turnover * (rate - SCENARIOS["maker"]),
            "positive_closed_leg_count": sum(leg["scenarios"][label]["net_pnl_usdt"] > 0
                                             for leg in legs),
        }
    return result


def analyze_snapshot(value: dict, *, population: str = "all",
                     scenarios: dict[str, float] | None = None) -> dict:
    scenarios = SCENARIOS.copy() if scenarios is None else dict(scenarios)
    if not scenarios:
        raise ValueError("at least one fee scenario is required")
    for label, rate in list(scenarios.items()):
        rate = number(rate, "scenario fee")
        if not isinstance(label, str) or not label or rate < 0 or rate >= .01:
            raise ValueError("scenario labels must be strings and fees in [0, .01)")
        scenarios[label] = rate
    if population not in ("all", "factorial", "legacy"):
        raise ValueError("population must be all, factorial or legacy")
    data = normalize_snapshot(value)
    accounts, all_legs, populations = [], [], defaultdict(list)
    for identity, account in sorted(data["accounts"].items()):
        kind = "factorial" if account["definition"].get("factorial_design_version") else "legacy"
        if population != "all" and population != kind:
            continue
        legs, seen = [], set()
        for trade in data["trades"][identity]:
            leg = reprice_leg(account, trade, scenarios)
            if leg["trade_id"] in seen:
                raise ValueError("duplicate closed trade identity")
            seen.add(leg["trade_id"])
            legs.append(leg)
        eligible = [leg for leg in legs if leg["eligible_for_factorial_evaluation"]]
        groups = {leg["group_id"] for leg in legs if leg["group_id"]}
        positions = account["state"].get("positions", account.get("open_positions", []))
        positions = list(positions.values()) if isinstance(positions, dict) else positions
        if not isinstance(positions, list):
            raise ValueError("open positions must be a list or object")
        open_groups = {p.get("group_id") for p in positions if isinstance(p, dict) and p.get("group_id")}
        accounts.append({
            "strategy_id": identity, "population": kind,
            "rule_hash": account["rule_hash"], "execution_hash": account.get("execution_hash"),
            "frozen_fee_rate_per_side": account["execution"]["fee_rate_per_side"],
            "execution_enabled": account["definition"].get("execution_enabled", True),
            "blocked_execution_reason": account["definition"].get("blocked_execution_reason"),
            "all_complete_closed_legs": aggregate(legs, scenarios),
            "eligible_factorial_closed_legs": aggregate(eligible, scenarios),
            "group_ids_observed_on_closed_legs_count": len(groups),
            "closed_group_ids_with_still_open_legs_count": len(groups & open_groups),
            "excluded_open_position_count": len(positions),
            "evidence_status": ("no_eligible_factorial_samples" if not eligible else
                                "insufficient_samples" if len(eligible) < 100 else
                                "requires_independence_check_and_joint_review"),
            "closed_legs": legs,
        })
        all_legs.extend(legs)
        populations[kind].extend(legs)
    return {"method": METHOD, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": data["source"], "population_filter": population,
            "account_count": len(accounts), "sample_quality_cutoff_ms": SAMPLE_QUALITY_CUTOFF_MS,
            "scenario_rates_per_side": scenarios, "limitations": LIMITATIONS,
            "population_accounting_totals_not_independent_evidence": {
                kind: aggregate(legs, scenarios) for kind, legs in sorted(populations.items())},
            "all_complete_closed_legs": aggregate(all_legs, scenarios),
            "eligible_factorial_closed_legs": aggregate(
                [leg for leg in all_legs if leg["eligible_for_factorial_evaluation"]], scenarios),
            "accounts": accounts}


def read_database_snapshot(*, schema: str = "quant") -> dict:
    """No migration, writes or locks; retain every closed event without LIMIT."""
    load_environment()
    dsn = os.getenv("QUANT_DATABASE_URL")
    if not dsn:
        raise ValueError("QUANT_DATABASE_URL is not configured")
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True, connect_timeout=10) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
            conn.execute("SET LOCAL statement_timeout TO '120s'")
            stamp = conn.execute("SELECT transaction_timestamp() AS snapshot_at").fetchone()
            watermark = conn.execute("SELECT MAX(sequence_id) AS last_sequence_id FROM strategy_sim_events").fetchone()
            accounts = conn.execute("""SELECT strategy_id,rule_hash,execution_hash,definition,
                execution,state FROM strategy_sim_accounts ORDER BY strategy_id""").fetchall()
            trades = conn.execute("""SELECT sequence_id,event_id,strategy_id,event_type,rule_hash,payload
                FROM strategy_sim_events WHERE event_type='trade_closed' ORDER BY sequence_id""").fetchall()
    return {"accounts": accounts, "trades": trades,
            "source": {"kind": "postgresql_read_only_repeatable_read",
                       "snapshot_at_utc": stamp["snapshot_at"].astimezone(timezone.utc).isoformat(),
                       "last_ledger_sequence_id": watermark["last_sequence_id"],
                       "closed_trade_events_read_without_limit": len(trades)}}


def compact_summary(report: dict) -> dict:
    return {"method": report["method"], "account_count": report["account_count"],
            "closed_leg_count": report["all_complete_closed_legs"]["closed_leg_count"],
            "eligible_factorial_closed_leg_count": report["eligible_factorial_closed_legs"]["closed_leg_count"],
            "gross_pnl_usdt": report["all_complete_closed_legs"]["gross_pnl_usdt"],
            "scenario_net_pnl_usdt": {name: value["net_pnl_usdt"] for name, value in
                                      report["all_complete_closed_legs"]["scenarios"].items()},
            "source": report["source"], "scope": "accounting_only_not_pooled_strategy_evidence"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="offline complete snapshot/account_detail JSON")
    parser.add_argument("--schema", default="quant", help="database schema; default quant")
    parser.add_argument("--population", choices=("all", "factorial", "legacy"), default="all")
    parser.add_argument("--output", type=Path, help="local JSON audit report (no database writes)")
    args = parser.parse_args(argv)
    try:
        snapshot = (json.loads(args.input.read_text(encoding="utf-8")) if args.input
                    else read_database_snapshot(schema=args.schema))
        report = analyze_snapshot(snapshot, population=args.population)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, allow_nan=False, indent=2)
                handle.write("\n")
            print(json.dumps(dict(compact_summary(report), output=str(args.output)),
                             ensure_ascii=False, allow_nan=False))
        else:
            print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    except (ValueError, OSError) as exc:
        # Do not echo arbitrary exported fields, filesystem content or database DSNs.
        print(f"Audit failed ({type(exc).__name__}); verify complete input and local configuration.",
              file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Read-only audit failed ({type(exc).__name__}); no database writes were attempted.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""DSH read-only audit for R11 forward-paper ledgers (T025–T028)."""
from __future__ import annotations
import argparse, json
from pathlib import Path

def read_jsonl(p):
    if not p.exists(): return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="artifacts/r11_forward_paper_t027_20261025")
    ap.add_argument("--prefix", default="dryrun_")
    a = ap.parse_args(); d = Path(a.dir); p = a.prefix
    ledger = read_jsonl(d / f"{p}ledger_events.jsonl")
    eq = read_jsonl(d / f"{p}equity_daily.jsonl")
    state = json.loads((d / f"{p}equity_state.json").read_text()) if (d / f"{p}equity_state.json").exists() else {}
    checks, fails = [], []
    def chk(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok: fails.append(f"{name}: {detail}")
    by = {}
    for e in ledger: by.setdefault(e.get("position_id"), []).append(e)
    for pid, evs in by.items():
        if pid is None: continue
        kinds = [e.get("event") for e in evs]
        chk(f"{pid}:risk_gate_before_fill", "risk_gate" in kinds and kinds.index("risk_gate") < kinds.index("fill") if "fill" in kinds else True, str(kinds))
        if "fill" in kinds:
            fill = evs[kinds.index("fill")]
            chk(f"{pid}:fill_qty", abs(float(fill.get("quantity_per_leg", 0)) - 0.1) < 1e-9, str(fill.get("quantity_per_leg")))
        entry_premium = None
        if "fill" in kinds:
            entry_premium = float(evs[kinds.index("fill")].get("paper_premium_bid_usd") or evs[kinds.index("fill")].get("premium_usd") or 0.0)
        for e in evs:
            if e.get("event") == "exit":
                exp = float(e.get("realized_pnl_usd", 0)); ask = float(e.get("exit_premium_usd", 0))
                if entry_premium is not None:
                    chk(f"{pid}:exit_realized", abs(exp - (entry_premium - ask)) < 1e-6, f"{exp} vs {entry_premium}-{ask}")
                if "spread_cost_usd" in e:
                    chk(f"{pid}:spread_cost", float(e["spread_cost_usd"]) >= -1e-6, str(e["spread_cost_usd"]))
    prev = None
    for i, r in enumerate(eq):
        eqv = float(r["equity"]); dp = float(r["daily_pnl_usd"])
        if prev is not None:
            chk(f"equity_row{i}:daily_pnl", abs(dp - (eqv - prev)) < 1e-4, f"{dp} vs {eqv-prev}")
        obs = int(r.get("observations", -1))
        chk(f"equity_row{i}:obs", obs == (0 if p == "dryrun_" else i + 1), str(obs))
        chk(f"equity_row{i}:active", int(r.get("active_positions", -1)) >= 0, str(r.get("active_positions")))
        prev = eqv
    if state:
        chk("state:cash_equity", "cash_equity" in state, str(list(state)))
        chk("state:positions", isinstance(state.get("positions"), dict), str(type(state.get("positions"))))
    out = {"dir": str(d), "prefix": p, "ledger_events": len(ledger), "positions": len(by), "equity_rows": len(eq), "checks": len(checks), "fails": fails, "status": "PASS" if not fails else "FIX_REQUIRED"}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if not fails else 1

if __name__ == "__main__":
    raise SystemExit(main())

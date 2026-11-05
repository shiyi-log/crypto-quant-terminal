#!/usr/bin/env python3
"""Run the R11 forward-paper job daily at 08:00 Asia/Shanghai (00:00 UTC)."""
from __future__ import annotations
import os, subprocess, time
from datetime import datetime, timedelta
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/r11_forward_paper_t032_20261025"
LOG = OUT / "logs" / "watchdog.log"
def next_run(now: datetime) -> datetime:
    target = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target
def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True); (OUT / "logs").mkdir(exist_ok=True)
    while True:
        now = datetime.now().astimezone()
        target = next_run(now)
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(f"{now.isoformat()} sleeping until {target.isoformat()}\n")
        time.sleep(max(1.0, (target - now).total_seconds()))
        with LOG.open("a") as fh:
            fh.write(f"{datetime.now().astimezone().isoformat()} running forward job\n")
        for attempt in range(1, 4):
            try:
                env = os.environ.copy()
                env["NO_PROXY"] = "*"; env["no_proxy"] = "*"
                env["PYTHONPATH"] = str(ROOT / "tools/deribit_dns_fix") + ":" + env.get("PYTHONPATH", "")
                rc = subprocess.run([str(ROOT / ".venv/bin/python"), str(ROOT / "tools/run_r11_forward_paper_t032.py"),
                                     "--output", str(OUT), "--daily", "--record-entry"],
                                    cwd=ROOT, env=env, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
            except Exception as exc:  # noqa: BLE001
                rc = -1
                with LOG.open("a") as fh:
                    fh.write(f"attempt {attempt} error: {exc}\n")
            if rc == 0:
                break
            with LOG.open("a") as fh:
                fh.write(f"attempt {attempt} failed rc={rc}; retry in 60s\n")
            time.sleep(60)
        time.sleep(120)
if __name__ == "__main__":
    raise SystemExit(main())

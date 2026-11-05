#!/usr/bin/env python3
"""T031 conditional forward-paper wrapper.

It reuses the T028 executable-size state machine but gates new entries on the
trailing-DVOL median rule. The unconditional T028 artifact remains available
as the baseline comparison and this wrapper never connects an order endpoint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.run_r11_forward_paper_executable import run as executable_run  # noqa: E402
from tools.run_r11_vol_risk_premium import sha256_file  # noqa: E402

SPEC_PATH = ROOT / "research_multi_strategy/R11_CONDITIONAL_T031_20261025.md"
OUTPUT_DEFAULT = ROOT / "artifacts/r11_forward_paper_t031_20261025"
RUN_ID = "r11-forward-paper-t031-20261025-v1"
LABEL = "com.quant.r11forward.conditional"
UTC = timezone.utc


def run(output: Path = OUTPUT_DEFAULT, *, now_ms: int | None = None, record_entry: bool = False,
        daily: bool = False, dry_run_lifecycle: bool = False) -> dict:
    report = executable_run(output, now_ms=now_ms, record_entry=record_entry, daily=daily,
                            dry_run_lifecycle=dry_run_lifecycle, conditional_entry=True)
    report.update({"task": "T031", "run_id": RUN_ID, "strategy_id": "r11_forward_paper_conditional_v1",
                   "spec_sha256": sha256_file(SPEC_PATH), "conditional_rule": "entry_dvol_1h >= trailing_252d_median_252_valid_days",
                   "unconditional_baseline": "artifacts/r11_forward_paper_t027_20261025", "launchd_label": LABEL})
    (output / "REPORT.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    report_md = output / "REPORT.md"
    if report_md.exists():
        text = report_md.read_text().replace("# T028 executable-size daily forward paper", "# T031 conditional executable-size daily forward paper")
        text += "\n- conditional rule: `entry_dvol_1h >= trailing_252d_median_252_valid_days`\n- low-IV behavior: `skipped_low_iv` with no fill\n- unconditional baseline: `artifacts/r11_forward_paper_t027_20261025`\n"
        report_md.write_text(text)
    plist = output / f"{LABEL}.plist"
    plist.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LABEL}</string>
<key>ProgramArguments</key><array>
<string>/usr/bin/python3</string><string>{ROOT / 'tools/run_r11_forward_paper_conditional.py'}</string>
<string>--output</string><string>{output}</string><string>--daily</string><string>--record-entry</string>
</array>
<key>WorkingDirectory</key><string>{ROOT}</string>
<key>StartCalendarInterval</key><dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
<key>StandardOutPath</key><string>{output / 'launchd.log'}</string>
<key>StandardErrorPath</key><string>{output / 'launchd.error.log'}</string>
</dict></plist>
''')
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest.update({"task": "T031", "run_id": RUN_ID, "execution_evidence": "forward_paper", "spec_sha256": sha256_file(SPEC_PATH),
                     "code_sha256": sha256_file(Path(__file__)), "state_runner_code_sha256": sha256_file(ROOT / "tools/run_r11_forward_paper_t027.py"),
                     "unconditional_baseline": "artifacts/r11_forward_paper_t027_20261025", "launchd_label": LABEL,
                     "generated_at_utc": datetime.now(UTC).isoformat()})
    files = {}
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files[str(path.resolve().relative_to(ROOT.resolve()))] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    manifest["files"] = files
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--daily", action="store_true")
    parser.add_argument("--record-entry", action="store_true")
    parser.add_argument("--dry-run-lifecycle", action="store_true")
    parser.add_argument("--timestamp-ms", type=int, default=None)
    args = parser.parse_args()
    report = run(args.output, now_ms=args.timestamp_ms, record_entry=args.record_entry, daily=args.daily, dry_run_lifecycle=args.dry_run_lifecycle)
    print(report["status"], report["observations"], report["conditional_entry"].get("decision"))


if __name__ == "__main__":
    main()

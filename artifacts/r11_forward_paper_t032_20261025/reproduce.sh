#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
python3 tools/run_r11_forward_paper_t032.py --output artifacts/r11_forward_paper_t032_20261025 --once --record-entry --dry-run-lifecycle
# Live daily: --daily --record-entry at 00:00 UTC.

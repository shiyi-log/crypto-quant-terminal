#!/bin/zsh
# Minimal process supervisor for the quant terminal. Idempotent: manage.py start
# reuses live PIDs; only starts missing services. Does not handle services that
# need extra identity/config (strategy-lab) or are intentionally gated off.
set -u
ROOT="/Users/shiyi/Documents/ChatGPT/量化"
cd "$ROOT" || exit 1
LOG="$ROOT/logs/supervisor.log"
SERVICES=(api webserver auth web sync market market-trx market-spot market-live)

log() { print -r -- "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

while true; do
  for svc in "${SERVICES[@]}"; do
    out=$(.venv/bin/python manage.py start "$svc" 2>&1)
    if print -r -- "$out" | grep -q "已启动"; then
      log "started $svc"
    elif print -r -- "$out" | grep -q "操作失败"; then
      log "FAILED $svc: $out"
    fi
  done

  # integrity: launched manually because manage.py start lacks PYTHONPATH for it.
  if ! pgrep -f "tools/check_candle_integrity.py --watch" >/dev/null 2>&1; then
    PYTHONPATH="$ROOT" nohup .venv/bin/python -u tools/check_candle_integrity.py \
      --watch --interval 300 >> "$ROOT/logs/integrity.log" 2>&1 &
    log "started integrity (manual PYTHONPATH)"
  fi

  # Data freshness log only (no restarts): warn if newest futures 1m is older than 30 min.
  .venv/bin/python - <<'PY' 2>/dev/null | while read -r line; do
import sys
from datetime import datetime, timezone
from data_store import DataStore
s = DataStore().initialize()
with s._connection() as c:
    r = c.execute("select max(timestamp) mx from candles where market='futures' and candle_type='futures' and timeframe='1m'").fetchone()
    age = (datetime.now(timezone.utc) - datetime.fromtimestamp(int(r['mx'])/1000, timezone.utc)).total_seconds()/60
    if age > 30:
        print(f"STALE futures 1m age={age:.0f}min")
s.close()
PY
    log "$line"
  done

  [[ "${SUPERVISOR_ONCE:-0}" == "1" ]] && exit 0
  sleep 60
done

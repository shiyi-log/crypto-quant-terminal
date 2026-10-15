#!/usr/bin/env bash
# 解析并输出 QUANT_DATABASE_URL。
#
# 为什么存在：/tmp 会被 macOS 周期清理（2026-10-15 实际发生，导致所有依赖
# /tmp/bot_env.txt 的检查命令静默失效）。仓库内的 .env 才是权威来源。
#
# 用法:
#   export QUANT_DATABASE_URL="$(tools/dburl.sh)"
#   QUANT_DATABASE_URL="$(tools/dburl.sh)" .venv/bin/python3 tools/xxx.py
#
# 解析顺序: 环境变量 → 仓库 .env → /tmp/bot_env.txt
set -euo pipefail

if [[ -n "${QUANT_DATABASE_URL:-}" ]]; then
  printf '%s' "$QUANT_DATABASE_URL"
  exit 0
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for candidate in "$root/.env" /tmp/bot_env.txt; do
  if [[ -f "$candidate" ]]; then
    line="$(grep -m1 '^QUANT_DATABASE_URL=' "$candidate" || true)"
    if [[ -n "$line" ]]; then
      printf '%s' "${line#QUANT_DATABASE_URL=}"
      exit 0
    fi
  fi
done

echo "dburl.sh: 未找到 QUANT_DATABASE_URL（环境变量、$root/.env、/tmp/bot_env.txt 均无）" >&2
exit 1

#!/bin/zsh
# One repository definition owns lifecycle and persistent operator intent.
set -eu
TASK_ROOT="${QUANT_PROJECT_ROOT:-${0:A:h:h}}"
cd "$TASK_ROOT"
TASK_ARGS=(supervise)
[[ "${SUPERVISOR_ONCE:-0}" == "1" ]] && TASK_ARGS+=(--once)
exec "$TASK_ROOT/.venv/bin/python" "$TASK_ROOT/manage.py" "${TASK_ARGS[@]}" "$@"

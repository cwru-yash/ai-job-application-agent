#!/bin/zsh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="${HOME}/.applypilot/run_always_on.pid"

if [[ -f "${PID_FILE}" ]]; then
  pid="$(cat "${PID_FILE}")"
  if [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" >/dev/null 2>&1; then
    kill "${pid}" >/dev/null 2>&1 || true
    echo "Stopped always-on supervisor PID ${pid}"
  fi
  rm -f "${PID_FILE}"
fi

pkill -f "run_always_on.sh" >/dev/null 2>&1 || true
pkill -f "daily_concurrent.py" >/dev/null 2>&1 || true
pkill -f "python -m applypilot.cli apply" >/dev/null 2>&1 || true
pkill -f "local_apply_agent.py" >/dev/null 2>&1 || true

echo "Always-on processes stopped."

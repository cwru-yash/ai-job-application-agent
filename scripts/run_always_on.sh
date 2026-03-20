#!/bin/zsh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${HOME}/.applypilot/logs"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOG_FILE="${LOG_DIR}/always_on_${TIMESTAMP}.log"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

SESSION_PAUSE_SECONDS="${APPLYPILOT_ALWAYS_ON_SESSION_PAUSE_SECONDS:-60}"
ERROR_PAUSE_SECONDS="${APPLYPILOT_ALWAYS_ON_ERROR_PAUSE_SECONDS:-180}"

mkdir -p "${LOG_DIR}"

iteration=0

{
  echo "[$(date)] Starting always-on supervisor"
  echo "[$(date)] Repo: ${REPO_ROOT}"
  echo "[$(date)] Session pause seconds: ${SESSION_PAUSE_SECONDS}"
  echo "[$(date)] Error pause seconds: ${ERROR_PAUSE_SECONDS}"

  while true; do
    iteration=$((iteration + 1))
    echo
    echo "[$(date)] Supervisor iteration ${iteration}: starting concurrent session"

    set +e
    python3 "${REPO_ROOT}/scripts/daily_concurrent.py"
    exit_code=$?
    set -e

    echo "[$(date)] Supervisor iteration ${iteration}: session exit code ${exit_code}"

    if [[ "${exit_code}" -eq 0 ]]; then
      echo "[$(date)] Sleeping ${SESSION_PAUSE_SECONDS}s before next session"
      sleep "${SESSION_PAUSE_SECONDS}"
    else
      echo "[$(date)] Sleeping ${ERROR_PAUSE_SECONDS}s after failure"
      sleep "${ERROR_PAUSE_SECONDS}"
    fi
  done
} 2>&1 | tee -a "${LOG_FILE}"

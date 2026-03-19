#!/bin/zsh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${HOME}/.applypilot/logs"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOG_FILE="${LOG_DIR}/daily_run_${TIMESTAMP}.log"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

MIN_SCORE="${APPLYPILOT_DAILY_MIN_SCORE:-7}"
APPLY_LIMIT="${APPLYPILOT_DAILY_APPLY_LIMIT:-3}"
WORKERS="${APPLYPILOT_DAILY_WORKERS:-1}"

mkdir -p "${LOG_DIR}"

{
  echo "[$(date)] Starting daily pipeline"
  echo "[$(date)] Repo: ${REPO_ROOT}"
  echo "[$(date)] Settings: min_score=${MIN_SCORE} apply_limit=${APPLY_LIMIT} workers=${WORKERS}"

  python3 -m applypilot.cli run discover enrich score tailor cover pdf --min-score "${MIN_SCORE}"

  echo "[$(date)] Pipeline finished"
  echo "[$(date)] Starting auto-apply"

  set +e
  python3 -m applypilot.cli apply --limit "${APPLY_LIMIT}" --workers "${WORKERS}" --headless --agent-backend command
  APPLY_EXIT=$?
  set -e

  echo "[$(date)] Auto-apply exit code: ${APPLY_EXIT}"
  echo "[$(date)] Final status snapshot"
  python3 -m applypilot.cli status || true
} 2>&1 | tee -a "${LOG_FILE}"

exit 0

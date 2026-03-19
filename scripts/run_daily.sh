#!/bin/zsh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${HOME}/.applypilot/logs"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOG_FILE="${LOG_DIR}/daily_run_${TIMESTAMP}.log"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

MIN_SCORE="${APPLYPILOT_DAILY_MIN_SCORE:-8}"
SCORE_LIMIT="${APPLYPILOT_DAILY_SCORE_LIMIT:-90}"
TAILOR_LIMIT="${APPLYPILOT_DAILY_TAILOR_LIMIT:-35}"
COVER_LIMIT="${APPLYPILOT_DAILY_COVER_LIMIT:-35}"
APPLY_LIMIT="${APPLYPILOT_DAILY_APPLY_LIMIT:-30}"
WORKERS="${APPLYPILOT_DAILY_WORKERS:-1}"
APPLY_MIN_SCORE="${APPLYPILOT_DAILY_APPLY_MIN_SCORE:-8}"
VALIDATION_MODE="${APPLYPILOT_DAILY_VALIDATION:-lenient}"

mkdir -p "${LOG_DIR}"

{
  echo "[$(date)] Starting daily pipeline"
  echo "[$(date)] Repo: ${REPO_ROOT}"
  echo "[$(date)] Settings: min_score=${MIN_SCORE} score_limit=${SCORE_LIMIT} tailor_limit=${TAILOR_LIMIT} cover_limit=${COVER_LIMIT} apply_limit=${APPLY_LIMIT} apply_min_score=${APPLY_MIN_SCORE} validation=${VALIDATION_MODE} workers=${WORKERS}"

  python3 "${REPO_ROOT}/scripts/daily_pipeline.py"

  echo "[$(date)] Pipeline finished"
  echo "[$(date)] Starting auto-apply"

  set +e
  python3 -m applypilot.cli apply --limit "${APPLY_LIMIT}" --workers "${WORKERS}" --min-score "${APPLY_MIN_SCORE}" --headless --agent-backend command
  APPLY_EXIT=$?
  set -e

  echo "[$(date)] Auto-apply exit code: ${APPLY_EXIT}"
  echo "[$(date)] Final status snapshot"
  python3 -m applypilot.cli status || true
} 2>&1 | tee -a "${LOG_FILE}"

exit 0

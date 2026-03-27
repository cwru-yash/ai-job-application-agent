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
TARGET_SUBMISSIONS="${APPLYPILOT_DAILY_TARGET_SUBMISSIONS:-25}"
APPLY_BATCH="${APPLYPILOT_DAILY_APPLY_BATCH:-${APPLYPILOT_DAILY_APPLY_LIMIT:-5}}"
WORKERS="${APPLYPILOT_DAILY_WORKERS:-1}"
APPLY_MIN_SCORE="${APPLYPILOT_DAILY_APPLY_MIN_SCORE:-8}"
VALIDATION_MODE="${APPLYPILOT_DAILY_VALIDATION:-lenient}"
MAX_CYCLES="${APPLYPILOT_DAILY_MAX_CYCLES:-6}"

mkdir -p "${LOG_DIR}"

python3 "${REPO_ROOT}/scripts/log_session_event.py" \
  control_action \
  --mode daily_batch \
  --pid $$ \
  --log-path "${LOG_FILE}" \
  --message "run_daily.sh invoked" \
  --field action=run_daily \
  --field max_cycles="${MAX_CYCLES}" \
  --field target_submissions="${TARGET_SUBMISSIONS}" >/dev/null 2>&1 || true

{
  echo "[$(date)] Starting daily pipeline"
  echo "[$(date)] Repo: ${REPO_ROOT}"
  echo "[$(date)] Settings: min_score=${MIN_SCORE} score_limit=${SCORE_LIMIT} tailor_limit=${TAILOR_LIMIT} cover_limit=${COVER_LIMIT} target_submissions=${TARGET_SUBMISSIONS} apply_batch=${APPLY_BATCH} apply_min_score=${APPLY_MIN_SCORE} validation=${VALIDATION_MODE} max_cycles=${MAX_CYCLES} workers=${WORKERS}"

  python3 "${REPO_ROOT}/scripts/daily_concurrent.py"

  echo "[$(date)] Final status snapshot"
  python3 -m applypilot.cli status || true
} 2>&1 | tee -a "${LOG_FILE}"

python3 "${REPO_ROOT}/scripts/log_session_event.py" \
  control_action \
  --mode daily_batch \
  --pid $$ \
  --log-path "${LOG_FILE}" \
  --message "run_daily.sh completed" \
  --field action=run_daily_completed >/dev/null 2>&1 || true

exit 0

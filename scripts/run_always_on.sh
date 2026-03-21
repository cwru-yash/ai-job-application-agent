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
STARTUP_DELAY_SECONDS="${APPLYPILOT_ALWAYS_ON_STARTUP_DELAY_SECONDS:-300}"
WAKE_GRACE_SECONDS="${APPLYPILOT_ALWAYS_ON_WAKE_GRACE_SECONDS:-300}"
WAKE_POLL_SECONDS="${APPLYPILOT_ALWAYS_ON_WAKE_POLL_SECONDS:-15}"

mkdir -p "${LOG_DIR}"

iteration=0
last_handled_wake_epoch=""

parse_mac_epoch() {
  local stamp="${1:-}"
  if [[ -z "${stamp}" ]]; then
    return 1
  fi
  /bin/date -j -f "%Y-%m-%d %H:%M:%S" "${stamp}" "+%s" 2>/dev/null
}

last_user_wake_epoch() {
  local line stamp
  line="$(pmset -g log | grep -E 'Wake[[:space:]]+(Wake from|DarkWake to FullWake)' | tail -n 1 || true)"
  if [[ -z "${line}" ]]; then
    return 1
  fi
  stamp="$(echo "${line}" | cut -c1-19)"
  parse_mac_epoch "${stamp}"
}

wait_for_recent_wake() {
  local reason="${1:-wake}"
  local wake_epoch now remaining
  wake_epoch="$(last_user_wake_epoch || true)"
  if [[ -z "${wake_epoch}" ]]; then
    return 0
  fi
  if [[ -n "${last_handled_wake_epoch}" ]] && (( wake_epoch <= last_handled_wake_epoch )); then
    return 0
  fi

  now="$(date +%s)"
  remaining=$((WAKE_GRACE_SECONDS - (now - wake_epoch)))
  if (( remaining > 0 )); then
    echo "[$(date)] Detected recent wake (${reason}); waiting ${remaining}s before continuing"
    sleep "${remaining}"
  fi
  last_handled_wake_epoch="${wake_epoch}"
}

{
  echo "[$(date)] Starting always-on supervisor"
  echo "[$(date)] Repo: ${REPO_ROOT}"
  echo "[$(date)] Session pause seconds: ${SESSION_PAUSE_SECONDS}"
  echo "[$(date)] Error pause seconds: ${ERROR_PAUSE_SECONDS}"
  echo "[$(date)] Startup delay seconds: ${STARTUP_DELAY_SECONDS}"
  echo "[$(date)] Wake grace seconds: ${WAKE_GRACE_SECONDS}"
  echo "[$(date)] Wake poll seconds: ${WAKE_POLL_SECONDS}"

  echo "[$(date)] Initial startup delay ${STARTUP_DELAY_SECONDS}s"
  sleep "${STARTUP_DELAY_SECONDS}"
  wait_for_recent_wake "startup"

  while true; do
    iteration=$((iteration + 1))
    echo
    echo "[$(date)] Supervisor iteration ${iteration}: starting concurrent session"

    set +e
    python3 "${REPO_ROOT}/scripts/daily_concurrent.py" &
    child_pid=$!
    set -e

    session_start_epoch="$(date +%s)"
    exit_code=""
    woke_during_session=0

    while kill -0 "${child_pid}" >/dev/null 2>&1; do
      wake_epoch="$(last_user_wake_epoch || true)"
      if [[ -n "${wake_epoch}" ]] && (( wake_epoch > session_start_epoch )); then
        if [[ -z "${last_handled_wake_epoch}" ]] || (( wake_epoch > last_handled_wake_epoch )); then
          echo "[$(date)] Wake detected during active session; stopping child ${child_pid} and entering grace period"
          kill "${child_pid}" >/dev/null 2>&1 || true
          set +e
          wait "${child_pid}"
          exit_code=$?
          set -e
          woke_during_session=1
          last_handled_wake_epoch="${wake_epoch}"
          break
        fi
      fi
      sleep "${WAKE_POLL_SECONDS}"
    done

    if [[ -z "${exit_code}" ]]; then
      set +e
      wait "${child_pid}"
      exit_code=$?
      set -e
    fi

    echo "[$(date)] Supervisor iteration ${iteration}: session exit code ${exit_code}"

    if (( woke_during_session )); then
      wait_for_recent_wake "post-wake resume"
      continue
    fi

    wait_for_recent_wake "between sessions"

    if [[ "${exit_code}" -eq 0 ]]; then
      echo "[$(date)] Sleeping ${SESSION_PAUSE_SECONDS}s before next session"
      sleep "${SESSION_PAUSE_SECONDS}"
    else
      echo "[$(date)] Sleeping ${ERROR_PAUSE_SECONDS}s after failure"
      sleep "${ERROR_PAUSE_SECONDS}"
    fi
  done
} 2>&1 | tee -a "${LOG_FILE}"

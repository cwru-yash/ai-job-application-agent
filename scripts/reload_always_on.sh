#!/bin/zsh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.ai-job-application-agent.always-on"
DAILY_LABEL="com.ai-job-application-agent.daily"
PLIST_DIR="${HOME}/Library/LaunchAgents"
ALWAYS_ON_PLIST="${PLIST_DIR}/${LABEL}.plist"
DAILY_PLIST="${PLIST_DIR}/${DAILY_LABEL}.plist"
LOG_DIR="${HOME}/.applypilot/logs"
PID_FILE="${HOME}/.applypilot/run_always_on.pid"
SUPERVISOR_LOG="${LOG_DIR}/always_on.supervisor.out.log"

cd "${REPO_ROOT}"

launchctl bootout "gui/$(id -u)" "${ALWAYS_ON_PLIST}" >/dev/null 2>&1 || true
launchctl bootout "gui/$(id -u)" "${DAILY_PLIST}" >/dev/null 2>&1 || true

pkill -f "run_daily.sh" >/dev/null 2>&1 || true
pkill -f "daily_concurrent.py" >/dev/null 2>&1 || true
pkill -f "run_always_on.sh" >/dev/null 2>&1 || true
pkill -f "python -m applypilot.cli apply" >/dev/null 2>&1 || true
pkill -f "local_apply_agent.py" >/dev/null 2>&1 || true

mkdir -p "${LOG_DIR}"

(
  ./scripts/run_always_on.sh >> "${SUPERVISOR_LOG}" 2>&1 &
  echo $! > "${PID_FILE}"
)
sleep 1
pid="$(cat "${PID_FILE}")"

echo
echo "Started always-on supervisor"
echo "PID: ${pid}"
echo "PID file: ${PID_FILE}"
echo "Supervisor log: ${SUPERVISOR_LOG}"

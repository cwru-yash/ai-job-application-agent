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
RUNNER_DIR="${HOME}/.applypilot/bin"
MANUAL_RUNNER="${RUNNER_DIR}/run_always_on_manual.sh"

cd "${REPO_ROOT}"

launchctl bootout "gui/$(id -u)" "${ALWAYS_ON_PLIST}" >/dev/null 2>&1 || true
launchctl bootout "gui/$(id -u)" "${DAILY_PLIST}" >/dev/null 2>&1 || true

pkill -f "run_daily.sh" >/dev/null 2>&1 || true
pkill -f "daily_concurrent.py" >/dev/null 2>&1 || true
pkill -f "run_always_on.sh" >/dev/null 2>&1 || true
pkill -f "python -m applypilot.cli apply" >/dev/null 2>&1 || true
pkill -f "local_apply_agent.py" >/dev/null 2>&1 || true

mkdir -p "${LOG_DIR}"
mkdir -p "${RUNNER_DIR}"

log_event_async() {
  python3 "${REPO_ROOT}/scripts/log_session_event.py" "$@" >/dev/null 2>&1 &!
}

log_event_async \
  control_action \
  --mode always_on \
  --pid $$ \
  --log-path "${SUPERVISOR_LOG}" \
  --message "reload always-on requested" \
  --field action=reload_always_on

cat > "${MANUAL_RUNNER}" <<EOF
#!/bin/zsh
set -euo pipefail
cd '${REPO_ROOT}'
./scripts/run_always_on.sh >> '${SUPERVISOR_LOG}' 2>&1
EOF
chmod +x "${MANUAL_RUNNER}"

if [[ "$(uname -s)" == "Darwin" ]] && command -v osascript >/dev/null 2>&1; then
  osascript \
    -e 'tell application "Terminal" to activate' \
    -e "tell application \"Terminal\" to do script \"/bin/zsh '${MANUAL_RUNNER}'\""
else
  nohup "${MANUAL_RUNNER}" >/dev/null 2>&1 < /dev/null &
fi

for _ in {1..20}; do
  if [[ -f "${PID_FILE}" ]]; then
    pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" >/dev/null 2>&1; then
      break
    fi
  fi
  sleep 1
done

pid="$(cat "${PID_FILE}" 2>/dev/null || true)"

log_event_async \
  control_action \
  --mode always_on \
  --pid $$ \
  --log-path "${SUPERVISOR_LOG}" \
  --message "always-on supervisor reloaded" \
  --field action=reload_always_on_started \
  --field child_pid="${pid}"

echo
echo "Started always-on supervisor"
echo "PID: ${pid}"
echo "PID file: ${PID_FILE}"
echo "Supervisor log: ${SUPERVISOR_LOG}"

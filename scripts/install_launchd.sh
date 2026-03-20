#!/bin/zsh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.ai-job-application-agent.daily"
SCHEDULE="${1:-09:00}"
HOUR="${SCHEDULE%:*}"
MINUTE="${SCHEDULE#*:}"
PLIST_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${PLIST_DIR}/${LABEL}.plist"
LOG_DIR="${HOME}/.applypilot/logs"
RUNNER_DIR="${HOME}/.applypilot/bin"
RUNNER_PATH="${RUNNER_DIR}/run_daily_launchd.sh"

if [[ ! "${HOUR}" =~ ^[0-9]+$ ]] || [[ ! "${MINUTE}" =~ ^[0-9]+$ ]]; then
  echo "Expected time in HH:MM format, got: ${SCHEDULE}" >&2
  exit 1
fi

mkdir -p "${PLIST_DIR}" "${LOG_DIR}" "${RUNNER_DIR}"

cat > "${RUNNER_PATH}" <<EOF
#!/bin/zsh
set -euo pipefail

cd '${REPO_ROOT}'
./scripts/run_daily.sh >>'${LOG_DIR}/launchd.out.log' 2>>'${LOG_DIR}/launchd.err.log'
EOF

chmod +x "${RUNNER_PATH}"

cat > "${PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/osascript</string>
    <string>-e</string>
    <string>tell application "Terminal" to activate</string>
    <string>-e</string>
    <string>tell application "Terminal" to do script "/bin/zsh '${RUNNER_PATH}'"</string>
  </array>

  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>${HOUR}</integer>
    <key>Minute</key>
    <integer>${MINUTE}</integer>
  </dict>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>

  <key>StandardOutPath</key>
  <string>${LOG_DIR}/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/launchd.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "${PLIST_PATH}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "${PLIST_PATH}"

echo "Installed ${LABEL} at ${PLIST_PATH}"
echo "Scheduled for ${HOUR}:${MINUTE}"
echo "Wrapper: ${RUNNER_PATH}"
echo
echo "Run immediately:"
echo "  launchctl kickstart -k gui/$(id -u)/${LABEL}"
echo
echo "Check status:"
echo "  launchctl print gui/$(id -u)/${LABEL}"
echo
echo "Remove schedule:"
echo "  launchctl bootout gui/$(id -u) ${PLIST_PATH}"

#!/bin/zsh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.ai-job-application-agent.daily"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"
DEFAULT_SCHEDULE="12:00"
RUN_NOW=0

if [[ "${1:-}" == "--run-now" ]]; then
  RUN_NOW=1
elif [[ -n "${1:-}" ]]; then
  echo "Usage: ./scripts/reload_daily.sh [--run-now]" >&2
  exit 1
fi

detect_schedule() {
  if [[ ! -f "${PLIST_PATH}" ]]; then
    echo "${DEFAULT_SCHEDULE}"
    return
  fi

  local hour minute
  hour="$(/usr/libexec/PlistBuddy -c 'Print :StartCalendarInterval:Hour' "${PLIST_PATH}" 2>/dev/null || true)"
  minute="$(/usr/libexec/PlistBuddy -c 'Print :StartCalendarInterval:Minute' "${PLIST_PATH}" 2>/dev/null || true)"

  if [[ "${hour}" =~ ^[0-9]+$ ]] && [[ "${minute}" =~ ^[0-9]+$ ]]; then
    printf '%02d:%02d\n' "${hour}" "${minute}"
    return
  fi

  echo "${DEFAULT_SCHEDULE}"
}

SCHEDULE="$(detect_schedule)"

cd "${REPO_ROOT}"
./scripts/install_launchd.sh "${SCHEDULE}"

echo
echo "Reloaded daily schedule at ${SCHEDULE}"

if [[ "${RUN_NOW}" -eq 1 ]]; then
  launchctl kickstart -k "gui/$(id -u)/${LABEL}"
  echo "Started a fresh run immediately."
fi

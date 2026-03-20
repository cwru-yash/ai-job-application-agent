#!/bin/zsh
set -euo pipefail

LABELS=(
  "com.ai-job-application-agent.daily"
  "com.ai-job-application-agent.always-on"
)
PATTERN='run_always_on.sh|daily_concurrent.py|run_daily.sh|applypilot\.cli apply|local_apply_agent'

matches="$(ps -axo pid,etime,command | grep -E "${PATTERN}" | grep -v grep || true)"

if [[ -n "${matches}" ]]; then
  echo "RUNNING"
  echo
  echo "${matches}"
  exit 0
fi

echo "NOT RUNNING"
echo
for label in "${LABELS[@]}"; do
  launchctl print "gui/$(id -u)/${label}" 2>/dev/null | sed -n '1,35p' || true
  echo
done

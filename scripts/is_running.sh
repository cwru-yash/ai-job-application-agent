#!/bin/zsh
set -euo pipefail

LABEL="com.ai-job-application-agent.daily"
PATTERN='daily_concurrent.py|run_daily.sh|applypilot\.cli apply|local_apply_agent'

matches="$(ps -axo pid,etime,command | grep -E "${PATTERN}" | grep -v grep || true)"

if [[ -n "${matches}" ]]; then
  echo "RUNNING"
  echo
  echo "${matches}"
  exit 0
fi

echo "NOT RUNNING"
echo
launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null | sed -n '1,35p' || true

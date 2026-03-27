#!/bin/zsh
set -euo pipefail

cat <<'EOF'
# ApplyPilot OpenClaw Commands

- `/skill job-agent-help`
  - show this help
- `/skill job-agent-status`
  - show runtime, pipeline summary, ready queue, recent apply activity, failures, and config
- `/skill job-agent-activity`
  - show daily pipeline activity and recent session history
- `/skill job-agent-jobs ready`
  - show the ready-to-apply queue
- `/skill job-agent-jobs recent`
  - show recent applied/failed jobs
- `/skill job-agent-jobs failures`
  - show failure breakdowns
- `/skill job-agent-control reload-always-on`
  - restart the always-on supervisor
- `/skill job-agent-control stop-always-on`
  - stop the always-on supervisor
- `/skill job-agent-control open-dashboard`
  - open the HTML dashboard on this Mac
- `/skill job-agent-control menu`
  - launch the numbered terminal control menu
EOF

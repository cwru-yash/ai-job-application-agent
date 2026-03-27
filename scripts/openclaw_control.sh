#!/bin/zsh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ACTION="${1:-status}"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

cd "${REPO_ROOT}"
case "${ACTION}" in
  status)
    printf '## Runtime Control\n\n```text\n'
    ./scripts/is_running.sh
    printf '```\n'
    ;;
  reload-always-on)
    printf '## Runtime Control\n\n```text\n'
    ./scripts/reload_always_on.sh
    printf '```\n'
    ;;
  stop-always-on)
    printf '## Runtime Control\n\n```text\n'
    ./scripts/stop_always_on.sh
    printf '```\n'
    ;;
  open-dashboard)
    python3 -m applypilot.cli dashboard >/dev/null
    printf '## Runtime Control\n\nOpened the ApplyPilot HTML dashboard in the default browser.\n'
    ;;
  menu)
    osascript -e "tell application \"Terminal\" to activate" \
      -e "tell application \"Terminal\" to do script \"cd '${REPO_ROOT}' && ./scripts/job_agent_menu.sh\"" >/dev/null
    printf '## Runtime Control\n\nOpened the numbered ApplyPilot menu in Terminal.\n'
    ;;
  run-daily)
    printf '## Runtime Control\n\n```text\n'
    ./scripts/run_daily.sh
    printf '```\n'
    ;;
  *)
    echo "Unknown control action: ${ACTION}" >&2
    echo "Use: status | reload-always-on | stop-always-on | open-dashboard | menu | run-daily" >&2
    exit 1
    ;;
esac

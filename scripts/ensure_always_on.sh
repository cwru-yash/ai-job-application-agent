#!/bin/zsh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if pgrep -f "run_always_on.sh" >/dev/null 2>&1; then
  echo "Always-on supervisor already running."
  exit 0
fi

cd "${REPO_ROOT}"
exec ./scripts/reload_always_on.sh

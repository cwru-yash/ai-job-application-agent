#!/bin/zsh
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <job-url>" >&2
  exit 1
fi

JOB_URL="$1"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

cd "${REPO_ROOT}"
exec python3 -m applypilot.cli apply \
  --url "${JOB_URL}" \
  --limit 1 \
  --workers 1 \
  --headless \
  --agent-backend command \
  --dry-run

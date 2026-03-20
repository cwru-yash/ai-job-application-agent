#!/bin/zsh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cd "${REPO_ROOT}"
exec "${REPO_ROOT}/scripts/run_daily.sh"

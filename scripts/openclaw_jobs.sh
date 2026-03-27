#!/bin/zsh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-ready}"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

cd "${REPO_ROOT}"
case "${MODE}" in
  ready)
    exec python3 -m applypilot.cli report --section ready --format markdown --limit 20
    ;;
  recent)
    exec python3 -m applypilot.cli report --section recent --format markdown --limit 20
    ;;
  failures)
    exec python3 -m applypilot.cli report --section failures --format markdown --limit 20
    ;;
  overview)
    exec python3 -m applypilot.cli report --section overview --format markdown --limit 20
    ;;
  *)
    echo "Unknown jobs mode: ${MODE}" >&2
    echo "Use: ready | recent | failures | overview" >&2
    exit 1
    ;;
esac

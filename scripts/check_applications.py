#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from applypilot.config import ensure_dirs, load_env
from applypilot.database import init_db
from applypilot.reporting import build_report, render_report


def main() -> int:
    load_env()
    ensure_dirs()
    init_db()
    sections = []
    for name in ("overview", "activity", "ready", "recent", "failures"):
        payload = build_report(section=name, days=14, limit=20)
        sections.append(render_report(payload, section=name, output_format="table"))
    print("\n\n".join(sections))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

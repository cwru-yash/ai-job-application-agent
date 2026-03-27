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
from applypilot.reporting import runtime_status


def main() -> int:
    load_env()
    ensure_dirs()
    init_db()
    runtime = runtime_status()
    if runtime["running"]:
        print("RUNNING")
        print()
        for proc in runtime["processes"]:
            print(f"{proc['pid']}\t{proc['elapsed']}\t{proc['command']}")
        return 0

    print("NOT RUNNING")
    print()
    pid_file = runtime["pid_file"]
    print(f"mode: {runtime['mode']}")
    print(f"pid_file: {pid_file['path']}")
    print(f"pid_file_exists: {pid_file['exists']}")
    print(f"pid_file_live: {pid_file['live']}")
    print(f"always_on_plist_exists: {runtime['launchd']['always_on_plist_exists']}")
    print(f"daily_plist_exists: {runtime['launchd']['daily_plist_exists']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

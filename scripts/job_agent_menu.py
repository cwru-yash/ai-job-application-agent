#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from applypilot.config import ensure_dirs, load_env
from applypilot.database import init_db
from applypilot.reporting import build_report, render_report, runtime_status


def run_cmd(args: list[str]) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(SRC_DIR)
    subprocess.run(args, cwd=REPO_ROOT, env=env, check=False)


def show_section(section: str) -> None:
    report = build_report(section=section, days=14, limit=20)
    print(render_report(report, section=section, output_format="table"))


def open_dashboard() -> None:
    run_cmd([sys.executable, "-m", "applypilot.cli", "dashboard"])


def tail_logs() -> None:
    runtime = runtime_status()
    log_path = runtime["logs"].get("latest_always_on_log") or runtime["logs"].get("latest_daily_log") or runtime["logs"].get("supervisor_log")
    if not log_path:
        print("No log file found.")
        return
    print(f"Tailing {log_path}. Press Ctrl+C to return to the menu.\n")
    try:
        subprocess.run(["tail", "-f", log_path], check=False)
    except KeyboardInterrupt:
        pass


def prompt() -> str:
    print("\nApplyPilot Control Menu")
    print("----------------------")
    print("1. Overview")
    print("2. Runtime status")
    print("3. Daily activity")
    print("4. Source breakdown")
    print("5. Ready queue")
    print("6. Recent applied/failed jobs")
    print("7. Failure breakdown")
    print("8. Open HTML dashboard")
    print("9. Tail live logs")
    print("10. Start or reload always-on")
    print("11. Stop always-on")
    print("12. Refresh")
    print("13. Quit")
    return input("\nChoose an option: ").strip()


def wait_for_enter() -> None:
    input("\nPress Enter to continue...")


def main() -> int:
    load_env()
    ensure_dirs()
    init_db()

    while True:
        choice = prompt()
        print()
        if choice == "1":
            show_section("overview")
        elif choice == "2":
            show_section("runtime")
        elif choice == "3":
            show_section("activity")
            print()
            show_section("history")
        elif choice == "4":
            show_section("sources")
        elif choice == "5":
            show_section("ready")
        elif choice == "6":
            show_section("recent")
        elif choice == "7":
            show_section("failures")
        elif choice == "8":
            open_dashboard()
        elif choice == "9":
            tail_logs()
        elif choice == "10":
            run_cmd(["./scripts/reload_always_on.sh"])
        elif choice == "11":
            run_cmd(["./scripts/stop_always_on.sh"])
        elif choice == "12":
            continue
        elif choice == "13":
            return 0
        else:
            print("Unknown option.")
        if choice not in {"8", "9", "12", "13"}:
            wait_for_enter()


if __name__ == "__main__":
    raise SystemExit(main())

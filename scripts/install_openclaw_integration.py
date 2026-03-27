#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OPENCLAW_DIR = Path.home() / ".openclaw"
CONFIG_PATH = OPENCLAW_DIR / "openclaw.json"
SKILL_DIR = REPO_ROOT / "openclaw_skills"


def main() -> int:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"OpenClaw config not found: {CONFIG_PATH}")

    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = OPENCLAW_DIR / f"openclaw.json.bak.{timestamp}"
    backup_path.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    skills = data.setdefault("skills", {})
    load = skills.setdefault("load", {})
    extra_dirs = load.setdefault("extraDirs", [])
    skill_dir_str = str(SKILL_DIR)
    if skill_dir_str not in extra_dirs:
        extra_dirs.append(skill_dir_str)

    CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    print(f"Updated {CONFIG_PATH}")
    print(f"Backup: {backup_path}")
    print(f"Registered extra skill dir: {skill_dir_str}")
    print("Start a new OpenClaw session or restart the dashboard to load the new skills.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

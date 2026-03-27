#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from applypilot.config import ensure_dirs, load_env
from applypilot.events import record_event


def parse_field(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("Fields must use key=value")
    key, value = raw.split("=", 1)
    return key.strip(), value.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Append a session event to the ApplyPilot ledger.")
    parser.add_argument("event_type")
    parser.add_argument("--mode")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--session-id")
    parser.add_argument("--log-path")
    parser.add_argument("--message")
    parser.add_argument("--field", action="append", type=parse_field, default=[])
    args = parser.parse_args()

    load_env()
    ensure_dirs()
    extra = dict(args.field)
    record_event(
        args.event_type,
        mode=args.mode,
        pid=args.pid,
        session_id=args.session_id,
        log_path=args.log_path,
        message=args.message,
        extra=extra,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

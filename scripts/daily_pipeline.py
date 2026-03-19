#!/usr/bin/env python3
"""Budgeted daily pipeline runner.

Runs discovery/enrichment plus bounded scoring/tailoring/cover generation so the
daily workflow reaches the apply stage instead of spending the whole day on
backlog scoring.
"""

from __future__ import annotations

import os
from typing import Callable


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def run_stage(label: str, fn: Callable[[], object]) -> object:
    print(f"\n=== {label} ===", flush=True)
    result = fn()
    print(result, flush=True)
    return result


def main() -> int:
    from applypilot.config import ensure_dirs, load_env
    from applypilot.database import get_stats, init_db
    from applypilot.discovery.jobspy import run_discovery
    from applypilot.discovery.workday import run_workday_discovery
    from applypilot.discovery.smartextract import run_smart_extract
    from applypilot.enrichment.detail import run_enrichment
    from applypilot.scoring.cover_letter import run_cover_letters
    from applypilot.scoring.pdf import batch_convert
    from applypilot.scoring.scorer import run_scoring
    from applypilot.scoring.tailor import run_tailoring

    load_env()
    ensure_dirs()
    init_db()

    discover_enabled = env_flag("APPLYPILOT_DAILY_DISCOVER", True)
    enrich_enabled = env_flag("APPLYPILOT_DAILY_ENRICH", True)
    score_limit = env_int("APPLYPILOT_DAILY_SCORE_LIMIT", 90)
    tailor_limit = env_int("APPLYPILOT_DAILY_TAILOR_LIMIT", 35)
    cover_limit = env_int("APPLYPILOT_DAILY_COVER_LIMIT", 35)
    discover_workers = env_int("APPLYPILOT_DAILY_DISCOVER_WORKERS", 1)
    enrich_workers = env_int("APPLYPILOT_DAILY_ENRICH_WORKERS", 1)
    min_score = env_int("APPLYPILOT_DAILY_MIN_SCORE", 8)
    validation_mode = os.environ.get("APPLYPILOT_DAILY_VALIDATION", "lenient")

    print("Daily pipeline settings:", flush=True)
    print(
        {
            "discover_enabled": discover_enabled,
            "enrich_enabled": enrich_enabled,
            "score_limit": score_limit,
            "tailor_limit": tailor_limit,
            "cover_limit": cover_limit,
            "discover_workers": discover_workers,
            "enrich_workers": enrich_workers,
            "min_score": min_score,
            "validation_mode": validation_mode,
        },
        flush=True,
    )

    if discover_enabled:
        run_stage("discover: jobspy", run_discovery)
        run_stage("discover: workday", lambda: run_workday_discovery(workers=discover_workers))
        run_stage("discover: smartextract", lambda: run_smart_extract(workers=discover_workers))
    else:
        print("\n=== discover skipped ===", flush=True)

    if enrich_enabled:
        run_stage("enrich", lambda: run_enrichment(workers=enrich_workers))
    else:
        print("\n=== enrich skipped ===", flush=True)

    run_stage("score", lambda: run_scoring(limit=score_limit))
    run_stage(
        "tailor",
        lambda: run_tailoring(min_score=min_score, limit=tailor_limit, validation_mode=validation_mode),
    )
    run_stage(
        "cover",
        lambda: run_cover_letters(min_score=min_score, limit=cover_limit, validation_mode=validation_mode),
    )
    run_stage("pdf", batch_convert)

    print("\nFinal stats:", flush=True)
    print(get_stats(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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


def load_daily_pipeline_settings() -> dict[str, object]:
    discover_enabled = env_flag("APPLYPILOT_DAILY_DISCOVER", True)
    return {
        "discover_enabled": discover_enabled,
        "greenhouse_discover_enabled": env_flag("APPLYPILOT_DAILY_GREENHOUSE_DISCOVER", discover_enabled),
        "enrich_enabled": env_flag("APPLYPILOT_DAILY_ENRICH", True),
        "score_limit": env_int("APPLYPILOT_DAILY_SCORE_LIMIT", 90),
        "tailor_limit": env_int("APPLYPILOT_DAILY_TAILOR_LIMIT", 35),
        "cover_limit": env_int("APPLYPILOT_DAILY_COVER_LIMIT", 35),
        "discover_workers": env_int("APPLYPILOT_DAILY_DISCOVER_WORKERS", 1),
        "enrich_workers": env_int("APPLYPILOT_DAILY_ENRICH_WORKERS", 1),
        "min_score": env_int("APPLYPILOT_DAILY_MIN_SCORE", 8),
        "validation_mode": os.environ.get("APPLYPILOT_DAILY_VALIDATION", "lenient"),
    }


def run_daily_pipeline_once(
    settings: dict[str, object] | None = None,
    *,
    emit_settings: bool = True,
) -> dict:
    from applypilot.config import ensure_dirs, load_env
    from applypilot.database import get_stats, init_db
    from applypilot.discovery.jobspy import run_discovery
    from applypilot.discovery.greenhouse import run_greenhouse_discovery
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

    resolved = load_daily_pipeline_settings()
    if settings:
        resolved.update(settings)

    discover_enabled = bool(resolved["discover_enabled"])
    greenhouse_discover_enabled = bool(resolved.get("greenhouse_discover_enabled", discover_enabled))
    enrich_enabled = bool(resolved["enrich_enabled"])
    score_limit = int(resolved["score_limit"])
    tailor_limit = int(resolved["tailor_limit"])
    cover_limit = int(resolved["cover_limit"])
    discover_workers = int(resolved["discover_workers"])
    enrich_workers = int(resolved["enrich_workers"])
    min_score = int(resolved["min_score"])
    validation_mode = str(resolved["validation_mode"])

    if emit_settings:
        print("Daily pipeline settings:", flush=True)
        print(resolved, flush=True)

    if discover_enabled:
        run_stage("discover: jobspy", run_discovery)
        run_stage("discover: workday", lambda: run_workday_discovery(workers=discover_workers))
        run_stage("discover: smartextract", lambda: run_smart_extract(workers=discover_workers))
    else:
        print("\n=== discover skipped ===", flush=True)

    if greenhouse_discover_enabled:
        run_stage("discover: greenhouse", lambda: run_greenhouse_discovery(workers=discover_workers))
    else:
        print("\n=== discover: greenhouse skipped ===", flush=True)

    if enrich_enabled:
        run_stage("enrich", lambda: run_enrichment(workers=enrich_workers))
    else:
        print("\n=== enrich skipped ===", flush=True)

    if score_limit > 0:
        run_stage("score", lambda: run_scoring(limit=score_limit))
    else:
        print("\n=== score skipped ===", flush=True)

    if tailor_limit > 0:
        run_stage(
            "tailor",
            lambda: run_tailoring(min_score=min_score, limit=tailor_limit, validation_mode=validation_mode),
        )
    else:
        print("\n=== tailor skipped ===", flush=True)

    if cover_limit > 0:
        run_stage(
            "cover",
            lambda: run_cover_letters(min_score=min_score, limit=cover_limit, validation_mode=validation_mode),
        )
    else:
        print("\n=== cover skipped ===", flush=True)

    run_stage("pdf", batch_convert)

    print("\nFinal stats:", flush=True)
    stats = get_stats()
    print(stats, flush=True)
    return stats


def main() -> int:
    run_daily_pipeline_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Daily controller that targets actual submissions, not just apply attempts."""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from pathlib import Path

from daily_pipeline import env_flag, env_int, load_daily_pipeline_settings, run_daily_pipeline_once


REPO_ROOT = Path(__file__).resolve().parents[1]


def print_summary(label: str, stats: dict) -> None:
    print(
        f"{label}: "
        f"total={stats['total']} "
        f"scored={stats['scored']} "
        f"tailored={stats['tailored']} "
        f"covers={stats['with_cover_letter']} "
        f"ready={stats['ready_to_apply']} "
        f"applied={stats['applied']}",
        flush=True,
    )


def run_apply_batch(limit: int, workers: int, min_score: int, *, headless: bool) -> int:
    if limit <= 0:
        print("Apply batch size is 0, skipping auto-apply.", flush=True)
        return 0

    cmd = [
        sys.executable,
        "-m",
        "applypilot.cli",
        "apply",
        "--limit",
        str(limit),
        "--workers",
        str(workers),
        "--min-score",
        str(min_score),
        "--agent-backend",
        "command",
    ]
    if headless:
        cmd.append("--headless")

    print(f"\n=== apply batch (limit={limit}) ===", flush=True)
    completed = subprocess.run(cmd, cwd=REPO_ROOT, check=False)
    print(f"Apply exit code: {completed.returncode}", flush=True)
    return completed.returncode


def main() -> int:
    src_dir = REPO_ROOT / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    from applypilot.config import ensure_dirs, load_env
    from applypilot.database import get_stats, init_db

    load_env()
    ensure_dirs()
    init_db()

    base_settings = load_daily_pipeline_settings()
    target_submissions = env_int("APPLYPILOT_DAILY_TARGET_SUBMISSIONS", 25)
    max_cycles = max(1, env_int("APPLYPILOT_DAILY_MAX_CYCLES", 6))
    apply_batch = env_int(
        "APPLYPILOT_DAILY_APPLY_BATCH",
        env_int("APPLYPILOT_DAILY_APPLY_LIMIT", 5),
    )
    workers = env_int("APPLYPILOT_DAILY_WORKERS", 1)
    apply_min_score = env_int(
        "APPLYPILOT_DAILY_APPLY_MIN_SCORE",
        env_int("APPLYPILOT_DAILY_MIN_SCORE", 8),
    )
    headless = env_flag("APPLYPILOT_DAILY_HEADLESS", True)
    discover_each_cycle = env_flag("APPLYPILOT_DAILY_DISCOVER_EACH_CYCLE", False)
    sleep_seconds = max(0, env_int("APPLYPILOT_DAILY_SLEEP_SECONDS", 10))
    idle_break_limit = max(1, env_int("APPLYPILOT_DAILY_IDLE_BREAK_LIMIT", 2))

    remaining_score_budget = int(base_settings["score_limit"])
    remaining_tailor_budget = int(base_settings["tailor_limit"])
    remaining_cover_budget = int(base_settings["cover_limit"])

    initial_stats = get_stats()
    start_applied = initial_stats["applied"]
    print_summary("Starting stats", initial_stats)
    print(
        {
            "target_submissions": target_submissions,
            "max_cycles": max_cycles,
            "apply_batch": apply_batch,
            "apply_min_score": apply_min_score,
            "headless": headless,
            "discover_each_cycle": discover_each_cycle,
            "idle_break_limit": idle_break_limit,
        },
        flush=True,
    )

    idle_cycles = 0

    for cycle in range(1, max_cycles + 1):
        current_stats = get_stats()
        submitted_so_far = current_stats["applied"] - start_applied
        if submitted_so_far >= target_submissions:
            print(f"Target reached after cycle {cycle - 1}: submissions={submitted_so_far}", flush=True)
            break

        if (
            remaining_score_budget <= 0
            and remaining_tailor_budget <= 0
            and remaining_cover_budget <= 0
            and current_stats["ready_to_apply"] <= 0
        ):
            print("No remaining stage budget and nothing ready to apply; stopping.", flush=True)
            break

        cycles_left = max_cycles - cycle + 1
        cycle_settings = dict(base_settings)
        cycle_settings["discover_enabled"] = bool(base_settings["discover_enabled"]) and (
            cycle == 1 or discover_each_cycle
        )
        cycle_settings["enrich_enabled"] = bool(base_settings["enrich_enabled"]) and (
            cycle == 1 or discover_each_cycle
        )
        cycle_settings["score_limit"] = (
            math.ceil(remaining_score_budget / cycles_left) if remaining_score_budget > 0 else 0
        )
        cycle_settings["tailor_limit"] = (
            math.ceil(remaining_tailor_budget / cycles_left) if remaining_tailor_budget > 0 else 0
        )
        cycle_settings["cover_limit"] = (
            math.ceil(remaining_cover_budget / cycles_left) if remaining_cover_budget > 0 else 0
        )

        print(
            f"\n##### cycle {cycle}/{max_cycles} #####",
            flush=True,
        )
        print_summary("Before cycle", current_stats)
        print(f"Cycle settings: {cycle_settings}", flush=True)

        before_stats = current_stats
        run_daily_pipeline_once(settings=cycle_settings, emit_settings=(cycle == 1))
        pipeline_stats = get_stats()

        scored_delta = max(0, pipeline_stats["scored"] - before_stats["scored"])
        tailored_delta = max(0, pipeline_stats["tailored"] - before_stats["tailored"])
        cover_delta = max(0, pipeline_stats["with_cover_letter"] - before_stats["with_cover_letter"])

        remaining_score_budget = max(0, remaining_score_budget - scored_delta)
        remaining_tailor_budget = max(0, remaining_tailor_budget - tailored_delta)
        remaining_cover_budget = max(0, remaining_cover_budget - cover_delta)

        remaining_target = max(0, target_submissions - (pipeline_stats["applied"] - start_applied))
        ready_to_apply = pipeline_stats["ready_to_apply"]
        apply_exit_code = None

        if remaining_target > 0 and ready_to_apply > 0:
            apply_limit = min(apply_batch, ready_to_apply, remaining_target)
            apply_exit_code = run_apply_batch(
                apply_limit,
                workers,
                apply_min_score,
                headless=headless,
            )
        else:
            print(
                f"Skipping apply batch: remaining_target={remaining_target} ready_to_apply={ready_to_apply}",
                flush=True,
            )

        after_stats = get_stats()
        submitted_total = after_stats["applied"] - start_applied
        submitted_delta = after_stats["applied"] - before_stats["applied"]
        progress = any(
            delta > 0
            for delta in (
                scored_delta,
                tailored_delta,
                cover_delta,
                submitted_delta,
            )
        )

        print(
            {
                "cycle": cycle,
                "scored_delta": scored_delta,
                "tailored_delta": tailored_delta,
                "cover_delta": cover_delta,
                "submitted_delta": submitted_delta,
                "submitted_total": submitted_total,
                "apply_exit_code": apply_exit_code,
                "remaining_score_budget": remaining_score_budget,
                "remaining_tailor_budget": remaining_tailor_budget,
                "remaining_cover_budget": remaining_cover_budget,
            },
            flush=True,
        )
        print_summary("After cycle", after_stats)

        if progress:
            idle_cycles = 0
        else:
            idle_cycles += 1
            if idle_cycles >= idle_break_limit:
                print(
                    f"No meaningful progress for {idle_cycles} consecutive cycle(s); stopping.",
                    flush=True,
                )
                break

        if cycle < max_cycles and submitted_total < target_submissions and sleep_seconds > 0:
            print(f"Sleeping {sleep_seconds}s before next cycle...", flush=True)
            time.sleep(sleep_seconds)

    final_stats = get_stats()
    print_summary("Final stats", final_stats)
    print(
        {
            "submitted_today": final_stats["applied"] - start_applied,
            "target_submissions": target_submissions,
        },
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

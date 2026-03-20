#!/usr/bin/env python3
"""Concurrent daily controller.

Runs a preparation loop and an apply loop at the same time:
- prep loop: discover/enrich/score/tailor/cover/pdf in bounded cycles
- apply loop: continuously applies ready jobs in small batches
"""

from __future__ import annotations

import math
import os
import threading
import time
from pathlib import Path

from daily_orchestrator import print_summary, run_apply_batch
from daily_pipeline import env_flag, env_int, load_daily_pipeline_settings, run_daily_pipeline_once


REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    src_dir = REPO_ROOT / "src"
    import sys

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
    prep_sleep_seconds = max(0, env_int("APPLYPILOT_DAILY_SLEEP_SECONDS", 10))
    apply_poll_seconds = max(5, env_int("APPLYPILOT_DAILY_APPLY_POLL_SECONDS", 20))
    idle_break_limit = max(1, env_int("APPLYPILOT_DAILY_IDLE_BREAK_LIMIT", 2))

    remaining_score_budget = int(base_settings["score_limit"])
    remaining_tailor_budget = int(base_settings["tailor_limit"])
    remaining_cover_budget = int(base_settings["cover_limit"])

    initial_stats = get_stats()
    start_applied = initial_stats["applied"]

    print_summary("Starting stats", initial_stats)
    print(
        {
            "mode": "concurrent",
            "target_submissions": target_submissions,
            "max_cycles": max_cycles,
            "apply_batch": apply_batch,
            "apply_min_score": apply_min_score,
            "headless": headless,
            "discover_each_cycle": discover_each_cycle,
            "prep_sleep_seconds": prep_sleep_seconds,
            "apply_poll_seconds": apply_poll_seconds,
            "idle_break_limit": idle_break_limit,
        },
        flush=True,
    )

    stop_event = threading.Event()
    prep_done = threading.Event()
    prep_failed = threading.Event()

    def submitted_so_far() -> int:
        return get_stats()["applied"] - start_applied

    def prep_loop() -> None:
        nonlocal remaining_score_budget, remaining_tailor_budget, remaining_cover_budget

        idle_cycles = 0

        try:
            for cycle in range(1, max_cycles + 1):
                if stop_event.is_set():
                    break
                if submitted_so_far() >= target_submissions:
                    print(f"Prep loop stopping early: target reached before cycle {cycle}", flush=True)
                    break

                current_stats = get_stats()
                if (
                    remaining_score_budget <= 0
                    and remaining_tailor_budget <= 0
                    and remaining_cover_budget <= 0
                ):
                    print("Prep loop: remaining stage budget exhausted.", flush=True)
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

                print(f"\n##### prep cycle {cycle}/{max_cycles} #####", flush=True)
                print_summary("Prep before", current_stats)
                print(f"Prep cycle settings: {cycle_settings}", flush=True)

                before_stats = current_stats
                run_daily_pipeline_once(settings=cycle_settings, emit_settings=(cycle == 1))
                after_stats = get_stats()

                scored_delta = max(0, after_stats["scored"] - before_stats["scored"])
                tailored_delta = max(0, after_stats["tailored"] - before_stats["tailored"])
                cover_delta = max(0, after_stats["with_cover_letter"] - before_stats["with_cover_letter"])

                remaining_score_budget = max(0, remaining_score_budget - scored_delta)
                remaining_tailor_budget = max(0, remaining_tailor_budget - tailored_delta)
                remaining_cover_budget = max(0, remaining_cover_budget - cover_delta)

                progress = any(delta > 0 for delta in (scored_delta, tailored_delta, cover_delta))
                print(
                    {
                        "prep_cycle": cycle,
                        "scored_delta": scored_delta,
                        "tailored_delta": tailored_delta,
                        "cover_delta": cover_delta,
                        "remaining_score_budget": remaining_score_budget,
                        "remaining_tailor_budget": remaining_tailor_budget,
                        "remaining_cover_budget": remaining_cover_budget,
                    },
                    flush=True,
                )
                print_summary("Prep after", after_stats)

                if progress:
                    idle_cycles = 0
                else:
                    idle_cycles += 1
                    if idle_cycles >= idle_break_limit:
                        print(
                            f"Prep loop stopping after {idle_cycles} idle cycle(s).",
                            flush=True,
                        )
                        break

                if cycle < max_cycles and prep_sleep_seconds > 0 and not stop_event.is_set():
                    print(f"Prep loop sleeping {prep_sleep_seconds}s...", flush=True)
                    time.sleep(prep_sleep_seconds)
        except Exception:
            prep_failed.set()
            stop_event.set()
            raise
        finally:
            prep_done.set()

    def apply_loop() -> None:
        idle_polls = 0
        while not stop_event.is_set():
            stats = get_stats()
            submitted = stats["applied"] - start_applied
            if submitted >= target_submissions:
                print(f"Apply loop stopping: target reached with {submitted} submission(s).", flush=True)
                stop_event.set()
                break

            ready_to_apply = stats["ready_to_apply"]
            if ready_to_apply > 0:
                idle_polls = 0
                apply_limit = min(apply_batch, ready_to_apply, max(0, target_submissions - submitted))
                if apply_limit > 0:
                    run_apply_batch(
                        apply_limit,
                        workers,
                        apply_min_score,
                        headless=headless,
                    )
                continue

            idle_polls += 1
            if prep_done.is_set():
                if idle_polls >= idle_break_limit:
                    print(
                        f"Apply loop stopping after {idle_polls} idle poll(s) with prep complete.",
                        flush=True,
                    )
                    break
            time.sleep(apply_poll_seconds)

    prep_thread = threading.Thread(target=prep_loop, name="daily-prep")
    apply_thread = threading.Thread(target=apply_loop, name="daily-apply")

    prep_thread.start()
    apply_thread.start()

    prep_thread.join()
    if prep_failed.is_set():
        stop_event.set()

    apply_thread.join()

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

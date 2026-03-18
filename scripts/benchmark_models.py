#!/usr/bin/env python3
"""Benchmark local LLMs on ApplyPilot scoring and tailoring prompts.

This script does not mutate the ApplyPilot database. It loads one real job,
runs the exact scoring prompt and a single-pass tailoring prompt against one
or more models, and prints JSON results for easy comparison.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from applypilot.config import DB_PATH, RESUME_PATH, load_env, load_profile
import applypilot.llm as llm_module
from applypilot.scoring.scorer import score_job
from applypilot.scoring.tailor import tailor_resume


@contextmanager
def time_limit(seconds: int):
    """Raise TimeoutError if the wrapped block exceeds the limit."""

    if seconds <= 0:
        yield
        return

    def _handler(signum, frame):  # noqa: ARG001
        raise TimeoutError(f"timed out after {seconds}s")

    previous = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def reset_llm_client() -> None:
    """Close and reset the module singleton so env overrides take effect."""
    if hasattr(llm_module, "reset_clients"):
        llm_module.reset_clients()
        return
    client = getattr(llm_module, "_instance", None)
    if client is not None:
        try:
            client.close()
        except Exception:
            pass
    llm_module._instance = None


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def fetch_job(url: str | None, title: str | None) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if url:
        row = conn.execute(
            """
            SELECT url, title, site, location, full_description, fit_score
            FROM jobs
            WHERE url = ?
            LIMIT 1
            """,
            (url,),
        ).fetchone()
    elif title:
        row = conn.execute(
            """
            SELECT url, title, site, location, full_description, fit_score
            FROM jobs
            WHERE title = ? AND full_description IS NOT NULL
            ORDER BY discovered_at DESC
            LIMIT 1
            """,
            (title,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT url, title, site, location, full_description, fit_score
            FROM jobs
            WHERE full_description IS NOT NULL
              AND length(full_description) > 1000
            ORDER BY discovered_at DESC
            LIMIT 1
            """
        ).fetchone()

    if row is None:
        raise SystemExit("No benchmark job found.")

    return dict(row)


def benchmark_model(
    model: str,
    job: dict,
    resume_text: str,
    profile: dict,
    timeout_seconds: int,
) -> dict:
    os.environ["LLM_MODEL"] = model
    reset_llm_client()

    result: dict = {
        "model": model,
        "scoring": {},
        "tailoring": {},
    }

    try:
        progress(f"[{model}] scoring start")
        started = time.perf_counter()
        with time_limit(timeout_seconds):
            score = score_job(resume_text, job)
        result["scoring"] = {
            "ok": score.get("score", 0) > 0,
            "elapsed_s": round(time.perf_counter() - started, 2),
            "score": score.get("score"),
            "keywords": score.get("keywords", ""),
            "reasoning": score.get("reasoning", ""),
        }
        progress(f"[{model}] scoring done in {result['scoring']['elapsed_s']}s")
    except Exception as exc:
        result["scoring"] = {
            "ok": False,
            "error": str(exc),
        }
        progress(f"[{model}] scoring failed: {exc}")

    try:
        progress(f"[{model}] tailoring start")
        started = time.perf_counter()
        with time_limit(timeout_seconds):
            tailored_text, report = tailor_resume(
                resume_text,
                job,
                profile,
                max_retries=0,
                validation_mode="lenient",
            )
        result["tailoring"] = {
            "ok": report.get("status") == "approved",
            "elapsed_s": round(time.perf_counter() - started, 2),
            "status": report.get("status"),
            "attempts": report.get("attempts"),
            "validator_passed": (report.get("validator") or {}).get("passed"),
            "validator_errors": (report.get("validator") or {}).get("errors", []),
            "validator_warnings": (report.get("validator") or {}).get("warnings", []),
            "text_chars": len(tailored_text),
        }
        progress(f"[{model}] tailoring done in {result['tailoring']['elapsed_s']}s with {result['tailoring']['status']}")
    except Exception as exc:
        result["tailoring"] = {
            "ok": False,
            "error": str(exc),
        }
        progress(f"[{model}] tailoring failed: {exc}")
    finally:
        reset_llm_client()

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        dest="models",
        action="append",
        required=True,
        help="Model name to benchmark. Repeat for multiple models.",
    )
    parser.add_argument("--job-url", help="Benchmark a specific job URL.")
    parser.add_argument("--job-title", help="Benchmark the newest job with this title.")
    parser.add_argument(
        "--timeout-per-case",
        type=int,
        default=150,
        help="Maximum seconds for each scoring or tailoring case.",
    )
    args = parser.parse_args()

    load_env()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    profile = load_profile()
    job = fetch_job(args.job_url, args.job_title)

    results = {
        "job": {
            "url": job["url"],
            "title": job["title"],
            "site": job["site"],
            "location": job.get("location"),
            "description_chars": len(job.get("full_description") or ""),
        },
        "results": [
            benchmark_model(model, job, resume_text, profile, args.timeout_per_case)
            for model in args.models
        ],
    }

    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

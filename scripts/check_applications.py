#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
from pathlib import Path


DB_PATH = Path.home() / ".applypilot" / "applypilot.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def print_section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def fmt(value: object | None) -> str:
    if value is None:
        return "-"
    text = str(value).strip()
    return text if text else "-"


def print_rows(rows: list[sqlite3.Row], columns: list[tuple[str, str]]) -> None:
    if not rows:
        print("None")
        return

    widths = []
    for key, label in columns:
        max_width = len(label)
        for row in rows:
            max_width = max(max_width, len(fmt(row[key])))
        widths.append(max_width)

    header = "  ".join(label.ljust(width) for (_, label), width in zip(columns, widths))
    print(header)
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(fmt(row[key]).ljust(width) for (key, _), width in zip(columns, widths)))


def main() -> int:
    conn = connect()

    overview = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM jobs) AS total_jobs,
          (SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL) AS with_description,
          (SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL) AS scored,
          (SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL) AS tailored,
          (SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL) AS covers,
          (SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL) AS applied,
          (SELECT COUNT(*) FROM jobs WHERE apply_status = 'failed') AS failed,
          (
            SELECT COUNT(*)
            FROM jobs
            WHERE fit_score >= 7
              AND tailored_resume_path IS NOT NULL
              AND application_url IS NOT NULL
              AND applied_at IS NULL
              AND (apply_status IS NULL OR apply_status NOT IN ('applied', 'in_progress'))
          ) AS ready_queue
        """
    ).fetchone()

    print_section("Overview")
    for key in overview.keys():
        print(f"{key}: {overview[key]}")

    daily = conn.execute(
        """
        WITH events AS (
            SELECT substr(discovered_at, 1, 10) AS day, 1 AS discovered, 0 AS enriched, 0 AS scored, 0 AS tailored, 0 AS cover_letters, 0 AS applied
            FROM jobs WHERE discovered_at IS NOT NULL
            UNION ALL
            SELECT substr(detail_scraped_at, 1, 10), 0, 1, 0, 0, 0, 0
            FROM jobs WHERE detail_scraped_at IS NOT NULL
            UNION ALL
            SELECT substr(scored_at, 1, 10), 0, 0, 1, 0, 0, 0
            FROM jobs WHERE scored_at IS NOT NULL
            UNION ALL
            SELECT substr(tailored_at, 1, 10), 0, 0, 0, 1, 0, 0
            FROM jobs WHERE tailored_at IS NOT NULL
            UNION ALL
            SELECT substr(cover_letter_at, 1, 10), 0, 0, 0, 0, 1, 0
            FROM jobs WHERE cover_letter_at IS NOT NULL
            UNION ALL
            SELECT substr(applied_at, 1, 10), 0, 0, 0, 0, 0, 1
            FROM jobs WHERE applied_at IS NOT NULL
        )
        SELECT day, SUM(discovered) AS discovered, SUM(enriched) AS enriched, SUM(scored) AS scored,
               SUM(tailored) AS tailored, SUM(cover_letters) AS covers, SUM(applied) AS applied
        FROM events
        GROUP BY day
        ORDER BY day DESC
        LIMIT 10
        """
    ).fetchall()
    print_section("Daily Activity")
    print_rows(daily, [
        ("day", "day"),
        ("discovered", "discovered"),
        ("enriched", "enriched"),
        ("scored", "scored"),
        ("tailored", "tailored"),
        ("covers", "covers"),
        ("applied", "applied"),
    ])

    ready = conn.execute(
        """
        SELECT title, site, location, salary, fit_score, tailored_at
        FROM jobs
        WHERE fit_score >= 7
          AND tailored_resume_path IS NOT NULL
          AND application_url IS NOT NULL
          AND applied_at IS NULL
          AND (apply_status IS NULL OR apply_status NOT IN ('applied', 'in_progress'))
        ORDER BY fit_score DESC, tailored_at DESC
        LIMIT 10
        """
    ).fetchall()
    print_section("Ready To Apply")
    print_rows(ready, [
        ("title", "title"),
        ("site", "site"),
        ("location", "location"),
        ("salary", "salary"),
        ("fit_score", "score"),
        ("tailored_at", "tailored_at"),
    ])

    recent = conn.execute(
        """
        SELECT title, site, location, salary, fit_score, apply_status, apply_error,
               applied_at, last_attempted_at
        FROM jobs
        WHERE apply_status IS NOT NULL OR applied_at IS NOT NULL
        ORDER BY COALESCE(applied_at, last_attempted_at) DESC
        LIMIT 20
        """
    ).fetchall()
    print_section("Recent Apply Activity")
    print_rows(recent, [
        ("title", "title"),
        ("site", "site"),
        ("location", "location"),
        ("salary", "salary"),
        ("fit_score", "score"),
        ("apply_status", "status"),
        ("apply_error", "error"),
        ("applied_at", "applied_at"),
        ("last_attempted_at", "last_attempt"),
    ])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

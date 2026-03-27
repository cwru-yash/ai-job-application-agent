from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from applypilot.applyability import is_supported_autoapply_job
from applypilot.config import (
    APP_DIR,
    DB_PATH,
    ENV_PATH,
    LOG_DIR,
    PROFILE_PATH,
    SEARCH_CONFIG_PATH,
    SESSION_EVENT_DIR,
    get_apply_backend,
    get_apply_agent_command,
    get_chrome_path,
    load_env,
)
from applypilot.database import get_connection, get_stats

PROCESS_PATTERNS: tuple[tuple[str, str], ...] = (
    ("always_on_supervisor", "run_always_on.sh"),
    ("daily_supervisor", "run_daily.sh"),
    ("daily_controller", "daily_concurrent.py"),
    ("apply_cli", "applypilot.cli apply"),
    ("local_apply_agent", "local_apply_agent.py"),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt(value: object | None) -> str:
    if value is None:
        return "-"
    text = str(value).strip()
    return text if text else "-"


def _table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], *, markdown: bool = False) -> str:
    if not rows:
        return "None"

    if markdown:
        header = "| " + " | ".join(label for _, label in columns) + " |"
        sep = "| " + " | ".join("---" for _ in columns) + " |"
        body = []
        for row in rows:
            body.append("| " + " | ".join(_fmt(row.get(key)).replace("\n", " ") for key, _ in columns) + " |")
        return "\n".join([header, sep, *body])

    widths = []
    for key, label in columns:
        max_width = len(label)
        for row in rows:
            max_width = max(max_width, len(_fmt(row.get(key))))
        widths.append(max_width)

    header = "  ".join(label.ljust(width) for (_, label), width in zip(columns, widths))
    divider = "  ".join("-" * width for width in widths)
    body = []
    for row in rows:
        body.append("  ".join(_fmt(row.get(key)).ljust(width) for (key, _), width in zip(columns, widths)))
    return "\n".join([header, divider, *body])


def _kv_table(data: dict[str, Any], *, markdown: bool = False) -> str:
    rows = [{"metric": key, "value": value} for key, value in data.items()]
    return _table(rows, [("metric", "metric"), ("value", "value")], markdown=markdown)


def _ps_processes() -> list[dict[str, Any]]:
    try:
        output = subprocess.run(
            ["ps", "-axo", "pid=,etime=,command="],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    except Exception:
        return []

    results: list[dict[str, Any]] = []
    for line in output:
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 2)
        if len(parts) < 3:
            continue
        pid_text, etime, command = parts
        for kind, pattern in PROCESS_PATTERNS:
            if pattern in command:
                results.append(
                    {
                        "kind": kind,
                        "pid": int(pid_text),
                        "elapsed": etime,
                        "command": command,
                    }
                )
                break
    return results


def _latest_log(pattern: str) -> str | None:
    matches = sorted(LOG_DIR.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return str(matches[0]) if matches else None


def _pid_file_info() -> dict[str, Any]:
    pid_file = APP_DIR / "run_always_on.pid"
    info: dict[str, Any] = {"path": str(pid_file), "exists": pid_file.exists(), "pid": None, "live": False}
    if not pid_file.exists():
        return info
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        return info
    info["pid"] = pid
    try:
        os.kill(pid, 0)
        info["live"] = True
    except OSError:
        info["live"] = False
    return info


def _runtime_mode(processes: list[dict[str, Any]]) -> str:
    kinds = {row["kind"] for row in processes}
    if "always_on_supervisor" in kinds:
        return "always_on"
    if "daily_supervisor" in kinds or "daily_controller" in kinds:
        return "daily"
    if "apply_cli" in kinds or "local_apply_agent" in kinds:
        return "apply_only"
    return "idle"


def _safe_chrome_path() -> str | None:
    try:
        return get_chrome_path()
    except Exception:
        return None


def _config_snapshot() -> dict[str, Any]:
    load_env()
    return {
        "app_dir": str(APP_DIR),
        "db_path": str(DB_PATH),
        "env_path": str(ENV_PATH),
        "profile_path": str(PROFILE_PATH),
        "searches_path": str(SEARCH_CONFIG_PATH),
        "apply_backend": get_apply_backend(),
        "agent_command_configured": bool(get_apply_agent_command()),
        "llm_model": os.environ.get("LLM_MODEL") or None,
        "llm_url": os.environ.get("LLM_URL") or None,
        "chrome_path": _safe_chrome_path(),
        "daily_min_score": int(os.environ.get("APPLYPILOT_DAILY_MIN_SCORE", "8")),
        "daily_apply_min_score": int(os.environ.get("APPLYPILOT_DAILY_APPLY_MIN_SCORE", os.environ.get("APPLYPILOT_DAILY_MIN_SCORE", "8"))),
        "daily_score_limit": int(os.environ.get("APPLYPILOT_DAILY_SCORE_LIMIT", "90")),
        "daily_tailor_limit": int(os.environ.get("APPLYPILOT_DAILY_TAILOR_LIMIT", "35")),
        "daily_cover_limit": int(os.environ.get("APPLYPILOT_DAILY_COVER_LIMIT", "35")),
        "daily_target_submissions": int(os.environ.get("APPLYPILOT_DAILY_TARGET_SUBMISSIONS", "25")),
        "daily_apply_batch": int(os.environ.get("APPLYPILOT_DAILY_APPLY_BATCH", os.environ.get("APPLYPILOT_DAILY_APPLY_LIMIT", "5"))),
        "daily_workers": int(os.environ.get("APPLYPILOT_DAILY_WORKERS", "1")),
        "always_on_session_pause_seconds": int(os.environ.get("APPLYPILOT_ALWAYS_ON_SESSION_PAUSE_SECONDS", "60")),
        "always_on_error_pause_seconds": int(os.environ.get("APPLYPILOT_ALWAYS_ON_ERROR_PAUSE_SECONDS", "180")),
        "always_on_startup_delay_seconds": int(os.environ.get("APPLYPILOT_ALWAYS_ON_STARTUP_DELAY_SECONDS", "300")),
        "always_on_wake_grace_seconds": int(os.environ.get("APPLYPILOT_ALWAYS_ON_WAKE_GRACE_SECONDS", "300")),
    }


def _overview(conn: sqlite3.Connection, *, min_score: int) -> dict[str, Any]:
    stats = get_stats(conn)
    failure_reasons = _apply_failure_reasons(conn, limit=5)
    ready_rows = conn.execute(
        """
        SELECT title, site, location, salary, fit_score, application_url, url, link_check_status
        FROM jobs
        WHERE fit_score >= ?
          AND tailored_resume_path IS NOT NULL
          AND cover_letter_path IS NOT NULL
          AND application_url IS NOT NULL
          AND applied_at IS NULL
          AND (apply_status IS NULL OR apply_status NOT IN ('applied', 'in_progress'))
        """,
        (min_score,),
    ).fetchall()
    ready_queue = sum(1 for row in ready_rows if is_supported_autoapply_job(dict(row)))
    return {
        "jobs_sourced": stats["total"],
        "descriptions_extracted": stats["with_description"],
        "pending_enrichment": stats["pending_detail"],
        "enrichment_errors": stats["detail_errors"],
        "dead_links": stats["dead_links"],
        "scored": stats["scored"],
        "pending_scoring": stats["unscored"],
        "tailored_resumes": stats["tailored"],
        "pending_tailoring": stats["untailored_eligible"],
        "cover_letters": stats["with_cover_letter"],
        "ready_queue": ready_queue,
        "applied": stats["applied"],
        "failed_jobs_current": stats["failed_jobs_current"],
        "failed_job_reasons": failure_reasons,
        "apply_errors": stats["apply_errors"],
        "score_distribution": [{"score": score, "count": count} for score, count in stats["score_distribution"]],
    }


def _daily_activity(conn: sqlite3.Connection, *, days: int) -> list[dict[str, Any]]:
    rows = conn.execute(
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
        LIMIT ?
        """,
        (days,),
    ).fetchall()
    return [dict(row) for row in rows]


def _source_breakdown(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT site,
               COUNT(*) AS total,
               SUM(CASE WHEN fit_score >= 7 THEN 1 ELSE 0 END) AS high_fit,
               SUM(CASE WHEN fit_score BETWEEN 5 AND 6 THEN 1 ELSE 0 END) AS mid_fit,
               SUM(CASE WHEN fit_score < 5 AND fit_score IS NOT NULL THEN 1 ELSE 0 END) AS low_fit,
               SUM(CASE WHEN fit_score IS NULL THEN 1 ELSE 0 END) AS unscored,
               ROUND(AVG(fit_score), 1) AS avg_score
        FROM jobs
        GROUP BY site
        ORDER BY total DESC, high_fit DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _apply_failure_reasons(conn: sqlite3.Connection, *, limit: int = 5) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT COALESCE(apply_error, 'unknown') AS reason, COUNT(*) AS count
        FROM jobs
        WHERE apply_status = 'failed'
        GROUP BY COALESCE(apply_error, 'unknown')
        ORDER BY count DESC, reason ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _ready_queue(conn: sqlite3.Connection, *, limit: int, min_score: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT title, site, location, salary, fit_score, tailored_at, application_url, url, link_check_status
        FROM jobs
        WHERE fit_score >= ?
          AND tailored_resume_path IS NOT NULL
          AND cover_letter_path IS NOT NULL
          AND application_url IS NOT NULL
          AND applied_at IS NULL
          AND (apply_status IS NULL OR apply_status NOT IN ('applied', 'in_progress'))
        ORDER BY fit_score DESC, tailored_at DESC
        LIMIT ?
        """,
        (min_score, limit),
    ).fetchall()
    return [dict(row) for row in rows if is_supported_autoapply_job(dict(row))]


def _recent_apply_activity(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT title, site, location, salary, fit_score, apply_status, apply_error,
               applied_at, last_attempted_at, application_url, url
        FROM jobs
        WHERE apply_status IS NOT NULL OR applied_at IS NOT NULL
        ORDER BY COALESCE(applied_at, last_attempted_at) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _failure_breakdown(conn: sqlite3.Connection, *, limit: int) -> dict[str, Any]:
    apply_rows = _apply_failure_reasons(conn, limit=limit)
    detail_rows = conn.execute(
        """
        SELECT COALESCE(detail_error, 'unknown') AS error, COUNT(*) AS count
        FROM jobs
        WHERE detail_error IS NOT NULL
        GROUP BY COALESCE(detail_error, 'unknown')
        ORDER BY count DESC, error ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return {
        "apply_errors": apply_rows,
        "detail_errors": [dict(row) for row in detail_rows],
    }


def _session_history(*, days: int, limit: int) -> list[dict[str, Any]]:
    if not SESSION_EVENT_DIR.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows: list[dict[str, Any]] = []
    for path in sorted(SESSION_EVENT_DIR.glob("*.jsonl"), reverse=True):
        try:
            day = datetime.fromisoformat(path.stem).replace(tzinfo=timezone.utc)
        except ValueError:
            day = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if day < cutoff:
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            continue
    rows.sort(key=lambda row: row.get("timestamp", ""), reverse=True)
    return rows[:limit]


def runtime_status() -> dict[str, Any]:
    load_env()
    processes = _ps_processes()
    return {
        "running": bool(processes),
        "mode": _runtime_mode(processes),
        "processes": processes,
        "pid_file": _pid_file_info(),
        "logs": {
            "latest_always_on_log": _latest_log("always_on_*.log"),
            "latest_daily_log": _latest_log("daily_run_*.log"),
            "supervisor_log": str(LOG_DIR / "always_on.supervisor.out.log"),
            "launchd_out_log": str(LOG_DIR / "always_on.launchd.out.log"),
            "launchd_err_log": str(LOG_DIR / "always_on.launchd.err.log"),
        },
        "launchd": {
            "always_on_plist": str(Path.home() / "Library" / "LaunchAgents" / "com.ai-job-application-agent.always-on.plist"),
            "daily_plist": str(Path.home() / "Library" / "LaunchAgents" / "com.ai-job-application-agent.daily.plist"),
            "always_on_plist_exists": (Path.home() / "Library" / "LaunchAgents" / "com.ai-job-application-agent.always-on.plist").exists(),
            "daily_plist_exists": (Path.home() / "Library" / "LaunchAgents" / "com.ai-job-application-agent.daily.plist").exists(),
        },
    }


def build_report(*, section: str = "all", days: int = 14, limit: int = 20) -> dict[str, Any]:
    load_env()
    conn = get_connection()
    config = _config_snapshot()
    generated_at = _now_iso()
    if section != "all":
        section_map = {
            "runtime": lambda: runtime_status(),
            "overview": lambda: _overview(conn, min_score=config["daily_apply_min_score"]),
            "activity": lambda: _daily_activity(conn, days=days),
            "sources": lambda: _source_breakdown(conn),
            "ready": lambda: _ready_queue(conn, limit=limit, min_score=config["daily_apply_min_score"]),
            "recent": lambda: _recent_apply_activity(conn, limit=limit),
            "failures": lambda: _failure_breakdown(conn, limit=limit),
            "config": lambda: config,
            "history": lambda: _session_history(days=days, limit=limit),
        }
        key_map = {
            "activity": "daily_activity",
            "sources": "source_breakdown",
            "ready": "ready_queue",
            "recent": "recent_apply_activity",
            "failures": "failure_breakdown",
            "history": "session_history",
        }
        value = section_map[section]()
        return {
            "generated_at": generated_at,
            key_map.get(section, section): value,
        }

    report = {
        "generated_at": generated_at,
        "runtime": runtime_status(),
        "overview": _overview(conn, min_score=config["daily_apply_min_score"]),
        "daily_activity": _daily_activity(conn, days=days),
        "source_breakdown": _source_breakdown(conn),
        "ready_queue": _ready_queue(conn, limit=limit, min_score=config["daily_apply_min_score"]),
        "recent_apply_activity": _recent_apply_activity(conn, limit=limit),
        "failure_breakdown": _failure_breakdown(conn, limit=limit),
        "config": config,
        "session_history": _session_history(days=days, limit=limit),
    }
    if section == "all":
        return report
    return report


def _render_runtime(runtime: dict[str, Any], *, markdown: bool = False) -> str:
    lines = []
    summary = {
        "running": runtime["running"],
        "mode": runtime["mode"],
        "pid_file": runtime["pid_file"].get("path"),
        "pid": runtime["pid_file"].get("pid"),
        "pid_live": runtime["pid_file"].get("live"),
        "latest_always_on_log": runtime["logs"].get("latest_always_on_log"),
        "latest_daily_log": runtime["logs"].get("latest_daily_log"),
        "supervisor_log": runtime["logs"].get("supervisor_log"),
    }
    lines.append(_kv_table(summary, markdown=markdown))
    if runtime["processes"]:
        lines.append(_table(runtime["processes"], [("kind", "kind"), ("pid", "pid"), ("elapsed", "elapsed"), ("command", "command")], markdown=markdown))
    else:
        lines.append("None")
    return "\n\n".join(lines)


def _render_section(title: str, body: str, *, markdown: bool = False) -> str:
    if markdown:
        return f"## {title}\n\n{body}"
    return f"{title}\n{'-' * len(title)}\n{body}"


def render_report(report: dict[str, Any], *, section: str, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, ensure_ascii=True)

    markdown = output_format == "markdown"
    payload = report if section == "all" else report
    sections: list[str] = []

    def add(name: str, body: str) -> None:
        sections.append(_render_section(name, body, markdown=markdown))

    if section in ("all", "runtime"):
        runtime = payload["runtime"] if section == "all" else payload["runtime"]
        add("Runtime", _render_runtime(runtime, markdown=markdown))

    if section in ("all", "overview"):
        overview = payload["overview"] if section == "all" else payload["overview"]
        overview_labels = [
            ("jobs_sourced", "Jobs sourced"),
            ("descriptions_extracted", "Descriptions extracted"),
            ("pending_enrichment", "Pending enrichment"),
            ("enrichment_errors", "Enrichment errors"),
            ("dead_links", "Dead links skipped"),
            ("scored", "Scored by LLM"),
            ("pending_scoring", "Pending scoring"),
            ("tailored_resumes", "Tailored resumes"),
            ("pending_tailoring", "Pending tailoring"),
            ("cover_letters", "Cover letters"),
            ("ready_queue", "Ready to apply"),
            ("applied", "Applied"),
            ("failed_jobs_current", "Currently failed jobs"),
            ("apply_errors", "Rows with failure notes"),
        ]
        summary_rows = [
            {"metric": label, "value": overview.get(key)}
            for key, label in overview_labels
            if key in overview
        ]
        body = _table(summary_rows, [("metric", "metric"), ("value", "value")], markdown=markdown)
        failure_rows = overview.get("failed_job_reasons", [])
        if failure_rows:
            body += "\n\nTop current failure reasons\n\n" + _table(
                failure_rows,
                [("reason", "failure_reason"), ("count", "jobs")],
                markdown=markdown,
            )
        score_rows = overview.get("score_distribution", [])
        if score_rows:
            body += "\n\n" + _table(score_rows, [("score", "score"), ("count", "count")], markdown=markdown)
        add("Overview", body)

    if section in ("all", "activity"):
        rows = payload["daily_activity"] if section == "all" else payload["daily_activity"]
        add("Daily Activity", _table(rows, [("day", "day"), ("discovered", "discovered"), ("enriched", "enriched"), ("scored", "scored"), ("tailored", "tailored"), ("covers", "covers"), ("applied", "applied")], markdown=markdown))

    if section in ("all", "sources"):
        rows = payload["source_breakdown"] if section == "all" else payload["source_breakdown"]
        add("Source Breakdown", _table(rows, [("site", "site"), ("total", "total"), ("high_fit", "high_fit"), ("mid_fit", "mid_fit"), ("low_fit", "low_fit"), ("unscored", "unscored"), ("avg_score", "avg_score")], markdown=markdown))

    if section in ("all", "ready"):
        rows = payload["ready_queue"] if section == "all" else payload["ready_queue"]
        add("Ready Queue", _table(rows, [("title", "title"), ("site", "site"), ("location", "location"), ("salary", "salary"), ("fit_score", "score"), ("tailored_at", "tailored_at")], markdown=markdown))

    if section in ("all", "recent"):
        rows = payload["recent_apply_activity"] if section == "all" else payload["recent_apply_activity"]
        add("Recent Apply Activity", _table(rows, [("title", "title"), ("site", "site"), ("location", "location"), ("salary", "salary"), ("fit_score", "score"), ("apply_status", "status"), ("apply_error", "failure_reason"), ("applied_at", "applied_at"), ("last_attempted_at", "last_attempt")], markdown=markdown))

    if section in ("all", "failures"):
        data = payload["failure_breakdown"] if section == "all" else payload["failure_breakdown"]
        body = "Currently failed jobs by reason\n\n" + _table(
            data["apply_errors"],
            [("reason", "failure_reason"), ("count", "jobs")],
            markdown=markdown,
        )
        body += "\n\nDetail / enrichment failures\n\n" + _table(
            data["detail_errors"],
            [("error", "error"), ("count", "count")],
            markdown=markdown,
        )
        add("Failure Breakdown", body)

    if section in ("all", "config"):
        data = payload["config"] if section == "all" else payload["config"]
        add("Config", _kv_table(data, markdown=markdown))

    if section in ("all", "history"):
        rows = payload["session_history"] if section == "all" else payload["session_history"]
        add("Session History", _table(rows, [("timestamp", "timestamp"), ("event_type", "event"), ("mode", "mode"), ("pid", "pid"), ("message", "message")], markdown=markdown))

    return "\n\n".join(sections)

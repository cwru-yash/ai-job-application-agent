from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot.config import DB_PATH, LOG_DIR, SESSION_EVENT_DIR, load_env

VALID_EVENT_TYPES = {
    "supervisor_started",
    "startup_delay",
    "wake_detected",
    "session_started",
    "session_finished",
    "session_failed",
    "control_action",
}


_ENV_KEYS = {
    "apply_backend": "APPLYPILOT_APPLY_BACKEND",
    "agent_command": "APPLYPILOT_AGENT_COMMAND",
    "llm_model": "LLM_MODEL",
    "llm_url": "LLM_URL",
    "daily_min_score": "APPLYPILOT_DAILY_MIN_SCORE",
    "daily_apply_min_score": "APPLYPILOT_DAILY_APPLY_MIN_SCORE",
    "daily_score_limit": "APPLYPILOT_DAILY_SCORE_LIMIT",
    "daily_tailor_limit": "APPLYPILOT_DAILY_TAILOR_LIMIT",
    "daily_cover_limit": "APPLYPILOT_DAILY_COVER_LIMIT",
    "daily_target_submissions": "APPLYPILOT_DAILY_TARGET_SUBMISSIONS",
    "daily_apply_batch": "APPLYPILOT_DAILY_APPLY_BATCH",
    "daily_workers": "APPLYPILOT_DAILY_WORKERS",
    "always_on_session_pause": "APPLYPILOT_ALWAYS_ON_SESSION_PAUSE_SECONDS",
    "always_on_error_pause": "APPLYPILOT_ALWAYS_ON_ERROR_PAUSE_SECONDS",
    "always_on_startup_delay": "APPLYPILOT_ALWAYS_ON_STARTUP_DELAY_SECONDS",
    "always_on_wake_grace": "APPLYPILOT_ALWAYS_ON_WAKE_GRACE_SECONDS",
}


def _safe_stats() -> dict[str, Any]:
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=1)
        conn.execute("PRAGMA busy_timeout=500")
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN full_description IS NOT NULL THEN 1 ELSE 0 END) AS with_description,
              SUM(CASE WHEN link_check_status = 'dead' THEN 1 ELSE 0 END) AS dead_links,
              SUM(CASE WHEN fit_score IS NOT NULL THEN 1 ELSE 0 END) AS scored,
              SUM(CASE WHEN tailored_resume_path IS NOT NULL THEN 1 ELSE 0 END) AS tailored,
              SUM(CASE WHEN cover_letter_path IS NOT NULL THEN 1 ELSE 0 END) AS with_cover_letter,
              SUM(
                CASE
                  WHEN tailored_resume_path IS NOT NULL
                   AND cover_letter_path IS NOT NULL
                   AND applied_at IS NULL
                   AND application_url IS NOT NULL
                  THEN 1 ELSE 0
                END
              ) AS ready_to_apply,
              SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END) AS applied,
              SUM(CASE WHEN apply_error IS NOT NULL THEN 1 ELSE 0 END) AS apply_errors
            FROM jobs
            """
        ).fetchone()
        conn.close()
        if row is None:
            return {}
        keys = (
            "total",
            "with_description",
            "dead_links",
            "scored",
            "tailored",
            "with_cover_letter",
            "ready_to_apply",
            "applied",
            "apply_errors",
        )
        return {key: row[idx] for idx, key in enumerate(keys)}
    except Exception as exc:  # pragma: no cover - observability should not crash the app
        return {"error": str(exc)}


def _config_snapshot() -> dict[str, Any]:
    load_env()
    snapshot: dict[str, Any] = {}
    for key, env_name in _ENV_KEYS.items():
        value = os.environ.get(env_name)
        if value:
            if key == "agent_command":
                snapshot[key] = "configured"
            else:
                snapshot[key] = value
    return snapshot


def _coerce(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return value


def record_event(
    event_type: str,
    *,
    mode: str | None = None,
    pid: int | None = None,
    session_id: str | None = None,
    log_path: str | Path | None = None,
    message: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Append an event to the session-event ledger."""
    if event_type not in VALID_EVENT_TYPES:
        raise ValueError(f"Unsupported event type: {event_type}")

    load_env()
    SESSION_EVENT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    out_path = SESSION_EVENT_DIR / f"{now.date().isoformat()}.jsonl"
    payload: dict[str, Any] = {
        "timestamp": now.isoformat(),
        "event_type": event_type,
        "mode": mode,
        "pid": pid,
        "session_id": session_id,
        "log_path": _coerce(log_path) if log_path else None,
        "message": message,
        "logs": {
            "log_dir": str(LOG_DIR),
        },
        "config": _config_snapshot(),
        "stats": _safe_stats(),
    }
    if extra:
        payload.update({key: _coerce(value) for key, value in extra.items()})

    with out_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")

    return out_path

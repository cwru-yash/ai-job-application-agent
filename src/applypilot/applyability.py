from __future__ import annotations

import os
from urllib.parse import urlparse
from typing import Iterable

from applypilot.config import (
    get_apply_agent_command,
    get_apply_backend,
    is_manual_ats,
    load_blocked_sites,
)

_DEFAULT_DEPRIORITIZED_SITE_HINTS = (
    "netflix",
    "adobe",
    "thomson reuters",
)
_DEFAULT_PREFERRED_SITE_HINTS = (
    "greenhouse.io",
    "bmo",
    "rbc",
    "cisco",
    "salesforce",
    "mastercard",
    "motorola solutions",
    "td bank",
    "moderna",
    "nvidia",
)


def _csv_env(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        values = default
    else:
        values = tuple(part.strip().lower() for part in raw.split(","))
    return tuple(value for value in values if value)


def supported_autoapply_url_patterns() -> tuple[str, ...]:
    configured = _csv_env("APPLYPILOT_SUPPORTED_AUTOAPPLY_PATTERNS")
    if configured:
        return configured

    backend = get_apply_backend()
    if backend == "command":
        command = (get_apply_agent_command() or "").lower()
        if "local_apply_agent.py" in command:
            return (
                "greenhouse.io",
                "job-boards.greenhouse.io",
                "boards.greenhouse.io",
                "myworkdayjobs.com",
            )
    return ()


def prep_preferred_site_hints() -> tuple[str, ...]:
    return _csv_env("APPLYPILOT_PREP_PREFERRED_SITES", _DEFAULT_PREFERRED_SITE_HINTS)


def prep_deprioritized_site_hints() -> tuple[str, ...]:
    return _csv_env("APPLYPILOT_PREP_DEPRIORITIZE_SITES", _DEFAULT_DEPRIORITIZED_SITE_HINTS)


def _job_text_haystacks(job: dict) -> tuple[str, ...]:
    apply_url = str(job.get("application_url") or job.get("url") or "").strip().lower()
    host = (urlparse(apply_url).hostname or "").lower()
    return (
        str(job.get("site") or "").strip().lower(),
        str(job.get("title") or "").strip().lower(),
        str(job.get("location") or "").strip().lower(),
        host,
        apply_url,
    )


def _job_matches_hints(job: dict, hints: tuple[str, ...]) -> bool:
    if not hints:
        return False
    haystacks = _job_text_haystacks(job)
    return any(hint in haystack for hint in hints for haystack in haystacks if haystack)


def _matches_supported_patterns(apply_url: str, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return True
    url_lower = apply_url.lower()
    return any(pattern in url_lower for pattern in patterns)


def prep_autoapply_only_enabled() -> bool:
    raw = os.environ.get("APPLYPILOT_PREP_AUTOAPPLY_ONLY")
    if raw is None:
        return False
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def is_supported_autoapply_job(job: dict) -> bool:
    site = str(job.get("site") or "").strip().lower()
    apply_url = str(job.get("application_url") or "").strip()
    if not apply_url:
        return False
    if str(job.get("link_check_status") or "").strip().lower() == "dead":
        return False
    if site == "linkedin":
        return False

    url_lower = apply_url.lower()
    if "linkedin.com/jobs/view" in url_lower:
        return False
    if is_manual_ats(apply_url):
        return False

    blocked_sites, blocked_patterns = load_blocked_sites()
    if site in {item.strip().lower() for item in blocked_sites}:
        return False
    for pattern in blocked_patterns:
        needle = str(pattern or "").lower().strip("%")
        if needle and needle in url_lower:
            return False
    supported_patterns = supported_autoapply_url_patterns()
    if supported_patterns and not _matches_supported_patterns(apply_url, supported_patterns):
        return False
    return True


def filter_jobs_for_autoapply(jobs: Iterable[dict]) -> tuple[list[dict], int]:
    filtered: list[dict] = []
    skipped = 0
    for job in jobs:
        if is_supported_autoapply_job(job):
            filtered.append(job)
        else:
            skipped += 1
    return filtered, skipped


def expanded_fetch_limit(limit: int) -> int:
    """Over-fetch before filtering so supported jobs are not starved by noisy rows."""
    if limit <= 0:
        return 0
    return max(limit * 100, limit + 200, 1000)


def _ats_priority_rank(apply_url: str) -> int:
    url_lower = apply_url.lower()
    if "greenhouse.io" in url_lower:
        return 0
    if "myworkdayjobs.com" in url_lower:
        return 1
    return 2


def autoapply_priority_key(job: dict) -> tuple[int, int, int, int]:
    supported_patterns = supported_autoapply_url_patterns()
    apply_url = str(job.get("application_url") or job.get("url") or "").strip().lower()
    support_rank = 0 if _matches_supported_patterns(apply_url, supported_patterns) else 1
    ats_rank = _ats_priority_rank(apply_url)
    deprioritized_rank = 1 if _job_matches_hints(job, prep_deprioritized_site_hints()) else 0
    preferred_rank = 0 if _job_matches_hints(job, prep_preferred_site_hints()) else 1
    attempts = int(job.get("apply_attempts") or 0)
    return (support_rank, ats_rank, attempts, deprioritized_rank, preferred_rank)


def sort_jobs_for_autoapply(jobs: Iterable[dict]) -> list[dict]:
    """Sort jobs so easier, supported targets are prepared and applied first.

    The sort is intentionally shallow and relies on Python's stable sorting:
    DB order still controls recency/score within each bucket.
    """
    return sorted(list(jobs), key=autoapply_priority_key)

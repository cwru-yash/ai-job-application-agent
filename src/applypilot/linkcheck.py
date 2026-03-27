"""Cheap URL health checks to prevent wasting browser/LLM work on dead jobs."""

from __future__ import annotations

import os
import re
from urllib import error, request


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
PERMANENT_FAILURES = {404, 410, 451}
UNCERTAIN_FAILURES = {401, 403, 405, 408, 409, 429, 500, 502, 503, 504, 999}
DEAD_PAGE_PATTERNS = (
    "page you are looking for doesn t exist",
    "page you are looking for doesn't exist",
    "page you are looking for does not exist",
    "job is no longer available",
    "this job is no longer available",
    "job posting is no longer available",
    "no longer accepting applications",
    "position is no longer available",
    "position has been filled",
    "job no longer exists",
    "this posting is no longer available",
)


def _timeout_seconds() -> float:
    raw = (os.environ.get("APPLYPILOT_LINKCHECK_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return 8.0
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 8.0


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def dead_page_reason(haystack: str) -> str | None:
    normalized = _normalize(haystack)
    for pattern in DEAD_PAGE_PATTERNS:
        if pattern in normalized:
            return pattern
    return None


def check_url(url: str) -> dict[str, str | int | None]:
    """Classify a job link as alive, dead, or uncertain.

    This intentionally uses a cheap urllib GET with a small body read. It is
    much cheaper than Playwright and good enough to catch obvious dead postings.
    """
    if not url:
        return {"status": "dead", "reason": "missing_url", "http_status": None, "final_url": None}

    req = request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )

    try:
        with request.urlopen(req, timeout=_timeout_seconds()) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            final_url = resp.geturl()
            content_type = resp.headers.get("Content-Type", "")
            body = ""
            if "html" in content_type.lower() or not content_type:
                body = resp.read(16384).decode("utf-8", "ignore")

            if status in PERMANENT_FAILURES:
                return {"status": "dead", "reason": f"http_{status}", "http_status": status, "final_url": final_url}

            pattern = dead_page_reason(f"{final_url}\n{body}")
            if pattern:
                return {"status": "dead", "reason": f"dead_page:{pattern}", "http_status": status, "final_url": final_url}

            if status in UNCERTAIN_FAILURES:
                return {"status": "uncertain", "reason": f"http_{status}", "http_status": status, "final_url": final_url}

            return {"status": "alive", "reason": "ok", "http_status": status, "final_url": final_url}
    except error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(16384).decode("utf-8", "ignore")
        except Exception:
            pass
        final_url = getattr(exc, "url", url)
        pattern = dead_page_reason(f"{final_url}\n{body}")
        if exc.code in PERMANENT_FAILURES or pattern:
            reason = f"http_{exc.code}"
            if pattern:
                reason = f"dead_page:{pattern}"
            return {"status": "dead", "reason": reason, "http_status": exc.code, "final_url": final_url}
        return {"status": "uncertain", "reason": f"http_{exc.code}", "http_status": exc.code, "final_url": final_url}
    except Exception as exc:
        return {"status": "uncertain", "reason": str(exc)[:120], "http_status": None, "final_url": url}

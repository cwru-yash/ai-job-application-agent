"""Direct Greenhouse board discovery.

Bootstraps from Greenhouse application URLs already seen in the database and
optionally from ~/.applypilot/greenhouse_boards.json. Uses Greenhouse's public
board API to source additional jobs directly from easier ATS flows.
"""

from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from applypilot.config import APP_DIR, load_search_config
from applypilot.database import get_connection, init_db
from applypilot.discovery.workday import strip_html

log = logging.getLogger(__name__)

GREENHOUSE_BOARDS_PATH = APP_DIR / "greenhouse_boards.json"
GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9+#]+", str(text or "").lower())


def _search_query_tokens() -> list[tuple[str, set[str]]]:
    cfg = load_search_config() or {}
    queries = cfg.get("queries", []) or []
    token_sets: list[tuple[str, set[str]]] = []
    for item in queries:
        query = item.get("query") if isinstance(item, dict) else str(item or "")
        query = str(query or "").strip().lower()
        if not query:
            continue
        tokens = {tok for tok in _tokenize(query) if len(tok) >= 2}
        if tokens:
            token_sets.append((query, tokens))
    return token_sets


def _matches_search_queries(title: str) -> bool:
    title_norm = str(title or "").strip().lower()
    if not title_norm:
        return False
    title_tokens = set(_tokenize(title_norm))
    for query, query_tokens in _search_query_tokens():
        if query in title_norm:
            return True
        if len(title_tokens & query_tokens) >= 2:
            return True
    return False


def _load_location_filter(search_cfg: dict | None = None) -> tuple[list[str], list[str]]:
    if search_cfg is None:
        search_cfg = load_search_config() or {}
    accept = search_cfg.get("location_accept", []) or []
    reject = search_cfg.get("location_reject_non_remote", []) or []
    return list(accept), list(reject)


def _location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    if not location:
        return True

    loc = location.lower()
    if any(r in loc for r in ("remote", "anywhere", "work from home", "wfh", "distributed")):
        return True

    for r in reject:
        if r.lower() in loc:
            return False
    for a in accept:
        if a.lower() in loc:
            return True
    return False


def _hours_old_limit(search_cfg: dict | None = None) -> int:
    if search_cfg is None:
        search_cfg = load_search_config() or {}
    defaults = search_cfg.get("defaults", {}) or {}
    try:
        return max(0, int(defaults.get("hours_old", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _infer_slug(url: str) -> str | None:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    path = parsed.path.strip("/")
    query = parse_qs(parsed.query)

    if host.startswith("job-boards.greenhouse.io") or host.startswith("job-boards.eu.greenhouse.io"):
        parts = path.split("/") if path else []
        if parts and parts[0] not in {"embed", "job_app"}:
            return parts[0]
        if "for" in query and query["for"]:
            return query["for"][0]
    if host.startswith("boards.greenhouse.io"):
        parts = path.split("/") if path else []
        if parts:
            return parts[0]
    return None


def _load_board_seeds() -> dict[str, str]:
    if not GREENHOUSE_BOARDS_PATH.exists():
        return {}
    try:
        data = json.loads(GREENHOUSE_BOARDS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    seeds: dict[str, str] = {}
    boards = data.get("boards", data if isinstance(data, list) else [])
    if isinstance(boards, list):
        for item in boards:
            if isinstance(item, str):
                seeds[item.strip()] = item.strip()
            elif isinstance(item, dict):
                slug = str(item.get("slug") or "").strip()
                name = str(item.get("name") or item.get("company_name") or slug).strip()
                if slug:
                    seeds[slug] = name or slug
    return seeds


def discover_board_slugs(conn: sqlite3.Connection | None = None) -> dict[str, str]:
    if conn is None:
        conn = get_connection()

    slugs = _load_board_seeds()
    rows = conn.execute(
        """
        SELECT DISTINCT COALESCE(application_url, url)
        FROM jobs
        WHERE lower(COALESCE(application_url, url)) LIKE '%greenhouse.io%'
        """
    ).fetchall()
    for (url,) in rows:
        slug = _infer_slug(str(url or ""))
        if slug and slug not in slugs:
            slugs[slug] = slug
    return slugs


def _fetch_board(slug: str) -> dict:
    req = urllib.request.Request(
        GREENHOUSE_API.format(slug=slug),
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _published_recent_enough(job: dict, hours_old: int) -> bool:
    if hours_old <= 0:
        return True
    published = str(job.get("first_published") or "").strip()
    if not published:
        return True
    try:
        published_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return True
    if published_dt.tzinfo is None:
        published_dt = published_dt.replace(tzinfo=timezone.utc)
    return published_dt >= datetime.now(timezone.utc) - timedelta(hours=hours_old)


def _job_record(job: dict, fallback_site: str) -> dict:
    full_description = strip_html(html.unescape(job.get("content") or ""))
    description = re.sub(r"\s+", " ", full_description)[:400] if full_description else None
    location = ((job.get("location") or {}).get("name") or "").strip() or None
    company_name = str(job.get("company_name") or "").strip() or fallback_site
    return {
        "url": str(job.get("absolute_url") or "").strip(),
        "title": str(job.get("title") or "").strip(),
        "salary": None,
        "description": description,
        "location": location,
        "site": company_name,
        "strategy": "greenhouse_api",
        "full_description": full_description or None,
        "application_url": str(job.get("absolute_url") or "").strip(),
    }


def _upsert_job(conn: sqlite3.Connection, record: dict) -> tuple[bool, bool]:
    for attempt in range(6):
        try:
            now = datetime.now(timezone.utc).isoformat()
            url = record["url"]
            existing = conn.execute(
                """
                SELECT url
                FROM jobs
                WHERE url = ? OR application_url = ?
                LIMIT 1
                """,
                (url, url),
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE jobs
                    SET title = COALESCE(?, title),
                        salary = COALESCE(?, salary),
                        description = COALESCE(?, description),
                        location = COALESCE(?, location),
                        site = COALESCE(?, site),
                        strategy = ?,
                        full_description = COALESCE(?, full_description),
                        application_url = COALESCE(application_url, ?),
                        detail_scraped_at = COALESCE(detail_scraped_at, ?)
                    WHERE url = ?
                    """,
                    (
                        record.get("title"),
                        record.get("salary"),
                        record.get("description"),
                        record.get("location"),
                        record.get("site"),
                        record.get("strategy"),
                        record.get("full_description"),
                        record.get("application_url"),
                        now,
                        existing["url"],
                    ),
                )
                return False, True

            conn.execute(
                """
                INSERT INTO jobs (
                    url, title, salary, description, location, site, strategy, discovered_at,
                    full_description, application_url, detail_scraped_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["url"],
                    record.get("title"),
                    record.get("salary"),
                    record.get("description"),
                    record.get("location"),
                    record.get("site"),
                    record.get("strategy"),
                    now,
                    record.get("full_description"),
                    record.get("application_url"),
                    now if record.get("full_description") else None,
                ),
            )
            return True, False
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 5:
                raise
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError("unreachable greenhouse upsert retry path")


def _run_one_board(slug: str, fallback_site: str, accept_locs: list[str], reject_locs: list[str], hours_old: int) -> dict:
    try:
        data = _fetch_board(slug)
    except Exception as exc:
        return {"slug": slug, "site": fallback_site, "new": 0, "existing": 0, "error": str(exc)}

    conn = get_connection()
    new = 0
    existing = 0
    matched = 0

    for job in data.get("jobs", []):
        title = str(job.get("title") or "").strip()
        location = ((job.get("location") or {}).get("name") or "").strip()
        if not _matches_search_queries(title):
            continue
        if not _location_ok(location, accept_locs, reject_locs):
            continue
        if not _published_recent_enough(job, hours_old):
            continue
        record = _job_record(job, fallback_site)
        if not record["url"]:
            continue
        is_new, is_existing = _upsert_job(conn, record)
        new += int(is_new)
        existing += int(is_existing)
        matched += 1

    conn.commit()
    return {"slug": slug, "site": fallback_site, "matched": matched, "new": new, "existing": existing, "error": None}


def run_greenhouse_discovery(workers: int = 1) -> dict:
    init_db()
    conn = get_connection()
    search_cfg = load_search_config() or {}
    accept_locs, reject_locs = _load_location_filter(search_cfg)
    hours_old = _hours_old_limit(search_cfg)
    boards = discover_board_slugs(conn)

    if not boards:
        return {"boards": 0, "new": 0, "existing": 0, "matched": 0, "errors": []}

    results: list[dict] = []
    max_workers = max(1, workers)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_run_one_board, slug, site, accept_locs, reject_locs, hours_old): slug
            for slug, site in boards.items()
        }
        for future in as_completed(futures):
            results.append(future.result())

    errors = [result for result in results if result.get("error")]
    return {
        "boards": len(boards),
        "matched": sum(int(result.get("matched", 0) or 0) for result in results),
        "new": sum(int(result.get("new", 0) or 0) for result in results),
        "existing": sum(int(result.get("existing", 0) or 0) for result in results),
        "errors": errors,
        "results": sorted(results, key=lambda item: str(item.get("slug") or "")),
    }

"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from applypilot.applyability import (
    expanded_fetch_limit,
    filter_jobs_for_autoapply,
    prep_autoapply_only_enabled,
    sort_jobs_for_autoapply,
)
from applypilot.config import RESUME_PATH, load_profile, load_search_config
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.linkcheck import check_url, dead_page_reason
from applypilot.llm import get_client

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight technical skills heavily (programming languages, frameworks, tools)
- Consider transferable experience (automation, scripting, API work)
- Factor in the candidate's project experience
- Be realistic about experience level vs. job requirements (years of experience, seniority)

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    reasoning = response

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client(os.environ.get("SCORING_LLM_MODEL"))
        response = client.chat(messages, max_tokens=512, temperature=0.2)
        parsed = _parse_score_response(response)
        if parsed["score"] == 0:
            log.warning(
                "Unparseable score response for job '%s'; keeping job pending for retry.",
                job.get("title", "?"),
            )
            parsed["reasoning"] = response
        return parsed
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}"}


def _needs_workday_browser_precheck(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return "myworkdayjobs.com" in host


def _workday_browser_precheck(url: str) -> dict[str, str] | None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            try:
                title = page.title()
            except Exception:
                title = ""
            try:
                body_text = page.locator("body").inner_text(timeout=3000)
            except Exception:
                body_text = ""
            final_url = page.url
            browser.close()
    except Exception as exc:
        log.debug("Workday browser precheck skipped for %s: %s", url, exc)
        return None

    reason = dead_page_reason(f"{final_url}\n{title}\n{body_text}")
    if not reason:
        return None
    return {"status": "dead", "reason": f"browser_dead:{reason}"}


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


def _collect_autoapply_prep_jobs(conn, limit: int) -> tuple[list[dict], int, int, int]:
    chunk_size = expanded_fetch_limit(limit)
    offset = 0
    scanned = 0
    skipped = 0
    relevant_skipped = 0
    selected: list[dict] = []
    seen_urls: set[str] = set()

    while True:
        batch = get_jobs_by_stage(conn=conn, stage="pending_score", limit=chunk_size, offset=offset)
        if not batch:
            break
        scanned += len(batch)
        offset += len(batch)

        supported, unsupported_count = filter_jobs_for_autoapply(batch)
        skipped += unsupported_count
        relevant = [job for job in supported if _matches_search_queries(job.get("title", ""))]
        relevant_skipped += max(0, len(supported) - len(relevant))
        for job in sort_jobs_for_autoapply(relevant):
            url = job.get("url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            selected.append(job)
            if limit > 0 and len(selected) >= limit:
                return selected, scanned, skipped, relevant_skipped

        if len(batch) < chunk_size:
            break

    return selected, scanned, skipped, relevant_skipped


def run_scoring(limit: int = 0, rescore: bool = False) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    prep_only = prep_autoapply_only_enabled()
    fetch_limit = expanded_fetch_limit(limit) if prep_only else limit

    if rescore:
        query = (
            "SELECT * FROM jobs WHERE full_description IS NOT NULL "
            "AND COALESCE(link_check_status, '') != 'dead'"
        )
        if fetch_limit > 0:
            query += f" LIMIT {fetch_limit}"
        jobs = conn.execute(query).fetchall()
    elif prep_only:
        jobs, scanned, skipped, relevant_skipped = _collect_autoapply_prep_jobs(conn, limit or fetch_limit or 20)
        if skipped:
            log.info("Auto-apply prep mode: skipped %d non-autoapplyable job(s) while scanning %d pending jobs.", skipped, scanned)
        if relevant_skipped:
            log.info("Auto-apply prep mode: skipped %d supported but non-target job(s) by title relevance.", relevant_skipped)
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=fetch_limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    if prep_only and not rescore:
        pass
    elif prep_only:
        jobs, skipped = filter_jobs_for_autoapply(jobs)
        if skipped:
            log.info("Auto-apply prep mode: skipped %d non-autoapplyable job(s) before scoring.", skipped)
        if not jobs:
            log.info("No auto-applyable jobs eligible for scoring.")
            return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}
        jobs = sort_jobs_for_autoapply(jobs)
        if limit > 0:
            jobs = jobs[:limit]

    checked_at = datetime.now(timezone.utc).isoformat()
    filtered_jobs: list[dict] = []
    for job in jobs:
        if job.get("link_check_status") == "dead":
            continue
        if job.get("link_checked_at"):
            filtered_jobs.append(job)
            continue

        target_url = job.get("application_url") or job.get("url") or ""
        link_result = check_url(target_url)
        if link_result.get("status") == "alive" and _needs_workday_browser_precheck(target_url):
            browser_result = _workday_browser_precheck(target_url)
            if browser_result:
                link_result = browser_result
        status = str(link_result.get("status") or "uncertain")
        reason = str(link_result.get("reason") or "unknown")
        conn.execute(
            "UPDATE jobs SET link_check_status = ?, link_checked_at = ?, link_check_error = ? WHERE url = ?",
            (status, checked_at, None if status == "alive" else reason, job["url"]),
        )
        if status == "dead":
            conn.execute(
                "UPDATE jobs SET detail_error = ? WHERE url = ?",
                (f"link_dead:{reason}", job["url"]),
            )
            continue
        filtered_jobs.append(job)
    conn.commit()
    jobs = filtered_jobs

    if not jobs:
        log.info("No scoreable jobs remain after link precheck.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    log.info("Scoring %d jobs sequentially...", len(jobs))
    t0 = time.time()
    completed = 0
    errors = 0
    results: list[dict] = []

    for job in jobs:
        result = score_job(resume_text, job)
        result["url"] = job["url"]
        completed += 1

        if result["score"] == 0:
            errors += 1

        results.append(result)

        log.info(
            "[%d/%d] score=%d  %s",
            completed, len(jobs), result["score"], job.get("title", "?")[:60],
        )

    # Write scores to DB
    now = datetime.now(timezone.utc).isoformat()
    for r in results:
        score_value = r["score"] if r["score"] > 0 else None
        scored_at = now if score_value is not None else None
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
            (score_value, f"{r['keywords']}\n{r['reasoning']}", scored_at, r["url"]),
        )
    conn.commit()

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed, len(results) / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }

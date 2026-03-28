#!/usr/bin/env python3
"""Local browser-driving apply agent for ApplyPilot's command backend."""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import html
import imaplib
import json
import os
import re
import secrets
import sys
import time
import traceback
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from email import header, policy
from email.parser import BytesParser
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


RESULT_RE = re.compile(r"RESULT:(?:APPLIED|EXPIRED|CAPTCHA|LOGIN_ISSUE|FAILED(?::[^\s]+)?)")
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OPENAI_URL = "http://127.0.0.1:8000/v1"
PROFILE_PATH = Path.home() / ".applypilot" / "profile.json"
WORKDAY_ACCOUNTS_PATH = Path.home() / ".applypilot" / "workday_accounts.json"
ACCOUNT_PASSWORDS_PATH = Path.home() / ".applypilot" / "account_passwords.json"
SHARED_WORKDAY_PASSWORD_PATH = Path.home() / ".applypilot" / "workday_shared_password.txt"
QUESTION_OVERRIDES_PATH = Path.home() / ".applypilot" / "question_overrides.json"
QUESTION_SUGGESTIONS_PATH = Path.home() / ".applypilot" / "question_override_suggestions.json"
WORKDAY_QUESTION_REVIEW_PATH = Path.home() / ".applypilot" / "workday_question_review.jsonl"
QUESTION_MEMORY_DIR = Path.home() / ".applypilot" / "question_memory"
RESUME_TEXT_PATH = Path.home() / ".applypilot" / "resume.txt"
RUNTIME_ERROR_DIR = Path.home() / ".applypilot" / "logs" / "runtime_errors"


@dataclass(slots=True)
class PromptContext:
    prompt: str
    job_title: str
    company: str
    job_url: str
    job_description: str
    resume_pdf: str
    cover_pdf: str
    resume_text: str
    cover_text: str
    dry_run: bool
    qualitative_cache: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(slots=True)
class MailLink:
    subject: str
    sender: str
    link: str


def log(msg: str) -> None:
    print(msg, flush=True)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def write_runtime_error_artifacts(page, host: str) -> list[str]:
    if page is None:
        return []
    try:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        host_slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", host or "unknown").strip("_") or "unknown"
        out_dir = RUNTIME_ERROR_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        base = out_dir / f"{stamp}_{host_slug}"
        html_path = base.with_suffix(".html")
        screenshot_path = base.with_suffix(".png")
        html_path.write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(screenshot_path), full_page=True)
        return [str(html_path), str(screenshot_path)]
    except Exception as exc:
        log(f"ACTION: runtime artifact capture failed error={type(exc).__name__}: {exc}")
        return []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=("ollama", "openai"),
        default="ollama",
        help="Backend API protocol to use for optional text generation.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Model endpoint base URL. Defaults to OLLAMA_HOST or localhost.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("APPLYPILOT_MODEL") or os.environ.get("LLM_MODEL") or "",
        help="Model name to request from the provider.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("APPLYPILOT_AGENT_API_KEY") or os.environ.get("OPENAI_API_KEY") or "",
        help="API key for OpenAI-compatible endpoints.",
    )
    parser.add_argument(
        "--prompt-file",
        type=Path,
        help="Read the ApplyPilot prompt from a file instead of stdin.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="LLM HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print extra action logs and model responses.",
    )
    return parser.parse_args()


def resolve_base_url(provider: str, explicit_url: str | None) -> str:
    if explicit_url:
        return explicit_url.rstrip("/")
    if provider == "ollama":
        return (
            os.environ.get("OLLAMA_HOST")
            or os.environ.get("APPLYPILOT_AGENT_BASE_URL")
            or DEFAULT_OLLAMA_URL
        ).rstrip("/")
    return (os.environ.get("APPLYPILOT_AGENT_BASE_URL") or DEFAULT_OPENAI_URL).rstrip("/")


def read_prompt(prompt_file: Path | None) -> str:
    if prompt_file:
        return prompt_file.read_text(encoding="utf-8")
    return sys.stdin.read()


def is_dry_run_prompt(prompt: str) -> bool:
    lower_prompt = prompt.lower()
    return (
        "do not click the final submit/apply button" in lower_prompt
        and "this was a dry run" in lower_prompt
    )


def extract_section(prompt: str, start_marker: str, end_marker: str) -> str:
    if start_marker not in prompt or end_marker not in prompt:
        return ""
    start = prompt.index(start_marker) + len(start_marker)
    end = prompt.index(end_marker, start)
    return prompt[start:end].strip()


def extract_line(prompt: str, prefix: str) -> str:
    match = re.search(rf"^{re.escape(prefix)}\s*(.+)$", prompt, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def parse_prompt(prompt: str) -> PromptContext:
    return PromptContext(
        prompt=prompt,
        job_title=extract_line(prompt, "Title:"),
        company=extract_line(prompt, "Company:"),
        job_url=extract_line(prompt, "URL:"),
        job_description=extract_section(
            prompt,
            "== JOB DESCRIPTION (use for qualitative answers) ==",
            "== FILES ==",
        ),
        resume_pdf=extract_line(prompt, "Resume PDF (upload this):"),
        cover_pdf=extract_line(prompt, "Cover Letter PDF (upload if asked):"),
        resume_text=extract_section(
            prompt,
            "== RESUME TEXT (use when filling text fields) ==",
            "== COVER LETTER TEXT (paste if text field, upload PDF if file field) ==",
        ),
        cover_text=extract_section(
            prompt,
            "== COVER LETTER TEXT (paste if text field, upload PDF if file field) ==",
            "== APPLICANT PROFILE ==",
        ),
        dry_run=is_dry_run_prompt(prompt),
    )


def load_profile() -> dict[str, Any]:
    if not PROFILE_PATH.exists():
        return {}
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def load_question_overrides() -> dict[str, Any]:
    if not QUESTION_OVERRIDES_PATH.exists():
        return {}
    try:
        return json.loads(QUESTION_OVERRIDES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_question_overrides(data: dict[str, Any]) -> None:
    QUESTION_OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUESTION_OVERRIDES_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def normalize_override_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def ats_family_for_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "myworkdayjobs.com" in host:
        return "workday"
    if "greenhouse.io" in host:
        return "greenhouse"
    return "generic"


def slugify_company_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_value).strip("_") or "unknown"


def company_memory_path(ctx: PromptContext) -> Path:
    ats = ats_family_for_url(ctx.job_url)
    company_slug = slugify_company_name(ctx.company or "unknown")
    return QUESTION_MEMORY_DIR / ats / f"{company_slug}.json"


def load_company_memory_entries(ctx: PromptContext) -> list[dict[str, Any]]:
    path = company_memory_path(ctx)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    questions = data.get("questions")
    if not isinstance(questions, list):
        return []
    return [item for item in questions if isinstance(item, dict)]


def save_company_memory(ctx: PromptContext, questions: list[dict[str, Any]], seen_questions: list[dict[str, Any]]) -> None:
    path = company_memory_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ats": ats_family_for_url(ctx.job_url),
        "company": ctx.company,
        "updated_at": now_iso(),
        "questions": questions,
        "seen_questions": seen_questions,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def context_override_entries(ctx: PromptContext) -> list[dict[str, Any]]:
    data = load_question_overrides()
    entries: list[dict[str, Any]] = []
    company_key = normalize_override_key(ctx.company)
    host_key = (urlparse(ctx.job_url).hostname or "").lower()

    for key, bucket in (data.get("hosts") or {}).items():
        if str(key).lower() == host_key and isinstance(bucket, list):
            entries.extend(item for item in bucket if isinstance(item, dict))

    for key, bucket in (data.get("companies") or {}).items():
        if normalize_override_key(str(key)) == company_key and isinstance(bucket, list):
            entries.extend(item for item in bucket if isinstance(item, dict))

    entries.extend(load_company_memory_entries(ctx))

    defaults = data.get("defaults")
    if isinstance(defaults, list):
        entries.extend(item for item in defaults if isinstance(item, dict))

    return entries


def override_entry_fragments(entry: dict[str, Any]) -> list[str]:
    raw = entry.get("match_any") or []
    if isinstance(raw, str):
        raw = [raw]
    return [normalize_override_key(str(item)) for item in raw if str(item).strip()]


def override_entry_answers(entry: dict[str, Any]) -> list[str]:
    raw = entry.get("answer")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [str(raw).strip()] if str(raw).strip() else []


def lookup_question_override(
    ctx: PromptContext,
    label: str,
    options: list[str] | None = None,
) -> str | None:
    label_key = normalize_override_key(label)
    if not label_key:
        return None
    for entry in context_override_entries(ctx):
        fragments = override_entry_fragments(entry)
        if not fragments or not any(fragment in label_key for fragment in fragments):
            continue
        answers = override_entry_answers(entry)
        if not answers:
            continue
        if options:
            for answer in answers:
                selected = select_best_option(options, answer)
                if selected:
                    return selected
        return answers[0]
    return None


def workday_override_answers(ctx: PromptContext) -> list[tuple[list[str], list[str]]]:
    overrides: list[tuple[list[str], list[str]]] = []
    for entry in context_override_entries(ctx):
        fragments = override_entry_fragments(entry)
        answers = override_entry_answers(entry)
        if fragments and answers:
            overrides.append((fragments, answers))
    return overrides


def workday_yes_no_override_answers(ctx: PromptContext) -> list[tuple[str, str]]:
    yes_no: list[tuple[str, str]] = []
    for fragments, answers in workday_override_answers(ctx):
        if not answers:
            continue
        answer = answers[0]
        if answer.lower() not in {"yes", "no", "si", "sí"}:
            continue
        for fragment in fragments:
            yes_no.append((fragment, answer))
    return yes_no


def company_override_bucket(data: dict[str, Any], company: str) -> list[dict[str, Any]]:
    companies = data.setdefault("companies", {})
    bucket = companies.get(company)
    if isinstance(bucket, list):
        return bucket
    bucket = []
    companies[company] = bucket
    return bucket


def merge_memory_question_entry(
    target: list[dict[str, Any]],
    fragments: list[str],
    answer: Any,
    ctx: PromptContext,
    *,
    metadata: dict[str, Any] | None = None,
) -> bool:
    cleaned_fragments = [normalize_override_key(item) for item in fragments if normalize_override_key(item)]
    if not cleaned_fragments or answer in (None, "", []):
        return False
    clean_metadata = {
        str(key): value
        for key, value in (metadata or {}).items()
        if value not in (None, "", [], {})
    }
    for existing in target:
        existing_fragments = [normalize_override_key(item) for item in existing.get("match_any", []) if item]
        if sorted(existing_fragments) == sorted(cleaned_fragments) and existing.get("answer") == answer:
            changed = False
            existing["last_seen_at"] = now_iso()
            changed = True
            titles = existing.setdefault("job_titles", [])
            if ctx.job_title and ctx.job_title not in titles:
                titles.append(ctx.job_title)
                changed = True
            for key, value in clean_metadata.items():
                if existing.get(key) != value:
                    existing[key] = value
                    changed = True
            return changed
    entry = {
        "match_any": cleaned_fragments,
        "answer": answer,
        "last_seen_at": now_iso(),
        "job_titles": [ctx.job_title] if ctx.job_title else [],
    }
    entry.update(clean_metadata)
    target.append(entry)
    return True


def merge_seen_question_entry(
    target: list[dict[str, Any]],
    question_text: str,
    ctx: PromptContext,
    *,
    suggested_answer: Any = None,
    source: str | None = None,
) -> bool:
    normalized = normalize_override_key(question_text)
    if not normalized:
        return False
    clean_suggested = suggested_answer
    if clean_suggested in ("", []):
        clean_suggested = None
    for existing in target:
        if normalize_override_key(existing.get("label", "")) == normalized:
            changed = False
            existing["last_seen_at"] = now_iso()
            changed = True
            titles = existing.setdefault("job_titles", [])
            if ctx.job_title and ctx.job_title not in titles:
                titles.append(ctx.job_title)
                changed = True
            if clean_suggested is not None and existing.get("suggested_answer") != clean_suggested:
                existing["suggested_answer"] = clean_suggested
                changed = True
            if source and existing.get("source") != source:
                existing["source"] = source
                changed = True
            return changed
    entry = {
        "label": normalized,
        "last_seen_at": now_iso(),
        "job_titles": [ctx.job_title] if ctx.job_title else [],
    }
    if clean_suggested is not None:
        entry["suggested_answer"] = clean_suggested
    if source:
        entry["source"] = source
    target.append(
        entry
    )
    return True


def persist_company_question_memory(
    ctx: PromptContext,
    learned: list[dict[str, Any]],
    seen_questions: list[str | dict[str, Any]],
) -> None:
    if not ctx.company or (not learned and not seen_questions):
        return
    path = company_memory_path(ctx)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}

    raw_questions = data.get("questions")
    questions = [item for item in raw_questions if isinstance(item, dict)] if isinstance(raw_questions, list) else []
    raw_seen = data.get("seen_questions")
    seen_bucket = [item for item in raw_seen if isinstance(item, dict)] if isinstance(raw_seen, list) else []

    changed = False
    for entry in learned:
        changed |= merge_memory_question_entry(
            questions,
            entry.get("match_any", []),
            entry.get("answer"),
            ctx,
            metadata={
                key: entry.get(key)
                for key in ("label", "source", "generated_at", "accepted_at", "acceptance_level")
            },
        )
    for item in seen_questions:
        if isinstance(item, dict):
            label = (
                item.get("label")
                or item.get("question")
                or next((frag for frag in item.get("match_any", []) if frag), "")
            )
            suggested_answer = item.get("suggested_answer", item.get("answer"))
            source = item.get("source")
        else:
            label = str(item)
            suggested_answer = None
            source = None
        changed |= merge_seen_question_entry(
            seen_bucket,
            label,
            ctx,
            suggested_answer=suggested_answer,
            source=source,
        )

    if changed or not path.exists():
        save_company_memory(ctx, questions, seen_bucket)


def remember_learned_answer(
    learned: list[dict[str, Any]],
    fragments: list[str],
    answers: list[str],
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    cleaned_fragments = []
    for fragment in fragments:
        normalized = normalize_override_key(fragment)
        if normalized and normalized not in cleaned_fragments:
            cleaned_fragments.append(normalized)
    cleaned_answers = [str(answer).strip() for answer in answers if str(answer).strip()]
    if not cleaned_fragments or not cleaned_answers:
        return
    clean_metadata = {
        str(key): value
        for key, value in (metadata or {}).items()
        if value not in (None, "", [], {})
    }
    answer_value: str | list[str]
    answer_value = cleaned_answers[0] if len(cleaned_answers) == 1 else cleaned_answers
    for entry in learned:
        existing_fragments = [normalize_override_key(item) for item in entry.get("match_any", [])]
        existing_answer = entry.get("answer")
        if (
            sorted(existing_fragments) == sorted(cleaned_fragments)
            and existing_answer == answer_value
        ):
            for key, value in clean_metadata.items():
                if entry.get(key) != value:
                    entry[key] = value
            return
    learned.append({"match_any": cleaned_fragments, "answer": answer_value, **clean_metadata})


def record_successful_qualitative_answers(
    target: list[dict[str, Any]],
    page_answers: list[dict[str, Any]],
    *,
    acceptance_level: str,
) -> None:
    if not page_answers:
        return
    accepted_at = now_iso()
    for entry in page_answers:
        answer = normalize_space(str(entry.get("answer") or ""))
        fragments = [normalize_override_key(item) for item in entry.get("match_any", []) if item]
        if not answer or not fragments:
            continue
        remember_learned_answer(
            target,
            fragments,
            [answer],
            metadata={
                "label": entry.get("label"),
                "source": entry.get("source", "llm_generated"),
                "generated_at": entry.get("generated_at"),
                "accepted_at": accepted_at,
                "acceptance_level": acceptance_level,
            },
        )


def persist_company_question_answers(ctx: PromptContext, learned: list[dict[str, Any]]) -> None:
    if not ctx.company or not learned:
        return
    data = load_question_overrides()
    bucket = company_override_bucket(data, ctx.company)
    changed = False
    for entry in learned:
        fragments = [normalize_override_key(item) for item in entry.get("match_any", []) if item]
        answer = entry.get("answer")
        if not fragments or answer in (None, "", []):
            continue
        duplicate = False
        for existing in bucket:
            existing_fragments = [normalize_override_key(item) for item in existing.get("match_any", []) if item]
            if sorted(existing_fragments) == sorted(fragments) and existing.get("answer") == answer:
                duplicate = True
                break
        if duplicate:
            continue
        new_entry = {
            "match_any": fragments,
            "answer": answer,
            "learned_from_success": True,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
        bucket.append(new_entry)
        changed = True
    if changed:
        save_question_overrides(data)
    persist_company_question_memory(ctx, learned, [])


def suggest_answer_for_question(question_text: str, profile: dict[str, Any], ctx: PromptContext) -> str | None:
    label = normalize_override_key(question_text)
    if not label:
        return None
    override = lookup_question_override(ctx, label, ["Yes", "No", "LinkedIn", "Indeed", "Other"])
    if override:
        return override
    if any(term in label for term in ("how did you hear", "hear about us", "source")):
        return applicant_value(profile, "heard_about")
    if any(term in label for term in ("authorized to work", "legally authorized", "work authorization")):
        return applicant_value(profile, "authorized")
    if any(term in label for term in ("sponsorship", "visa support", "require visa")):
        return applicant_value(profile, "sponsorship")
    if any(term in label for term in ("work permit", "permit type")):
        return applicant_value(profile, "permit_type")
    if any(term in label for term in ("previously worked for", "have previously worked for", "worked for any of")):
        return "No"
    if any(term in label for term in ("restrictive covenants", "non compete", "confidentiality agreements")):
        return "No"
    if any(term in label for term in ("current contractor", "vendor", "temporary worker")):
        return "No"
    return None


def should_track_workday_question(question_text: str) -> bool:
    normalized = normalize_override_key(question_text)
    if not normalized:
        return False
    ignored = (
        "page you are looking for does not exist",
        "page you are looking for doesn t exist",
        "page you are looking for doesn't exist",
        "page is loaded",
        "search for jobs",
        "skip to main content",
        "privacy statement",
        "cookie policy",
        "no longer accepting applications",
        "job is no longer available",
    )
    if any(fragment in normalized for fragment in ignored):
        return False
    question_markers = (
        "how ",
        "do ",
        "are ",
        "have ",
        "will ",
        "can ",
        "please indicate",
        "authorized",
        "sponsorship",
        "source",
        "worked for",
        "contractor",
        "covenants",
        "visa",
    )
    return normalized.endswith("?") or any(marker in normalized for marker in question_markers)


def greenhouse_tracking_label(field: dict[str, Any]) -> str:
    return normalize_space(
        str(field.get("label") or field.get("name") or field.get("placeholder") or "")
    )


def should_track_greenhouse_question(field: dict[str, Any]) -> bool:
    label = greenhouse_tracking_label(field)
    normalized = normalize_override_key(label)
    if not normalized:
        return False
    ignored_exact = {
        "first name",
        "preferred first name",
        "last name",
        "full name",
        "name",
        "email",
        "email address",
        "phone",
        "phone number",
        "mobile phone",
        "location",
        "city",
        "country",
        "state",
        "province",
        "postal code",
        "zip code",
        "address",
        "address line 1",
        "address line 2",
        "linkedin url",
        "github url",
        "website",
        "portfolio",
        "resume",
        "cover letter",
        "salary",
        "current company",
        "current title",
    }
    ignored_contains = (
        "security code",
        "verification code",
        "resume",
        "cover letter",
        "curriculum vitae",
        "attach",
        "upload",
        "password",
    )
    if normalized in ignored_exact or any(fragment in normalized for fragment in ignored_contains):
        return False
    question_markers = (
        "how ",
        "do ",
        "are ",
        "have ",
        "will ",
        "can ",
        "please indicate",
        "authorized",
        "authorization",
        "sponsorship",
        "visa",
        "source",
        "time zone",
        "describe you",
        "veteran",
        "disability",
        "gender",
        "race",
        "ethnicity",
        "self identify",
        "accommodation",
        "citizenship",
        "work eligibility",
    )
    return (
        normalized.endswith("?")
        or bool(field.get("options"))
        or field.get("type") in {"checkbox", "radio"}
        or greenhouse_select_like(field)
        or any(marker in normalized for marker in question_markers)
    )


def greenhouse_memory_answer(field: dict[str, Any], value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = normalize_space(str(value))
    if not text:
        return None
    if field.get("tag") == "textarea":
        return None
    if len(text) > 160:
        return None
    return text


def build_greenhouse_question_memory(
    page,
    profile: dict[str, Any],
    ctx: PromptContext,
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], list[str]]:
    learned: list[dict[str, Any]] = []
    seen: list[str] = []

    for field in annotate_fields(page):
        label = greenhouse_tracking_label(field)
        if not label or not should_track_greenhouse_question(field):
            continue
        if label not in seen:
            seen.append(label)
        answer = greenhouse_memory_answer(field, resolve_field_value(field, profile, ctx, args))
        if answer is None:
            continue
        remember_learned_answer(learned, [label], [answer])

    for error in visible_error_texts(page):
        clean = normalize_space(re.sub(r"^Error[-: ]*", "", error, flags=re.I))
        if clean and should_track_greenhouse_question({"label": clean}):
            if clean not in seen:
                seen.append(clean)

    return learned, seen


def build_workday_question_suggestions(
    page,
    ctx: PromptContext,
    profile: dict[str, Any],
    learned_answers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    for learned in learned_answers:
        remember_learned_answer(
            suggestions,
            learned.get("match_any", []),
            learned.get("answer") if isinstance(learned.get("answer"), list) else [learned.get("answer")],
        )

    candidates = []
    for error in visible_error_texts(page):
        clean = normalize_space(re.sub(r"^Error[-: ]*", "", error, flags=re.I))
        if clean:
            candidates.append(clean)
    for field in summarize_visible_fields(page, limit=12):
        label = normalize_space(field.split("[", 1)[0])
        if label:
            candidates.append(label)

    seen: set[str] = {
        normalize_override_key(fragment)
        for item in suggestions
        for fragment in item.get("match_any", [])
        if fragment
    }
    for question_text in candidates:
        normalized = normalize_override_key(question_text)
        if normalized in seen or not should_track_workday_question(question_text):
            continue
        seen.add(normalized)
        if lookup_question_override(ctx, question_text):
            continue
        suggestion = suggest_answer_for_question(question_text, profile, ctx)
        if suggestion:
            remember_learned_answer(suggestions, [question_text], [suggestion])
        else:
            suggestions.append(
                {
                    "match_any": [normalized],
                    "answer": "",
                }
            )
    return suggestions


def build_workday_memory_entries(suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_entries: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for entry in suggestions:
        fragments = [normalize_override_key(item) for item in entry.get("match_any", []) if normalize_override_key(item)]
        if not fragments:
            continue
        label = fragments[0]
        if label in seen_labels:
            continue
        seen_labels.add(label)
        answer = entry.get("answer")
        seen_entries.append(
            {
                "label": label,
                "suggested_answer": answer if answer not in (None, "", []) else None,
                "source": "workday_suggestion",
            }
        )
    return seen_entries


def persist_question_override_suggestions(ctx: PromptContext, suggestions: list[dict[str, Any]]) -> None:
    if not ctx.company or not suggestions:
        return
    data = load_json_file(QUESTION_SUGGESTIONS_PATH)
    bucket = company_override_bucket(data, ctx.company)
    changed = False
    for entry in suggestions:
        fragments = [normalize_override_key(item) for item in entry.get("match_any", []) if item]
        answer = entry.get("answer")
        if not fragments:
            continue
        duplicate = False
        for existing in bucket:
            existing_fragments = [normalize_override_key(item) for item in existing.get("match_any", []) if item]
            if sorted(existing_fragments) == sorted(fragments):
                duplicate = True
                break
        if duplicate:
            continue
        bucket.append(
            {
                "match_any": fragments,
                "answer": answer or "",
                "suggested_from_workday": True,
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }
        )
        changed = True
    if changed:
        save_json_file(QUESTION_SUGGESTIONS_PATH, data)


def append_workday_question_review(
    page,
    ctx: PromptContext,
    result: str,
    suggestions: list[dict[str, Any]],
) -> None:
    WORKDAY_QUESTION_REVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "company": ctx.company,
        "job_title": ctx.job_title,
        "job_url": ctx.job_url,
        "host": (urlparse(ctx.job_url).hostname or "").lower(),
        "result": result,
        "headings": page_headings(page),
        "buttons": visible_buttons(page)[:12],
        "errors": visible_error_texts(page),
        "fields": summarize_visible_fields(page, limit=12),
        "suggestions": suggestions,
    }
    with WORKDAY_QUESTION_REVIEW_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def name_parts(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split()
    if not parts:
        return "", ""
    return parts[0], parts[-1]


def phone_digits(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit())


def phone_local_number(phone: str) -> str:
    digits = phone_digits(phone)
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    if len(digits) >= 10:
        return digits[-10:]
    return digits


def phone_country_code(phone: str) -> str:
    digits = phone_digits(phone)
    if len(digits) == 11 and digits.startswith("1"):
        return "+1"
    if phone.strip().startswith("+"):
        parts = re.findall(r"\+\d+", phone)
        if parts:
            return parts[0]
    return "+1"


MONTH_NAME_MAP = {
    "jan": "January",
    "january": "January",
    "feb": "February",
    "february": "February",
    "mar": "March",
    "march": "March",
    "apr": "April",
    "april": "April",
    "may": "May",
    "jun": "June",
    "june": "June",
    "jul": "July",
    "july": "July",
    "aug": "August",
    "august": "August",
    "sep": "September",
    "sept": "September",
    "september": "September",
    "oct": "October",
    "october": "October",
    "nov": "November",
    "november": "November",
    "dec": "December",
    "december": "December",
}

DATE_RANGE_RE = re.compile(
    r"^\s*([A-Za-z]{3,9})\.?\s+(\d{4})\s*[–-]\s*(Present|[A-Za-z]{3,9}\.?\s+\d{4})\s*$",
    re.I,
)


def normalize_month_name(value: str) -> str:
    key = re.sub(r"[^A-Za-z]", "", value or "").lower()
    return MONTH_NAME_MAP.get(key, value.strip())


def parse_date_range(line: str) -> dict[str, Any] | None:
    match = DATE_RANGE_RE.match(normalize_space(line))
    if not match:
        return None
    start_month_raw, start_year, end_raw = match.groups()
    start_month = normalize_month_name(start_month_raw)
    if end_raw.lower() == "present":
        return {
            "start_month": start_month,
            "start_year": start_year,
            "end_month": "",
            "end_year": "",
            "current": True,
        }
    end_parts = end_raw.replace(".", "").split()
    if len(end_parts) != 2:
        return None
    end_month = normalize_month_name(end_parts[0])
    end_year = end_parts[1]
    return {
        "start_month": start_month,
        "start_year": start_year,
        "end_month": end_month,
        "end_year": end_year,
        "current": False,
    }


@lru_cache(maxsize=1)
def resume_work_history() -> list[dict[str, Any]]:
    if not RESUME_TEXT_PATH.exists():
        return []
    text = RESUME_TEXT_PATH.read_text(encoding="utf-8", errors="replace").replace("\x0c", "\n")
    lines = [normalize_space(line) for line in text.splitlines() if normalize_space(line)]
    in_experience = False
    entries: list[dict[str, Any]] = []
    for line in lines:
        lower = line.lower()
        if lower == "experience":
            in_experience = True
            continue
        if in_experience and lower == "projects":
            break
        if not in_experience:
            continue
        parsed = parse_date_range(line)
        if parsed:
            entries.append(parsed)
    return entries


def address_line1(address: str) -> str:
    if not address:
        return ""
    parts = [part.strip() for part in address.split(",") if part.strip()]
    if len(parts) >= 3:
        return " ".join(parts[:3])
    if len(parts) >= 2:
        return " ".join(parts[:2])
    return parts[0]


def normalize_salary(value: str) -> str:
    return re.sub(r"[^\d]", "", value or "")


def short_cover_text(cover_text: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", cover_text.strip())
    trimmed = " ".join(s for s in sentences[:3] if s)
    return trimmed[:900]


def load_workday_accounts() -> dict[str, Any]:
    if not WORKDAY_ACCOUNTS_PATH.exists():
        return {}
    try:
        data = json.loads(WORKDAY_ACCOUNTS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def save_workday_accounts(data: dict[str, Any]) -> None:
    WORKDAY_ACCOUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    WORKDAY_ACCOUNTS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(WORKDAY_ACCOUNTS_PATH, 0o600)
    except OSError:
        pass


def default_imap_host(email_address: str) -> str:
    domain = (email_address.rsplit("@", 1)[-1] if "@" in email_address else "").lower()
    if domain in {"gmail.com", "googlemail.com"}:
        return "imap.gmail.com"
    if domain in {"outlook.com", "hotmail.com", "live.com", "office365.com", "case.edu"}:
        return "outlook.office365.com"
    if domain == "yahoo.com":
        return "imap.mail.yahoo.com"
    return ""


def mailbox_config(email_address: str) -> dict[str, Any]:
    user = (
        os.environ.get("APPLYPILOT_IMAP_USER")
        or os.environ.get("APPLYPILOT_MAIL_USER")
        or email_address
    ).strip()
    password = (
        os.environ.get("APPLYPILOT_IMAP_PASSWORD")
        or os.environ.get("APPLYPILOT_MAIL_PASSWORD")
        or ""
    ).strip()
    host = (
        os.environ.get("APPLYPILOT_IMAP_HOST")
        or os.environ.get("APPLYPILOT_MAIL_HOST")
        or default_imap_host(user)
    ).strip()
    if host.lower() == "imap.gmail.com":
        password = re.sub(r"\s+", "", password)
    port_raw = (
        os.environ.get("APPLYPILOT_IMAP_PORT")
        or os.environ.get("APPLYPILOT_MAIL_PORT")
        or ""
    ).strip()
    folder = (
        os.environ.get("APPLYPILOT_IMAP_FOLDER")
        or os.environ.get("APPLYPILOT_MAIL_FOLDER")
        or "INBOX"
    ).strip()
    try:
        port = int(port_raw) if port_raw else 993
    except ValueError:
        port = 993
    return {"user": user, "password": password, "host": host, "port": port, "folder": folder}


def mailbox_enabled(email_address: str) -> bool:
    cfg = mailbox_config(email_address)
    return bool(cfg["host"] and cfg["user"] and cfg["password"])


def load_account_password_overrides() -> dict[str, str]:
    path = Path(
        os.environ.get("APPLYPILOT_ACCOUNT_PASSWORDS_FILE", "").strip() or ACCOUNT_PASSWORDS_PATH
    )
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str) and key.strip() and value:
            normalized[key.strip().lower()] = value
    return normalized


def decode_mime_words(value: str) -> str:
    try:
        parts = header.decode_header(value or "")
    except Exception:
        return value or ""
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded).strip()


def extract_links_from_text(text: str) -> list[str]:
    if not text:
        return []
    text = html.unescape(text)
    pattern = re.compile(r"https?://[^\s<>'\")]+")
    links = []
    for match in pattern.findall(text):
        link = match.rstrip(").,>")
        if link not in links:
            links.append(link)
    return links


def parse_mail_links(raw_message: bytes) -> tuple[str, str, list[str]]:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    subject = decode_mime_words(message.get("subject", ""))
    sender = decode_mime_words(message.get("from", ""))
    links: list[str] = []

    parts = [message]
    if message.is_multipart():
        parts = list(message.walk())

    for part in parts:
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            body = part.get_content()
        except Exception:
            try:
                payload = part.get_payload(decode=True) or b""
                body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            except Exception:
                body = ""
        for link in extract_links_from_text(body):
            if link not in links:
                links.append(link)
    return subject, sender, links


def parse_mail_bodies(raw_message: bytes) -> tuple[str, str, list[str]]:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    subject = decode_mime_words(message.get("subject", ""))
    sender = decode_mime_words(message.get("from", ""))
    bodies: list[str] = []

    parts = [message]
    if message.is_multipart():
        parts = list(message.walk())

    for part in parts:
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            body = part.get_content()
        except Exception:
            try:
                payload = part.get_payload(decode=True) or b""
                body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            except Exception:
                body = ""
        if body:
            bodies.append(str(body))
    return subject, sender, bodies


def score_mail_link(link: str, *, host: str, mode: str) -> int:
    lower = link.lower()
    score = 0
    if host.lower() in lower:
        score += 5
    if "myworkdayjobs.com" in lower:
        score += 4
    if mode == "reset":
        if any(token in lower for token in ("reset", "forgot", "change-password", "password")):
            score += 6
    if mode == "verify":
        if any(token in lower for token in ("verify", "verification", "confirm", "activate")):
            score += 6
    return score


def tenant_tokens_for_host(host: str) -> list[str]:
    ignored = {"wd1", "wd2", "wd3", "wd4", "wd5", "wd6", "wd7", "myworkdayjobs", "com"}
    tokens: list[str] = []
    for part in host.lower().split("."):
        if part in ignored:
            continue
        for token in re.split(r"[^a-z0-9]+", part):
            token = token.strip()
            if len(token) >= 4 and token not in ignored and token not in tokens:
                tokens.append(token)
    return tokens


def message_matches_workday_host(subject: str, sender: str, links: list[str], host: str) -> bool:
    haystack = f"{subject} {sender}".lower()
    link_text = " ".join(link.lower() for link in links)
    if host.lower() in haystack or host.lower() in link_text:
        return True
    return any(token in haystack or token in link_text for token in tenant_tokens_for_host(host))


def fetch_workday_mail_link(email_address: str, host: str, mode: str, timeout_s: int = 300) -> MailLink | None:
    if not mailbox_enabled(email_address):
        return None

    cfg = mailbox_config(email_address)
    deadline = time.time() + max(15, timeout_s)
    best: MailLink | None = None

    while time.time() < deadline:
        mailbox = None
        try:
            mailbox = imaplib.IMAP4_SSL(cfg["host"], cfg["port"])
            mailbox.login(cfg["user"], cfg["password"])
            mailbox.select(cfg["folder"])
            status, data = mailbox.search(None, "ALL")
            if status != "OK":
                time.sleep(5)
                continue
            message_ids = (data[0] or b"").split()[-50:]
            for msg_id in reversed(message_ids):
                fetch_status, payload = mailbox.fetch(msg_id, "(RFC822)")
                if fetch_status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                subject, sender, links = parse_mail_links(payload[0][1])
                subject_sender = f"{subject} {sender}".lower()
                if "workday" not in subject_sender and "password" not in subject_sender and "verify" not in subject_sender:
                    if not any("myworkdayjobs.com" in link.lower() or host.lower() in link.lower() for link in links):
                        continue
                if not message_matches_workday_host(subject, sender, links, host):
                    continue
                scored = sorted(
                    ((score_mail_link(link, host=host, mode=mode), link) for link in links),
                    reverse=True,
                )
                if scored and scored[0][0] > 0:
                    best = MailLink(subject=subject, sender=sender, link=scored[0][1])
                    break
            if best:
                break
        except Exception as exc:
            log(f"ACTION: mailbox poll error={type(exc).__name__}")
        finally:
            if mailbox is not None:
                try:
                    mailbox.logout()
                except Exception:
                    pass
        time.sleep(5)
    return best


def greenhouse_security_code_from_bodies(subject: str, sender: str, bodies: list[str], company: str) -> str | None:
    haystack = normalize_override_key(" ".join([subject, sender, company]))
    if "greenhouse" not in haystack and "security code" not in haystack:
        return None

    company_key = normalize_override_key(company)
    for body in bodies:
        normalized_body = normalize_override_key(body)
        if company_key and company_key not in normalized_body and company_key not in haystack:
            continue
        html_match = re.search(r"<h1[^>]*>\s*([A-Za-z0-9]{8})\s*</h1>", body, flags=re.I | re.S)
        if html_match:
            return html_match.group(1)
        text = html.unescape(re.sub(r"<[^>]+>", " ", body))
        text_match = re.search(
            r"(?:security code|confirm you're a human|confirm you are a human)[^A-Za-z0-9]{0,80}([A-Za-z0-9]{8})",
            text,
            flags=re.I | re.S,
        )
        if text_match:
            return text_match.group(1)
        generic_match = re.search(r"\b([A-Za-z0-9]{8})\b", text)
        if generic_match and ("security code" in normalized_body or "confirm youre a human" in normalized_body):
            return generic_match.group(1)
    return None


def fetch_greenhouse_security_code(email_address: str, company: str, timeout_s: int = 180) -> str | None:
    if not mailbox_enabled(email_address):
        return None

    cfg = mailbox_config(email_address)
    deadline = time.time() + max(15, timeout_s)

    while time.time() < deadline:
        mailbox = None
        try:
            mailbox = imaplib.IMAP4_SSL(cfg["host"], cfg["port"])
            mailbox.login(cfg["user"], cfg["password"])
            mailbox.select(cfg["folder"])
            status, data = mailbox.search(None, "ALL")
            if status != "OK":
                time.sleep(5)
                continue
            message_ids = (data[0] or b"").split()[-50:]
            for msg_id in reversed(message_ids):
                fetch_status, payload = mailbox.fetch(msg_id, "(RFC822)")
                if fetch_status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                subject, sender, bodies = parse_mail_bodies(payload[0][1])
                code = greenhouse_security_code_from_bodies(subject, sender, bodies, company)
                if code:
                    return code
        except Exception as exc:
            log(f"ACTION: greenhouse mailbox poll error={type(exc).__name__}")
        finally:
            if mailbox is not None:
                try:
                    mailbox.logout()
                except Exception:
                    pass
        time.sleep(5)
    return None


def generate_password() -> str:
    token = secrets.token_urlsafe(12)
    return f"Ap{token}!9a"


def shared_workday_password() -> str:
    explicit = os.environ.get("APPLYPILOT_WORKDAY_SHARED_PASSWORD", "").strip()
    if explicit:
        return explicit
    if SHARED_WORKDAY_PASSWORD_PATH.exists():
        existing = SHARED_WORKDAY_PASSWORD_PATH.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    password = generate_password()
    SHARED_WORKDAY_PASSWORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHARED_WORKDAY_PASSWORD_PATH.write_text(password, encoding="utf-8")
    try:
        os.chmod(SHARED_WORKDAY_PASSWORD_PATH, 0o600)
    except Exception:
        pass
    return password


def account_status(entry: dict[str, Any]) -> str:
    return str(entry.get("status") or "").strip().lower()


def account_ready_for_sign_in(entry: dict[str, Any]) -> bool:
    return account_status(entry) in {"created", "ready", "verified"}


def scoped_env_account_password(host: str) -> str:
    override = load_account_password_overrides().get(host.lower(), "")
    if override:
        return override
    explicit = os.environ.get("APPLYPILOT_ACCOUNT_PASSWORD", "").strip()
    if not explicit:
        return ""
    raw_hosts = os.environ.get("APPLYPILOT_ACCOUNT_PASSWORD_HOSTS", "").strip()
    if not raw_hosts:
        return explicit
    allowed_hosts = {item.strip().lower() for item in raw_hosts.split(",") if item.strip()}
    if not allowed_hosts:
        return explicit
    return explicit if host.lower() in allowed_hosts else ""


def account_key(host: str, email: str) -> str:
    return f"{host.lower()}::{email.lower()}"


def get_account_entry(host: str, email: str) -> dict[str, Any]:
    data = load_workday_accounts()
    entry = data.get(account_key(host, email))
    return entry if isinstance(entry, dict) else {}


def update_account_entry(host: str, email: str, **updates: Any) -> dict[str, Any]:
    data = load_workday_accounts()
    key = account_key(host, email)
    entry = dict(data.get(key) or {})
    entry["host"] = host.lower()
    entry["email"] = email
    entry["login_id"] = email
    entry.setdefault("account_url", f"https://{host}/")
    for field, value in updates.items():
        if value is None:
            entry.pop(field, None)
        else:
            entry[field] = value
    entry["updated_at"] = now_iso()
    data[key] = entry
    save_workday_accounts(data)
    return entry


def get_or_create_account_password(host: str, email: str) -> str:
    explicit = scoped_env_account_password(host)
    entry = get_account_entry(host, email)
    if entry and entry.get("password"):
        return entry["password"]

    if explicit:
        return explicit
    password = shared_workday_password()
    update_account_entry(
        host,
        email,
        password=password,
        source="agent_created_default",
        status="planned",
    )
    return password


def get_known_account_password(host: str, email: str) -> str:
    explicit = scoped_env_account_password(host)
    if explicit:
        return explicit

    entry = get_account_entry(host, email)
    if entry and entry.get("password") and account_ready_for_sign_in(entry):
        return entry["password"]
    return ""


def ensure_account_password(host: str, email: str) -> str:
    known = get_known_account_password(host, email)
    if known:
        return known

    entry = get_account_entry(host, email)
    if entry and entry.get("password") and entry.get("source") not in {"generated", "shared_generated"}:
        return str(entry["password"])

    password = shared_workday_password()
    update_account_entry(
        host,
        email,
        password=password,
        source="agent_created_default",
        status="planned",
    )
    return password


def clear_account_password(host: str, email: str, *, status: str | None = None) -> None:
    updates: dict[str, Any] = {"password": None}
    if status is not None:
        updates["status"] = status
    update_account_entry(host, email, **updates)


def complete_workday_password_reset(page, host: str, email: str) -> bool:
    password = ensure_account_password(host, email)
    fields = annotate_fields(page)
    password_fields = [field for field in fields if field.get("autocomplete") == "new-password"]
    if not password_fields:
        password_fields = [
            field for field in fields
            if "password" in " ".join(
                str(value or "") for value in (field.get("label"), field.get("name"), field.get("placeholder"))
            ).lower()
        ]
    if not password_fields:
        return False

    fill_field(page, password_fields[0], password)
    if len(password_fields) > 1:
        fill_field(page, password_fields[1], password)

    submitted = click_button_exact(page, "Save", submit_only=True)
    if not submitted:
        submitted = click_button_exact(page, "Reset Password", submit_only=True)
    if not submitted:
        submitted = click_button_exact(page, "Submit", submit_only=True)
    if not submitted:
        submit_buttons = page.get_by_role("button", name=re.compile(r"^Submit$", re.I))
        for idx in range(submit_buttons.count()):
            if click_locator(submit_buttons.nth(idx), wait_ms=1000):
                submitted = True
                break
    if not submitted:
        submitted = click_exact_any(page, "Reset Password", use_last=True)
    if not submitted:
        submitted = click_exact_any(page, "Save", use_last=True)
    if not submitted:
        submitted = click_exact_any(page, "Submit", use_last=True)
    if not submitted:
        submitted = click_exact_any(page, "Continue", use_last=True)
    if not submitted:
        try:
            form = page.locator("form").first
            if form.count():
                form.evaluate("(node) => node.requestSubmit ? node.requestSubmit() : node.submit()")
                page.wait_for_timeout(1000)
                submitted = True
        except Exception:
            submitted = False
    if not submitted:
        return False

    page.wait_for_timeout(5000)
    text = f"{page_text(page)} {' '.join(visible_error_texts(page))}"
    if contains_any(
        text,
        (
            "password has been reset",
            "password was changed",
            "sign in",
            "successfully reset",
        ),
    ):
        update_account_entry(host, email, password=password, status="ready")
        return True
    return False


def open_workday_mail_link(page, link: str) -> str:
    try:
        page.goto(link, wait_until="networkidle", timeout=120000)
        page.wait_for_timeout(2000)
    except Exception:
        return "failed"

    text = page_text(page)
    if contains_any(
        text,
        (
            "verify your email",
            "confirm your email",
            "set your password",
            "reset your password",
            "new password",
        ),
    ):
        return "password"
    if contains_any(
        text,
        (
            "account verified",
            "email verified",
            "thank you for verifying",
            "verification successful",
        ),
    ):
        return "verified"
    return "opened"


def maybe_complete_workday_mailbox_flow(page, host: str, email: str, mode: str) -> str | None:
    mail_link = fetch_workday_mail_link(email, host, mode)
    if mail_link is None:
        return None

    log(f"ACTION: mailbox link matched subject={mail_link.subject!r} sender={mail_link.sender!r}")
    open_result = open_workday_mail_link(page, mail_link.link)
    if mode == "reset":
        if open_result == "password" and complete_workday_password_reset(page, host, email):
            log(f"ACTION: mailbox password reset completed for {host}")
            return "reset_completed"
        if open_result in {"verified", "opened"}:
            return "reset_link_opened"
    if mode == "verify":
        if open_result in {"verified", "opened"}:
            update_account_entry(host, email, status="verified")
            log(f"ACTION: mailbox verification link opened for {host}")
            return "verification_completed"
    return None


def post_json(url: str, payload: dict[str, Any], timeout: float, headers: dict[str, str]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method="POST")
    with request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def call_ollama(base_url: str, model: str, messages: list[dict[str, str]], timeout: float) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": 4096},
    }
    response = post_json(
        f"{base_url}/api/chat",
        payload,
        timeout,
        headers={"Content-Type": "application/json"},
    )
    return response.get("message", {}).get("content", "").strip()


def call_openai(base_url: str, model: str, messages: list[dict[str, str]], timeout: float, api_key: str) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 180,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = post_json(
        f"{base_url}/chat/completions",
        payload,
        timeout,
        headers=headers,
    )
    choices = response.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message", {}).get("content") or "").strip()


def llm_text(args: argparse.Namespace, prompt: str) -> str:
    if not args.model:
        return ""
    messages = [
        {
            "role": "system",
            "content": (
                "Write concise, professional job application text. "
                "Follow the user's instructions exactly, stay factual, and return plain text only."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    try:
        base_url = resolve_base_url(args.provider, args.base_url)
        if args.provider == "ollama":
            return call_ollama(base_url, args.model, messages, args.timeout)
        return call_openai(base_url, args.model, messages, args.timeout, args.api_key)
    except Exception:
        return ""


def page_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(pattern in lower for pattern in patterns)


def accept_cookies(page) -> None:
    for name in ("Accept Cookies", "Accept"):
        try:
            locator = page.get_by_role("button", name=name)
            if locator.count():
                locator.first.click(timeout=2000)
                page.wait_for_timeout(750)
                return
        except Exception:
            continue


def click_text(page, texts: list[str], tags: str = "button, a, [role='button']") -> bool:
    for text in texts:
        locator = page.locator(tags).filter(has_text=re.compile(rf"^{re.escape(text)}$", re.I))
        if locator.count():
            try:
                locator.first.click(timeout=5000)
                page.wait_for_timeout(1000)
                return True
            except Exception:
                continue
    return False


def click_contains(page, texts: list[str], tags: str = "button, a, [role='button']") -> bool:
    for text in texts:
        locator = page.locator(tags).filter(has_text=re.compile(re.escape(text), re.I))
        if locator.count():
            try:
                locator.first.click(timeout=5000)
                page.wait_for_timeout(1000)
                return True
            except Exception:
                continue
    return False


def click_locator(locator, *, use_last: bool = False, wait_ms: int = 1000) -> bool:
    if not locator.count():
        return False
    try:
        target = locator.last if use_last else locator.first
        target.scroll_into_view_if_needed(timeout=3000)
        target.click(timeout=5000)
        if wait_ms:
            target.page.wait_for_timeout(wait_ms)
        return True
    except Exception:
        return False


def click_button_exact(page, text: str, *, submit_only: bool = False, use_last: bool = False) -> bool:
    selector = "button[type='submit']" if submit_only else "button"
    locator = page.locator(selector).filter(has_text=re.compile(rf"^{re.escape(text)}$", re.I))
    return click_locator(locator, use_last=use_last)


def click_named_control(
    page,
    texts: list[str] | tuple[str, ...] | str,
    *,
    submit_only: bool = False,
    use_last: bool = False,
) -> bool:
    labels = [texts] if isinstance(texts, str) else [text for text in texts if text]
    if not labels:
        return False
    try:
        clicked = page.evaluate(
            """({ labels, submitOnly, useLast }) => {
                const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const wanted = labels.map(normalize).filter(Boolean);
                if (!wanted.length) return false;
                const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' &&
                        style.display !== 'none' &&
                        rect.width > 0 &&
                        rect.height > 0;
                };
                const selectors = submitOnly
                    ? ['button[type="submit"]', 'input[type="submit"]']
                    : ['button', 'input[type="submit"]', 'input[type="button"]', '[role="button"]', 'a'];
                const candidates = [];
                for (const selector of selectors) {
                    for (const el of document.querySelectorAll(selector)) {
                        if (!visible(el) || el.disabled) continue;
                        const values = [
                            el.innerText,
                            el.textContent,
                            el.value,
                            el.getAttribute('aria-label'),
                            el.getAttribute('title'),
                            el.getAttribute('name'),
                        ]
                            .map(normalize)
                            .filter(Boolean);
                        if (!values.length) continue;
                        const matched = values.some((value) =>
                            wanted.some((label) => value === label || value.includes(label) || label.includes(value))
                        );
                        if (matched) {
                            candidates.push(el);
                        }
                    }
                }
                if (!candidates.length) return false;
                const target = useLast ? candidates[candidates.length - 1] : candidates[0];
                target.scrollIntoView({ block: 'center', inline: 'center' });
                try {
                    target.click();
                } catch (error) {
                    target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                }
                return true;
            }""",
            {"labels": labels, "submitOnly": submit_only, "useLast": use_last},
        )
    except Exception:
        clicked = False
    if clicked:
        page.wait_for_timeout(1000)
    return bool(clicked)


def click_role_button(page, pattern: str) -> bool:
    locator = page.get_by_role("button", name=re.compile(pattern, re.I))
    return click_locator(locator)


def click_exact_any(page, text: str, *, use_last: bool = False) -> bool:
    locator = page.locator("button, a, [role='button']").filter(
        has_text=re.compile(rf"^{re.escape(text)}$", re.I)
    )
    return click_locator(locator, use_last=use_last)


def visible_buttons(page) -> list[str]:
    try:
        return page.locator("button, a, [role='button']").evaluate_all(
            """els => els.map(el => (el.innerText || el.getAttribute('aria-label') || '').trim()).filter(Boolean)"""
        )
    except Exception:
        return []


def visible_action_controls(page, limit: int = 20) -> list[str]:
    try:
        values = page.locator("button, a, [role='button'], input[type='submit'], input[type='button']").evaluate_all(
            """els => els
                .map(el => (
                    el.innerText ||
                    el.textContent ||
                    el.value ||
                    el.getAttribute('aria-label') ||
                    el.getAttribute('title') ||
                    ''
                ).trim())
                .filter(Boolean)
            """
        )
    except Exception:
        return []
    unique: list[str] = []
    for value in values:
        text = normalize_space(value)
        if not text or text in unique:
            continue
        unique.append(text)
        if len(unique) >= limit:
            break
    return unique


def page_headings(page, limit: int = 6) -> list[str]:
    try:
        headings = page.locator(
            "h1, h2, h3, [data-automation-id='pageHeader'], [data-automation-id='pageTitle']"
        ).evaluate_all(
            """els => els.map(el => (el.innerText || el.textContent || '').trim()).filter(Boolean)"""
        )
    except Exception:
        return []

    unique: list[str] = []
    for heading in headings:
        clean = normalize_space(heading)
        if clean and clean not in unique:
            unique.append(clean)
        if len(unique) >= limit:
            break
    return unique


def summarize_visible_fields(page, limit: int = 10) -> list[str]:
    try:
        fields = annotate_fields(page)
    except Exception:
        return []

    summaries: list[str] = []
    for field in fields:
        label = normalize_space(
            " ".join(
                part for part in [field.get("label"), field.get("name"), field.get("placeholder")] if part
            )
        )
        if not label:
            continue
        marker = "required" if field.get("required") else "optional"
        value = normalize_space(str(field.get("value") or ""))[:40]
        summaries.append(
            f"{label} [{field.get('tag') or field.get('role') or field.get('type')}] {marker} value={value}"
        )
        if len(summaries) >= limit:
            break
    return summaries


def visible_error_texts(page, limit: int = 8) -> list[str]:
    selectors = ", ".join(
        [
            "[data-automation-id*='error']",
            "[id*='error']",
            "[aria-live='assertive']",
            "[role='alert']",
            ".error",
        ]
    )
    try:
        values = page.locator(selectors).evaluate_all(
            """els => els.map(el => (el.innerText || el.textContent || '').trim()).filter(Boolean)"""
        )
    except Exception:
        return []

    unique: list[str] = []
    for value in values:
        clean = normalize_space(value)
        if clean and clean not in unique:
            unique.append(clean)
        if len(unique) >= limit:
            break
    return unique


def force_workday_english(page, text: str) -> bool:
    lower = text.lower()
    needs_reset = (
        "/es/" in page.url
        or "mi información" in lower
        or "preguntas de solicitud" in lower
        or "se han encontrado errores" in lower
    )
    if not needs_reset:
        return False
    english_url = page.url.replace("/es/", "/en-US/")
    if english_url == page.url:
        return False
    page.goto(english_url, wait_until="networkidle", timeout=120000)
    page.wait_for_timeout(1500)
    return True


def workday_signature(page, text: str) -> str:
    payload = {
        "url": page.url,
        "headings": page_headings(page),
        "buttons": visible_buttons(page)[:12],
        "fields": summarize_visible_fields(page, limit=12),
        "errors": visible_error_texts(page),
        "text_head": normalize_space(text)[:1200],
        "text_tail": normalize_space(text)[-800:],
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def log_workday_snapshot(page, text: str, prefix: str = "ACTION") -> None:
    log(
        f"{prefix}: snapshot headings={page_headings(page)} "
        f"buttons={visible_buttons(page)[:10]} "
        f"errors={visible_error_texts(page)} "
        f"fields={summarize_visible_fields(page, limit=8)} "
        f"text={normalize_space(text)[:240]!r}"
    )


def is_workday_listing_page(page, text: str) -> bool:
    buttons = [normalize_space(button).lower() for button in visible_buttons(page)]
    if not any(button in {"apply", "start your application", "start application", "get started"} for button in buttons):
        return False
    if summarize_visible_fields(page, limit=3):
        return False
    lower = text.lower()
    if "create account/sign in" in lower or "sign in with email" in lower:
        return False
    if "apply manually" in lower or "autofill with resume" in lower:
        return False
    return True


def is_success_page(text: str) -> bool:
    return contains_any(
        text,
        (
            "application submitted",
            "application received",
            "thank you for applying",
            "thanks for applying",
            "we have received your application",
        ),
    )


def is_expired_page(text: str) -> bool:
    return contains_any(
        text,
        (
            "no longer accepting applications",
            "job is no longer available",
            "job is no longer accepting",
            "posting is no longer available",
            "this job post is closed",
            "this opening is no longer available",
            "this position has been filled",
            "page you are looking for does not exist",
            "page you are looking for doesn t exist",
            "page you are looking for doesn't exist",
            "job no longer exists",
            "position is no longer available",
        ),
    )


def annotate_fields(page) -> list[dict[str, Any]]:
    return page.evaluate(
        """() => {
            const visible = (el) => {
              const style = window.getComputedStyle(el);
              const rect = el.getBoundingClientRect();
              return style.visibility !== 'hidden' && style.display !== 'none' &&
                rect.width > 0 && rect.height > 0;
            };
            const labelFor = (el) => {
              const labels = [];
              if (el.labels) {
                for (const label of el.labels) {
                  const text = (label.innerText || '').trim();
                  if (text) labels.push(text);
                }
              }
              if (el.id) {
                const byFor = document.querySelector(`label[for="${el.id}"]`);
                if (byFor) {
                  const text = (byFor.innerText || '').trim();
                  if (text) labels.push(text);
                }
              }
              const parentLabel = el.closest('label');
              if (parentLabel) {
                const text = (parentLabel.innerText || '').trim();
                if (text) labels.push(text);
              }
              const ariaLabel = el.getAttribute('aria-label') || '';
              const placeholder = el.getAttribute('placeholder') || '';
              const name = el.getAttribute('name') || '';
              return labels.filter(Boolean).join(' | ') || ariaLabel || placeholder || name;
            };
            let idx = 0;
            const nodes = document.querySelectorAll(
              'input, textarea, select, [role="combobox"], [role="textbox"]'
            );
            return Array.from(nodes)
              .filter((el) => visible(el) && !el.disabled && (el.type || '').toLowerCase() !== 'hidden')
              .map((el) => {
                const apgId = `apg-${++idx}`;
                el.setAttribute('data-apg-id', apgId);
                const role = el.getAttribute('role') || '';
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                const options = tag === 'select'
                  ? Array.from(el.options || []).map((opt) => (opt.textContent || '').trim()).filter(Boolean)
                  : [];
                return {
                  apgId,
                  tag,
                  role,
                  type,
                  id: el.getAttribute('id') || '',
                  class_name: el.className || '',
                  label: labelFor(el),
                  name: el.getAttribute('name') || '',
                  placeholder: el.getAttribute('placeholder') || '',
                  autocomplete: el.getAttribute('autocomplete') || '',
                  required: el.required || el.getAttribute('aria-required') === 'true',
                  checked: !!el.checked,
                  options,
                  value: el.value || '',
                };
              });
          }"""
    )


def locator_for(page, apg_id: str):
    return page.locator(f'[data-apg-id="{apg_id}"]').first


def select_best_option(options: list[str], desired: str) -> str | None:
    if not desired:
        return None
    desired_lower = desired.lower()
    for option in options:
        if option.lower() == desired_lower:
            return option
    for option in options:
        if desired_lower in option.lower() or option.lower() in desired_lower:
            return option
    return None


def applicant_value(profile: dict[str, Any], key: str) -> str:
    personal = profile.get("personal", {})
    work = profile.get("work_authorization", {})
    comp = profile.get("compensation", {})
    exp = profile.get("experience", {})
    avail = profile.get("availability", {})
    eeo = profile.get("eeo_voluntary", {})

    first_name, last_name = name_parts(personal.get("full_name", ""))
    values = {
        "full_name": personal.get("full_name", ""),
        "first_name": first_name,
        "last_name": last_name,
        "email": personal.get("email", ""),
        "phone": personal.get("phone", ""),
        "phone_digits": phone_digits(personal.get("phone", "")),
        "phone_local": phone_local_number(personal.get("phone", "")),
        "phone_country_code": phone_country_code(personal.get("phone", "")),
        "city": personal.get("city", ""),
        "state": personal.get("province_state", ""),
        "country": personal.get("country", ""),
        "postal_code": personal.get("postal_code", ""),
        "address": personal.get("address", ""),
        "address_line1": address_line1(personal.get("address", "")),
        "linkedin": personal.get("linkedin_url", ""),
        "github": personal.get("github_url", ""),
        "website": personal.get("website_url", "") or personal.get("portfolio_url", ""),
        "salary": normalize_salary(comp.get("salary_expectation", "")),
        "salary_range": comp.get("salary_range_min", ""),
        "authorized": "Yes" if work.get("legally_authorized_to_work") else "No",
        "sponsorship": "Yes" if work.get("require_sponsorship") else "No",
        "permit_type": work.get("work_permit_type", ""),
        "years": str(exp.get("years_of_experience_total", "")),
        "education": exp.get("education_level", ""),
        "current_title": exp.get("current_title", ""),
        "start_date": avail.get("earliest_start_date", ""),
        "gender": eeo.get("gender", "Decline to self-identify"),
        "race": eeo.get("race_ethnicity", "Decline to self-identify"),
        "veteran": eeo.get("veteran_status", "Decline to self-identify"),
        "disability": eeo.get("disability_status", "Decline to self-identify"),
        "heard_about": "LinkedIn",
        "phone_device_type": "Mobile",
        "previous_employer": "No",
    }
    return values.get(key, "")


QUALITATIVE_STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "been",
    "being",
    "between",
    "both",
    "company",
    "describe",
    "experience",
    "from",
    "have",
    "into",
    "role",
    "tell",
    "that",
    "their",
    "them",
    "this",
    "what",
    "when",
    "where",
    "which",
    "with",
    "would",
    "your",
}


def split_context_chunks(text: str, *, max_chunk_chars: int = 500) -> list[str]:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = [normalize_space(block) for block in re.split(r"\n\s*\n+", normalized) if normalize_space(block)]
    chunks: list[str] = []
    for block in blocks:
        if len(block) <= max_chunk_chars:
            chunks.append(block)
            continue
        sentences = [normalize_space(part) for part in re.split(r"(?<=[.!?])\s+", block) if normalize_space(part)]
        current = ""
        for sentence in sentences:
            candidate = f"{current} {sentence}".strip()
            if current and len(candidate) > max_chunk_chars:
                chunks.append(current)
                current = sentence
            else:
                current = candidate
        if current:
            chunks.append(current)
    return chunks


def keyword_tokens(text: str) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9+#./-]*", normalize_override_key(text))
        if len(token) >= 3 and token not in QUALITATIVE_STOPWORDS
    }
    return tokens


def select_relevant_snippets(
    question: str,
    source_text: str,
    *,
    role_hint: str = "",
    max_chunks: int,
    max_chars: int,
) -> list[str]:
    chunks = split_context_chunks(source_text)
    if not chunks:
        return []
    q_tokens = keyword_tokens(question)
    role_tokens = keyword_tokens(role_hint)

    scored: list[tuple[int, int, str]] = []
    for idx, chunk in enumerate(chunks):
        c_tokens = keyword_tokens(chunk)
        overlap = len(q_tokens & c_tokens)
        role_overlap = len(role_tokens & c_tokens)
        score = overlap * 5 + role_overlap * 2
        if score == 0 and idx >= max_chunks * 2:
            continue
        scored.append((score, idx, chunk))

    if not scored:
        scored = [(0, idx, chunk) for idx, chunk in enumerate(chunks[:max_chunks])]

    top = sorted(scored, key=lambda item: (-item[0], item[1]))[: max(max_chunks, 1)]
    selected: list[str] = []
    total_chars = 0
    for _score, _idx, chunk in top:
        if total_chars and total_chars + len(chunk) > max_chars:
            continue
        selected.append(chunk)
        total_chars += len(chunk)
        if len(selected) >= max_chunks or total_chars >= max_chars:
            break
    return selected


def candidate_fact_lines(profile: dict[str, Any]) -> list[str]:
    facts = [
        ("Current title", applicant_value(profile, "current_title")),
        ("Years of experience", applicant_value(profile, "years")),
        ("Education", applicant_value(profile, "education")),
        ("Location", ", ".join(part for part in [
            applicant_value(profile, "city"),
            applicant_value(profile, "state"),
            applicant_value(profile, "country"),
        ] if part)),
        ("Work authorization", applicant_value(profile, "authorized")),
        ("Sponsorship needed", applicant_value(profile, "sponsorship")),
        ("Work permit type", applicant_value(profile, "permit_type")),
        ("Available to start", applicant_value(profile, "start_date")),
    ]
    return [f"{label}: {value}" for label, value in facts if value]


def conservative_open_ended_fallback(ctx: PromptContext) -> str:
    fallback = short_cover_text(ctx.cover_text)
    if fallback:
        return fallback
    return (
        f"I am excited about {ctx.job_title} at {ctx.company} because it aligns with my background in "
        "AI-driven software and backend systems. I would bring hands-on experience building reliable, "
        "production-focused engineering solutions."
    )


def should_route_to_qualitative_llm(field: dict[str, Any]) -> bool:
    label = normalize_override_key(
        " ".join(
            part for part in [field.get("label"), field.get("name"), field.get("placeholder")] if part
        )
    )
    if not label:
        return False

    excluded_fragments = (
        "password",
        "security code",
        "verification code",
        "email address",
        "phone number",
        "linkedin",
        "github",
        "website",
        "portfolio",
        "salary",
        "compensation",
        "pay expectation",
        "first name",
        "last name",
        "full name",
        "address",
        "city",
        "state",
        "province",
        "country",
        "postal code",
        "zip code",
        "resume",
        "cover letter",
        "upload",
        "attachment",
        "autofill",
    )
    if any(fragment in label for fragment in excluded_fragments):
        return False

    if field.get("tag") == "textarea":
        return True

    qualitative_markers = (
        "why ",
        "what interests",
        "tell us",
        "describe",
        "summary",
        "anything else",
        "additional information",
        "anything you would like us to know",
        "motivation",
        "background",
        "relevant experience",
    )
    return any(marker in label for marker in qualitative_markers)


def build_qualitative_prompt(field_label: str, ctx: PromptContext, profile: dict[str, Any]) -> str:
    role_hint = f"{ctx.job_title} {ctx.company}".strip()
    jd_snippets = select_relevant_snippets(
        field_label,
        ctx.job_description,
        role_hint=role_hint,
        max_chunks=4,
        max_chars=1800,
    )
    resume_snippets = select_relevant_snippets(
        field_label,
        ctx.resume_text,
        role_hint=role_hint,
        max_chunks=4,
        max_chars=1800,
    )
    cover_snippets = select_relevant_snippets(
        field_label,
        ctx.cover_text,
        role_hint=role_hint,
        max_chunks=2,
        max_chars=900,
    )
    candidate_facts = candidate_fact_lines(profile)

    def bullet_block(items: list[str], empty: str) -> str:
        if not items:
            return f"- {empty}"
        return "\n".join(f"- {item}" for item in items)

    return (
        "Write a concise job application answer.\n\n"
        f"Question:\n{normalize_space(field_label)}\n\n"
        f"Company: {ctx.company}\n"
        f"Role: {ctx.job_title}\n\n"
        "Rules:\n"
        "- Write in first person.\n"
        "- Keep it to 2-4 sentences unless the question clearly asks for more detail.\n"
        "- Stay factual and grounded in the provided materials.\n"
        "- Do not invent employers, tools, metrics, credentials, or years.\n"
        "- Do not mention the resume or cover letter explicitly.\n"
        "- Plain text only.\n\n"
        "Candidate facts:\n"
        f"{bullet_block(candidate_facts, 'Use only the resume and cover letter context below.')}\n\n"
        "Relevant job description:\n"
        f"{bullet_block(jd_snippets, 'Not available.')}\n\n"
        "Relevant tailored resume details:\n"
        f"{bullet_block(resume_snippets, 'Not available.')}\n\n"
        "Relevant cover letter details:\n"
        f"{bullet_block(cover_snippets, 'Not available.')}\n\n"
        "Final answer:"
    )


def qualitative_memory_entry(field_label: str, answer: str) -> dict[str, Any]:
    normalized = normalize_override_key(field_label)
    return {
        "match_any": [normalized],
        "answer": normalize_space(answer),
        "label": normalize_space(field_label),
        "source": "llm_generated",
        "generated_at": now_iso(),
    }


def open_ended_answer(
    field: dict[str, Any],
    ctx: PromptContext,
    profile: dict[str, Any],
    args: argparse.Namespace,
) -> tuple[str, dict[str, Any] | None]:
    field_label = normalize_space(str(field.get("label") or field.get("name") or "application question"))
    cached = ctx.qualitative_cache.get(normalize_override_key(field_label))
    if cached:
        return str(cached.get("answer") or ""), cached

    override = lookup_question_override(ctx, field_label, field.get("options") or [])
    if override is not None:
        return override, None

    prompt = build_qualitative_prompt(field_label, ctx, profile)
    generated = normalize_space(llm_text(args, prompt))
    if generated:
        entry = qualitative_memory_entry(field_label, generated)
        ctx.qualitative_cache[normalize_override_key(field_label)] = entry
        if args.verbose:
            log(
                f"ACTION: qualitative answer generated label={field_label!r} "
                f"answer={generated[:160]!r}"
            )
        return generated, entry
    return conservative_open_ended_fallback(ctx), None


def resolve_field_value(field: dict[str, Any], profile: dict[str, Any], ctx: PromptContext, args: argparse.Namespace) -> Any:
    label = " ".join(
        value for value in [field.get("label"), field.get("name"), field.get("placeholder")] if value
    ).lower()
    field_type = field.get("type", "")
    options = field.get("options") or []
    field_name = str(field.get("name") or field.get("id") or "").lower()
    today = datetime.now()

    if field.get("name") == "website":
        return None
    if "password" in label:
        return None
    if field_name.startswith("workexperience-") and ("roledescription" in field_name or "datesection" in field_name):
        return None
    if field_type == "file":
        if "cover" in label and ctx.cover_pdf and ctx.cover_pdf != "N/A":
            return ctx.cover_pdf
        if any(term in label for term in ("resume", "cv", "autofill")):
            return ctx.resume_pdf
        return None
    override = lookup_question_override(ctx, label, options)
    if override is not None:
        if field_type == "checkbox":
            return override.strip().lower() in {"1", "true", "yes", "on", "checked"}
        return override
    if field_type == "checkbox":
        if any(term in label for term in ("consent", "acknowledg", "agree", "terms", "privacy")):
            return True
        disability_pref = applicant_value(profile, "disability").lower()
        if "yes, i have a disability" in label or "have had one in the past" in label:
            return disability_pref.startswith("yes")
        if "no, i do not have a disability" in label:
            return disability_pref.startswith("no")
        if "i do not want to answer" in label:
            return "decline" in disability_pref or "do not want" in disability_pref
        if any(term in label for term in ("veteran", "disability", "gender", "race", "ethnicity")):
            return False
        return None
    if label.strip() == "month":
        return today.strftime("%m")
    if label.strip() == "day":
        return today.strftime("%d")
    if label.strip() == "year":
        return today.strftime("%Y")
    if "email" in label:
        return applicant_value(profile, "email")
    if "first name" in label or "given name" in label:
        return applicant_value(profile, "first_name")
    if "last name" in label or "family name" in label or "surname" in label:
        return applicant_value(profile, "last_name")
    if any(term in label for term in ("full name", "legal name", "applicant name", "name")) and "user" not in label:
        return applicant_value(profile, "full_name")
    if "phone extension" in label:
        return ""
    if "country phone code" in label:
        return applicant_value(profile, "phone_country_code")
    if "phone device type" in label:
        desired = applicant_value(profile, "phone_device_type")
        return select_best_option(options, desired) if options else desired
    if "phone number" in label:
        return applicant_value(profile, "phone_local")
    if "phone" in label:
        return applicant_value(profile, "phone_digits") if "digit" in label else applicant_value(profile, "phone")
    if any(term in label for term in ("how did you hear", "hear about us", "source")):
        desired = applicant_value(profile, "heard_about")
        return select_best_option(options, desired) if options else desired
    if "previously worked for" in label or "previously employed" in label:
        desired = applicant_value(profile, "previous_employer")
        return select_best_option(options, desired) if options else desired
    if "address line 1" in label or "street address" in label:
        return applicant_value(profile, "address_line1")
    if "address line 2" in label:
        return ""
    if "accommodation" in label or "accommodations" in label:
        return "No accommodations needed at this time."
    if "city" in label:
        return applicant_value(profile, "city")
    if any(term in label for term in ("state", "province", "region")):
        desired = applicant_value(profile, "state")
        return select_best_option(options, desired) if options else desired
    if any(term in label for term in ("authorized to work", "legally authorized", "work authorization", "eligible to work", "eligible for work")):
        desired = applicant_value(profile, "authorized")
        return select_best_option(options, desired) if options else desired
    if "based and plan to work from the us" in label:
        desired = "Yes"
        return select_best_option(options, desired) if options else desired
    if "based and plan to work from canada" in label:
        desired = "No" if applicant_value(profile, "country").lower() not in {"canada", "ca"} else "Yes"
        return select_best_option(options, desired) if options else desired
    if "based in quebec" in label:
        desired = "No"
        return select_best_option(options, desired) if options else desired
    if any(term in label for term in ("sponsorship", "sponsor", "visa support", "require visa")):
        desired = applicant_value(profile, "sponsorship")
        return select_best_option(options, desired) if options else desired
    if field_name == "country" or field.get("id") == "country":
        desired = applicant_value(profile, "country")
        return select_best_option(options, desired) if options else desired
    if "country" in label:
        desired = applicant_value(profile, "country")
        return select_best_option(options, desired) if options else desired
    if any(term in label for term in ("zip", "postal")):
        return applicant_value(profile, "postal_code")
    if "linkedin" in label:
        return applicant_value(profile, "linkedin")
    if "github" in label:
        return applicant_value(profile, "github")
    if any(term in label for term in ("website", "portfolio", "personal site")):
        return applicant_value(profile, "website")
    if "salary" in label or "compensation" in label or "pay expectation" in label:
        return applicant_value(profile, "salary")
    if "work permit" in label or "permit type" in label:
        desired = applicant_value(profile, "permit_type")
        return select_best_option(options, desired) if options else desired
    if any(term in label for term in ("years of experience", "years experience")):
        return applicant_value(profile, "years")
    if any(term in label for term in ("education", "degree")):
        desired = applicant_value(profile, "education")
        return select_best_option(options, desired) if options else desired
    if any(term in label for term in ("current title", "job title", "current role")):
        return applicant_value(profile, "current_title")
    if any(term in label for term in ("start date", "available to start", "availability")):
        return applicant_value(profile, "start_date")
    if "gender" in label:
        desired = applicant_value(profile, "gender")
        return select_best_option(options, desired) if options else desired
    if any(term in label for term in ("race", "ethnicity")):
        desired = applicant_value(profile, "race")
        return select_best_option(options, desired) if options else desired
    if "veteran" in label:
        desired = applicant_value(profile, "veteran")
        return select_best_option(options, desired) if options else desired
    if "disability" in label:
        desired = applicant_value(profile, "disability")
        return select_best_option(options, desired) if options else desired
    if any(
        phrase in label
        for phrase in (
            "have you worked in aws with clinical trial data pipelines",
            "have you created and executed compliance plans",
            "have you worked with clinical trial data pipelines",
            "have you executed compliance plans",
        )
    ):
        desired = "No"
        return select_best_option(options, desired) if options else desired
    return None


def greenhouse_select_like(field: dict[str, Any]) -> bool:
    class_name = str(field.get("class_name") or "").lower()
    placeholder = str(field.get("placeholder") or "").lower()
    label = str(field.get("label") or "").lower()
    name = str(field.get("name") or field.get("id") or "").lower()
    selectish_labels = (
        "country",
        "time zone",
        "timezone",
        "which of the following",
        "best describes you",
        "eligible to work",
        "authorized to work",
        "based and plan to work from the us",
        "sponsorship",
        "require sponsorship",
        "how did you hear",
        "source",
        "veteran",
        "disability",
        "gender",
        "race",
        "ethnicity",
    )
    return (
        field.get("tag") == "select"
        or field.get("role") == "combobox"
        or "select__input" in class_name
        or placeholder == "select..."
        or label.endswith("*") and "select" in placeholder
        or any(fragment in label for fragment in selectish_labels)
        or any(fragment in name for fragment in ("country", "timezone", "sponsorship", "eligib", "source"))
    )




def greenhouse_select_committed(locator, desired: str) -> bool:
    desired_norm = normalize_override_key(desired)
    try:
        current = normalize_space(locator.input_value(timeout=1000))
    except Exception:
        current = ""
    if current and desired_norm and desired_norm in normalize_override_key(current):
        return True
    try:
        context = normalize_space(locator.evaluate("""el => {
            const roots = [];
            let node = el;
            for (let i = 0; node && i < 4; i += 1) {
                roots.push(node);
                node = node.parentElement;
            }
            const chunks = [];
            for (const root of roots) {
                chunks.push(root.innerText || root.textContent || '');
                for (const btn of root.querySelectorAll('button, [role="button"], [aria-label]')) {
                    chunks.push(btn.getAttribute('aria-label') || btn.innerText || btn.textContent || '');
                }
            }
            return chunks.join(' ');
        }"""))
    except Exception:
        context = ""
    context_norm = normalize_override_key(context)
    if desired_norm and desired_norm in context_norm:
        return True
    return False


def greenhouse_value_variants(field: dict[str, Any], value: Any) -> list[str]:
    raw = normalize_space(str(value))
    if not raw:
        return []
    variants: list[str] = []

    def add(item: str) -> None:
        clean = normalize_space(item)
        if clean and clean not in variants:
            variants.append(clean)

    add(raw)
    label = normalize_space(str(field.get("label") or field.get("name") or "")).lower()
    normalized = normalize_override_key(raw)

    if "country" in label:
        if normalized in {"united states", "usa", "us", "united states of america"}:
            add("United States")
            add("United States of America")
            add("USA")
            add("US")
        if normalized in {"canada", "ca"}:
            add("Canada")

    if any(term in label for term in ("eligible to work", "authorized to work", "based and plan to work from the us", "sponsorship")):
        if normalized in {"yes", "true"}:
            add("Yes")
            add("Y")
            add("True")
        if normalized in {"no", "false"}:
            add("No")
            add("N")
            add("False")

    if any(term in label for term in ("which of the following", "best describes you")):
        if "individual contributor" in normalized:
            add("Individual Contributor")
            add("IC")

    if any(term in label for term in ("time zone", "timezone")):
        if "eastern" in normalized or "new york" in normalized:
            add("Eastern Time")
            add("Eastern Standard Time")
            add("US Eastern")
            add("United States / Eastern Time")

    return variants

def fill_greenhouse_select_like(page, locator, field: dict[str, Any], value: Any) -> bool:
    desired = normalize_space(str(value))
    label = normalize_space(str(field.get("label") or field.get("name") or "")).lower()
    if not desired:
        return False
    desired_variants = greenhouse_value_variants(field, value)
    log(f"ACTION: greenhouse select start label={label!r} desired={desired!r}")
    if "country" in label:
        if click_contains(page, ["Select country"], tags="button, [role='button'], div, span"):
            page.wait_for_timeout(300)
            if choose_text_option(page, desired_variants, allow_first_visible=True):
                log(f"ACTION: greenhouse select committed label={label!r} value={desired_variants[0]!r}")
                return True
    try:
        locator.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        locator.click(timeout=5000)
        page.wait_for_timeout(250)
    except Exception:
        pass
    try:
        locator.fill("", timeout=2000)
    except Exception:
        pass
    try:
        locator.type(desired_variants[0], delay=20, timeout=5000)
        page.wait_for_timeout(700)
    except Exception:
        try:
            locator.fill(desired_variants[0], timeout=5000)
            page.wait_for_timeout(700)
        except Exception:
            return False
    if choose_text_option(page, desired_variants, allow_first_visible=True):
        for candidate in desired_variants:
            if greenhouse_select_committed(locator, candidate):
                log(f"ACTION: greenhouse select committed label={label!r} value={candidate!r}")
                return True
    for candidate in desired_variants:
        try:
            locator.fill(candidate, timeout=4000)
            locator.press("Tab")
            page.wait_for_timeout(500)
            if greenhouse_select_committed(locator, candidate):
                log(f"ACTION: greenhouse select committed label={label!r} value={candidate!r}")
                return True
        except Exception:
            continue
    if any(term in label for term in ("location", "city")):
        try:
            locator.fill(desired_variants[0], timeout=4000)
            locator.press("Tab")
            page.wait_for_timeout(500)
            current = normalize_space(locator.input_value(timeout=1000))
            if current:
                log(f"ACTION: greenhouse select committed label={label!r} value={current!r}")
                return True
        except Exception:
            pass
        try:
            locator.evaluate("""(el, value) => {
                el.value = value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""", desired_variants[0])
            page.wait_for_timeout(300)
            current = normalize_space(locator.input_value(timeout=1000))
            if current:
                log(f"ACTION: greenhouse select committed label={label!r} value={current!r}")
                return True
        except Exception:
            pass
    for candidate in desired_variants:
        try:
            locator.fill(candidate, timeout=4000)
        except Exception:
            pass
        try:
            locator.press("ArrowDown")
            locator.press("Enter")
            page.wait_for_timeout(700)
            if greenhouse_select_committed(locator, candidate):
                log(f"ACTION: greenhouse select committed label={label!r} value={candidate!r}")
                return True
        except Exception:
            try:
                page.keyboard.press("ArrowDown")
                page.keyboard.press("Enter")
                page.wait_for_timeout(700)
                if greenhouse_select_committed(locator, candidate):
                    log(f"ACTION: greenhouse select committed label={label!r} value={candidate!r}")
                    return True
            except Exception:
                continue
    return any(greenhouse_select_committed(locator, candidate) for candidate in desired_variants)


def fill_field(page, field: dict[str, Any], value: Any) -> bool:
    if value is None:
        return False
    locator = locator_for(page, field["apgId"])
    tag = field.get("tag", "")
    role = field.get("role", "")
    field_type = field.get("type", "")
    try:
        if field_type == "file":
            path = str(value)
            if path and path != "N/A" and Path(path).exists():
                locator.set_input_files(path, timeout=10000)
                return True
            return False
        if field_type == "checkbox":
            desired = bool(value)
            if desired and not locator.is_checked():
                locator.check(timeout=5000)
                return True
            return False
        if tag == "select":
            desired = str(value)
            locator.select_option(label=desired, timeout=5000)
            return True
        if greenhouse_select_like(field):
            return fill_greenhouse_select_like(page, locator, field, value)
        if role == "combobox":
            locator.click(timeout=5000)
            locator.fill(str(value), timeout=5000)
            locator.press("ArrowDown")
            locator.press("Enter")
            return True
        locator.click(timeout=5000)
        if field_type in {"radio"}:
            return True
        locator.fill(str(value), timeout=5000)
        return True
    except Exception:
        return False


def fill_visible_password_inputs(page, password: str) -> int:
    changed = 0
    try:
        inputs = page.locator("input[type='password']")
        for idx in range(inputs.count()):
            field = inputs.nth(idx)
            try:
                box = field.bounding_box()
                if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
                    continue
                field.click(timeout=3000)
                field.fill(str(password), timeout=5000)
                changed += 1
            except Exception:
                continue
    except Exception:
        return changed
    if changed:
        page.wait_for_timeout(300)
    return changed


def fill_workday_experience_dates(page, fields: list[dict[str, Any]]) -> tuple[int, set[str]]:
    history = resume_work_history()
    if not history:
        return 0, set()

    changed = 0
    handled: set[str] = set()
    entry_order: list[str] = []

    for field in fields:
        field_name = str(field.get("name") or field.get("id") or "").strip()
        match = re.match(r"workExperience-(\d+)--(.+)", field_name)
        if not match:
            continue
        entry_id, suffix = match.groups()
        if entry_id not in entry_order:
            entry_order.append(entry_id)
        idx = entry_order.index(entry_id)
        if idx >= len(history):
            continue
        dates = history[idx]
        desired = None
        lowered = suffix.lower()
        if lowered == "currentlyworkhere":
            desired = bool(dates.get("current"))
        elif lowered == "startdate-datesectionmonth-input":
            desired = dates.get("start_month")
        elif lowered == "startdate-datesectionyear-input":
            desired = dates.get("start_year")
        elif lowered == "enddate-datesectionmonth-input" and not dates.get("current"):
            desired = dates.get("end_month")
        elif lowered == "enddate-datesectionyear-input" and not dates.get("current"):
            desired = dates.get("end_year")
        elif "roledescription" in lowered:
            handled.add(field.get("apgId", ""))
            continue

        if desired is None:
            continue
        handled.add(field.get("apgId", ""))
        if fill_field(page, field, desired):
            changed += 1

    return changed, {apg_id for apg_id in handled if apg_id}


def fill_visible_fields(
    page,
    profile: dict[str, Any],
    ctx: PromptContext,
    args: argparse.Namespace,
) -> tuple[int, list[dict[str, Any]]]:
    fields = annotate_fields(page)
    changed, handled_apg_ids = fill_workday_experience_dates(page, fields)
    qualitative_answers: list[dict[str, Any]] = []
    for field in fields:
        if field.get("apgId") in handled_apg_ids:
            continue
        value = resolve_field_value(field, profile, ctx, args)
        qualitative_entry = None
        if value is None and should_route_to_qualitative_llm(field):
            value, qualitative_entry = open_ended_answer(field, ctx, profile, args)
        preview = normalize_space(str(value))[:80] if value is not None else "None"
        log(
            f"ACTION: fill field label={normalize_space(str(field.get('label') or field.get('name') or ''))!r} "
            f"type={field.get('type') or field.get('tag') or ''} value={preview!r}"
        )
        if fill_field(page, field, value):
            changed += 1
            log("ACTION: fill field committed")
            if qualitative_entry:
                qualitative_answers.append(dict(qualitative_entry))
        elif value is not None:
            log("ACTION: fill field skipped_or_failed")
    if changed:
        page.wait_for_timeout(1000)
    return changed, qualitative_answers


def upload_matching_file_input(page, keywords: tuple[str, ...], path: str) -> bool:
    if not path or path == "N/A" or not Path(path).exists():
        return False
    try:
        inputs = page.locator("input[type='file']")
        count = inputs.count()
    except Exception:
        return False
    lowered_keywords = tuple(keyword.lower() for keyword in keywords if keyword)
    fallback = None
    for idx in range(count):
        locator = inputs.nth(idx)
        try:
            context = normalize_space(
                locator.evaluate(
                    """el => {
                        const root =
                          el.closest('label, .application, .field, .question, .form-group, section, div')
                          || el.parentElement
                          || el;
                        return [
                          el.getAttribute('name') || '',
                          el.getAttribute('id') || '',
                          el.getAttribute('aria-label') || '',
                          root.innerText || root.textContent || ''
                        ].join(' ');
                    }"""
                )
            ).lower()
        except Exception:
            context = ""
        if fallback is None:
            fallback = locator
        if lowered_keywords and not any(keyword in context for keyword in lowered_keywords):
            continue
        try:
            locator.set_input_files(path, timeout=10000)
            page.wait_for_timeout(1200)
            return True
        except Exception:
            continue
    if fallback is not None and count == 1:
        try:
            fallback.set_input_files(path, timeout=10000)
            page.wait_for_timeout(1200)
            return True
        except Exception:
            return False
    return False


def greenhouse_prepare_uploads(page, ctx: PromptContext) -> int:
    changed = 0
    if upload_matching_file_input(page, ("resume", "cv"), ctx.resume_pdf):
        log("ACTION: uploaded Greenhouse resume")
        changed += 1
    if ctx.cover_pdf and ctx.cover_pdf != "N/A":
        if upload_matching_file_input(page, ("cover",), ctx.cover_pdf):
            log("ACTION: uploaded Greenhouse cover letter")
            changed += 1
    return changed


def fill_greenhouse_security_code(page, profile: dict[str, Any], ctx: PromptContext) -> tuple[str | None, int]:
    try:
        first_input = page.locator("#security-input-0")
        if not first_input.count():
            return None, 0
    except Exception:
        return None, 0

    try:
        existing = first_input.first.input_value(timeout=1000).strip()
        if existing:
            return None, 0
    except Exception:
        pass

    email_address = applicant_value(profile, "email")
    code = fetch_greenhouse_security_code(email_address, ctx.company, timeout_s=120)
    if not code:
        log(f"ACTION: greenhouse verification code not found for {ctx.company}")
        return "RESULT:FAILED:verification_code_sent", 0

    filled = 0
    for idx, ch in enumerate(code[:8]):
        locator = page.locator(f"#security-input-{idx}")
        if not locator.count():
            continue
        try:
            locator.fill(ch, timeout=3000)
            filled += 1
        except Exception:
            continue
    if filled:
        log(f"ACTION: filled Greenhouse verification code for {ctx.company}")
        page.wait_for_timeout(500)
    return None, filled


def start_greenhouse_application(page) -> bool:
    accept_cookies(page)
    return (
        click_text(page, ["Apply for this job", "Apply Now", "Apply", "Submit Application"], tags="a, button, [role='button']")
        or click_contains(page, ["Apply for this job", "Apply Now", "Apply"], tags="a, button, [role='button']")
    )


def fill_text_by_label(page, pattern: str, value: str) -> bool:
    if value is None:
        return False
    locator = page.get_by_label(re.compile(pattern, re.I))
    if not locator.count():
        return False
    try:
        locator.first.click(timeout=3000)
        locator.first.fill(str(value), timeout=5000)
        page.wait_for_timeout(300)
        return True
    except Exception:
        return False


def visible_prompt_options(page, limit: int = 20) -> list[str]:
    selectors = ", ".join(
        [
            "[role='option']",
            "li[role='option']",
            "[data-automation-id='promptOption']",
            "[data-automation-id='menuItem']",
            "[data-automation-id='promptOptionText']",
        ]
    )
    try:
        values = page.locator(selectors).evaluate_all(
            """els => els.map(el => (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim()).filter(Boolean)"""
        )
    except Exception:
        return []

    cleaned: list[str] = []
    for value in values:
        text = normalize_space(value)
        if not text or text in cleaned:
            continue
        cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


def choose_text_option(page, desired_texts: list[str], *, allow_first_visible: bool = False) -> bool:
    option_snapshots: list[str] = []
    prompt_selectors = [
        "[role='option']",
        "li[role='option']",
        "[data-automation-id='promptOption']",
        "[data-automation-id='menuItem']",
    ]

    def current_options() -> list[str]:
        nonlocal option_snapshots
        option_snapshots = visible_prompt_options(page)
        return option_snapshots

    for _ in range(8):
        current_options()
        for text in desired_texts:
            for selector in prompt_selectors:
                locator = page.locator(selector).filter(has_text=re.compile(rf"^{re.escape(text)}$", re.I))
                if click_locator(locator, wait_ms=500):
                    return True
        if option_snapshots:
            break
        page.wait_for_timeout(250)

    for text in desired_texts:
        exact_patterns = [
            "[role='option']",
            "li[role='option']",
            "[data-automation-id='promptOption']",
            "[data-automation-id='menuItem']",
            "button",
            "[role='button']",
        ]
        for selector in exact_patterns:
            locator = page.locator(selector).filter(has_text=re.compile(rf"^{re.escape(text)}$", re.I))
            if click_locator(locator, wait_ms=500):
                return True

    tags = "[role='option'], li, [data-automation-id='promptOption'], button, [role='button'], div, span"
    locator = page.locator(tags)
    count = min(locator.count(), 120)
    for text in desired_texts:
        for idx in range(count):
            candidate = locator.nth(idx)
            try:
                label = normalize_space(
                    candidate.evaluate(
                        """el => (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim()"""
                    )
                )
            except Exception:
                continue
            if not label or len(label) > 80 or "\n" in label or not text_matches_option(label, text):
                continue
            try:
                candidate.click(timeout=3000)
                page.wait_for_timeout(500)
                return True
            except Exception:
                continue

    if allow_first_visible:
        ignored_fragments = (
            "search",
            "select one",
            "no matches",
            "no results",
            "loading",
        )
        for label in option_snapshots or current_options():
            lower = label.lower()
            if any(fragment in lower for fragment in ignored_fragments):
                continue
            for selector in prompt_selectors + ["button", "[role='button']"]:
                locator = page.locator(selector).filter(has_text=re.compile(rf"^{re.escape(label)}$", re.I))
                if click_locator(locator, wait_ms=500):
                    return True
    return False


def text_matches_option(candidate: str, desired: str) -> bool:
    def squash(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

    candidate_norm = squash(candidate)
    desired_norm = squash(desired)
    if not candidate_norm or not desired_norm:
        return False
    return (
        candidate_norm == desired_norm
        or desired_norm in candidate_norm
        or candidate_norm in desired_norm
    )


def workday_error_present(page, fragments: list[str]) -> bool:
    haystacks = visible_error_texts(page) + [button for button in visible_buttons(page) if button.lower().startswith("error")]
    if not haystacks:
        return False
    lowered = [normalize_space(item).lower() for item in haystacks if item]
    return any(fragment.lower() in item for fragment in fragments for item in lowered if fragment)


def workday_locator_context(locator) -> str:
    try:
        return normalize_space(
            locator.evaluate(
                """el => {
                    const root = el.closest(
                      '[data-automation-id="formField"], [data-automation-id="fieldSetContent"], [role="group"], fieldset, section, li'
                    ) || el.parentElement || el;
                    return (root.innerText || root.textContent || '').trim();
                }"""
            )
        )
    except Exception:
        return ""


def workday_selected_item_text(page) -> str:
    selectors = ", ".join(
        [
            "[data-automation-id='selectedItem']",
            "[data-automation-id='multiselectTag']",
            "[data-automation-id='promptOption']",
            "[data-automation-id='selectedValue']",
        ]
    )
    try:
        return normalize_space(
            " ".join(
                page.locator(selectors).evaluate_all(
                    """els => els.map(el => (el.innerText || el.textContent || '').trim()).filter(Boolean)"""
                )
            )
        )
    except Exception:
        return ""


def workday_selection_committed(
    page, locator, desired_options: list[str], error_fragments: list[str], *, had_error: bool = False
) -> bool:
    if had_error and error_fragments and not workday_error_present(page, error_fragments):
        return True
    try:
        value = normalize_space(locator.input_value(timeout=1000))
        if any(text_matches_option(value, option) for option in desired_options if option):
            return True
        if value and value.lower() not in {"search", "select one"}:
            return True
    except Exception:
        pass
    context_text = workday_locator_context(locator)
    if any(text_matches_option(context_text, option) for option in desired_options if option):
        return True
    selected_text = workday_selected_item_text(page)
    return any(text_matches_option(selected_text, option) for option in desired_options if option)


def workday_mark_active_control(page) -> str | None:
    try:
        return page.evaluate(
            """() => {
                document.querySelectorAll('[data-apg-active-control]').forEach(
                  el => el.removeAttribute('data-apg-active-control')
                );
                let el = document.activeElement;
                if (!el) return null;
                const target = el.matches('input, button, [role="combobox"], [role="textbox"], [aria-haspopup="listbox"]')
                  ? el
                  : el.closest('input, button, [role="combobox"], [role="textbox"], [aria-haspopup="listbox"]');
                if (!target) return null;
                const marker = `apg-active-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
                target.setAttribute('data-apg-active-control', marker);
                return marker;
            }"""
        )
    except Exception:
        return None


def workday_choose_from_control(
    page,
    locator,
    desired_options: list[str],
    error_fragments: list[str],
    *,
    had_error: bool = False,
    allow_first_visible: bool = False,
) -> bool:
    if not locator or not locator.count():
        return False
    for text in desired_options:
        try:
            locator.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        try:
            locator.click(timeout=3000)
            page.wait_for_timeout(300)
        except Exception:
            pass
        try:
            locator.fill("", timeout=2000)
        except Exception:
            pass
        try:
            locator.type(text, delay=20, timeout=5000)
            page.wait_for_timeout(900)
        except Exception:
            try:
                page.keyboard.type(text, delay=20)
                page.wait_for_timeout(900)
            except Exception:
                pass
        if choose_text_option(page, [text], allow_first_visible=allow_first_visible):
            if workday_selection_committed(page, locator, [text], error_fragments, had_error=had_error):
                return True
        try:
            locator.press("ArrowDown")
            locator.press("Enter")
            page.wait_for_timeout(700)
        except Exception:
            try:
                page.keyboard.press("ArrowDown")
                page.keyboard.press("Enter")
                page.wait_for_timeout(700)
            except Exception:
                pass
        if workday_selection_committed(page, locator, [text], error_fragments, had_error=had_error):
            return True
        try:
            locator.press("Tab")
            page.wait_for_timeout(400)
        except Exception:
            try:
                page.keyboard.press("Tab")
                page.wait_for_timeout(400)
            except Exception:
                pass
        if workday_selection_committed(page, locator, [text], error_fragments, had_error=had_error):
            return True
    return False


def workday_select_button_option(page, button_id: str, desired_options: list[str]) -> bool:
    button = page.locator(f"button[id='{button_id}']").first
    if not button.count():
        return False
    try:
        button.click(timeout=3000)
        page.wait_for_timeout(500)
    except Exception:
        return False

    for text in desired_options:
        option = page.get_by_role("option", name=re.compile(rf"^{re.escape(text)}$", re.I))
        if option.count():
            try:
                option.first.click(timeout=3000)
                page.wait_for_timeout(500)
                return True
            except Exception:
                continue

    if choose_text_option(page, desired_options):
        return True

    try:
        for text in desired_options:
            page.keyboard.type(text, delay=20)
            page.wait_for_timeout(200)
            option = page.get_by_role("option", name=re.compile(re.escape(text), re.I))
            if option.count():
                option.first.click(timeout=3000)
                page.wait_for_timeout(500)
                return True
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            button.click(timeout=3000)
            page.wait_for_timeout(400)
    except Exception:
        return False

    return False


def workday_searchable_select_by_label(
    page, pattern: str, desired_options: list[str], error_fragments: list[str] | None = None
) -> bool:
    locator = page.get_by_label(re.compile(pattern, re.I)).first
    if not locator.count():
        return False
    fragments = error_fragments or [pattern]
    return workday_choose_from_control(
        page,
        locator,
        desired_options,
        fragments,
        had_error=workday_error_present(page, fragments),
    )


def workday_searchable_select_by_field(
    page, label_fragments: list[str], desired_options: list[str], error_fragments: list[str] | None = None
) -> bool:
    lower_fragments = [fragment.lower() for fragment in label_fragments if fragment]
    if not lower_fragments:
        return False
    try:
        fields = annotate_fields(page)
    except Exception:
        return False

    for field in fields:
        label_text = " ".join(
            str(value or "") for value in (field.get("label"), field.get("name"), field.get("placeholder"))
        ).lower()
        if not any(fragment in label_text for fragment in lower_fragments):
            continue
        locator = locator_for(page, field["apgId"])
        try:
            locator.click(timeout=3000)
            page.wait_for_timeout(300)
        except Exception:
            continue

        fragments = error_fragments or lower_fragments
        if workday_choose_from_control(
            page,
            locator,
            desired_options,
            fragments,
            had_error=workday_error_present(page, fragments),
        ):
            return True
    return False


def workday_source_field_locator(page):
    try:
        fields = annotate_fields(page)
    except Exception:
        return None

    for field in fields:
        label_text = " ".join(
            str(value or "") for value in (field.get("label"), field.get("name"), field.get("placeholder"))
        ).lower()
        if "how did you hear about us" in label_text or label_text.startswith("source"):
            return locator_for(page, field["apgId"])
    locator = page.get_by_label(re.compile(r"How Did You Hear About Us|Source", re.I)).first
    return locator if locator.count() else None


def workday_source_field_committed(page, desired_options: list[str]) -> bool:
    container = page.locator("[data-automation-id='formField-source']").first
    if not container.count():
        return False
    try:
        context = normalize_space(container.inner_text(timeout=1500))
    except Exception:
        return False
    lower = context.lower()
    if "0 items selected" not in lower and "item selected" in lower:
        return True
    return any(text_matches_option(context, option) for option in desired_options if option)


def workday_fill_source_field(page, desired_options: list[str]) -> bool:
    locator = workday_source_field_locator(page)
    if locator is None or not locator.count():
        return False

    fragments = ["how did you hear about us", "source"]
    container = page.locator("[data-automation-id='formField-source']").first
    if container.count():
        prompt_button = container.locator(
            "[data-automation-id='promptSearchButton'], [data-automation-id='promptIcon']"
        ).first
        if prompt_button.count():
            try:
                prompt_button.scroll_into_view_if_needed(timeout=3000)
                prompt_button.click(timeout=3000, force=True)
                page.wait_for_timeout(700)
            except Exception:
                pass
            options = visible_prompt_options(page, limit=12)
            log(f"ACTION: Workday source options={options}")
            for option_text in desired_options:
                option = page.locator("[data-automation-id='menuItem'], [role='option']").filter(
                    has_text=re.compile(rf"^{re.escape(option_text)}$", re.I)
                ).first
                if option.count():
                    try:
                        option.click(timeout=3000, force=True)
                        page.wait_for_timeout(700)
                    except Exception:
                        continue
                    if workday_source_field_committed(page, [option_text]):
                        return True
            if choose_text_option(page, desired_options, allow_first_visible=True):
                page.wait_for_timeout(600)
                if workday_source_field_committed(page, desired_options):
                    return True

    if workday_choose_from_control(
        page,
        locator,
        desired_options,
        fragments,
        had_error=workday_error_present(page, fragments),
        allow_first_visible=True,
    ):
        return True

    try:
        locator.click(timeout=3000)
        page.wait_for_timeout(600)
        options = visible_prompt_options(page, limit=12)
        if options:
            log(f"ACTION: Workday source options={options}")
            if choose_text_option(page, options[:3], allow_first_visible=True):
                page.wait_for_timeout(500)
                return workday_source_field_committed(page, options[:3]) or not workday_error_present(page, fragments)
    except Exception:
        return False
    return False


def workday_fill_primary_questionnaire(
    page,
    question_answers: list[tuple[list[str], list[str]]],
    learned_answers: list[dict[str, Any]] | None = None,
) -> int:
    buttons = page.locator("button[id^='primaryQuestionnaire--']")
    button_count = buttons.count()
    if not button_count:
        return 0
    changed = 0
    for idx in range(button_count):
        button = buttons.nth(idx)
        try:
            current = normalize_space(button.inner_text(timeout=1000))
            context_text = normalize_space(
                button.evaluate(
                    """el => (el.closest('fieldset, section, div.css-gvoll6, div.css-1obf64m')?.innerText || '').trim()"""
                )
            ).lower()
        except Exception:
            current = ""
            context_text = ""
        desired_options = None
        for fragments, options in question_answers:
            if any(fragment in context_text for fragment in fragments):
                desired_options = options
                break
        if not desired_options:
            continue
        if any(text_matches_option(current, option) for option in desired_options):
            if learned_answers is not None:
                remember_learned_answer(learned_answers, fragments, desired_options)
            continue
        for _ in range(2):
            try:
                button.scroll_into_view_if_needed(timeout=3000)
                button.click(timeout=3000)
                page.wait_for_timeout(400)
            except Exception:
                continue
            exact_option_clicked = False
            options = page.locator("li[role='option'], [role='option'], [data-automation-id='menuItem']")
            for option_text in desired_options:
                option = options.filter(
                    has_text=re.compile(rf"^{re.escape(option_text)}$", re.I)
                )
                if click_locator(option, wait_ms=400):
                    exact_option_clicked = True
                    break
            if not exact_option_clicked and choose_text_option(page, desired_options):
                page.wait_for_timeout(400)
            elif not exact_option_clicked:
                for text in desired_options:
                    option = page.get_by_role("option", name=re.compile(rf"^{re.escape(text)}$", re.I))
                    if option.count() and click_locator(option.first, wait_ms=400):
                        break
            try:
                current = normalize_space(button.inner_text(timeout=1000))
            except Exception:
                current = ""
            if any(text_matches_option(current, option) for option in desired_options):
                changed += 1
                if learned_answers is not None:
                    remember_learned_answer(learned_answers, fragments, desired_options)
                break
    return changed


def workday_check_checkbox_label(page, label_pattern: str) -> bool:
    locator = page.get_by_label(re.compile(label_pattern, re.I))
    if not locator.count():
        return False
    try:
        target = locator.first
        if not target.is_checked():
            target.check(timeout=3000)
            page.wait_for_timeout(300)
        return target.is_checked()
    except Exception:
        return False


def workday_fill_self_identify(page, profile: dict[str, Any]) -> int:
    body = page_text(page).lower()
    if "voluntary self-identification of disability" not in body and "self identify" not in body:
        return 0

    changed = 0
    today = datetime.now()
    for label, value in (
        (r"^Month$", today.strftime("%m")),
        (r"^Day$", today.strftime("%d")),
        (r"^Year$", today.strftime("%Y")),
    ):
        if fill_text_by_label(page, label, value):
            changed += 1

    disability_pref = applicant_value(profile, "disability").lower()
    disability_label = None
    if disability_pref.startswith("yes"):
        disability_label = r"^Yes, I have a disability, or have had one in the past"
    elif disability_pref.startswith("no"):
        disability_label = r"^No, I do not have a disability and have not had one in the past"
    elif "decline" in disability_pref or "do not want" in disability_pref:
        disability_label = r"^I do not want to answer"
    if disability_label and workday_check_checkbox_label(page, disability_label):
        changed += 1

    return changed


def preferred_skills(profile: dict[str, Any]) -> list[str]:
    skills = profile.get("skills_boundary", {})
    ordered = []
    for key in ("programming_languages", "frameworks", "tools"):
        for item in skills.get(key, []):
            if item and item not in ordered:
                ordered.append(item)
    defaults = ["Python", "PyTorch", "FastAPI", "AWS", "Docker"]
    for item in defaults:
        if item not in ordered:
            ordered.append(item)
    return ordered[:8]


def workday_add_skill(page, skill: str) -> bool:
    field = page.locator("input#skills--skills").first
    if not field.count():
        field = page.get_by_label(re.compile(r"Type to Add Skills", re.I)).first
    if not field.count():
        field = page.locator("input[placeholder*='Search']").first
    if not field.count():
        return False
    try:
        field.click(timeout=3000)
        field.fill(skill, timeout=3000)
        page.wait_for_timeout(700)
        option = page.get_by_role("option", name=re.compile(re.escape(skill), re.I))
        if option.count():
            option.first.click(timeout=3000)
        else:
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
        page.wait_for_timeout(700)
        selected = page.locator("[data-automation-id='selectedItem'], [data-automation-id='multiselectTag']").count()
        if selected:
            return True
        body = page_text(page).lower()
        return "1 item selected" in body or skill.lower() in body
    except Exception:
        return False


def workday_upload_autofill_resume(page, ctx: PromptContext) -> bool:
    if not ctx.resume_pdf or ctx.resume_pdf == "N/A":
        return False
    resume_path = Path(ctx.resume_pdf)
    if not resume_path.exists():
        return False
    locator = page.locator("input[type='file']").first
    if not locator.count():
        return False
    try:
        locator.set_input_files(str(resume_path), timeout=10000)
        page.wait_for_timeout(3000)
        return True
    except Exception:
        return False


def workday_pick_select_one(page, error_label: str, desired_options: list[str]) -> bool:
    click_contains(page, [f"Error-{error_label}"], tags="button")
    page.wait_for_timeout(300)
    active_marker = workday_mark_active_control(page)
    if active_marker:
        active_control = page.locator(f'[data-apg-active-control="{active_marker}"]').first
        if workday_choose_from_control(
            page,
            active_control,
            desired_options,
            [error_label],
            had_error=workday_error_present(page, [error_label]),
        ):
            return True
    container = None
    pattern = re.compile(re.escape(error_label[:80]), re.I)
    candidates = page.locator(
        "[data-automation-id^='formField-'], fieldset, [role='group'], section, li, div"
    ).filter(has_text=pattern)
    for idx in range(min(candidates.count(), 20)):
        candidate = candidates.nth(idx)
        button = candidate.locator("button").first
        if button.count():
            container = candidate
            break

    if container is not None:
        try:
            container_text = normalize_space(container.inner_text(timeout=2000))
            if any(
                re.search(rf"(?:^|\\b){re.escape(option)}(?:\\b|$)", container_text, re.I)
                and "select one" not in container_text.lower()
                for option in desired_options
            ):
                return True
        except Exception:
            pass

        buttons = container.locator("button")
        opened = False
        for idx in range(min(buttons.count(), 4)):
            if click_locator(buttons.nth(idx), wait_ms=500):
                opened = True
                break
        if not opened:
            inputs = container.locator("input, [role='combobox'], [role='textbox']")
            for idx in range(min(inputs.count(), 4)):
                candidate = inputs.nth(idx)
                if workday_choose_from_control(
                    page,
                    candidate,
                    desired_options,
                    [error_label],
                    had_error=workday_error_present(page, [error_label]),
                ):
                    return True
        if not opened:
            return False
    else:
        select_buttons = page.locator("button[id^='primaryQuestionnaire--'], button, [role='button'], [role='combobox'], div").filter(
            has_text=re.compile(r"^Select One$", re.I)
        )
        opened = False
        for idx in range(min(select_buttons.count(), 6)):
            try:
                button = select_buttons.nth(idx)
                box = button.bounding_box()
                if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                    button.click(timeout=3000)
                    page.wait_for_timeout(500)
                    opened = True
                    break
            except Exception:
                continue
        if not opened:
            return False
    page.wait_for_timeout(500)
    options = page.locator("li[role='option'], [role='option'], [data-automation-id='menuItem']")
    for text in desired_options:
        option = options.filter(has_text=re.compile(rf"^{re.escape(text)}$", re.I)).first
        if click_locator(option, wait_ms=300):
            page.wait_for_timeout(300)
            if not workday_error_present(page, [error_label]):
                return True
    if choose_text_option(page, desired_options):
        page.wait_for_timeout(300)
        if not workday_error_present(page, [error_label]):
            return True
    for text in desired_options:
        try:
            page.keyboard.type(text, delay=25)
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)
            if not workday_error_present(page, [error_label]):
                return True
        except Exception:
            continue
    return False


def workday_answer_yes_no(page, question_text: str, answer: str) -> bool:
    click_contains(page, [f"Error-{question_text}"], tags="button")
    page.wait_for_timeout(300)
    return click_text(page, [answer], tags="label, button, [role='button'], div, span")


def workday_profile_requirements(page, profile: dict[str, Any]) -> str | None:
    text = page_text(page)
    if "Address Line 1" in text and not applicant_value(profile, "address"):
        return "missing_profile_address"
    return None


def workday_select_one_answers(profile: dict[str, Any], ctx: PromptContext) -> list[tuple[list[str], list[str]]]:
    yes_options = ["Yes", "Sí", "Si"]
    no_options = ["No"]
    answers = [
        (
            [
                "legally authorized to work",
                "authorized to work in the country",
                "authorized to legally work",
                "legally work in the job location",
                "legalmente autorizado para trabajar",
            ],
            yes_options if applicant_value(profile, "authorized") == "Yes" else no_options,
        ),
        (
            [
                "require sponsorship",
                "require visa sponsorship",
                "sponsorship to legally work",
                "sponsorship to work in the job location",
                "require sponsorship for employment visa status",
                "requiere ahora",
                "patrocinio para obtener el estado de visa",
            ],
            yes_options if applicant_value(profile, "sponsorship") == "Yes" else no_options,
        ),
        (
            [
                "restrictive covenants",
                "non compete",
                "confidentiality agreements",
                "contractual obligations",
                "acuerdos restrictivos",
            ],
            no_options,
        ),
        (
            [
                "current contractor at any thomson reuters location",
                "currently working for",
                "independent, vendor, or temporary worker",
                "actualmente un contractor",
            ],
            no_options,
        ),
        (
            [
                "previously worked for thomson reuters",
                "have previously worked for thomson reuters",
                "have you worked for",
                "worked for netflix",
                "worked for any of",
                "subsidiaries in the past",
            ],
            no_options,
        ),
    ]
    answers.extend(workday_override_answers(ctx))
    return answers


def fill_workday_overrides(
    page,
    profile: dict[str, Any],
    ctx: PromptContext,
) -> tuple[int, str | None, list[dict[str, Any]]]:
    blocker = workday_profile_requirements(page, profile)
    if blocker:
        return 0, blocker, []

    changed = 0
    learned_answers: list[dict[str, Any]] = []
    if fill_text_by_label(page, r"Address Line 1", applicant_value(profile, "address")):
        changed += 1
    if fill_text_by_label(page, r"City", applicant_value(profile, "city")):
        changed += 1
    if fill_text_by_label(page, r"Postal Code", applicant_value(profile, "postal_code")):
        changed += 1
    if fill_text_by_label(page, r"Country Phone Code", applicant_value(profile, "phone_country_code")):
        changed += 1
    if fill_text_by_label(page, r"Phone Number", applicant_value(profile, "phone_local")):
        changed += 1
    if fill_text_by_label(page, r"Phone Extension", ""):
        changed += 1

    source_options = [
        "Job Boards",
        "Applied",
        "LinkedIn",
        "Indeed",
        "Glassdoor",
        "Sourced",
        "Referred",
        "Employee Referral",
        "Other",
    ]
    source_answer = lookup_question_override(ctx, "how did you hear about us", source_options) or applicant_value(profile, "heard_about")
    if workday_fill_source_field(page, source_options):
        changed += 1
        remember_learned_answer(learned_answers, ["how did you hear about us", "source"], [source_answer])
    elif workday_searchable_select_by_field(
        page,
        ["how did you hear about us", "source"],
        source_options,
        error_fragments=["how did you hear about us", "source"],
    ):
        changed += 1
        remember_learned_answer(learned_answers, ["how did you hear about us", "source"], [source_answer])
    elif workday_searchable_select_by_label(
        page,
        r"How Did You Hear About Us",
        source_options,
        error_fragments=["how did you hear about us", "source"],
    ):
        changed += 1
        remember_learned_answer(learned_answers, ["how did you hear about us", "source"], [source_answer])
    elif workday_select_button_option(page, "source--source", source_options):
        changed += 1
        remember_learned_answer(learned_answers, ["how did you hear about us", "source"], [source_answer])
    if workday_select_button_option(page, "address--countryRegion", [applicant_value(profile, "state")]):
        changed += 1
    if workday_select_button_option(page, "phoneNumber--phoneType", ["Mobile", "Telephone"]):
        changed += 1
    if workday_answer_yes_no(page, "Please indicate if you have previously worked for Thomson Reuters", "No"):
        changed += 1

    body = page_text(page)
    if "Type to Add Skills" in body:
        for skill in preferred_skills(profile):
            if workday_add_skill(page, skill):
                changed += 1
                break

    select_one_answers = workday_select_one_answers(profile, ctx)
    changed += workday_fill_primary_questionnaire(page, select_one_answers, learned_answers)
    for fragments, desired_options in select_one_answers:
        if any(fragment in body.lower() for fragment in fragments):
            for fragment in fragments:
                if workday_pick_select_one(page, fragment, desired_options):
                    changed += 1
                    remember_learned_answer(learned_answers, fragments, desired_options)
                    break

    yes_no_answers = [
        ("Please indicate if you have previously worked for Thomson Reuters", "No"),
    ]
    yes_no_answers.extend(workday_yes_no_override_answers(ctx))
    for question_text, answer in yes_no_answers:
        if question_text.lower() in body.lower():
            if workday_answer_yes_no(page, question_text, answer):
                changed += 1
                remember_learned_answer(learned_answers, [question_text], [answer])

    changed += workday_fill_self_identify(page, profile)

    return changed, None, learned_answers


def is_workday_signin_screen(page, text: str, fields: list[dict[str, Any]] | None = None) -> bool:
    fields = fields or annotate_fields(page)
    return (
        "sign in" in text.lower()
        and any(field.get("autocomplete") == "current-password" for field in fields)
    )


def auth_fields_snapshot(page, *, attempts: int = 5, wait_ms: int = 800) -> tuple[str, list[dict[str, Any]]]:
    last_error: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            text = page_text(page)
            fields = annotate_fields(page)
            return text, fields
        except Exception as exc:
            last_error = exc
            try:
                page.wait_for_timeout(wait_ms)
            except Exception:
                time.sleep(wait_ms / 1000)
    if last_error is not None:
        raise last_error
    return "", []


def auth_field(fields: list[dict[str, Any]], autocomplete: str) -> dict[str, Any] | None:
    return next((field for field in fields if field.get("autocomplete") == autocomplete), None)


def switch_workday_to_sign_in(page) -> bool:
    actions = [
        lambda: click_button_exact(page, "Sign In", use_last=True),
        lambda: click_exact_any(page, "Sign In", use_last=True),
        lambda: click_role_button(page, r"^sign in$"),
        lambda: click_contains(page, ["Sign In"], tags="button, a, [role='button'], div"),
    ]
    for action in actions:
        try:
            if action():
                page.wait_for_timeout(1200)
                text, fields = auth_fields_snapshot(page, attempts=3, wait_ms=600)
                if is_workday_signin_screen(page, text, fields):
                    return True
                if "sign in with email" in text.lower():
                    if click_role_button(page, r"sign in with email"):
                        page.wait_for_timeout(1200)
                        text, fields = auth_fields_snapshot(page, attempts=3, wait_ms=600)
                        if is_workday_signin_screen(page, text, fields):
                            return True
        except Exception:
            continue
    return False


def trigger_workday_password_reset(page, host: str, email: str) -> str:
    opened = click_exact_any(page, "Forgot your password?", use_last=True)
    if not opened:
        try:
            button = page.locator("button[data-automation-id='forgotPasswordLink']").first
            if button.count():
                button.click(timeout=3000, force=True)
                page.wait_for_timeout(1200)
                opened = True
        except Exception:
            opened = False
    if not opened:
        return "failed"

    page.wait_for_timeout(1500)
    fields = annotate_fields(page)
    email_field = next(
        (
            field for field in fields
            if field.get("autocomplete") == "email"
            or "email" in " ".join(
                str(value or "") for value in (field.get("label"), field.get("name"), field.get("placeholder"))
            ).lower()
        ),
        None,
    )
    if email_field:
        fill_field(page, email_field, email)

    submitted = click_button_exact(page, "Submit", submit_only=True)
    if not submitted:
        submitted = click_exact_any(page, "Submit", use_last=True)
    if not submitted:
        submitted = click_exact_any(page, "Reset Password", use_last=True)
    if not submitted:
        submitted = click_exact_any(page, "Send Reset Email", use_last=True)
    if not submitted:
        try:
            button = page.locator(
                "button[data-automation-id*='forgot'], button[data-automation-id*='reset']"
            ).first
            if button.count():
                button.click(timeout=3000, force=True)
                page.wait_for_timeout(1000)
                submitted = True
        except Exception:
            submitted = False
    if not submitted:
        try:
            form = page.locator("form").first
            if form.count():
                form.evaluate("(node) => node.requestSubmit ? node.requestSubmit() : node.submit()")
                page.wait_for_timeout(1000)
                submitted = True
        except Exception:
            submitted = False
    if not submitted:
        return "failed"

    page.wait_for_timeout(3000)
    text = page_text(page)
    success_patterns = (
        "check your email",
        "sent you an email",
        "password reset",
        "reset link",
        "if an account exists",
        "email has been sent",
    )
    if contains_any(text, success_patterns):
        clear_account_password(host, email, status="reset_requested")
        log(f"ACTION: password reset requested for {host}")
        mailbox_result = maybe_complete_workday_mailbox_flow(page, host, email, "reset")
        if mailbox_result == "reset_completed":
            return "reset_completed"
        if mailbox_result:
            return mailbox_result
        return "reset_requested"
    log(
        f"ACTION: password reset did not confirm errors={visible_error_texts(page)} "
        f"text={normalize_space(text)[:240]!r}"
    )
    return "failed"


def submit_workday_create_account(page, password_fields: list[dict[str, Any]]) -> bool:
    submitted = click_button_exact(page, "Create Account", submit_only=True)
    if not submitted:
        submitted = click_named_control(page, "Create Account", submit_only=True)
    if not submitted:
        submitted = click_role_button(page, r"^create account$")
    if not submitted:
        submitted = click_exact_any(page, "Create Account", use_last=True)
    if not submitted:
        submitted = click_named_control(page, "Create Account", use_last=True)
    if not submitted:
        try:
            button = page.locator(
                "button[data-automation-id*='createAccount'], button[data-automation-id*='create-account']"
            ).first
            if button.count():
                button.click(timeout=3000, force=True)
                page.wait_for_timeout(1000)
                submitted = True
        except Exception:
            submitted = False
    if not submitted and password_fields:
        try:
            field = locator_for(page, password_fields[-1]["apgId"])
            field.click(timeout=3000)
            field.press("Enter", timeout=3000)
            page.wait_for_timeout(1000)
            submitted = True
        except Exception:
            submitted = False
    if not submitted:
        try:
            page.keyboard.press("Enter")
            page.wait_for_timeout(1000)
            submitted = True
        except Exception:
            submitted = False
    if not submitted:
        try:
            form = page.locator("form").first
            if form.count():
                form.evaluate("(node) => node.requestSubmit ? node.requestSubmit() : node.submit()")
                page.wait_for_timeout(1000)
                submitted = True
        except Exception:
            submitted = False
    return submitted


def workday_account_gate(page, profile: dict[str, Any], ctx: PromptContext) -> str:
    text = page_text(page)
    if "Create Account/Sign In" not in text and "Create Account" not in text and "Sign In" not in text:
        return "skip"

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_function(
            """() => {
                const text = document.body && document.body.innerText ? document.body.innerText : '';
                const hasEmailInput = !!document.querySelector('input[autocomplete="email"]');
                const hasPasswordInput = !!document.querySelector('input[autocomplete="current-password"], input[autocomplete="new-password"]');
                return text.includes('Sign in with email')
                    || (hasEmailInput && hasPasswordInput);
            }""",
            timeout=20000,
        )
        page.wait_for_timeout(1000)
    except Exception:
        pass

    email = applicant_value(profile, "email")
    host = urlparse(page.url).hostname or "workday"
    entry = get_account_entry(host, email)
    known_password = get_known_account_password(host, email)
    text, fields = auth_fields_snapshot(page, attempts=4, wait_ms=800)
    log(f"ACTION: auth buttons={visible_buttons(page)[:20]}")
    log(f"ACTION: auth text={text[:240]!r}")

    email_field = auth_field(fields, "email")
    current_password = auth_field(fields, "current-password")
    new_password = auth_field(fields, "new-password")

    if not (email_field and (current_password or new_password)):
        opened_email_sign_in = click_role_button(page, r"sign in with email")
        if not opened_email_sign_in:
            opened_email_sign_in = click_exact_any(page, "Sign in with email", use_last=True)
        if not opened_email_sign_in:
            opened_email_sign_in = click_contains(
                page,
                ["Sign in with email"],
                tags="button, a, [role='button'], div, span",
            )
        if not opened_email_sign_in and click_button_exact(page, "Sign In", use_last=True):
            log("ACTION: opened generic sign-in")
            page.wait_for_timeout(1500)
            text, fields = auth_fields_snapshot(page, attempts=3, wait_ms=600)
            opened_email_sign_in = click_role_button(page, r"sign in with email")
        if opened_email_sign_in:
            log("ACTION: opened email sign-in")
            text, fields = auth_fields_snapshot(page, attempts=3, wait_ms=600)
        email_field = auth_field(fields, "email")
        current_password = auth_field(fields, "current-password")
        new_password = auth_field(fields, "new-password")

    log(
        "ACTION: auth fields "
        f"email={bool(email_field)} current_password={bool(current_password)} new_password={bool(new_password)} "
        f"known_password={bool(known_password)} account_status={account_status(entry) or 'missing'}"
    )

    if known_password and email_field and new_password and not current_password:
        if switch_workday_to_sign_in(page):
            text, fields = auth_fields_snapshot(page, attempts=4, wait_ms=800)
            email_field = auth_field(fields, "email")
            current_password = auth_field(fields, "current-password")
            new_password = auth_field(fields, "new-password")
            log(
                "ACTION: switched create-account view to sign-in "
                f"current_password={bool(current_password)} new_password={bool(new_password)}"
            )

    if current_password and email_field and known_password:
        fill_field(page, email_field, email)
        fill_field(page, current_password, known_password)
        submitted = click_button_exact(page, "Sign In", submit_only=True)
        if not submitted:
            submitted = click_button_exact(page, "Sign In", use_last=True)
        if not submitted:
            submitted = click_exact_any(page, "Sign In", use_last=True)
        if not submitted:
            try:
                button = page.locator("button[data-automation-id='signInSubmitButton']").first
                if button.count():
                    button.click(timeout=3000, force=True)
                    page.wait_for_timeout(1000)
                    submitted = True
            except Exception:
                submitted = False
        if not submitted:
            submitted = click_button_exact(page, "Submit", submit_only=True)
        if not submitted:
            submitted = click_role_button(page, r"^submit$")
        if not submitted:
            try:
                locator_for(page, current_password["apgId"]).press("Enter", timeout=3000)
                page.wait_for_timeout(1000)
                submitted = True
            except Exception:
                submitted = False
        if not submitted:
            try:
                form = page.locator("form").first
                if form.count():
                    form.evaluate("(node) => node.requestSubmit ? node.requestSubmit() : node.submit()")
                    page.wait_for_timeout(1000)
                    submitted = True
            except Exception:
                submitted = False
        if submitted:
            log("ACTION: attempted Workday sign-in")
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            new_text, new_fields = auth_fields_snapshot(page, attempts=6, wait_ms=900)
            auth_error = normalize_space(" ".join(visible_error_texts(page)))
            if not is_workday_signin_screen(page, new_text, new_fields):
                update_account_entry(host, email, status="ready")
                return "handled"
            log(
                f"ACTION: sign-in rejected errors={visible_error_texts(page)} "
                f"text={normalize_space(new_text)[:240]!r}"
            )
            if auth_error:
                clear_account_password(host, email, status="sign_in_rejected")
            reset_result = trigger_workday_password_reset(page, host, email)
            if reset_result == "reset_completed":
                return "handled"
            if reset_result in {"reset_requested", "reset_link_opened"}:
                return "RESULT:FAILED:password_reset_sent"
            return "RESULT:LOGIN_ISSUE"

    create_account_ready = False
    if not known_password and new_password and email_field:
        create_account_ready = True
        log("ACTION: defaulting to current create-account form")
    elif not known_password and click_role_button(page, r"^create account$"):
        create_account_ready = True
        log("ACTION: moved to Workday create-account form")

    if create_account_ready:
        page.wait_for_timeout(1500)
        pre_submit_signature = workday_signature(page, text)
        fields = annotate_fields(page)
        password = ensure_account_password(host, email)
        email_field = auth_field(fields, "email")
        password_fields = [f for f in fields if f.get("autocomplete") == "new-password"]
        consent_boxes = [f for f in fields if f.get("type") == "checkbox"]
        log(
            "ACTION: create-account fields "
            f"email={bool(email_field)} passwords={len(password_fields)} consent={len(consent_boxes)}"
        )
        if email_field:
            fill_field(page, email_field, email)
        if password_fields:
            fill_field(page, password_fields[0], password)
        if len(password_fields) > 1:
            fill_field(page, password_fields[1], password)
        fill_visible_password_inputs(page, password)
        for box in consent_boxes:
            fill_field(page, box, True)
        submitted = submit_workday_create_account(page, password_fields)
        if submitted:
            log("ACTION: submitted Workday create-account form")
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(2500)
            new_text, new_fields = auth_fields_snapshot(page, attempts=6, wait_ms=900)
            log(
                f"ACTION: post-create-account errors={visible_error_texts(page)} "
                f"text={normalize_space(new_text)[:240]!r}"
            )
            retry_needed = any("password" in err.lower() for err in visible_error_texts(page))
            new_current_password = auth_field(new_fields, "current-password")
            new_new_password = auth_field(new_fields, "new-password")
            post_signature = workday_signature(page, new_text)
            if not retry_needed and new_new_password and not new_current_password and post_signature == pre_submit_signature:
                retry_needed = True
            if retry_needed:
                log("ACTION: retrying create-account submission after no-progress/password error")
                if email_field:
                    fill_field(page, email_field, email)
                fields = annotate_fields(page)
                retry_password_fields = [f for f in fields if f.get("autocomplete") == "new-password"]
                if retry_password_fields:
                    fill_field(page, retry_password_fields[0], password)
                if len(retry_password_fields) > 1:
                    fill_field(page, retry_password_fields[1], password)
                fill_visible_password_inputs(page, password)
                retry_consent_boxes = [f for f in fields if f.get("type") == "checkbox"]
                for box in retry_consent_boxes:
                    fill_field(page, box, True)
                if submit_workday_create_account(page, retry_password_fields):
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    page.wait_for_timeout(2500)
                    new_text, new_fields = auth_fields_snapshot(page, attempts=6, wait_ms=900)
                    log(
                        f"ACTION: post-retry-create-account errors={visible_error_texts(page)} "
                        f"text={normalize_space(new_text)[:240]!r}"
                    )
            if contains_any(
                new_text,
                (
                    "check your email",
                    "verify your email",
                    "activation link",
                    "confirm your email",
                ),
            ):
                update_account_entry(host, email, password=password, status="verification_required")
                mailbox_result = maybe_complete_workday_mailbox_flow(page, host, email, "verify")
                if mailbox_result == "verification_completed":
                    return "handled"
                return "RESULT:FAILED:account_confirmation_required"
            if is_workday_signin_screen(page, new_text, new_fields):
                update_account_entry(host, email, password=password, status="created")
                return "handled"
            new_current_password = auth_field(new_fields, "current-password")
            new_new_password = auth_field(new_fields, "new-password")
            if new_new_password and not new_current_password:
                if switch_workday_to_sign_in(page):
                    switch_text, switch_fields = auth_fields_snapshot(page, attempts=4, wait_ms=800)
                    if is_workday_signin_screen(page, switch_text, switch_fields):
                        update_account_entry(host, email, password=password, status="created")
                        return "handled"
                post_signature = workday_signature(page, new_text)
                if post_signature == pre_submit_signature:
                    log("ACTION: create-account submission did not advance page state")
            update_account_entry(host, email, password=password, status="created")
            return "handled"
        log(
            "ACTION: create-account submit unavailable "
            f"controls={visible_action_controls(page, limit=20)} "
            f"fields={summarize_visible_fields(page, limit=10)}"
        )

    log("ACTION: falling back to password reset as last resort")
    reset_result = trigger_workday_password_reset(page, host, email)
    if reset_result == "reset_completed":
        return "handled"
    if reset_result in {"reset_requested", "reset_link_opened"}:
        return "RESULT:FAILED:password_reset_sent"
    return "RESULT:LOGIN_ISSUE"


def click_workday_nav_button(page, selectors: list[str], texts: list[str]) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if click_locator(locator, use_last=True, wait_ms=1500):
                return True
        except Exception:
            continue

    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(500)
    except Exception:
        pass

    if click_contains(page, texts):
        return True
    for text in texts:
        if click_button_exact(page, text, use_last=True):
            return True
    return False


def click_primary_navigation(page, dry_run: bool) -> tuple[str | None, bool]:
    text = page_text(page)
    if is_success_page(text):
        return "RESULT:APPLIED", False
    if is_expired_page(text):
        return "RESULT:EXPIRED", False

    submit_texts = ["Submit", "Review and Submit", "Submit Application", "Apply"]
    next_texts = ["Save and Continue", "Continue", "Next", "Review"]
    workday_next_selectors = [
        "button[data-automation-id='pageFooterNextButton']",
        "button[data-automation-id='bottom-navigation-next-button']",
        "button[data-automation-id='saveAndContinue']",
    ]
    workday_submit_selectors = [
        "button[data-automation-id='pageFooterSubmitButton']",
        "button[data-automation-id='bottom-navigation-submit-button']",
        "button[data-automation-id='reviewAndSubmit']",
    ]

    if dry_run:
        for text_name in submit_texts:
            locator = page.locator("button, a, [role='button']").filter(
                has_text=re.compile(rf"^{re.escape(text_name)}$", re.I)
            )
            if locator.count():
                return "RESULT:APPLIED dry_run_local_agent", False

    if click_workday_nav_button(page, workday_next_selectors, next_texts):
        return None, True
    if not dry_run and click_workday_nav_button(page, workday_submit_selectors, submit_texts):
        page.wait_for_timeout(5000)
        return None, True
    return None, False


def click_greenhouse_navigation(page, dry_run: bool) -> tuple[str | None, bool]:
    text = page_text(page)
    if is_success_page(text) or "your application has been submitted" in text.lower():
        return "RESULT:APPLIED", False
    if is_expired_page(text):
        return "RESULT:EXPIRED", False

    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(500)
    except Exception:
        pass

    submit_patterns = ["Submit Application", "Submit", "Apply"]
    form = page.locator("#application-form").first
    submit_candidates = [
        form.locator("button[type='submit']"),
        form.locator("input[type='submit']"),
    ]

    for label in submit_patterns:
        submit_candidates.append(
            form.locator("button, input[type='submit'], [role='button']").filter(
                has_text=re.compile(rf"^{re.escape(label)}$", re.I)
            )
        )

    if dry_run:
        for locator in submit_candidates:
            try:
                if locator.count():
                    return "RESULT:APPLIED dry_run_local_agent", False
            except Exception:
                continue

    for locator in submit_candidates:
        try:
            if click_locator(locator, use_last=True, wait_ms=5000):
                return None, True
        except Exception:
            continue
    return None, False


def start_workday_application(page, *, prefer_manual: bool = False) -> bool:
    accept_cookies(page)
    entry_buttons = [
        "Apply",
        "Start Your Application",
        "Start Application",
        "Get Started",
    ]
    start_url = page.url
    before_signature = workday_signature(page, page_text(page))
    clicked_entry = click_text(page, entry_buttons, tags="a, button, [role='button']")
    if not clicked_entry:
        clicked_entry = click_exact_any(page, "Apply", use_last=True) or click_role_button(page, r"^Apply$")
    if clicked_entry:
        for _ in range(8):
            page.wait_for_timeout(1000)
            current_text = page_text(page)
            if page.url != start_url:
                break
            if is_expired_page(current_text):
                break
            if not is_workday_listing_page(page, current_text):
                break
            current_signature = workday_signature(page, current_text)
            if current_signature != before_signature:
                break
    options = ["Apply Manually", "Autofill with Resume"] if prefer_manual else ["Autofill with Resume", "Apply Manually"]
    option_clicked = click_contains(page, options, tags="a, button, [role='button']")
    page.wait_for_timeout(1500)
    return clicked_entry or option_clicked


def run_workday(page, profile: dict[str, Any], ctx: PromptContext, args: argparse.Namespace) -> str:
    if args.verbose:
        log(f"ACTION: navigating to target job url={ctx.job_url}")
    page.goto(ctx.job_url, wait_until="networkidle", timeout=120000)
    page.wait_for_timeout(2000)

    text = page_text(page)
    if is_expired_page(text):
        return "RESULT:EXPIRED"

    prefer_manual = False
    manual_fallback_used = False
    learned_answers: list[dict[str, Any]] = []
    accepted_qualitative_answers: list[dict[str, Any]] = []
    start_workday_application(page, prefer_manual=prefer_manual)
    stagnant = 0
    last_signature = ""

    def finalize_workday_result(result: str) -> str:
        if result.startswith("RESULT:APPLIED"):
            persist_company_question_answers(ctx, learned_answers)
        suggestions = build_workday_question_suggestions(page, ctx, profile, learned_answers)
        persist_company_question_memory(
            ctx,
            learned_answers + accepted_qualitative_answers,
            build_workday_memory_entries(suggestions),
        )
        persist_question_override_suggestions(ctx, suggestions)
        append_workday_question_review(page, ctx, result, suggestions)
        return result

    for step in range(30):
        accept_cookies(page)
        text = page_text(page)
        if force_workday_english(page, text):
            text = page_text(page)
        if args.verbose:
            log(f"ACTION: workday step {step + 1} url={page.url}")
            log_workday_snapshot(page, text)

        if is_success_page(text):
            return finalize_workday_result("RESULT:APPLIED")
        if is_expired_page(text):
            return finalize_workday_result("RESULT:EXPIRED")
        if "captcha" in text.lower():
            return finalize_workday_result("RESULT:CAPTCHA")
        if "reset your password due to an administrator request" in text.lower():
            return finalize_workday_result("RESULT:LOGIN_ISSUE")

        if "userhome" in page.url.lower():
            log("ACTION: returning from Workday userHome to application")
            page.goto(ctx.job_url, wait_until="networkidle", timeout=120000)
            page.wait_for_timeout(2000)
            start_workday_application(page, prefer_manual=prefer_manual)
            continue

        if is_workday_listing_page(page, text):
            if args.verbose:
                log("ACTION: still on Workday listing page, retrying application entry")
            if start_workday_application(page, prefer_manual=prefer_manual):
                page.wait_for_timeout(2000)
                continue

        if "something went wrong" in text.lower():
            if not manual_fallback_used:
                manual_fallback_used = True
                prefer_manual = True
                log("ACTION: Workday autofill failed, retrying with Apply Manually")
                page.goto(ctx.job_url, wait_until="networkidle", timeout=120000)
                page.wait_for_timeout(2000)
                start_workday_application(page, prefer_manual=True)
                continue
                return finalize_workday_result("RESULT:FAILED:autofill_error")

        if "Upload either DOC" in text or "Select file" in text:
            if workday_upload_autofill_resume(page, ctx):
                log("ACTION: uploaded resume for Workday autofill")
                page.wait_for_timeout(2000)

        if (
            "Create Account/Sign In" in text
            or "Sign in with email" in text
            or "Create Account" in text
            or is_workday_signin_screen(page, text)
        ):
            auth_result = workday_account_gate(page, profile, ctx)
            if auth_result == "handled":
                page.wait_for_timeout(2500)
                continue
            if auth_result != "skip":
                return finalize_workday_result(auth_result)
            page.wait_for_timeout(2500)
            continue

        override_changed, blocker, page_answers = fill_workday_overrides(page, profile, ctx)
        for entry in page_answers:
            remember_learned_answer(
                learned_answers,
                entry.get("match_any", []),
                entry.get("answer") if isinstance(entry.get("answer"), list) else [entry.get("answer")],
            )
        if blocker:
            return finalize_workday_result(f"RESULT:FAILED:{blocker}")

        field_changes, page_qualitative_answers = fill_visible_fields(page, profile, ctx, args)
        changed = override_changed + field_changes
        nav_result, nav_clicked = click_primary_navigation(page, ctx.dry_run)
        if nav_result:
            if nav_result.startswith("RESULT:APPLIED"):
                record_successful_qualitative_answers(
                    accepted_qualitative_answers,
                    page_qualitative_answers,
                    acceptance_level="submitted",
                )
            return finalize_workday_result(nav_result)

        current_text = page_text(page)
        signature = workday_signature(page, current_text)
        if nav_clicked and signature != last_signature and not visible_error_texts(page):
            record_successful_qualitative_answers(
                accepted_qualitative_answers,
                page_qualitative_answers,
                acceptance_level="page_advanced",
            )
        if not changed and not nav_clicked and signature == last_signature:
            stagnant += 1
        else:
            stagnant = 0
            last_signature = signature

        if stagnant >= 3:
            log_workday_snapshot(page, current_text, prefix="ERROR")
            return finalize_workday_result("RESULT:FAILED:stuck")

        page.wait_for_timeout(2000)

    return finalize_workday_result("RESULT:FAILED:stuck")


def run_greenhouse(page, profile: dict[str, Any], ctx: PromptContext, args: argparse.Namespace) -> str:
    page.goto(ctx.job_url, wait_until="networkidle", timeout=120000)
    page.wait_for_timeout(1500)

    stagnant = 0
    last_signature = ""
    learned_answers: list[dict[str, Any]] = []
    accepted_qualitative_answers: list[dict[str, Any]] = []
    seen_questions: list[str] = []
    seen_keys: set[str] = set()

    def remember_greenhouse_snapshot() -> None:
        snapshot_learned, snapshot_seen = build_greenhouse_question_memory(page, profile, ctx, args)
        for entry in snapshot_learned:
            remember_learned_answer(
                learned_answers,
                entry.get("match_any", []),
                entry.get("answer") if isinstance(entry.get("answer"), list) else [entry.get("answer")],
            )
        for question_text in snapshot_seen:
            normalized = normalize_override_key(question_text)
            if not normalized or normalized in seen_keys:
                continue
            seen_keys.add(normalized)
            seen_questions.append(question_text)

    def finalize_greenhouse_result(result: str) -> str:
        remember_greenhouse_snapshot()
        if result.startswith("RESULT:APPLIED"):
            persist_company_question_answers(ctx, learned_answers)
        persist_company_question_memory(ctx, learned_answers + accepted_qualitative_answers, seen_questions)
        return result

    for step in range(20):
        accept_cookies(page)
        text = page_text(page)
        lower = text.lower()
        form_visible = page.locator('#application-form').count() > 0
        log(f"ACTION: greenhouse step {step + 1} url={page.url} form_visible={form_visible}")
        if args.verbose or step == 0:
            log(
                f"ACTION: greenhouse snapshot headings={page_headings(page)} "
                f"buttons={visible_buttons(page)[:12]} "
                f"errors={visible_error_texts(page)} "
                f"fields={summarize_visible_fields(page, limit=12)} "
                f"text={normalize_space(text)[:280]!r}"
            )

        remember_greenhouse_snapshot()

        if is_success_page(text) or "your application has been submitted" in lower:
            return finalize_greenhouse_result("RESULT:APPLIED")
        if is_expired_page(text):
            return finalize_greenhouse_result("RESULT:EXPIRED")
        if "captcha" in lower:
            return finalize_greenhouse_result("RESULT:CAPTCHA")

        if not form_visible:
            if start_greenhouse_application(page):
                log('ACTION: greenhouse clicked application entry')
                page.wait_for_timeout(1500)
                continue

        changed = greenhouse_prepare_uploads(page, ctx)
        field_changes, page_qualitative_answers = fill_visible_fields(page, profile, ctx, args)
        changed += field_changes
        security_result, security_changes = fill_greenhouse_security_code(page, profile, ctx)
        if security_result:
            return finalize_greenhouse_result(security_result)
        changed += security_changes
        if changed:
            log(f"ACTION: greenhouse changed_fields={field_changes} total_changes={changed}")
        nav_result, nav_clicked = click_greenhouse_navigation(page, ctx.dry_run)
        if nav_clicked:
            log('ACTION: greenhouse clicked primary navigation')
        if nav_result:
            if nav_result.startswith("RESULT:APPLIED"):
                record_successful_qualitative_answers(
                    accepted_qualitative_answers,
                    page_qualitative_answers,
                    acceptance_level="submitted",
                )
            return finalize_greenhouse_result(nav_result)

        current_text = page_text(page)
        signature = workday_signature(page, current_text)
        if nav_clicked and signature != last_signature and not visible_error_texts(page):
            record_successful_qualitative_answers(
                accepted_qualitative_answers,
                page_qualitative_answers,
                acceptance_level="page_advanced",
            )
        if not changed and not nav_clicked and signature == last_signature:
            stagnant += 1
        else:
            stagnant = 0
            last_signature = signature

        if stagnant >= 2:
            log(
                f"ERROR: greenhouse stuck buttons={visible_buttons(page)[:12]} "
                f"errors={visible_error_texts(page)} "
                f"fields={summarize_visible_fields(page, limit=12)} "
                f"text={normalize_space(current_text)[:320]!r}"
            )
            return finalize_greenhouse_result("RESULT:FAILED:stuck")

        page.wait_for_timeout(1500)

    return finalize_greenhouse_result("RESULT:FAILED:stuck")


def connect_page(port: str, preferred_url: str = "", connect_timeout_s: float = 30.0):
    def canonical_url(value: str) -> str:
        raw = (value or "").strip()
        if not raw:
            return ""
        raw = raw.split("#", 1)[0].split("?", 1)[0].rstrip("/")
        return raw.lower()

    cdp_url = f"http://127.0.0.1:{port}"
    playwright = sync_playwright().start()
    deadline = time.time() + max(1.0, connect_timeout_s)
    last_error: Exception | None = None
    browser = None
    while time.time() < deadline:
        try:
            browser = playwright.chromium.connect_over_cdp(cdp_url)
            break
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    if browser is None:
        try:
            playwright.stop()
        except Exception:
            pass
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Unable to connect to CDP at {cdp_url}")
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = None
    preferred_norm = canonical_url(preferred_url)
    for candidate in context.pages:
        candidate_url = canonical_url(candidate.url or "")
        if preferred_norm and candidate_url == preferred_norm:
            page = candidate
            break
    if page is None:
        page = context.new_page()
    return playwright, browser, context, page


def extract_result_line(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("RESULT:"):
            return line
    match = RESULT_RE.search(text)
    return match.group(0) if match else None


def run_browser_agent(args: argparse.Namespace, ctx: PromptContext, profile: dict[str, Any]) -> str:
    cdp_port = os.environ.get("APPLYPILOT_CDP_PORT", "").strip()
    if not cdp_port:
        return "RESULT:FAILED:missing_cdp_port"
    if not ctx.job_url:
        return "RESULT:FAILED:missing_job_url"

    playwright = browser = page = None
    host = urlparse(ctx.job_url).hostname or ""
    try:
        log(f"ACTION: agent start host={host} title={ctx.job_title!r}")
        log(f"ACTION: connecting to CDP port {cdp_port} for {ctx.job_url}")
        playwright, browser, _context, page = connect_page(cdp_port, preferred_url=ctx.job_url)
        log(f"ACTION: connected page initial_url={page.url}")
        if "myworkdayjobs.com" in host:
            log("ACTION: entering Workday flow")
            return run_workday(page, profile, ctx, args)
        if "greenhouse.io" in host:
            log("ACTION: entering Greenhouse flow")
            return run_greenhouse(page, profile, ctx, args)
        return "RESULT:FAILED:unsupported_site"
    except PlaywrightTimeoutError:
        return "RESULT:FAILED:page_timeout"
    except Exception as exc:
        log(f"ACTION: agent_runtime_error type={type(exc).__name__} error={exc}")
        try:
            if page is not None:
                log(f"ACTION: agent_runtime_error page_url={page.url}")
        except Exception:
            pass
        for artifact in write_runtime_error_artifacts(page, host):
            log(f"ACTION: runtime artifact={artifact}")
        log(traceback.format_exc().rstrip())
        return "RESULT:FAILED:agent_runtime_error"
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if playwright:
            try:
                playwright.stop()
            except Exception:
                pass


def main() -> int:
    args = parse_args()
    prompt = read_prompt(args.prompt_file)
    if not prompt.strip():
        print("RESULT:FAILED:empty_prompt")
        return 1

    ctx = parse_prompt(prompt)
    profile = load_profile()
    log(f"ACTION: prompt parsed job={ctx.job_title!r} dry_run={ctx.dry_run}")
    result_line = run_browser_agent(args, ctx, profile)
    if args.verbose:
        maybe_result = extract_result_line(result_line)
        if maybe_result:
            log(f"FINAL: {maybe_result}")
    print(result_line)
    return 0 if result_line.startswith("RESULT:APPLIED") else 1


if __name__ == "__main__":
    raise SystemExit(main())

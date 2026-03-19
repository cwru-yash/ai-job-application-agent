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
from dataclasses import dataclass
from datetime import datetime
from email import header, policy
from email.parser import BytesParser
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
CREDENTIALS_PATH = Path.home() / ".applypilot" / "local_agent_credentials.json"
ACCOUNT_PASSWORDS_PATH = Path.home() / ".applypilot" / "account_passwords.json"


@dataclass(slots=True)
class PromptContext:
    prompt: str
    job_title: str
    company: str
    job_url: str
    resume_pdf: str
    cover_pdf: str
    resume_text: str
    cover_text: str
    dry_run: bool


@dataclass(slots=True)
class MailLink:
    subject: str
    sender: str
    link: str


def log(msg: str) -> None:
    print(msg, flush=True)


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


def load_credentials() -> dict[str, Any]:
    if not CREDENTIALS_PATH.exists():
        return {}
    try:
        return json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_credentials(data: dict[str, Any]) -> None:
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(CREDENTIALS_PATH, 0o600)
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


def fetch_workday_mail_link(email_address: str, host: str, mode: str, timeout_s: int = 120) -> MailLink | None:
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
            message_ids = (data[0] or b"").split()[-20:]
            for msg_id in reversed(message_ids):
                fetch_status, payload = mailbox.fetch(msg_id, "(RFC822)")
                if fetch_status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                subject, sender, links = parse_mail_links(payload[0][1])
                subject_sender = f"{subject} {sender}".lower()
                if "workday" not in subject_sender and "password" not in subject_sender and "verify" not in subject_sender:
                    if not any("myworkdayjobs.com" in link.lower() or host.lower() in link.lower() for link in links):
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


def generate_password() -> str:
    token = secrets.token_urlsafe(12)
    return f"Ap{token}!9a"


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
    data = load_credentials()
    entry = data.get(account_key(host, email))
    return entry if isinstance(entry, dict) else {}


def update_account_entry(host: str, email: str, **updates: Any) -> dict[str, Any]:
    data = load_credentials()
    key = account_key(host, email)
    entry = dict(data.get(key) or {})
    entry["email"] = email
    for field, value in updates.items():
        if value is None:
            entry.pop(field, None)
        else:
            entry[field] = value
    data[key] = entry
    save_credentials(data)
    return entry


def get_or_create_account_password(host: str, email: str) -> str:
    explicit = scoped_env_account_password(host)
    entry = get_account_entry(host, email)
    if entry and entry.get("password"):
        return entry["password"]

    if explicit:
        return explicit
    password = generate_password()
    update_account_entry(host, email, password=password, source="generated", status="generated")
    return password


def get_known_account_password(host: str, email: str) -> str:
    explicit = scoped_env_account_password(host)
    if explicit:
        return explicit

    entry = get_account_entry(host, email)
    if entry and entry.get("password"):
        return entry["password"]
    return ""


def ensure_account_password(host: str, email: str) -> str:
    known = get_known_account_password(host, email)
    if known:
        return known

    password = generate_password()
    update_account_entry(host, email, password=password, source="generated", status="generated")
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
        {"role": "system", "content": "Write a short, professional job application response in 2-3 sentences."},
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


def open_ended_answer(field_label: str, ctx: PromptContext, profile: dict[str, Any], args: argparse.Namespace) -> str:
    fallback = short_cover_text(ctx.cover_text)
    if fallback:
        return fallback
    prompt = (
        f"Job title: {ctx.job_title}\n"
        f"Company: {ctx.company}\n"
        f"Candidate title: {applicant_value(profile, 'current_title')}\n"
        f"Question: {field_label}\n"
        "Answer in 2-3 sentences, concrete and professional."
    )
    generated = llm_text(args, prompt)
    return generated.strip() or f"I am excited about {ctx.job_title} at {ctx.company} because it matches my background in AI-driven software and backend systems."


def resolve_field_value(field: dict[str, Any], profile: dict[str, Any], ctx: PromptContext, args: argparse.Namespace) -> Any:
    label = " ".join(
        value for value in [field.get("label"), field.get("name"), field.get("placeholder")] if value
    ).lower()
    field_type = field.get("type", "")
    options = field.get("options") or []
    today = datetime.now()

    if field.get("name") == "website":
        return None
    if "password" in label:
        return None
    if field_type == "file":
        if "cover" in label and ctx.cover_pdf and ctx.cover_pdf != "N/A":
            return ctx.cover_pdf
        if any(term in label for term in ("resume", "cv", "autofill")):
            return ctx.resume_pdf
        return None
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
    if "city" in label:
        return applicant_value(profile, "city")
    if any(term in label for term in ("state", "province", "region")):
        desired = applicant_value(profile, "state")
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
    if any(term in label for term in ("authorized to work", "legally authorized", "work authorization")):
        desired = applicant_value(profile, "authorized")
        return select_best_option(options, desired) if options else desired
    if any(term in label for term in ("sponsorship", "sponsor", "visa support", "require visa")):
        desired = applicant_value(profile, "sponsorship")
        return select_best_option(options, desired) if options else desired
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
    if field.get("tag") == "textarea":
        return open_ended_answer(field.get("label", "application question"), ctx, profile, args)
    if "why" in label or "motivation" in label or "tell us" in label or "summary" in label:
        return open_ended_answer(field.get("label", "application question"), ctx, profile, args)
    return None


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


def fill_visible_fields(page, profile: dict[str, Any], ctx: PromptContext, args: argparse.Namespace) -> int:
    fields = annotate_fields(page)
    changed = 0
    for field in fields:
        value = resolve_field_value(field, profile, ctx, args)
        if fill_field(page, field, value):
            changed += 1
    if changed:
        page.wait_for_timeout(1000)
    return changed


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


def choose_text_option(page, desired_texts: list[str]) -> bool:
    for text in desired_texts:
        exact_patterns = [
            "[role='option']",
            "li[role='option']",
            "[data-automation-id='promptOption']",
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
    page, locator, desired_options: list[str], error_fragments: list[str], *, had_error: bool = False
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
            page.wait_for_timeout(400)
        except Exception:
            try:
                page.keyboard.type(text, delay=20)
                page.wait_for_timeout(400)
            except Exception:
                pass
        if choose_text_option(page, [text]):
            if workday_selection_committed(page, locator, [text], error_fragments, had_error=had_error):
                return True
        try:
            locator.press("ArrowDown")
            locator.press("Enter")
            page.wait_for_timeout(500)
        except Exception:
            try:
                page.keyboard.press("ArrowDown")
                page.keyboard.press("Enter")
                page.wait_for_timeout(500)
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


def workday_fill_primary_questionnaire(
    page, question_answers: list[tuple[list[str], list[str]]]
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
            continue
        for _ in range(2):
            try:
                button.scroll_into_view_if_needed(timeout=3000)
                button.click(timeout=3000)
                page.wait_for_timeout(400)
            except Exception:
                continue
            exact_option_clicked = False
            for option_text in desired_options:
                option = page.locator("li[role='option']").filter(
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
    candidates = page.locator("div, section, li, fieldset").filter(has_text=pattern)
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
        select_buttons = page.locator("button, [role='button'], [role='combobox'], div").filter(
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


def fill_workday_overrides(page, profile: dict[str, Any]) -> tuple[int, str | None]:
    blocker = workday_profile_requirements(page, profile)
    if blocker:
        return 0, blocker

    changed = 0
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
    if workday_searchable_select_by_field(
        page,
        ["how did you hear about us", "source"],
        source_options,
        error_fragments=["how did you hear about us", "source"],
    ):
        changed += 1
    elif workday_searchable_select_by_label(
        page,
        r"How Did You Hear About Us",
        source_options,
        error_fragments=["how did you hear about us", "source"],
    ):
        changed += 1
    elif workday_select_button_option(page, "source--source", source_options):
        changed += 1
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

    yes_options = ["Yes", "Sí", "Si"]
    no_options = ["No"]
    select_one_answers = [
        (["legally authorized to work", "legalmente autorizado para trabajar"], yes_options if applicant_value(profile, "authorized") == "Yes" else no_options),
        (["require sponsorship for employment visa status", "requiere ahora", "patrocinio para obtener el estado de visa"], yes_options if applicant_value(profile, "sponsorship") == "Yes" else no_options),
        (["restrictive covenants", "acuerdos restrictivos"], no_options),
        (["current contractor at any thomson reuters location", "actualmente un contractor"], no_options),
    ]
    changed += workday_fill_primary_questionnaire(page, select_one_answers)
    for fragments, desired_options in select_one_answers:
        if any(fragment in body.lower() for fragment in fragments):
            for fragment in fragments:
                if workday_pick_select_one(page, fragment, desired_options):
                    changed += 1
                    break

    yes_no_answers = [
        ("Please indicate if you have previously worked for Thomson Reuters", "No"),
    ]
    for question_text, answer in yes_no_answers:
        if question_text.lower() in body.lower():
            if workday_answer_yes_no(page, question_text, answer):
                changed += 1

    changed += workday_fill_self_identify(page, profile)

    return changed, None


def is_workday_signin_screen(page, text: str, fields: list[dict[str, Any]] | None = None) -> bool:
    fields = fields or annotate_fields(page)
    return (
        "sign in" in text.lower()
        and any(field.get("autocomplete") == "current-password" for field in fields)
    )


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
    known_password = get_known_account_password(host, email)
    fields = annotate_fields(page)
    log(f"ACTION: auth buttons={visible_buttons(page)[:20]}")
    log(f"ACTION: auth text={text[:240]!r}")

    opened_email_sign_in = click_role_button(page, r"sign in with email")
    if not opened_email_sign_in:
        if click_button_exact(page, "Sign In", use_last=True):
            log("ACTION: opened generic sign-in")
            page.wait_for_timeout(1500)
            fields = annotate_fields(page)
            opened_email_sign_in = click_role_button(page, r"sign in with email")
    if opened_email_sign_in:
        log("ACTION: opened email sign-in")
        fields = annotate_fields(page)

    email_field = next((f for f in fields if f.get("autocomplete") == "email"), None)
    current_password = next((f for f in fields if f.get("autocomplete") == "current-password"), None)
    new_password = next((f for f in fields if f.get("autocomplete") == "new-password"), None)
    log(
        "ACTION: auth fields "
        f"email={bool(email_field)} current_password={bool(current_password)} new_password={bool(new_password)} "
        f"known_password={bool(known_password)}"
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
            page.wait_for_timeout(3000)
            new_text = page_text(page)
            new_fields = annotate_fields(page)
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

    if not known_password and click_role_button(page, r"^create account$"):
        log("ACTION: moved to Workday create-account form")
        page.wait_for_timeout(1500)
        fields = annotate_fields(page)
        password = ensure_account_password(host, email)
        email_field = next((f for f in fields if f.get("autocomplete") == "email"), None)
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
        for box in consent_boxes:
            fill_field(page, box, True)
        submitted = click_button_exact(page, "Create Account", submit_only=True)
        if not submitted:
            submitted = click_role_button(page, r"^create account$")
        if submitted:
            log("ACTION: submitted Workday create-account form")
            page.wait_for_timeout(4000)
            new_text = page_text(page)
            log(
                f"ACTION: post-create-account errors={visible_error_texts(page)} "
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
                update_account_entry(host, email, status="verification_required")
                mailbox_result = maybe_complete_workday_mailbox_flow(page, host, email, "verify")
                if mailbox_result == "verification_completed":
                    return "handled"
                return "RESULT:FAILED:account_confirmation_required"
            if is_workday_signin_screen(page, new_text):
                return "handled"
            return "handled"
        log(f"ACTION: create-account submit unavailable buttons={visible_buttons(page)[:20]}")

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


def start_workday_application(page, *, prefer_manual: bool = False) -> None:
    accept_cookies(page)
    entry_buttons = [
        "Apply",
        "Start Your Application",
        "Start Application",
        "Get Started",
    ]
    if click_text(page, entry_buttons, tags="a, button, [role='button']"):
        page.wait_for_timeout(1500)
    options = ["Apply Manually", "Autofill with Resume"] if prefer_manual else ["Autofill with Resume", "Apply Manually"]
    click_contains(page, options, tags="a, button, [role='button']")
    page.wait_for_timeout(1500)


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
    start_workday_application(page, prefer_manual=prefer_manual)
    stagnant = 0
    last_signature = ""

    for step in range(30):
        accept_cookies(page)
        text = page_text(page)
        if force_workday_english(page, text):
            text = page_text(page)
        if args.verbose:
            log(f"ACTION: workday step {step + 1} url={page.url}")
            log_workday_snapshot(page, text)

        if is_success_page(text):
            return "RESULT:APPLIED"
        if is_expired_page(text):
            return "RESULT:EXPIRED"
        if "captcha" in text.lower():
            return "RESULT:CAPTCHA"
        if "reset your password due to an administrator request" in text.lower():
            return "RESULT:LOGIN_ISSUE"

        if "userhome" in page.url.lower():
            log("ACTION: returning from Workday userHome to application")
            page.goto(ctx.job_url, wait_until="networkidle", timeout=120000)
            page.wait_for_timeout(2000)
            start_workday_application(page, prefer_manual=prefer_manual)
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
            return "RESULT:FAILED:autofill_error"

        if "Upload either DOC" in text or "Select file" in text:
            if workday_upload_autofill_resume(page, ctx):
                log("ACTION: uploaded resume for Workday autofill")
                page.wait_for_timeout(2000)

        if "Create Account/Sign In" in text or "Sign in with email" in text or "Create Account" in text:
            auth_result = workday_account_gate(page, profile, ctx)
            if auth_result == "handled":
                page.wait_for_timeout(2500)
                continue
            if auth_result != "skip":
                return auth_result
            page.wait_for_timeout(2500)
            continue

        override_changed, blocker = fill_workday_overrides(page, profile)
        if blocker:
            return f"RESULT:FAILED:{blocker}"

        changed = override_changed + fill_visible_fields(page, profile, ctx, args)
        nav_result, nav_clicked = click_primary_navigation(page, ctx.dry_run)
        if nav_result:
            return nav_result

        current_text = page_text(page)
        signature = workday_signature(page, current_text)
        if not changed and not nav_clicked and signature == last_signature:
            stagnant += 1
        else:
            stagnant = 0
            last_signature = signature

        if stagnant >= 3:
            log_workday_snapshot(page, current_text, prefix="ERROR")
            return "RESULT:FAILED:stuck"

        page.wait_for_timeout(2000)

    return "RESULT:FAILED:stuck"


def connect_page(port: str, preferred_url: str = ""):
    cdp_url = f"http://127.0.0.1:{port}"
    playwright = sync_playwright().start()
    browser = playwright.chromium.connect_over_cdp(cdp_url)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = None
    preferred_host = (urlparse(preferred_url).hostname or "").lower()
    for candidate in context.pages:
        candidate_url = (candidate.url or "").lower()
        if preferred_url and candidate.url == preferred_url:
            page = candidate
            break
        if preferred_host and preferred_host in candidate_url:
            page = candidate
            break
    if page is None:
        page = context.pages[0] if context.pages else context.new_page()
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

    playwright = browser = None
    try:
        if args.verbose:
            log(f"ACTION: connecting to CDP port {cdp_port} for {ctx.job_url}")
        playwright, browser, _context, page = connect_page(cdp_port, preferred_url=ctx.job_url)
        if args.verbose:
            log(f"ACTION: connected page initial_url={page.url}")
        host = urlparse(ctx.job_url).hostname or ""
        if "myworkdayjobs.com" in host:
            return run_workday(page, profile, ctx, args)
        return "RESULT:FAILED:unsupported_site"
    except PlaywrightTimeoutError:
        return "RESULT:FAILED:page_timeout"
    except Exception as exc:
        if args.verbose:
            log(f"agent error: {exc}")
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
    result_line = run_browser_agent(args, ctx, profile)
    if args.verbose:
        maybe_result = extract_result_line(result_line)
        if maybe_result:
            log(f"FINAL: {maybe_result}")
    print(result_line)
    return 0 if result_line.startswith("RESULT:APPLIED") else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


OVERRIDES_PATH = Path.home() / ".applypilot" / "question_overrides.json"
SUGGESTIONS_PATH = Path.home() / ".applypilot" / "question_override_suggestions.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Promote reviewed Workday question suggestions into the active overrides file."
    )
    parser.add_argument(
        "--company",
        action="append",
        default=[],
        help="Only promote suggestions for this company. Repeat to include multiple companies.",
    )
    parser.add_argument(
        "--prune-promoted",
        action="store_true",
        help="Remove promoted or duplicate entries from the suggestions file.",
    )
    parser.add_argument(
        "--drop-empty",
        action="store_true",
        help="Also drop blank-answer suggestions from the suggestions file.",
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def clean_fragments(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    fragments: list[str] = []
    for item in raw or []:
        normalized = normalize(str(item))
        if normalized and normalized not in fragments:
            fragments.append(normalized)
    return fragments


def clean_answer(raw: Any) -> str | list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        answers = [str(item).strip() for item in raw if str(item).strip()]
        if not answers:
            return None
        return answers if len(answers) > 1 else answers[0]
    answer = str(raw).strip()
    return answer or None


def company_bucket(data: dict[str, Any], company: str) -> list[dict[str, Any]]:
    companies = data.setdefault("companies", {})
    bucket = companies.get(company)
    if isinstance(bucket, list):
        return bucket
    bucket = []
    companies[company] = bucket
    return bucket


def is_duplicate(bucket: list[dict[str, Any]], fragments: list[str], answer: str | list[str]) -> bool:
    fragment_set = set(fragments)
    for existing in bucket:
        existing_fragments = clean_fragments(existing.get("match_any"))
        existing_set = set(existing_fragments)
        if existing.get("answer") != answer:
            continue
        if sorted(existing_fragments) == sorted(fragments):
            return True
        if fragment_set and existing_set and (
            fragment_set.issubset(existing_set) or existing_set.issubset(fragment_set)
        ):
            return True
    return False


def selected_company(companies: list[str], company: str) -> bool:
    if not companies:
        return True
    company_key = normalize(company)
    return any(normalize(item) == company_key for item in companies)


def main() -> int:
    args = parse_args()
    overrides = load_json(OVERRIDES_PATH)
    suggestions = load_json(SUGGESTIONS_PATH)

    source_companies = suggestions.get("companies")
    if not isinstance(source_companies, dict) or not source_companies:
        print("No company suggestions found.")
        return 0

    promoted = 0
    duplicates = 0
    unresolved = 0
    dropped_empty = 0
    kept_companies: dict[str, list[dict[str, Any]]] = {}

    for company, entries in source_companies.items():
        if not selected_company(args.company, company):
            kept_companies[company] = list(entries) if isinstance(entries, list) else []
            continue

        bucket = company_bucket(overrides, company)
        remaining: list[dict[str, Any]] = []

        for entry in entries if isinstance(entries, list) else []:
            fragments = clean_fragments(entry.get("match_any"))
            answer = clean_answer(entry.get("answer"))
            if not fragments:
                continue
            if answer is None:
                unresolved += 1
                if args.drop_empty:
                    dropped_empty += 1
                else:
                    remaining.append(entry)
                continue

            if is_duplicate(bucket, fragments, answer):
                duplicates += 1
                if not args.prune_promoted:
                    remaining.append(entry)
                continue

            bucket.append(
                {
                    "match_any": fragments,
                    "answer": answer,
                    "promoted_from_suggestions": True,
                }
            )
            promoted += 1
            if not args.prune_promoted:
                remaining.append(entry)

        if remaining:
            kept_companies[company] = remaining

    overrides_changed = promoted > 0
    suggestions_changed = args.prune_promoted or args.drop_empty

    if overrides_changed:
        save_json(OVERRIDES_PATH, overrides)
    if suggestions_changed:
        save_json(SUGGESTIONS_PATH, {"companies": kept_companies})

    print(f"Promoted: {promoted}")
    print(f"Duplicates skipped: {duplicates}")
    print(f"Unresolved kept: {unresolved - dropped_empty}")
    print(f"Unresolved dropped: {dropped_empty}")
    print(f"Overrides path: {OVERRIDES_PATH}")
    print(f"Suggestions path: {SUGGESTIONS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

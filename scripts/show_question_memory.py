#!/usr/bin/env python3
"""Inspect per-company ATS question memory files."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path


QUESTION_MEMORY_DIR = Path.home() / ".applypilot" / "question_memory"


def slugify_company_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_value).strip("_") or "unknown"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("company", nargs="?", help="Company name to inspect, e.g. 'Grafana Labs'")
    parser.add_argument(
        "--ats",
        choices=("all", "greenhouse", "workday", "generic"),
        default="all",
        help="Restrict to one ATS family. Defaults to all.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List matching memory files instead of printing full JSON.",
    )
    return parser.parse_args()


def ats_dirs(selected: str) -> list[Path]:
    if selected == "all":
        return sorted([path for path in QUESTION_MEMORY_DIR.iterdir() if path.is_dir()]) if QUESTION_MEMORY_DIR.exists() else []
    path = QUESTION_MEMORY_DIR / selected
    return [path] if path.exists() else []


def matching_files(company: str | None, selected_ats: str) -> list[Path]:
    dirs = ats_dirs(selected_ats)
    if not company:
        files: list[Path] = []
        for ats_dir in dirs:
            files.extend(sorted(ats_dir.glob("*.json")))
        return files
    slug = slugify_company_name(company)
    matches: list[Path] = []
    for ats_dir in dirs:
        candidate = ats_dir / f"{slug}.json"
        if candidate.exists():
            matches.append(candidate)
    return matches


def summarize_file(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return f"{path} :: invalid_json"
    questions = payload.get("questions") or []
    question_count = len(questions)
    generated_count = sum(1 for item in questions if isinstance(item, dict) and item.get("source") == "llm_generated")
    manual_count = max(0, question_count - generated_count)
    seen_count = len(payload.get("seen_questions") or [])
    company = payload.get("company") or path.stem
    ats = payload.get("ats") or path.parent.name
    return (
        f"{path} :: ats={ats} company={company} questions={question_count} "
        f"(manual={manual_count} generated={generated_count}) seen={seen_count}"
    )


def main() -> int:
    args = parse_args()
    files = matching_files(args.company, args.ats)
    if not files:
        print("No question memory files found.")
        return 1

    if args.list or len(files) > 1 or not args.company:
        for path in files:
            print(summarize_file(path))
        return 0

    path = files[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    print(path)
    questions = payload.get("questions") or []
    generated_count = sum(1 for item in questions if isinstance(item, dict) and item.get("source") == "llm_generated")
    manual_count = max(0, len(questions) - generated_count)
    print(f"ats={payload.get('ats') or path.parent.name} company={payload.get('company') or path.stem}")
    print(f"questions={len(questions)} manual={manual_count} generated={generated_count} seen={len(payload.get('seen_questions') or [])}")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

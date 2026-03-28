from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def load_local_apply_agent():
    spec = importlib.util.spec_from_file_location(
        "local_apply_agent",
        ROOT / "scripts" / "local_apply_agent.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


local_apply_agent = load_local_apply_agent()
from applypilot.apply import prompt as prompt_mod  # noqa: E402


class QualitativeAnswerTests(unittest.TestCase):
    def test_should_route_to_qualitative_llm_for_narrative_fields(self) -> None:
        self.assertTrue(
            local_apply_agent.should_route_to_qualitative_llm(
                {"tag": "textarea", "label": "Why are you interested in this role?"}
            )
        )
        self.assertTrue(
            local_apply_agent.should_route_to_qualitative_llm(
                {"tag": "input", "type": "text", "label": "Tell us about a relevant project"}
            )
        )

    def test_should_not_route_operational_fields(self) -> None:
        self.assertFalse(
            local_apply_agent.should_route_to_qualitative_llm(
                {"tag": "textarea", "label": "LinkedIn URL"}
            )
        )
        self.assertFalse(
            local_apply_agent.should_route_to_qualitative_llm(
                {"tag": "input", "type": "text", "label": "Security code"}
            )
        )

    def test_select_relevant_snippets_prefers_matching_content(self) -> None:
        text = (
            "Built internal dashboards for hiring analytics.\n\n"
            "Designed distributed systems for backend data pipelines and resilient services.\n\n"
            "Maintained documentation and onboarding guides."
        )
        snippets = local_apply_agent.select_relevant_snippets(
            "Describe your distributed systems experience",
            text,
            role_hint="Backend Engineer",
            max_chunks=2,
            max_chars=1000,
        )
        self.assertTrue(snippets)
        self.assertIn("distributed systems", snippets[0].lower())

    def test_record_successful_qualitative_answers_upgrades_acceptance(self) -> None:
        learned: list[dict[str, object]] = []
        generated = [
            {
                "match_any": ["why are you interested in this role"],
                "answer": "I enjoy building reliable backend systems that align well with this role.",
                "label": "Why are you interested in this role?",
                "source": "llm_generated",
                "generated_at": "2026-03-28T12:00:00-04:00",
            }
        ]
        local_apply_agent.record_successful_qualitative_answers(
            learned,
            generated,
            acceptance_level="page_advanced",
        )
        self.assertEqual(len(learned), 1)
        self.assertEqual(learned[0]["source"], "llm_generated")
        self.assertEqual(learned[0]["acceptance_level"], "page_advanced")

        local_apply_agent.record_successful_qualitative_answers(
            learned,
            generated,
            acceptance_level="submitted",
        )
        self.assertEqual(len(learned), 1)
        self.assertEqual(learned[0]["acceptance_level"], "submitted")

    def test_parse_prompt_extracts_job_description_section(self) -> None:
        prompt = """== JOB ==
URL: https://example.com/job
Title: Staff Engineer
Company: Example
Fit Score: 9/10

== JOB DESCRIPTION (use for qualitative answers) ==
Lead backend architecture and mentor engineers.

== FILES ==
Resume PDF (upload this): /tmp/resume.pdf
Cover Letter PDF (upload if asked): /tmp/cover.pdf

== RESUME TEXT (use when filling text fields) ==
Built backend systems.

== COVER LETTER TEXT (paste if text field, upload PDF if file field) ==
I enjoy building resilient services.

== APPLICANT PROFILE ==
Name: Example Candidate
"""
        ctx = local_apply_agent.parse_prompt(prompt)
        self.assertIn("backend architecture", ctx.job_description.lower())

    def test_job_description_for_prompt_is_bounded(self) -> None:
        long_text = "Line about role details. " * 600
        rendered = prompt_mod._job_description_for_prompt({"full_description": long_text}, max_chars=500)
        self.assertIn("Job description truncated", rendered)
        self.assertLess(len(rendered), 700)


if __name__ == "__main__":
    unittest.main()

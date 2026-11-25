"""Tailoring that keeps the person's voice (D75): few rows change, British spelling, a visible before/after."""

from __future__ import annotations

import copy
import json

import httpx
from sqlalchemy.orm import Session

from app.tailor import prompts
from app.tailor.run import write_tailored_cvs
from app.tailor.ui import changes_block
from app.tailor.voice import (
    MAX_REWRITES,
    letter_notes,
    limit_rewrites,
    recommendations,
    rewrites,
    style_notes,
    uk_spelling,
    voice_sample,
)
from tests.test_cv_tailor import BULLET, MASTER, _package, _prepare, _TailorAi
from tests.test_matching import SOON

ROWS = [f"Row {n} about a project with real work number {n}." for n in range(10)]


def _cv(rows: list[str], summary: str = "I build backend services.") -> dict:
    return {
        "personalInfo": {},
        "summary": summary,
        "workExperience": [{"id": 1, "title": "Developer", "company": "Acme", "description": rows}],
        "education": [{"id": 1, "institution": "University of Manchester", "degree": "BSc", "description": ""}],
        "personalProjects": [],
        "additional": {},
    }


def test_rewrites_pair_each_changed_row_with_the_persons_wording() -> None:
    master = _cv(ROWS[:3])
    tailored = _cv([ROWS[0], "Row 1 about a Python project with real work number 1.", ROWS[2]], summary="I build Python backend services.")

    found = rewrites(master, tailored)

    assert [(r.where, r.before) for r in found] == [
        ("Summary", "I build backend services."),
        ("Developer at Acme, bullet 2", ROWS[1]),
    ]
    assert found[1].after.startswith("Row 1 about a Python")


def test_only_a_few_rows_change_and_the_explained_edits_win() -> None:
    master = _cv(ROWS)
    tailored = _cv([row.replace("project", "Python project") for row in ROWS])

    capped, kept = limit_rewrites(master, tailored, first=["workExperience[0].description[9]"])

    assert len(kept) == MAX_REWRITES
    assert "workExperience[0].description[9]" in {r.path for r in kept}
    rows = capped["workExperience"][0]["description"]
    assert sum(row != original for row, original in zip(rows, ROWS, strict=True)) == MAX_REWRITES
    assert rows[9] != ROWS[9]
    assert tailored["workExperience"][0]["description"][8] != ROWS[8]  # The input is not changed in place.


def test_british_spelling_except_where_the_person_wrote_otherwise() -> None:
    data = {"summary": "Optimized queries and analyzed the color of the Organization's data; resized images."}

    fixed, changed = uk_spelling(data)
    kept, _ = uk_spelling(data, protected_text="I optimized everything.")

    assert fixed["summary"] == "Optimised queries and analysed the colour of the Organisation's data; resized images."
    assert changed == ["analyzed", "color", "optimized", "organization"]
    assert kept["summary"].startswith("Optimized queries and analysed")


def test_machine_patterns_become_advice() -> None:
    master = _cv(["Wrote the API.", "Fixed bugs.", "Ran tests.", "Met users."])
    tailored = _cv(
        ["Developed Python APIs here.", "Developed Python tools here.", "Developed Python tests here.", "Developed Python docs here."],
        summary="Python developer using Python.",
    )
    changed = rewrites(master, tailored)
    keywords = {"required_skills": ["Python"], "preferred_skills": [], "keywords": []}

    notes = style_notes(master, tailored, keywords, changed)

    assert any('4 bullets start with "Developed"' in note for note in notes)
    assert any("same length" in note for note in notes)
    assert any('"Python" appears 6 times' in note for note in notes)


def test_stock_letter_phrases_are_flagged() -> None:
    notes = letter_notes("Dear Hiring Manager,\n\nI am thrilled to apply. I am passionate about data.")

    assert any('"i am thrilled"' in note for note in notes)
    assert any('"passionate about"' in note for note in notes)
    assert letter_notes("Dear Hiring Manager,\n\nYour data team caught my eye.") == []


def test_recommendations_name_required_gaps_honestly() -> None:
    coverage = [
        {"term": "Kubernetes", "priority": "required", "status": "missing (gap)"},
        {"term": "teamwork", "priority": "keyword", "status": "missing (gap)"},
        {"term": "SQL", "priority": "required", "status": "missing (have it)"},
    ]

    advice = recommendations(coverage, ["A style note."])

    assert advice[0] == "A style note."
    assert '"Kubernetes"' in advice[1] and "if not, leave it out" in advice[1] and "teamwork" not in advice[1]
    assert '"SQL"' in advice[2]


def test_job_page_shows_before_and_after() -> None:
    block = changes_block(
        {"changes": [{"where": "Summary", "before": "Old <b>", "after": "New"}], "recommendations": ["Add X."]}
    )

    assert "See what changed (1 change)" in block
    assert "<del>Old &lt;b&gt;</del>" in block and "New" in block
    assert "Recommendations (1)" in block and "Add X." in block
    assert "already matched" in changes_block({"changes": []})
    assert changes_block({}) == ""


def test_prompts_no_longer_ask_for_keywords_everywhere() -> None:
    assert "DEFAULT" not in prompts.DIFF_IMPROVE_PROMPT and "DEFAULT" not in prompts.KEYWORD_INJECTION_PROMPT
    assert "EVERY section" not in prompts.KEYWORD_INJECTION_PROMPT
    assert "{max_changes}" in prompts.DIFF_IMPROVE_PROMPT
    assert "{voice_sample}" in prompts.COVER_LETTER_PROMPT


class _VoiceAi(_TailorAi):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: dict[str, str] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "api.anthropic.com" in str(request.url):
            body = json.loads(request.content)
            self.prompts[body.get("system", "")] = body["messages"][0]["content"]
        return super().__call__(request)


async def test_package_keeps_the_changes_and_writes_the_letter_in_the_persons_voice(session: Session) -> None:
    ai = _VoiceAi()
    applicant, gateway, settings = await _prepare(session, ai)

    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)

    row = _package(session, applicant)
    assert row.ats_report["changes"] == [
        {
            "where": "student society, bullet 1",
            "before": BULLET,
            "after": "Led a REST API web service in Python with FastAPI and PostgreSQL for a student society.",
        }
    ]
    assert any('"Kubernetes"' in line for line in row.ats_report["recommendations"])
    letter_prompt = next(text for system, text in ai.prompts.items() if system.startswith("You are a professional career"))
    assert f"- {BULLET}" in letter_prompt
    diff_prompt = next(text for system, text in ai.prompts.items() if system.startswith("You are an expert resume editor"))
    assert f"Reword at most {MAX_REWRITES} pieces of text" in diff_prompt


def test_voice_sample_is_the_persons_own_sentences() -> None:
    master = copy.deepcopy(MASTER)
    master["summary"] = "I like tidy code."

    assert voice_sample(master) == f"- I like tidy code.\n- {BULLET}"

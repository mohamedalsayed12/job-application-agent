"""Mechanical tells that send a tailored line back to the person's own wording."""

import copy

from app.tailor.preservation import finalize_ai_resume
from app.tailor.voice import restore_lines, voice_problems


def _cv(line: str) -> dict:
    return {"summary": "", "workExperience": [{"description": [line]}], "additional": {"technicalSkills": ["Python"]}}


def _rules(master: dict, tailored: dict, posting: str = "") -> set[str]:
    return {item["rule"] for item in voice_problems(master, tailored, posting=posting)}


def test_a_stock_phrase_on_a_changed_line_is_flagged() -> None:
    master = _cv("Built a Python API for the society.")
    tailored = _cv("Led a dynamic, results-driven Python API for the society.")

    assert "stock_phrase" in _rules(master, tailored, posting="Python API")


def test_the_persons_own_wording_is_not_flagged() -> None:
    master = _cv("Built a dynamic Python API for the society.")

    assert voice_problems(master, master, posting="") == []
    changed = _cv("Built a dynamic Python API for the student society.")
    assert "stock_phrase" not in _rules(master, changed)


def test_a_phrase_the_advert_uses_is_not_flagged() -> None:
    master = _cv("Built a Python API for the society.")
    tailored = _cv("Built a results-driven Python API for the society.")

    assert "stock_phrase" not in _rules(master, tailored, posting="We want a results-driven engineer")


def test_a_long_dash_is_flagged() -> None:
    master = _cv("Built a Python API for the society.")
    tailored = _cv("Built a Python API — for the society.")

    assert "long_dash" in _rules(master, tailored)


def test_a_repeated_word_is_flagged() -> None:
    master = _cv("Built a Python API for the society.")
    tailored = _cv("Python Python Python API for the society.")

    assert "stuffing" in _rules(master, tailored)


def test_a_claim_with_no_detail_from_the_cv_is_flagged_and_restored() -> None:
    master = _cv("Built a Python API for the society.")
    tailored = _cv("Improved efficiency across global stakeholders.")
    findings = voice_problems(master, tailored, posting="efficiency")

    assert "no_detail" in {item["rule"] for item in findings}
    restored = restore_lines(master, tailored, findings)
    assert restored["workExperience"][0]["description"] == ["Built a Python API for the society."]


def test_three_bullets_of_one_shape_are_flagged() -> None:
    master = {
        "summary": "",
        "workExperience": [
            {"description": ["Built a short Python API.", "Kept the database small.", "Wrote the test suite."]}
        ],
    }
    tailored = {
        "summary": "",
        "workExperience": [
            {
                "description": [
                    "Built a Python service for students.",
                    "Built a Python service for members.",
                    "Built a Python service for clients.",
                ]
            }
        ],
    }

    assert "uniform" in _rules(master, tailored, posting="Python")


def test_a_moved_bullet_is_kept_when_a_machine_line_is_restored() -> None:
    master = {
        "summary": "",
        "workExperience": [
            {
                "description": [
                    "Led a dynamic Python API for the society.",
                    "Built a payments service in FastAPI.",
                ]
            }
        ],
    }
    tailored = {
        "summary": "",
        "workExperience": [
            {
                "description": [
                    "Built a payments service in FastAPI.",
                    "Delivered a results-driven transformation for stakeholders.",
                ]
            }
        ],
    }

    findings = [item for item in voice_problems(master, tailored, posting="") if item["rule"] != "uniform"]
    restored = restore_lines(master, tailored, findings)
    texts = restored["workExperience"][0]["description"]

    assert "Built a payments service in FastAPI." in texts
    assert "Led a dynamic Python API for the society." in texts


def test_a_restored_line_takes_back_its_own_bullet_and_adds_no_others() -> None:
    master = {
        "summary": "",
        "workExperience": [
            {
                "company": "Society",
                "description": [
                    "Built a Python API for the society.",
                    "Ran the weekly coding club for 30 members.",
                    "Wrote tests for the booking app.",
                ],
            }
        ],
    }
    tailored = {
        "summary": "",
        "workExperience": [
            {
                "company": "Society",
                "description": [
                    "Built a Python REST API for the student society.",
                    "Spearheaded synergies across stakeholders.",
                    "Wrote pytest tests for the booking app.",
                ],
            }
        ],
    }

    findings = [item for item in voice_problems(master, tailored, posting="") if item["rule"] != "uniform"]
    restored = restore_lines(master, tailored, findings)

    assert restored["workExperience"][0]["description"] == [
        "Built a Python REST API for the student society.",
        "Ran the weekly coding club for 30 members.",
        "Wrote pytest tests for the booking app.",
    ]


def test_a_line_is_restored_from_its_own_job_when_jobs_moved() -> None:
    master = {
        "summary": "",
        "workExperience": [
            {"company": "Society", "description": ["Built a Python API for the society."]},
            {"company": "Cafe", "description": ["Served 200 customers a day at the till."]},
        ],
    }
    tailored = {
        "summary": "",
        "workExperience": [
            {"company": "Cafe", "description": ["Spearheaded synergies across stakeholders."]},
            {"company": "Society", "description": ["Built a Python API for the society."]},
        ],
    }

    findings = voice_problems(master, tailored, posting="")
    restored = restore_lines(master, tailored, findings)

    assert [item["path"] for item in findings] == ["workExperience[0].description[0]"] * 2
    assert restored["workExperience"] == [
        {"company": "Cafe", "description": ["Served 200 customers a day at the till."]},
        {"company": "Society", "description": ["Built a Python API for the society."]},
    ]


def test_a_draft_held_to_its_master_still_finds_the_master_entry() -> None:
    master = {
        "summary": "",
        "workExperience": [
            {"company": "Society", "description": ["Built a Python API for the society.", "Ran the coding club."]}
        ],
    }
    candidate = copy.deepcopy(master)
    candidate["workExperience"][0]["description"][1] = "Spearheaded synergies across stakeholders."
    tailored = finalize_ai_resume(master, candidate, allow_review_claims=True)

    findings = voice_problems(master, tailored, posting="")
    restored = restore_lines(master, tailored, findings)

    assert [item["path"] for item in findings] == ["workExperience[0].description[1]"] * 2
    assert restored["workExperience"][0]["description"] == ["Built a Python API for the society.", "Ran the coding club."]

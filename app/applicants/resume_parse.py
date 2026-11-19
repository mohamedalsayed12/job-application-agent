"""Resume-Matcher's CV parser, adapted to suggest profile entries.

Ported from srbhr/Resume-Matcher@959e458 (Apache-2.0, see app/tailor/LICENSE-resume-matcher.txt):
the parse_resume_to_json prompt and schema in apps/backend/app/services/parser.py, which the
tailoring port already holds in app/tailor, and restore_skills_from_markdown from the same file.
Two changes fit it to the profile form: a jobSearch block for target roles, places and level, and
certificationsTraining items that carry issuer, licence and expiry instead of a bare name.
Resume-Matcher's MarkItDown text extraction is not used, because its Word reader parses XML (D38).
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from app.models import ExperienceLevel
from app.tailor.prompts import PARSE_RESUME_PROMPT, PARSE_SYSTEM, RESUME_SCHEMA_EXAMPLE
from app.tailor.resume import RESUME_SCHEMA

PROFILE_RULES = """
Also fill jobSearch, and give certificationsTraining details:
- jobSearch.aimedAt: the one job title this CV is written for, judging by its headline, profile, projects and wording,
  such as "Data Analyst". People keep several CVs, each aimed at a different job. Use "" if it isn't clear.
- jobSearch.targetRoles: up to 3 job titles this person is ready to apply for next, based on their education and
  experience.
- jobSearch.locations: towns, cities or countries the CV gives as where they live or want to work, copied as written.
- jobSearch.experienceLevels: the levels this person should apply at, usually one. "internship" only if they are a
  student asking for an internship or placement; "entry" for graduates, career changers and anyone with under about
  2 years of relevant work; "mid" for about 2 to 5 years; "senior" for more. Empty if the CV doesn't show enough.
- additional.technicalSkills also takes every tool, framework, language and technology named only inside a work or
  project bullet, copied as written. No soft skills.
- additional.certificationsTraining: each certificate and licence, name copied as written. is_licence is true for
  licences such as a driving licence. expires_on is YYYY-MM-DD only if the CV states an expiry date, otherwise "".
- education[].degree: the level and subject only, such as "BSc Business Management". No university, grade or dates.
- Leave a list empty rather than guess."""

_CERTIFICATION = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string"},
        "issuer": {"type": "string"},
        "is_licence": {"type": "boolean"},
        "expires_on": {"type": "string"},
    },
    "required": ["name", "issuer", "is_licence", "expires_on"],
}
_JOB_SEARCH = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "aimedAt": {"type": "string"},
        "targetRoles": {"type": "array", "items": {"type": "string"}},
        "locations": {"type": "array", "items": {"type": "string"}},
        "experienceLevels": {
            "type": "array",
            "items": {"type": "string", "enum": [level.value for level in ExperienceLevel]},
        },
    },
    "required": ["aimedAt", "targetRoles", "locations", "experienceLevels"],
}


def _cv_read_schema() -> dict:
    schema = copy.deepcopy(RESUME_SCHEMA)
    schema["properties"]["additional"]["properties"]["certificationsTraining"] = {"type": "array", "items": _CERTIFICATION}
    schema["properties"]["jobSearch"] = _JOB_SEARCH
    schema["required"].append("jobSearch")
    return schema


def _cv_read_example() -> str:
    example = json.loads(RESUME_SCHEMA_EXAMPLE)
    example["additional"]["certificationsTraining"] = [
        {"name": "AWS Solutions Architect", "issuer": "Amazon Web Services", "is_licence": False, "expires_on": "2027-05-01"}
    ]
    example["jobSearch"] = {
        "aimedAt": "Software Engineer",
        "targetRoles": ["Software Engineer"],
        "locations": ["Manchester"],
        "experienceLevels": ["mid"],
    }
    return json.dumps(example, indent=2)


CV_READ_SCHEMA = _cv_read_schema()
CV_READ_SYSTEM = PARSE_SYSTEM + "\n" + PROFILE_RULES
_EXAMPLE = _cv_read_example()


def parse_prompt(text: str) -> str:
    data = json.dumps({"cv": text}, ensure_ascii=False)
    return PARSE_RESUME_PROMPT.format(schema=_EXAMPLE, resume_text=f"<CV_DATA>\n{data}\n</CV_DATA>")


def profile_payload(parsed: dict[str, Any], also_skills: list[str]) -> dict[str, Any]:
    """The parsed CV as the profile's fields, before grounding. ``also_skills`` come after the parser's own."""
    job_search, additional = parsed["jobSearch"], parsed["additional"]
    return {
        "aimed_at": job_search["aimedAt"],
        "target_roles": job_search["targetRoles"],
        "locations": job_search["locations"],
        "experience_levels": job_search["experienceLevels"],
        "skills": [*additional["technicalSkills"], *also_skills],
        "certifications": additional["certificationsTraining"],
        "degrees": [entry["degree"] for entry in parsed["education"]],
    }


# A source line counts as a skill list when it has at least this many short items
# and at least _MIN_LINE_ANCHORS of them were parsed as skills.
_MIN_SKILL_LINE_ITEMS = 3
_MIN_LINE_ANCHORS = 2
_MAX_SKILL_ITEM_WORDS = 5
_MAX_SKILL_ITEM_CHARS = 40
# A real skill line has far fewer items; longer lines are rejected without being fully split.
_MAX_SKILL_LINE_ITEMS = 200
# No surrounding \s*: it backtracks quadratically on a long run of whitespace. Chunks are stripped instead.
_SKILL_SEPARATOR_RE = re.compile(r"[,|;•·]")
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+●▪◦‣]|\d+[.)])\s+")
_LINE_LABEL_RE = re.compile(r"^[^:,]{1,80}:\s*")
# PDF text extraction renders a visual gap (e.g. after a bold label) as 2+ spaces.
_EXTRACTION_GAP_RE = re.compile(r"\s{2,}")
_FINAL_CONJUNCTION_RE = re.compile(r"^(?:and|or|&)\s+", re.IGNORECASE)
# "Rust and Go" closing a list holds two skills; "&" stays whole: "Drag & Drop", "R & D".
_INLINE_CONJUNCTION_RE = re.compile(r"(?<!\s)\s+(?:and|or)\s+", re.IGNORECASE)
_SKILL_HEADING_RE = re.compile(
    r"^(?:(?:technical|core|key|other)\s+)?"
    r"(?:skills?|technologies|tools|frameworks?|platforms?|tech(?:nology)?\s+stack|stack)$",
    re.IGNORECASE,
)
_PROSE_WORDS = frozenset({"with", "for", "to", "the", "of", "in", "on", "using", "by", "from", "at", "into"})
# Function words that also appear inside names ("Ruby on Rails", "Internet of Things").
_NAME_CONNECTORS = frozenset({"on", "of", "the", "in", "for"})


def _skill_line_items(line: str, anchors: set[str]) -> list[str]:
    """Items of a skill-list line, or [] if the line isn't a list of short items.

    A gap-separated first segment is kept only when it is one of the parsed ``anchors``; otherwise
    it is a label such as "Stack".
    """
    text = _LINE_LABEL_RE.sub("", _LIST_MARKER_RE.sub("", line).strip())
    chunks = _SKILL_SEPARATOR_RE.split(text, maxsplit=_MAX_SKILL_LINE_ITEMS)
    if len(chunks) > _MAX_SKILL_LINE_ITEMS:
        return []
    items: list[str] = []
    for index, chunk in enumerate(chunks):
        segments = [seg for seg in _EXTRACTION_GAP_RE.split(chunk.strip(), maxsplit=_MAX_SKILL_LINE_ITEMS) if seg]
        if index == 0 and len(segments) > 1 and segments[0].casefold() not in anchors:
            segments = segments[1:]
        items.extend(segments)
        if len(items) > _MAX_SKILL_LINE_ITEMS:
            return []
    # Keep a leading dot (".NET"); drop trailing sentence punctuation.
    items = [item for item in (item.strip().rstrip(".").strip() for item in items) if item]
    if items and _SKILL_HEADING_RE.match(items[0]):
        items = items[1:]
    # A conjunction opening an item mid-line, or left dangling at the end, means a sentence wrapped across lines.
    if any(_FINAL_CONJUNCTION_RE.match(item) for item in items[:-1]) or any(
        item.split()[-1].casefold() in {"and", "or", "&"} for item in items
    ):
        return []
    if items:
        last = _FINAL_CONJUNCTION_RE.sub("", items.pop())
        items.extend(_INLINE_CONJUNCTION_RE.split(last, maxsplit=_MAX_SKILL_ITEM_WORDS))
    if len(items) < _MIN_SKILL_LINE_ITEMS:
        return []
    for item in items:
        words = item.split()
        if (
            len(item) > _MAX_SKILL_ITEM_CHARS
            or len(words) > _MAX_SKILL_ITEM_WORDS
            or "@" in item
            or "://" in item
            or _is_prose(words)
        ):
            return []
    return items


def _is_prose(words: list[str]) -> bool:
    """"Built services with Python" is prose; "Ruby on Rails" is a name because its other words are capitalised."""
    function_words = {word.casefold() for word in words} & _PROSE_WORDS
    if not function_words:
        return False
    if function_words - _NAME_CONNECTORS:
        return True
    return not all(not word[0].isalpha() or word[0].isupper() for word in words if word.casefold() not in _PROSE_WORDS)


def restore_skills(parsed: dict[str, Any], text: str) -> dict[str, Any]:
    """Adds skills the parse dropped from the CV's skill-list lines, in CV order.

    A line counts when it is a list of at least three short items and at least two of them were
    parsed as skills, so one match ("Developer, Java, Acme Corp") can't pull in an unrelated line.
    """
    skills = parsed["additional"]["technicalSkills"]
    anchors = {skill.casefold() for skill in skills}
    known = set(anchors)
    restored: list[str] = []
    for line in text.splitlines():
        items = _skill_line_items(line, anchors)
        if sum(item.casefold() in anchors for item in items) < _MIN_LINE_ANCHORS:
            continue
        for item in items:
            if item.casefold() not in known:
                known.add(item.casefold())
                restored.append(item)
    if not restored:
        return parsed
    result = copy.deepcopy(parsed)
    result["additional"]["technicalSkills"] = [*skills, *restored]
    return result

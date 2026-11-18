"""Local refinement passes run after the AI's edits.

Ported from Resume-Matcher's refiner (srbhr/Resume-Matcher@959e458,
apps/backend/app/services/refiner.py). The keyword-injection AI call lives in ``app.tailor.ai``;
everything here is local and free.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from app.tailor.preservation import similarity
from app.tailor.prompts import AI_PHRASE_REPLACEMENTS, PLURAL_REPLACEMENTS
from app.tailor.resume import all_text, stem_in_text, term_in_text

KEYWORD_FIELDS = ("required_skills", "preferred_skills", "keywords")


@dataclass(frozen=True, slots=True)
class KeywordGaps:
    missing: list[str]
    injectable: list[str]
    """Missing from the tailored CV but present in the master, so safe to add back."""
    gaps: list[str]
    """Missing from both: the candidate can't truthfully claim these."""


def job_keyword_terms(keywords: dict[str, Any]) -> list[str]:
    """Distinct posting terms the match is scored on, in posting order."""
    terms: dict[str, None] = {}
    for field in KEYWORD_FIELDS:
        for term in keywords.get(field) or []:
            terms.setdefault(term, None)
    return list(terms)


def keyword_match(resume: dict[str, Any], keywords: dict[str, Any]) -> float:
    terms = job_keyword_terms(keywords)
    if not terms:
        return 0.0
    text = all_text(resume)
    return sum(term_in_text(term, text) for term in terms) / len(terms) * 100


def analyze_keyword_gaps(keywords: dict[str, Any], tailored: dict[str, Any], master: dict[str, Any]) -> KeywordGaps:
    tailored_text, master_text = all_text(tailored), all_text(master)
    missing = [term for term in job_keyword_terms(keywords) if not term_in_text(term, tailored_text)]
    injectable = [term for term in missing if term_in_text(term, master_text)]
    return KeywordGaps(
        missing=missing, injectable=injectable, gaps=[term for term in missing if term not in injectable]
    )


def remove_ai_phrases(data: Any, job_description: str, protected_text: str) -> tuple[Any, list[str]]:
    """Swap stock AI wording for plain words.

    A phrase the posting uses, or the candidate's own master CV uses, is their vocabulary and stays.
    """
    keep = {
        phrase
        for phrase, pattern in _PATTERNS.items()
        if phrase in job_description.lower() or pattern.search(protected_text)
    }
    removed: set[str] = set()

    def clean(text: str) -> str:
        for phrase, pattern in _PATTERNS.items():
            if phrase in keep or not pattern.search(text):
                continue
            removed.add(phrase)
            text = pattern.sub(lambda match, phrase=phrase: _substitute(match, phrase), text)
            if not AI_PHRASE_REPLACEMENTS[phrase]:
                text = re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", text)
                text = re.sub(r"(?<![ \t])[ \t]+([.,;:!?])", r"\1", text)
                text = text.strip(" \t")
        return text

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return clean(value)
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    return walk(data), sorted(removed)


def drop_fabricated_skills(tailored: dict[str, Any], master: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Remove skills the master CV never mentions, even as a variant or in its text."""
    additional = tailored.get("additional") or {}
    master_skills = {skill.lower() for skill in (master.get("additional") or {}).get("technicalSkills") or []}
    master_text = all_text(master).lower()
    kept: list[str] = []
    fabricated: list[str] = []
    for skill in additional.get("technicalSkills") or []:
        lower = skill.lower()
        known = (
            lower in master_skills
            or any(lower in master or master in lower for master in master_skills if master)
            or term_in_text(skill, master_text)
        )
        (kept if known else fabricated).append(skill)
    if not fabricated:
        return tailored, []
    return {**tailored, "additional": {**additional, "technicalSkills": kept}}, fabricated


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Whole-word, case-blind match. A plural "s" and a leading "a"/"an" belong to the match."""
    starts_word, ends_word = phrase[:1].isalnum(), phrase[-1:].isalnum()
    article = r"(?:(?<!\w)(?P<article>an?)\s+)?" if starts_word else ""
    start = r"(?<!\w)" if starts_word else ""
    end = r"(?P<plural>s?)(?!\w)" if ends_word else ""
    return re.compile(f"{article}{start}(?P<phrase>{re.escape(phrase)}){end}", re.IGNORECASE)


# Longest first, so "paradigm shift" is replaced whole before "paradigm" and "---" before "--".
_PATTERNS = {
    phrase: _phrase_pattern(phrase) for phrase in sorted(AI_PHRASE_REPLACEMENTS, key=len, reverse=True)
}


def _substitute(match: re.Match[str], phrase: str) -> str:
    replacement = AI_PHRASE_REPLACEMENTS[phrase]
    plural = match.groupdict().get("plural") or ""
    if plural and phrase in PLURAL_REPLACEMENTS:
        replacement, plural = PLURAL_REPLACEMENTS[phrase], ""
    if replacement and match.group("phrase")[0].isupper():
        replacement = replacement[0].upper() + replacement[1:]
    if replacement:
        replacement += plural
    article = match.groupdict().get("article")
    if not article:
        return replacement
    if not replacement:
        return ""
    wanted = "an" if replacement[0].lower() in "aeiou" else "a"
    return f"{wanted.capitalize() if article[0].isupper() else wanted} {replacement}"


def revert_unsupported_claims(
    tailored: dict[str, Any], master: dict[str, Any], keywords: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Put a row back to the master's wording when it newly names a posting term the master lacks.

    Catches "JavaScript-adjacent" and "to monitor data accuracy": no wording that says the candidate
    does what the posting asks, but the CV never showed, survives.
    """
    master_text = all_text(master)
    gaps = [term for term in job_keyword_terms(keywords) if not (term_in_text(term, master_text) or stem_in_text(term, master_text))]
    if not gaps:
        return tailored, []
    reverted: list[str] = []

    def claims(text: str, source: str) -> list[str]:
        return [term for term in gaps if stem_in_text(term, text) and not stem_in_text(term, source)]

    def restore(text: str, sources: list[str]) -> str:
        source = max(sources, key=lambda row: similarity(row, text), default="")
        found = claims(text, source)
        if not found:
            return text
        reverted.extend(term for term in found if term not in reverted)
        return source

    result = copy.deepcopy(tailored)
    if isinstance(result.get("summary"), str):
        result["summary"] = restore(result["summary"], [str(master.get("summary") or "")])
    for section in ("workExperience", "personalProjects"):
        for entry in result.get(section) or []:
            source_entry = next((e for e in master.get(section) or [] if e.get("id") == entry.get("id")), None)
            sources = [str(row) for row in (source_entry or {}).get("description") or []]
            entry["description"] = [restore(str(row), sources) for row in entry.get("description") or []]
    return result, reverted

"""Hold an AI-authored CV to its source, row by row.

Ported from Resume-Matcher's final AI seam (srbhr/Resume-Matcher@959e458,
apps/backend/app/services/resume_preservation.py) without custom sections, row styles or
skill-row layouts. Identity fields and dates always come from the source. A rewritten row that adds
a number, drifts too far from every source row, or picks up a word from the job posting that the
source CV never uses (such as "agile" in a CV that never mentions it), goes back to the source wording.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any

from app.tailor.resume import LIST_FIELDS, all_text

GROUNDING_THRESHOLD = 0.45

_ENTRY_FIELDS: dict[str, tuple[str, ...]] = {
    "workExperience": ("company", "title"),
    "education": ("institution", "degree"),
    "personalProjects": ("name", "role"),
}
_PROTECTED_FIELDS: dict[str, tuple[str, ...]] = {
    "workExperience": ("id", "title", "company", "location", "years"),
    "education": ("id", "institution", "degree", "years"),
    "personalProjects": ("id", "name", "role", "years"),
}
_TOKEN = re.compile(r"[\w+#./-]+", re.UNICODE)
_NUMBER = re.compile(
    r"(?<![\w.])(?P<currency>[$€£])?(?P<number>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*(?P<unit>thousand|million|billion|percent|times|ms|gb|mb|tb|[kmb%x]))?"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_SCALE = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000, "b": 10**9, "billion": 10**9}
_STOP_WORDS = frozenset(["a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "the", "to", "using", "with"])
_BULLET_MARK = re.compile(r"^\s*(?:[-*•‣◦▪▫]+|\d+[.)])\s*")


def finalize_ai_resume(
    source: dict[str, Any], candidate: dict[str, Any], *, allow_review_claims: bool, posting: str = ""
) -> dict[str, Any]:
    """The candidate CV, repaired so it claims nothing the source does not.

    With ``allow_review_claims`` a weakly grounded rewrite stays; without it, a rewrite less
    similar to its source row than ``GROUNDING_THRESHOLD`` goes back to the source wording.
    With ``posting``, a rewrite that uses a posting word the source CV never uses goes back too:
    tailoring may reword what the CV shows, not borrow the advert's claims.
    """
    borrowed = frozenset(_tokens(posting) - _tokens(all_text(source))) if posting else frozenset()
    result = copy.deepcopy(candidate)
    result["personalInfo"] = copy.deepcopy(source.get("personalInfo", {}))
    source_summary = str(source.get("summary") or "")
    summary = result.get("summary")
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or _novel_numbers(source_summary, summary)
        or _borrows(summary, borrowed)
        or (not allow_review_claims and _similarity(source_summary, summary) < GROUNDING_THRESHOLD)
    ):
        result["summary"] = source_summary
    for section in _ENTRY_FIELDS:
        result[section] = _merge_entries(
            source.get(section), result.get(section), section, allow_review_claims=allow_review_claims, borrowed=borrowed
        )
    result["additional"] = _merge_additional(source.get("additional"), result.get("additional"))
    return result


def _borrows(candidate: str, borrowed: frozenset[str]) -> bool:
    """Whether the rewrite uses a word from the posting that the source CV never uses."""
    return bool(borrowed and _tokens(candidate) & borrowed)


def _merge_entries(
    source_entries: Any,
    candidate_entries: Any,
    section: str,
    *,
    allow_review_claims: bool,
    borrowed: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    sources = [entry for entry in source_entries or [] if isinstance(entry, dict)]
    available = set(range(len(sources)))
    merged_entries: list[dict[str, Any]] = []
    for candidate in candidate_entries if isinstance(candidate_entries, list) else []:
        if not isinstance(candidate, dict):
            continue
        index = _matching_source_index(candidate, sources, available, section)
        if index is None:
            continue
        available.remove(index)
        source = sources[index]
        merged = copy.deepcopy(candidate)
        for field in _PROTECTED_FIELDS[section]:
            merged[field] = copy.deepcopy(source.get(field, ""))
        if section == "education":
            merged["description"] = _merged_text(
                str(source.get("description") or ""), merged.get("description"), allow_review_claims, borrowed
            )
        else:
            merged["description"] = _merge_rows(
                source.get("description"), merged.get("description"), allow_review_claims, borrowed
            )
        merged_entries.append(merged)
    merged_entries += [copy.deepcopy(sources[index]) for index in sorted(available)]
    return merged_entries


def _merged_text(
    source: str, candidate: Any, allow_review_claims: bool, borrowed: frozenset[str] = frozenset()
) -> str:
    if (
        not isinstance(candidate, str)
        or not candidate.strip()
        or _novel_numbers(source, candidate)
        or _borrows(candidate, borrowed)
        or (not allow_review_claims and _similarity(source, candidate) < GROUNDING_THRESHOLD)
    ):
        return source
    return candidate


def _merge_rows(
    source_rows: Any, candidate_rows: Any, allow_review_claims: bool, borrowed: frozenset[str] = frozenset()
) -> list[str]:
    """One row per source row: each candidate row is matched to its closest unused source row."""
    sources = [str(row) for row in source_rows or []]
    source_text = " ".join(sources)
    candidates = _candidate_rows(candidate_rows)
    available = set(range(len(sources)))
    merged: list[str] = []
    for position, candidate in enumerate(candidates[: len(sources)]):
        if not available:
            break
        score, index = max((_similarity(sources[i], candidate), -i) for i in available)
        index = -index
        if not candidate.strip() and position in available:
            index = position
        available.remove(index)
        restore = (
            not candidate.strip()
            or _novel_numbers(source_text, candidate)
            or _borrows(candidate, borrowed)
            or (not allow_review_claims and score < GROUNDING_THRESHOLD)
        )
        merged.append(sources[index] if restore else candidate)
    merged += [sources[index] for index in sorted(available)]
    return merged


def _candidate_rows(value: Any) -> list[str]:
    rows: list[str] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            rows += [line for raw in re.split(r"\r?\n+", item) if (line := _BULLET_MARK.sub("", raw).strip())]
    return rows


def _merge_additional(source: Any, candidate: Any) -> dict[str, list[str]]:
    """Skills may be reordered or gain items; other lists only keep and reorder source items.

    Nothing the source lists is ever dropped.
    """
    source = source if isinstance(source, dict) else {}
    candidate = candidate if isinstance(candidate, dict) else {}
    result: dict[str, list[str]] = {}
    for field in LIST_FIELDS:
        source_items = [item for item in source.get(field) or [] if isinstance(item, str)]
        items = [item for item in candidate.get(field) or [] if isinstance(item, str)]
        if field != "technicalSkills":
            remaining = Counter(_normalized(item) for item in source_items)
            kept = []
            for item in items:
                if remaining[_normalized(item)] > 0:
                    remaining[_normalized(item)] -= 1
                    kept.append(item)
            items = kept
        unplaced = Counter(_normalized(item) for item in items)
        result[field] = list(items)
        for item in source_items:
            if unplaced[_normalized(item)] > 0:
                unplaced[_normalized(item)] -= 1
            else:
                result[field].append(item)
    return result


def _matching_source_index(
    candidate: dict[str, Any], sources: list[dict[str, Any]], available: set[int], section: str
) -> int | None:
    candidate_id = candidate.get("id")
    if isinstance(candidate_id, int) and candidate_id != 0:
        for index in sorted(available):
            if sources[index].get("id") == candidate_id:
                return index
    identity = _field_identity(candidate, section)
    if identity is None:
        return None
    matches = [index for index in sorted(available) if _field_identity(sources[index], section) == identity]
    if len(matches) <= 1:
        return matches[0] if matches else None
    text = _description_text(candidate)
    return max(matches, key=lambda index: (_similarity(_description_text(sources[index]), text), -index))


def _field_identity(entry: dict[str, Any], section: str) -> tuple[str, ...] | None:
    identity = tuple(_normalized(entry.get(field)) for field in _ENTRY_FIELDS[section])
    return identity if any(identity) else None


def _description_text(entry: dict[str, Any]) -> str:
    description = entry.get("description")
    return " ".join(map(str, description)) if isinstance(description, list) else str(description or "")


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _tokens(text: str) -> set[str]:
    return {
        token.casefold()
        for token in _TOKEN.findall(text)
        if len(token) > 1 and token.casefold() not in _STOP_WORDS
    }


def _similarity(left: str, right: str) -> float:
    left, right = _normalized(left), _normalized(right)
    if left == right:
        return 1.0
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    overlap = len(left_tokens & right_tokens) / max(1, len(left_tokens), len(right_tokens))
    sequence = SequenceMatcher(None, left, right).ratio()
    if left_tokens < right_tokens or right_tokens < left_tokens:
        # Character similarity must not hide a material expansion or truncation.
        return overlap * sequence
    return max(overlap, sequence)


def _novel_numbers(source: str, candidate: str) -> bool:
    """Whether the candidate states a number more often than the source does."""
    source_claims, candidate_claims = _numeric_claims(source), _numeric_claims(candidate)
    return any(count > source_claims[claim] for claim, count in candidate_claims.items())


def _numeric_claims(text: str) -> Counter[str]:
    claims: Counter[str] = Counter()
    for match in _NUMBER.finditer(text):
        raw = match.group("number").replace(",", "")
        unit = (match.group("unit") or "").casefold()
        before = text[max(0, match.start() - 16) : match.start()].casefold()
        if re.search(r"(?:python|node(?:\.js)?|java|version|v)\s*$", before):
            continue
        bare_year = not unit and not match.group("currency") and raw.isdigit() and 1900 <= int(raw) <= 2100
        if bare_year and _date_like(text, match):
            continue
        try:
            value = Decimal(raw) * _SCALE.get(unit, 1)
        except InvalidOperation:
            continue
        kind = (
            match.group("currency")
            or ("%" if unit in {"%", "percent"} else "")
            or ("x" if unit in {"x", "times"} else "")
            or (unit if unit in {"ms", "gb", "mb", "tb"} else "")
        )
        claims[f"{kind}:{format(value.normalize(), 'f')}"] += 1
    return claims


def _date_like(text: str, match: re.Match[str]) -> bool:
    before = text[max(0, match.start() - 24) : match.start()].casefold()
    after = text[match.end() : match.end() + 16].casefold()
    return bool(
        re.search(rf"\b{_MONTH}\s*$", before)
        or (
            re.search(r"\b(?:in|since|during|from|until|through|between)\s*$", before)
            and re.match(r"^\s*(?:$|[,.;:)])", after)
        )
        or re.search(r"\b\d{4}\s*[-–—/]\s*$", before)
        or re.match(r"^\s*[-–—/]\s*(?:\d{4}|present|current)\b", after)
    )

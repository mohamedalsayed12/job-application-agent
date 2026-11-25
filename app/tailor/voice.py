"""Keep a tailored CV in the candidate's own voice (D75).

The AI may reword a few rows so they use the job's language. Everything here is local and free: a cap on
how many rows change, British spelling, checks for the patterns that make a CV read as machine-written,
the before/after list shown to the person, and the recommendations that go with it.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from app.tailor.refiner import job_keyword_terms

# Rows (summary, bullets, education notes) the tailoring may reword. The rest stay as the person wrote them.
MAX_REWRITES = 6
_ROW_SECTIONS = ("workExperience", "personalProjects")
_ENTRY_NAME = {"workExperience": ("title", "company"), "personalProjects": ("name", "role")}
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")

# American spellings the AI tends to write. Words ending in -ize/-yze are handled by the patterns below.
_US_TO_UK = {
    "color": "colour", "colors": "colours", "colored": "coloured",
    "behavior": "behaviour", "behaviors": "behaviours", "behavioral": "behavioural",
    "favorite": "favourite", "honor": "honour", "honors": "honours", "labor": "labour",
    "center": "centre", "centers": "centres", "centered": "centred",
    "modeling": "modelling", "modeled": "modelled", "traveling": "travelling", "traveled": "travelled",
    "labeled": "labelled", "labeling": "labelling", "canceled": "cancelled", "canceling": "cancelling",
    "catalog": "catalogue", "catalogs": "catalogues", "dialog": "dialogue",
    "defense": "defence", "offense": "offence", "fulfill": "fulfil", "skillful": "skilful",
    "enrollment": "enrolment", "aging": "ageing", "gray": "grey", "mold": "mould",
}
_IZE = re.compile(r"\b([A-Za-z]{3,}?)iz(e|es|ed|er|ers|ing|ation|ations|ational)\b")
_YZE = re.compile(r"\b(ana|para|cata)lyz(e|es|ed|er|ers|ing)\b", re.IGNORECASE)
_IZE_KEEP = ("size", "prize", "seize", "maize", "baize", "capsiz", "citizen")

# Phrases that mark a cover letter as generated. The prompt forbids them; anything left is flagged.
STOCK_LETTER_PHRASES = (
    "i am writing to express", "i am excited to apply", "i am thrilled", "thrilled to",
    "passionate about", "i am confident that", "aligns perfectly", "perfect fit", "fast-paced",
    "dynamic team", "eager to contribute", "unique blend", "proven track record", "hit the ground running",
    "i would welcome the opportunity", "esteemed", "deeply resonates", "resonates with me", "testament to",
)


@dataclass(frozen=True, slots=True)
class Rewrite:
    path: str
    where: str
    before: str
    after: str

    def to_json(self) -> dict[str, str]:
        return {"where": self.where, "before": self.before, "after": self.after}


def rewrites(master: dict[str, Any], tailored: dict[str, Any]) -> list[Rewrite]:
    """Every row the tailoring reworded, in CV order, each with the person's own wording beside it."""
    found: list[Rewrite] = []
    before, after = str(master.get("summary") or ""), str(tailored.get("summary") or "")
    if after.strip() and _differs(before, after):
        found.append(Rewrite("summary", "Summary", before, after))
    for section in _ROW_SECTIONS:
        sources = [entry for entry in master.get(section) or [] if isinstance(entry, dict)]
        for i, entry in enumerate(tailored.get(section) or []):
            if not isinstance(entry, dict):
                continue
            source = _same_entry(entry, sources, section, i)
            if source is None:
                continue
            originals = [str(row) for row in source.get("description") or []]
            unused = [row for row in originals]
            rows = [str(row) for row in entry.get("description") or []]
            for row in rows:
                if row in unused:
                    unused.remove(row)
            for j, row in enumerate(rows):
                if row in originals or not unused:
                    continue
                closest = max(unused, key=lambda old, row=row: _ratio(old, row))
                unused.remove(closest)
                if _differs(closest, row):
                    found.append(Rewrite(f"{section}[{i}].description[{j}]", _where(entry, section, j), closest, row))
    for i, entry in enumerate(tailored.get("education") or []):
        sources = [e for e in master.get("education") or [] if isinstance(e, dict)]
        source = next(
            (e for e in sources if e.get("institution") == entry.get("institution") and e.get("degree") == entry.get("degree")),
            sources[i] if i < len(sources) else None,
        )
        if source is None:
            continue
        before, after = str(source.get("description") or ""), str(entry.get("description") or "")
        if after.strip() and _differs(before, after):
            found.append(Rewrite(f"education[{i}].description", f"Education: {entry.get('institution') or ''}".strip(), before, after))
    return found


def limit_rewrites(
    master: dict[str, Any], tailored: dict[str, Any], *, limit: int = MAX_REWRITES, first: Iterable[str] = ()
) -> tuple[dict[str, Any], list[Rewrite]]:
    """The tailored CV with at most `limit` rows reworded; the rest go back to the person's wording.

    Rows on the paths in `first` (the edits the AI explained) are kept before any others.
    """
    found = rewrites(master, tailored)
    preferred = set(first)
    ranked = [r for r in found if r.path in preferred] + [r for r in found if r.path not in preferred]
    kept, dropped = ranked[:limit], ranked[limit:]
    if not dropped:
        return tailored, found
    result = copy.deepcopy(tailored)
    for rewrite in dropped:
        _put(result, rewrite.path, rewrite.before)
    order = {r.path: n for n, r in enumerate(found)}
    return result, sorted(kept, key=lambda r: order[r.path])


def uk_spelling(data: Any, protected_text: str = "") -> tuple[Any, list[str]]:
    """British spelling for words the AI wrote. A word the person's own CV uses is theirs and stays."""
    own = {word.lower() for word in _WORD.findall(protected_text)}
    changed: set[str] = set()

    def fix(match: re.Match[str], uk: str) -> str:
        word = match.group(0)
        if word.lower() in own:
            return word
        changed.add(word.lower())
        return uk[0].upper() + uk[1:] if word[0].isupper() else uk

    def clean(text: str) -> str:
        for us, uk in _US_TO_UK.items():
            text = re.sub(rf"\b{us}\b", lambda m, uk=uk: fix(m, uk), text, flags=re.IGNORECASE)
        text = _YZE.sub(lambda m: fix(m, f"{m.group(1).lower()}lys{m.group(2)}"), text)

        def ize(match: re.Match[str]) -> str:
            word = match.group(0)
            if any(keep in word.lower() for keep in _IZE_KEEP):
                return word
            return fix(match, f"{match.group(1)}is{match.group(2)}".lower())

        return _IZE.sub(ize, text)

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return clean(value)
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    return walk(data), sorted(changed)


def style_notes(master: dict[str, Any], tailored: dict[str, Any], keywords: dict[str, Any], changed: list[Rewrite]) -> list[str]:
    """Signs a tailored CV reads as machine-written, as advice the person can act on."""
    notes: list[str] = []
    openings = Counter(_first_word(row) for row in _bullets(tailored) if _first_word(row))
    own_openings = Counter(_first_word(row) for row in _bullets(master) if _first_word(row))
    for word, count in openings.most_common():
        if count >= 3 and count > own_openings.get(word, 0):
            notes.append(f'{count} bullets start with "{word.capitalize()}". Varying the first word reads more naturally.')
    lengths = [len(r.after.split()) for r in changed if r.path != "summary"]
    if len(lengths) >= 4:
        mean = sum(lengths) / len(lengths)
        if mean and all(abs(n - mean) <= mean * 0.15 for n in lengths):
            notes.append("The reworded bullets are all about the same length. Shortening one or two makes the CV read less uniform.")
    text, own_text = _text(tailored), _text(master)
    for term in job_keyword_terms(keywords):
        uses, own_uses = _count(term, text), _count(term, own_text)
        if uses > max(own_uses, 2):
            notes.append(f'"{term}" appears {uses} times. Once or twice is enough for an ATS and reads better.')
    return notes


def letter_notes(letter: str) -> list[str]:
    lower = letter.lower()
    found = [phrase for phrase in STOCK_LETTER_PHRASES if phrase in lower]
    notes = [f'The cover letter uses "{phrase}", a stock phrase; say it in your own words.' for phrase in found]
    openings = [line.split()[0].lower() for line in re.split(r"(?<=[.!?])\s+", letter) if line.split()]
    if len(openings) >= 6 and openings.count("i") > len(openings) / 2:
        notes.append('Most sentences in the cover letter start with "I". Starting some with the work or the employer reads better.')
    return notes


def recommendations(coverage: list[dict[str, str]], notes: list[str]) -> list[str]:
    """What the person could do next, after reading the changes."""
    advice = list(notes)
    gaps = [row["term"] for row in coverage if row.get("status") == "missing (gap)" and row.get("priority") in ("required", "preferred")]
    if gaps:
        advice.append(
            "The advert asks for " + _join(gaps) + ", which your CV does not show. "
            "If you have real experience of it, add a line to your master CV and tailor again; if not, leave it out."
        )
    have = [row["term"] for row in coverage if row.get("status") == "missing (have it)"]
    if have:
        advice.append("Your master CV mentions " + _join(have) + " but this version does not. Check it reads the way you want.")
    return advice


def voice_sample(master: dict[str, Any], limit: int = 8) -> str:
    """The person's own sentences, so a letter can be written the way they write."""
    rows = [str(master.get("summary") or "").strip(), *_bullets(master)[:limit]]
    return "\n".join(f"- {row}" for row in rows if row)


def _same_entry(entry: dict[str, Any], sources: list[dict[str, Any]], section: str, index: int) -> dict[str, Any] | None:
    keys = _ENTRY_NAME[section]
    for source in sources:
        if all(source.get(key) == entry.get(key) for key in keys):
            return source
    return sources[index] if index < len(sources) else None


def _where(entry: dict[str, Any], section: str, row: int) -> str:
    first, second = (str(entry.get(key) or "").strip() for key in _ENTRY_NAME[section])
    name = f"{first} at {second}" if section == "workExperience" and first and second else first or second
    return f"{name or 'Entry'}, bullet {row + 1}"


def _put(data: dict[str, Any], path: str, value: str) -> None:
    current: Any = data
    parts = re.findall(r"([a-zA-Z_]+)(?:\[(\d+)\])?", path)
    for n, (name, index) in enumerate(parts):
        last = n == len(parts) - 1
        if index == "":
            if last:
                current[name] = value
                return
            current = current[name]
        else:
            if last:
                current[name][int(index)] = value
                return
            current = current[name][int(index)]


def _differs(before: str, after: str) -> bool:
    return " ".join(before.split()).casefold() != " ".join(after.split()).casefold()


def _ratio(left: str, right: str) -> float:
    return SequenceMatcher(None, left.lower(), right.lower()).ratio()


def _bullets(data: dict[str, Any]) -> list[str]:
    return [
        str(row)
        for section in _ROW_SECTIONS
        for entry in data.get(section) or []
        if isinstance(entry, dict)
        for row in entry.get("description") or []
    ]


def _first_word(row: str) -> str:
    words = _WORD.findall(row)
    return words[0].lower() if words else ""


def _text(data: dict[str, Any]) -> str:
    return " ".join([str(data.get("summary") or ""), *_bullets(data)])


def _count(term: str, text: str) -> int:
    return len(re.findall(rf"(?<![\w]){re.escape(term)}(?![\w])", text, re.IGNORECASE))


def _join(items: list[str]) -> str:
    quoted = [f'"{item}"' for item in items]
    return quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " and " + quoted[-1]

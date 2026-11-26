"""Keep a tailored CV in the candidate's own voice (D76).

The AI may reword a few rows so they use the job's language. Everything here is local and free: a cap on
how many rows change, British spelling, checks for the patterns that make a CV read as machine-written,
the before/after list shown to the person, and the recommendations that go with it.

Recruiters name the same tells: stock phrases and em dashes (Reed, Gem), bullets of one
shape and length (Reed, Bryq), a word repeated until it stuffs the line (Bryq), and a
claim with none of the person's concrete detail (Sift, Gem). A line the person wrote,
or a phrase the advert itself uses, is not a tell. Flagged lines go back to the person's
own wording after one revise pass.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from app.tailor.preservation import PROTECTED_FIELDS, similarity
from app.tailor.prompts import AI_PHRASE_REPLACEMENTS
from app.tailor.refiner import job_keyword_terms
from app.tailor.resume import all_text

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

_EXTRA = (
    "results-driven",
    "result-driven",
    "detail-oriented",
    "team player",
    "self-starter",
    "passionate about",
    "proven track record",
    "go-getter",
    "delve into",
    "delve",
    "cross-functional",
    "fast-paced",
    "actionable insights",
    "at the intersection of",
    "dynamic",
    "excited to",
    "thrilled to",
)
_STOCK = tuple(sorted(set(AI_PHRASE_REPLACEMENTS) | set(_EXTRA), key=len, reverse=True))
_DASH = re.compile(r"[–—]")
_PATH = re.compile(
    r"^(?P<section>workExperience|personalProjects|education)\[(?P<index>\d+)\]\.description(?:\[(?P<row>\d+)\])?$"
)
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]{3,}|\d+")
_STUFF_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]{3,}")
_STOP = frozenset(
    "with from that this have been were your into also they them just more than over under "
    "about after before their there these those using through where when what which while "
    "would could should and the for are was not you all can will our".split()
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


def voice_problems(master: dict[str, Any], tailored: dict[str, Any], *, posting: str) -> list[dict[str, str]]:
    """Tells on lines the draft changed. The person's own wording is never flagged."""
    posting_l = posting.lower()
    master_text = all_text(master)
    findings: list[dict[str, str]] = []
    bullets: list[tuple[str, str]] = []
    for path, line in _changed(master, tailored):
        if _PATH.fullmatch(path) and path.endswith("]") and "description[" in path:
            bullets.append((path, line))
        for rule, detail in _line_rules(line, posting_l, master_text):
            findings.append({"rule": rule, "path": path, "line": line, "detail": detail})
    if _uniform([line for _, line in bullets]):
        findings.extend(
            {"rule": "uniform", "path": path, "line": line, "detail": "same shape"} for path, line in bullets
        )
    return findings


def restore_lines(master: dict[str, Any], tailored: dict[str, Any], findings: list[dict[str, str]]) -> dict[str, Any]:
    """Put each flagged line back to the line on the person's CV it was rewritten from."""
    result = copy.deepcopy(tailored)
    restored_entries: set[tuple[str, int]] = set()
    for finding in findings:
        path = finding["path"]
        if path == "summary":
            result["summary"] = master.get("summary") or ""
            continue
        match = _PATH.fullmatch(path)
        if match is None:
            continue
        section = match.group("section")
        index = int(match.group("index"))
        targets = result.get(section) or []
        sources = _sources(master, tailored, section)
        if index >= len(targets) or sources[index] is None:
            continue
        source = sources[index]
        if match.group("row") is None:
            targets[index]["description"] = source.get("description") or ""
            continue
        if (section, index) in restored_entries:
            continue
        restored_entries.add((section, index))
        flagged = {finding["line"] for finding in findings if finding["path"].startswith(f"{section}[{index}].")}
        origins = _origins(source.get("description") or [], targets[index].get("description") or [])
        targets[index]["description"] = [
            origin if line in flagged else line
            for line, origin in origins
            if line not in flagged or origin is not None
        ]
    return result


def _sources(master: dict[str, Any], tailored: dict[str, Any], section: str) -> list[dict[str, Any] | None]:
    """The master entry behind each tailored entry, matched on the fields the draft copies from it."""
    def identity(entry: dict[str, Any]) -> list[str]:
        # The draft holds "" where the master has no such field.
        return [str(entry.get(field) or "") for field in PROTECTED_FIELDS[section]]

    unused = list(master.get(section) or [])
    found: list[dict[str, Any] | None] = []
    for entry in tailored.get(section) or []:
        source = next((item for item in unused if identity(item) == identity(entry)), None)
        if source is not None:
            unused.remove(source)
        found.append(source)
    return found


def _origins(source_rows: list[Any], target_rows: list[Any]) -> list[tuple[str, str | None]]:
    """Each tailored bullet with the person's bullet it came from: itself when unchanged, else the
    closest one left, closest pairs first so a vague rewrite gets the bullet no other rewrite claims."""
    targets = [str(row) for row in target_rows]
    unused = Counter(str(row) for row in source_rows)
    origins: list[str | None] = [None] * len(targets)
    for position, row in enumerate(targets):
        if unused[row]:
            unused[row] -= 1
            origins[position] = row
    left = list(unused.elements())
    rewrites_idx = [position for position, origin in enumerate(origins) if origin is None]
    ranked = sorted(
        (
            (similarity(left[source], targets[position]), position, source)
            for position in rewrites_idx
            for source in range(len(left))
        ),
        key=lambda item: -item[0],
    )
    taken: set[int] = set()
    for _, position, source in ranked:
        if origins[position] is None and source not in taken:
            origins[position] = left[source]
            taken.add(source)
    return list(zip(targets, origins, strict=True))


def _changed(master: dict[str, Any], tailored: dict[str, Any]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    original_summary = str(master.get("summary") or "")
    summary = str(tailored.get("summary") or "")
    if summary.strip() and summary != original_summary:
        found.append(("summary", summary))
    for section in ("workExperience", "personalProjects", "education"):
        sources = _sources(master, tailored, section)
        for index, entry in enumerate(tailored.get(section) or []):
            source = sources[index] or {}
            if section == "education":
                original = str(source.get("description") or "")
                line = str(entry.get("description") or "")
                if line.strip() and line != original:
                    found.append((f"education[{index}].description", line))
                continue
            # A bullet that still equals one the person wrote is theirs, even if it moved slots.
            unused = Counter(str(item) for item in (source.get("description") or []))
            for row, line in enumerate(entry.get("description") or []):
                text = str(line)
                if unused[text]:
                    unused[text] -= 1
                    continue
                found.append((f"{section}[{index}].description[{row}]", text))
    return found


def _line_rules(line: str, posting_l: str, master_text: str):
    for phrase in _STOCK:
        if _has(line, phrase) and not _has(master_text, phrase) and phrase not in posting_l:
            yield "stock_phrase", phrase
            break
    if _DASH.search(line) and not _DASH.search(master_text):
        yield "long_dash", "dash"
    stuffed = _stuffed(line)
    if stuffed:
        yield "stuffing", stuffed
    if _vague(line, master_text):
        yield "no_detail", "no concrete detail from the CV"


def _has(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE) is not None


def _stuffed(line: str) -> str:
    counts = Counter(word for word in _STUFF_WORD.findall(line.casefold()) if word not in _STOP)
    for word, count in counts.items():
        if count >= 3:
            return word
    return ""


def _vague(line: str, master_text: str) -> bool:
    master_tokens = {token.casefold() for token in _TOKEN.findall(master_text) if token.casefold() not in _STOP}
    line_tokens = {token.casefold() for token in _TOKEN.findall(line) if token.casefold() not in _STOP}
    return not (line_tokens & master_tokens)


def _uniform(lines: list[str]) -> bool:
    """Three or more bullets, one length and one shape."""
    if len(lines) < 3:
        return False
    lengths = [len(line) for line in lines]
    if max(lengths) - min(lengths) > 12:
        return False
    counts = [len(line.split()) for line in lines]
    return max(counts) - min(counts) <= 1


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

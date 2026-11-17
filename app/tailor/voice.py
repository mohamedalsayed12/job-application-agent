"""Flag tailored lines that still sound machine-written, then put the person's own line back.

Recruiters name the same tells: stock phrases and em dashes (Reed, Gem), bullets of one
shape and length (Reed, Bryq), a word repeated until it stuffs the line (Bryq), and a
claim with none of the person's concrete detail (Sift, Gem). A line the person wrote,
or a phrase the advert itself uses, is not a tell.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from typing import Any

from app.tailor.prompts import AI_PHRASE_REPLACEMENTS
from app.tailor.resume import all_text

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
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]{3,}")
_STOP = frozenset(
    "with from that this have been were your into also they them just more than over under "
    "about after before their there these those using through where when what which while "
    "would could should and the for are was not you all can will our".split()
)


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
    """Put each flagged line back to the wording on the person's CV."""
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
        sources = master.get(section) or []
        targets = result.get(section) or []
        if index >= len(sources) or index >= len(targets):
            continue
        row = match.group("row")
        if row is None:
            targets[index]["description"] = sources[index].get("description") or ""
            continue
        if (section, index) in restored_entries:
            continue
        restored_entries.add((section, index))
        source_rows = [str(item) for item in (sources[index].get("description") or [])]
        target_rows = [str(item) for item in (targets[index].get("description") or [])]
        flagged = {finding["line"] for finding in findings if finding["path"].startswith(f"{section}[{index}].")}
        missing = Counter(source_rows)
        for line in target_rows:
            if missing[line]:
                missing[line] -= 1
        replacements = list(missing.elements())
        cursor = 0
        kept: list[str] = []
        for line in target_rows:
            if line not in flagged:
                kept.append(line)
                continue
            if cursor < len(replacements):
                kept.append(replacements[cursor])
                cursor += 1
        kept.extend(replacements[cursor:])
        targets[index]["description"] = kept
    return result


def _changed(master: dict[str, Any], tailored: dict[str, Any]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    original_summary = str(master.get("summary") or "")
    summary = str(tailored.get("summary") or "")
    if summary.strip() and summary != original_summary:
        found.append(("summary", summary))
    for section in ("workExperience", "personalProjects", "education"):
        sources = master.get(section) or []
        for index, entry in enumerate(tailored.get(section) or []):
            source = sources[index] if index < len(sources) else {}
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
    counts = Counter(word for word in _WORD.findall(line.casefold()) if word not in _STOP)
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

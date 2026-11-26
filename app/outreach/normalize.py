"""Turn Apify people rows into contacts we can store."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.models import OutreachRoleKind

_LINKEDIN_HOSTS = frozenset({"linkedin.com", "www.linkedin.com"})
_TITLE_KINDS: tuple[tuple[re.Pattern[str], OutreachRoleKind], ...] = (
    (re.compile(r"\b(ceo|cto|cfo|coo|chief|founder|co-?founder|president)\b", re.I), OutreachRoleKind.executive),
    (re.compile(r"\b(hiring\s*manager|head of|director of|vp of|vice president)\b", re.I), OutreachRoleKind.hiring_manager),
    (re.compile(r"\b(recruiter|talent\s*acquisition|talent\s*partner|sourcer)\b", re.I), OutreachRoleKind.recruiter),
)


@dataclass(frozen=True, slots=True)
class FoundContact:
    full_name: str
    title: str | None
    company_name: str | None
    linkedin_url: str | None
    email: str | None
    role_kind: OutreachRoleKind
    evidence: dict[str, Any]
    source_actor: str


def linkedin_job_url(apply_url: str) -> str | None:
    try:
        parsed = urlparse(apply_url.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host not in _LINKEDIN_HOSTS:
        return None
    if "/jobs/" not in parsed.path.lower():
        return None
    return apply_url.strip()


def normalize_people(items: list[dict[str, Any]], *, actor: str, company_fallback: str | None = None) -> list[FoundContact]:
    found: list[FoundContact] = []
    seen: set[str] = set()
    for item in items:
        contact = _one(item, actor=actor, company_fallback=company_fallback)
        if contact is None:
            continue
        key = (contact.linkedin_url or "").lower() or f"name:{(contact.full_name or '').lower()}"
        if key in seen:
            continue
        seen.add(key)
        found.append(contact)
    return found


def normalize_research(item: dict[str, Any]) -> tuple[str | None, list[dict[str, str]]]:
    about = _text(
        item.get("about")
        or item.get("summary")
        or item.get("aboutText")
        or item.get("description")
    )
    quotes: list[dict[str, str]] = []
    for post in _list(item.get("posts") or item.get("recentPosts") or item.get("activity")):
        text = _text(post.get("text") or post.get("commentary") or post.get("content") if isinstance(post, dict) else post)
        if not text:
            continue
        url = ""
        if isinstance(post, dict):
            url = _text(post.get("url") or post.get("postUrl") or post.get("link")) or ""
        quotes.append({"text": text[:500], "source_url": url})
    hooks = item.get("outreachHooks") or item.get("hooks") or item.get("quotes")
    for hook in _list(hooks):
        text = _text(hook.get("text") or hook.get("quote") if isinstance(hook, dict) else hook)
        if text:
            quotes.append({"text": text[:500], "source_url": ""})
    if about and not any(q["text"] == about[:500] for q in quotes):
        # Keep about separate; only short distinctive lines become quotes when no posts.
        for line in about.splitlines():
            line = line.strip()
            if len(line) >= 40:
                quotes.append({"text": line[:500], "source_url": ""})
                break
    return about, quotes[:8]


def role_kind_for(title: str | None) -> OutreachRoleKind:
    if not title:
        return OutreachRoleKind.unknown
    for pattern, kind in _TITLE_KINDS:
        if pattern.search(title):
            return kind
    return OutreachRoleKind.employee


def _one(item: dict[str, Any], *, actor: str, company_fallback: str | None) -> FoundContact | None:
    name = _text(
        item.get("fullName")
        or item.get("name")
        or item.get("full_name")
        or " ".join(p for p in (_text(item.get("firstName")), _text(item.get("lastName"))) if p)
    )
    if not name or name.lower() == "linkedin member":
        return None
    position = _current_position(item)
    title = _text(
        item.get("title") or position.get("title") or item.get("headline") or item.get("currentTitle") or item.get("position")
    )
    company = (
        _company_text(item.get("company") or item.get("companyName") or item.get("currentCompany"))
        or _company_text(position.get("companyName") or position.get("company"))
        or company_fallback
    )
    url = _linkedin_url(
        item.get("linkedinUrl")
        or item.get("profileUrl")
        or item.get("url")
        or item.get("linkedin_url")
        or item.get("profile_url")
    )
    email = _email(item.get("email") or item.get("workEmail") or item.get("verifiedEmail"))
    kind_hint = _text(item.get("roleKind") or item.get("role") or item.get("type"))
    kind = _kind_from_hint(kind_hint) if kind_hint else role_kind_for(title)
    evidence = {
        "snippets": [s for s in (_text(item.get("headline")), _text(item.get("hiringNiche")), title) if s][:3],
        "raw_keys": sorted(item.keys())[:20],
    }
    return FoundContact(
        full_name=name,
        title=title,
        company_name=company,
        linkedin_url=url,
        email=email,
        role_kind=kind,
        evidence=evidence,
        source_actor=actor,
    )


def _current_position(item: dict[str, Any]) -> dict[str, Any]:
    """The job a people row says the person holds now (harvestapi rows keep it in currentPositions)."""
    positions = [p for p in _list(item.get("currentPositions")) if isinstance(p, dict)]
    current = [p for p in positions if p.get("current") is not False]
    return (current or positions or [{}])[0]


def _company_text(value: object) -> str | None:
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("companyName"))
    return _text(value)


_COMPANY_SUFFIX = re.compile(r"\b(?:ltd|limited|plc|llp|llc|inc|incorporated|corp|corporation)\b")


def company_key(name: str | None) -> str:
    """A company name reduced so "TPP Ltd" and "tpp" compare equal, but "TPP Retail" does not."""
    words = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    return " ".join(_COMPANY_SUFFIX.sub(" ", words).split())


_BRACKETED = re.compile(r"\(([^()]*)\)")


def _company_keys(name: str) -> set[str]:
    """ "TPP (The Phoenix Partnership)" answers to its whole name, "TPP" and "The Phoenix Partnership"."""
    keys = {company_key(name), company_key(_BRACKETED.sub(" ", name))}
    keys.update(company_key(inside) for inside in _BRACKETED.findall(name))
    return {key for key in keys if key}


def works_at(contact: FoundContact, company: str) -> bool:
    """False only when the row names a different current employer: a search by name finds namesakes.

    Two firms can share a name exactly ("TPP Ltd"), so a match here is likely, not proven.
    """
    return not contact.company_name or bool(_company_keys(contact.company_name) & _company_keys(company))


def _kind_from_hint(hint: str) -> OutreachRoleKind:
    lowered = hint.lower().replace(" ", "_")
    for kind in OutreachRoleKind:
        if kind.value in lowered or kind.name in lowered:
            return kind
    return role_kind_for(hint)


def _linkedin_url(value: object) -> str | None:
    text = _text(value)
    if not text:
        return None
    if text.startswith("/in/"):
        text = f"https://www.linkedin.com{text}"
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host not in _LINKEDIN_HOSTS:
        return None
    if "/in/" not in parsed.path.lower() and "/pub/" not in parsed.path.lower():
        return None
    return f"https://www.linkedin.com{parsed.path.rstrip('/')}"


_EMAIL = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$", re.I)


def parse_email(value: object) -> str | None:
    text = _text(value)
    if not text or not _EMAIL.fullmatch(text):
        return None
    return text.lower()


def _email(value: object) -> str | None:
    return parse_email(value)


def _text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []

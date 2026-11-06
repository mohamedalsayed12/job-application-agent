"""Parse Greenhouse / Lever board IDs from result URLs (T10)."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

from app.boards.ids import normalize_board_id
from app.boards.result import BoardInstance, BoardSystem
from app.extraction.types import BoardRef

_GREENHOUSE_HOSTS = frozenset(
    {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "boards.eu.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }
)
_LEVER_GLOBAL_HOSTS = frozenset({"jobs.lever.co"})
_LEVER_EU_HOSTS = frozenset({"jobs.eu.lever.co"})
_SKIP_SEGMENTS = frozenset(
    {
        "embed",
        "jobs",
        "job",
        "apply",
        "job_app",
        "job_board",
        "v1",
        "boards",
        ".",
        "..",
    }
)


def parse_board_from_url(url: str) -> BoardRef | None:
    """Return a board ref only when the board id appears in the URL itself.

    Bare ``gh_jid`` on a non-Greenhouse host is not enough: inventing a board
    id would break T10 (board ids come only from code matching on real URLs).
    """
    raw = url.strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if host in _GREENHOUSE_HOSTS:
        return _greenhouse_board(parsed)
    if host in _LEVER_GLOBAL_HOSTS:
        return _lever_board(parsed, instance="lever_global")
    if host in _LEVER_EU_HOSTS:
        return _lever_board(parsed, instance="lever_eu")
    return None


def _greenhouse_board(parsed) -> BoardRef | None:
    segments = [unquote(s) for s in parsed.path.split("/") if s]
    if segments and segments[0].lower() == "embed":
        query = parse_qs(parsed.query)
        for_values = query.get("for") or []
        if not for_values:
            return None
        return _board("greenhouse", "greenhouse", for_values[0])

    if not segments:
        return None
    token = segments[0]
    if token.lower() in _SKIP_SEGMENTS:
        return None
    return _board("greenhouse", "greenhouse", token)


def _lever_board(parsed, *, instance: BoardInstance) -> BoardRef | None:
    segments = [unquote(s) for s in parsed.path.split("/") if s]
    if not segments:
        return None
    site = segments[0]
    if site.lower() in _SKIP_SEGMENTS:
        return None
    return _board("lever", instance, site)


def _board(
    system: BoardSystem, instance: BoardInstance, raw_id: str
) -> BoardRef | None:
    board_id = normalize_board_id(raw_id)
    if board_id is None:
        return None
    # Live unique key is case-sensitive text; canonicalize to lowercase.
    return BoardRef(system=system, instance=instance, board_id=board_id.lower())

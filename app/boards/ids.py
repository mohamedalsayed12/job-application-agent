"""Board / site id validation at the client trust boundary."""

from __future__ import annotations


def normalize_board_id(board_id: str) -> str | None:
    """Return a safe path segment, or None if the id must not be requested."""
    value = board_id.strip()
    if not value:
        return None
    if any(ch in value for ch in ("/", "\\", "?", "#", "&", " ")):
        return None
    if ".." in value:
        return None
    return value

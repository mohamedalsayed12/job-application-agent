"""Admin key gate until Milestone 2 accounts exist."""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Query, status

from app.core.config import Settings, get_settings


def _matches(provided: str | None, settings: Settings) -> bool:
    expected = settings.admin_key or ""
    if not expected:
        return False
    return secrets.compare_digest(provided or "", expected)


def require_admin_key(
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    admin_key: str | None = Query(default=None),
) -> str:
    """Accept ``X-Admin-Key`` header or ``admin_key`` query (HTML forms). Returns the key."""
    settings = get_settings()
    provided = x_admin_key if x_admin_key is not None else admin_key
    if not _matches(provided, settings):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing admin key",
        )
    assert provided is not None
    return provided

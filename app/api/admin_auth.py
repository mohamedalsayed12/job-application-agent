"""Who counts as an admin: an admin session in the browser, or the key in a header for scripts.

The key is never accepted in a URL, because URLs end up in logs and browser history (D38).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import Header, HTTPException, Request, status

from app.core.config import get_settings

ADMIN_SESSION_SECONDS = 12 * 60 * 60


def admin_key_matches(provided: str | None) -> bool:
    expected = get_settings().admin_key or ""
    if not expected:
        return False
    return secrets.compare_digest((provided or "").encode(), expected.encode())


def _session_mark() -> str:
    """Goes in the cookie instead of the key. Changing the key ends every admin session."""
    settings = get_settings()
    return hmac.new(settings.session_secret.encode(), settings.admin_key.encode(), hashlib.sha256).hexdigest()


def start_admin_session(request: Request) -> None:
    request.session.pop("csrf", None)
    request.session["admin"] = _session_mark()
    request.session["admin_until"] = int(time.time()) + ADMIN_SESSION_SECONDS


def end_admin_session(request: Request) -> None:
    request.session.pop("admin", None)
    request.session.pop("admin_until", None)


def _has_admin_session(request: Request) -> bool:
    mark = request.session.get("admin")
    return (
        bool(get_settings().admin_key)
        and isinstance(mark, str)
        and secrets.compare_digest(mark.encode(), _session_mark().encode())
        and request.session.get("admin_until", 0) > time.time()
    )


def require_admin(request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> None:
    allowed = admin_key_matches(x_admin_key) if x_admin_key is not None else _has_admin_session(request)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin access needed. Log in at /admin/login.",
        )

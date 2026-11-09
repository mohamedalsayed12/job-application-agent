"""Form tokens that stop another website from submitting a form as a logged-in person (D38)."""

from __future__ import annotations

import html
import secrets

from fastapi import Request
from fastapi.responses import HTMLResponse

from app.api.admin_auth import admin_key_matches
from app.api.admin_html import page

_MAX_FORM_FILES = 1
_MAX_FORM_FIELDS = 200


class FormRefused(Exception):
    """A form submission that can't go ahead, shown to the person as a page."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def form_refused_page(_: Request, exc: FormRefused) -> HTMLResponse:
    return page("That didn't work", f'<h1>That didn\'t work</h1><p class="error">{html.escape(exc.message)}</p>', status_code=exc.status_code)


def csrf_field(request: Request) -> str:
    token = request.session.setdefault("csrf", secrets.token_urlsafe(32))
    return f'<input type="hidden" name="csrf_token" value="{token}"/>'


async def require_csrf(request: Request) -> None:
    """On every POST route. A valid admin-key header is exempt: other sites can't set that header."""
    if admin_key_matches(request.headers.get("X-Admin-Key")):
        return
    form = await request.form(max_files=_MAX_FORM_FILES, max_fields=_MAX_FORM_FIELDS)
    expected = request.session.get("csrf")
    provided = form.get("csrf_token")
    if (
        not isinstance(expected, str)
        or not isinstance(provided, str)
        or not secrets.compare_digest(provided.encode(), expected.encode())
    ):
        raise FormRefused(403, "This form has expired. Go back, reload the page and try again.")

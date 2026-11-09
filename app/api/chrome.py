"""The top bars: one for applicants, one for admins. Log out is a form, so each bar needs the request."""

from __future__ import annotations

from fastapi import Request

from app.api.forms import csrf_field

_APPLICANT_LINKS = (("matches", "Matches", "/listings"), ("profile", "Profile", "/profile"), ("account", "Account", "/account"))
_ADMIN_LINKS = (
    ("quality", "Quality", "/admin/quality"),
    ("discovery", "Discovery", "/admin/quality#discovery"),
    ("review", "Review queue", "/admin/review"),
    ("applicants", "Applicants", "/admin/applicants"),
    ("audit", "Audit", "/admin/audit"),
)


def _bar(request: Request, *, links: tuple[tuple[str, str, str], ...], active: str, brand: str, logout: str, admin: bool) -> str:
    current = ' class="active"'
    items = "".join(f'<a href="{href}"{current if key == active else ""}>{label}</a>' for key, label, href in links)
    return f"""<header class="topbar{" admin" if admin else ""}"><div class="topbar-in">
<a class="brand" href="{links[0][2]}"><span class="brand-mark">J</span>{brand}</a>
<nav class="nav">{items}<form method="post" action="{logout}">{csrf_field(request)}<button type="submit">Log out</button></form></nav>
</div></header>"""


def applicant_bar(request: Request, active: str) -> str:
    return _bar(request, links=_APPLICANT_LINKS, active=active, brand="Job Agent", logout="/logout", admin=False)


def admin_bar(request: Request, active: str) -> str:
    return _bar(request, links=_ADMIN_LINKS, active=active, brand="Job Agent · Admin", logout="/admin/logout", admin=True)


def public_bar() -> str:
    return """<header class="topbar"><div class="topbar-in">
<a class="brand" href="/login"><span class="brand-mark">J</span>Job Agent</a>
<nav class="nav"><a href="/login">Log in</a><a href="/register">Register</a></nav>
</div></header>"""

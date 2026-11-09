"""Register, log in, log out, delete an account, and admin login (T29, T32)."""

from __future__ import annotations

import html
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.accounts.service import (
    AccountError,
    InvalidLinkError,
    applicant_for_token,
    authenticate,
    delete_account,
    end_all_sessions,
    register,
    set_password,
    set_password_token,
)
from app.api.admin_auth import (
    admin_key_matches,
    end_admin_session,
    require_admin,
    start_admin_session,
)
from app.api.admin_html import page
from app.api.chrome import admin_bar, applicant_bar, public_bar
from app.api.forms import csrf_field, require_csrf
from app.core.db import get_db
from app.models import Applicant

router = APIRouter(tags=["accounts"])
admin_session_router = APIRouter(prefix="/admin", tags=["admin-accounts"])
admin_router = APIRouter(prefix="/admin/accounts", tags=["admin-accounts"], dependencies=[Depends(require_admin)])

_LOGIN_FAILED = "Email or password is wrong."
_TOO_MANY = "Too many attempts. Wait a while and try again."
_SEE_OTHER = status.HTTP_303_SEE_OTHER
_CSRF = [Depends(require_csrf)]


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _error(message: str | None) -> str:
    return f'<p class="error">{html.escape(message)}</p>' if message else ""


def _credentials_page(
    request: Request,
    title: str,
    path: str,
    *,
    new_password: bool,
    error: str | None = None,
    email: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    other = (
        '<p>Already registered? <a href="/login">Log in</a></p>'
        if new_password
        else '<p>New here? <a href="/register">Register</a></p>'
    )
    return page(
        title,
        f"""
<div class="card">
<h1>{html.escape(title)}</h1>
{_error(error)}
<form method="post" action="{path}">
  {csrf_field(request)}
  <label>Email <input name="email" type="email" required autocomplete="username" value="{html.escape(email)}"/></label>
  <label>Password
    <input name="password" type="password" required
      autocomplete="{'new-password' if new_password else 'current-password'}"/>
  </label>
  <p><button type="submit" class="primary">{html.escape(title)}</button></p>
</form>
{other}
</div>
""",
        status_code=status_code,
        header=public_bar(),
        narrow=True,
    )


def _start_session(request: Request, applicant: Applicant) -> RedirectResponse:
    request.session.clear()
    request.session["applicant"] = str(applicant.id)
    request.session["version"] = applicant.session_version
    return RedirectResponse("/account", status_code=_SEE_OTHER)


def current_applicant(request: Request, db: Session = Depends(get_db)) -> Applicant | None:
    applicant_id = request.session.get("applicant")
    if not applicant_id:
        return None
    applicant = db.get(Applicant, uuid.UUID(applicant_id))
    if applicant is None or not applicant.is_active or applicant.session_version != request.session.get("version"):
        return None
    return applicant


def require_login(applicant: Applicant | None = Depends(current_applicant)) -> Applicant:
    if applicant is None:
        raise HTTPException(status_code=_SEE_OTHER, headers={"Location": "/login"})
    return applicant


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request) -> HTMLResponse:
    return _credentials_page(request, "Register", "/register", new_password=True)


@router.post("/register", response_class=HTMLResponse, dependencies=_CSRF)
def register_submit(
    request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)
) -> Response:
    if not throttle.take_register_attempt(_client_ip(request)):
        return _credentials_page(
            request, "Register", "/register", new_password=True, error=_TOO_MANY, email=email, status_code=429
        )
    try:
        applicant = register(db, email=email, password=password)
        db.commit()
    except AccountError as exc:
        db.rollback()
        return _credentials_page(
            request, "Register", "/register", new_password=True, error=str(exc), email=email, status_code=400
        )
    return _start_session(request, applicant)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return _credentials_page(request, "Log in", "/login", new_password=False)


@router.post("/login", response_class=HTMLResponse, dependencies=_CSRF)
def login_submit(
    request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)
) -> Response:
    ip = _client_ip(request)
    if not throttle.login_allowed(email, ip):
        return _credentials_page(
            request, "Log in", "/login", new_password=False, error=_TOO_MANY, email=email, status_code=429
        )
    applicant = authenticate(db, email=email, password=password)
    if applicant is None:
        throttle.record_failed_login(email, ip)
        return _credentials_page(
            request, "Log in", "/login", new_password=False, error=_LOGIN_FAILED, email=email, status_code=401
        )
    db.commit()
    return _start_session(request, applicant)


@router.post("/logout", dependencies=_CSRF)
def logout(
    request: Request,
    applicant: Applicant | None = Depends(current_applicant),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Ends the session on every device, so a copied cookie stops working too."""
    if applicant is not None:
        end_all_sessions(applicant)
        db.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=_SEE_OTHER)


def _account_page(
    request: Request, applicant: Applicant, *, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    return page(
        "Your account",
        f"""
<h1>Your account</h1>
<div class="card">
<p>Logged in as {html.escape(applicant.email)}.</p>
<p><a href="/listings">Your job matches</a> · <a href="/profile">Your profile and CV</a></p>
<form method="post" action="/logout">{csrf_field(request)}<button type="submit">Log out</button></form>
</div>
<div class="card" id="delete">
<h2>Delete your account</h2>
<p>This permanently removes your profile, CV, preferences and job matches, and can't be undone.</p>
{_error(error)}
<form method="post" action="/account/delete">
  {csrf_field(request)}
  <label>Enter your password to confirm
    <input name="password" type="password" required autocomplete="current-password"/>
  </label>
  <p><button type="submit" class="danger">Delete my account</button></p>
</form>
</div>
""",
        status_code=status_code,
        header=applicant_bar(request, "account"),
        narrow=True,
    )


@router.get("/account", response_class=HTMLResponse)
def account(request: Request, applicant: Applicant = Depends(require_login)) -> HTMLResponse:
    return _account_page(request, applicant)


@router.post("/account/delete", response_class=HTMLResponse, dependencies=_CSRF)
def account_delete(
    request: Request,
    password: str = Form(...),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    email, ip = applicant.email, _client_ip(request)
    if not throttle.login_allowed(email, ip):
        return _account_page(request, applicant, error=_TOO_MANY, status_code=429)
    if authenticate(db, email=email, password=password) is None:
        throttle.record_failed_login(email, ip)
        return _account_page(request, applicant, error="That password is wrong.", status_code=401)
    delete_account(db, applicant)
    db.commit()
    request.session.clear()
    return page(
        "Account deleted",
        '<div class="card"><h1>Your account has been deleted</h1><p><a href="/register">Register again</a></p></div>',
        header=public_bar(),
        narrow=True,
    )


def _set_password_page(request: Request, token: str, *, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    response = page(
        "Set your password",
        f"""
<h1>Set your password</h1>
{_error(error)}
<form method="post" action="/set-password">
  {csrf_field(request)}
  <input type="hidden" name="token" value="{html.escape(token)}"/>
  <label>New password <input name="password" type="password" required autocomplete="new-password"/></label>
  <p><button type="submit" class="primary">Set password</button></p>
</form>
""",
        status_code=status_code,
        header=public_bar(),
        narrow=True,
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _bad_link(message: str, status_code: int = 400) -> HTMLResponse:
    return page("Set your password", f"<h1>Set your password</h1>{_error(message)}", status_code=status_code)


@router.get("/set-password", response_class=HTMLResponse)
def set_password_form(request: Request, token: str = Query(...), db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        applicant_for_token(db, token)
    except InvalidLinkError as exc:
        return _bad_link(str(exc))
    return _set_password_page(request, token)


@router.post("/set-password", response_class=HTMLResponse, dependencies=_CSRF)
def set_password_submit(
    request: Request, token: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)
) -> Response:
    if not throttle.take_set_password_attempt(_client_ip(request)):
        return _bad_link(_TOO_MANY, status_code=429)
    try:
        set_password(db, token=token, password=password)
        db.commit()
    except InvalidLinkError as exc:
        db.rollback()
        return _bad_link(str(exc))
    except AccountError as exc:
        db.rollback()
        return _set_password_page(request, token, error=str(exc), status_code=400)
    request.session.clear()
    return RedirectResponse("/login", status_code=_SEE_OTHER)


def _admin_login_page(request: Request, *, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    return page(
        "Admin login",
        f"""
<h1>Admin login</h1>
{_error(error)}
<form method="post" action="/admin/login">
  {csrf_field(request)}
  <label>Admin key <input name="admin_key" type="password" required autocomplete="off"/></label>
  <p><button type="submit" class="primary">Log in</button></p>
</form>
""",
        status_code=status_code,
        narrow=True,
    )


@admin_session_router.get("/login", response_class=HTMLResponse)
def admin_login_form(request: Request) -> HTMLResponse:
    return _admin_login_page(request)


@admin_session_router.post("/login", response_class=HTMLResponse, dependencies=_CSRF)
def admin_login(request: Request, admin_key: str = Form(...)) -> Response:
    if not throttle.take_admin_login_attempt(_client_ip(request)):
        return _admin_login_page(request, error=_TOO_MANY, status_code=429)
    if not admin_key_matches(admin_key):
        return _admin_login_page(request, error="That key is wrong.", status_code=401)
    start_admin_session(request)
    return RedirectResponse("/admin/applicants", status_code=_SEE_OTHER)


@admin_session_router.post("/logout", dependencies=_CSRF)
def admin_logout(request: Request) -> RedirectResponse:
    end_admin_session(request)
    return RedirectResponse("/admin/login", status_code=_SEE_OTHER)


def _admin_link_page(request: Request, *, body: str = "", error: str | None = None, status_code: int = 200) -> HTMLResponse:
    return page(
        "Set-password link",
        f"""
<h1>Set-password link</h1>
<p>For an admin-created applicant, or anyone who has forgotten their password. The link works
once and lasts 24 hours. Send it to the person privately.</p>
{_error(error)}
{body}
<form method="post" action="/admin/accounts/set-password-link">
  {csrf_field(request)}
  <label>Email <input name="email" type="email" required/></label>
  <p><button type="submit" class="primary">Create link</button></p>
</form>
""",
        status_code=status_code,
        header=admin_bar(request, "applicants"),
    )


@admin_router.get("/set-password-link", response_class=HTMLResponse)
def admin_link_form(request: Request) -> HTMLResponse:
    return _admin_link_page(request)


@admin_router.post("/set-password-link", response_class=HTMLResponse, dependencies=_CSRF)
def admin_link_create(request: Request, email: str = Form(...), db: Session = Depends(get_db)) -> HTMLResponse:
    applicant = db.scalar(select(Applicant).where(Applicant.email == email.strip()))
    if applicant is None:
        return _admin_link_page(request, error="No applicant has that email.", status_code=404)
    link = f"{request.base_url}set-password?token={set_password_token(applicant)}"
    return _admin_link_page(request, body=f"<p><code>{html.escape(link)}</code></p>")

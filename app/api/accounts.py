"""Register, log in, log out, confirm an email, reset a password, delete an account, and admin login (T29, T32).

Registering and asking for a reset or a new confirm link give the same reply whether or not the
email has an account, and emails go out after the reply, so neither the words nor the timing say
which addresses are registered.
"""

from __future__ import annotations

import html
import uuid

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.accounts import mail, throttle
from app.accounts.service import (
    RESET_PASSWORD_MINUTES,
    VERIFY_EMAIL_HOURS,
    AccountError,
    InvalidLinkError,
    authenticate,
    check_token,
    confirm_email,
    delete_account,
    end_all_sessions,
    find_active,
    issue_token,
    register,
    reset_password,
)
from app.api.admin_auth import admin_key_matches, end_admin_session, start_admin_session
from app.api.admin_html import page
from app.api.chrome import applicant_bar, onboarding_bar, public_bar
from app.api.forms import (
    csrf_field,
    password_field,
    password_toggle_script,
    require_csrf,
)
from app.core.db import get_db
from app.models import Applicant, AuthTokenPurpose

router = APIRouter(tags=["accounts"])
admin_session_router = APIRouter(prefix="/admin", tags=["admin-accounts"])

_LOGIN_FAILED = "Email or password is wrong."
_TOO_MANY = "Too many attempts. Wait a while and try again."
_SEE_OTHER = status.HTTP_303_SEE_OTHER
_CSRF = [Depends(require_csrf)]
# Fixed messages chosen by a query parameter, so nothing in the URL is ever echoed into the page.
_NOTICES = {
    "verified": "Your email is confirmed. Log in to carry on.",
    "reset": "Your password has been changed and every device has been logged out. Log in with the new password.",
}
_RESET_SENT = (
    "If an account uses that email, we've sent it a link to reset the password. "
    f"The link works once and expires in {RESET_PASSWORD_MINUTES} minutes."
)
_VERIFY_SENT = (
    "If that email has an account that still needs confirming, we've sent it a new link. "
    f"The link works once and expires in {VERIFY_EMAIL_HOURS} hours."
)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _error(message: str | None) -> str:
    return f'<p class="error" role="alert">{html.escape(message)}</p>' if message else ""


def _notice(message: str | None) -> str:
    return f'<p class="notice" role="status">{html.escape(message)}</p>' if message else ""


def _no_referrer(response: HTMLResponse) -> HTMLResponse:
    """Pages opened from an emailed link: the token in their URL must not leak to other sites."""
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _public_page(title: str, body: str, *, status_code: int = 200) -> HTMLResponse:
    return page(title, f'<div class="card">{body}</div>', status_code=status_code, header=public_bar(), narrow=True)


def _credentials_page(
    request: Request,
    title: str,
    path: str,
    *,
    new_password: bool,
    error: str | None = None,
    notice: str | None = None,
    email: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    other = (
        '<p class="hint">We keep only what&#39;s needed to match you to jobs. Read the <a href="/privacy">privacy notice</a>.</p>'
        '<p class="switch">Already have an account? <a href="/login">Log in</a></p>'
        if new_password
        else '<p class="switch"><a href="/forgot-password">Forgot your password?</a></p>'
        '<p class="switch">New here? <a href="/register">Create an account</a></p>'
    )
    password = password_field(
        "Password",
        autocomplete="new-password" if new_password else "current-password",
        hint="At least 12 characters." if new_password else "",
    )
    return page(
        title,
        f"""
<div class="card">
<h1>{html.escape(title)}</h1>
{_notice(notice)}
{_error(error)}
<form method="post" action="{path}">
  {csrf_field(request)}
  <label for="field-email">Email</label>
  <input id="field-email" name="email" type="email" required autocomplete="username" value="{html.escape(email)}"/>
  {password}
  <p><button type="submit" class="primary">{html.escape(title)}</button></p>
</form>
{other}
</div>
{password_toggle_script()}
""",
        status_code=status_code,
        header=public_bar(),
        narrow=True,
    )


def after_login_url(applicant: Applicant) -> str:
    """Unconfirmed accounts confirm first; someone partway through signing up carries on; everyone else sees matches."""
    if applicant.email_verified_at is None:
        return "/verify-email"
    return "/onboarding" if applicant.onboarding_step else "/listings"


def _start_session(request: Request, applicant: Applicant) -> RedirectResponse:
    # A fresh cookie: nothing from before logging in, including the form token, carries over.
    request.session.clear()
    request.session["applicant"] = str(applicant.id)
    request.session["version"] = applicant.session_version
    return RedirectResponse(after_login_url(applicant), status_code=_SEE_OTHER)


def current_applicant(request: Request, db: Session = Depends(get_db)) -> Applicant | None:
    """Whoever the cookie belongs to, confirmed or not. Pages with personal data use require_login."""
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
    if applicant.email_verified_at is None:
        raise HTTPException(status_code=_SEE_OTHER, headers={"Location": "/verify-email"})
    return applicant


# Register.


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request) -> HTMLResponse:
    return _credentials_page(request, "Register", "/register", new_password=True)


@router.post("/register", response_class=HTMLResponse, dependencies=_CSRF)
def register_submit(
    request: Request,
    background: BackgroundTasks,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    if not throttle.take_register_attempt(_client_ip(request)):
        return _credentials_page(
            request, "Register", "/register", new_password=True, error=_TOO_MANY, email=email, status_code=429
        )
    try:
        applicant = register(db, email=email, password=password)
    except AccountError as exc:
        db.rollback()
        return _credentials_page(
            request, "Register", "/register", new_password=True, error=str(exc), email=email, status_code=400
        )
    address = email.strip()
    if applicant is not None:
        token = issue_token(db, applicant, AuthTokenPurpose.verify_email)
        db.commit()
        if throttle.take_mail_to(address):
            background.add_task(mail.send, mail.verify_email(applicant.email, token, VERIFY_EMAIL_HOURS))
    elif throttle.take_mail_to(address):
        # The owner hears someone tried; the person registering sees the same page as for a new account.
        background.add_task(mail.send, mail.already_registered(address))
    return RedirectResponse("/check-email", status_code=_SEE_OTHER)


def _resend_form(request: Request) -> str:
    return f"""
<h2>Didn't get it?</h2>
<p class="muted">Check your spam folder first. Links expire after {VERIFY_EMAIL_HOURS} hours.</p>
<form method="post" action="/verify-email/resend">
  {csrf_field(request)}
  <label for="resend-email">Email</label>
  <input id="resend-email" name="email" type="email" required autocomplete="email"/>
  <p><button type="submit">Send a new link</button></p>
</form>"""


@router.get("/check-email", response_class=HTMLResponse)
def check_email(request: Request) -> HTMLResponse:
    return _public_page(
        "Check your email",
        f"""<h1>Check your email</h1>
<p>We've sent a link to the address you entered. Open it to confirm your email, then log in.</p>
<p class="muted">If that address already has an account, we've emailed it instead.</p>
{_resend_form(request)}
<p class="switch"><a href="/login">Log in</a></p>""",
    )


# Log in and log out.


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, notice: str = Query("")) -> HTMLResponse:
    return _credentials_page(request, "Log in", "/login", new_password=False, notice=_NOTICES.get(notice))


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


# Confirm an email.


@router.get("/verify-email", response_class=HTMLResponse, response_model=None)
def verify_email_page(
    request: Request, resent: str = Query(""), applicant: Applicant | None = Depends(current_applicant)
) -> Response:
    """Where a logged-in account waits until its email is confirmed."""
    if applicant is None:
        return RedirectResponse("/login", status_code=_SEE_OTHER)
    if applicant.email_verified_at is not None:
        return RedirectResponse(after_login_url(applicant), status_code=_SEE_OTHER)
    return page(
        "Confirm your email",
        f"""<div class="card">
<h1>Confirm your email</h1>
{_notice(_VERIFY_SENT if resent else None)}
<p>We sent a link to <b>{html.escape(applicant.email)}</b>. Open it to confirm your address and carry on.</p>
<p class="muted">Check your spam folder. Links expire after {VERIFY_EMAIL_HOURS} hours.</p>
<form method="post" action="/verify-email/resend">{csrf_field(request)}
<button type="submit">Send a new link</button></form>
</div>""",
        header=onboarding_bar(request),
        narrow=True,
    )


@router.post("/verify-email/resend", response_class=HTMLResponse, dependencies=_CSRF)
def verify_email_resend(
    request: Request,
    background: BackgroundTasks,
    email: str = Form(""),
    applicant: Applicant | None = Depends(current_applicant),
    db: Session = Depends(get_db),
) -> Response:
    """For the logged-in account, or else for the email typed. The reply is the same either way."""
    if not throttle.take_email_request("verify", _client_ip(request)):
        return _public_page("Too many requests", f"<h1>Too many requests</h1>{_error(_TOO_MANY)}", status_code=429)
    logged_in = applicant is not None
    target = applicant if logged_in else find_active(db, email)
    address = target.email if logged_in else email
    if target is not None and target.email_verified_at is None and throttle.take_mail_to(address):
        token = issue_token(db, target, AuthTokenPurpose.verify_email)
        db.commit()
        background.add_task(mail.send, mail.verify_email(target.email, token, VERIFY_EMAIL_HOURS))
    if logged_in:
        return RedirectResponse("/verify-email?resent=1", status_code=_SEE_OTHER)
    return _public_page(
        "Check your email", f'<h1>Check your email</h1>{_notice(_VERIFY_SENT)}<p><a href="/login">Log in</a></p>'
    )


def _invalid_link(title: str, again: str) -> HTMLResponse:
    return _no_referrer(
        _public_page(title, f"<h1>{html.escape(title)}</h1>{_error(str(InvalidLinkError()))}<p>{again}</p>", status_code=400)
    )


_VERIFY_AGAIN = '<a href="/check-email">Get a new confirm link</a>'
_RESET_AGAIN = '<a href="/forgot-password">Ask for a new reset link</a>'


@router.get("/verify-email/confirm", response_class=HTMLResponse)
def verify_email_confirm_form(request: Request, token: str = Query(""), db: Session = Depends(get_db)) -> HTMLResponse:
    """Opening the link only shows a button. Mail scanners that open links can't use it up."""
    try:
        check_token(db, token, AuthTokenPurpose.verify_email)
    except InvalidLinkError:
        return _invalid_link("Confirm your email", _VERIFY_AGAIN)
    return _no_referrer(
        _public_page(
            "Confirm your email",
            f"""<h1>Confirm your email</h1>
<form method="post" action="/verify-email/confirm">
  {csrf_field(request)}
  <input type="hidden" name="token" value="{html.escape(token)}"/>
  <p><button type="submit" class="primary">Confirm my email</button></p>
</form>""",
        )
    )


@router.post("/verify-email/confirm", response_class=HTMLResponse, dependencies=_CSRF)
def verify_email_confirm(
    request: Request,
    token: str = Form(""),
    current: Applicant | None = Depends(current_applicant),
    db: Session = Depends(get_db),
) -> Response:
    if not throttle.take_link_use(_client_ip(request)):
        return _public_page("Too many requests", f"<h1>Too many requests</h1>{_error(_TOO_MANY)}", status_code=429)
    try:
        applicant = confirm_email(db, token)
        db.commit()
    except InvalidLinkError:
        db.rollback()
        return _invalid_link("Confirm your email", _VERIFY_AGAIN)
    # Confirming never logs anyone in: whoever holds the link still needs the password.
    if current is not None and current.id == applicant.id:
        return RedirectResponse(after_login_url(applicant), status_code=_SEE_OTHER)
    return RedirectResponse("/login?notice=verified", status_code=_SEE_OTHER)


# Forgotten password.


def _forgot_page(request: Request, *, notice: str | None = None, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    return _public_page(
        "Forgot your password",
        f"""<h1>Forgot your password?</h1>
{_notice(notice)}
{_error(error)}
<p class="muted">Enter your account's email and we'll send a link to choose a new password.</p>
<form method="post" action="/forgot-password">
  {csrf_field(request)}
  <label for="field-email">Email</label>
  <input id="field-email" name="email" type="email" required autocomplete="username"/>
  <p><button type="submit" class="primary">Send reset link</button></p>
</form>
<p class="switch"><a href="/login">Back to log in</a></p>""",
        status_code=status_code,
    )


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_form(request: Request) -> HTMLResponse:
    return _forgot_page(request)


@router.post("/forgot-password", response_class=HTMLResponse, dependencies=_CSRF)
def forgot_password_submit(
    request: Request, background: BackgroundTasks, email: str = Form(...), db: Session = Depends(get_db)
) -> HTMLResponse:
    if not throttle.take_email_request("reset", _client_ip(request)):
        return _forgot_page(request, error=_TOO_MANY, status_code=429)
    applicant = find_active(db, email)
    if applicant is not None and throttle.take_mail_to(email):
        token = issue_token(db, applicant, AuthTokenPurpose.reset_password)
        db.commit()
        background.add_task(mail.send, mail.reset_password(applicant.email, token, RESET_PASSWORD_MINUTES))
    return _forgot_page(request, notice=_RESET_SENT)


def _reset_page(request: Request, token: str, *, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    return _no_referrer(
        page(
            "Choose a new password",
            f"""<div class="card">
<h1>Choose a new password</h1>
{_error(error)}
<p class="muted">Saving it logs you out on every device.</p>
<form method="post" action="/reset-password">
  {csrf_field(request)}
  <input type="hidden" name="token" value="{html.escape(token)}"/>
  {password_field("New password", autocomplete="new-password", hint="At least 12 characters.")}
  <p><button type="submit" class="primary">Save new password</button></p>
</form>
</div>
{password_toggle_script()}""",
            status_code=status_code,
            header=public_bar(),
            narrow=True,
        )
    )


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_form(request: Request, token: str = Query(""), db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        check_token(db, token, AuthTokenPurpose.reset_password)
    except InvalidLinkError:
        return _invalid_link("Choose a new password", _RESET_AGAIN)
    return _reset_page(request, token)


@router.post("/reset-password", response_class=HTMLResponse, dependencies=_CSRF)
def reset_password_submit(
    request: Request, token: str = Form(""), password: str = Form(...), db: Session = Depends(get_db)
) -> Response:
    if not throttle.take_link_use(_client_ip(request)):
        return _public_page("Too many requests", f"<h1>Too many requests</h1>{_error(_TOO_MANY)}", status_code=429)
    try:
        reset_password(db, token=token, password=password)
        db.commit()
    except InvalidLinkError:
        db.rollback()
        return _invalid_link("Choose a new password", _RESET_AGAIN)
    except AccountError as exc:
        db.rollback()
        return _reset_page(request, token, error=str(exc), status_code=400)
    request.session.clear()
    return RedirectResponse("/login?notice=reset", status_code=_SEE_OTHER)


# Account page.


def _account_page(
    request: Request, applicant: Applicant, *, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    return page(
        "Your account",
        f"""
<h1>Your account</h1>
<div class="card">
<p>Logged in as {html.escape(applicant.email)}.</p>
<p><a href="/listings">Your job matches</a> · <a href="/applications">Applications</a> ·
<a href="/profile">Your profile and CV</a> · <a href="/profile/apply-answers">Apply answers</a> ·
<a href="/account/gmail">Gmail for outreach</a></p>
<form method="post" action="/logout">{csrf_field(request)}<button type="submit">Log out</button></form>
</div>
<div class="card" id="extension">
<h2>Browser extension</h2>
<p>The extension fills employer forms in your tab and never clicks Submit. Create a token, paste it into the extension,
and keep this site's origin in the extension settings.</p>
<form method="post" action="/account/extension-token">{csrf_field(request)}
<button type="submit">Create or rotate extension token</button></form>
</div>
<div class="card" id="delete">
<h2>Delete your account</h2>
<p>This permanently removes your profile, CV, preferences, apply answers, applications, job matches, outreach contacts and drafts, and Gmail connection, and can't be undone.</p>
{_error(error)}
<form method="post" action="/account/delete">
  {csrf_field(request)}
  {password_field("Enter your password to confirm", autocomplete="current-password", field_id="delete-password")}
  <p><button type="submit" class="danger">Delete my account</button></p>
</form>
</div>
{password_toggle_script()}
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
    return RedirectResponse("/admin/quality", status_code=_SEE_OTHER)


@admin_session_router.post("/logout", dependencies=_CSRF)
def admin_logout(request: Request) -> RedirectResponse:
    end_admin_session(request)
    return RedirectResponse("/admin/login", status_code=_SEE_OTHER)


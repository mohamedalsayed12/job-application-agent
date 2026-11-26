"""Email confirmation, password reset, their limits, and what they reveal about which emails are registered."""

from __future__ import annotations

import hashlib
import logging
import smtplib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.accounts import mail, throttle
from app.core.config import get_settings
from app.core.log_redact import RedactTokens, redact
from app.main import app
from app.models import Applicant, AuthToken, AuthTokenPurpose
from tests import test_accounts
from tests.auth import emailed_token, sign_up
from tests.test_accounts import EMAIL, OTHER_PASSWORD, PASSWORD, _admin_created

# The same logged-out client, with form tokens switched off, as the account tests use.
client = test_accounts.client

PROTECTED = ("/account", "/profile", "/listings", "/applications", "/workflow", "/onboarding", "/onboarding/cv")


async def _register_only(client: httpx.AsyncClient, email: str = EMAIL, password: str = PASSWORD) -> httpx.Response:
    return await client.post("/register", data={"email": email, "password": password})


async def _login(client: httpx.AsyncClient, email: str = EMAIL, password: str = PASSWORD) -> httpx.Response:
    return await client.post("/login", data={"email": email, "password": password})


def _applicant(session: Session, email: str = EMAIL) -> Applicant:
    return session.scalar(select(Applicant).where(Applicant.email == email))


def _mails(to: str = EMAIL, word: str = "") -> list[mail.Mail]:
    return [m for m in mail.outbox if m.to.lower() == to.lower() and word in m.subject]


def _expire(session: Session, purpose: AuthTokenPurpose) -> None:
    session.execute(
        update(AuthToken).where(AuthToken.purpose == purpose.value).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    session.flush()


async def _forgot(client: httpx.AsyncClient, email: str = EMAIL) -> httpx.Response:
    return await client.post("/forgot-password", data={"email": email})


async def _reset(client: httpx.AsyncClient, token: str, password: str = OTHER_PASSWORD) -> httpx.Response:
    return await client.post("/reset-password", data={"token": token, "password": password})


# Registering and confirming.


async def test_registering_emails_a_confirm_link_and_logs_nobody_in(client: httpx.AsyncClient, session: Session) -> None:
    response = await _register_only(client)
    assert (response.status_code, response.headers["location"]) == (303, "/check-email")
    assert "jaa_session" not in response.headers.get("set-cookie", "")
    [message] = _mails(word="Confirm")
    assert "https://testserver/verify-email/confirm?token=" in message.text
    assert _applicant(session).email_verified_at is None
    assert (await client.get("/account")).headers["location"] == "/login"


async def test_an_unconfirmed_account_can_log_in_but_sees_only_the_confirm_page(client: httpx.AsyncClient) -> None:
    await _register_only(client)
    login = await _login(client)
    assert (login.status_code, login.headers["location"]) == (303, "/verify-email")
    waiting = await client.get("/verify-email")
    assert waiting.status_code == 200
    assert EMAIL in waiting.text
    for path in (*PROTECTED, "/"):
        assert (await client.get(path)).headers["location"] == "/verify-email", path


async def test_opening_the_link_shows_a_button_and_does_not_use_it_up(client: httpx.AsyncClient, session: Session) -> None:
    await _register_only(client)
    token = emailed_token(EMAIL, "Confirm")
    for _ in range(2):
        opened = await client.get(f"/verify-email/confirm?token={token}")
        assert opened.status_code == 200
        assert opened.headers["referrer-policy"] == "no-referrer"
        assert "Confirm my email" in opened.text
    assert _applicant(session).email_verified_at is None
    confirmed = await client.post("/verify-email/confirm", data={"token": token})
    assert (confirmed.status_code, confirmed.headers["location"]) == (303, "/login?notice=verified")
    assert _applicant(session).email_verified_at is not None
    assert "Your email is confirmed" in (await client.get("/login?notice=verified")).text


async def test_confirming_while_logged_in_goes_straight_on_to_the_wizard(client: httpx.AsyncClient) -> None:
    await _register_only(client)
    await _login(client)
    confirmed = await client.post("/verify-email/confirm", data={"token": emailed_token(EMAIL, "Confirm")})
    assert confirmed.headers["location"] == "/onboarding"
    assert (await client.get("/account")).status_code == 200


async def test_a_confirm_link_works_once(client: httpx.AsyncClient) -> None:
    await _register_only(client)
    token = emailed_token(EMAIL, "Confirm")
    assert (await client.post("/verify-email/confirm", data={"token": token})).status_code == 303
    reused = await client.post("/verify-email/confirm", data={"token": token})
    assert reused.status_code == 400
    assert "expired or has already been used" in reused.text
    assert (await client.get(f"/verify-email/confirm?token={token}")).status_code == 400


async def test_an_expired_confirm_link_is_refused(client: httpx.AsyncClient, session: Session) -> None:
    await _register_only(client)
    token = emailed_token(EMAIL, "Confirm")
    _expire(session, AuthTokenPurpose.verify_email)
    assert (await client.get(f"/verify-email/confirm?token={token}")).status_code == 400
    assert (await client.post("/verify-email/confirm", data={"token": token})).status_code == 400
    assert _applicant(session).email_verified_at is None


@pytest.mark.parametrize("token", ["", "made-up", "x" * 500])
async def test_an_unknown_confirm_link_is_refused(client: httpx.AsyncClient, token: str) -> None:
    assert (await client.post("/verify-email/confirm", data={"token": token})).status_code == 400


async def test_a_new_link_replaces_the_old_one(client: httpx.AsyncClient, session: Session) -> None:
    await _register_only(client)
    first = emailed_token(EMAIL, "Confirm")
    await _login(client)
    resent = await client.post("/verify-email/resend")
    assert resent.headers["location"] == "/verify-email?resent=1"
    second = emailed_token(EMAIL, "Confirm")
    assert first != second
    assert (await client.post("/verify-email/confirm", data={"token": first})).status_code == 400
    assert (await client.post("/verify-email/confirm", data={"token": second})).status_code == 303


async def test_only_a_hash_of_each_token_is_stored(client: httpx.AsyncClient, session: Session) -> None:
    await _register_only(client)
    token = emailed_token(EMAIL, "Confirm")
    [row] = session.scalars(select(AuthToken)).all()
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in row.token_hash
    assert row.expires_at - row.created_at <= timedelta(hours=24, minutes=1)


async def test_registering_a_taken_email_looks_the_same_and_tells_the_owner(client: httpx.AsyncClient, session: Session) -> None:
    await sign_up(client, EMAIL, PASSWORD)
    client.cookies.clear()
    mail.outbox.clear()
    taken = await _register_only(client, password=OTHER_PASSWORD)
    new = await _register_only(client, email="new@example.com", password=OTHER_PASSWORD)
    assert (taken.status_code, taken.headers["location"]) == (new.status_code, new.headers["location"])
    assert ("set-cookie" in taken.headers) == ("set-cookie" in new.headers)
    [notice] = _mails()
    assert "already" in notice.subject
    assert "token=" not in notice.text
    assert (await _login(client, password=OTHER_PASSWORD)).status_code == 401


async def test_resending_for_an_email_gives_the_same_reply_whoever_owns_it(client: httpx.AsyncClient) -> None:
    await _register_only(client)
    await sign_up(client, "confirmed@example.com", PASSWORD)
    client.cookies.clear()
    mail.outbox.clear()
    replies = [
        await client.post("/verify-email/resend", data={"email": email})
        for email in (EMAIL, "nobody@example.com", "confirmed@example.com")
    ]
    assert {reply.status_code for reply in replies} == {200}
    assert replies[0].text == replies[1].text == replies[2].text
    # Only the unconfirmed account gets a link.
    assert [m.to for m in mail.outbox] == [EMAIL]


# Rate limits.


async def test_emails_to_one_address_are_limited(client: httpx.AsyncClient) -> None:
    await _register_only(client)
    for _ in range(5):
        await client.post("/verify-email/resend", data={"email": EMAIL})
    # One with registering, two resends, then nothing more this hour.
    assert len(_mails(word="Confirm")) == 3


async def test_resend_requests_are_limited_per_place(client: httpx.AsyncClient) -> None:
    for number in range(10):
        assert (await client.post("/verify-email/resend", data={"email": f"p{number}@example.com"})).status_code == 200
    assert (await client.post("/verify-email/resend", data={"email": "p99@example.com"})).status_code == 429


async def test_reset_requests_are_limited_per_place_and_per_address(client: httpx.AsyncClient) -> None:
    await sign_up(client, EMAIL, PASSWORD)
    throttle.reset()
    mail.outbox.clear()
    for _ in range(5):
        assert (await _forgot(client)).status_code == 200
    assert len(_mails(word="Reset")) == 3
    for number in range(5):
        assert (await _forgot(client, f"p{number}@example.com")).status_code == 200
    assert (await _forgot(client, "p9@example.com")).status_code == 429


async def test_using_links_is_limited_per_place(client: httpx.AsyncClient) -> None:
    for _ in range(20):
        assert (await client.post("/verify-email/confirm", data={"token": "guess"})).status_code == 400
    assert (await client.post("/verify-email/confirm", data={"token": "guess"})).status_code == 429
    assert (await _reset(client, "guess")).status_code == 429


# Resetting a password.


async def test_forgot_password_replies_the_same_for_known_and_unknown_emails(client: httpx.AsyncClient) -> None:
    await sign_up(client, EMAIL, PASSWORD)
    client.cookies.clear()
    mail.outbox.clear()
    known = await _forgot(client)
    unknown = await _forgot(client, "nobody@example.com")
    assert known.status_code == unknown.status_code == 200
    assert known.text == unknown.text
    assert "If an account uses that email" in known.text
    assert [m.to for m in mail.outbox] == [EMAIL]


async def test_a_deactivated_account_gets_no_reset_link(client: httpx.AsyncClient, session: Session) -> None:
    await sign_up(client, EMAIL, PASSWORD)
    _applicant(session).is_active = False
    session.flush()
    mail.outbox.clear()
    assert (await _forgot(client)).status_code == 200
    assert mail.outbox == []


async def test_resetting_changes_the_password(client: httpx.AsyncClient, session: Session) -> None:
    await sign_up(client, EMAIL, PASSWORD)
    await client.post("/logout")
    await _forgot(client)
    token = emailed_token(EMAIL, "Reset")
    opened = await client.get(f"/reset-password?token={token}")
    assert opened.status_code == 200
    assert opened.headers["referrer-policy"] == "no-referrer"
    done = await _reset(client, token)
    assert (done.status_code, done.headers["location"]) == (303, "/login?notice=reset")
    assert (await _login(client)).status_code == 401
    assert (await _login(client, password=OTHER_PASSWORD)).status_code == 303


async def test_a_reset_link_works_once(client: httpx.AsyncClient) -> None:
    await sign_up(client, EMAIL, PASSWORD)
    await _forgot(client)
    token = emailed_token(EMAIL, "Reset")
    assert (await _reset(client, token)).status_code == 303
    assert (await _reset(client, token, password="a third long password")).status_code == 400
    assert (await client.get(f"/reset-password?token={token}")).status_code == 400
    assert (await _login(client, password="a third long password")).status_code == 401


async def test_an_expired_reset_link_is_refused(client: httpx.AsyncClient, session: Session) -> None:
    await sign_up(client, EMAIL, PASSWORD)
    await _forgot(client)
    token = emailed_token(EMAIL, "Reset")
    _expire(session, AuthTokenPurpose.reset_password)
    assert (await client.get(f"/reset-password?token={token}")).status_code == 400
    assert (await _reset(client, token)).status_code == 400
    assert (await _login(client)).status_code == 303


async def test_a_reset_link_expires_within_an_hour(client: httpx.AsyncClient, session: Session) -> None:
    await sign_up(client, EMAIL, PASSWORD)
    await _forgot(client)
    row = session.scalar(select(AuthToken).where(AuthToken.purpose == AuthTokenPurpose.reset_password.value))
    assert row.expires_at - row.created_at <= timedelta(minutes=61)


async def test_a_too_short_new_password_keeps_the_link_working(client: httpx.AsyncClient) -> None:
    await sign_up(client, EMAIL, PASSWORD)
    await _forgot(client)
    token = emailed_token(EMAIL, "Reset")
    refused = await _reset(client, token, password="short")
    assert refused.status_code == 400
    assert "at least 12 characters" in refused.text
    assert (await _reset(client, token)).status_code == 303


async def test_a_reset_link_cannot_confirm_an_email_and_a_confirm_link_cannot_reset(client: httpx.AsyncClient) -> None:
    await _register_only(client)
    confirm = emailed_token(EMAIL, "Confirm")
    assert (await _reset(client, confirm)).status_code == 400
    await _forgot(client)
    reset = emailed_token(EMAIL, "Reset")
    assert (await client.post("/verify-email/confirm", data={"token": reset})).status_code == 400


async def test_resetting_also_confirms_the_email(client: httpx.AsyncClient, session: Session) -> None:
    await _register_only(client)
    await _forgot(client)
    assert (await _reset(client, emailed_token(EMAIL, "Reset"))).status_code == 303
    assert _applicant(session).email_verified_at is not None


async def test_resetting_logs_out_every_existing_session(client: httpx.AsyncClient) -> None:
    await sign_up(client, EMAIL, PASSWORD)
    assert (await client.get("/account")).status_code == 200
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as other:
        await _forgot(other)
        assert (await _reset(other, emailed_token(EMAIL, "Reset"))).status_code == 303
    assert (await client.get("/account")).headers["location"] == "/login"


async def test_a_newer_reset_link_replaces_the_older_one(client: httpx.AsyncClient) -> None:
    await sign_up(client, EMAIL, PASSWORD)
    await _forgot(client)
    first = emailed_token(EMAIL, "Reset")
    await _forgot(client)
    assert (await _reset(client, first)).status_code == 400
    assert (await _reset(client, emailed_token(EMAIL, "Reset"))).status_code == 303


async def test_resetting_gives_an_admin_created_applicant_a_password(client: httpx.AsyncClient, session: Session) -> None:
    _admin_created(session)
    await _forgot(client)
    assert (await _reset(client, emailed_token(EMAIL, "Reset"))).status_code == 303
    assert (await _login(client, password=OTHER_PASSWORD)).status_code == 303


# Sessions and access.


@pytest.mark.parametrize("path", PROTECTED)
async def test_pages_with_personal_data_send_visitors_to_log_in(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert (response.status_code, response.headers["location"]) == (303, "/login")


async def test_logging_in_starts_a_fresh_session(client: httpx.AsyncClient) -> None:
    await sign_up(client, EMAIL, PASSWORD)
    await client.post("/logout")
    await client.get("/login")
    before = client.cookies["jaa_session"]
    await _login(client)
    assert client.cookies["jaa_session"] != before


async def test_logging_out_ends_the_session_on_every_device(client: httpx.AsyncClient) -> None:
    await sign_up(client, EMAIL, PASSWORD)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as other:
        assert (await _login(other)).status_code == 303
        assert (await other.get("/account")).status_code == 200
        await client.post("/logout")
        assert (await other.get("/account")).headers["location"] == "/login"
    assert (await client.get("/account")).headers["location"] == "/login"


async def test_a_notice_in_the_url_is_never_echoed(client: httpx.AsyncClient) -> None:
    page = await client.get("/login?notice=<script>alert(1)</script>")
    assert "<script>alert(1)" not in page.text


# The password fields.


@pytest.mark.parametrize("path", ["/register", "/login"])
async def test_password_fields_have_a_show_hide_button(client: httpx.AsyncClient, path: str) -> None:
    text = (await client.get(path)).text
    assert 'id="field-password" name="password" type="password"' in text
    assert 'aria-controls="field-password"' in text
    assert '<span class="pw-word">Show</span><span class="visually-hidden"> password</span>' in text
    assert 'aria-live="polite"' in text
    assert 'label for="field-password"' in text


async def test_the_reset_and_delete_password_fields_have_the_button_too(client: httpx.AsyncClient) -> None:
    await sign_up(client, EMAIL, PASSWORD)
    assert 'aria-controls="delete-password"' in (await client.get("/account")).text
    await _forgot(client)
    reset = await client.get(f"/reset-password?token={emailed_token(EMAIL, 'Reset')}")
    assert 'aria-controls="field-password"' in reset.text


# Secrets stay out of logs.


def test_the_access_log_never_shows_a_token() -> None:
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4", "GET", "/reset-password?token=SECRETVALUE&x=1", "1.1", 200), None,
    )
    RedactTokens().filter(record)
    assert "SECRETVALUE" not in record.getMessage()
    assert "token=[redacted]&x=1" in record.getMessage()
    assert redact("/verify-email/confirm?token=abc") == "/verify-email/confirm?token=[redacted]"
    assert any(isinstance(f, RedactTokens) for f in logging.getLogger("uvicorn.access").filters)


def test_a_failed_send_logs_nothing_from_the_message(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("MAIL_BACKEND", "smtp")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-secret-value")
    get_settings.cache_clear()

    def _refuse(*_: object, **__: object) -> None:
        raise smtplib.SMTPException("rejected https://testserver/reset-password?token=SECRETVALUE")

    monkeypatch.setattr(smtplib, "SMTP", _refuse)
    # Alembic's logging setup during the test migration switches off loggers that already existed.
    monkeypatch.setattr(mail.logger, "disabled", False)
    try:
        with caplog.at_level(logging.DEBUG):
            mail.send(mail.reset_password(EMAIL, "SECRETVALUE", 60))
    finally:
        get_settings.cache_clear()
    assert "account email not sent" in caplog.text
    assert "SECRETVALUE" not in caplog.text
    assert "smtp-secret-value" not in caplog.text
    assert mail.outbox == []


def test_resend_backend_posts_to_the_https_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_BACKEND", "resend")
    monkeypatch.setenv("SMTP_PASSWORD", "re_test_key")
    monkeypatch.setenv("MAIL_FROM", "Job Agent <onboarding@resend.dev>")
    get_settings.cache_clear()
    calls: list[dict[str, object]] = []

    class _Resp:
        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def post(self, url: str, **kwargs: object) -> _Resp:
            calls.append({"url": url, **kwargs})
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    try:
        mail.send(mail.verify_email(EMAIL, "SECRETVALUE", 24))
    finally:
        get_settings.cache_clear()
    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.resend.com/emails"
    headers = calls[0]["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer re_test_key"
    body = calls[0]["json"]
    assert isinstance(body, dict)
    assert body["to"] == [EMAIL]
    assert "SECRETVALUE" in str(body["text"])
    assert mail.outbox == []


def test_links_use_the_configured_address_not_the_request_host() -> None:
    assert mail.link("/reset-password", "abc") == "https://testserver/reset-password?token=abc"

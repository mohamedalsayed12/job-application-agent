"""Step 17: register, log in and log out, with passwords stored only as hashes. T29."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.accounts.service import InvalidLinkError, set_password, set_password_token
from app.api.forms import require_csrf
from app.applicants.service import create_applicant, parse_preference_input
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.main import app
from app.models import Applicant

ADMIN = "test-admin-key-step17"
EMAIL = "person@example.com"
PASSWORD = "correct horse battery"
OTHER_PASSWORD = "another long password"


@pytest.fixture
async def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("ADMIN_KEY", ADMIN)
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    get_settings.cache_clear()
    throttle.reset()

    def _override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = _override_db
    # Form tokens have their own tests in test_forms.py.
    app.dependency_overrides[require_csrf] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as test_client:
        yield test_client
    app.dependency_overrides.clear()
    throttle.reset()
    get_settings.cache_clear()


async def _register(client: httpx.AsyncClient, email: str = EMAIL, password: str = PASSWORD) -> httpx.Response:
    return await client.post("/register", data={"email": email, "password": password})


async def _login(client: httpx.AsyncClient, email: str = EMAIL, password: str = PASSWORD) -> httpx.Response:
    return await client.post("/login", data={"email": email, "password": password})


def _applicant(session: Session, email: str = EMAIL) -> Applicant:
    return session.scalar(select(Applicant).where(Applicant.email == email))


def _admin_created(session: Session, email: str = EMAIL) -> Applicant:
    preference = parse_preference_input(target_roles=["Backend Developer"], locations=["Manchester"])
    applicant = create_applicant(session, email=email, preference=preference)
    # A refused request rolls back, which must not take this row with it.
    session.commit()
    return applicant


async def test_t29_register_logs_in_and_shows_the_account_page(client: httpx.AsyncClient) -> None:
    response = await _register(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/account"
    page = await client.get("/account")
    assert page.status_code == 200
    assert EMAIL in page.text
    assert page.headers["cache-control"] == "no-store"


async def test_t29_pages_with_personal_data_need_a_login(client: httpx.AsyncClient) -> None:
    response = await client.get("/account")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert EMAIL not in response.text


async def test_t29_a_password_is_stored_only_as_a_strong_hash(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    stored = _applicant(session).password_hash
    assert PASSWORD not in stored
    memory, iterations = re.match(r"\$argon2id\$v=19\$m=(\d+),t=(\d+),p=\d+\$", stored).groups()
    assert int(memory) >= 19456
    assert int(iterations) >= 2


async def test_t29_log_in_and_log_out(client: httpx.AsyncClient) -> None:
    await _register(client)
    await client.post("/logout")
    assert (await client.get("/account")).status_code == 303
    assert (await _login(client)).status_code == 303
    assert (await client.get("/account")).status_code == 200


async def test_a_copied_cookie_stops_working_after_log_out(client: httpx.AsyncClient) -> None:
    await _register(client)
    copied = client.cookies["jaa_session"]
    await client.post("/logout")
    client.cookies.clear()
    client.cookies.set("jaa_session", copied, domain="testserver")
    assert (await client.get("/account")).status_code == 303


async def test_the_login_cookie_cannot_be_read_by_scripts_or_sent_over_http(client: httpx.AsyncClient) -> None:
    cookie = (await _register(client)).headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_the_cookie_is_https_only_by_default() -> None:
    assert Settings(_env_file=None).session_cookie_secure is True


async def test_an_altered_cookie_counts_as_logged_out(client: httpx.AsyncClient) -> None:
    await _register(client)
    value = client.cookies["jaa_session"]
    client.cookies.clear()
    client.cookies.set("jaa_session", value[:-2] + "xx", domain="testserver")
    assert (await client.get("/account")).status_code == 303


async def test_a_wrong_password_and_an_unknown_email_look_the_same(client: httpx.AsyncClient) -> None:
    await _register(client)
    await client.post("/logout")
    wrong = await _login(client, password=OTHER_PASSWORD)
    unknown = await _login(client, email="nobody@example.com")
    assert wrong.status_code == unknown.status_code == 401
    assert "Email or password is wrong." in wrong.text
    assert "Email or password is wrong." in unknown.text


async def test_email_case_does_not_matter(client: httpx.AsyncClient) -> None:
    await _register(client)
    await client.post("/logout")
    assert (await _login(client, email="PERSON@Example.com")).status_code == 303


@pytest.mark.parametrize("password", ["too short", "x" * 129])
async def test_a_password_of_the_wrong_length_is_refused(client: httpx.AsyncClient, session: Session, password: str) -> None:
    assert (await _register(client, password=password)).status_code == 400
    assert _applicant(session) is None


@pytest.mark.parametrize("email", ["no-at-sign", "@example.com", "two words@example.com", "a@" + "b" * 260])
async def test_a_bad_email_is_refused(client: httpx.AsyncClient, session: Session, email: str) -> None:
    assert (await _register(client, email=email)).status_code == 400


async def test_registering_with_an_existing_email_changes_nothing(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    await client.post("/logout")
    assert (await _register(client, password=OTHER_PASSWORD)).status_code == 400
    assert (await _login(client, password=OTHER_PASSWORD)).status_code == 401
    assert (await _login(client)).status_code == 303


async def test_registering_cannot_claim_an_admin_created_applicant(client: httpx.AsyncClient, session: Session) -> None:
    applicant = _admin_created(session)
    assert (await _register(client)).status_code == 400
    session.refresh(applicant)
    assert applicant.password_hash is None
    assert (await client.get("/account")).status_code == 303


async def test_an_admin_created_applicant_cannot_log_in_until_they_set_a_password(
    client: httpx.AsyncClient, session: Session
) -> None:
    _admin_created(session)
    assert (await _login(client)).status_code == 401

    response = await client.post("/admin/accounts/set-password-link", data={"email": EMAIL}, headers={"X-Admin-Key": ADMIN})
    token = re.search(r"set-password\?token=([^<\s]+)", response.text).group(1)
    assert (await client.get(f"/set-password?token={token}")).status_code == 200
    set_response = await client.post("/set-password", data={"token": token, "password": PASSWORD})
    assert set_response.status_code == 303
    assert (await _login(client)).status_code == 303
    assert (await client.get("/account")).status_code == 200


async def test_a_set_password_link_works_once(client: httpx.AsyncClient, session: Session) -> None:
    token = set_password_token(_admin_created(session))
    assert (await client.post("/set-password", data={"token": token, "password": PASSWORD})).status_code == 303
    assert (await client.post("/set-password", data={"token": token, "password": OTHER_PASSWORD})).status_code == 400
    assert (await client.get(f"/set-password?token={token}")).status_code == 400
    assert (await _login(client, password=OTHER_PASSWORD)).status_code == 401


async def test_a_short_password_does_not_use_up_the_link(client: httpx.AsyncClient, session: Session) -> None:
    token = set_password_token(_admin_created(session))
    assert (await client.post("/set-password", data={"token": token, "password": "short"})).status_code == 400
    assert (await client.post("/set-password", data={"token": token, "password": PASSWORD})).status_code == 303


async def test_an_expired_or_altered_link_is_refused(client: httpx.AsyncClient, session: Session) -> None:
    applicant = _admin_created(session)
    token = set_password_token(applicant)
    with pytest.raises(InvalidLinkError):
        set_password(session, token=token, password=PASSWORD, max_age=-1)
    assert (await client.get(f"/set-password?token={token[:-2]}xx")).status_code == 400
    assert applicant.password_hash is None


async def test_setting_a_password_ends_existing_sessions(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    copied = client.cookies["jaa_session"]
    token = set_password_token(_applicant(session))
    assert (await client.post("/set-password", data={"token": token, "password": OTHER_PASSWORD})).status_code == 303
    client.cookies.clear()
    client.cookies.set("jaa_session", copied, domain="testserver")
    assert (await client.get("/account")).status_code == 303


async def test_a_deactivated_account_is_shut_out_at_once(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    _applicant(session).is_active = False
    session.flush()
    assert (await client.get("/account")).status_code == 303
    assert (await _login(client)).status_code == 401


async def test_guessing_stops_after_five_failures_even_with_the_right_password(client: httpx.AsyncClient) -> None:
    await _register(client)
    await client.post("/logout")
    for _ in range(5):
        assert (await _login(client, password=OTHER_PASSWORD)).status_code == 401
    assert (await _login(client)).status_code == 429


async def test_guessing_at_an_unknown_email_is_limited_the_same_way(client: httpx.AsyncClient) -> None:
    for _ in range(5):
        assert (await _login(client, email="nobody@example.com")).status_code == 401
    assert (await _login(client, email="NOBODY@example.com")).status_code == 429


async def test_guessing_at_one_account_does_not_block_another(client: httpx.AsyncClient) -> None:
    await _register(client)
    await client.post("/logout")
    for _ in range(5):
        await _login(client, email="nobody@example.com")
    assert (await _login(client)).status_code == 303


async def test_successful_logins_are_not_limited(client: httpx.AsyncClient) -> None:
    await _register(client)
    for _ in range(7):
        await client.post("/logout")
        assert (await _login(client)).status_code == 303


async def test_registering_is_limited_per_address(client: httpx.AsyncClient) -> None:
    for number in range(5):
        assert (await _register(client, email=f"person{number}@example.com")).status_code == 303
    assert (await _register(client, email="person9@example.com")).status_code == 429


async def test_a_login_gives_no_admin_access(client: httpx.AsyncClient) -> None:
    await _register(client)
    for path in ("/admin/applicants", "/admin/quality", "/admin/accounts/set-password-link"):
        assert (await client.get(path)).status_code == 401


async def test_only_an_admin_can_create_a_set_password_link(client: httpx.AsyncClient, session: Session) -> None:
    _admin_created(session)
    denied = await client.post("/admin/accounts/set-password-link", data={"email": EMAIL})
    assert denied.status_code == 401
    missing = await client.post("/admin/accounts/set-password-link", data={"email": "no@example.com"}, headers={"X-Admin-Key": ADMIN})
    assert missing.status_code == 404


async def test_what_a_person_types_is_escaped(client: httpx.AsyncClient) -> None:
    response = await _register(client, email='"><script>x</script>@example.com', password="short")
    assert "<script>x" not in response.text
    await _register(client, email="<b>bold</b>@example.com")
    assert "<b>bold" not in (await client.get("/account")).text


@pytest.mark.parametrize(
    ("secret", "key", "problem"),
    [
        ("", "private-key", "SESSION_SECRET"),
        ("too-short", "private-key", "SESSION_SECRET"),
        ("s" * 32, "change-me", "ADMIN_KEY"),
        ("s" * 32, "", "ADMIN_KEY"),
    ],
)
def test_the_web_app_refuses_unsafe_settings(secret: str, key: str, problem: str) -> None:
    problems = Settings(_env_file=None, session_secret=secret, admin_key=key).web_problems()
    assert any(problem in line for line in problems)


def test_safe_settings_pass() -> None:
    assert Settings(_env_file=None, session_secret="s" * 32, admin_key="private-key").web_problems() == []


async def test_the_privacy_notice_is_public_and_linked_from_every_page(client: httpx.AsyncClient) -> None:
    notice = await client.get("/privacy")
    assert notice.status_code == 200
    for expected in ("controller", "Argon2", "jaa_session", "30 days", "ico.org.uk", "the text of your CV is sent to Anthropic", "your target roles, experience level, skills and degrees"):
        assert expected in notice.text
    register = (await client.get("/register")).text
    assert 'href="/privacy"' in register
    assert register.count('href="/privacy"') == 2

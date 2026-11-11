"""Step 18: form tokens on every POST, and the admin key out of URLs (D38)."""

from __future__ import annotations

import base64
import re
import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api import admin_auth
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app

ADMIN = "test-admin-key-step18"
EMAIL = "person@example.com"
PASSWORD = "correct horse battery"
ADMIN_PAGES = ("/admin/applicants", "/admin/applicants/new", "/admin/quality", "/admin/accounts/set-password-link")
ADMIN_FORM_PAGES = ("/admin/applicants/new", "/admin/accounts/set-password-link")
ROUTES = app.openapi()["paths"]
POST_PATHS = sorted(
    path.replace("{applicant_id}", str(uuid.uuid4()))
    for path, operations in ROUTES.items()
    if "post" in operations
)


@pytest.fixture
async def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("ADMIN_KEY", ADMIN)
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    get_settings.cache_clear()
    throttle.reset()

    def _override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = _override_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as test_client:
        yield test_client
    app.dependency_overrides.clear()
    throttle.reset()
    get_settings.cache_clear()


def _token(page_text: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', page_text).group(1)


async def _post_from(client: httpx.AsyncClient, form_page: str, path: str, data: dict[str, str]) -> httpx.Response:
    """What a browser does: open the page, then submit its form."""
    opened = await client.get(form_page)
    return await client.post(path, data={**data, "csrf_token": _token(opened.text)})


async def _log_in_as_applicant_and_admin(client: httpx.AsyncClient) -> None:
    assert (await _post_from(client, "/register", "/register", {"email": EMAIL, "password": PASSWORD})).status_code == 303
    assert (await _post_from(client, "/admin/login", "/admin/login", {"admin_key": ADMIN})).status_code == 303


def test_no_route_changes_data_without_being_a_post() -> None:
    assert not [path for path, operations in ROUTES.items() if {"put", "patch", "delete"} & set(operations)]
    assert len(POST_PATHS) == 14


@pytest.mark.parametrize("path", POST_PATHS)
async def test_every_post_without_a_token_changes_nothing(client: httpx.AsyncClient, path: str) -> None:
    await _log_in_as_applicant_and_admin(client)
    response = await client.post(path, data={"email": "other@example.com", "password": PASSWORD})
    assert response.status_code == 403
    assert "This form has expired" in response.text
    assert (await client.get("/account")).status_code == 200


@pytest.mark.parametrize("path", POST_PATHS)
async def test_every_post_with_a_wrong_token_changes_nothing(client: httpx.AsyncClient, path: str) -> None:
    await _log_in_as_applicant_and_admin(client)
    assert (await client.post(path, data={"csrf_token": "guess"})).status_code == 403


async def test_a_token_from_another_session_is_refused(client: httpx.AsyncClient) -> None:
    other_token = _token((await client.get("/login")).text)
    client.cookies.clear()
    await client.get("/login")
    response = await client.post("/login", data={"email": EMAIL, "password": PASSWORD, "csrf_token": other_token})
    assert response.status_code == 403


async def test_register_log_out_and_log_in_work_with_the_tokens_on_the_page(client: httpx.AsyncClient) -> None:
    assert (await _post_from(client, "/register", "/register", {"email": EMAIL, "password": PASSWORD})).status_code == 303
    assert (await _post_from(client, "/account", "/logout", {})).status_code == 303
    assert (await client.get("/account")).status_code == 303
    assert (await _post_from(client, "/login", "/login", {"email": EMAIL, "password": PASSWORD})).status_code == 303
    assert (await client.get("/account")).status_code == 200


async def test_logging_in_replaces_the_token(client: httpx.AsyncClient) -> None:
    before = _token((await client.get("/register")).text)
    await client.post("/register", data={"email": EMAIL, "password": PASSWORD, "csrf_token": before})
    assert _token((await client.get("/account")).text) != before


async def test_every_form_on_every_page_carries_a_token(client: httpx.AsyncClient) -> None:
    await _log_in_as_applicant_and_admin(client)
    pages = ("/account", "/profile", "/register", "/login", "/admin/login", *ADMIN_FORM_PAGES)
    for path in pages:
        text = (await client.get(path)).text
        forms = re.findall(r"<form\b.*?</form>", text, flags=re.S)
        assert forms, path
        assert all('name="csrf_token"' in form for form in forms), path


async def test_forms_for_an_admin_work_with_the_token_on_the_page(client: httpx.AsyncClient) -> None:
    await _log_in_as_applicant_and_admin(client)
    response = await _post_from(
        client, "/admin/accounts/set-password-link", "/admin/accounts/set-password-link", {"email": EMAIL}
    )
    assert response.status_code == 200
    assert "set-password?token=" in response.text


async def test_the_admin_key_header_needs_no_token_for_scripts(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/admin/accounts/set-password-link", data={"email": "no@example.com"}, headers={"X-Admin-Key": ADMIN}
    )
    assert response.status_code == 404


async def test_a_wrong_admin_key_header_does_not_skip_the_token(client: httpx.AsyncClient) -> None:
    await _log_in_as_applicant_and_admin(client)
    response = await client.post("/login", data={"email": EMAIL, "password": PASSWORD}, headers={"X-Admin-Key": "wrong"})
    assert response.status_code == 403


@pytest.mark.parametrize("path", ADMIN_PAGES)
async def test_the_admin_key_is_no_longer_accepted_in_a_url(client: httpx.AsyncClient, path: str) -> None:
    assert (await client.get(f"{path}?admin_key={ADMIN}")).status_code == 401


@pytest.mark.parametrize("path", ADMIN_PAGES)
async def test_admin_pages_open_after_admin_login_and_never_show_the_key(client: httpx.AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 401
    await _post_from(client, "/admin/login", "/admin/login", {"admin_key": ADMIN})
    response = await client.get(path)
    assert response.status_code == 200
    assert ADMIN not in response.text
    assert "admin_key=" not in response.text


async def test_the_admin_key_is_not_stored_in_the_cookie(client: httpx.AsyncClient) -> None:
    await _post_from(client, "/admin/login", "/admin/login", {"admin_key": ADMIN})
    payload = client.cookies["jaa_session"].split(".")[0]
    decoded = base64.b64decode(payload + "=" * (-len(payload) % 4))
    assert ADMIN.encode() not in decoded


async def test_a_wrong_admin_key_is_refused_and_then_limited(client: httpx.AsyncClient) -> None:
    for _ in range(10):
        response = await _post_from(client, "/admin/login", "/admin/login", {"admin_key": "wrong"})
        assert response.status_code == 401
    assert (await _post_from(client, "/admin/login", "/admin/login", {"admin_key": ADMIN})).status_code == 429
    assert (await client.get("/admin/quality")).status_code == 401


async def test_admin_log_out_ends_admin_access_but_not_the_applicant_session(client: httpx.AsyncClient) -> None:
    await _log_in_as_applicant_and_admin(client)
    assert (await _post_from(client, "/account", "/admin/logout", {})).status_code == 303
    assert (await client.get("/admin/quality")).status_code == 401
    assert (await client.get("/account")).status_code == 200


async def test_an_admin_session_expires(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_auth, "ADMIN_SESSION_SECONDS", -1)
    await _post_from(client, "/admin/login", "/admin/login", {"admin_key": ADMIN})
    assert (await client.get("/admin/quality")).status_code == 401


async def test_changing_the_admin_key_ends_admin_sessions(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    await _post_from(client, "/admin/login", "/admin/login", {"admin_key": ADMIN})
    monkeypatch.setenv("ADMIN_KEY", "a-different-key")
    get_settings.cache_clear()
    assert (await client.get("/admin/quality")).status_code == 401


async def test_an_applicant_login_gives_no_admin_session(client: httpx.AsyncClient) -> None:
    await _post_from(client, "/register", "/register", {"email": EMAIL, "password": PASSWORD})
    assert (await client.get("/admin/quality")).status_code == 401

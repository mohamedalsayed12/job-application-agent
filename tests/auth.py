"""Signing up in tests the way a person does: register, open the emailed link, then log in."""

from __future__ import annotations

import re

import httpx

from app.accounts import mail

_LINK_TOKEN = re.compile(r"token=([A-Za-z0-9_\-]+)")


def emailed_token(to: str, subject_word: str) -> str:
    """The token from the newest test-outbox email to this address whose subject has the word."""
    for message in reversed(mail.outbox):
        if message.to.lower() == to.lower() and subject_word in message.subject:
            return _LINK_TOKEN.search(message.text).group(1)
    raise AssertionError(f"no {subject_word!r} email to {to}")


async def sign_up(client: httpx.AsyncClient, email: str, password: str) -> httpx.Response:
    """For clients with form tokens switched off. Returns the log-in response (303 into the wizard)."""
    registered = await client.post("/register", data={"email": email, "password": password})
    assert registered.status_code == 303, registered.text
    token = emailed_token(email, "Confirm")
    confirmed = await client.post("/verify-email/confirm", data={"token": token})
    assert confirmed.status_code == 303, confirmed.text
    return await client.post("/login", data={"email": email, "password": password})

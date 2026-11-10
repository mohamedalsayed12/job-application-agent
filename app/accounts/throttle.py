"""Limits on password guessing and on slow password hashing.

Counts live in this process, so they reset on restart and need one web process.
Behind a proxy, uvicorn needs ``--proxy-headers``, or every visitor shares one IP limit.
"""

from __future__ import annotations

from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter

_FAILED_LOGIN_LIMITS = (
    # One person mistyping or guessing from one place.
    (parse("5/15 minutes"), "account-ip"),
    # Many places guessing one account. A low limit here would let anyone lock a person out.
    (parse("20/hour"), "account"),
    # One place guessing many accounts.
    (parse("50/hour"), "ip"),
)
_REGISTER = parse("5/hour")
_SET_PASSWORD = parse("10/hour")
_ADMIN_LOGIN = parse("10/15 minutes")
_CV_UPLOAD = parse("20/hour")

_storage = MemoryStorage()
_limiter = MovingWindowRateLimiter(_storage)


def _login_keys(email: str, ip: str) -> dict[str, tuple[str, ...]]:
    account = email.strip().lower()
    return {"account-ip": (account, ip), "account": (account,), "ip": (ip,)}


def login_allowed(email: str, ip: str) -> bool:
    keys = _login_keys(email, ip)
    return all(_limiter.test(limit, "failed-login", kind, *keys[kind]) for limit, kind in _FAILED_LOGIN_LIMITS)


def record_failed_login(email: str, ip: str) -> None:
    keys = _login_keys(email, ip)
    for limit, kind in _FAILED_LOGIN_LIMITS:
        _limiter.hit(limit, "failed-login", kind, *keys[kind])


def take_register_attempt(ip: str) -> bool:
    return _limiter.hit(_REGISTER, "register", ip)


def take_set_password_attempt(ip: str) -> bool:
    return _limiter.hit(_SET_PASSWORD, "set-password", ip)


def take_admin_login_attempt(ip: str) -> bool:
    return _limiter.hit(_ADMIN_LOGIN, "admin-login", ip)


def take_cv_upload_attempt(applicant_id: str) -> bool:
    return _limiter.hit(_CV_UPLOAD, "cv-upload", applicant_id)


def reset() -> None:
    _storage.reset()

"""Limits on password guessing and on slow password hashing.

Counts live in Postgres (``rate_limit_event``), so they survive restarts and are shared by
every web process. A limit is a moving window: an attempt is one row, and a key is over its
limit when it has ``amount`` rows newer than now minus the window. Each call uses its own
short session and commits at once, so a count is kept even when the request's own
transaction rolls back (a failed login). Postgres time is used throughout, so processes
need no synced clocks.
Behind a proxy, uvicorn needs ``--proxy-headers``, or every visitor shares one IP limit.
"""

from __future__ import annotations

from limits import parse
from limits.limits import RateLimitItem
from sqlalchemy import text

from app.core import db

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

# Rows older than the longest window can never count again.
_LONGEST_WINDOW_SECONDS = max(
    limit.get_expiry()
    for limit in (*(item[0] for item in _FAILED_LOGIN_LIMITS), _REGISTER, _SET_PASSWORD, _ADMIN_LOGIN, _CV_UPLOAD)
)

_COUNT = text(
    "SELECT count(*) FROM rate_limit_event "
    "WHERE key = :key AND created_at > now() - make_interval(secs => :window)"
)
_INSERT = text("INSERT INTO rate_limit_event (key) VALUES (:key)")
_PURGE = text("DELETE FROM rate_limit_event WHERE created_at < now() - make_interval(secs => :window)")


def _key(*parts: str) -> str:
    # \x1f cannot appear in an email or IP, so different parts never join into the same key.
    return "\x1f".join(parts)


def _test(limit: RateLimitItem, key: str) -> bool:
    with db.SessionLocal() as session:
        used = session.execute(_COUNT, {"key": key, "window": limit.get_expiry()}).scalar_one()
    return used < limit.amount


def _hit(limit: RateLimitItem, key: str) -> bool:
    """Record an attempt unless the key is already at its limit. True when it was allowed."""
    with db.SessionLocal() as session, session.begin():
        # Serialise hits on one key across processes, so two at once cannot both take the last slot.
        session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})
        used = session.execute(_COUNT, {"key": key, "window": limit.get_expiry()}).scalar_one()
        if used >= limit.amount:
            return False
        session.execute(_INSERT, {"key": key})
        session.execute(_PURGE, {"window": _LONGEST_WINDOW_SECONDS})
        return True


def _login_keys(email: str, ip: str) -> dict[str, str]:
    account = email.strip().lower()
    return {
        "account-ip": _key("failed-login", "account-ip", account, ip),
        "account": _key("failed-login", "account", account),
        "ip": _key("failed-login", "ip", ip),
    }


def login_allowed(email: str, ip: str) -> bool:
    keys = _login_keys(email, ip)
    return all(_test(limit, keys[kind]) for limit, kind in _FAILED_LOGIN_LIMITS)


def record_failed_login(email: str, ip: str) -> None:
    keys = _login_keys(email, ip)
    for limit, kind in _FAILED_LOGIN_LIMITS:
        _hit(limit, keys[kind])


def take_register_attempt(ip: str) -> bool:
    return _hit(_REGISTER, _key("register", ip))


def take_set_password_attempt(ip: str) -> bool:
    return _hit(_SET_PASSWORD, _key("set-password", ip))


def take_admin_login_attempt(ip: str) -> bool:
    return _hit(_ADMIN_LOGIN, _key("admin-login", ip))


def take_cv_upload_attempt(applicant_id: str) -> bool:
    return _hit(_CV_UPLOAD, _key("cv-upload", applicant_id))


def reset() -> None:
    """Forget every count. For tests."""
    with db.SessionLocal() as session, session.begin():
        session.execute(text("DELETE FROM rate_limit_event"))

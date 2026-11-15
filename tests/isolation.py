"""Per-worktree Postgres names for parallel agent checkouts."""

from __future__ import annotations

import re

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError

_DEFAULT_TEST = "job_app_agent_test"
_LIVE_PREFIX = "job_app_agent_"
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")  # script slug and jaa-<slug>


def require_slug(slug: str) -> str:
    cleaned = slug.strip().lower()
    if not _SLUG.fullmatch(cleaned):
        raise ValueError(
            f"slug must match {_SLUG.pattern} (no leading/trailing/doubled hyphens); "
            f"got {slug!r}"
        )
    # Live name is job_app_agent_<slug>; pytest uses job_app_agent_test[_<slug>].
    # Ban test / test-* so live never lands on a pytest database.
    if cleaned == "test" or cleaned.startswith("test-"):
        raise ValueError(
            f"slug {slug!r} collides with pytest DB names "
            f"(job_app_agent_test…); choose another"
        )
    return cleaned


def worktree_slug(repo_dir_name: str) -> str | None:
    if not repo_dir_name.startswith("jaa-"):
        return None
    return require_slug(repo_dir_name.removeprefix("jaa-"))


def _postgres_name(prefix: str, slug: str) -> str:
    name = f"{prefix}{require_slug(slug).replace('-', '_')}"
    if len(name) > 63:
        raise ValueError(f"database name too long: {name!r}")
    return name


def resolve_live_database_name(slug: str) -> str:
    return _postgres_name(_LIVE_PREFIX, slug)


def resolve_test_database_name(repo_dir_name: str) -> str:
    slug = worktree_slug(repo_dir_name)
    if slug is None:
        return _DEFAULT_TEST
    return _postgres_name(f"{_DEFAULT_TEST}_", slug)


def with_database(url: str, database: str) -> str:
    return make_url(url).set(database=database).render_as_string(hide_password=False)


def ensure_database(url: str, name: str) -> None:
    """CREATE DATABASE if missing. ``name`` must already be a safe identifier."""
    engine = create_engine(
        with_database(url, "postgres"),
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": name},
            ).scalar()
            if exists:
                return
            try:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
            except ProgrammingError:
                pass  # concurrent creator won
    finally:
        engine.dispose()

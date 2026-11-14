"""Per-worktree DB naming so parallel agents do not share databases."""

from __future__ import annotations

import re

from sqlalchemy.engine import make_url

_DEFAULT_TEST = "job_app_agent_test"
_LIVE_PREFIX = "job_app_agent_"
OVERRIDE_ENV = "JOB_APP_AGENT_TEST_DB"
# One grammar for script slugs and jaa-<slug> directory names.
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_IDENT = re.compile(r"^[a-z0-9_]+$")
_RESERVED = frozenset({"postgres", "template0", "template1"})


def require_slug(slug: str) -> str:
    cleaned = slug.strip().lower()
    if not _SLUG.fullmatch(cleaned):
        raise ValueError(
            f"slug must match {_SLUG.pattern} (no leading/trailing/doubled hyphens); "
            f"got {slug!r}"
        )
    return cleaned


def worktree_slug(repo_dir_name: str) -> str | None:
    """Return the slug for a ``jaa-<slug>`` checkout, or None for the primary tree."""
    if not repo_dir_name.startswith("jaa-"):
        return None
    return require_slug(repo_dir_name.removeprefix("jaa-"))


def resolve_live_database_name(slug: str) -> str:
    """Live/app DB for a worktree: ``job_app_agent_<slug_with_underscores>``."""
    safe = require_slug(slug).replace("-", "_")
    name = f"{_LIVE_PREFIX}{safe}"
    if len(name) > 63:
        raise ValueError(f"live database name too long: {name!r}")
    return name


def resolve_test_database_name(
    repo_dir_name: str, override: str | None = None
) -> str:
    """Pytest DB: primary → ``job_app_agent_test``; ``jaa-<slug>`` → ``…_test_<slug>``."""
    if override is not None:
        name = override.strip().lower()
        if (
            not name
            or len(name) > 63
            or not _IDENT.fullmatch(name)
            or name.startswith("_")
        ):
            raise ValueError(
                f"{OVERRIDE_ENV} must be a postgres identifier "
                f"(a-z, 0-9, underscore, 1-63 chars); got {override!r}"
            )
        assert_safe_database_name(name)
        return name
    slug = worktree_slug(repo_dir_name)
    if slug is None:
        return _DEFAULT_TEST
    name = f"{_DEFAULT_TEST}_{slug.replace('-', '_')}"
    if len(name) > 63:
        raise ValueError(f"derived test DB name too long: {name!r}")
    return name


def assert_safe_database_name(
    name: str, *, source_database: str | None = None
) -> None:
    """Reject cluster templates and the source URL's database (pytest override guard)."""
    if name in _RESERVED:
        raise ValueError(f"refusing reserved database name {name!r}")
    if source_database and name == source_database:
        raise ValueError(
            f"refusing to use source database {name!r} as the pytest target"
        )


def with_database(url: str, database: str) -> str:
    """Return ``url`` with the database name replaced; fail if nothing changed."""
    parsed = make_url(url)
    if parsed.database == database:
        raise ValueError(
            f"DATABASE_URL database segment already {database!r}; refusing no-op rewrite"
        )
    return parsed.set(database=database).render_as_string(hide_password=False)

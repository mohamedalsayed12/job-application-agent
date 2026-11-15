from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url

from tests.isolation import (
    require_slug,
    resolve_live_database_name,
    resolve_test_database_name,
    with_database,
)


def test_primary_checkout_keeps_shared_default() -> None:
    assert resolve_test_database_name("job-application-agent") == "job_app_agent_test"


def test_jaa_worktree_gets_slug_db() -> None:
    assert resolve_test_database_name("jaa-matching") == "job_app_agent_test_matching"
    assert (
        resolve_test_database_name("jaa-profile-search")
        == "job_app_agent_test_profile_search"
    )


def test_reject_slug_aliases_that_would_collide() -> None:
    with pytest.raises(ValueError):
        resolve_test_database_name("jaa-profile--search")
    with pytest.raises(ValueError):
        resolve_test_database_name("jaa-profile-search-")
    with pytest.raises(ValueError):
        require_slug("matching-")


def test_reject_slugs_that_collide_with_pytest_db_names() -> None:
    with pytest.raises(ValueError, match="collides with pytest"):
        require_slug("test")
    with pytest.raises(ValueError, match="collides with pytest"):
        resolve_live_database_name("test-foo")
    with pytest.raises(ValueError, match="collides with pytest"):
        resolve_test_database_name("jaa-test")
    assert resolve_live_database_name("testing") == "job_app_agent_testing"


def test_live_and_test_names_share_slug_grammar() -> None:
    assert resolve_live_database_name("profile-search") == "job_app_agent_profile_search"
    assert (
        resolve_test_database_name("jaa-profile-search")
        == "job_app_agent_test_profile_search"
    )


def test_with_database_keeps_query() -> None:
    url = "postgresql+psycopg://jaa:jaa@localhost:5432/jaa?sslmode=disable"
    out = with_database(url, "job_app_agent_matching")
    parsed = make_url(out)
    assert parsed.database == "job_app_agent_matching"
    assert parsed.query.get("sslmode") == "disable"

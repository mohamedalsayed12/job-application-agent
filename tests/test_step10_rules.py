"""Step 10 part 2 rule changes (docs/step10-audit-findings.md)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applicants.types import DiscoveryPreference
from app.core.config import Settings
from app.extraction.persist import persist_code_hits
from app.extraction.types import BoardRef, CodeHit
from app.models import Candidate
from app.queue.agencies import is_recruitment_agency
from app.search.queries import build_query
from app.verify.blocklist import is_aggregator_domain

_PREF = DiscoveryPreference(target_roles=("Graduate Software Engineer",), locations=("Manchester",), sector="tech")


def test_r6_defaults_leave_room_for_claude_output() -> None:
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    assert cfg.ai_extraction_max_output_tokens == 1500
    assert cfg.ai_extraction_per_item_token_cap == 4000


def test_r4_cf3_query_asks_for_official_website_only() -> None:
    query = build_query("CF3", _PREF, employer_name="Octopus Energy")
    assert query.text == '"Octopus Energy" official website'
    assert "Manchester" not in query.text
    assert " OR " not in query.text


@pytest.mark.parametrize(
    "host",
    ["uk.jooble.org", "bebee.com", "torre.ai", "www.glassdoor.co.uk", "en.wikipedia.org", "www.facebook.com"],
)
def test_r4_cf3_noise_hosts_are_never_official(host: str) -> None:
    assert is_aggregator_domain(host)


def test_r4_real_company_domains_still_allowed() -> None:
    assert not is_aggregator_domain("octopus.energy")
    assert not is_aggregator_domain("matillion.com")


@pytest.mark.parametrize(
    ("name", "agency"),
    [
        ("Hays Specialist Recruitment Limited", True),
        ("Alex James Resourcing", True),
        ("Robert Walters", True),
        ("Lorien", True),
        ("Picture More", True),
        ("Matillion", False),
        ("Octopus Energy", False),
        ("TalentLMS", False),
        ("Personnel Checks Ltd", False),
        ("Lorien Assisted Living", False),
        ("", False),
    ],
)
def test_agency_rule(name: str, agency: bool) -> None:
    assert is_recruitment_agency(name) is agency


def test_agencies_never_become_candidates(session: Session) -> None:
    hits = [
        CodeHit(company_name="VIQU IT Recruitment", board=None, quoted_text="VIQU IT Recruitment", source_url="https://www.reed.co.uk/jobs/1"),
        CodeHit(company_name="Hays", board=None, quoted_text="Hays", source_url="https://www.reed.co.uk/jobs/2"),
        CodeHit(company_name="Matillion", board=None, quoted_text="Matillion", source_url="https://www.reed.co.uk/jobs/3"),
        CodeHit(
            company_name=None,
            board=BoardRef(system="lever", instance="lever_global", board_id="octoenergy"),
            quoted_text="jobs.lever.co/octoenergy",
            source_url="https://jobs.lever.co/octoenergy",
        ),
    ]
    touched = persist_code_hits(session, hits)
    names = sorted(c.company_name for c in session.scalars(select(Candidate)))
    assert names == ["Matillion", "octoenergy"]
    assert len(touched) == 2

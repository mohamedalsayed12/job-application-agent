"""Aggregator / job-board domains that can never be official (Stage 5 step 1)."""

from __future__ import annotations

from app.fetch.policy import BLOCKED_SUFFIXES, host_matches_suffix

# Board hosts from extraction URL parsing — never official company domains.
_BOARD_HOSTS = (
    "jobs.lever.co",
    "jobs.eu.lever.co",
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "boards.eu.greenhouse.io",
    "job-boards.eu.greenhouse.io",
)

# MVP §7 examples beyond the outbound fetch blocklist.
_MVP_AGGREGATORS = (
    "reed.co.uk",
    "adzuna.co.uk",
    "adzuna.com",
    "totaljobs.com",
    "cwjobs.co.uk",
    "monster.co.uk",
    "monster.com",
)

# Seen in Step 10 CF3 results: job boards, aggregators, directories, social sites.
_CF3_OBSERVED = (
    "glassdoor.co.uk",
    "indeed.co.uk",
    "cv-library.co.uk",
    "jobsite.co.uk",
    "jooble.org",
    "bebee.com",
    "torre.ai",
    "studysmarter.co.uk",
    "efinancialcareers.com",
    "efinancialcareers.co.uk",
    "irishjobs.ie",
    "levels.fyi",
    "startup.jobs",
    "devitjobs.uk",
    "jobbeacon.app",
    "computerwork.com",
    "staffera.co.uk",
    "nationalcareers.service.gov.uk",
    "find-and-update.company-information.service.gov.uk",
    "wikipedia.org",
    "facebook.com",
    "reddit.com",
    "x.com",
    "twitter.com",
    "instagram.com",
    "youtube.com",
)

OFFICIAL_DOMAIN_BLOCKLIST: tuple[str, ...] = (
    *BLOCKED_SUFFIXES,
    *_MVP_AGGREGATORS,
    *_CF3_OBSERVED,
    *_BOARD_HOSTS,
)


def is_aggregator_domain(domain: str) -> bool:
    host = domain.lower().rstrip(".")
    return any(host_matches_suffix(host, suffix) for suffix in OFFICIAL_DOMAIN_BLOCKLIST)

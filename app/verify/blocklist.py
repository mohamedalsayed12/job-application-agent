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

OFFICIAL_DOMAIN_BLOCKLIST: tuple[str, ...] = (
    *BLOCKED_SUFFIXES,
    *_MVP_AGGREGATORS,
    *_BOARD_HOSTS,
)


def is_aggregator_domain(domain: str) -> bool:
    host = domain.lower().rstrip(".")
    return any(host_matches_suffix(host, suffix) for suffix in OFFICIAL_DOMAIN_BLOCKLIST)

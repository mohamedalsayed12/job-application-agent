"""Aggregator / job-board domains that can never be official (Stage 5 step 1)."""

from __future__ import annotations

from app.fetch.policy import host_matches_suffix

# MVP §7 examples plus close cousins already blocked for outbound fetch.
OFFICIAL_DOMAIN_BLOCKLIST: tuple[str, ...] = (
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "reed.co.uk",
    "adzuna.co.uk",
    "adzuna.com",
    "totaljobs.com",
    "cwjobs.co.uk",
    "monster.co.uk",
    "monster.com",
    "jobs.lever.co",
    "jobs.eu.lever.co",
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "boards.eu.greenhouse.io",
    "job-boards.eu.greenhouse.io",
)


def is_aggregator_domain(domain: str) -> bool:
    host = domain.lower().rstrip(".")
    return any(host_matches_suffix(host, suffix) for suffix in OFFICIAL_DOMAIN_BLOCKLIST)

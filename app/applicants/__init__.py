"""Applicant preference domain: matching salary vs discovery projection (D3)."""

from app.applicants.discovery import to_discovery_preference
from app.applicants.salary import job_excluded_for_salary, salary_match_status
from app.applicants.types import DiscoveryPreference, ExpectedSalary, PreferenceInput

__all__ = [
    "DiscoveryPreference",
    "ExpectedSalary",
    "PreferenceInput",
    "job_excluded_for_salary",
    "salary_match_status",
    "to_discovery_preference",
]

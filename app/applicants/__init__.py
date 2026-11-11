"""Applicant preference domain and salary matching (D3)."""

from app.applicants.salary import job_excluded_for_salary, salary_match_status
from app.applicants.types import ExpectedSalary, PreferenceInput

__all__ = [
    "ExpectedSalary",
    "PreferenceInput",
    "job_excluded_for_salary",
    "salary_match_status",
]

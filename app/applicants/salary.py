"""Salary status for matching. No expected salary ⇒ unknown; unknown is never excluded (T2, D4)."""

from __future__ import annotations

from decimal import Decimal

from app.applicants.types import ExpectedSalary
from app.models import SalaryStatus


def salary_match_status(
    expected: ExpectedSalary | None,
    *,
    job_salary_known: bool,
    job_min: Decimal | None = None,
    job_max: Decimal | None = None,
    job_currency: str | None = None,
    job_period: str | None = None,
) -> SalaryStatus:
    """Compare applicant expected salary to a job. Missing preference never filters."""
    if expected is None:
        return SalaryStatus.unknown
    if not job_salary_known:
        return SalaryStatus.unknown
    if job_currency is None or job_period is None:
        return SalaryStatus.unknown
    if job_currency.upper() != expected.currency.upper():
        return SalaryStatus.unknown
    if job_period != expected.period:
        return SalaryStatus.unknown
    # Prefer max when present (offer ceiling); else min.
    job_amount = job_max if job_max is not None else job_min
    if job_amount is None:
        return SalaryStatus.unknown
    if job_amount >= expected.amount:
        return SalaryStatus.meets
    return SalaryStatus.below


def job_excluded_for_salary(status: SalaryStatus) -> bool:
    """Milestone 1: only ``below`` excludes; ``unknown`` and ``meets`` stay in (T2)."""
    return status is SalaryStatus.below

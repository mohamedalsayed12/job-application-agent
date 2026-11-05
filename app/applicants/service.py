"""Create and update applicants with preferences (Milestone 1 admin)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.applicants.types import ExpectedSalary, PreferenceInput
from app.models import Applicant, ApplicantPreference

MILESTONE1_SECTOR = "tech"
SALARY_PERIODS = frozenset({"year", "month", "day", "hour"})


class ApplicantConflictError(Exception):
    """Email already registered."""


class ApplicantNotFoundError(Exception):
    """No applicant for the given id."""


class PreferenceValidationError(Exception):
    """Invalid preference fields."""


def parse_preference_input(
    *,
    target_roles: list[str],
    locations: list[str],
    sector: str = MILESTONE1_SECTOR,
    salary_amount: Decimal | str | None = None,
    salary_currency: str | None = None,
    salary_period: str | None = None,
) -> PreferenceInput:
    roles = tuple(r.strip() for r in target_roles if r and r.strip())
    locs = tuple(loc.strip() for loc in locations if loc and loc.strip())
    if not roles:
        raise PreferenceValidationError("at least one target role is required")
    if not locs:
        raise PreferenceValidationError("at least one location is required")
    sector_clean = sector.strip().lower()
    if sector_clean != MILESTONE1_SECTOR:
        raise PreferenceValidationError("sector must be tech for Milestone 1")

    amount_raw = salary_amount
    if isinstance(amount_raw, str):
        amount_raw = amount_raw.strip() or None
    # Blank amount means no salary (HTML forms still post currency/period defaults).
    if amount_raw is None:
        expected: ExpectedSalary | None = None
    else:
        currency = (salary_currency or "").strip() or None
        period = (salary_period or "").strip() or None
        if currency is None or period is None:
            raise PreferenceValidationError(
                "salary requires amount, currency, and period together"
            )
        amount = amount_raw if isinstance(amount_raw, Decimal) else Decimal(amount_raw)
        if amount <= 0:
            raise PreferenceValidationError("salary amount must be positive")
        if period not in SALARY_PERIODS:
            raise PreferenceValidationError(
                f"salary period must be one of: {', '.join(sorted(SALARY_PERIODS))}"
            )
        expected = ExpectedSalary(amount=amount, currency=currency.upper(), period=period)

    return PreferenceInput(
        target_roles=roles,
        locations=locs,
        sector=sector_clean,
        expected_salary=expected,
    )


def _apply_preference(row: ApplicantPreference, pref: PreferenceInput) -> None:
    row.target_roles = list(pref.target_roles)
    row.locations = list(pref.locations)
    row.sector = pref.sector
    if pref.expected_salary is None:
        row.salary_amount = None
        row.salary_currency = None
        row.salary_period = None
    else:
        row.salary_amount = pref.expected_salary.amount
        row.salary_currency = pref.expected_salary.currency
        row.salary_period = pref.expected_salary.period


def create_applicant(
    session: Session,
    *,
    email: str,
    preference: PreferenceInput,
    display_name: str | None = None,
) -> Applicant:
    email_clean = email.strip()
    if not email_clean or "@" not in email_clean:
        raise PreferenceValidationError("valid email is required")
    applicant = Applicant(
        email=email_clean,
        display_name=(display_name.strip() if display_name else None) or None,
    )
    session.add(applicant)
    session.flush()
    pref_row = ApplicantPreference(applicant_id=applicant.id)
    _apply_preference(pref_row, preference)
    session.add(pref_row)
    try:
        session.flush()
    except IntegrityError as exc:
        raise ApplicantConflictError("email already exists") from exc
    return applicant


def update_applicant(
    session: Session,
    applicant_id: uuid.UUID,
    *,
    preference: PreferenceInput,
    display_name: str | None = None,
    email: str | None = None,
) -> Applicant:
    applicant = session.get(Applicant, applicant_id)
    if applicant is None:
        raise ApplicantNotFoundError(str(applicant_id))
    if email is not None:
        email_clean = email.strip()
        if not email_clean or "@" not in email_clean:
            raise PreferenceValidationError("valid email is required")
        applicant.email = email_clean
    if display_name is not None:
        applicant.display_name = display_name.strip() or None
    pref_row = session.scalar(
        select(ApplicantPreference).where(ApplicantPreference.applicant_id == applicant_id)
    )
    if pref_row is None:
        pref_row = ApplicantPreference(applicant_id=applicant_id)
        session.add(pref_row)
    _apply_preference(pref_row, preference)
    try:
        session.flush()
    except IntegrityError as exc:
        raise ApplicantConflictError("email already exists") from exc
    return applicant


def list_applicants(session: Session) -> list[tuple[Applicant, ApplicantPreference]]:
    rows = session.execute(
        select(Applicant, ApplicantPreference)
        .join(ApplicantPreference, ApplicantPreference.applicant_id == Applicant.id)
        .order_by(Applicant.created_at)
    ).all()
    return [(a, p) for a, p in rows]


def get_applicant_with_preference(
    session: Session, applicant_id: uuid.UUID
) -> tuple[Applicant, ApplicantPreference] | None:
    row = session.execute(
        select(Applicant, ApplicantPreference)
        .join(ApplicantPreference, ApplicantPreference.applicant_id == Applicant.id)
        .where(Applicant.id == applicant_id)
    ).one_or_none()
    return (row[0], row[1]) if row else None

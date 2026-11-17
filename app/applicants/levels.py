"""The experience levels a person can choose, and what each one means wherever it is used (D63).

The form, the LinkedIn and Indeed searches and matching all read `LEVELS`, so a level is described in one place.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.matching.titles import ENTRY_TITLE
from app.models import ApplicantProfile, ExperienceLevel


@dataclass(frozen=True, slots=True)
class LevelFacts:
    label: str
    # LinkedIn's f_E filter: 1 internship, 2 entry level, 3 associate, 4 mid-senior level.
    linkedin_codes: tuple[str, ...]
    # Indeed has no level filter, so each level is searched as job titles; "{}" is the role.
    indeed_forms: tuple[str, ...]
    # Whether a job must show it is for people starting out before it counts for this level (D46).
    needs_entry_evidence: bool


# In the order the form shows them.
LEVELS: dict[ExperienceLevel, LevelFacts] = {
    ExperienceLevel.entry: LevelFacts(
        "Entry level (graduate, junior, entry)", ("2",), ("Graduate {}", "Junior {}", "Entry Level {}"), True
    ),
    ExperienceLevel.mid: LevelFacts("Mid level", ("3", "4"), ("{}",), False),
    ExperienceLevel.senior: LevelFacts("Senior", ("4",), ("Senior {}",), False),
    ExperienceLevel.internship: LevelFacts("Internship (students only)", ("1",), ("{} Intern",), False),
}
Levels = frozenset[ExperienceLevel]


def parse_levels(values: Iterable[str]) -> Levels:
    """Raises ValueError for a value that is not a level."""
    return frozenset(ExperienceLevel(value) for value in values)


def in_order(levels: Iterable[str]) -> list[ExperienceLevel]:
    chosen = set(levels)
    return [level for level in LEVELS if level in chosen]


def linkedin_levels(levels: Levels) -> str:
    """The f_E value of a search for jobs at any of these levels."""
    return ",".join(sorted({code for level in levels for code in LEVELS[level].linkedin_codes}))


def chosen_or_inferred(levels: Levels, typed_roles: Iterable[str]) -> Levels:
    """Someone with no saved levels, such as a person the operator added, is read from their titles.

    A graduate or junior title means entry level. Otherwise every level but internship.
    """
    if levels:
        return levels
    if any(ENTRY_TITLE.search(role) for role in typed_roles):
        return frozenset({ExperienceLevel.entry})
    return frozenset({ExperienceLevel.entry, ExperienceLevel.mid, ExperienceLevel.senior})


def saved_levels(session: Session) -> dict[uuid.UUID, Levels]:
    rows = session.execute(select(ApplicantProfile.applicant_id, ApplicantProfile.experience_levels))
    return {applicant_id: parse_levels(levels) for applicant_id, levels in rows}

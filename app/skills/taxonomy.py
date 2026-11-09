"""Load the skills list, map profile and job skills onto it, and report ESCO's coverage (A12)."""

from __future__ import annotations

import csv
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    ApplicantSkill,
    Job,
    JobFreshness,
    JobSkill,
    Skill,
    SkillLabel,
    SkillSource,
)
from app.skills.local_skills import LOCAL_SKILLS
from app.skills.mapping import LabelEntry, find_in_text, label_forms, normalize_label

ESCO_FILE = Path(__file__).parent / "data" / "esco_technical_skills.csv"
ESCO_VERSION = "ESCO v1.2.1"
LOCAL_VERSION = "local 2026-11-09"
LABEL_SEPARATOR = "|"
SUGGESTION_LIMIT = 10
MIN_SUGGESTION_LENGTH = 2


@dataclass(frozen=True, slots=True)
class SkillEntry:
    taxonomy_id: str
    source: SkillSource
    preferred: str
    alternatives: tuple[str, ...]
    version: str


@dataclass(slots=True)
class CoverageReport:
    open_jobs: int = 0
    jobs_with_skills: int = 0
    mentions: int = 0
    esco_mentions: int = 0
    local_mentions: int = 0
    most_common: list[tuple[str, str, int]] = field(default_factory=list)


def read_esco_file(path: Path = ESCO_FILE) -> list[SkillEntry]:
    with path.open(encoding="utf-8", newline="") as source:
        return [
            SkillEntry(
                row["uri"],
                SkillSource.esco,
                row["preferred"],
                tuple(label for label in row["alternatives"].split(LABEL_SEPARATOR) if label),
                ESCO_VERSION,
            )
            for row in csv.DictReader(source)
        ]


def local_entries() -> list[SkillEntry]:
    return [
        SkillEntry(f"local:{normalize_label(name)}", SkillSource.local, name, alternatives, LOCAL_VERSION)
        for name, alternatives in LOCAL_SKILLS.items()
    ]


def _labels(entry: SkillEntry) -> dict[str, tuple[str, bool]]:
    """Each searchable form of a skill's names, with its preferred name winning any clash."""
    forms: dict[str, tuple[str, bool]] = {}
    for label in label_forms(entry.preferred):
        forms.setdefault(normalize_label(label), (label, True))
    for alternative in entry.alternatives:
        for label in label_forms(alternative):
            forms.setdefault(normalize_label(label), (label, False))
    return {key: value for key, value in forms.items() if any(c.isalnum() for c in key)}


def load_taxonomy(session: Session, entries: list[SkillEntry] | None = None) -> int:
    """Safe to repeat. An entry dropped from the file stays, so saved links to it keep working."""
    entries = read_esco_file() + local_entries() if entries is None else entries
    existing = {row.taxonomy_id: row for row in session.scalars(select(Skill))}
    for entry in entries:
        row = existing.get(entry.taxonomy_id)
        if row is None:
            row = Skill(taxonomy_id=entry.taxonomy_id)
            session.add(row)
        row.source = entry.source
        row.preferred_label = entry.preferred
        row.alternative_labels = list(entry.alternatives)
        row.is_technical = True
        row.taxonomy_version = entry.version
    session.flush()

    ids = {row.taxonomy_id: row.id for row in session.scalars(select(Skill))}
    session.execute(delete(SkillLabel).where(SkillLabel.skill_id.in_([ids[e.taxonomy_id] for e in entries])))
    session.add_all(
        SkillLabel(skill_id=ids[entry.taxonomy_id], label=label, normalized=key, is_preferred=is_preferred)
        for entry in entries
        for key, (label, is_preferred) in _labels(entry).items()
    )
    session.flush()
    remap_applicant_skills(session)
    return len(entries)


def label_index(session: Session) -> dict[str, LabelEntry]:
    """Name to skill. Where two skills share a name, ours wins, because ESCO's "SPARK" isn't Apache Spark."""
    rows = session.execute(
        select(SkillLabel, Skill.source)
        .join(Skill, Skill.id == SkillLabel.skill_id)
        .where(Skill.is_technical)
        .order_by(SkillLabel.is_preferred.desc(), Skill.taxonomy_id)
    ).all()
    index: dict[str, LabelEntry] = {}
    for label, source in rows:
        current = index.get(label.normalized)
        entry = LabelEntry(label.skill_id, label.label, label.is_preferred)
        if current is None or source == SkillSource.local:
            index[label.normalized] = entry
    return index


def skill_for_text(index: dict[str, LabelEntry], typed: str) -> uuid.UUID | None:
    entry = index.get(normalize_label(typed))
    return entry.skill_id if entry else None


def remap_applicant_skills(session: Session) -> None:
    index = label_index(session)
    for row in session.scalars(select(ApplicantSkill)):
        row.skill_id = skill_for_text(index, row.text_as_typed)
    session.flush()


def map_job_skills(session: Session) -> int:
    """Stores the skills named in every job's advert. Safe to repeat. Returns how many are stored."""
    index = label_index(session)
    existing: dict[uuid.UUID, dict[uuid.UUID, JobSkill]] = {}
    for row in session.scalars(select(JobSkill)):
        existing.setdefault(row.job_id, {})[row.skill_id] = row
    total = 0
    jobs = session.execute(select(Job.id, Job.description).where(Job.description.is_not(None))).all()
    for job_id, description in jobs:
        rows = existing.get(job_id, {})
        hits = {hit.skill_id: hit for hit in find_in_text(description, index)}
        for skill_id, row in rows.items():
            if skill_id not in hits:
                session.delete(row)
        for skill_id, hit in hits.items():
            row = rows.get(skill_id) or JobSkill(job_id=job_id, skill_id=skill_id)
            row.matched_text = hit.matched_text
            row.how_matched = hit.how_matched
            session.add(row)
        total += len(hits)
    session.flush()
    return total


def suggest_skills(session: Session, typed: str) -> list[str]:
    """Names beginning with what was typed, shortest first. Used by the profile form."""
    key = normalize_label(typed)
    if len(key) < MIN_SUGGESTION_LENGTH:
        return []
    escaped = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return list(
        session.scalars(
            select(SkillLabel.label)
            .where(SkillLabel.normalized.like(f"{escaped}%", escape="\\"))
            .group_by(SkillLabel.label)
            .order_by(func.length(SkillLabel.label), SkillLabel.label)
            .limit(SUGGESTION_LIMIT)
        )
    )


def coverage_report(session: Session, *, top: int = 25) -> CoverageReport:
    """A12. Skills on neither list can't be seen here, so this is an upper limit on ESCO's real share."""
    report = CoverageReport()
    report.open_jobs = session.scalar(
        select(func.count()).select_from(Job).where(Job.freshness == JobFreshness.open)
    )
    rows = session.execute(
        select(JobSkill.job_id, Skill.preferred_label, Skill.source)
        .join(Skill, Skill.id == JobSkill.skill_id)
        .join(Job, Job.id == JobSkill.job_id)
        .where(Job.freshness == JobFreshness.open)
    ).all()
    report.mentions = len(rows)
    report.jobs_with_skills = len({job_id for job_id, _, _ in rows})
    counts = Counter((label, source) for _, label, source in rows)
    report.esco_mentions = sum(count for (_, source), count in counts.items() if source == SkillSource.esco)
    report.local_mentions = report.mentions - report.esco_mentions
    report.most_common = [(label, source, count) for (label, source), count in counts.most_common(top)]
    return report

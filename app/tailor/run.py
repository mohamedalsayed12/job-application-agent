"""Worker entry: tailor CVs for Apply / Consider fit reports (D55)."""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from itertools import islice
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.anthropic import AiCallFailure
from app.ai.injection import looks_like_injection
from app.applicants.cv_read import CvUnreadable, cv_text
from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate
from app.budget.pricing import estimate_cost
from app.core.config import Settings
from app.fetch.gateway import FetchGateway
from app.matching.verdicts import job_text, load_profiles
from app.models import (
    CompanyResearchStatus,
    CvFile,
    CvFileType,
    Job,
    JobReport,
    SpendKind,
    TailoredCv,
    TailoredCvStatus,
)
from app.tailor import ats as ats_mod
from app.tailor import draft as draft_mod
from app.tailor import keywords as keywords_mod
from app.tailor.company import research_company, research_for_prompt
from app.tailor.render import render_docx, render_pdf

PROMPT_VERSION = "cv-tailor-pipeline-v2"
TAILOR_VERDICTS = frozenset({"Apply", "Consider"})
REQUESTED = "requested"
MAX_PDF_BYTES = 5 * 1024 * 1024


@dataclass
class TailorRun:
    outcomes: Counter[str] = field(default_factory=Counter)
    written: set[uuid.UUID] = field(default_factory=set)
    stopped_by: str | None = None


@dataclass(frozen=True, slots=True)
class _Work:
    applicant_id: uuid.UUID
    job_id: uuid.UUID
    company_name: str
    apply_url: str | None
    posting: str
    profile: str
    cv: str
    cv_bytes: bytes
    input_hash: str
    prefer_apply: bool


def package_hash(
    *,
    posting: str,
    cv_bytes: bytes,
    profile: str,
    company_key: str,
    research_digest: str = "",
) -> str:
    material = "\n".join(
        [
            PROMPT_VERSION,
            company_key,
            research_digest,
            profile,
            hashlib.sha256(cv_bytes).hexdigest(),
            posting,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def write_tailored_cvs(
    session: Session,
    *,
    gateway: FetchGateway,
    settings: Settings,
    persist: Callable[[], None],
    now: datetime | None = None,
) -> TailorRun:
    """Tailor up to a cycle's worth of Apply/Consider packages; budget waits leave work pending."""
    now = now or datetime.now(UTC)
    run = TailorRun()
    api_key = settings.anthropic_api_key.strip()
    if settings.ai_provider.strip().lower() != "anthropic" or not api_key:
        run.stopped_by = "not_configured"
        return run

    work = list(islice(_todo(session, now, settings), settings.ai_tailor_per_cycle))
    if not work:
        return run

    cost = estimate_cost(
        provider=settings.ai_provider, kind=SpendKind.ai_cv_tailor, units=Decimal(1), settings=settings
    )
    gate = BudgetGate(session, clock=lambda: now)

    for item in work:
        try:
            await _process_one(
                session,
                item,
                gateway=gateway,
                settings=settings,
                api_key=api_key,
                gate=gate,
                cost=cost,
                persist=persist,
                now=now,
                run=run,
            )
        except BudgetWait:
            row = _row_for(session, item)
            # Keep a previous ready package downloadable; only mark waiting when nothing is ready yet.
            if row.status != TailoredCvStatus.ready.value:
                row.status = TailoredCvStatus.waiting_budget.value
                row.input_hash = item.input_hash
                row.prompt_version = PROMPT_VERSION
                row.updated_at = now
                session.flush()
            run.outcomes["waiting_budget"] += 1
            run.stopped_by = "budget"
            persist()
            return run
    return run


def tailor_allowed(settings: Settings, applicant_id: uuid.UUID) -> bool:
    allowed = settings.ai_applicants()
    return allowed is None or applicant_id in allowed


def request_tailored_cv(
    session: Session, applicant_id: uuid.UUID, job_id: uuid.UUID, *, now: datetime | None = None
) -> TailoredCv:
    """Ask the worker for a package on any job; a failed one is queued again."""
    now = now or datetime.now(UTC)
    row = session.scalars(
        select(TailoredCv).where(TailoredCv.applicant_id == applicant_id, TailoredCv.job_id == job_id)
    ).first()
    if row is None:
        row = TailoredCv(
            applicant_id=applicant_id,
            job_id=job_id,
            status=TailoredCvStatus.pending.value,
            input_hash=REQUESTED,
            prompt_version=PROMPT_VERSION,
            updated_at=now,
        )
        session.add(row)
    elif row.status == TailoredCvStatus.failed.value:
        row.status = TailoredCvStatus.pending.value
        row.input_hash = REQUESTED
        row.updated_at = now
    session.flush()
    return row


def _todo(session: Session, now: datetime, settings: Settings) -> list[_Work]:
    allowed = settings.ai_applicants()
    reports = {
        (row.applicant_id, row.job_id): row
        for row in session.scalars(
            select(JobReport).where(JobReport.verdict.in_(tuple(TAILOR_VERDICTS)))
        )
    }
    # A package row is itself demand: a person asked for it, or a report made one before.
    existing = {
        (row.applicant_id, row.job_id): row for row in session.scalars(select(TailoredCv))
    }
    pairs = [
        pair
        for pair in reports.keys() | existing.keys()
        if allowed is None or pair[0] in allowed
    ]
    if not pairs:
        return []

    applicant_ids = {applicant_id for applicant_id, _ in pairs}
    profiles = load_profiles(session, applicant_ids)
    cvs = {
        row.applicant_id: row
        for row in session.scalars(select(CvFile).where(CvFile.applicant_id.in_(applicant_ids)))
    }
    jobs = {
        row.id: row
        for row in session.scalars(select(Job).where(Job.id.in_({job_id for _, job_id in pairs})))
    }

    work: list[tuple[int, float, _Work]] = []
    for applicant_id, job_id in pairs:
        report = reports.get((applicant_id, job_id))
        current = existing.get((applicant_id, job_id))
        job = jobs.get(job_id)
        if job is None or not job.description:
            continue
        cv_file = cvs.get(applicant_id)
        if cv_file is None:
            _ensure_skipped(session, applicant_id, job_id, reason="no_cv", now=now)
            continue
        try:
            text = cv_text(
                cv_file.content, CvFileType(cv_file.file_type), max_chars=settings.ai_tailor_cv_chars
            )
        except CvUnreadable:
            _ensure_skipped(session, applicant_id, job_id, reason="unreadable", now=now)
            continue
        profile = profiles.get(applicant_id)
        profile_blob = profile.text if profile is not None else ""
        posting = job_text(
            title=job.title,
            company=job.company_name,
            places=job.places if isinstance(job.places, list) else [],
            apply_url=job.apply_url,
            description=job.description,
        )
        if looks_like_injection(posting):
            if current is not None and current.status == TailoredCvStatus.pending.value:
                current.status = TailoredCvStatus.failed.value
                current.input_hash = "posting_flagged"
                current.draft = {"reason": "posting_flagged"}
                current.updated_at = now
                session.flush()
            continue
        wanted = package_hash(
            posting=posting,
            cv_bytes=cv_file.content,
            profile=profile_blob,
            company_key=job.company_name.strip().lower(),
        )
        if current is not None and current.input_hash == wanted and current.status in {
            TailoredCvStatus.ready.value,
            TailoredCvStatus.skipped.value,
            TailoredCvStatus.failed.value,
        }:
            continue
        verdict = report.verdict if report is not None else None
        requested = current is not None and current.input_hash == REQUESTED
        # Someone pressed the button and is waiting, so they go first.
        prefer = 0 if requested else 1 if verdict == "Apply" else 2 if verdict == "Consider" else 3
        work.append(
            (
                prefer,
                -float(report.score) if report is not None else 0.0,
                _Work(
                    applicant_id=applicant_id,
                    job_id=job_id,
                    company_name=job.company_name,
                    apply_url=job.apply_url,
                    posting=posting,
                    profile=profile_blob,
                    cv=text,
                    cv_bytes=bytes(cv_file.content),
                    input_hash=wanted,
                    prefer_apply=verdict == "Apply",
                ),
            )
        )
    work.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in work]


def _ensure_skipped(
    session: Session, applicant_id: uuid.UUID, job_id: uuid.UUID, *, reason: str, now: datetime
) -> None:
    row = session.scalars(
        select(TailoredCv).where(TailoredCv.applicant_id == applicant_id, TailoredCv.job_id == job_id)
    ).first()
    payload = {"reason": reason}
    if row is None:
        session.add(
            TailoredCv(
                applicant_id=applicant_id,
                job_id=job_id,
                status=TailoredCvStatus.skipped.value,
                input_hash=reason,
                draft=payload,
                keywords=None,
                ats_report=None,
                prompt_version=PROMPT_VERSION,
                updated_at=now,
            )
        )
    else:
        row.status = TailoredCvStatus.skipped.value
        row.input_hash = reason
        row.draft = payload
        row.pdf_content = None
        row.docx_content = None
        row.pdf_size_bytes = None
        row.docx_size_bytes = None
        row.updated_at = now
    session.flush()


def _row_for(session: Session, item: _Work) -> TailoredCv:
    row = session.scalars(
        select(TailoredCv).where(
            TailoredCv.applicant_id == item.applicant_id, TailoredCv.job_id == item.job_id
        )
    ).first()
    if row is None:
        row = TailoredCv(
            applicant_id=item.applicant_id,
            job_id=item.job_id,
            status=TailoredCvStatus.pending.value,
            input_hash=item.input_hash,
            prompt_version=PROMPT_VERSION,
        )
        session.add(row)
        session.flush()
    return row


def _fail(session: Session, item: _Work, *, now: datetime, reason: str) -> None:
    row = _row_for(session, item)
    # Do not wipe a previous ready download when a refresh fails.
    if row.status == TailoredCvStatus.ready.value and row.pdf_content:
        row.draft = {**(row.draft or {}), "last_error": reason}
        row.updated_at = now
        session.flush()
        return
    row.status = TailoredCvStatus.failed.value
    row.input_hash = item.input_hash
    row.prompt_version = PROMPT_VERSION
    row.draft = {"reason": reason}
    row.updated_at = now
    session.flush()


async def _process_one(
    session: Session,
    item: _Work,
    *,
    gateway: FetchGateway,
    settings: Settings,
    api_key: str,
    gate: BudgetGate,
    cost: Decimal,
    persist: Callable[[], None],
    now: datetime,
    run: TailorRun,
) -> None:
    research = await research_company(
        session,
        gateway,
        company_name=item.company_name,
        apply_url=item.apply_url,
        now=now,
    )
    company_blob = research_for_prompt(research)
    available = research.status == CompanyResearchStatus.ready
    research_digest = hashlib.sha256(
        f"{research.status}:{research.official_domain or ''}:{(research.research or {}).get('excerpt', '')}".encode()
    ).hexdigest()
    # Recompute hash with research so a later research change rebuilds the package.
    item_hash = package_hash(
        posting=item.posting,
        cv_bytes=item.cv_bytes,
        profile=item.profile,
        company_key=item.company_name.strip().lower(),
        research_digest=research_digest,
    )
    item = _Work(
        applicant_id=item.applicant_id,
        job_id=item.job_id,
        company_name=item.company_name,
        apply_url=item.apply_url,
        posting=item.posting,
        profile=item.profile,
        cv=item.cv,
        cv_bytes=item.cv_bytes,
        input_hash=item_hash,
        prefer_apply=item.prefer_apply,
    )
    row = _row_for(session, item)
    if row.input_hash == item_hash and row.status == TailoredCvStatus.ready.value:
        return

    kw_call = await _paid_ai(
        gate,
        cost,
        settings,
        related_item_id=item.job_id,
        persist=persist,
        call=lambda: keywords_mod.extract_keywords(
            gateway, item.posting, settings=settings, api_key=api_key
        ),
    )
    if isinstance(kw_call, AiCallFailure):
        _fail(session, item, now=now, reason=kw_call.reason)
        run.outcomes["failed"] += 1
        persist()
        return
    keywords, kw_response = kw_call

    draft_call = await _paid_ai(
        gate,
        cost,
        settings,
        related_item_id=item.job_id,
        persist=persist,
        call=lambda: draft_mod.tailor_draft(
            gateway,
            profile=item.profile,
            cv=item.cv,
            company=company_blob,
            keywords=keywords,
            posting=item.posting,
            settings=settings,
            api_key=api_key,
        ),
    )
    if isinstance(draft_call, AiCallFailure):
        _fail(session, item, now=now, reason=draft_call.reason)
        run.outcomes["failed"] += 1
        persist()
        return
    draft, draft_response = draft_call

    pdf = render_pdf(draft)
    report = ats_mod.ats_report(pdf_bytes=pdf, draft=draft, keywords=keywords, master_cv=item.cv)
    prompt_tokens = kw_response.prompt_tokens + draft_response.prompt_tokens
    completion_tokens = kw_response.completion_tokens + draft_response.completion_tokens
    total_cost = cost * 2
    model = draft_response.model

    if report["missing_have_it"]:
        try:
            revised = await _paid_ai(
                gate,
                cost,
                settings,
                related_item_id=item.job_id,
                persist=persist,
                call=lambda: draft_mod.revise_draft(
                    gateway,
                    profile=item.profile,
                    cv=item.cv,
                    draft=draft,
                    missing=report["missing_have_it"],
                    settings=settings,
                    api_key=api_key,
                ),
            )
        except BudgetWait:
            # First draft is already grounded; ship it rather than discarding.
            revised = AiCallFailure(reason="budget")
        if not isinstance(revised, AiCallFailure):
            draft, rev_response = revised
            pdf = render_pdf(draft)
            report = ats_mod.ats_report(
                pdf_bytes=pdf, draft=draft, keywords=keywords, master_cv=item.cv
            )
            prompt_tokens += rev_response.prompt_tokens
            completion_tokens += rev_response.completion_tokens
            total_cost += cost
            model = rev_response.model or model

    docx = render_docx(draft)
    if len(pdf) > MAX_PDF_BYTES or len(docx) > MAX_PDF_BYTES:
        _fail(session, item, now=now, reason="too_large")
        run.outcomes["failed"] += 1
        persist()
        return

    row.status = TailoredCvStatus.ready.value
    row.input_hash = item_hash
    row.draft = draft
    row.keywords = {"items": keywords}
    row.ats_report = report
    row.company_research_id = research.id
    row.company_research_available = available
    row.pdf_content = pdf
    row.docx_content = docx
    row.pdf_size_bytes = len(pdf)
    row.docx_size_bytes = len(docx)
    row.model = model
    row.prompt_version = PROMPT_VERSION
    row.prompt_tokens = prompt_tokens
    row.completion_tokens = completion_tokens
    row.cost_amount = total_cost
    row.updated_at = now
    session.flush()
    run.outcomes["ready"] += 1
    run.written.add(item.applicant_id)
    persist()


async def _paid_ai(
    gate: BudgetGate,
    cost: Decimal,
    settings: Settings,
    *,
    related_item_id: uuid.UUID,
    persist: Callable[[], None],
    call,
):
    reservation = gate.reserve(
        SpendEstimate(
            provider=settings.ai_provider,
            kind=SpendKind.ai_cv_tailor,
            units=Decimal(1),
            unit_label="cv_tailor",
            estimated_cost=cost,
            related_item_id=related_item_id,
        )
    )
    persist()
    result = await call()
    if isinstance(result, AiCallFailure):
        gate.settle(reservation, consumed=False)
        persist()
        return result
    gate.settle(reservation, actual_cost=cost, units=Decimal(1))
    persist()
    return result

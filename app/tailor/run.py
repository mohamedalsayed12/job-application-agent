"""Worker entry: tailor CVs with the Resume-Matcher pipeline (D60).

Per package: parse the master CV into structure, extract the posting's keywords, ask for
path-based edits, apply only the edits that quote their source, inject keywords the master CV
already supports, strip stock AI wording and unsupported skills, then hold the result to the
master CV row by row before rendering.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from itertools import islice
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.anthropic import AiCallFailure, AiCallResponse
from app.ai.injection import looks_like_injection
from app.applicants.cv_read import CvUnreadable, cv_text
from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate
from app.budget.pricing import estimate_cost
from app.core.config import Settings
from app.fetch.gateway import FetchGateway
from app.matching.verdicts import job_text
from app.models import (
    CvFile,
    CvFileType,
    Job,
    JobReport,
    SpendKind,
    TailoredCv,
    TailoredCvStatus,
)
from app.tailor import ai
from app.tailor.ats import ats_report
from app.tailor.improver import apply_diffs, verify_diff_result
from app.tailor.preservation import finalize_ai_resume
from app.tailor.refiner import (
    analyze_keyword_gaps,
    drop_fabricated_skills,
    remove_ai_phrases,
)
from app.tailor.render import render_docx, render_pdf
from app.tailor.resume import all_text

PROMPT_VERSION = "resume-matcher-959e458"
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
    posting: str
    cv: str
    input_hash: str


@dataclass
class _Spend:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str | None = None

    def add(self, response: AiCallResponse) -> None:
        self.calls += 1
        self.prompt_tokens += response.prompt_tokens
        self.completion_tokens += response.completion_tokens
        self.model = response.model or self.model


def package_hash(*, posting: str, cv_bytes: bytes) -> str:
    material = "\n".join([PROMPT_VERSION, hashlib.sha256(cv_bytes).hexdigest(), posting])
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
        wanted = package_hash(posting=posting, cv_bytes=cv_file.content)
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
                    posting=posting,
                    cv=text,
                    input_hash=wanted,
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
    spend = _Spend()

    async def paid(call: Callable[[], Awaitable[Any]]) -> Any:
        result = await _paid_ai(gate, cost, settings, related_item_id=item.job_id, persist=persist, call=call)
        if not isinstance(result, AiCallFailure):
            spend.add(result[1])
        return result

    def failed(reason: str) -> None:
        _fail(session, item, now=now, reason=reason)
        run.outcomes["failed"] += 1
        persist()

    key = {"settings": settings, "api_key": api_key}
    parsed = await paid(lambda: ai.parse_master(gateway, item.cv, **key))
    if isinstance(parsed, AiCallFailure):
        return failed(parsed.reason)
    master = parsed[0]
    if master is None:
        return failed("cv_not_parsed")

    extracted = await paid(lambda: ai.extract_job_keywords(gateway, item.posting, **key))
    if isinstance(extracted, AiCallFailure):
        return failed(extracted.reason)
    keywords = extracted[0]

    diffs = await paid(
        lambda: ai.generate_resume_diffs(gateway, master=master, posting=item.posting, keywords=keywords, **key)
    )
    if isinstance(diffs, AiCallFailure):
        return failed(diffs.reason)
    tailored, applied, rejected = apply_diffs(master, diffs[0])
    warnings = verify_diff_result(master, tailored, applied)
    if rejected:
        warnings.append(f"{len(rejected)} change(s) rejected during verification")

    injectable = analyze_keyword_gaps(keywords, tailored, master).injectable
    if injectable:
        try:
            injected = await paid(
                lambda: ai.inject_keywords(
                    gateway, tailored=tailored, keywords=injectable, master=master, posting=item.posting, **key
                )
            )
        except BudgetWait:
            # The edited CV is already grounded; ship it rather than discard it.
            injected = AiCallFailure(reason="budget")
        if isinstance(injected, AiCallFailure) or injected[0] is None:
            reason = injected.reason if isinstance(injected, AiCallFailure) else "dropped_section"
            warnings.append(f"Keyword injection skipped: {reason}")
        else:
            tailored = finalize_ai_resume(tailored, injected[0], allow_review_claims=True)

    tailored, phrases = remove_ai_phrases(tailored, item.posting, protected_text=all_text(master))
    tailored, fabricated = drop_fabricated_skills(tailored, master)
    tailored = finalize_ai_resume(master, tailored, allow_review_claims=False)
    if phrases:
        warnings.append(f"Replaced stock AI wording: {', '.join(phrases)}")
    if fabricated:
        warnings.append(f"Removed skills the CV never mentions: {', '.join(fabricated)}")

    pdf = render_pdf(tailored)
    docx = render_docx(tailored)
    if len(pdf) > MAX_PDF_BYTES or len(docx) > MAX_PDF_BYTES:
        return failed("too_large")

    row = _row_for(session, item)
    row.status = TailoredCvStatus.ready.value
    row.input_hash = item.input_hash
    row.draft = tailored
    row.keywords = keywords
    row.ats_report = ats_report(pdf_bytes=pdf, resume=tailored, master=master, keywords=keywords, warnings=warnings)
    row.company_research_id = None
    row.company_research_available = False
    row.pdf_content = pdf
    row.docx_content = docx
    row.pdf_size_bytes = len(pdf)
    row.docx_size_bytes = len(docx)
    row.model = spend.model
    row.prompt_version = PROMPT_VERSION
    row.prompt_tokens = spend.prompt_tokens
    row.completion_tokens = spend.completion_tokens
    row.cost_amount = cost * spend.calls
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

"""Worker entry: tailor CVs with the Resume-Matcher pipeline (D60).

Per package: parse the master CV into structure, extract the posting's keywords, ask for
path-based edits, apply only the edits that quote their source, inject keywords the master CV
already supports, strip stock AI wording and unsupported skills, then hold the result to the
master CV row by row before rendering. Last, write a cover letter from that final CV.
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
from app.applicants.cv import cvs_by_applicant, pick_base_cv
from app.applicants.cv_read import CvUnreadable, cv_text
from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate
from app.budget.pricing import estimate_cost
from app.core.config import Settings
from app.fetch.gateway import FetchGateway
from app.matching.verdicts import job_text
from app.models import (
    ApplicantJobMatch,
    CvFile,
    CvFileType,
    Job,
    JobFreshness,
    JobReport,
    SpendKind,
    TAILOR_VERDICTS,
    TailoredCv,
    TailoredCvStatus,
    attempt_failed,
)
from app.tailor import ai
from app.tailor.ats import ats_report
from app.tailor.company import load_site_terms, with_keyword_sources
from app.tailor.improver import apply_diffs, verify_diff_result
from app.tailor.preservation import finalize_ai_resume
from app.tailor.refiner import (
    analyze_keyword_gaps,
    drop_fabricated_skills,
    remove_ai_phrases,
)
from app.tailor.render import render_docx, render_pdf
from app.tailor.resume import all_text
from app.tailor.voice import restore_lines, voice_problems

PROMPT_VERSION = "resume-matcher-959e458-cover-letter"
# Placeholder hash for a requested package, so it never looks already built.
REQUESTED = "requested"
MAX_PDF_BYTES = 5 * 1024 * 1024
_DONE = frozenset({TailoredCvStatus.ready.value, TailoredCvStatus.skipped.value, TailoredCvStatus.failed.value})


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
    company_name: str
    apply_url: str
    cv_file_id: uuid.UUID


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
            # A refresh that hits the cap keeps the previous download visible and stays due.
            if row.pdf_content:
                row.status = TailoredCvStatus.ready.value
            else:
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


def base_cv(session: Session, applicant_id: uuid.UUID) -> CvFile | None:
    """The CV a package is tailored from. Tailoring reads the person's CV only here."""
    return session.scalar(select(CvFile).where(CvFile.applicant_id == applicant_id))


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
        )
        session.add(row)
    elif row.status == TailoredCvStatus.failed.value or attempt_failed(row.status, row.draft):
        row.status = TailoredCvStatus.pending.value
        row.input_hash = REQUESTED
    row.requested_at = now
    row.updated_at = now
    session.flush()
    return row


def _todo(session: Session, now: datetime, settings: Settings) -> list[_Work]:
    """Packages due now: requests oldest first, then listed Apply jobs, then listed Consider jobs, best score first.

    A closed job is never tailored. A job hidden from the listings is tailored only when the person asked and it is still open.
    """
    allowed = settings.ai_applicants()
    is_open = Job.freshness == JobFreshness.open
    rows = {(row.applicant_id, row.job_id): row for row in session.scalars(select(TailoredCv))}
    rank: dict[tuple[uuid.UUID, uuid.UUID], tuple[int, float]] = {
        (applicant_id, job_id): (1 + TAILOR_VERDICTS.index(verdict), -float(score))
        for applicant_id, job_id, verdict, score in session.execute(
            select(JobReport.applicant_id, JobReport.job_id, JobReport.verdict, JobReport.score)
            .join(
                ApplicantJobMatch,
                (ApplicantJobMatch.applicant_id == JobReport.applicant_id)
                & (ApplicantJobMatch.job_id == JobReport.job_id),
            )
            .join(Job, Job.id == JobReport.job_id)
            .where(JobReport.verdict.in_(TAILOR_VERDICTS), is_open)
        )
    }
    rank |= {pair: (0, row.requested_at.timestamp()) for pair, row in rows.items() if row.requested_at is not None}
    pairs = sorted((pair for pair in rank if allowed is None or pair[0] in allowed), key=rank.__getitem__)
    if not pairs:
        return []
    jobs = {job.id: job for job in session.scalars(select(Job).where(Job.id.in_({j for _, j in pairs}), is_open))}

    texts: dict[uuid.UUID, str | None] = {}
    cvs = cvs_by_applicant(session, {applicant_id for applicant_id, _ in pairs})
    work: list[_Work] = []
    for applicant_id, job_id in pairs:
        job = jobs.get(job_id)
        if job is None or not job.description:
            continue
        current = rows.get((applicant_id, job_id))
        cv_file = pick_base_cv(cvs.get(applicant_id, []), title=job.title, advert=job.description)
        if cv_file is None:
            _skip(session, applicant_id, job_id, current, reason="no_cv", now=now)
            continue
        if cv_file.id not in texts:
            try:
                texts[cv_file.id] = cv_text(
                    cv_file.content, CvFileType(cv_file.file_type), max_chars=settings.ai_tailor_cv_chars
                )
            except CvUnreadable:
                texts[cv_file.id] = None
        text = texts[cv_file.id]
        if text is None:
            _skip(session, applicant_id, job_id, current, reason="unreadable", now=now)
            continue
        posting = job_text(
            title=job.title,
            company=job.company_name,
            places=job.places if isinstance(job.places, list) else [],
            apply_url=job.apply_url,
            description=job.description,
        )
        item = _Work(
            applicant_id=applicant_id,
            job_id=job_id,
            posting=posting,
            cv=text,
            input_hash=package_hash(posting=posting, cv_bytes=cv_file.content),
            company_name=job.company_name,
            apply_url=job.apply_url,
            cv_file_id=cv_file.id,
        )
        if current is not None and current.input_hash == item.input_hash and current.status in _DONE:
            continue
        if looks_like_injection(posting):
            _fail(session, item, now=now, reason="posting_flagged")
            continue
        work.append(item)
    return work


def _skip(
    session: Session,
    applicant_id: uuid.UUID,
    job_id: uuid.UUID,
    row: TailoredCv | None,
    *,
    reason: str,
    now: datetime,
) -> None:
    if row is not None and row.status == TailoredCvStatus.skipped.value and row.input_hash == reason:
        return
    if row is None:
        row = TailoredCv(applicant_id=applicant_id, job_id=job_id)
        session.add(row)
    row.status = TailoredCvStatus.skipped.value
    row.input_hash = reason
    row.draft = {"reason": reason}
    row.cover_letter = None
    row.keywords = None
    row.ats_report = None
    row.pdf_content = None
    row.docx_content = None
    row.pdf_size_bytes = None
    row.docx_size_bytes = None
    row.prompt_version = PROMPT_VERSION
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
    # A refresh that fails keeps the previous download and records this attempt so it is not retried.
    if row.pdf_content:
        row.status = TailoredCvStatus.ready.value
        row.input_hash = item.input_hash
        previous = row.draft if isinstance(row.draft, dict) else {}
        row.draft = {**previous, "last_error": reason}
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
    row = _row_for(session, item)
    row.status = TailoredCvStatus.running.value
    row.prompt_version = PROMPT_VERSION
    row.updated_at = now
    session.flush()
    persist()

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
    site = await load_site_terms(
        session,
        gateway,
        company_name=item.company_name,
        apply_url=item.apply_url,
        named_url=str(keywords.get("company_website") or ""),
        posting=item.posting,
        now=now,
    )
    keywords = with_keyword_sources(keywords, site.terms, site.note)

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
    tailored, voice_notes = await _human_voice(
        tailored,
        master=master,
        posting=item.posting,
        gateway=gateway,
        settings=settings,
        api_key=api_key,
        paid=paid,
    )
    warnings.extend(voice_notes)

    # Upstream treats the letter as optional output: its failure is a warning, never a failed CV.
    try:
        written = await paid(
            lambda: ai.generate_cover_letter(gateway, resume=tailored, posting=item.posting, **key)
        )
    except BudgetWait:
        written = AiCallFailure(reason="budget")
    letter = None
    if isinstance(written, AiCallFailure) or written[0] is None:
        reason = written.reason if isinstance(written, AiCallFailure) else "empty"
        warnings.append(f"Cover letter skipped: {reason}")
    else:
        letter, letter_phrases = remove_ai_phrases(written[0], item.posting, protected_text=all_text(master))
        if letter_phrases:
            warnings.append(f"Replaced stock AI wording in the cover letter: {', '.join(letter_phrases)}")

    pdf = render_pdf(tailored)
    docx = render_docx(tailored)
    if len(pdf) > MAX_PDF_BYTES or len(docx) > MAX_PDF_BYTES:
        return failed("too_large")

    row = _row_for(session, item)
    row.status = TailoredCvStatus.ready.value
    row.input_hash = item.input_hash
    row.cv_file_id = item.cv_file_id
    row.draft = tailored
    row.cover_letter = letter
    row.keywords = keywords
    row.ats_report = ats_report(
        pdf_bytes=pdf, docx_bytes=docx, resume=tailored, master=master, keywords=keywords, warnings=warnings
    )
    row.company_research_id = site.row.id if site.row is not None else None
    row.company_research_available = bool(site.terms)
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


async def _human_voice(
    tailored: dict[str, Any],
    *,
    master: dict[str, Any],
    posting: str,
    gateway: FetchGateway,
    settings: Settings,
    api_key: str,
    paid,
) -> tuple[dict[str, Any], list[str]]:
    """One revise call for lines that sound machine-written, then the person's own wording."""
    findings = voice_problems(master, tailored, posting=posting)
    if not findings:
        return tailored, []
    try:
        revised = await paid(
            lambda: ai.revise_voice(
                gateway,
                master=master,
                tailored=tailored,
                findings=findings,
                posting=posting,
                settings=settings,
                api_key=api_key,
            )
        )
    except BudgetWait:
        revised = AiCallFailure(reason="budget")
    if not isinstance(revised, AiCallFailure) and revised[0]:
        tailored, _, _ = apply_diffs(tailored, revised[0])
        tailored, _ = remove_ai_phrases(tailored, posting, protected_text=all_text(master))
        tailored, _ = drop_fabricated_skills(tailored, master)
        tailored = finalize_ai_resume(master, tailored, allow_review_claims=False)
    still = voice_problems(master, tailored, posting=posting)
    hard = [item for item in still if item["rule"] != "uniform"]
    notes: list[str] = []
    if hard:
        tailored = restore_lines(master, tailored, hard)
        notes.append("Restored lines that still sounded machine-written")
    elif any(item["rule"] == "uniform" for item in still):
        notes.append("Bullets stayed the same shape")
    return tailored, notes


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

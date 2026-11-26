"""A tailoring run is stored in Postgres, so a crash resumes it without paying again for finished steps."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts.service import delete_account
from app.applicants.cv import store_cv
from app.models import Applicant, Job, SpendKind, SpendLedger, TailoredCv, TailoredCvStatus, TailorRun
from app.tailor import run as tailor
from app.tailor.run import MAX_CRASHES, request_tailored_cv, write_tailored_cvs
from tests.test_cv_read import _docx
from tests.test_cv_tailor import _package, _prepare, _TailorAi
from tests.test_fit_report import CV_LINES
from tests.test_matching import SOON, _applicant


def _break_site_reading(monkeypatch: pytest.MonkeyPatch, times: int) -> None:
    """The step between the paid keyword call and the paid edit call raises `times` times, then works."""
    real = tailor.load_site_terms
    left = [times]

    async def flaky(*args, **kwargs):
        if left[0]:
            left[0] -= 1
            raise RuntimeError("site reader broke")
        return await real(*args, **kwargs)

    monkeypatch.setattr(tailor, "load_site_terms", flaky)


def _spends(session: Session, job_id) -> list[SpendLedger]:
    return list(
        session.scalars(
            select(SpendLedger).where(SpendLedger.kind == SpendKind.ai_cv_tailor, SpendLedger.related_item_id == job_id)
        )
    )


async def test_a_crashed_run_resumes_without_paying_again(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    ai = _TailorAi()
    applicant, gateway, settings = await _prepare(session, ai)
    _break_site_reading(monkeypatch, times=1)

    first = await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.commit, now=SOON)

    assert first.outcomes["crashed"] == 1
    assert ai.tailor_calls == ["parse", "keywords"]
    row = _package(session, applicant)
    assert row.status == TailoredCvStatus.running.value
    run = session.scalars(select(TailorRun)).one()
    assert set(run.steps) == {"parse", "keywords"}
    assert run.last_error == "RuntimeError: site reader broke"
    assert len(_spends(session, row.job_id)) == 2

    second = await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.commit, now=SOON)

    assert second.outcomes["ready"] == 1
    assert ai.tailor_calls == ["parse", "keywords", "diffs", "inject", "letter"]
    spends = _spends(session, row.job_id)
    assert [spend.actual_cost for spend in spends] == [Decimal("0.003")] * 5
    session.refresh(row)
    assert row.status == TailoredCvStatus.ready.value
    assert row.cost_amount == Decimal("0.003") * 5
    assert row.pdf_content.startswith(b"%PDF")
    assert session.scalars(select(TailorRun)).first() is None


async def test_a_run_resumed_from_stored_edits_writes_the_same_cv(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai = _TailorAi()
    applicant, gateway, settings = await _prepare(session, ai)
    real = tailor.render_pdf
    left = [1]

    def flaky(resume):
        if left[0]:
            left[0] -= 1
            raise RuntimeError("renderer broke")
        return real(resume)

    monkeypatch.setattr(tailor, "render_pdf", flaky)
    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.commit, now=SOON)
    paid_for = list(ai.tailor_calls)
    assert {"diffs", "inject"} <= set(paid_for)

    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.commit, now=SOON)

    assert ai.tailor_calls == paid_for
    resumed = _package(session, applicant)
    assert resumed.status == TailoredCvStatus.ready.value
    other = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    store_cv(session, other, _docx(CV_LINES))
    request_tailored_cv(session, other, resumed.job_id, now=SOON)
    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.commit, now=SOON)
    assert _package(session, other).draft == resumed.draft


async def test_one_crashing_package_does_not_stop_the_others(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai = _TailorAi()
    applicant, gateway, settings = await _prepare(session, ai)
    job = session.scalars(select(Job)).one()
    other = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    store_cv(session, other, _docx(CV_LINES))
    # A request goes first, so the crash comes before the listed Apply job in the same cycle.
    request_tailored_cv(session, other, job.id, now=SOON)
    _break_site_reading(monkeypatch, times=1)

    cycle = await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.commit, now=SOON)

    assert (cycle.outcomes["crashed"], cycle.outcomes["ready"]) == (1, 1)
    assert _package(session, other).status == TailoredCvStatus.running.value
    assert _package(session, applicant).status == TailoredCvStatus.ready.value

    cycle = await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.commit, now=SOON)

    assert cycle.outcomes["ready"] == 1
    assert _package(session, other).status == TailoredCvStatus.ready.value


async def test_a_package_that_keeps_crashing_fails_until_asked_again(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai = _TailorAi()
    applicant, gateway, settings = await _prepare(session, ai)
    _break_site_reading(monkeypatch, times=MAX_CRASHES)

    for _ in range(MAX_CRASHES):
        cycle = await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.commit, now=SOON)
        assert cycle.outcomes["crashed"] == 1
    cycle = await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.commit, now=SOON)

    assert cycle.outcomes["failed"] == 1
    row = _package(session, applicant)
    assert (row.status, row.draft) == (TailoredCvStatus.failed.value, {"reason": "crashed"})
    assert ai.tailor_calls == ["parse", "keywords"]
    assert session.scalars(select(TailorRun)).first() is None
    cycle = await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.commit, now=SOON)
    assert not cycle.outcomes

    request_tailored_cv(session, applicant, row.job_id, now=SOON)
    cycle = await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.commit, now=SOON)

    assert cycle.outcomes["ready"] == 1
    assert _package(session, applicant).status == TailoredCvStatus.ready.value


async def test_a_changed_advert_starts_a_fresh_run(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    ai = _TailorAi()
    applicant, gateway, settings = await _prepare(session, ai)
    _break_site_reading(monkeypatch, times=1)
    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.commit, now=SOON)
    job = session.get(Job, _package(session, applicant).job_id)
    job.description = (job.description or "") + "\nMust know Django."
    session.commit()

    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.commit, now=SOON)

    assert ai.tailor_calls == ["parse", "keywords", "parse", "keywords", "diffs", "inject", "letter"]
    assert _package(session, applicant).status == TailoredCvStatus.ready.value


async def test_stored_steps_go_with_the_package_and_the_account(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai = _TailorAi()
    applicant, gateway, settings = await _prepare(session, ai)
    _break_site_reading(monkeypatch, times=1)
    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.commit, now=SOON)
    assert session.scalars(select(TailorRun)).one().steps["parse"]["output"]["workExperience"]

    session.delete(_package(session, applicant))
    session.flush()
    assert session.scalars(select(TailorRun)).first() is None

    _break_site_reading(monkeypatch, times=1)
    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.commit, now=SOON)
    assert session.scalars(select(TailorRun)).one()

    delete_account(session, session.get(Applicant, applicant))
    session.expire_all()
    assert session.scalars(select(TailorRun)).first() is None
    assert session.scalars(select(TailoredCv)).first() is None

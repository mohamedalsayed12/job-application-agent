"""Step 15: measured outcomes reach the planner. T7, T8 and T9."""

from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import ceil

import httpx
import pytest
from sqlalchemy.orm import Session

from app.applicants.types import DiscoveryPreference
from app.core.config import Settings
from app.fetch.gateway import FetchGateway
from app.models import (
    Candidate,
    CandidateEvidence,
    CandidateState,
    EmployerBoard,
    ExtractedBy,
    JobFreshness,
    SearchResult,
    SearchRun,
    SearchRunStatus,
    SearchTemplate,
    SearchTemplateStatus,
    SpendKind,
    SpendLedger,
    TemplateStats,
)
from app.search.feedback import record_outcomes
from app.search.planner import EXPLORATION_SHARE, plan_searches
from app.search.schedule import create_runs_from_plan
from app.search.templates import UNPROVEN_RUN_THRESHOLD, ensure_search_templates
from app.worker import run_cycle
from tests.test_cleaning import _board, _Collector, _job
from tests.test_matching import SOON, _applicant, _gh

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
VERIFIED = T0 + timedelta(days=5)
AFTER = VERIFIED + timedelta(days=5)
PREF = DiscoveryPreference(target_roles=("Software Engineer",), locations=("London",), sector="tech")


def _settings() -> Settings:
    return Settings(search_request_unit_price=Decimal("0.003"))


def _run(session: Session, template: str, applicant: uuid.UUID, *, planned_at: datetime = T0) -> SearchRun:
    run = SearchRun(
        template_id=template,
        applicant_id=applicant,
        query=f"query {uuid.uuid4()}",
        provider="serpapi",
        status=SearchRunStatus.succeeded,
        finished_at=planned_at,
        created_at=planned_at,
    )
    session.add(run)
    session.flush()
    return run


def _candidate(session: Session, board: EmployerBoard) -> Candidate:
    candidate = Candidate(
        company_name="Acme",
        cleaned_name="acme",
        primary_domain="acme.example",
        board_system=board.system,
        board_instance=board.instance,
        board_id=board.board_id,
        state=CandidateState.registered,
        extracted_by=ExtractedBy.code,
    )
    session.add(candidate)
    session.flush()
    return candidate


def _found(session: Session, candidate: Candidate, run: SearchRun | None, *, at: datetime) -> None:
    result_id = None
    if run is not None:
        result = SearchResult(search_run_id=run.id, rank=1, url=f"https://x.example/{uuid.uuid4()}", title="t", snippet="s")
        session.add(result)
        session.flush()
        result_id = result.id
    session.add(
        CandidateEvidence(
            candidate_id=candidate.id,
            search_result_id=result_id,
            quoted_text="q",
            source_url="https://x.example",
            created_at=at,
        )
    )
    session.flush()


def _verified_board(session: Session) -> tuple[EmployerBoard, Candidate]:
    ensure_search_templates(session)
    board = _board(session)
    board.verified_at = VERIFIED
    return board, _candidate(session, board)


def _stats(session: Session, template: str, **fields: object) -> None:
    values: dict[str, object] = dict(
        run_count=10,
        total_cost=Decimal(0),
        new_candidates=Decimal(0),
        new_verified_boards=Decimal(0),
        relevant_jobs=Decimal(0),
        freshness_share=Decimal(1),
        updated_at=T0,
    )
    values.update(fields)
    session.add(TemplateStats(template_id=template, **values))
    session.flush()


def _slots(session: Session, total: int = 10, pref: DiscoveryPreference = PREF) -> dict[str, int]:
    plan = plan_searches(session, pref, total_slots=total)
    return {item.template_id: item.request_slots for item in plan.items}


def test_t8_refinding_a_board_already_in_the_directory_adds_nothing(session: Session) -> None:
    board, candidate = _verified_board(session)
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    finder = _run(session, "BD1", applicant)
    refinder = _run(session, "CF2", applicant, planned_at=AFTER)
    _found(session, candidate, finder, at=T0)
    _found(session, candidate, refinder, at=AFTER)

    record_outcomes(session, now=AFTER)

    assert finder.new_verified_boards == 1
    assert refinder.new_verified_boards == 0
    assert session.get(TemplateStats, "BD1").new_verified_boards == 1
    assert session.get(TemplateStats, "CF2").new_verified_boards == 0
    assert board.verified_at == VERIFIED


def test_t8_a_board_already_listed_before_any_search_found_it_credits_no_search(session: Session) -> None:
    _, candidate = _verified_board(session)
    run = _run(session, "BD1", _applicant(session, roles=["Backend Developer"], locations=["Manchester"]))
    _found(session, candidate, run, at=AFTER)

    record_outcomes(session, now=AFTER)

    assert run.new_verified_boards == 0


def test_a_board_from_the_seed_list_credits_no_search(session: Session) -> None:
    _, candidate = _verified_board(session)
    run = _run(session, "BD1", _applicant(session, roles=["Backend Developer"], locations=["Manchester"]))
    _found(session, candidate, None, at=T0)
    _found(session, candidate, run, at=T0 + timedelta(days=1))

    record_outcomes(session, now=AFTER)

    assert run.new_verified_boards == 0
    assert run.new_candidates == 0


def test_a_board_found_by_several_runs_of_the_first_plan_is_shared(session: Session) -> None:
    _, candidate = _verified_board(session)
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    first = _run(session, "BD1", applicant)
    second = _run(session, "CF2", applicant)
    later_plan = _run(session, "CF1", applicant, planned_at=T0 + timedelta(days=1))
    _found(session, candidate, first, at=T0)
    _found(session, candidate, second, at=T0 + timedelta(minutes=1))
    _found(session, candidate, later_plan, at=T0 + timedelta(days=1))

    record_outcomes(session, now=AFTER)

    assert (first.new_verified_boards, second.new_verified_boards) == (Decimal("0.5"), Decimal("0.5"))
    assert later_plan.new_verified_boards == 0


async def test_t9_job_outcomes_are_recorded_once_known_and_closed_jobs_lower_freshness(session: Session) -> None:
    ensure_search_templates(session)
    board = _board(session)
    board.verified_at = VERIFIED
    candidate = _candidate(session, board)
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    run = _run(session, "BD1", applicant)
    _found(session, candidate, run, at=T0)
    assert (run.new_verified_boards, run.relevant_jobs, run.freshness_share, run.outcomes_at) == (None, None, None, None)

    collector = _Collector(session)
    await collector.read(
        200,
        {
            "jobs": [
                _gh(1),
                _gh(2, title="Backend Engineer"),
                _gh(3, city="Berlin, Germany"),
                _gh(4, title="Account Manager"),
            ]
        },
    )
    _job(session, 2).freshness = JobFreshness.closed

    record_outcomes(session, now=SOON)

    assert run.outcomes_at == SOON
    assert run.relevant_jobs == 2
    assert run.freshness_share == Decimal("0.5")
    stats = session.get(TemplateStats, "BD1")
    assert stats.relevant_jobs == 2
    assert stats.freshness_share == Decimal("0.5")


def test_t9_cost_includes_the_ai_the_run_caused(session: Session) -> None:
    _verified_board(session)
    run = _run(session, "CF2", _applicant(session, roles=["Backend Developer"], locations=["Manchester"]))
    for provider, kind, cost in (("serpapi", SpendKind.search, "0.01"), ("anthropic", SpendKind.ai_extraction, "0.02")):
        session.add(
            SpendLedger(
                provider=provider,
                kind=kind,
                related_run_id=run.id,
                estimated_cost=Decimal(cost),
                actual_cost=Decimal(cost),
                units=Decimal(1),
                unit_label="request",
                occurred_at=T0,
            )
        )
    session.flush()

    record_outcomes(session, now=AFTER)

    assert session.get(TemplateStats, "CF2").total_cost == Decimal("0.03")


def test_recording_twice_gives_the_same_figures(session: Session) -> None:
    _, candidate = _verified_board(session)
    run = _run(session, "BD1", _applicant(session, roles=["Backend Developer"], locations=["Manchester"]))
    _found(session, candidate, run, at=T0)

    record_outcomes(session, now=AFTER)
    record_outcomes(session, now=AFTER)

    assert session.get(TemplateStats, "BD1").new_verified_boards == 1
    assert session.get(TemplateStats, "BD1").new_candidates == 1


def test_a_template_with_five_runs_stops_being_unproven(session: Session) -> None:
    ensure_search_templates(session)
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    _run(session, "BD1", applicant)
    _stats(session, "BD1", run_count=UNPROVEN_RUN_THRESHOLD)
    _run(session, "BD2", applicant)
    _stats(session, "BD2", run_count=UNPROVEN_RUN_THRESHOLD - 1)

    record_outcomes(session, now=AFTER)

    assert session.get(SearchTemplate, "BD1").status == SearchTemplateStatus.active
    assert session.get(SearchTemplate, "BD2").status == SearchTemplateStatus.unproven


def test_t7_higher_scorers_get_more_and_a_fifth_still_goes_to_unproven(session: Session) -> None:
    ensure_search_templates(session)
    _stats(session, "BD1", new_verified_boards=Decimal(6))
    _stats(session, "BD2", new_verified_boards=Decimal(2))
    _stats(session, "CF1", run_count=UNPROVEN_RUN_THRESHOLD - 1)

    slots = _slots(session, total=10)

    assert slots["BD1"] > slots["BD2"] > 0
    unproven = ("CF1", "CF1b", "CF2")
    assert sum(slots.get(template, 0) for template in unproven) >= ceil(10 * EXPLORATION_SHARE)
    assert sum(slots.values()) == 10


def test_a_cheaper_template_with_the_same_value_scores_higher(session: Session) -> None:
    ensure_search_templates(session)
    _stats(session, "BD1", new_verified_boards=Decimal(4), total_cost=Decimal("0.10"))
    _stats(session, "BD2", new_verified_boards=Decimal(4), total_cost=Decimal("0.40"))

    slots = _slots(session, total=10)

    assert slots["BD1"] > slots["BD2"]


def test_a_stale_haul_scores_below_a_fresh_one(session: Session) -> None:
    ensure_search_templates(session)
    _stats(session, "BD1", relevant_jobs=Decimal(50), freshness_share=Decimal("0.2"))
    _stats(session, "BD2", relevant_jobs=Decimal(50), freshness_share=Decimal("1"))

    slots = _slots(session, total=10)

    assert slots["BD2"] > slots.get("BD1", 0)


def test_all_proven_and_all_zero_shares_equally_and_drops_nobody(session: Session) -> None:
    ensure_search_templates(session)
    for template in ("CF1", "CF1b", "CF2", "BD1", "BD2"):
        _stats(session, template)

    slots = _slots(session, total=10)

    assert slots == {"CF1": 2, "CF1b": 2, "CF2": 2, "BD1": 2, "BD2": 2}


def test_the_reserve_keeps_a_low_scorer_alive_when_nothing_is_unproven(session: Session) -> None:
    ensure_search_templates(session)
    _stats(session, "BD1", new_verified_boards=Decimal(9))
    for template in ("CF1", "CF1b", "CF2", "BD2"):
        _stats(session, template)

    slots = _slots(session, total=20)

    assert all(slots[template] >= 1 for template in ("CF1", "CF1b", "CF2", "BD2"))
    assert slots["BD1"] > slots["BD2"]


def test_more_slots_buy_more_different_searches_never_the_same_one_twice(session: Session) -> None:
    ensure_search_templates(session)
    _stats(session, "BD1", new_verified_boards=Decimal(9))
    for template in ("CF1", "CF1b", "CF2", "BD2"):
        _stats(session, template)
    pref = DiscoveryPreference(
        target_roles=("Junior Python Developer", "Junior Backend Developer"), locations=("Manchester", "Leeds"), sector="tech"
    )
    plan = plan_searches(session, pref, total_slots=20)
    runs = create_runs_from_plan(session, plan, applicant_id=_applicant(session, roles=["Backend Developer"], locations=["Leeds"]))

    bd1 = [run.query for run in runs if run.template_id == "BD1"]
    bd2 = [run.query for run in runs if run.template_id == "BD2"]
    assert len(bd1) == 4
    assert len(set(bd1)) == 4
    assert len(bd2) == 1


def test_a_single_role_and_location_never_repeats_a_query(session: Session) -> None:
    ensure_search_templates(session)
    _stats(session, "BD1", new_verified_boards=Decimal(9))
    plan = plan_searches(session, PREF, total_slots=10)
    runs = create_runs_from_plan(session, plan, applicant_id=_applicant(session, roles=["Backend Developer"], locations=["Leeds"]))

    assert len([run for run in runs if run.template_id == "BD1"]) == 1


async def test_the_worker_records_outcomes_after_it_collects(session: Session) -> None:
    board, candidate = _verified_board(session)
    run = _run(session, "BD1", _applicant(session, roles=["Backend Developer"], locations=["Manchester"]))
    _found(session, candidate, run, at=T0)
    gateway = FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=frozenset({"boards-api.greenhouse.io"}),
        resolve=lambda host: [ipaddress.IPv4Address("93.184.216.34")],
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"jobs": [_gh(1)]})),
    )
    await run_cycle(session, gateway)
    await gateway.aclose()

    assert run.new_verified_boards == 1
    assert run.relevant_jobs == 1
    assert run.freshness_share == 1
    assert board.verified_at == VERIFIED


@pytest.mark.parametrize("total", [1, 5, 10, 17])
def test_every_slot_is_accounted_for(session: Session, total: int) -> None:
    ensure_search_templates(session)
    _stats(session, "BD1", new_verified_boards=Decimal(3))
    _stats(session, "BD2", new_verified_boards=Decimal(1), total_cost=Decimal("0.5"))
    _stats(session, "CF1", run_count=1)

    assert sum(_slots(session, total=total).values()) == total

"""Stage 5 step 3: public API access via Step 3 board clients."""

from __future__ import annotations

from app.boards import fetch_greenhouse_board, fetch_lever_board
from app.boards.result import BoardFetchResult
from app.fetch.gateway import FetchGateway
from app.models import FetchOutcome, VerificationOutcome
from app.verify.types import ApiAccessCheck, CheckRecord


async def check_api_access(
    gateway: FetchGateway,
    *,
    system: str,
    board_id: str,
) -> ApiAccessCheck:
    if system == "greenhouse":
        result = await fetch_greenhouse_board(gateway, board_id)
    elif system == "lever":
        result = await fetch_lever_board(gateway, board_id)
    else:
        return ApiAccessCheck(
            access="not_found",
            record=CheckRecord(
                check_type="api_access",
                outcome=VerificationOutcome.fail,
                evidence_snippet=f"unsupported_board_system:{system}",
            ),
        )
    return _from_board_result(result)


def _from_board_result(result: BoardFetchResult) -> ApiAccessCheck:
    company_names = tuple(
        str(job["company_name"])
        for job in result.jobs
        if isinstance(job.get("company_name"), str) and job["company_name"]
    )
    job_ids = tuple(str(job["id"]) for job in result.jobs if job.get("id") is not None)

    if result.is_full_read():
        return ApiAccessCheck(
            access="confirmed",
            record=CheckRecord(
                check_type="api_access",
                outcome=VerificationOutcome.pass_,
                evidence_url=result.url,
                evidence_snippet=result.label.value,
                http_status=result.status_code,
            ),
            instance=result.instance,
            job_count=len(result.jobs),
            company_names=company_names,
            job_ids=job_ids,
        )

    if result.label == FetchOutcome.not_found:
        return ApiAccessCheck(
            access="not_found",
            record=CheckRecord(
                check_type="api_access",
                outcome=VerificationOutcome.fail,
                evidence_url=result.url,
                evidence_snippet=result.detail or "not_found",
                http_status=result.status_code,
            ),
            instance=result.instance,
            failure_detail=result.detail or "not_found",
        )

    # failed / partial → retryable verification failure (D8 / T16)
    return ApiAccessCheck(
        access="failed",
        record=CheckRecord(
            check_type="api_access",
            outcome=VerificationOutcome.error,
            evidence_url=result.url,
            evidence_snippet=result.detail or result.label.value,
            http_status=result.status_code,
        ),
        instance=result.instance,
        failure_detail=result.detail or result.label.value,
    )

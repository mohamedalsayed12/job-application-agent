import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from app.api.accounts import admin_router as admin_accounts_router
from app.api.accounts import admin_session_router, current_applicant
from app.api.accounts import router as accounts_router
from app.api.applicants import router as applicants_router
from app.api.applications import router as applications_router
from app.api.apply_answers import router as apply_answers_router
from app.api.extension import router as extension_router
from app.api.forms import FormRefused, form_refused_page
from app.api.health import router as health_router
from app.api.interview_intel import router as interview_intel_router
from app.api.interview_job import router as interview_job_router
from app.api.jobs import router as jobs_router
from app.api.listings import JobUnavailable, job_unavailable_page
from app.api.listings import router as listings_router
from app.api.outreach import router as outreach_router
from app.api.privacy import router as privacy_router
from app.api.profile import router as profile_router
from app.api.budget import router as budget_router
from app.api.quality import router as quality_router
from app.api.workflow import router as workflow_router
from app.core.config import get_settings
from app.models import Applicant

SESSION_DAYS = 14


@asynccontextmanager
async def _refuse_unsafe_settings(_: FastAPI) -> AsyncIterator[None]:
    problems = get_settings().web_problems()
    if problems:
        raise RuntimeError("web app not started: " + "; ".join(problems))
    yield


settings = get_settings()
app = FastAPI(title="Job Application Agent", version="0.1.0", lifespan=_refuse_unsafe_settings)
app.add_middleware(
    SessionMiddleware,
    # A blank secret never serves anyone: startup refuses it. The random value is for tests.
    secret_key=settings.session_secret or secrets.token_urlsafe(32),
    session_cookie="jaa_session",
    max_age=SESSION_DAYS * 24 * 60 * 60,
    same_site="lax",
    https_only=settings.session_cookie_secure,
)
app.add_exception_handler(FormRefused, form_refused_page)
app.add_exception_handler(JobUnavailable, job_unavailable_page)
app.include_router(health_router)
app.include_router(privacy_router)
app.include_router(accounts_router)
app.include_router(workflow_router)
app.include_router(profile_router)
app.include_router(apply_answers_router)
app.include_router(applications_router)
app.include_router(extension_router)
app.include_router(interview_intel_router)
app.include_router(interview_job_router)
app.include_router(jobs_router)
app.include_router(outreach_router)
app.include_router(listings_router)
app.include_router(admin_session_router)
app.include_router(admin_accounts_router)
app.include_router(applicants_router)
app.include_router(quality_router)
app.include_router(budget_router)


@app.get("/", include_in_schema=False)
def home(applicant: Applicant | None = Depends(current_applicant)) -> RedirectResponse:
    return RedirectResponse("/listings" if applicant else "/login", status_code=303)

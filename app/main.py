import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.api.accounts import admin_router as admin_accounts_router
from app.api.accounts import admin_session_router
from app.api.accounts import router as accounts_router
from app.api.applicants import router as applicants_router
from app.api.audit import router as audit_router
from app.api.forms import FormRefused, form_refused_page
from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.api.listings import router as listings_router
from app.api.privacy import router as privacy_router
from app.api.profile import router as profile_router
from app.api.quality import router as quality_router
from app.api.review import router as review_router
from app.core.config import get_settings

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
app.include_router(health_router)
app.include_router(privacy_router)
app.include_router(accounts_router)
app.include_router(profile_router)
app.include_router(jobs_router)
app.include_router(listings_router)
app.include_router(admin_session_router)
app.include_router(admin_accounts_router)
app.include_router(applicants_router)
app.include_router(review_router)
app.include_router(audit_router)
app.include_router(quality_router)

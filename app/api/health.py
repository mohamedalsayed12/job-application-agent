import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.db import engine

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.api_route("/health", methods=["GET", "HEAD"])
def health() -> JSONResponse:
    """Liveness + DB reachability. Returns 503 when the database is unreachable."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        # Log server-side only; never expose DB host/user details to callers.
        logger.exception("Health check database probe failed")
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "database": "unreachable"},
        )
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "database": "ok"},
    )

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.db import engine

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> JSONResponse:
    """Liveness + DB reachability. Returns 503 when the database is unreachable."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — surface any DB failure as unhealthy
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "database": "unreachable", "detail": str(exc)},
        )
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "database": "ok"},
    )

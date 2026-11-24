"""Daily AI token totals from tables that record prompt and completion usage."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session


def tokens_used_today(session: Session, model: type, now: datetime) -> int:
    day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    used = session.scalar(
        select(func.coalesce(func.sum(model.prompt_tokens + model.completion_tokens), 0)).where(
            model.updated_at >= day_start
        )
    )
    return int(used or 0)

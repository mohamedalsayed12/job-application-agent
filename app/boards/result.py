"""Domain result for one board API fetch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.models import FetchOutcome

BoardSystem = Literal["greenhouse", "lever"]
BoardInstance = Literal["greenhouse", "lever_global", "lever_eu"]


@dataclass(frozen=True, slots=True)
class BoardFetchResult:
    """Labeled board fetch. Callers branch on `label`; never infer from HTTP alone."""

    label: FetchOutcome
    system: BoardSystem
    instance: BoardInstance | None
    jobs: tuple[dict[str, Any], ...] = ()
    status_code: int | None = None
    url: str | None = None
    raw_body: bytes = b""
    detail: str | None = None
    attempts: tuple[BoardInstance, ...] = field(default_factory=tuple)

    def is_full_read(self) -> bool:
        """True when the body is a whole board list (including zero jobs)."""
        return self.label in (FetchOutcome.complete, FetchOutcome.empty)


def invalid_board_id_result(system: BoardSystem) -> BoardFetchResult:
    return BoardFetchResult(
        label=FetchOutcome.failed,
        system=system,
        instance=None,
        detail="invalid_board_id",
    )

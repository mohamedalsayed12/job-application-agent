"""Domain shapes for code-only candidate extraction (Stage 3 / Step 6)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

BoardSystem = Literal["greenhouse", "lever"]
BoardInstance = Literal["greenhouse", "lever_global", "lever_eu"]


@dataclass(frozen=True, slots=True)
class BoardRef:
    system: BoardSystem
    instance: BoardInstance
    board_id: str


@dataclass(frozen=True, slots=True)
class CodeHit:
    """One code extraction from a stored result or posting row."""

    company_name: str | None
    board: BoardRef | None
    quoted_text: str
    source_url: str
    search_result_id: uuid.UUID | None = None
    apply_url: str | None = None

    def __post_init__(self) -> None:
        if self.company_name is None and self.board is None:
            raise ValueError("CodeHit needs a company_name and/or board")
        if len(self.quoted_text) > 200:
            object.__setattr__(self, "quoted_text", self.quoted_text[:200])

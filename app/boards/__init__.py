"""Public board API clients (Greenhouse, Lever) over the fetch gateway."""

from app.boards.greenhouse import fetch_greenhouse_board
from app.boards.lever import fetch_lever_board
from app.boards.result import BoardFetchResult

__all__ = [
    "BoardFetchResult",
    "fetch_greenhouse_board",
    "fetch_lever_board",
]

"""Parsed provider hits and call outcomes at the search boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class SearchHit:
    url: str
    title: str
    snippet: str


@dataclass(frozen=True, slots=True)
class ProviderSuccess:
    hits: tuple[SearchHit, ...]


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    reason: str
    retryable: bool = True


ProviderOutcome = ProviderSuccess | ProviderFailure

RunTerminal = Literal["succeeded", "failed", "inconclusive"]

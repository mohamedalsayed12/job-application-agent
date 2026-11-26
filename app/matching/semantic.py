"""Local text embeddings for matching titles by meaning (D68).

Used by TitleMeaning when judging whether a job title is close enough to a person's
target or related titles. Vectors are made on this machine, so matching costs nothing
and sends nothing away.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from functools import cache
from typing import Protocol

MODEL = "BAAI/bge-small-en-v1.5"


class Embedder(Protocol):
    model: str

    def query(self, text: str) -> list[float]: ...

    def passages(self, texts: list[str]) -> list[list[float]]: ...


class LocalEmbedder:
    model = MODEL

    def __init__(self) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(MODEL)

    def query(self, text: str) -> list[float]:
        return [float(x) for x in next(iter(self._model.query_embed(text)))]

    def passages(self, texts: list[str]) -> list[list[float]]:
        return [[float(x) for x in vector] for vector in self._model.passage_embed(texts)]


@cache
def local_embedder() -> LocalEmbedder:
    """Loaded once per process, on first use: loading the model takes a second or two."""
    return LocalEmbedder()


def similarity(a: Sequence[float], b: Sequence[float]) -> float:
    norms = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / norms if norms else 0.0

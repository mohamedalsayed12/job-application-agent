"""Order someone's matches by meaning, for the search box on the listings page (D48).

A job's vector comes from its title and its checked summary, or the start of its description when
it has no summary. Vectors are made on this machine, so searching costs nothing and sends nothing
away. The worker makes them ahead of time; a search makes any that are missing or out of date.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Sequence
from functools import cache
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ApplicantJobMatch,
    Job,
    JobEmbedding,
    JobFreshness,
    JobSummary,
    SummaryStatus,
)

MODEL = "BAAI/bge-small-en-v1.5"
DESCRIPTION_CHARS = 2000
_BATCH = 256


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
    """Loaded once per process, on the first search: loading the model takes a second or two."""
    return LocalEmbedder()


def job_text(job: Job, summary: str | None) -> str:
    return f"{job.title}\n{summary or (job.description or '')[:DESCRIPTION_CHARS]}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def vectors_for(session: Session, jobs: Sequence[Job], embedder: Embedder) -> dict[uuid.UUID, list[float]]:
    """Each job's vector, making and saving the ones that are missing or out of date."""
    ids = [job.id for job in jobs]
    summaries = dict(
        session.execute(
            select(JobSummary.job_id, JobSummary.summary_text).where(
                JobSummary.job_id.in_(ids), JobSummary.status == SummaryStatus.accepted
            )
        ).all()
    )
    texts = {job.id: job_text(job, summaries.get(job.id)) for job in jobs}
    stored = {row.job_id: row for row in session.scalars(select(JobEmbedding).where(JobEmbedding.job_id.in_(ids)))}
    stale = [
        job_id
        for job_id, text in texts.items()
        if job_id not in stored
        or stored[job_id].model != embedder.model
        or stored[job_id].source_hash != _digest(text)
    ]
    for start in range(0, len(stale), _BATCH):
        chunk = stale[start : start + _BATCH]
        for job_id, vector in zip(chunk, embedder.passages([texts[job_id] for job_id in chunk]), strict=True):
            row = stored.get(job_id) or JobEmbedding(job_id=job_id)
            row.model, row.source_hash, row.vector = embedder.model, _digest(texts[job_id]), vector
            stored[job_id] = row
            session.add(row)
    session.flush()
    return {job_id: list(stored[job_id].vector) for job_id in ids}


def similarity(a: Sequence[float], b: Sequence[float]) -> float:
    norms = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / norms if norms else 0.0


def closeness(query: str, vectors: dict[uuid.UUID, list[float]], embedder: Embedder) -> dict[uuid.UUID, float]:
    wanted = embedder.query(query)
    return {job_id: similarity(wanted, vector) for job_id, vector in vectors.items()}


def embed_matches(session: Session, embedder: Embedder) -> int:
    """Vectors for every open job in anyone's matches, so searches don't wait. Returns how many jobs."""
    jobs = session.scalars(
        select(Job)
        .where(Job.freshness == JobFreshness.open, Job.id.in_(select(ApplicantJobMatch.job_id)))
        .order_by(Job.id)
    ).all()
    vectors_for(session, jobs, embedder)
    return len(jobs)

from __future__ import annotations

import os
import re
import zlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alembic import command

from app.core.config import Settings, get_settings
from tests.isolation import ensure_database, resolve_test_database_name, with_database

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_ENV_FILE = _REPO_ROOT / ".env"
_TEST_DB_NAME = resolve_test_database_name(_REPO_ROOT.name)

# Swap DATABASE_URL to the per-worktree test DB before any import that builds
# app.core.db.engine (including app.matching → semantic).
_source_url = Settings(
    _env_file=str(_ENV_FILE) if _ENV_FILE.is_file() else None
).database_url
os.environ["DATABASE_URL"] = with_database(_source_url, _TEST_DB_NAME)
get_settings.cache_clear()

from app.core import db as app_db  # noqa: E402
from app.matching import semantic  # noqa: E402


@pytest.fixture(scope="session")
def migrated_engine() -> Iterator[Engine]:
    ensure_database(os.environ["DATABASE_URL"], _TEST_DB_NAME)
    engine = app_db.engine
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.fail(f"Pytest database {_TEST_DB_NAME!r} is unreachable: {exc}")
    command.upgrade(Config(str(_ALEMBIC_INI)), "head")
    yield engine


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    connection = migrated_engine.connect()
    transaction = connection.begin()
    bound = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield bound
    finally:
        bound.close()
        transaction.rollback()
        connection.close()


class WordEmbedder:
    """Stands in for the real model: texts that share words point the same way."""

    model = "test-words"
    dims = 256

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dims
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            vector[zlib.crc32(word.encode()) % self.dims] += 1.0
        return vector

    def query(self, text: str) -> list[float]:
        return self._vector(text)

    def passages(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]


@pytest.fixture(autouse=True)
def _no_real_embedding_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never download or load the embedding model."""
    monkeypatch.setattr(semantic, "local_embedder", WordEmbedder)

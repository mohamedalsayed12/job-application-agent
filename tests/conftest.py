from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import get_settings

_ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


@pytest.fixture(scope="session")
def migrated_engine() -> Iterator[Engine]:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        engine.dispose()
        pytest.skip(f"Postgres not available for unique-key checks: {exc}")
    command.upgrade(Config(str(_ALEMBIC_INI)), "head")
    yield engine
    engine.dispose()


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

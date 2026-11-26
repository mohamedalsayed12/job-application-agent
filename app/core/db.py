from collections.abc import Generator

from sqlalchemy import Engine, MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

# Naming convention keeps Alembic diffs stable as models appear in later steps.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# Without this, a port that accepts TCP but never answers (Docker's relay while the
# container starts) blocks a connect forever, so /health hangs instead of returning 503.
CONNECT_TIMEOUT_SECONDS = 5


def make_engine(url: str) -> Engine:
    return create_engine(
        url, pool_pre_ping=True, connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS}
    )


settings = get_settings()
engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

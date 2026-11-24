# Railway / production image: API or worker, same build.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

RUN uv sync --frozen --no-dev

EXPOSE 8000

# Railway sets RAILWAY_SERVICE_NAME per service (web vs worker).
CMD ["sh", "-c", "if [ \"$RAILWAY_SERVICE_NAME\" = \"worker\" ]; then exec python -m app.worker; else alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}; fi"]

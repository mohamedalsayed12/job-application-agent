# Job Application Agent (MVP)

Step 0 scaffold: FastAPI + SQLAlchemy 2 + PostgreSQL 16 + Alembic + uv.

Source of truth for product scope: [`MVP.md`](./MVP.md) and [`PROMPTS.md`](./PROMPTS.md).

Always-on agent quality stack (ponytail + pstack + thermos): [`docs/quality-stack.md`](./docs/quality-stack.md) · gate checklist [`docs/code-quality-gate.md`](./docs/code-quality-gate.md).

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Docker + Docker Compose

## Quick start

### 1. Start Postgres

```bash
docker compose up -d
```

This starts `postgres:16` on port `5432` with user/password/db `jaa` / `jaa` / `jaa` (see `.env.example`).

### 2. Configure environment

```bash
cp .env.example .env
```

### 3. Install dependencies

```bash
uv sync
```

If the project directory cannot hold a `.venv` symlink (some shared stores), point uv at a local path:

```bash
export UV_PROJECT_ENVIRONMENT=/tmp/jaa-venv
uv sync
```

### 4. Run migrations

```bash
uv run alembic upgrade head
```

### 5. Run the API

```bash
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Health check: `GET http://localhost:8000/health` — returns `200` when the DB is reachable, `503` otherwise.

### 6. Run tests

```bash
uv run pytest
```

## Stack (D22)

| Piece | Choice |
|---|---|
| Language | Python 3.12+ |
| API | FastAPI |
| ORM | SQLAlchemy 2.x |
| DB | PostgreSQL 16 (Docker Compose) |
| Migrations | Alembic |
| Deps | uv + `pyproject.toml` |
| Settings | pydantic-settings (`.env`) |
| Tests | pytest + httpx + pytest-asyncio |

## Notes

- Monthly budget is unset until B1; paid calls stay blocked (stubs in `.env.example`).
- Domain entities (section 15) start in Step 1 — this revision only establishes the migration chain.

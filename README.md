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

### 7. Classification audit sheet (Step 10)

Sample classified companies (`verified` / `needs_review` / `unsupported`) into
`audit_sample` rows and download an operator-fillable CSV (auditor columns stay
empty — do not auto-judge):

```bash
# Live DB after discovery/verification (--commit required so CSV ids match rows)
uv run python -m app.audit --commit --out step10-audit-sample.csv

# Offline template when Reed/SerpAPI keys are missing (£0 path; money_cap may stay unset).
# Writes ephemeral ids only — does not insert fake candidates into the DB.
uv run python -m app.audit --from-fixtures --out step10-audit-sample.csv
```

Admin UI: `GET /admin/audit` (same admin key as applicants). One open sheet:
a second download re-exports unfilled `audit_sample` rows. Offline fixture JSON:
`app/audit/data/classified_companies.json`.

### 8. Audit usefulness sheet (Step 16)

List the open matching jobs on each audited company's verified board, and write a sheet
with a blank `usefulness` column for the operator. This only reads the database, and it
needs `python -m app.matching` to have run first.

```bash
uv run python -m app.audit.usefulness --sheet docs/step10-audit-sample-4.csv \
  --out-sheet docs/step16-audit-usefulness.csv --out-jobs docs/step16-audit-jobs.csv
```

## Daily collection

The worker moves verified companies into the shared board directory, then reads each
Greenhouse and Lever board that is due. It stores every fetch run and every raw job.
A board is due once a day. A failed read retries after 1, 5 and 30 minutes, then waits
for the next daily read. The worker catches up after the machine has been off.

```bash
uv run python -m app.worker          # runs until stopped
uv run python -m app.worker --once   # one cycle
```

## Cleaning

The worker cleans each fetch as it stores it. Cleaning turns raw jobs into postings,
links duplicates to one master job, and sets each job to `open`, `closed` or `unknown`.
A job closes only after 2 consecutive complete fetches of its board do not list it and
the board then confirms that the listing has gone.

To replay every stored fetch and print job counts and completeness per field, run:

```bash
uv run python -m app.cleaning
```

Replaying is safe to repeat. Use it after a cleaning error or a parser change.

## Quality report

The worker checks every job for Recent, Real, Complete and Unique after each cycle that
reads a board, and stores the results on the job. To run the check now and print where
things stand against the targets, run:

```bash
uv run python -m app.quality
```

The full report is at `/admin/quality?admin_key=<ADMIN_KEY>`. Opening it changes nothing.
The latest results and their causes are in `docs/step13-quality-round2.md`.

## Matching

After each cycle that reads a board, the worker matches stored jobs to every applicant
on role, location and salary status. This uses rules only, so it costs nothing and makes
no requests. To run it now and print the best matches for each applicant, run:

```bash
uv run python -m app.matching
```

Running it again is safe. Afterwards the table holds only the current matches.

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
- Milestone 1 tables land in revision `0002_milestone1_schema` (`app/models.py`). Unique-key proofs are in `tests/test_unique_keys.py`.
- Leftover AI extraction (D30 / Step 7) uses Anthropic Claude Haiku 5.5 through the fetch gateway. Set `ANTHROPIC_API_KEY` in `.env` and a monthly `money_cap` in `budget_policy` for live runs; CI mocks the provider (T11/T12, `tests/test_ai_anthropic.py`). `AI_PROVIDER=openai` switches back to `gpt-4o-mini`.
- Tests assume an empty database. Once you have run live discovery, point tests at a separate one: `DATABASE_URL=postgresql+psycopg://…/job_app_agent_test uv run pytest`.
- Step 6 code extraction: `app/extraction/` (Greenhouse/Lever board IDs from URLs; Reed/Adzuna employer + apply). T10 fixtures under `tests/fixtures/extraction/`.
- Admin applicants (Milestone 1): set `ADMIN_KEY`, then open `/admin/applicants?admin_key=…` (or send header `X-Admin-Key`). Expected salary is a matching preference only (D3).

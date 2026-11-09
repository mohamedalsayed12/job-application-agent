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

The API will not start until `.env` has a private `ADMIN_KEY` and a `SESSION_SECRET` of at
least 32 characters. See `.env.example`. For plain http on localhost, also set
`SESSION_COOKIE_SECURE=false`, or the browser drops the login cookie.

## Accounts

People register at `/register`, log in at `/login` and see their account at `/account`.
Logging out ends the session on every device. Admin-created applicants have no password, so
they cannot log in until an admin makes them a one-time link at
`/admin/accounts/set-password-link`. The same link is the way back from a forgotten password,
because the app sends no email. The rules are in `MVP.md` decision D37.

People fill in their profile at `/profile`: target roles, locations, optional salary, skills,
certifications and licences, experience level and optional travel distance. They can upload one
CV (PDF or DOCX, up to 5 MB) to replace or delete at any time. Only its owner can download it,
and nothing reads its text. `/account` also deletes the account. The rules are in decision D38.

Admins log in at `/admin/login` with `ADMIN_KEY`. The session lasts 12 hours. Scripts can send
the key in an `X-Admin-Key` header instead. The key is not accepted in a URL.

Before the site is public:

- Run one web process only, because login limits are counted in that process.
- Behind a proxy, run uvicorn with `--proxy-headers` and `--forwarded-allow-ips` set to the
  proxy's address. Otherwise every visitor shares one IP limit.

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

Admin UI: `GET /admin/audit` (after logging in at `/admin/login`). One open sheet:
a second download re-exports unfilled `audit_sample` rows. Offline fixture JSON:
`app/audit/data/classified_companies.json`.

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

The full report is at `/admin/quality`, after logging in at `/admin/login`. Opening it changes nothing.
Relevance, meaning how well matches fit each applicant, has its own section, so poor matching
can be told apart from poor data.
The latest results and their causes are in `docs/step13-quality-round2.md`.

## Matching

After each cycle that reads a board, the worker matches stored jobs to every applicant
on role, location, salary status, skills and required licences. Each match lists the skills
the applicant has, the skills they don't, and any licence the advert requires that they don't
hold. This uses rules only, so it costs nothing and makes no requests. The rules are in
`MVP.md` decisions D35 and D40. To run it now and print the best matches for each
applicant, run:

```bash
uv run python -m app.matching
```

Running it again is safe. Afterwards the table holds only the current matches.

## Search feedback

After matching, and before each discovery pass plans its searches, the worker measures
what every finished search run led to: new candidates, new verified boards, relevant jobs,
freshness and cost. Each run keeps its own figures, and `template_stats` holds the totals.
The planner gives templates with 5 or more runs a share of the searches by score, and keeps
20% for exploration. The rules are in `MVP.md` section 8 and decision D36.

## Skills

Profile skills and job skills map to the same entries, so "Postgres" and "PostgreSQL" match. The list
is ESCO's technical skills plus our own marked additions. To load it, map every job and print how
much of it ESCO covers, run:

```bash
uv run python -m app.skills
```

Running it again is safe. Afterwards, the worker maps job skills in each cycle that reads a board. The
rules and results are in `MVP.md` decision D39. This service uses the ESCO classification of the
European Commission. Our list is an adapted version of it.

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
- Admin applicants (Milestone 1): set `ADMIN_KEY`, log in at `/admin/login`, then open `/admin/applicants` (or send header `X-Admin-Key`). Expected salary is a matching preference only (D3).

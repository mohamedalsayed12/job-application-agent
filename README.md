> **Portfolio overview** — A job-discovery and application-management backend built around a real applicant workflow. It collects listings from LinkedIn and Indeed through Apify, manages accounts and candidate preferences, and enforces a budget limit before paid collection runs. This repository is an evolving MVP, not a claim of production deployment.
>
> **Why this matters:** job searches fragment across sources, repeat the same searches, and make it difficult to track relevant opportunities. I structured the API around user profiles, repeatable collection jobs, explicit cost controls, and testable persistence.
>
> **Engineering decisions:** FastAPI with SQLAlchemy 2 and PostgreSQL 16; Alembic migrations; scheduled collection with persisted outcomes; authentication and per-user data access; external scraper cost safeguards.

---

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

This starts `postgres:16` on port `5436` with user/password/db `jaa` / `jaa` / `jaa` (see `.env.example`).

### 2. Configure environment

```bash
cp .env.example .env
```

Job listings come from LinkedIn and Indeed, scraped once a day through Apify. Set `APIFY_API_TOKEN`
in `.env` to the API token from
[Apify integrations](https://console.apify.com/settings/integrations). Without that token the
worker does not search. Each search is a paid Apify run, so it also needs a monthly money cap
in `budget_policy`. Until that cap is set, paid calls stay blocked. The scrapers and the search
limits have defaults; see `.env.example`.

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
certifications and licences, experience levels and optional travel distance. They can keep up to 10
CVs (PDF or DOCX, up to 5 MB each), label them by target job, and choose a primary CV. Uploading
a CV or asking to fill the profile sends its text to the AI provider for editable suggestions.
When AI reads a CV, the job titles it suggests appear as checkboxes under the target roles, and ticked ones are
added on save. Under the box the page lists the related titles that are searched as well (D67). Tailoring and
application preparation choose a CV for the job. Only its owner can download it.
`/account` also deletes the account and all its CVs. The rules are in decisions D38, D43 and D65.

Before anything is searched, "Suggest roles and places" on the profile asks Claude for job titles that fit the
person's skills and degrees, and towns near their locations. Each role names the skills or degrees it rests on, and code
drops any that rest on nothing in the profile or that matching would hide at their levels. The suggestions go into the
form with the number of daily LinkedIn searches the profile will run and their cost cap. Nothing is saved until the
person presses Save. The rules are in decision D64.

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

## Daily collection

Jobs come from LinkedIn and Indeed. Once a day the worker runs one LinkedIn search per
title and location. A person's titles are their target roles, then related titles Claude
suggests for them, up to 10 in each location (D67). Indeed searches for the graduate, junior and entry-level
forms of each role in each location. The same query is searched once per source, however
many people want it. LinkedIn uses the Black Falcon Data scraper
(`blackfalcondata/linkedin-job-scraper`): entry-level, last 3 days, newest first, up to
50 jobs. Indeed uses `valig/indeed-jobs-scraper` with country `uk`, those entry title
forms, a 3-day window and the same limit; D46 still filters on the match side. The
worker stores every run and every raw job, then cleans them into postings.

A search is due 24 hours after the last one for that source, so a machine that was off
catches up the next time the worker runs. A search that fails is recorded, and the next
attempt waits until the following day. A run that does not finish within 15 minutes is
stopped.

Set `APIFY_API_TOKEN` first. Each run is reserved against the monthly money cap before it
starts, and the recorded cost is what Apify reports. A LinkedIn run reserves up to £0.07, so
10 titles a day for one person in one place come to about £21 a month. Raise
`budget_policy.money_cap` to suit.

```bash
uv run python -m app.worker          # runs until stopped
uv run python -m app.worker --once   # one cycle
```

## Cleaning

The worker cleans each search as it stores it. Cleaning turns raw jobs into postings,
links duplicates to one master job, and sets each job to `open` or `closed`. LinkedIn
does not say when a job closes. A job closes when no search has found it for 14 days,
and opens again when a later search finds it.

To replay every stored fetch and print job counts and completeness per field, run:

```bash
uv run python -m app.cleaning
```

Replaying is safe to repeat. Use it after a cleaning error or a parser change.

## Quality report

The worker checks every job for Recent, Real, Complete and Unique after a cycle that
collects LinkedIn jobs or closes stale ones, and stores the results on the job. To run
the check now and print where things stand against the targets, run:

```bash
uv run python -m app.quality
```

The full report is at `/admin/quality`, after logging in at `/admin/login`. Opening it changes nothing.
Relevance, meaning how well matches fit each applicant, has its own section, so poor matching
can be told apart from poor data. LinkedIn listings start at medium confidence for Real,
because anyone can post there.

## Matching

After a cycle that collects LinkedIn jobs or closes stale ones, the worker matches stored jobs to every applicant
on role, location, salary status, skills and required licences. Each match lists the skills
the applicant has, the skills they don't, and any licence the advert requires that they don't
hold. A job fits a role when its title has every word of one of the person's titles (their
target roles and the related titles), or is close to one in meaning, compared on this machine.
This costs nothing and makes no requests. The rules are in `MVP.md` decisions D35, D40 and D67. To run it now and print the best matches for each
applicant, run:

```bash
uv run python -m app.matching
```

Running it again is safe. Afterwards the table holds only the current matches.

## Listings

Logged-in people see their matched jobs at `/listings`, best match first, with the reasons for each
score, LinkedIn as the source, and the apply link. A job you have an application for also
shows its status (Preparing, Needs your input, Ready to fill, Filled, Applied, Not applying, Failed).
Saving a profile matches that person straight away.
The rules are in `MVP.md` decision D42.

## Job summaries

The first time a logged-in person opens a job at `/jobs/{id}`, the app asks Claude for a short
summary of the posting. Code rejects it if it contains a number, date or name the posting doesn't,
and saves one per job for everyone. Summaries spend from the AI budget, so nothing is called until
a monthly `money_cap` is set in `budget_policy` and `ANTHROPIC_API_KEY` is in `.env`. Until then,
and whenever a summary is rejected, the page shows the description. The rules are in `MVP.md`
decision D41.

## Skills

Profile skills and job skills map to the same entries, so "Postgres" and "PostgreSQL" match. The list
is ESCO's technical skills plus our own marked additions. To load it, map every job and print how
much of it ESCO covers, run:

```bash
uv run python -m app.skills
```

Running it again is safe. Afterwards, the worker maps job skills in each cycle that collects LinkedIn jobs or closes stale ones. The
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

- Monthly budget is unset until B1; paid calls, including Apify searches, stay blocked (stubs in `.env.example`).
- Milestone 1 tables land in revision `0002_milestone1_schema` (`app/models.py`). Unique-key proofs are in `tests/test_unique_keys.py`.
- Tests assume an empty database. Once jobs have been collected, point tests at a separate one: `DATABASE_URL=postgresql+psycopg://…/job_app_agent_test uv run pytest`.
- Admin applicants (Milestone 1): set `ADMIN_KEY`, log in at `/admin/login`, then open `/admin/applicants` (or send header `X-Admin-Key`). Expected salary is a matching preference only (D3).

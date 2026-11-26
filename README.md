> **Portfolio overview** — A job-discovery and application-management backend built around a real applicant workflow. It collects listings from LinkedIn and Indeed through Apify, manages accounts and candidate preferences, and enforces a budget limit before paid collection runs. This repository is an evolving MVP, not a claim of production deployment.
>
> **Why this matters:** job searches fragment across sources, repeat the same searches, and make it difficult to track relevant opportunities. I structured the API around user profiles, repeatable collection jobs, explicit cost controls, and testable persistence.
>
> **Engineering decisions:** FastAPI with SQLAlchemy 2 and PostgreSQL 16; Alembic migrations; scheduled collection with persisted outcomes; authentication and per-user data access; external scraper cost safeguards.

---

# Job Application Agent (MVP)

Step 0 scaffold: FastAPI + SQLAlchemy 2 + PostgreSQL 16 + Alembic + uv.

Source of truth for product scope: [`MVP.md`](./MVP.md).

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

The home page `/` is the register form, with a link to log in underneath. People register at
`/register`, log in at `/login` and see their account at `/account`.

Registering starts a sign-up wizard at `/onboarding`: upload a CV, check the contact details read
from it, choose the jobs wanted, then availability and optional diversity questions. Each step is
saved when the person presses Continue, so someone who leaves halfway carries on from the same step
at their next login. Every contact detail the CV read suggests must appear in the CV text, and right
to work, sponsorship and diversity answers are never taken from it. Finishing opens `/listings`.
Accounts made before the wizard existed skip it.
Logging out ends the session on every device. There is no password reset: the app sends no email.

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

Jobs come from LinkedIn, Indeed and Google Jobs (D73). Each role and location is due on its
own (D72), so a title added today is searched on the worker's next cycle. A person's LinkedIn
titles are their target roles, then related titles Claude suggests for them, up to 10 in each
location (D68), at the experience levels they chose (D63). Indeed and Google Jobs search the
graduate, junior and entry-level forms of each role in each location. The same query is searched
once per source, however many people want it. LinkedIn uses the Black Falcon Data scraper
(`blackfalcondata/linkedin-job-scraper`): the person's levels, up to 100 jobs; the first search
of a role and location looks back 14 days, repeats keep 3. Indeed uses `valig/indeed-jobs-scraper`
with country `uk` and the same windows. Google Jobs uses SerpAPI (`engine=google_jobs`, `gl=uk`),
one page of up to 10 results per search, never fetching google.com directly. The worker stores
every run and every raw job, then cleans them into postings.

A search that worked waits the source's usual hours before running again. A search that failed
is tried again after an hour, not every cycle and not a whole day later. A run that does not
finish within 15 minutes is stopped.

Set `APIFY_API_TOKEN` and optionally `SERPAPI_API_KEY` first. Each run is reserved against the
monthly money cap before it starts. Apify costs settle at what Apify reports; SerpAPI settles at
the configured search ceiling. A LinkedIn run reserves up to £0.07, so 10 titles a day for one
person in one place come to about £21 a month. Raise `budget_policy.money_cap` to suit.


### Jobs on company websites (D69)

Not every employer posts on LinkedIn or Indeed, so once a day the worker also searches
Google through [SerpAPI](https://serpapi.com) the way a person would. For each target role
and location it:

1. Googles the role (not in quotes, so similar titles come up too) at entry level in that place with "careers", leaving LinkedIn, Indeed
   and the big job boards out of the query.
2. Skips any result that is still a job board, and opens the rest on the company's own site
   through the fetch gateway (robots.txt, rate limit and domain checks still apply).
3. Keeps a page only when it describes a job in schema.org `JobPosting` data. A careers
   page that only lists jobs is followed into up to 3 of its links that look like the role.
4. Keeps the job only when it fits: the same or a similar title (a Python or Software
   Engineer for a Backend Developer), or a different title whose advert asks for your
   skillset (at least 3 of your profile skills, and at least half of the skills it names).
   It must also be still open and
   posted in the last 30 days, it is on the employer's own site or tracking system
   (Greenhouse, Lever, Workable and similar), and **nothing in it leads back to LinkedIn or
   Indeed**: no LinkedIn or Indeed job links and no "posted on / apply via" wording.
5. Reads the next page of Google results while fewer than 5 jobs were found, up to 3 pages
   and 20 company pages per search.

Kept jobs go through the same cleaning, de-duplication, matching and AI judging as the
other sources, and show as "Company website" on the listings page. Set `SERPAPI_API_KEY`
to turn it on. Each Google page is one SerpAPI search, reserved against the monthly money
cap at £0.02.

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
score, LinkedIn as the source, and the apply link. Saving a profile matches that person straight away.
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
- Admin: set `ADMIN_KEY`, log in at `/admin/login` (or send header `X-Admin-Key` on GETs) to open `/admin/quality`. Expected salary is a matching preference only (D3).

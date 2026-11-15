# Cursor Prompts — MVP

> **Current job sources (15 November 2026, decision D54).** The app collects job listings from LinkedIn and Indeed, once a day, through Apify scrapers. Set `APIFY_API_TOKEN`. The prompts below are the original build steps and are not how jobs are collected now. See the README and MVP.md decision D54.

Prompts for the steps in `MVP.md` section 19, in order. Step numbers match the build order, so some numbers are skipped. They split the kickoff prompt at the end of `MVP.md` into smaller steps.

## How to use this file
1. **Start a new Cursor chat for each step.** `MVP.md` carries the plan, so chats stay short and focused.
2. **"Decide" prompts go in Plan mode.** Cursor compares options and stops. You choose, then use the "Record a decision" prompt.
3. **"Build" prompts go in Agent mode.** Do one step at a time, and run the tests before moving on.
4. **After every build,** use "Explain" (so you understand the code), then "Wrap up" (tests and commit).
5. **Respect the gates.** At step 13 and at the end of the milestone, don't continue until the gate is met.
6. **"You do" lines are for you, not Cursor.** Keys go in `.env`, never in a chat.

## Before you start (you do)
1. Create a **new, empty** folder called `job-application-agent`. Don't reuse the old one.
2. Put `MVP.md` and this `PROMPTS.md` in it, open the folder in Cursor, and run `git init`.
3. **Budget:** until you decide (MVP.md B1), the monthly cap stays at 0, so no paid calls can run.

---

## Universal prompts
**Record a decision** (after every Decide prompt)
```
Update MVP.md section 20: [add/change] decision D__ to say: [what I chose] because [reason]. Don't change anything else.
```

**Explain** (after every build)
```
Explain the code you just wrote as if I'm a junior backend developer: what each file does, how data flows through it, and why it's structured this way. Then ask me 3 questions to check I understood.
```

**Wrap up** (end of every step)
```
Check this step against MVP.md: list each acceptance test it covers (T-numbers), whether it passes, and anything missing. Update the README with how to run it. Give me a git commit message.
```

**Review** (end of each stage)
```
Review this stage's code like a senior engineer. List problems by severity (bugs, security, missing tests, structure) and suggest fixes. Don't change anything yet.
```

**Debug**
```
Here's the error: [paste]. Explain the cause before fixing it, then fix it with the smallest change possible.
```

**Back on plan** (if Cursor drifts)
```
Stop. This conflicts with MVP.md section [N]: [what's wrong]. Re-read that section and redo this step to match it.
```

---

## Stage 0: Setup

### Step 0: Project setup
**Decide (Plan mode)**
```
Read MVP.md. It's the whole plan, and we're starting from an empty folder. We're on step 0 of section 19 (project setup). Compare options for: language and web framework, database (PostgreSQL vs SQLite), how to run the database locally, migrations, dependency management, test setup, settings from environment variables, and folder layout. Use D22 as the starting proposal. Recommend. No code yet.
```
**Build (Agent mode)**
```
Set up the project with the options I chose: app skeleton, database connection, migrations, settings from .env with a .env.example, a .gitignore that excludes .env, a health endpoint that also checks the database, and tests for both. Explain each folder. Stop when the tests pass and tell me how to run everything.
```

---

## Stage A: Foundations

### Step 1: Data model
**Decide (Plan mode)**
```
Read MVP.md section 15. Design the data model for the applicant, spending, collection and job entities (leave the skills and matching entities for later). Show the tables, key fields, relationships, unique keys and indexes, with a diagram. Compare any real alternatives, e.g. JSON columns vs separate tables for raw responses. No code yet.
```
**Build (Agent mode)**
```
Implement the data model I approved, with migrations. Add tests proving the unique keys work: the same source job ID can't be stored twice.
```

### Step 2: Fetch gateway, budget gate and spend ledger
**Decide (Plan mode)**
```
Read MVP.md sections 11 and 12. Compare options for: the HTTP client, robots.txt checking and caching, per-host rate limiting, blocking private and internal addresses (including after redirects), and how the budget gate estimates costs before a call and records them after. No code yet.
```
**Build (Agent mode)**
```
Build the fetch gateway and budget gate exactly as MVP.md sections 11 and 12 describe: allowlist, robots.txt, one request per second per host, timeouts, size and redirect limits, private-address blocking, LinkedIn/Indeed/Glassdoor blocked, a cost estimate before every paid call and a ledger entry after. The monthly cap defaults to 0, so no paid calls run until I set it. Write tests for T3, T19, T20 and T21 using mocked responses, with no real network calls.
```

---

## Stage B: Applicants

### Step 4: Admin form for applicants
**Build (Agent mode)**
```
Read MVP.md Stage 0 and D3. Build a simple admin-only way to create and edit applicants: target roles, locations, sector (tech) and optional expected salary (amount, currency, period). Protect it with an admin key from .env until accounts exist. Salary is stored only as a matching preference. Tests: T2, plus a test that salary isn't readable by the search or budget code.
```

---

## Stage C: Jobs

### Step 12: Cleaning
**Build (Agent mode)**
```
Read MVP.md Stage 9 and section 13. Build cleaning: the common job format with a link to the raw response; conservative duplicate linking (strong matches only, uncertain pairs kept separate); freshness with the 14-day closing rule; completeness per field; and salary as known or unknown. Tests: T22, T23, T25 and T26.
```

### Step 13: Quality checker and report (gate)
**Build (Agent mode)**
```
Build the data quality checker (Recent, Real, Complete, Unique) from MVP.md section 14, and the admin-only quality report from section 16. Then run it on the collected data and show me where we stand against the targets: 95%+ complete, no expired jobs shown, under 2% duplicates.
```
**If it misses the targets (Agent mode)**
```
The quality report shows [numbers]. Find the biggest causes, show examples, and propose fixes in order of impact. Don't change code until I approve.
```

---

## Stage D: Matching

### Step 14: Matching
**Build (Agent mode)**
```
Read MVP.md Stage 10 and section 14. Build matching: role/title match, location fit and salary status (meets, below or unknown; unknown is never excluded). Matching runs after collection, for every applicant. Tests: T1 (salary never appears in search queries, AI inputs or budget calculations), T2 and T27.
```

---

## Stage E: Accounts, profile and listings (Milestone 2)

### Step 17: Register and log in
**Decide (Plan mode)**
```
Read MVP.md D26 and T29. Compare options for authentication: the password hashing library, sessions vs tokens, and how existing admin-created applicants move to real accounts. Recommend a standard, well-tested approach. No code yet.
```
**Build (Agent mode)**
```
Build register, log in and log out using the approach I chose. Passwords are stored only as secure hashes, pages with personal data need a login, and the admin key still protects admin pages. Tests: T29.
```

### Step 18: Profile and CV upload
**Decide (Plan mode)**
```
Read MVP.md sections 3, 15 and 22, and D25. Compare options for storing CV files privately (e.g. local disk vs object storage) and for checking file type and size safely. Recommend. No code yet.
```
**Build (Agent mode)**
```
Build the full profile: target roles, locations, optional salary, skills, certifications and licences, experience level, travel distance, and CV upload (PDF or DOCX up to 5 MB, stored privately, not parsed, replaceable). Add account deletion that removes the profile, CV, preferences and matches. Tests: T30, T31 and T32.
```

### Step 19: Skills taxonomy (ESCO)
**Decide (Plan mode)**
```
Read MVP.md V10 and A12. Check ESCO's current licence, download formats and update frequency, and compare loading it into our database vs calling its API. Recommend. No code yet.
```
**Build (Agent mode)**
```
Load the ESCO skills we need. Map profile skills (with autocomplete in the profile form) and job skills to the same entries, storing how each job skill was matched. Report what share of job skills map to an entry (A12). Tests: T33.
```

### Step 20: Match score with reasons
**Build (Agent mode)**
```
Read MVP.md section 14. Extend matching with skill overlap and required certifications or licences. Each match shows matched skills, missing skills, role match, location fit, salary status and missing licences. Keep Relevant reported separately from the quality measures. Tests: T34.
```

### Step 21: Job summaries
**Build (Agent mode)**
```
Read MVP.md Stage 12. Build job summaries: generated on first view, saved once per job, written only from the posting text, and rejected if they contain a number, date or name that isn't in the posting. AI caps go through the budget gate, and the job description is shown when there's no summary. Tests: T35 and T37.
```

### Step 22: Listings page (final gate)
**Build (Agent mode)**
```
Read MVP.md Stage 13. Build the listings page: open jobs only, sorted by match score, each with title, employer, location, salary or "not stated", summary, match reasons, when it was last confirmed open, source, and apply link. Tests: T36 and T37.
```
**Final check (Agent mode)**
```
Check the MVP's "done when" (MVP.md section 18, Milestone 2) item by item, with evidence. Prepare a list of 30 random jobs with links to their original postings so I can check them by hand. List anything not met.
```

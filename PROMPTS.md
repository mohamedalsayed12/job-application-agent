# Cursor Prompts — MVP

Prompts for every step in `MVP.md` section 19, in order. They split the kickoff prompt at the end of `MVP.md` into smaller steps.

## How to use this file
1. **Start a new Cursor chat for each step.** `MVP.md` carries the plan, so chats stay short and focused.
2. **"Decide" prompts go in Plan mode.** Cursor compares options and stops. You choose, then use the "Record a decision" prompt.
3. **"Build" prompts go in Agent mode.** Do one step at a time, and run the tests before moving on.
4. **After every build,** use "Explain" (so you understand the code), then "Wrap up" (tests and commit).
5. **Respect the gates.** At steps 10, 13 and 16, and at the end of each milestone, don't continue until the gate is met.
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
Read MVP.md. It's the whole plan, and we're starting from an empty folder. We're on step 0 of section 19 (project setup). Compare options for: language and web framework, database (PostgreSQL vs SQLite for a database-backed queue), how to run the database locally, migrations, dependency management, test setup, settings from environment variables, and folder layout. Use D22 as the starting proposal. Recommend. No code yet.
```
**Build (Agent mode)**
```
Set up the project with the options I chose: app skeleton, database connection, migrations, settings from .env with a .env.example, a .gitignore that excludes .env, a health endpoint that also checks the database, and tests for both. Explain each folder. Stop when the tests pass and tell me how to run everything.
```

---

## Stage A: Foundations (Milestone 1)

### Step 1: Data model
**Decide (Plan mode)**
```
Read MVP.md sections 9 and 15. Design the Milestone 1 data model (leave the Milestone 2 entities for later). Show the tables, key fields, relationships, unique keys and indexes, with a diagram, and explain how queue state is stored so a worker picks up work safely. Compare any real alternatives, e.g. JSON columns vs separate tables for evidence. No code yet.
```
**Build (Agent mode)**
```
Implement the data model I approved, with migrations. Add tests proving the unique keys work: the same board, the same official domain and the same source job ID can't be stored twice.
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

### Step 3: Greenhouse and Lever clients
**Build (Agent mode)**
```
Read MVP.md section 7 (Stage 5, step 3) and V5. First, check Greenhouse's and Lever's current public API docs and tell me how each one signals "not found", "empty board" and "error", and which date fields their responses really contain. Save a few real responses as test fixtures, including an empty or missing board. Then build clients that label every response complete, partial, failed, not_found or empty, with Lever trying the global instance and then the EU one. All requests go through the fetch gateway. Tests: T18, plus every label.
```

---

## Stage B: Discovery (Milestone 1)

### Step 4: Admin form for applicants
**Build (Agent mode)**
```
Read MVP.md Stage 0 and D3. Build a simple admin-only way to create and edit Milestone 1 applicants: target roles, locations, sector (tech) and optional expected salary (amount, currency, period). Protect it with an admin key from .env until accounts exist. Salary is stored only as a matching preference. Tests: T2, plus a test that salary isn't readable by the search or budget code.
```

### Step 5: Search planner and runner
**You do first:** sign up for a Reed API key, and for whichever search API you choose after the Decide prompt. Read Adzuna's terms yourself before switching it on (V9).

**Decide (Plan mode)**
```
Read MVP.md sections 5, 7 (Stage 2), 8 and 11, and V1–V4 and V9. Check the current docs, terms, pricing and limits for the search API candidates (starting with Brave Search API), Reed and Adzuna. For each, tell me: can we store result URLs and short snippets, can results be limited to one website, what it costs, its rate limits, and anything that blocks this use. Recommend a search provider, and flag anything I must confirm myself. No code yet.
```
**Build (Agent mode)**
```
Build the search templates (CF1, CF1b, CF2, CF3, BD1, BD2), the cold-start planner and the search runner, using the provider I chose. Everything goes through the fetch gateway and budget gate. Keep CF1b (Adzuna) switched off until I confirm its terms. Record every run's status, results and cost from the first run. Tests: T5, T6, T9 and the search-run states (planned, running, succeeded, failed, inconclusive), using mocked API responses.
```

### Step 6: Code extraction
**Build (Agent mode)**
```
Read MVP.md Stage 3. Build code-only extraction: Greenhouse and Lever board IDs from result URLs (including gh_jid links and Lever EU pages), and employer names and apply links from Reed and Adzuna results. Each candidate keeps evidence links to the exact results it came from. Tests: T10, using saved real results as fixtures.
```

### Step 7: AI extraction with grounding checks
**You do first:** get an API key for the AI provider you choose.

**Decide (Plan mode)**
```
Read MVP.md section 10 and V6. Compare AI providers and models for extracting employer names and official-domain candidates from search results: cost per item, structured output support and limits. Recommend. No code yet.
```
**Build (Agent mode)**
```
Build AI extraction for leftover results only, following every guardrail in MVP.md section 10: fixed output format, evidence quotes, code grounding checks, ungrounded items logged, caching by input with model and prompt version, and token caps through the budget gate. Search results are passed to the AI as data, never as instructions. Tests: T11 and T12, including a fixture page that contains instructions aimed at the AI.
```

### Step 8: Candidate queue, dedupe and retries
**Build (Agent mode)**
```
Read MVP.md section 9. Build the candidate queue: its states, the four-step dedupe before any expensive check, and retry scheduling (1 hour, 6 hours, 24 hours, then needs_review). A matching name alone must never mark a duplicate. Tests: T13, and the retry path of T16.
```

### Step 9: Verifier and review list
**Build (Agent mode)**
```
Read MVP.md section 7 (Stage 5, steps 1–4). Build the verifier: official-domain checks, ownership evidence levels, public API access using the step 3 clients, and the three results (verified, needs_review, unsupported). Then build a simple admin review list where I can approve, reject or mark unsupported, with my reason saved. Tests: T14, T15, T16, T17 and T18.
```

### Step 10: Audit, part 1 (gate)
**You do first:** this runs real discovery, so set a budget (B1) or use the £0 fallback in MVP.md section 11.

**Build (Agent mode)**
```
Run discovery for my test applicants until at least 20 companies have been classified. Pick a sample of 20 across verified, needs_review and unsupported, and create an audit sheet (audit_sample rows plus a CSV I can fill in) with each company's evidence links. Don't judge them yourself.
```
**You do:** check each company by hand. Was its classification right?

**Then (Agent mode)**
```
My audit results are in [file]. Compute accuracy against D21 (the accuracy half of T28), list the mistakes by cause, and propose rule changes. Don't change any code until I approve.
```

---

## Stage C: Collection and jobs (Milestone 1)

### Step 11: Registration and daily collection
**Decide (Plan mode)**
```
Read MVP.md Stages 7 and 8 and D22. Compare options for running daily jobs with one worker process (e.g. a scheduler inside the app, a separate worker, or a system cron job). Recommend. No code yet.
```
**Build (Agent mode)**
```
Register verified boards in the shared directory, and build daily collection from Greenhouse and Lever boards plus Reed (and Adzuna once switched on), storing every fetch run and raw response. Each board is collected once, however many applicants there are. Tests: T4 (collection keeps running when the budget is used up), T22 and T27.
```

### Step 12: Cleaning
**Build (Agent mode)**
```
Read MVP.md Stage 9 and section 13. Build cleaning: the common job format with a link to the raw response; conservative duplicate linking (exact job-ID matches first, then strong matches, uncertain pairs kept separate); three-state freshness with the two-complete-fetches closing rule and the safety brake; completeness per field; and salary as known or unknown. Tests: T22, T23, T24, T25 and T26.
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

## Stage D: Matching and feedback (Milestone 1)

### Step 14: Matching
**Build (Agent mode)**
```
Read MVP.md Stage 10 and section 14. Build Milestone 1 matching: role/title match, location fit and salary status (meets, below or unknown; unknown is never excluded). Matching runs after collection, for every applicant. Tests: T1 (complete it: salary never appears in search queries, AI inputs or budget calculations), T2 and T27.
```

### Step 15: Feedback into the planner
**Build (Agent mode)**
```
Read MVP.md section 8. Connect measured outcomes to the planner: relevant jobs found, new verified boards, freshness and cost per template. Only new boards count, templates with under 5 runs stay unproven, and 20% of the budget stays reserved for exploration. Tests: T7, T8 and T9.
```

### Step 16: Audit, part 2 (gate)
**Build (Agent mode)**
```
For the same 20 audit companies, list each verified board's open jobs that match my test applicants, with links, and add a usefulness column to the audit sheet. Don't judge them yourself.
```
**You do:** fill in the usefulness column.

**Then (Agent mode)**
```
My usefulness results are in [file]. Compute usefulness against D21 (completing T28), then check Milestone 1's "done when" in MVP.md section 18 item by item, with evidence. List anything not met.
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
Read MVP.md Stage 13. Build the listings page: open jobs only, sorted by match score, each with title, employer, location, salary or "not stated", summary, match reasons, when it was last confirmed open, source, apply link, and the Adzuna label where needed. Tests: T36 and T37.
```
**Final check (Agent mode)**
```
Check the MVP's "done when" (MVP.md section 18, Milestone 2) item by item, with evidence. Prepare a list of 30 random jobs with links to their original postings so I can check them by hand. List anything not met.
```

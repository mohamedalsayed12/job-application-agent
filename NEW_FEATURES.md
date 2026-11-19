# New features (post-MVP)

Catalog of post-MVP features planned and built on 15–16 November 2026. These sit **outside** the core MVP discovery loop in [`MVP.md`](MVP.md). The connected workflow is prepared as a stack of review branches.

| Feature | Review branch | Original worktree | State |
|---|---|---|---|
| Apply fill agent | `integration/apply-agent` | `jaa-apply-agent` | ready for review |
| Job application auto-apply | — (with `feat/apply-agent`) | — | planned: fill → review → notify user to click Submit |
| Fake listing agent | `feat/p2-fake-listing` | `jaa-p2-fake-listing` | merged onto the current base |
| Personalized outreach | `integration/outreach` | `jaa-outreach` | ready for review |
| Interview intel | `integration/interview-job-pack` | `jaa-interview-intel` | ready for review |
| CV tailoring | `integration/cv-tailor` | `jaa-cv-tailor` | ready for review |
| Indeed Apify scraper | `integration/indeed-apify` | `jaa-indeed-apify` | ready for review |

Plans live under `~/.cursor/plans/` (`apply_fill_agent_*`, `fake_listing_agent_*`, `outreach_messaging_feature_*`, `interview_intel_feature_*`, `cv_tailoring_pipeline_*`, `indeed_apify_scraper_*`). Auto-apply has no separate plan yet; it extends the apply fill agent.

---

## 1. Apply fill agent

**Goal.** Fill employer application forms from the user's saved answers and CV, then stop before Submit so the user can review.

**How it works**
1. User clicks **Prepare apply** on a listing.
2. The extension reads the questions on the employer page the person has open. It works on any site. There is no Greenhouse, Lever, or Apify form fetch.
3. Server maps fields to the person's apply answers, saved answers, account email, or an exact CV quote (patterns first, model only to point at one of those).
4. A required question with nothing grounded becomes `needs_input`. The app and the extension name the questions. The person answers, the answer is saved, and a later form that asks the same thing in other words reuses it.
5. Browser extension (MV3) fills the open tab, uploads the CV (tailored PDF when that job has one ready, otherwise the uploaded CV), scrolls to Submit, and **never** clicks Submit.
6. Status becomes `filled_awaiting_review`; in-app + extension notify the user.

**Locked choices**
- Fill UX: browser extension on the employer page (not server-side submit).
- The person clicks Fill. `activeTab` and `scripting` cover that employer tab. The extension does not hold a standing permission to read every site. Host access is only the local app, so it can fetch the plan and the CV.
- Demographics default to Prefer not to say; never invent required answers.

**New data.** `application_answers`, `application` (status machine), `extension_device` (hashed API token).

**Out of v1.** Auto-submit (see §2); LinkedIn Easy Apply inside LinkedIn; Workday multi-step beyond best-effort; email notifications.

---

## 2. Job application auto-apply

**Goal.** Prepare and fill the employer application for the user, then **notify them to review and click Submit themselves** — the product never clicks Submit.

**Depends on.** Apply fill agent (§1): same `application` row, fill plan, extension device token, and `application_answers`. Optionally uses a tailored CV (§6) when one is ready for that job.

**How it works**
1. User (or an Apply-verdict queue) starts **Prepare apply** → the extension reads the open page, the server maps grounded answers, then the extension fills and uploads the CV (§1).
2. Status becomes `filled_awaiting_review`. The extension scrolls to / highlights Submit but **does not click it**.
3. Notify the user (extension badge/notification + in-app banner on `/applications` and the listings chip): form is ready — review the answers, then click Submit on the employer page.
4. User reviews and clicks Submit themselves.
5. Optional: user marks the application as submitted in-app (status `submitted`) so the list stays accurate; no headless confirmation scrape required in v1.

**Locked choices**
- **Review, then notify user to click Submit** — never auto-click Submit, never server-side / Apify submit.
- Prefer tailored CV upload when a package exists; otherwise master CV.
- Refuse fill when required answers are still missing (`needs_input`); do not notify “ready to submit” until the form is fully filled.

**New data (on top of §1).** Optional user-reported `submitted` / `abandoned` on `application`; notification already covered by fill-agent badge + in-app banner.

**Status.** The extension stops before Submit, and the person can mark the application submitted or record an application sent elsewhere. No automatic submit is implemented.

**Out of scope.** Extension or Apify clicking Submit; LinkedIn Easy Apply inside LinkedIn; inventing answers to clear `needs_input`; silent apply with no user review.

---

## 3. Fake listing verification agent (D55)

**Goal.** Soft-fail check that a listing is real before it stays on `/listings`. Clear fakes stay off the UI; when the same role is found on the company site, upgrade that job's apply link.

**Workflow (per open job)**
1. Indeed-style text scam heuristics (fees, get-rich, chat-only hire, etc.).
2. Companies House Public Data API — dissolved / liquidation → fake; no match → soft unknown.
3. Official domain via DuckDuckGo HTML search + homepage name check (fetch gateway).
4. Careers-page title match → set `apply_url` to company URL, raise Real confidence to high.
5. Persist `job_verification` (migration `0022_job_verification`); the worker rematches in the same cycle whenever it checked a job.

**Locked choices**
- Soft gate: missing careers hit alone does **not** hide a job.
- No AI spend; page + CH fetches only.
- Do **not** mark fakes `freshness=closed` (LinkedIn would reopen them).
- Hide via Real confidence `low` → no match → off listings. `/jobs/{id}` and every action under it (tailor, tailored CV downloads, mark applied, interview practice, outreach) answer 404 through one check, `listed_job`, even before the rematch.

**Config.** `COMPANIES_HOUSE_API_KEY` (optional); allowlist `api.company-information.service.gov.uk`.

**Out of scope.** Paid SERP / Anthropic for verification; inventing a second job row for the “real” listing.

---

## 4. Personalized outreach

**Goal.** After the user marks an application submitted, find hiring managers / company figures for that job, research public signals, draft a human message from a curated success-pattern library, let the user edit, then send via Gmail or copy for LinkedIn.

**Flow**
1. User marks the job as applied → the job's card on `/listings` and its job page show **Find people** (and, once drafted, each contact's messages in folded sections under the summary; before applying they say to mark the job applied) (Apify LinkedIn people / employees actors, budget-gated).
2. Pick contact → **Research & draft** (About + quotes as evidence).
3. User edits subject/body.
4. **Send with Gmail** (OAuth) or **Copy for LinkedIn** + open profile.

**Locked choices**
- Contacts: Apify (not manual paste-only, not Apollo).
- Send: Gmail OAuth after approval; LinkedIn is copy/open only — **no** InMail/connect bots.
- Patterns: versioned in-repo library, not live Reddit scraping per draft.

**New data.** `outreach_contact`, `outreach_research`, `outreach_draft`, `outreach_send`, `gmail_connection` (sealed refresh token).

**Spend kinds.** `linkedin_people`, `linkedin_profile`, `ai_outreach`.

**Out of scope.** LinkedIn message automation; auto-send without showing the draft; Companies House officers API (later).

---

## 5. Interview intel

**Goal.** Compare public success/failure interview stories for a target role, then tailor a grounded practice plan to a matched job and employer.

**Sources (v1).** Reddit, YouTube, LinkedIn via Apify only. Instagram and TikTok deferred.

**Flow**
1. Profile → pick a target role → Generate.
2. Cache hit (7-day TTL on shared `role_key`) → show pack.
3. Else: Apify collect → AI label success vs failure → synthesize pack with citeable URLs → page.
4. On a matched job page, build a job pack from the role pack, advert, cached company research, and employer interview stories collected through Apify. If the role pack is missing, queue it first. If no employer stories are usable, use the role pack and advert.
5. Reuse a job pack while its source hash and seven-day TTL hold; rebuild when the advert, role evidence, company evidence, or company research changes.

**Pack sections.** Findings summary; why people succeeded; why people failed; differentiators; materials/resources; ordered practice plan.

**New data.** `InterviewIntelRequest`, `InterviewEvidence`, `InterviewIntelPack` (shared by normalized role key), and `InterviewJobPack` (per applicant and job).

**Spend kinds.** `interview_collect`, `ai_interview_analyse`, `ai_interview_plan`, `ai_interview_tailor`.

**Out of v1.** Mock interview chat; storing full HTML/video; direct social HTTP (Apify only).

---

## 6. CV tailoring pipeline

**Goal.** For every job on a person's listings with an AI score of 3.0 or more (out of 5), automatically produce a grounded tailored CV (PDF + DOCX), a cover letter written from it, plus an ATS keyword coverage report.

**Pipeline order** (Resume-Matcher's tailorer, MVP.md D60)
1. Gate (listed with AI score 3.0+, or requested + master CV + budget; hash reuse).
2. Parse the master CV into structured sections; keep only what the CV text shows.
3. Extract the posting's required skills, preferred skills and keywords.
4. Claude proposes path-based edits that quote what they replace; only quoted edits on allowed paths land.
5. One keyword-injection pass for posting terms the master CV supports but the edit lost.
6. Replace stock AI wording; remove skills the master CV never mentions.
7. Hold the result to the master CV row by row (identity fields, years, numbers, similarity).
8. Write a cover letter from the final CV and the posting (Resume-Matcher's cover-letter prompt, MVP.md D68); a failure is a warning, not a failed CV.
9. Render single-column ATS-friendly PDF + DOCX; ATS text-layer checks + keyword coverage.

**Locked choices**
- Trigger: automatic for every job that lands on a person's listings with an AI score of 3.0 or more, whatever the verdict label, in the person's tailoring scope (their locations, or everywhere when they ticked that); the "Tailor my CV" button covers any other open job. Jobs matching hides (training roles, unsafe jobs) are never tailored automatically. When the budget or the per-cycle cap is short, requested packages go first, then best score, then newest.
- CV and cover letter; no LaTeX.
- Never invent employers, dates, metrics, or skills.
- Master CV on profile stays the source of truth; tailored files are per-job copies.

**Out of scope.** Company web research in cover letters; fetching URLs embedded in advert HTML; tailoring jobs that are off the listings or score under 3.0 without a request; fabricating skills to “pass ATS”.

---

## 7. Indeed Apify scraper

**Goal.** Daily Indeed collection alongside LinkedIn, same role × location fan-out, through Apify only.

**Actor.** `valig/indeed-jobs-scraper` (cheap store option). Country default `uk`; `datePosted` aligned with LinkedIn’s 3-day window.

**How it fits**
- Worker: `collect_linkedin` then `collect_indeed`; clean/match if either ran.
- Normalize `source = "indeed"` into the same posting / master-job path.
- `indeed.com` stays on the fetch blocklist — never scraped directly (same as LinkedIn).

**Spend kind.** `indeed_jobs`.

**Out of scope.** Shared Apify client abstraction (duplicate LinkedIn loop once); live Apify smoke in CI.

---

## Shared constraints (all of the above)

- Paid work goes through `BudgetGate` (reserve → settle; over-cap is wait, not failure).
- Outside HTTP goes through the fetch gateway (SSRF, robots for pages, allowlists for APIs).
- No headless browser in-process for discovery (D14); apply fill and auto-apply use the **user's** browser extension instead.
- Feature code and behaviour tests ship in the same step; writers use isolated worktrees (`scripts/new-agent-worktree.ps1`).
- Account deletion must cascade new personal data (answers, applications, outreach, tailored CVs, Gmail tokens, interview requests where applicant-scoped).

## Merge note

The connected workflow is stacked in review branches: base cleanup → Indeed → CV tailoring → apply → outreach → interview. Alembic revisions form one chain through `0022_job_verification`. Auto-submit remains out of scope; the person reviews and submits the form.

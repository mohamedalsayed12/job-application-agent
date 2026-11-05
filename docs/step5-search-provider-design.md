# Step 5: Search provider Decide

**Status:** approved (D29). Milestone 1 search provider: Mojeek Business + Reed; Adzuna CF1b off.

**Sources:** MVP.md sections 5, 7 (Stage 2), 8 and 11; V1-V4 and V9; PROMPTS Step 5 Decide. Schema expectations from `docs/step1-data-model.md` (`search_run`, `search_result`). Spend path from `docs/step2-fetch-budget-design.md`.

**Checked:** Brave, Reed and Adzuna primary docs and terms (and Mojeek as the fallback that publishes storage rights), as of 5 Nov 2026. Third-party blogs are cited only where the vendor page is silent.

**Goal:** pick one permitted web search API for CF2, CF3, BD1 and BD2, and state what Reed and Adzuna allow for CF1 / CF1b, so Stage 2 can record runs, results and cost without scraping.

---

## Recommendation

1. **Search API for Milestone 1: Mojeek Search API, Business plan.** It is the only evaluated self-serve option that both supports site-limited search (V2) and publishes indefinite storage rights for result URLs, titles and snippets (V1). Price is £3 per 1,000 requests (ex VAT). Keep the provider name in settings so Build does not hard-code Mojeek into domain types.
2. **Do not use Brave Search self-serve for persisted evidence.** Brave's Search plan meets V2 and V3, and it is the MVP's named candidate, but its Terms of Use forbid storing Search Results beyond transient operational storage. Brave's own FAQ says retention needs a plan with storage rights (Order Form / Enterprise; price unpublished). That conflicts with `search_result` rows and candidate evidence. Revisit Brave only if Mohammed gets written storage rights.
3. **Reed Jobseeker API: on for CF1.** Free self-serve key. Returns employer names, apply / job URLs and description text that CF1 needs. No money cost against the search budget. Rate limit and display terms are not published on the developer page; Mohammed must confirm those himself (V4) after signup.
4. **Adzuna: keep CF1b off until V9.** Official terms allow personal job search and publishing listings with an Adzuna label, but commercial / government / academic use is a 14-day trial then a licence. Mohammed must read the terms and decide whether this MVP counts as commercial before anyone turns CF1b on. PROMPTS already says he signs up for Reed and the chosen search API himself; Adzuna stays off until he confirms.

Approved: stay on Mojeek Business + Reed; Adzuna CF1b off (D29).

---

## What Stage 2 needs from a search API

| Need | Why | MVP id |
|---|---|---|
| Persist URL, title, short snippet per result | Evidence for extraction and verification; `search_result.snippet` retention is terms-bound | V1, D15 |
| Limit results to one website (or equivalent) | BD1 / BD2 (Greenhouse and Lever boards). Fallback is post-filter by host if the API cannot | V2 |
| Known price, free credit and rate limits | Budget gate and planner (§11). Per run ≤10 search requests; per applicant ≤20 / day | V3 |
| Official API, no scraping | Bing retired; Google CSE closed to new customers; SerpApi-style Google scrapers ruled out | §5, D19 |

Reed and Adzuna are not the "search API". They are job APIs for CF1 / CF1b. They do not need `site:` filters. They do need clear rules on storing and showing job fields.

---

## Provider comparison

### Brave Search API (MVP named candidate)

| Question | Finding | Source |
|---|---|---|
| Store URLs and short snippets? | **Blocked on self-serve.** Terms §3(b)(i): no store / cache / database of Search Results except "transient storage required for operation". FAQ: "It is prohibited to retain any and all data… If you need to store the results… searchapi-support@brave.com." Pricing FAQ: storage only "with a plan that includes storage rights." | [Terms](https://api-dashboard.search.brave.com/documentation/resources/terms-of-service), [Help FAQ](https://api-dashboard.search.brave.com/documentation/resources/help-feedback), [Pricing FAQ](https://brave.com/search/api/) |
| Limit to one website? | **Yes.** `site:` operator in the query. Goggles can `$discard` / `$boost` by domain. Operators are documented as experimental. | [Search operators](https://api-dashboard.search.brave.com/documentation/resources/search-operators), [Goggles](https://api-dashboard.search.brave.com/documentation/resources/goggles) |
| Cost | Search plan: **$5 per 1,000 requests**. **$5 free credit per plan per month** (~1,000 Search requests). The "up to $20 free" figure is $5 × four plans (Search, Answers, Autosuggest, Spellcheck), not $20 on Search alone. Card required to activate (anti-fraud); prepay can be $0 to use only free credit. Answers plan is irrelevant for CF2/CF3/BD1/BD2. | [Pricing](https://brave.com/search/api/), [Blog 12 Feb 2026](https://brave.com/blog/most-powerful-search-api-for-ai/) |
| Rate limits | Search plan capacity **50 queries/second**. Headers: `X-RateLimit-*` on a 1-second sliding window plus a monthly quota window. 429 when exceeded. Only successful responses count toward quota and billing. | [Rate limiting](https://api-dashboard.search.brave.com/documentation/guides/rate-limiting), pricing capacity |
| Attribution | Free-credit blog copy asks for attribution on the project site / about page. Terms make "POWERED BY BRAVE" + logo optional if you elect attribution. | Blog + Terms §4(d) |
| Blocks this use? | **Yes for persisted evidence without an Order Form.** Transient in-memory use during one run might fit "transient storage", but Milestone 1 schema and T9 need durable `search_result` rows. Training / fine-tuning on results is also forbidden. | Terms §3(b) |

**Verdict:** technically strong for V2/V3; fails V1 on the self-serve path the MVP can buy today.

### Mojeek Search API (recommended)

| Question | Finding | Source |
|---|---|---|
| Store URLs and short snippets? | **Yes on Business (and Enterprise).** FAQ: indefinite retention on Business / Enterprise. Startup: no compile/copy/store; cache at most 1 hour for the same user. Terms list those store/cache bans under Startup only. | [API page FAQ](https://www.mojeek.com/services/search/web-search-api/), [API Terms](https://www.mojeek.com/about/legal/api-terms.html) §1.6 |
| Limit to one website? | **Yes.** `site` request parameter and `site:` in `q`. Also `fi` (include domains). | [Request parameters](https://www.mojeek.com/support/api/search/request_parameters.html) |
| Cost | Business: **£3 per 1,000 requests** (ex VAT). Startup £2 / 1k (no durable storage). PAYG; no auto-charge unless you top up. Card needed to open an account; free credit available on Startup signup (confirm amount in the dashboard). | [API pricing](https://www.mojeek.com/services/search/web-search-api/) |
| Rate limits | Business: **10 queries/sec**, **400,000 queries/day**. Startup: 5/sec, 100,000/day. | Same page |
| Fit for discovery | Terms allow use "for the sole purpose of discovering sources of information" outside a classic search UI. Combining with other sources is allowed. Do not put results into a search index of your own. | API Terms §1.6 |
| Blocks this use? | **No on Business**, if we store only URL / title / snippet (and keep retention deletable). Startup is blocked for durable `search_result` rows. | - |

**Verdict:** choose Business. Slightly dearer than Brave's list price in GBP terms at current FX, but V1 is met without a sales call.

### Reed Jobseeker API (CF1)

| Question | Finding | Source |
|---|---|---|
| Store URLs and short snippets / job fields? | **Vendor-silent on the developer page.** Docs describe search + job-detail JSON (employer name, titles, salaries, `jobUrl`, `externalUrl`, expiration, description). Site policies page is jobseeker privacy / cookies, not API reuse. Third parties note "subject to Reed's usage and display terms" without citing a public API licence. | [Jobseeker API](https://www.reed.co.uk/developers/Jobseeker), [reed.co.uk/policies](https://www.reed.co.uk/policies) |
| Limit to one website? | N/A (job board API, not web search). | - |
| Cost | **Free** self-serve Jobseeker key. HTTP Basic auth: API key as username, empty password. | Developer page |
| Rate limits | **Not published** on the official developer docs. Third-party write-ups often claim ~1,000 requests/day and a 429; treat as undocumented until measured or confirmed with Reed. Search pages with `resultsToTake` ≤100 and `resultsToSkip`. | Official docs silent; e.g. CleanJobData / Scraperly (secondary) |
| Expired jobs | Job detail for a missing id returns a blank job. Useful for the closing rule once V4 is confirmed. | Developer "Jobs - Details" notes |
| Blocks this use? | **Not from published API docs**, but **V4 is open**: Mohammed must confirm display / storage for showing jobs in listings (Milestone 2) and any fair-use cap after he has a key. | V4 |

**Verdict:** enable CF1 in Build with mocked responses first; live key is Mohammed's signup. Enforce a conservative client throttle in the fetch gateway until the real limit is known.

### Adzuna API (CF1b, off until V9)

| Question | Finding | Source |
|---|---|---|
| Store / show listings? | Publishing Adzuna ads is a permitted use **with attribution**: "Adzuna" label ≥116×23 px, linked to adzuna.co.uk (or local domain). On termination, remove all Adzuna data from sites. Description in search responses is a **snippet only**. | [Terms of Service](https://developer.adzuna.com/docs/terms_of_service), [Search docs](https://developer.adzuna.com/docs/search) |
| Commercial use? | Commercial / government / academic orgs (and affiliates / individuals acting for them): **14-day trial** to validate coverage/quality only. Ongoing use, or any product that generates revenue or leads, needs a **licence**. Personal job search and personal/academic research are listed as permitted without that licence language. | Same Terms |
| Limit to one website? | N/A. | - |
| Cost | Free key to start. Paid personal/academic volume plans exist; commercial licence price not published on the developer site. | Terms "Access and limits" |
| Rate limits | Official terms: limits "shown in your account" and may change. Numeric defaults (**25/min, 250/day, 1,000/week, 2,500/month**) appear only in secondary write-ups, not on the public terms page checked today. | Terms; CleanJobData / JobsPipe (secondary) |
| Extra restriction | Do not contact third-party content providers behind Adzuna listings; queries go via Adzuna. Multiple accounts for one entity = breach. Aggregated stats for publication need written consent (MVP §5). | Terms |
| Blocks this use? | **Possibly.** If Mohammed treats the MVP as a personal job-search tool, permitted-use + label may be enough. If it is a product, org tool, or lead generator, CF1b needs a licence after 14 days. **He must decide (V9).** Keep the template wired but disabled. | V9, PROMPTS |

**Verdict:** CF1b stays feature-flagged off. No Adzuna calls in Step 5 Build.

---

## Alternatives for the web search slot

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **A. Mojeek Business** | Published storage rights; `site` filter; UK company; PAYG; discovery use called out in terms | Not named in MVP §5; index smaller than Brave's marketing claims; £3/1k | **Choose** |
| B. Brave Search self-serve | Named candidate; `site:`; $5 free / month; 50 qps | **Cannot persist results** without Order Form | Reject for Milestone 1 evidence store |
| C. Brave Search + storage Order Form | Same as B plus lawful retention | Price and turnaround unpublished; blocks Build until sales replies | Optional upgrade path; Mohammed confirms |
| D. Wait / scrape / SerpApi-style | - | Violates D19 and §5 | Reject |
| E. Reed / Adzuna only (£0 search) | Free discovery of employers and some board links | No BD1/BD2; no CF2/CF3 web discovery | Acceptable only under §11 £0 path while free search credit is zero; not a full Stage 2 |

---

## How this maps to templates and budget

| Template | Provider | Budget | Notes |
|---|---|---|---|
| CF1 | Reed | Provider quota only (no search money) | On |
| CF1b | Adzuna | Provider quota only | **Off until V9** |
| CF2, CF3, BD1, BD2 | Mojeek Business | Search money + per-run / per-applicant caps | `site` for BD1/BD2; post-filter by host as belt-and-braces |
| Cold start (§8) | Equal share across **enabled** templates | Exploration 20% reserved | CF1b excluded from the share while off |
| £0 money cap (§11) | Reed (+ Adzuna if ever on) | Free Mojeek credit if any; else stop paid search | Collection still runs |

Unit price for the budget gate: treat one Mojeek web search call as £0.003 until B1 / live invoice confirms. Record actual cost on settle (Step 2 design).

---

## Mohammed must confirm himself

Do these before or in parallel with Step 5 Build. Agents cannot complete them.

1. **Sign up for Mojeek Business** (or reject and pick Brave+storage / another provider). Read the API Terms when registering. Card required.
2. **Sign up for a Reed Jobseeker API key.** Ask Reed (or read any terms presented at signup) whether storing job JSON and showing listings with apply links is allowed for a personal / multi-user MVP. Note any rate limit they state (V4).
3. **Read Adzuna's terms** and decide V9: personal job search vs commercial product. Do not enable CF1b until that call is written down. If commercial, ask Adzuna about a licence before relying on the 14-day trial for ongoing runs.
4. **Optional:** email Brave (`searchapi-support@brave.com` / `api-sales@brave.com`) for storage-rights pricing if you prefer Brave's index later. Do not block Milestone 1 on the reply if Mojeek is approved.
5. **B1 / B2 still open:** monthly search+AI cap, and whether a card is on file for paid Mojeek usage beyond any free credit.

---

## Proposed MVP.md §20 decision

**New row after approval (next free id is D29):**

| ID | Decision |
|---|---|
| D29 | Web search provider for CF2/CF3/BD1/BD2 is Mojeek Search API on the Business plan (indefinite storage rights; `site` filter), per `docs/step5-search-provider-design.md`. Brave Search remains a candidate only after written storage rights. Reed powers CF1. CF1b (Adzuna) stays disabled until V9 is confirmed by the operator. Provider id is a setting, not a hard-coded domain type. |

Also mark in §20 validation:

| ID | Update after this Decide |
|---|---|
| V1 | Mojeek Business: pass for URL/title/snippet retention (operator still accepts terms at signup). Brave self-serve: fail until Order Form. |
| V2 | Mojeek `site` / Brave `site:`: pass. |
| V3 | Mojeek Business £3/1k, 10 qps, 400k/day. Brave Search $5/1k + $5 free/month, 50 qps. Wire prices into `app/budget/pricing.py` at Build. |
| V4 | Reed: key free; display/storage/rate limit still operator-owned. |
| V9 | Unchanged; CF1b off. |

Shorter form if preferred: one sentence D29 pointing at this doc as source of truth.

---

## Approval checklist

Confirm or adjust:

1. Mojeek Business as the Milestone 1 search API (not Brave self-serve)
2. Provider name stays in settings / allowlist config
3. Reed CF1 on; Adzuna CF1b off until your V9 call
4. You will sign up for Reed and Mojeek yourself (PROMPTS)
5. Add D29 to MVP.md §20 after approval (doc-only follow-up, or in Build)
6. Stop here until you approve; Step 5 Build then implements templates, cold-start planner, runner, T5/T6/T9 with **mocked** API responses

---

## Principles that shaped this

- **Experience First:** durable evidence links for candidates beat sticking to the named Brave candidate when Brave's self-serve terms forbid that store.
- **Boundary Discipline:** terms and quotas are decided at the provider boundary before Build writes `search_result` rows; CF1b stays behind an explicit off switch.
- **Laziness Protocol:** one paid search vendor with published storage rights, no dual-provider abstraction, no "transient-only" workaround that would gut the evidence tables already approved in Step 1.

---

## Out of scope for this Decide

- Planner / runner code and tests (Step 5 Build)
- Live calls or keys in the repo
- Enabling Adzuna
- AI provider choice (V6)
- Setting the monthly money cap (B1)
)

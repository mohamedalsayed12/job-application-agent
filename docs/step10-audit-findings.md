# Step 10 part 2: audit findings and proposed rule changes

Status: **proposal, waiting for approval. No code has changed.**
Input: [`step10-audit-sample.csv`](./step10-audit-sample.csv) (20 companies, filled 2026-11-07).
The auditor was Claude, working from checked evidence at the operator's request. Each note is marked for operator spot-check.

## 1. Result against D21

| D21 target | Result | Pass |
|---|---|---|
| Zero wrongly verified boards | 0 (nothing was verified) | ✅, but trivially |
| At least 16 of 20 classifications correct | **7 of 20 (35%)** | ❌ |
| At least half of verified boards have a relevant open job | n/a: no verified boards | ❌ cannot be measured |

The sample matches the whole run. Of 72 classified companies, 70 were `needs_review`, 2 were `unsupported` and 0 were `verified`.

## 2. Mistakes by cause

13 of the 20 rows are wrong. Some rows have more than one cause.

| # | Cause | Where in the code | Rows affected | Should have been |
|---|---|---|---|---|
| C1 | **A CF3 domain never reaches the company it was searched for.** Claude's grounded hit arrives with a domain. Dedupe step 3 (`_resolve` in `app/queue/dedupe.py`) only groups by name when the incoming hit has no board and no domain, so the hit becomes a new row. The original row (often the one with the board) stays without a domain and fails with `no_domain_in_evidence`. | `queue/dedupe.py` `_resolve` step 3 | matillion, Synextra, Adria, Picture More, Hays, Randstad, Right Now Group (outcome happened to be right) | verified ×1, unsupported ×5 |
| C2 | **A confirmed careers site that is not on Greenhouse or Lever is marked `needs_review`, not `unsupported`.** `verify/run.py` maps "no unsupported marker found" to `needs_review` (`no_supported_public_board`). MVP §Stage 5 says a confirmed careers page with an unsupported system *or no public board* is `unsupported`. Only Workday and Ashby are known markers, so Eploy, Firefish, GetGot and custom sites all fall through. | `verify/run.py`, `verify/types.py` `UNSUPPORTED_CAREERS_MARKERS` | Synextra, Adria, Forward Role, Lorien, Randox, Hays, Randstad, Robert Walters, Oliver James, Braundton (and 16 rows outside the sample with the same reason) | unsupported |
| C3 | **The CF3 query finds pages about the city, not the company.** `"X" careers OR "official site" Manchester` let SerpAPI return Manchester tourism pages, Wikipedia and job boards. | `search/queries.py` CF3 | Robert Walters, Business Connection Group, Lorien, Braundton, Forward Role | unsupported ×4 (BCG is still correctly `needs_review`) |
| C4 | **Lever boards are searched by their slug.** Lever has no company-name field, so the candidate's name is the slug (`octoenergy`). CF3 searched for the slug and found only the Lever board, never `octopus.energy`. | extraction of Lever URLs → CF3 | octoenergy | verified |
| C5 | **Claude's output limit truncated CF3 batches.** At 500 output tokens, batches stopped at `max_tokens`. The official site was in the results (randox.com, oliverjames.com) but was never extracted. **Already fixed** in `.env` (1500 output, 4000 per call). The setting defaults in code are still 500 / 2500. | `core/config.py` defaults | Randox, Oliver James | unsupported |
| C6 | **The homepage title check is too strict for long legal names.** "Hays Specialist Recruitment Limited" does not match the title "Hays UK \| Find Jobs…", so the official domain fails. The failure reason is stored as the raw page title, which hides why it failed. | `verify/domain.py` `names_match` | Hays (also reason text on VIQU, Eden) | unsupported |

Correct rows: Essity and Lloyds (Workday → `unsupported`); Right Now Group (JS-only page), VIQU (bot-check page), Aspire, Business Connection Group and Profile 29 (no confirmable official site) → `needs_review`.

**Population note:** 9 of the 20 are recruitment agencies, nearly all found through Reed. Reed's search API gives `employerName` only, with no agency flag. This is not a classification mistake, but it bears on usefulness. See decision D-A below.

## 3. Proposed rule changes

Each change comes with the test that would prove it. Nothing is built until approved.

| ID | Change | Fixes | New test |
|---|---|---|---|
| R1 | **CF3 enriches the company it was run for.** Run CF3 per candidate. Grounded domains from that run are added to that candidate's `domain_candidates`, but only when Claude's grounded company name matches the candidate's cleaned name. The domain still has to pass the official-domain checks, so nothing is trusted blindly. | C1 | Board-only candidate + CF3 hit with a domain for the same name → one candidate with board and domain, not two |
| R2 | **Verify only after CF3 has had its chance.** A queued candidate with no domain and no CF3 run yet waits for CF3 instead of failing with `no_domain_in_evidence`. After CF3 it may still end `needs_review`. | C1 | Candidate without a domain is not claimed by the verifier until its CF3 run has finished |
| R3 | **"Confirmed careers page, no Greenhouse/Lever board" becomes `unsupported`.** When the official domain is confirmed and a careers page on it loads (200), but it links to no Greenhouse or Lever board, the result is `unsupported` with reason `no_public_board` (MVP rule). If no careers page can be found, the result stays `needs_review`. Also add the markers Eploy, Firefish, GetGot, SmartRecruiters, Teamtailor, Workable, SuccessFactors and iCIMS so the reason names the system. | C2 | Custom careers page with no board links → `unsupported/no_public_board`; Eploy link → `unsupported/eploy`; no careers page → still `needs_review` |
| R4 | **Sharper CF3 query.** Replace it with `"X" official website` and no location. Drop known job-board and aggregator hosts from CF3 results before extraction, using the existing blocklist. One SerpAPI search per employer, as now. | C3 | `build_query("CF3", …)` has no location and no `OR`; blocklisted hosts are filtered from CF3 leftovers |
| R5 | **Lever boards get their display name first.** Before CF3, read the company name from the board's public page title (`jobs.lever.co/<slug>`; this host must be added to the fetch allowlist, which today has only `api.lever.co` / `api.eu.lever.co`) and use it for CF3 and the name checks. | C4 | Lever candidate `octoenergy` → CF3 query for "Octopus Energy" |
| R6 | **Defaults match what works.** Code defaults become `AI_EXTRACTION_MAX_OUTPUT_TOKENS=1500` and `AI_EXTRACTION_PER_ITEM_TOKEN_CAP=4000`. | C5 | Settings default test |
| R7 | **Name check accepts the brand prefix.** Treat the title as a match when the title's first segment (before `\|` / `-` / `–`) equals the leading words of the cleaned company name, and the matched part is at least one word that isn't generic (not "group", "uk", "recruitment", "solutions"). Store `title_mismatch` as the reason and keep the title as evidence. | C6 | "Hays Specialist Recruitment Limited" vs "Hays UK \| Find Jobs" → match; "Lorien" vs "Lorien Assisted Living" stays a mismatch |

**Expected effect (estimate, not measured):** with R1–R7, 15–18 of the 20 sample rows should come out correct. That covers both Lever boards (`verified`) and most agencies (`unsupported`). Picture More (no careers page at all) would stay `needs_review` under R3's cautious branch. The real number comes only from re-running discovery and a fresh audit.

## 4. Decisions for the operator

- **D-A, recruitment agencies.** Agencies are not the employers the MVP targets, and their own careers sites are never Greenhouse or Lever boards. Options:
  1. Keep them. R3 makes them `unsupported` (correct, but they still take up CF3 searches).
  2. Skip them before CF3, using a short keyword list on the name ("recruitment", "resourcing", "staffing", "talent") plus a reviewed list of big agencies (Hays, Randstad, Robert Walters…). Saves search budget.
  3. Leave them to reviewers.

  Recommendation: **2**. About 45% of this sample were agencies, and each one costs a CF3 search.
- **D-B, re-audit.** After R1–R7 land, run a fresh discovery and a new 20-company audit (V8: recalibrate D21 after the first audit). Recommendation: re-audit with a human auditor, so usefulness gets a real measurement.
- **D-C, my audit.** Spot-check the Claude-filled verdicts. Any change to `is_correct` changes the 7/20 above.

## 5. Order of work if approved

R6 and R4 first (one-line and query changes), then R1 + R2 (dedupe/CF3 linking, the biggest win), then R3 and R7 (verifier), then R5. Each lands as its own PR with the tests above, and the full suite runs on `job_app_agent_test`.

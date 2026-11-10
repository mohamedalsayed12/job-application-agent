# Step 22 final check: Milestone 2 "done when"

First checked on 2026-11-09 against `MVP.md` section 18, with the real database at schema `0009`.
Updated the same evening, after `0010` (job summaries) was applied to the real database and the
manual check was done.

| # | Done when | Result | Evidence |
|---|---|---|---|
| 1 | T29–T37 pass, and T1–T28 still pass | Met | Full suite on the merged frontend branch, which contains Steps 21 and 22: 888 passed, lint clean, migrations round-trip. Each of T29–T37 has tests: T29 `tests/test_accounts.py`, T30–T32 `tests/test_profile.py`, T33 `tests/test_skills.py`, T34 `tests/test_match_reasons.py`, T35 `tests/test_summaries.py`, T36 `tests/test_listings.py`, T37 `tests/test_summaries.py` and `tests/test_listings.py`. No CI runs on this repo, so this is a local run. |
| 2 | A new user can register, fill in their profile, upload a CV, and see UK tech jobs that are open, real, complete and free of duplicates, each with a summary and an explained match score | Met | The flow works in tests: `test_saving_a_profile_matches_at_once_and_lists_the_best_first` registers, saves a profile and sees ranked, explained matches. CV upload was checked on a real server in Step 18. On the real data, the operator's search (graduate, junior Python and junior backend roles in Manchester) shows 39 jobs in their locations and 110 anywhere. After `0010` was applied, the best-scoring match (Canonical, Junior Ubuntu Software Engineer) was opened through the job page's code. Its summary was written, passed the fact check and was saved. Every other job gets its summary the first time someone opens it, at about £0.001 each from the £2 monthly cap. |
| 3 | The quality report still meets its targets | Met | `python -m app.quality` after the switchover on 2026-11-09: Complete 98.6% against 95%+, expired jobs shown 0 against 0, duplicates 0.2% against under 2%. |
| 4 | A manual check of 30 jobs against their original postings finds no wrong locations, cut-off descriptions or closed jobs | Partly met | Claude checked all 30 jobs in `docs/step22-manual-check.csv` against their live board APIs on 2026-11-09. The operator asked Claude to do this check, so it isn't an independent human check. **Locations:** 27 right; 1 was right when collected (Ocado changed Hatfield to London after 8 November); 2 couldn't be checked because the jobs had closed. **Descriptions:** 28 complete, none cut off. **Closed:** 2 of 30 (Deliveroo number 20, Stripe number 23) had closed since they were last confirmed open on 2026-11-08 22:52 UTC. Jobs are checked once a day, so a job can stay listed for up to a day after it closes. The next daily collection closes them. |

The check is strict about closed jobs, so item 4 isn't fully met. Closing jobs within the hour
would need more frequent collection, which costs more board requests but no money.

## Also from Milestone 1

- **Two weeks of daily runs within budget:** the worker has run since 2026-11-07, not two weeks yet. Under the £2 monthly cap, this month's spending is AI extraction (£0.144, 298 calls on 7 and 8 November) and one job summary. Searches on SerpAPI and Reed cost nothing.
- **Audit against D21:** the usefulness column of `docs/step16-audit-usefulness.csv` is filled in, judged by Claude at the operator's request. All 5 verified boards have a relevant open job for at least one applicant profile, against a target of at least half. For the operator's own search, 2 of 5 (Stripe and Deliveroo) have an entry-level software job open, both in London, and none are in Manchester.

## Notes from the manual check

- Stripe numbers 3 and 27 used to show "Not stated". After the Greenhouse offices fix (#47) they show "US" and "US (remote)", which match their postings.
- Region names come from the boards, so "EMEA" or "Worldwide (remote)" is shown as given. Some boards give country codes, so "SE", "SG" and "GB" are shown as codes, and Stripe's "CDMX" stays as written.
- Most sampled jobs aren't UK tech roles, because collection keeps every job on each board. Listings only show a person's matches, so this affects the sample, not what a user sees.

## Before the web app is public

- Done on 2026-11-09: `ADMIN_KEY` and `SESSION_SECRET` are set in `.env` with random values, and the web app's startup check passes. The real database is at `0010`.
- Done: a privacy notice at `/privacy`, linked from every page and from the register form.
- **ICO:** while the operator is the only user, the data is the operator's own. If other people register, check the ICO's data protection fee self-assessment on ico.org.uk and register if it says so. Only the operator can do this.
- **Backups:** the rule is to keep the latest two, and none older than 30 days. The privacy notice says this. On 2026-11-10, with the operator's go-ahead, the two oldest backups were deleted. The two left are both from 2026-11-09, so they must be deleted by 2026-12-09.

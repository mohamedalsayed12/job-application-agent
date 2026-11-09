# Step 22 final check: Milestone 2 "done when"

Checked on 2026-11-09 against `MVP.md` section 18. The real database was read without changes. It
is at schema `0009`, so it doesn't have the summary table from Step 21 (`0010`) yet.

| # | Done when | Result | Evidence |
|---|---|---|---|
| 1 | T29–T37 pass, and T1–T28 still pass | Met | Full suite on this branch: 888 passed, lint clean, migrations round-trip. Each of T29–T37 has tests: T29 `tests/test_accounts.py`, T30–T32 `tests/test_profile.py`, T33 `tests/test_skills.py`, T34 `tests/test_match_reasons.py`, T35 `tests/test_summaries.py`, T36 `tests/test_listings.py`, T37 `tests/test_summaries.py` and `tests/test_listings.py`. No CI runs on this repo, so this is a local run. |
| 2 | A new user can register, fill in their profile, upload a CV, and see UK tech jobs that are open, real, complete and free of duplicates, each with a summary and an explained match score | Partly met | The flow works in tests: `test_saving_a_profile_matches_at_once_and_lists_the_best_first` registers, saves a profile and sees ranked, explained matches. CV upload was checked on a real server in Step 18. On the real data, the operator's search (graduate, junior Python and junior backend roles in Manchester) would show 39 jobs in their locations and 110 anywhere. **Not met yet:** no job has a summary. The real database doesn't have the summary table until `0010` is applied, and no one has opened a job since. The AI key and a £2 monthly money cap are both set, so after `0010` the first view of each job will write its summary, at about £0.001 each. |
| 3 | The quality report still meets its targets | Met | `python -m app.quality` after the switchover on 2026-11-09: Complete 98.6% against 95%+, expired jobs shown 0 against 0, duplicates 0.2% against under 2%. |
| 4 | A manual check of 30 jobs against their original postings finds no wrong locations, cut-off descriptions or closed jobs | Not done | 30 random open jobs (seed 20261109) are in `docs/step22-manual-check.csv`, with what the listing shows and the link to the original posting. The last four columns are for the operator to fill in. |

## Also still open from Milestone 1

- **Two weeks of daily runs within budget:** the worker has run since 2026-11-07, not two weeks yet. Under the £2 monthly cap, £0.144 has been spent this month, all on AI extraction (298 calls on 7 and 8 November). Searches on SerpAPI and Reed cost nothing.
- **Audit against D21:** the usefulness column of `docs/step16-audit-usefulness.csv` is still empty.

## Things to look at in the manual check

- Two of the 30 sampled jobs (Stripe, numbers 3 and 27) show "Not stated" for location. If their postings name a place, that's a wrong location.
- Region names come from the boards, so "EMEA" or "Worldwide (remote)" is shown as given.
- Most sampled jobs aren't UK tech roles, because collection keeps every job on each board. Listings only show a person's matches, so this affects the sample, not what a user sees.

## Before the web app is public

- Set `ADMIN_KEY` and `SESSION_SECRET` in `.env`.
- Run `alembic upgrade head` on the real database (`0009` to `0010`).
- Summaries will spend from the £2 monthly cap once `0010` is applied. Lower or remove the cap first if they shouldn't.
- UK GDPR check: privacy notice, ICO registration and backup retention, including the database backups in the scratchpad.

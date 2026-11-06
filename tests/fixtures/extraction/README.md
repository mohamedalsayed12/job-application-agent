# Extraction fixtures (Step 6 / T10)

Board URLs come from live public Greenhouse/Lever responses under `tests/fixtures/boards/` (captured 2026-11-05).

Reed:
- `reed_search_results.json` — Jobseeker search list shape (no External Url).
- `reed_job_details_*.json` — Jobseeker details shape, where External Url appears.

Adzuna fixture follows the public search result shape; tests store `employer=` in the snippet the same way the Reed client does.

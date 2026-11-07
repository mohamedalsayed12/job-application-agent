# Audit fixtures (Step 10)

Canonical offline JSON: `app/audit/data/classified_companies.json`
(24 synthetic employers, 8 each of `verified` / `needs_review` / `unsupported`).
This directory keeps a copy for discoverability plus `seed.py` for tests.

Offline operator template (no DB writes):

```bash
uv run python -m app.audit --from-fixtures --out /tmp/step10-audit-sample.csv
```

Tests call `seed_classified_from_fixture(session)` inside a rolled-back
transaction. Do **not** commit those rows into a shared database.

Replace the CSV after a real run with keys set (`REED_API_KEY`,
`SERPAPI_API_KEY`; `money_cap` may stay unset on the £0 path).

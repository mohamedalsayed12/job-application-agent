# Audit fixtures (Step 10)

`classified_companies.json` holds 24 synthetic classified employers (8 each of
`verified`, `needs_review`, `unsupported`) with evidence URLs.

Offline operator template (no DB writes):

```bash
uv run python -m app.audit --from-fixtures --out /tmp/step10-audit-sample.csv
```

Tests may call `seed_classified_from_fixture(session)` inside a rolled-back
transaction. Do **not** commit fixture candidates into a shared database — they
look like live classifications and would dominate oldest-first sampling.

Replace the CSV after a real run with keys set (`REED_API_KEY`,
`SERPAPI_API_KEY`; `money_cap` may stay unset on the £0 path).

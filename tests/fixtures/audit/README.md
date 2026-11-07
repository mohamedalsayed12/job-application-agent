# Audit fixtures (Step 10)

`classified_companies.json` holds 24 synthetic classified employers (8 each of
`verified`, `needs_review`, `unsupported`) with evidence URLs.

Use when live Reed / SerpAPI keys are missing:

```bash
uv run python -m app.audit --seed-fixtures --commit --out /tmp/step10-audit-sample.csv
```

These rows are **not** live discovery results. Replace the CSV after a real run
with keys set (`REED_API_KEY`, `SERPAPI_API_KEY`; `money_cap` may stay unset on
the £0 path).

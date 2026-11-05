# Board API fixtures

Saved from live public APIs on 2026-11-05 (trimmed where noted). Used by mocked CI tests only.

| File | Source | Notes |
|---|---|---|
| `greenhouse_complete_example.json` | `GET boards-api.greenhouse.io/v1/boards/example/jobs` | Trimmed to 2 jobs; `meta.total` set to 2 |
| `greenhouse_empty.json` | Shape from live list responses + docs | Live empty boards rare; `{"jobs":[],"meta":{"total":0}}` |
| `greenhouse_not_found.json` | Missing board token | Real 404 body |
| `greenhouse_partial_truncated.json` | First 800 bytes of real example response | Unparseable on purpose |
| `lever_global_complete_leverdemo.json` | `api.lever.co/v0/postings/leverdemo?mode=json` | Trimmed to 2 postings |
| `lever_global_empty_lever.json` | `api.lever.co/v0/postings/lever?mode=json` | Real `[]` |
| `lever_eu_complete_lever.json` | `api.eu.lever.co/v0/postings/lever?mode=json` | Trimmed to 2 postings |
| `lever_not_found.json` | Missing site on global | Real 404 body |
| `lever_partial_truncated.json` | First 500 bytes of leverdemo | Unparseable on purpose |

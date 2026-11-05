# V5 probe notes (live, 2026-11-05)

## Greenhouse Job Board API
- Docs: https://developers.greenhouse.io/job-board.html
- List jobs: `GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs`
- Auth: none for GET

### Signals (measured)
| Case | HTTP | Body |
|---|---|---|
| Board exists with jobs | 200 | `{"jobs":[...],"meta":{"total":N}}` |
| Board missing | 404 | `{"status":404,"error":"Job not found"}` |
| Empty board | 200 expected | `{"jobs":[],"meta":{"total":0}}` (live empty rare; shape from docs + live non-empty) |
| Error / rate limit | 5xx / 429 | treat as failed (docs do not specialize GET errors) |

### Date fields (live `example` / `stripe` boards)
- `updated_at` — ISO-8601 with offset (always present on sampled jobs)
- `first_published` — present as key, often `null`
- `application_deadline` — present as key, often `null`
- No `created_at` / `posted_at` on list jobs

## Lever Postings API
- Docs: https://github.com/lever/postings-api
- Global: `GET https://api.lever.co/v0/postings/{site}?mode=json`
- EU: `GET https://api.eu.lever.co/v0/postings/{site}?mode=json`
- Auth: none for GET list

### Signals (measured)
| Case | HTTP | Body |
|---|---|---|
| Site exists with jobs | 200 | JSON array of postings |
| Site exists, zero jobs | 200 | `[]` (real: global `lever`) |
| Site missing | 404 | `{"ok":false,"error":"Document not found"}` |
| Error / rate limit | 5xx / 429 | failed (POST docs mention 429; GET should same class) |

### Date fields (live)
- `createdAt` — Unix epoch **milliseconds** (number). Present on sampled postings.
- No `updatedAt` / posted date in sampled list responses (docs list `createdAt` among fields).

### Instance note
- Global and EU are separate boards. Same site name can exist on both with different content.
- Empty `[]` on global means that instance's board is empty, not "try EU". Only **404** triggers EU fallback (T18).
- Allowlist bug in Step 2: `eu.lever.co` must become `api.eu.lever.co`.

## Label mapping for clients
| Label | When |
|---|---|
| `complete` | 200, valid expected shape, ≥1 job |
| `empty` | 200, valid expected shape, 0 jobs |
| `not_found` | 404 (Lever: after global then EU both 404) |
| `failed` | timeout, gateway refusal, 5xx, 429, transport |
| `partial` | 200/other with truncated or unparseable body, or wrong JSON shape |

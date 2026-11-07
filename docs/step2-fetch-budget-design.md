# Step 2: Fetch gateway, budget gate and spend ledger

**Status:** design for approval. No application code in this step.

**Sources:** MVP.md sections 11 and 12, acceptance tests T3 / T19 / T20 / T21, D3, D14, D15, D18, D19, D22, B1. Schema for `budget_policy` and `spend_ledger` from the approved Step 1 design (PR #2 / `docs/step1-data-model.md`).

**Goal:** one door for every outside request, and one gate for every paid call, so safety and spending rules are written once.

---

## Recommendation

Build two small packages that every later client calls. Nothing else talks to the network or books spend.

1. **`httpx.AsyncClient` as the only outbound HTTP client**, with redirects disabled so the gateway can re-check each hop. Move `httpx` from the dev group into project dependencies (D22 already uses it for tests).
2. **Robots via stdlib `urllib.robotparser`**, with a 24-hour in-process cache keyed by origin. Apply robots only to web-page fetches, not to allowlisted API hosts.
3. **Per-host rate limit in process:** one in-flight request per host and at least 1 second between starts. Honour `Retry-After`. Enough for the one-worker process in D22.
4. **SSRF checks in the gateway before every connect and after every redirect:** HTTPS only, no IP-literal hosts, DNS resolve then refuse private / loopback / link-local / CGNAT / ULA, plus a hard block list for LinkedIn, Indeed and Glassdoor.
5. **Budget gate books an estimated `spend_ledger` row before a paid call and settles `actual_cost` after.** Missing, null or zero monthly `money_cap` refuses all paid calls (B1 / §11). Free API and page fetches skip the money check but still use the fetch gateway.

Approve this design before Step 2 Build writes code or tests.

---

## Domain shapes

Name these before Build. Callers pass them in. The gateway and gate own the rules.

| Shape | Fields (concept) | Role |
|---|---|---|
| `FetchRequest` | `url`, `method`, `kind` (`api` \| `page`), `headers`, `timeout_s`, optional `official_domain` for page mode | Input to the gateway |
| `FetchResponse` | `url` (final), `status_code`, `content_type`, `body` (bytes, capped), `fetched_at` | Output on success |
| `FetchRefusal` | `reason` (`blocked_host` \| `robots` \| `private_address` \| `allowlist` \| `size` \| `redirect_limit` \| `timeout` \| `scheme` \| …) | Soft failure for callers. Not a budget wait. |
| `SpendEstimate` | `provider`, `kind` (`search` \| `ai_extraction` \| `ai_summary`), `units`, `unit_label`, `estimated_cost`, optional `related_run_id` / `related_item_id`, optional `applicant_id` | Input to the gate before a paid call |
| `SpendReservation` | `ledger_id`, `estimated_cost` | Hold created before the call |
| `BudgetWait` | which cap tripped (`monthly` \| `daily` \| `per_run` \| `per_applicant`) | Caller leaves work queued. Never marks it failed (T3). |

`budget_policy` and `spend_ledger` stay as Step 1 defined them. No schema change in this step unless Build finds a missing index while writing T3.

---

## Where code lives

D22 layout is `app/{main.py,core,api}`. Fetch and budget are domain modules, not HTTP routes.

```
app/fetch/
  __init__.py
  gateway.py      # single public entry: fetch(request) -> response | refusal
  policy.py       # allowlist, blocked hosts, SSRF helpers
  robots.py       # robots.txt fetch + 24h cache
  rate_limit.py   # per-host spacing
app/budget/
  __init__.py
  gate.py         # estimate, reserve, settle, wait
  pricing.py      # unit prices from settings until V3 / B4 land
```

`app/core/config.py` grows settings for User-Agent, allowlisted API hosts, unit prices, and default money cap behaviour. No Celery, Redis or extra services.

---

## Alternatives compared

### 1. HTTP client

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **A. `httpx` async** | Already in the project for ASGI tests (D22); timeouts, header control, `follow_redirects=False`, `MockTransport` for tests | Must move to runtime deps | **Choose** |
| B. `aiohttp` | Mature async | Second HTTP stack beside httpx tests | Reject |
| C. `requests` | Familiar | Sync-only; fights FastAPI worker loop | Reject |
| D. stdlib `urllib` | No dependency | Painful redirects, timeouts and async | Reject |

Manual redirects (max 3) are required for T19. Auto-follow would skip the private-address re-check.

### 2. robots.txt checking and caching

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **A. `urllib.robotparser` + in-process TTL cache** | Stdlib; enough for one worker; easy to mock | Cache is per process | **Choose** |
| B. `protego` | Strict parser | New dependency for little MVP gain | Reject |
| C. Postgres `robots_cache` table | Shared across processes | Extra table before a second worker exists | Defer |
| D. Skip robots for everything allowlisted | Simpler | Breaks §12 item 4 for verification pages | Reject |

**Rules.**

- `kind=page`: GET `{origin}/robots.txt` through the same gateway path that already applies SSRF and rate limits, parse with `RobotFileParser`, cache allow/deny for that origin for 24 hours, then ask whether our User-Agent may fetch the target path.
- `kind=api`: skip robots. Allowlisted board and search APIs are not "web pages" in §12.
- Cache miss that fails to fetch robots.txt: treat as allow for that request, do not cache a long allow on transport failure (fail open once, retry next time). Record the miss in logs. shortcut: fail-open on robots transport errors until we have evidence sites need fail-closed; revisit after the first audit.
- User-Agent names the crawler and links the GitHub repo (§12 item 5).

### 3. Per-host rate limiting (1 req/s)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **A. In-process `asyncio` lock + last-start timestamp per host** | Tiny; matches one worker (D22); testable with a fake clock | Not shared across processes | **Choose** |
| B. Postgres row or advisory lock per host | Multi-worker ready | Heavy for Milestone 1 | Defer until a second worker |
| C. `aiolimiter` / similar | Known API | Dependency; still process-local | Reject |

**Rules.**

- Key by hostname (lowercased).
- At most one in-flight request per host.
- At least 1.0 s between request starts for that host (T21).
- On `429` / `503` with `Retry-After`, sleep that long (cap at a small maximum, e.g. 60 s) before the next attempt. Callers own retry/backoff policy for verification (D12). The gateway only spaces and waits when told to retry the same host.

### 4. Blocking private and internal addresses (including redirects)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **A. Resolve → classify with `ipaddress` → connect; `follow_redirects=False`; re-check each `Location`** | Meets T19; stdlib; clear boundary | DNS rebinding between resolve and connect remains a known gap | **Choose** |
| B. Third-party SSRF library | Less code to write | Extra dep; still needs redirect policy | Reject |
| C. Block only literal IPs, trust DNS | Easy | Misses `evil.com` → `127.0.0.1` | Reject |

**Checks on every hop (original URL and each redirect).**

1. Scheme is `https` only.
2. Host is a name, not an IP literal.
3. No userinfo in the URL.
4. Host is not on the block list: `linkedin.com`, `indeed.com`, `glassdoor.com` and any subdomain of those (T20, D19).
5. `kind=api`: host (or parent) is on the allowlist (search provider, Reed, Adzuna when enabled, Greenhouse, Lever global/EU, AI provider).
6. `kind=page`: host equals the candidate `official_domain` or is a subdomain of it. Off-domain links are evidence only, never fetched (§12 item 2).
7. `getaddrinfo` for the host. Refuse if any returned address is in private, loopback, link-local (includes `169.254.169.254`), shared CGNAT (`100.64.0.0/10`), IPv6 ULA, multicast or unspecified.
8. Enforce 10 s timeout, 2 MB max body, HTML or JSON `Content-Type` only, at most 3 redirects (§12 item 7).

**Not in scope for the gateway (already decided elsewhere).** No login, cookies, CAPTCHA, proxy rotation, JS or headless browser (D14, §12 items 8–9). Fetched bodies are untrusted input to later stages (§12 item 11). Web page storage stays snippets + hash, not full HTML (D15).

### 5. Budget gate: estimate before, record after

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **A. Check sums only, insert ledger after success** | Simple | Crash after a paid call can under-count; concurrent callers can overshoot | Reject as sole approach |
| **B. Insert `spend_ledger` with `estimated_cost` first, set `actual_cost` after** | Matches the Step 1 columns; holds budget for in-flight work; T3-friendly | Needs settle / abandon on failure | **Choose** |
| C. Separate `budget_reservation` table | Explicit holds | Duplicates `spend_ledger`; schema change | Reject |

**Paid vs free.**

- Paid (must pass the gate): search API requests, AI extraction, AI summaries (later).
- Free of money caps, still through the fetch gateway: Greenhouse, Lever, official-domain page fetches. Reed and Adzuna use provider quotas in the gateway / client, not the monthly money cap (§11 CF1 / CF1b notes). They still count toward per-run / per-applicant request caps when used as search requests.

**Default until B1.**

- Treat missing policy, `money_cap IS NULL`, or `money_cap <= 0` as "no paid calls" (§11, B1).
- Step 2 Build may seed one `budget_policy` row for `provider='*'`, `period='month'`, `money_cap=0`, so the admin can raise it later without code changes. Behaviour is identical to "no row".
- Daily cap = monthly ÷ 30 for the current UTC day. Unused daily budget does not roll over (§11).

**Algorithm (paid call).**

1. Build `SpendEstimate` from `pricing.py` (conservative unit price × units). Never read `applicant_preference` salary fields (D3, T1).
2. In one short DB transaction, sum `spend_ledger` rows that count toward each cap (use `coalesce(actual_cost, estimated_cost)` so open holds count). Compare against monthly, daily, per-run (max 10 search requests) and per-applicant-per-day (max 50 search requests).
3. If any cap would be exceeded, raise `BudgetWait`. Caller leaves the work queued / planned. Do not mark failed (T3, §11).
4. Else insert `spend_ledger` with `estimated_cost`, `actual_cost=NULL`, `units`, `occurred_at=now()`, and return `SpendReservation`.
5. Perform the call through the fetch gateway (or AI client later).
6. Settle: set `actual_cost` (provider-reported if present, else estimated) and final `units`. On hard failure after reserve, set `actual_cost=0` or keep estimate per provider rules, but keep the row so audits see the attempt. Prefer `actual_cost=0` when no billable units were consumed.
7. Warn (log / admin signal) when spend crosses 80% of any cap. No stop at 80%.

**£0 / free-credit path (§11).** With money cap 0, Reed/Adzuna and free board collection still run. Board-direct and other paid search stop. Summaries stop. Exploration share (20%) is a planner concern in a later step, not a second ledger.

**Priority order when money is tight (D18)** is enforced by the planner / worker that asks the gate, not by the gate itself. The gate only answers yes, wait, or (for free work) n/a.

---

## Request path (happy path)

```text
caller
  └─ budget.gate.reserve(estimate)     # paid only
       └─ fetch.gateway.fetch(request)
            ├─ policy: scheme, block list, allowlist / official domain
            ├─ policy: resolve + private-address check
            ├─ robots (page only, 24h cache)
            ├─ rate_limit: per-host 1/s, single flight
            ├─ httpx request (no auto-redirect)
            ├─ size / content-type / timeout checks
            └─ on 3xx: validate Location, loop (max 3)
       └─ budget.gate.settle(reservation, actual)
```

Every Greenhouse, Lever, Reed, search and page client in later steps calls this path. No raw `httpx` outside `app/fetch`.

---

## Tests planned for Build (mocks only)

No real network. Use `httpx.MockTransport` (or a test double for the transport hook) and a fake DNS resolver injected into `policy`.

| Test | Behaviour |
|---|---|
| T3 | Paid reserve refused when monthly / daily / per-run / per-applicant would exceed. Work stays non-failed. Cap 0 blocks paid calls. |
| T19 | `http://…`, IP-literal hosts, and names that resolve to private / loopback addresses are refused. A redirect `Location` to a private address is refused and the private hop is never requested. |
| T20 | robots.txt `Disallow` prevents the page fetch. LinkedIn / Indeed / Glassdoor hosts are refused before connect. |
| T21 | Two fetches to the same host are at least 1.0 s apart (fake clock or measured monotonic time under the rate limiter). |

Also add thin unit checks for allowlist vs page-domain mode and for ledger reserve/settle math. Keep them behavioural (Test Behavior, Not Implementation).

---

## Out of scope for this Decide

- Implementation code and tests (Step 2 Build, after approval)
- Greenhouse / Lever clients (Step 3)
- Search provider choice and planner (Step 5 Decide / Build)
- AI extraction and token caps beyond the gate hooks (Step 7)
- Multi-process rate limiting and shared robots cache
- Setting the real monthly pound/dollar amount (B1, operator)

---

## Proposed MVP.md section 20 decision

**New row after approval.**

| ID | Decision |
|---|---|
| D28 | Fetch gateway and budget gate follow `docs/step2-fetch-budget-design.md`: outbound HTTP via `httpx` with manual redirects; stdlib robots.txt parser with 24h in-process cache for page fetches only; in-process per-host 1 req/s limit; HTTPS + DNS/private-address checks on every hop; LinkedIn/Indeed/Glassdoor blocked; paid calls insert `spend_ledger` with estimate before the call and settle after; missing/null/zero monthly `money_cap` refuses paid calls until B1. |

Shorter form if preferred: point at this doc as source of truth and keep the D-row to one sentence.

---

## Approval checklist

Confirm or adjust:

1. `httpx` as the sole outbound client, redirects handled in the gateway
2. Stdlib robots + 24h in-process cache, pages only
3. In-process per-host 1 req/s until a second worker exists
4. SSRF policy as above (including re-check after redirects)
5. Reserve-then-settle on `spend_ledger`; cap 0 / unset blocks paid calls
6. Package layout `app/fetch/` and `app/budget/`
7. Add D28 after approval

Stop here until you approve. Step 2 Build then implements the gateway, gate and T3 / T19 / T20 / T21 with mocks only.

---

## Principles that shaped this

- **Model the Domain:** `FetchRequest` / `SpendEstimate` / `BudgetWait` carry the rules instead of scattered ifs in each client.
- **Boundary Discipline:** URL, DNS, robots, rate limit and spend checks sit at the network/money boundary. Clients trust the gateway result.
- **Laziness Protocol:** stdlib robots and in-process limits instead of Redis, protego or a reservation table. One HTTP library already in the repo.
)

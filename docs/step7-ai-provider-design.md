# Step 7: AI provider Decide

**Status:** design for approval. No application code in this step.

**Sources:** MVP.md §10 (AI guardrails) and V6; PROMPTS Step 7 Decide; budget path §11 / B4; schema `ai_extraction` from Milestone 1 data model.

**Checked:** OpenAI, Anthropic and Google Gemini official pricing + structured-output docs, as of 6 Nov 2026.

**Goal:** pick one AI provider/model for leftover extraction only (employer names + official-domain candidates from search results code couldn't handle), so Build can wire fixed JSON schema, grounding checks, cache-by-input and budget-gate token caps.

---

## Recommendation

**Use OpenAI `gpt-4o-mini` with Structured Outputs (`response_format` = JSON Schema, `strict: true`).**

1. Matches §10's fixed output shape (company name, domain candidates, evidence quotes) with constrained decoding — parse failures should be rare before code grounding runs.
2. Unit cost is low enough for leftovers-only: about **$0.0005 per leftover item** at the sizing below (~£0.0004). 1,000 leftovers ≈ **$0.50**.
3. Free tier exists (geo-limited) for T11/T12 and dry runs; paid Build/Launch tiers have published RPM/TPM for the budget gate.
4. Fits the existing Python stack (D22): official `openai` SDK + Pydantic schema → JSON Schema.
5. Keep provider/model ids in settings (`AI_PROVIDER`, `AI_EXTRACTION_MODEL`, prompt version) — never hard-code into domain types.

**Operator must do before Step 7 Build:** create an OpenAI API key at https://platform.openai.com/api-keys and put it in `.env` (e.g. `OPENAI_API_KEY`). Card optional for free-tier tests; required for sustained daily volume.

Approve this design before Step 7 Build writes extraction calls.

---

## What leftover extraction needs

| Need | Why | MVP id |
|---|---|---|
| Fixed structured fields | company name, official-domain candidates, evidence (result id + quote ≤200 chars); board IDs stay code-only | §10.3–4, D10 |
| Schema-guaranteed JSON | code grounding rejects bad result ids / unquoted names / invented domains; ungrounded rate is a metric | §10.4–5, A7, T11/T12 |
| Known $/token and caps | budget gate estimates before call; per-item and per-day token caps; settle B4 | §11, B4, V6 |
| Cache by input hash + model + prompt version | never pay twice for the same leftover batch | §10.6 |
| No tools / no browse / results as data | injection-safe; personal data never sent | §10.1–2, §22 |

**Sizing used for cost-per-item** (one leftover batch call): ~1,500 input tokens (prompt + labelled result rows) + ~400 output tokens (JSON). Real runs should measure and update B4.

---

## Cost / limits comparison

Prices are USD per 1M tokens from vendor pages (6 Nov 2026). Est. = cost at 1,500 in + 400 out.

| Provider / model | In / Out $/MTok | Est. $/item | Structured output | Limits / free path | Fit for leftovers? |
|---|---|---|---|---|---|
| **OpenAI `gpt-4o-mini` (recommend)** | $0.15 / $0.60 | **~$0.00047** | **Strict JSON Schema** via Chat Completions `response_format.json_schema` (`strict: true`). Schema subset; first new schema can add latency while compiled. | Free tier (allowed geos): ~3 RPM / 40k TPM on this model; paid Build from $5 credits → 500 RPM / 200k TPM. Monthly org usage caps by tier. Signup: https://platform.openai.com/api-keys | **Best overall:** cheap, strict schema, documented tiers for the gate, free path for tests. |
| Anthropic `claude-haiku-5-5` | $0.10 / $0.50 (prompts ≤100k) | ~$0.00035 | JSON Schema via `output_config.format` (GA). Schema subset; objects typically need `additionalProperties: false`. Marketed for extraction. | No lasting free API; prepaid Console credits (+ card). Max/Ultra/Team plan credits possible. Signup: https://platform.claude.com/settings/keys | Strong runner-up on paid unit price; blocked for £0 bootstrap while B2 is open. |
| Google `gemini-3.5-flash-lite` | Free tier $0; paid $0.30 / $2.50 (output includes thinking) | **$0 free**; paid ~$0.00145 | `response_json_schema` / structured JSON (schema subset). | Free of charge on Developer API for this model; free-tier RPM/TPM/RPD are **per project** and only shown in AI Studio (not a fixed public table). Free tier: prompts may be used to improve Google products. Signup: https://aistudio.google.com/apikey | Best £0 path (like SerpAPI free), but paid output is dearer and free quotas are soft / training-on-free. |
| OpenAI `gpt-5-nano` | $0.05 / $0.40 | ~$0.00024 | Structured Outputs yes | Paid tiers only (no free-tier rate-limit row). **Deprecated** toward newer Luna-class models. | Skip for MVP — cheapest but lifecycle risk. |
| OpenAI `gpt-5-mini` / `gpt-5.6-luna` | $0.25/$2.00 · $0.20/$1.20 | ~$0.0011–0.0008 | Structured Outputs yes | Paid | Overkill for quote-bound name/domain extraction. |
| Anthropic `claude-sonnet-5-5` | $2 / $10 | ~$0.007 | Structured Outputs yes | Prepaid credits | Too expensive for leftovers volume. |

Sources: [OpenAI pricing](https://developers.openai.com/api/docs/pricing), [OpenAI Structured Outputs](https://openai.com/index/introducing-structured-outputs-in-the-api/), [OpenAI rate limits](https://developers.openai.com/api/docs/guides/rate-limits), [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing), [Anthropic structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs), [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing), [Gemini structured outputs](https://ai.google.dev/gemini-api/docs/generate-content/structured-output), [Gemini rate limits](https://ai.google.dev/gemini-api/docs/rate-limits).

---

## Why not the runners-up

| Option | Why not default |
|---|---|
| Claude Haiku 5.5 | Slightly cheaper paid unit cost and extraction-shaped, but needs prepaid credits before any call. Prefer once B2 is "card OK" and free OpenAI headroom is gone. |
| Gemini 3.5 Flash-Lite free | £0 money cost, but free quotas are project-local and unpublished as fixed numbers; free tier may train on prompts; paid output ($2.50/MTok, thinking included) is ~3× gpt-4o-mini per item. Keep as optional `AI_PROVIDER=gemini` fallback, not the Milestone 1 default. |
| Larger / reasoning models | Waste tokens and money; grounding is code's job, not the model's. |

---

## Implications for Build (no code here)

- Call AI **only** on leftover `search_result` rows after Step 6 code extraction.
- One fixed JSON Schema: employers[] with `company_name`, `domain_candidates[]`, `evidence[]` (`result_id`, `quote` max 200 chars). **No board IDs from AI.**
- Wrap retrieved text as labelled data; ignore in-content instructions (T12).
- Code grounding per §10.4; log ungrounded; cache by input hash + model + prompt version.
- Budget gate: estimate tokens → refuse over per-item / per-day / money caps; settle actual usage into `spend_ledger` / `ai_extraction`.
- Suggested B4 seed until measured: **$0.0005 / leftover item**, daily AI money share from monthly money_cap ÷ 30 (or refuse if B1 unset).

---

## Proposed MVP.md §20 decision

**New row (next free id is D30):**

| ID | Decision |
|---|---|
| D30 | AI leftover extraction uses **OpenAI `gpt-4o-mini`** with strict Structured Outputs (JSON Schema). Provider/model are settings (`AI_PROVIDER`, `AI_EXTRACTION_MODEL`). Signup: https://platform.openai.com/api-keys. Cost seed for B4 ≈ $0.0005/item (1.5k in + 0.4k out). Claude Haiku 5.5 is the paid-unit-price runner-up; Gemini Flash-Lite free is the £0 optional fallback. Detail: `docs/step7-ai-provider-design.md`. |

Also mark V6:

| ID | Update |
|---|---|
| V6 | Settled for Milestone 1 default: OpenAI `gpt-4o-mini` strict JSON Schema; unit cost seed above. Revisit if free-tier RPM blocks daily leftovers or measured accuracy fails A6/A7. |

---

## Approval checklist

1. OpenAI `gpt-4o-mini` + strict Structured Outputs as Milestone 1 AI extraction default
2. Operator creates key at https://platform.openai.com/api-keys before Build
3. Provider/model stay in settings
4. Add D30 (+ V6 note) to MVP.md §20
5. Stop until approved; Step 7 Build then implements leftover AI extraction, grounding, cache, T11/T12

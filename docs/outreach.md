# Outreach (post-MVP)

Personal outreach on an open matched job: find contacts via Apify LinkedIn people actors, research public signals, draft from a curated pattern library, let the user edit, then send via Gmail OAuth or copy a LinkedIn note. LinkedIn messages are never automated (D14).

## Decisions

- **D55 / contacts.** People discovery uses Apify actors configured as `APIFY_OUTREACH_HM_ACTOR`, `APIFY_OUTREACH_EMPLOYEES_ACTOR`, and `APIFY_OUTREACH_PROFILE_ACTOR`. This app still never fetches `linkedin.com` (D19 / D54).
- **D56 / send.** Gmail OAuth only (no SMTP app password). Default `OUTREACH_GMAIL_DRY_RUN=true` records a dry-run send without calling Gmail. LinkedIn is copy + open profile.
- **D57 / patterns.** Patterns live in `app/outreach/data/patterns.json` (versioned). No live Reddit or LinkedIn scraping for “what works”.
- **D37 narrowed.** Email transport exists for user-approved outreach only; password reset remains admin one-time links.

## Where messages appear

Under the summary of each job on `/listings` and on the job page (`app/outreach/ui.py`): each contact with the drafted Gmail message and LinkedIn note folded in `<details>`, or the next step (mark applied, Find people, Research and draft). They reuse the outreach page's routes; the gate (outreach only after the job is marked applied) is unchanged. Open question for the operator: should drafting be allowed before applying?

## Spend

New `spend_ledger` kinds: `linkedin_people`, `linkedin_profile`, `ai_outreach`.

## Thermos (2026-11-15)

Fixed before done: Gmail access-token typed as always-failing `str` (P0); intent map split; multi-recipient emails; send form not posting edited body; quote check accepting supersets; summary fact-check rejecting first-person mail; people find under-reserving for HM+employees.

Waived: parallel `ApifyRun` vs `linkedin._Run` (plan allowed a parallel module; extract later if a third caller appears).

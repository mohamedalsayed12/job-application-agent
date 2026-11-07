# Step 10 audit, round 2

Sheet: [step10-audit-sample-2.csv](step10-audit-sample-2.csv). Fresh discovery on
2026-11-07 after rules R1–R7 and the agency skip (PRs #19–#27). Claude filled the
sheet from collected evidence; every note starts with "needs operator spot-check".
Three rows are marked `SPOT-CHECK FIRST` because the evidence there is weakest.

## D21 result

| Target | Round 1 | Round 2 | Met |
|---|---|---|---|
| 0 companies wrongly verified | 0 | 0 | yes |
| At least 16/20 correct | 7/20 | 9/20 | no |
| Usefulness of verified rows at least half | n/a (2 verified, both wrong) | n/a (0 verified) | not measurable |

Discovery found 38 companies: 0 verified, 9 unsupported, 27 needs_review, 2 retry.
The sample took 11 needs_review and 9 unsupported rows.

## What improved

- R3 works. Five companies with a readable careers page and no Greenhouse/Lever
  board ended as `unsupported` instead of `needs_review` (Express Solicitors, PA
  Consulting, CGI, Candour Solutions, Maxwell Bond). New ATS markers caught iCIMS
  (Arm) and Eploy (Innovative Technology).
- R1/R2/R5: every company got a CF3 domain search before verification.
- The known-agency list kept Hays, Lorien, Robert Walters and the rest out.

## Why 11 rows are still wrong

| Cause | Rows | Effect |
|---|---|---|
| A1. Agency names with no agency word | Oscar Technology, SRG, Noir, Reed (+ Maxwell Bond, right by luck) | Agencies reach the queue. Reed job text says so ("recruitment agency", "acting as an Employment Business"). |
| A2. Wrong domain from CF3 for a same-name business | Noir (car fragrances), Candour Solutions (India), Moody's (contacthr.com), Atos (atos.co.uk, not atos.net) | Checks run on the wrong site. |
| A3. Bot walls and fetch refusals end as needs_review | Co-op ("Pardon Our Interruption"), Atos (Cloudflare 403), Moody's (403), National Trust (robots), BNY (redirect refused) | True answer is unsupported; the system cannot read the site. |
| A4. No domain found | SRG, DCMS | needs_review. |
| A5. Careers-only domain | Boots (boots.jobs; /careers redirects to its homepage) | R3's careers-path guard does not fire. |
| A6. Subdomain and apex not deduped | Innovative Technology (two rows) | Duplicate work; both rows right. |
| A7. HTML entities in page titles | Oscar ("Oscar&#x27;s") | Title check compares escaped text. |

## The main blocker: no Greenhouse or Lever companies are found

Zero verified rows means usefulness cannot be measured and "correct" can only
come from unsupported/needs_review. The board searches that should find
Greenhouse/Lever employers do not work through Google:

- BD1 searched `site:boards.greenhouse.io`. Google now lists most boards on
  `job-boards.greenhouse.io` and `job-boards.eu.greenhouse.io`, so every BD1 run
  ended `inconclusive`. PR #27 widens it to `site:greenhouse.io`.
- Even then, Google through SerpAPI honours `site:` only sometimes. The same
  `site:greenhouse.io` query returned 10 boards once and Wikipedia pages later.
- BD2: Google ignores `site:jobs.lever.co` and `site:lever.co` every time and
  returns Indeed/LinkedIn pages.

## Proposed next fixes, in order of value

1. **Probe the public board APIs by name (no web search).** For each candidate,
   try `boards-api.greenhouse.io/v1/boards/{slug}` and
   `api.lever.co/v0/postings/{slug}` for slugs derived from the cleaned name, and
   accept a hit only when the board's name matches the company (same_company).
   Free, allowlisted, and it turns real Greenhouse/Lever employers into verified
   rows. This is the only fix that makes usefulness measurable.
2. **Agency detection from job text (A1).** Skip a Reed/CF2 employer when its
   posting says "recruitment agency", "employment business" or "on behalf of";
   drop "Reed" as an employer when Reed is the poster.
3. **Domain choice (A2, A3).** Prefer the CF3 domain whose host contains the
   company's core name; on a bot wall title ("Pardon Our Interruption",
   "Just a moment...") or a 403, try the next domain before giving up.
4. **Small fixes.** Unescape HTML entities in titles (A7); dedupe subdomain vs
   apex (A6); treat a `.jobs` or `careers.` host as a careers page for R3 (A5).

## Spot checks for the operator

Please check at least these rows and correct `auditor_result` / `is_correct`:

1. Oscar Technology: agency? (oscar-recruit.com)
2. Candour Solutions: who posted the Reed job, and is it an agency?
3. PA Consulting Group: confirm no Greenhouse/Lever board behind /careers.

Then pick any three other rows at random to check the rest of the sheet.

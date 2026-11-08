# Step 10 audit, round 4

Sheet: [step10-audit-sample-4.csv](step10-audit-sample-4.csv). This round adds the
16 MiB fetch cap, the ownership proof from own scripts and job links, and 54 more
seed employers. Claude graded the sheet and spot-checked 14 rows from fresh evidence.
The operator has not checked any round, so all numbers below are provisional.

## D21 result

| Target | Round 1 | Round 2 | Round 3 | Round 4 | Met |
|---|---|---|---|---|---|
| 0 companies wrongly verified | 0 | 0 | 0 of 6 | 0 of 5 | yes |
| At least 16/20 correct | 7/20 | 9/20 | 15/20 | 17/20 | yes |
| At least half of verified boards have a relevant open job | not measurable | not measurable | 3 of 6 | 5 of 5 | yes, see below |

## Read these numbers with care

**Three grades are not independent.** Stripe, Deliveroo and Graphcore are verified
because of hand approvals on the same day. Claude recommended Stripe and approved the
other two. Without those rows the score is 14/17, or 82%. The new ownership rules
reach the same result alone for Deliveroo and Graphcore. The verifier alone still
holds Stripe in `needs_review`.

**The seed list still drives the result.** Seeded rows scored 12/12 and discovered
rows scored 5/8. All 11 verified companies in the database came from the seed list.
Discovery has not produced a verified company in any round. The decision on how D21
treats seeded companies is still open.

**The sample is not random.** The tool skips companies audited before and takes the
oldest first. This round took all 5 remaining verified companies, 8 of 64
`needs_review` and 7 of 40 `unsupported`. The scores are not an estimate for the whole
database.

**Usefulness depends on the applicant.** The 5 of 5 counts any of the four applicant
profiles, three of which are test profiles.

| Measure, all 11 verified boards | Count |
|---|---|
| A relevant open job for any profile | 8 of 11 |
| An entry-level software job in the UK, for the operator's own search | 4 of 11 |
| An entry-level software job in Manchester | 0 of 11 |

The four boards are Ocado, Stripe, Deliveroo and Graphcore, with 14 jobs in total. The
entry-level search stopped at the daily money cap, so 11 of its 100 shortlisted jobs
were not judged. The operator's definition of entry-level excludes internships.

## Rows graded wrong

| Company | System result | Graded result | Why |
|---|---|---|---|
| BAE Systems | needs_review | unsupported | Main site returns 403. `jobsearch.baesystems.com` mentions Taleo. |
| Barclays | needs_review | unsupported | `search.jobs.barclays` mentions Workday. The system had tried a domain that is not Barclays' site. |
| AtkinsRéalis | needs_review | unsupported | Inferred. The careers site returns 403 and no Greenhouse, Lever or Ashby board answers. |

None of these is a false verification. All three are held for a person when the true
result is `unsupported`.

## Spot checks

Claude rechecked 14 rows of this sheet and the 4 flagged rows from round 3, using fresh
fetches instead of the verifier's stored checks. No grade changed after the recheck.
These checks used plain page fetches, not a browser, so content that only appears in
a browser was not seen. Claude also graded the sheet, so these are not independent.

| Rows | Check | Outcome |
|---|---|---|
| Paddle, Thought Machine, Cleo, Tractable | Ashby API, direct | Confirmed `unsupported`, with 15, 31, 19 and 5 jobs |
| Beamery, Faculty | Ashby API, direct | Confirmed `unsupported`, with 6 and 87 jobs. The reason should say Ashby. |
| Bloomberg | Own site | Confirmed `unsupported`: links to Avature. Stored evidence is a news article. |
| Stripe | Board job links | Pages load on `stripe.com`. Job title found on 2 of 3 in a fresh sample, where an earlier sample found 3 of 3. The third is a search link. |
| Round 3: Canonical | Careers page | Confirmed `verified`: links to its Greenhouse jobs. |
| Round 3: Zego | Ashby API, direct | Confirmed `unsupported`, with 35 jobs. The reason should say Ashby. |
| Round 3: Quant Capital | Site and board APIs | Still cannot be confirmed. `needs_review` stands. |
| Round 3: Roku | Careers pages | `roku.com/jobs` redirects to `weareroku.com`, and the Roku home page does not mention it. `needs_review` stands. |

The round 3 score stays at 15/20.

## What the audit found

| Finding | Evidence | Effect |
|---|---|---|
| Ashby is common among companies without a board | 19 of 115 have a board under a guessed name, with 688 jobs, 440 of them in the UK. Cleo's board uses a different name, so 19 is a minimum. Ownership of these boards was not checked. | These jobs cannot be read, but only one has an entry-level title in the UK: Lendable, Junior Data Engineer, London. None is an engineering role in Manchester. |
| Reason labels hide Ashby | Beamery, Faculty and Zego say `no_public_board` | Result right, reason wrong |
| Weak careers evidence | Bloomberg's stored page is a news article and Beamery's is a case study | A page can count as a careers page because of words in its URL |
| Homepage redirects to another domain | 7 rows with reason `allowlist`. BJSS goes to `cgi.com`, Ten10 to `scalefactory.com`, Redgate to `red-gate.com`. | Held for review, with a reason that does not explain the redirect. |
| Manchester seed employers | 18 added: 0 verified, 5 needs_review, 9 unsupported, 4 retry_wait | No new Manchester entry-level job |

Whole database after this round: 11 verified, 82 needs_review, 56 unsupported, 10
retry_wait.

## Proposed next steps, in order

1. **Agency detection from homepage titles.** Turnblock.io and Miller Maxwell from
   round 3 reached the queue. The work is in progress.
2. **Find more Manchester employers that use Greenhouse or Lever.** Adding more names
   by hand produced no Manchester result. A search for boards that list Manchester
   jobs may work better.
3. **Decide whether to support Ashby.** It adds many UK jobs but few entry-level ones.
4. **Follow redirects to a new domain and record them,** and improve the reason labels
   and careers-page evidence.

## Operator decisions

- **Independent check.** Check the three self-graded rows and any others you doubt.
  Filter the `notes` column for `SELF-GRADED` and `SPOT-CHECKED`.
- **Does Step 10 pass with a seed list?** Options: accept seeded companies as part of
  discovery, or require D21 on discovered companies alone (currently 5/8 in this
  sample, with none verified). This is a product decision.

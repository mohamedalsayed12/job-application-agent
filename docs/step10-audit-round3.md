# Step 10 audit, round 3

Sheet: [step10-audit-sample-3.csv](step10-audit-sample-3.csv). This round adds the
direct board lookup and a hand-written seed list of 68 UK tech employers
(`app/discovery/data/uk_tech_seed.csv`). Claude filled the sheet from collected
evidence, and every note starts with "needs operator spot-check". The operator has
not checked rounds 1, 2 or 3, so all numbers below are provisional.

## D21 result

| Target | Round 1 | Round 2 | Round 3 | Met |
|---|---|---|---|---|
| 0 companies wrongly verified | 0 | 0 | 0 of 6 | yes |
| At least 16/20 correct | 7/20 | 9/20 | 15/20 | no, one short |
| At least half of verified boards have a relevant open job | not measurable | not measurable | 3 of 6 | yes, see below |

## Read these numbers with care

**The seed list drives the improvement.** Seeded rows scored 13/13 and discovered
rows scored 2/7. All 6 verified companies came from the seed list, which was
written by hand from well-known tech employers. These results show that the
verifier works when it is given suitable companies. They do not show that
discovery finds such companies by itself.

**Usefulness depends on the applicant.** The 3 of 6 counts any of the four
applicant profiles, three of which are test profiles:

| Verified company | Best match | Counted useful |
|---|---|---|
| Zopa | Java/Kotlin Software Engineer, London and Manchester | yes |
| Monzo | Backend Engineer III, Cardiff, London or Remote (UK) | yes |
| Canonical | Distributed Systems Testing Software Engineer, Python / Go, home based EMEA | yes |
| Matillion | Manchester roles are Staff and Senior Staff | no |
| GoCardless | Data Engineer in Lisbon and Riga only | no |
| PolyAI | 3 jobs, none in engineering | no |

**For the operator's own search (graduate or junior, Python or backend, Manchester),
the count is 0 of 6.** No verified board has a role labelled graduate or junior in
software or backend in the UK. Zopa's Manchester role is the closest.

Sample size also matters: 6 verified boards is a small base for a percentage.

## What improved

- The lookup found 15 boards among the 68 seed companies. Board ids were guessed
  from company names and domains, with no search spend.
- Verification produced its first `verified` rows. The evidence check found each
  company's own careers page pointing to its board.
- Whole database after this round: 6 verified, 61 needs_review, 35 unsupported,
  4 retry_wait.

## Remaining problems

| Problem | Rows or examples | Effect |
|---|---|---|
| Agencies with no agency word in the name | Turnblock.io, Miller Maxwell | Reach the queue. Their homepage titles say "Recruitment" and "Recruiters". |
| Blocked or missing domains | bet365, Arm, Leonardo | End as needs_review; the true answer is unsupported. |
| Same-name duplicate rows | Arm | A row with no domain exists beside a row with arm.com. |
| Size limit on fetched pages | Octopus Energy, in retry_wait with reason `size` | A real Lever employer cannot verify. |
| Careers site on another domain | Roku: roku.com/jobs redirects to weareroku.com | No link can be read, so it stays in needs_review. |
| Company found on a board, no link found | 12 rows at ownership_medium, such as Stripe, Cloudflare, Deliveroo, GitLab | Held for review. This is the strict rule working, but the queue needs a person. |
| Reason label | Synthesia says no_public_board; its page mentions Ashby | Result right, reason imprecise. |

## Correction to round 2

Round 2 called the direct lookup "the only fix that makes usefulness measurable".
The lookup alone was not enough: on the 38 discovered companies it attached 2 boards
and verified none. Usefulness became measurable only when the seed list supplied
suitable companies.

## Proposed next steps, in order

1. **Fetch size limit.** Find out why Octopus Energy hits it, then decide whether
   the limit or the page reading should change.
2. **Review the 12 ownership_medium rows.** These boards carry the company's own
   name, so a person can clear them quickly. Roku has a Manchester internship.
3. **Add junior-friendly employers to the seed list,** and check whether it helps
   usefulness for the operator's own search.
4. **Agency detection from homepage titles and job text.**
5. **Decide how D21 treats seeded companies.** The targets were written for
   discovery. See the operator decision below.

## Operator decisions

- **Spot checks.** Check the four `SPOT-CHECK FIRST` rows: Canonical, Quant Capital,
  Roku and Zego. One more correct row would meet the 16/20 target, and one wrong
  verdict could move the score either way.
- **Does Step 10 pass with a seed list?** Options: accept seeded companies as part
  of discovery, or require D21 on discovered companies alone (currently 2/7 in this
  sample, with none verified). This is a product decision.

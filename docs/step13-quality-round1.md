# Step 13 quality report: round 1

Superseded by `step13-quality-round2.md`. This round's count of jobs naming several places is too high: about 20 Stripe jobs say `N/A`, which it counted as several places.

Run on 9 November 2026 against the real database, using `python -m app.quality`. The data is 1,837 jobs from 11 verified boards. Each board has been read once. This run does not include the changes in PR #39.

## Result: the gate is not met

| Target | Goal | Now | Result |
|---|---|---|---|
| Complete | 95%+ | 91.3% | Missed |
| Expired jobs shown | 0 | 0 | Met, but not yet proven |
| Duplicates | under 2% | 2.4% (45 jobs) | Missed |

Other measures:

| Measure | Result |
|---|---|
| Recent | 100%, because every board was read within the last 48 hours |
| Real | 1,833 high, 4 medium, 0 low |
| Older than 60 days | 904 jobs, flagged and kept as the plan says |
| Greenhouse | 1,622 jobs, 90.2% complete, 2.5% duplicates |
| Lever | 215 jobs, 100% complete, 1.9% duplicates |

## Why "expired jobs shown" is not yet proven

- No board has had a second read, so no job has had the chance to disappear.
- No stored job has a closing date.
- Job search does not read stored jobs yet, so this counts the jobs it would show. Step 14 must apply the Recent check when it shows jobs.

## Cause 1: jobs listed in several places have no location

All 159 incomplete jobs are missing only the location, and 155 of them are from Stripe. Meeting 95% needs 68 of them fixed.

The source does give a location for all 159. The parser only understands one place, so it stores nothing for text like these:

- `London, Dublin`
- `Dublin or London`
- `United Kingdom; Ireland`
- `SF, NYC, CHI, ATL, SEA`

Of the 159, 156 name several places. The other 3 say `na` (2 jobs) or `Global` (1 job).

This also hides jobs from the applicant. 18 of the 159 name a UK place or remote work, and one of those names Manchester (Deliveroo, Account Management Team Lead).

**Proposed fix:** store a list of places for each job, using a new column and a migration, and split the text into that list. It would also need a short table for abbreviations such as `SF` and `NYC`. This matches D17, which says a master record covers all its locations. Estimated result: 99.8% complete, with the 3 unclear jobs left missing.

## Cause 2: the same advert posted once per city

Of the 45 duplicates, 44 are the same advert in a different city, and 1 is in the same city. Stripe has 24 and Graphcore 16. Octopus Energy has 4 and Deliveroo 1. Meeting the target needs 9 of them removed.

Examples:

- Graphcore, AI Research Engineer: Bristol and London
- Stripe, Recruiting Scheduler: Chicago and New York
- Octopus Energy, Promoter*in: Münster and Bonn

The checker uses the rule in D17, where matching company, title and description within 30 days makes a repost, in any city. Linking (D32) only merges jobs in the same city. So these two rules disagree, and the operator needs to choose:

- **Option A, keep one job per city and count only same-city duplicates.** The rate becomes 0.05%. Each city keeps its own apply link, and an applicant filtering by city never sees the copies. This changes how the target is measured, not the data, and D17 would need rewording. This is the recommended option.
- **Option B, merge them into one job as D17 is written.** The rate falls to about 0%. This needs the list of places from cause 1, and showing a job would have to pick the apply link for the applicant's city.

No employer is on two sources yet, so every duplicate here is within one source.

## Also found, no target affected

- Four postings are general sign-ups, not specific roles. An example is Ocado's "Join our Talent Community". They are marked medium confidence and could be hidden in Step 14.
- 49% of jobs are older than 60 days. Step 14 may want to rank these lower.

## Not measured yet

- Summaries are not built.
- Companies House is not connected, so Real rests mostly on the board being verified.
- Safety-brake events are worked out from stored fetches, because nothing records them when they happen.
- Duplicate candidates removed cannot be reported, because they are folded in when found and not counted.
- Verified boards per £ flatters search, because the current 11 boards came from the seed list.

## Next round

No fixes have been made, because the plan says not to change code until the operator approves. After a decision, fix the causes, merge #39 and run the report again after at least one more board read.

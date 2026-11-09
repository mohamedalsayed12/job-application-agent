# Step 13 quality report: round 2

Measured on 9 November 2026 with the operator's two decisions from round 1 applied (D34). The new parser and checks ran read-only over the 1,837 stored jobs and their raw data. The real database has not been migrated, so these numbers need confirming with `python -m app.quality` after this work is merged.

## Result: the targets are met in this preview

| Target | Goal | Round 1 | Now | Result |
|---|---|---|---|---|
| Complete | 95%+ | 91.1% | 98.5% | Met |
| Expired jobs shown | 0 | 0 | 0 | Met, but not yet proven |
| Duplicates | under 2% | 2.4% | 0.22% (4 jobs) | Met |

Round 1's "now" figure is the one after PR #39 was merged. Complete by source is Greenhouse 98.3% and Lever 100%.

"Expired jobs shown" has the same limit as round 1: no board has had a second read, and no stored job has a closing date. Boards are next due around 22:50 UTC on 9 November.

## What changed

- **Places:** every job now keeps a list of the places it is listed for. 355 jobs name more than one.
- **Lever:** its own list of locations is now used. In round 1, 63 Lever jobs showed only their main location.
- **Duplicates:** the same advert in a different city is now a separate job, as the operator chose. 41 jobs are in this group, reported as their own figure and not counted as duplicates.
- **Manchester:** 28 jobs can now be found by city, and 6 of them are also listed in other places.

## What is left

**27 jobs have no usable location.** All of these are what the source sends:

| Location text | Jobs |
|---|---|
| `N/A` | 20 |
| `Deliveroo` | 3 |
| `na` | 2 |
| `In-person` | 1 |
| `Global` | 1 |

Stripe has 23 of them, Deliveroo 3 and Ocado 1.

**The 4 duplicates share a place with an earlier job:**

- Stripe, Account Executive, Velocity Platforms (Hunter): 3 jobs. One is listed for Chicago, New York and San Francisco, and others repeat those cities.
- Deliveroo, Golang Backend Engineer, Financial Product Fraud: 1 job, in London.

## Known parser limits

These affect a few jobs each and are left as they are:

- `San Francisco, NY, Seattle` loses New York, because `NY` is read as San Francisco's state.
- `New York, US - Remote (East Coast), Toronto` keeps only New York.
- `Albany, New York` would give two places. No stored job has this form, and treating `Seattle, New York` as two places matters more.
- `Toronto Canada` and `West Coast USA` are stored as if they were cities.
- `Chicago, IL` records no country, because `IL` is also Israel's code. This is the rule from PR #39.

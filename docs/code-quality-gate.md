# Code quality gate (always on)

**When to use:** After any non-trivial code edit in `job-application-agent` (every MVP build step, bugfix, refactor). Also before commit/push/PR.

**Desired result:** Smallest complete diff that meets MVP.md; tests green; thermos findings addressed or explicitly waived with reason.

## Steps

1. **Feature tests (while writing):** New feature code and pytest behavior tests land in the same step (`.cursor/rules/feature-tests.mdc`). Prefer MVP.md acceptance IDs when they apply. Run the touched tests before claiming done.
2. **Ponytail (while writing):** Apply the ladder in `.cursor/rules/ponytail.mdc` / AGENTS.md — skip unasked features; reuse codebase/stdlib; finish callers and tests; never cut validation, security, or error handling at trust boundaries.
3. **pstack / poteto-mode (while writing):** Name domain data shapes first; smallest verifiable unit; prove behavior with real tests (pytest); keep prose unslopped. Read poteto-mode / relevant principle skills when the change is nontrivial.
4. **Thermos (before done):** Gather `git diff` of the change. Run both review passes (correctness/security and code-quality / code-judo). Fix P0/P1 findings; list waived nits with reason.
5. **Record:** Note in the step report that the gate ran (tests + review summary).

## Exceptions

- Pure docs / decision recording (MVP.md D-row only) skips thermos and may skip new tests; still keep prose minimal.
- User explicitly says skip review/tests for a throwaway spike — say so in the report.

## Checks

- [ ] Diff is the smallest complete change for the asked step
- [ ] New/changed feature has pytest behavior tests in the same step
- [ ] Acceptance tests for the step pass
- [ ] Thermos (or equivalent dual review) completed on the diff
- [ ] No `.env` or secrets committed

## See also

- [quality-stack.md](./quality-stack.md)
- Project Context workflow: `workflows/code-quality-gate.md`

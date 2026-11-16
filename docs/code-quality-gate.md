# Code quality gate (always on)

**When to use:** After any non-trivial code edit in `job-application-agent` (every MVP build step, bugfix, refactor). Also before commit/push/PR.

**Desired result:** Smallest complete diff that meets MVP.md; tests green; thermos findings addressed or explicitly waived with reason.

## Steps

1. **Ponytail (while writing):** Apply the ladder in `.cursor/rules/ponytail.mdc` / AGENTS.md — skip unasked features; reuse codebase/stdlib; finish callers and tests; never cut validation, security, or error handling at trust boundaries.
2. **pstack / poteto-mode (while writing):** Name domain data shapes first; smallest verifiable unit; prove behavior with real tests (pytest); keep prose unslopped. Read poteto-mode / relevant principle skills when the change is nontrivial.
3. **Thermos (before done):** Gather `git diff` of the change. Run both review passes (correctness/security and code-quality / code-judo). Fix P0/P1 findings; list waived nits with reason.
4. **Record:** Note in the step report that the gate ran (tests + review summary).

## Exceptions

- Pure docs / decision recording (MVP.md D-row only) skips thermos; still keep prose minimal.
- User explicitly says skip review for a throwaway spike — say so in the report.

## Checks

- [ ] Diff is the smallest complete change for the asked step
- [ ] Acceptance tests for the step pass
- [ ] Thermos (or equivalent dual review) completed on the diff
- [ ] No `.env` or secrets committed

## See also

- [quality-stack.md](./quality-stack.md)
- Project Context workflow: `workflows/code-quality-gate.md`

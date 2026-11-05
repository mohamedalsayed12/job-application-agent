# Quality stack — ponytail, pstack, thermos

Always-on for this Project’s `job-application-agent` work.

| Tool | Role | How it stays active |
|---|---|---|
| **Ponytail** | Lazy-senior minimal diffs | Always-applied `.cursor/rules/ponytail.mdc` in the repo + Project preference |
| **pstack** | Poteto-mode principles, subagent rigor | Plugin enabled in `.cursor/settings.json`; `pstack-models.mdc` at `large`; poteto-mode on nontrivial work |
| **Thermos** | Thermo-nuclear dual review | Plugin enabled; `docs/code-quality-gate.md` requires both review passes before done |

## Install on your machine (Cursor IDE)

1. `/add-plugin pstack` then `/setup-pstack` (budget already set to **large** in-repo; adjust if you want).
2. `/add-plugin thermos`
3. Optional: install [ponytail](https://github.com/DietrichGebert/ponytail) for slash commands; the always-on rule is already vendored in `.cursor/rules/ponytail.mdc`.

Cloud agents for this Project get the same rules from the repo and the Project Context preferences/workflow even when plugins are not installed.

## Gate

See [code-quality-gate.md](./code-quality-gate.md).

"""Copy primary .env into a worktree with DATABASE_URL on job_app_agent_<slug>."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.isolation import resolve_live_database_name, with_database  # noqa: E402


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def write_worktree_env(*, src: Path, dst: Path, slug: str) -> str:
    live = resolve_live_database_name(slug)
    out: list[str] = []
    found = False
    for line in src.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            raw = _strip_wrapping_quotes(line.split("=", 1)[1].strip())
            out.append(f"DATABASE_URL={with_database(raw, live)}")
            found = True
        else:
            out.append(line)
    if not found:
        raise SystemExit(f"DATABASE_URL missing in {src}")
    dst.write_text("\n".join(out) + "\n", encoding="utf-8")
    return live


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--dst", type=Path, required=True)
    parser.add_argument("--slug", required=True)
    args = parser.parse_args()
    if not args.src.is_file():
        raise SystemExit(f"source .env not found: {args.src}")
    live = write_worktree_env(src=args.src, dst=args.dst, slug=args.slug)
    print(f"Wrote {args.dst} with DATABASE_URL database={live}")


if __name__ == "__main__":
    main()

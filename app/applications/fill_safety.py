"""Rules the extension must obey when applying a fill plan."""

from __future__ import annotations

import re

ALLOWED_ACTIONS = frozenset({"fill", "select", "check", "upload_cv"})
_SUBMIT = re.compile(
    r"\b(submit|apply now|send application|complete application|finish application)\b",
    re.I,
)


def is_submit_control(*, tag: str, type_attr: str, text: str, name: str = "", id_attr: str = "") -> bool:
    """True when the control must never be clicked by the filler."""
    if type_attr.lower() == "submit":
        return True
    hay = f"{text} {name} {id_attr}"
    return tag.lower() in ("button", "input", "a") and bool(_SUBMIT.search(hay))


def assert_plan_has_no_submit_clicks(plan: dict) -> None:
    for step in plan.get("steps") or []:
        action = str(step.get("action") or "")
        if action not in ALLOWED_ACTIONS:
            raise AssertionError(f"fill plans may only use {sorted(ALLOWED_ACTIONS)}, not {action!r}")

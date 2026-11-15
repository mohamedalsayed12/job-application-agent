"""Rules the extension must obey when applying a fill plan."""

from __future__ import annotations

import re

_SUBMIT = re.compile(
    r"\b(submit|apply now|send application|complete application|finish application)\b",
    re.I,
)


def is_submit_control(*, tag: str, type_attr: str, text: str, name: str = "", id_attr: str = "") -> bool:
    """True when the control must never be clicked by the filler."""
    if type_attr.lower() == "submit":
        return True
    hay = f"{text} {name} {id_attr}"
    if tag.lower() in ("button", "input", "a") and _SUBMIT.search(hay):
        return True
    return False


def assert_plan_has_no_submit_clicks(plan: dict) -> None:
    for step in plan.get("steps") or []:
        action = str(step.get("action") or "")
        if action == "click":
            raise AssertionError("fill plans must not include click actions")
        if is_submit_control(
            tag="button",
            type_attr="button",
            text=str(step.get("value") or ""),
            name=str(step.get("name") or ""),
        ) and action in ("click", "submit"):
            raise AssertionError("fill plan tries to activate a submit control")

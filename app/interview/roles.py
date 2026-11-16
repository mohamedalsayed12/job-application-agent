"""Normalise a target role into the shared cache key."""

from __future__ import annotations

import re

_SPACE = re.compile(r"\s+")


def role_key(role: str) -> str:
    return _SPACE.sub(" ", role.strip().casefold())

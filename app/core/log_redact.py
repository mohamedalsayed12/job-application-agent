"""Keeps one-time link tokens out of the server's access log.

Uvicorn logs each request's path with its query string, and emailed links carry ``?token=``.
"""

from __future__ import annotations

import logging
import re

_TOKEN = re.compile(r"(token=)[^&\s\"]+", re.IGNORECASE)


def redact(text: str) -> str:
    return _TOKEN.sub(r"\1[redacted]", text)


class RedactTokens(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact(arg) if isinstance(arg, str) else arg for arg in record.args)
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def install() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(existing, RedactTokens) for existing in logger.filters):
        logger.addFilter(RedactTokens())

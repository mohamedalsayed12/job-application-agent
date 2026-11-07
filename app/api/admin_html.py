"""Shared tiny HTML helpers for admin form pages."""

from __future__ import annotations

import html
from urllib.parse import quote

from fastapi.responses import HTMLResponse


def page(title: str, body: str) -> HTMLResponse:
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; max-width: 56rem; }}
    label {{ display: block; margin-top: 0.5rem; }}
    input, textarea, select {{ width: 100%; box-sizing: border-box; }}
    .error {{ color: #a00; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
    th, td {{ border-bottom: 1px solid #ccc; text-align: left; padding: 0.4rem; vertical-align: top; }}
    .actions {{ min-width: 14rem; }}
    code {{ background: #f4f4f4; padding: 0.1rem 0.3rem; }}
  </style>
</head>
<body>
{body}
</body>
</html>"""
    return HTMLResponse(doc)


def action(path: str, admin_key: str) -> str:
    return f"{path}?admin_key={quote(admin_key, safe='')}"

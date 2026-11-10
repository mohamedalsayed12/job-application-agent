"""The shared page shell and stylesheet for server-rendered pages."""

from __future__ import annotations

import html

from fastapi.responses import HTMLResponse

STYLE = """
:root {
  --bg: #f5f6f8; --card: #ffffff; --line: #e3e6eb; --text: #14171f; --muted: #5f6672;
  --primary: #2348c4; --primary-dark: #1b389c; --primary-soft: #e9eefc;
  --good: #1d7a46; --good-soft: #e5f4ea; --bad: #b42318; --bad-soft: #fdecea; --dark: #12151c;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
a { color: var(--primary); }
h1 { font-size: 1.6rem; margin: 0 0 0.25rem; }
h2 { font-size: 1.1rem; margin: 1.75rem 0 0.75rem; }
h3 { font-size: 0.95rem; margin: 1.25rem 0 0.5rem; }
p { margin: 0.5rem 0; }
code { background: #eef0f3; padding: 0.1rem 0.3rem; border-radius: 4px; word-break: break-all; }
.muted, small { color: var(--muted); }
.error { color: var(--bad); background: var(--bad-soft); padding: 0.5rem 0.75rem; border-radius: 6px; }

.topbar { background: var(--card); border-bottom: 1px solid var(--line); }
.topbar-in { max-width: 60rem; margin: 0 auto; padding: 0.6rem 1rem; display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }
.brand { display: flex; align-items: center; gap: 0.5rem; font-weight: 700; color: var(--text); text-decoration: none; }
.brand-mark { width: 1.5rem; height: 1.5rem; border-radius: 6px; background: var(--primary); color: #fff;
  display: grid; place-items: center; font-size: 0.8rem; }
.nav { margin-left: auto; display: flex; align-items: center; gap: 0.25rem; flex-wrap: wrap; }
.nav a, .nav button { color: var(--muted); text-decoration: none; padding: 0.35rem 0.7rem; border-radius: 6px;
  font: inherit; font-size: 0.9rem; font-weight: 400; background: none; border: 0; cursor: pointer; }
.nav a:hover, .nav button:hover { color: var(--text); }
.nav a.active { background: var(--primary-soft); color: var(--primary); font-weight: 600; }
.nav form { margin: 0; }
.topbar.admin { background: var(--dark); border-color: var(--dark); }
.topbar.admin .brand { color: #fff; }
.topbar.admin .nav a, .topbar.admin .nav button { color: #b9bfcb; }
.topbar.admin .nav a.active { background: #2a303c; color: #fff; }

main { max-width: 60rem; margin: 0 auto; padding: 1.75rem 1rem 3rem; }
main.narrow { max-width: 42rem; }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 1rem; flex-wrap: wrap; margin-bottom: 1rem; }

.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 1.1rem 1.25rem; margin-bottom: 1rem; }
.card > h2:first-child { margin-top: 0; }

label { display: block; margin-top: 0.75rem; font-size: 0.85rem; font-weight: 600; }
.hint { font-weight: 400; color: var(--muted); font-size: 0.8rem; }
input, textarea, select { display: block; width: 100%; margin-top: 0.3rem; padding: 0.5rem 0.6rem;
  border: 1px solid var(--line); border-radius: 6px; font: inherit; font-size: 0.9rem; background: #fff; color: var(--text); }
input:focus, textarea:focus, select:focus { outline: 2px solid var(--primary-soft); border-color: var(--primary); }
input[type=file] { padding: 0.4rem; }
.row { display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); gap: 0 0.75rem; }

button, .button { display: inline-block; padding: 0.5rem 0.95rem; border-radius: 6px; border: 1px solid var(--line);
  background: #fff; color: var(--text); font: inherit; font-size: 0.875rem; font-weight: 600; cursor: pointer; text-decoration: none; }
button:hover, .button:hover { border-color: #b9bfcb; }
.primary { background: var(--primary); border-color: var(--primary); color: #fff; }
.primary:hover { background: var(--primary-dark); border-color: var(--primary-dark); }
.danger { color: var(--bad); }
.danger:hover { border-color: var(--bad); }

table { border-collapse: collapse; width: 100%; font-size: 0.875rem; background: var(--card); }
th, td { border-bottom: 1px solid var(--line); text-align: left; padding: 0.5rem 0.6rem; vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 0.8rem; }
.table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; margin: 0.5rem 0 1rem; }
.table-wrap tr:last-child td { border-bottom: 0; }

.tags { display: flex; flex-wrap: wrap; gap: 0.35rem; margin: 0.4rem 0; }
.tag { display: inline-block; font-size: 0.75rem; padding: 0.1rem 0.5rem; border-radius: 999px;
  background: #eef0f3; color: var(--muted); }
.tag.good { background: var(--good-soft); color: var(--good); }
.tag.blue { background: var(--primary-soft); color: var(--primary); }
.tag.miss { background: #fff; border: 1px dashed #b9bfcb; }
.tag.bad { background: var(--bad-soft); color: var(--bad); }
.foot { max-width: 60rem; margin: 0 auto; padding: 0 1rem 2rem; font-size: 0.8rem; color: var(--muted); }
"""


def page(title: str, body: str, *, status_code: int = 200, header: str = "", narrow: bool = False) -> HTMLResponse:
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{html.escape(title)}</title>
  <style>{STYLE}</style>
</head>
<body>
{header}
<main{' class="narrow"' if narrow else ""}>
{body}
</main>
<footer class="foot"><a href="/privacy">Privacy notice</a></footer>
</body>
</html>"""
    # Pages hold personal data and form tokens, so no browser or proxy should keep a copy.
    return HTMLResponse(doc, status_code=status_code, headers={"Cache-Control": "no-store"})

"""Form tokens that stop another website from submitting a form as a logged-in person (D38)."""

from __future__ import annotations

import html
import secrets

from fastapi import Request
from fastapi.responses import HTMLResponse

from app.api.admin_html import page

_MAX_FORM_FILES = 1
_MAX_FORM_FIELDS = 200


class FormRefused(Exception):
    """A form submission that can't go ahead, shown to the person as a page."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def form_refused_page(_: Request, exc: FormRefused) -> HTMLResponse:
    return page("That didn't work", f'<h1>That didn\'t work</h1><p class="error">{html.escape(exc.message)}</p>', status_code=exc.status_code)


def csrf_field(request: Request) -> str:
    token = request.session.setdefault("csrf", secrets.token_urlsafe(32))
    return f'<input type="hidden" name="csrf_token" value="{token}"/>'


async def require_csrf(request: Request) -> None:
    """On every POST route."""
    form = await request.form(max_files=_MAX_FORM_FILES, max_fields=_MAX_FORM_FIELDS)
    expected = request.session.get("csrf")
    provided = form.get("csrf_token")
    if (
        not isinstance(expected, str)
        or not isinstance(provided, str)
        or not secrets.compare_digest(provided.encode(), expected.encode())
    ):
        raise FormRefused(403, "This form has expired. Go back, reload the page and try again.")


# Shows the toggle buttons only when scripts run, so without them the field is a plain password box.
_PASSWORD_TOGGLE_SCRIPT = """<script>
document.querySelectorAll(".pw-toggle").forEach(function (button) {
  var input = document.getElementById(button.getAttribute("aria-controls"));
  var word = button.querySelector(".pw-word");
  var status = document.getElementById(button.getAttribute("aria-controls") + "-status");
  button.hidden = false;
  button.addEventListener("click", function () {
    var show = input.type === "password";
    input.type = show ? "text" : "password";
    word.textContent = show ? "Hide" : "Show";
    status.textContent = show ? "Your password is visible." : "Your password is hidden.";
  });
});
</script>"""


def password_field(label: str, *, name: str = "password", autocomplete: str, field_id: str | None = None, hint: str = "") -> str:
    """A password box with a Show/Hide button. The button's name says what it will do next ("Show password")."""
    field_id = field_id or f"field-{name}"
    hint_id = f"{field_id}-hint"
    described = f' aria-describedby="{hint_id}"' if hint else ""
    hint_html = f'<span class="hint" id="{hint_id}">{html.escape(hint)}</span>' if hint else ""
    return f"""<label for="{field_id}">{html.escape(label)} {hint_html}</label>
<div class="pw-wrap">
  <input id="{field_id}" name="{name}" type="password" required autocomplete="{autocomplete}"{described}/>
  <button type="button" class="pw-toggle" aria-controls="{field_id}" hidden><span class="pw-word">Show</span><span class="visually-hidden"> password</span></button>
</div>
<span class="visually-hidden" id="{field_id}-status" aria-live="polite"></span>"""


def password_toggle_script() -> str:
    """Once per page, after the last password field."""
    return _PASSWORD_TOGGLE_SCRIPT

"""The unpacked extension fills local fixture pages and never submits them."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "apply_pages"
EXTENSION = Path(__file__).resolve().parents[1] / "extension"
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<<>>\n%%EOF"
APP_ID = "11111111-1111-1111-1111-111111111111"
NOTICE = "Everything is filled in. Review the form and click Submit yourself."


class _Handler(BaseHTTPRequestHandler):
    def _json(self, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/extension/applications/ready":
            self._json([{"id": APP_ID, "apply_url": "https://careers.example.com/jobs/1/apply"}])
            return
        if path == "/extension/cv":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'attachment; filename="cv.pdf"')
            self.send_header("Content-Length", str(len(PDF)))
            self.end_headers()
            self.wfile.write(PDF)
            return
        file = FIXTURES / path.lstrip("/")
        if file.is_file():
            data = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        path = urlparse(self.path).path
        if path.endswith("/form") or path.endswith("/plan"):
            steps = []
            for field in json.loads(raw)["fields"]:
                if field["type"] == "file":
                    step = {"field_id": field["id"], "action": "upload_cv", "value": "cv", "source": "cv"}
                elif field["type"] == "email":
                    step = {"field_id": field["id"], "action": "fill", "value": "ada@example.com", "source": "account"}
                else:
                    step = {"field_id": field["id"], "action": "fill", "value": "Ada Lovelace", "source": "answers"}
                steps.append(step)
            self._json({"status": "ready_to_fill", "plan": {"steps": steps, "missing": [], "attach": []}, "notice": NOTICE})
            return
        if path.endswith("/status"):
            self._json({"ok": True})
            return
        self.send_error(404)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def _filled(page, *, email: str | None, cv: bool) -> None:
    assert page.input_value("[name=first_name]") == "Ada Lovelace"
    if email is not None:
        assert page.input_value("[name=email]") == email
    if cv:
        assert page.evaluate("() => document.querySelector('[name=cv]').files.length") == 1
    assert page.evaluate("() => window.__submitted") == 0
    assert "submitted=1" not in page.url
    assert "click Submit yourself" in page.inner_text("#jaa-fill-notice")
    button = page.eval_on_selector("#submit", "el => el.outerHTML")
    assert 'data-jaa-highlight="submit"' in button, button
    assert "outline" in button, button


@pytest.fixture(scope="module")
def extension_app():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_extension_fills_local_forms_and_never_submits(extension_app, tmp_path) -> None:
    from playwright.sync_api import sync_playwright

    extension = str(EXTENSION)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(tmp_path / "chrome"),
            headless=False,
            ignore_default_args=["--disable-extensions"],
            args=[
                f"--disable-extensions-except={extension}",
                f"--load-extension={extension}",
                "--disable-features=DisableLoadExtensionCommandLineSwitch",
            ],
        )
        try:
            worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
            worker.evaluate(
                """async (base) => { await chrome.storage.local.set({ apiBase: base, token: "test-token" }); }""",
                extension_app,
            )
            page = context.pages[0] if context.pages else context.new_page()
            for name in ("plain.html", "multi.html", "div.html", "continue.html"):
                page.goto(f"{extension_app}/{name}")
                page.bring_to_front()
                result = worker.evaluate("() => fillActiveTab()")
                assert result["ok"] is True, result
                assert result.get("highlighted") == "submit", result
                page.wait_for_selector("#jaa-fill-notice")
                page.wait_for_selector("#submit[data-jaa-highlight=submit]")
                _filled(page, email="ada@example.com", cv=name not in ("div.html", "continue.html"))
                if name == "multi.html":
                    assert page.evaluate("() => window.__next") == 1
                    assert page.is_visible("#step2")
        finally:
            context.close()

"""Ownership proof from scripts and job links on the company's own domain."""

from __future__ import annotations

import ipaddress

import httpx

from app.fetch.gateway import FetchGateway, FetchRequest, FetchResponse
from app.models import Candidate, EvidenceLevel
from app.verify.ownership import assess_ownership

HOME = "https://acme.example/"
BOARD_ID = "acme"
JOB_IDS = ("8358586", "8353637", "8358847", "8331009")


def _gateway(pages: dict[str, tuple[str, str]], fetched: list[str] | None = None) -> FetchGateway:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if fetched is not None:
            fetched.append(url)
        if url in pages:
            body, content_type = pages[url]
            return httpx.Response(200, text=body, headers={"content-type": content_type})
        return httpx.Response(404, text="missing", headers={"content-type": "text/html"})

    return FetchGateway(
        allowlist=frozenset(),
        resolve=lambda host: [ipaddress.IPv4Address("93.184.216.34")],
        transport=httpx.MockTransport(handler),
    )


async def _level(
    home_html: str,
    pages: dict[str, tuple[str, str]] | None = None,
    *,
    board_id: str = BOARD_ID,
    system: str = "greenhouse",
    job_ids: tuple[str, ...] = JOB_IDS,
    fetched: list[str] | None = None,
):
    gateway = _gateway(pages or {}, fetched)
    candidate = Candidate(company_name="Acme", board_system=system, board_id=board_id)
    result = await assess_ownership(
        gateway,
        candidate,
        domain="acme.example",
        homepage_html=home_html,
        homepage_url=HOME,
        api_job_ids=job_ids,
    )
    await gateway.aclose()
    return result


def _script_page(src: str) -> str:
    return f'<html><head><script src="{src}"></script></head><body>Jobs</body></html>'


def _links(*hrefs: str) -> str:
    return "<html><body>" + "".join(f'<a href="{h}">role</a>' for h in hrefs) + "</body></html>"


JS = "application/javascript"


async def test_own_script_calling_this_greenhouse_board_is_strong() -> None:
    script = 'fetch("https://boards-api.greenhouse.io/v1/boards/acme/departments?render_as=tree")'
    result = await _level(_script_page("/assets/jobs.js"), {"https://acme.example/assets/jobs.js": (script, JS)})
    assert result.level == EvidenceLevel.strong
    assert result.record.evidence_url == "https://acme.example/assets/jobs.js"


async def test_own_script_calling_this_lever_board_is_strong() -> None:
    script = 'const u = "https://api.lever.co/v0/postings/acme?mode=json";'
    result = await _level(
        _script_page("/jobs.js"), {"https://acme.example/jobs.js": (script, JS)}, system="lever"
    )
    assert result.level == EvidenceLevel.strong


async def test_inline_script_calling_this_board_is_strong() -> None:
    html = '<script>fetch("https://boards-api.greenhouse.io/v1/boards/acme/jobs")</script>'
    result = await _level(html)
    assert result.level == EvidenceLevel.strong


async def test_own_script_calling_another_board_proves_nothing() -> None:
    script = 'fetch("https://boards-api.greenhouse.io/v1/boards/acmeholdings/jobs")'
    result = await _level(_script_page("/jobs.js"), {"https://acme.example/jobs.js": (script, JS)})
    assert result.level == EvidenceLevel.weak


async def test_script_on_another_site_is_not_fetched_or_trusted() -> None:
    fetched: list[str] = []
    result = await _level(
        _script_page("https://cdn.other.example/jobs.js"),
        {
            "https://cdn.other.example/jobs.js": (
                'fetch("https://boards-api.greenhouse.io/v1/boards/acme/jobs")',
                JS,
            )
        },
        fetched=fetched,
    )
    assert result.level == EvidenceLevel.weak
    assert "https://cdn.other.example/jobs.js" not in fetched


async def test_script_fetches_are_capped() -> None:
    fetched: list[str] = []
    html = "".join(f'<script src="/s{i}.js"></script>' for i in range(30))
    await _level(html, fetched=fetched)
    assert sum(url.endswith(".js") for url in fetched) <= 8


async def test_scripts_named_for_jobs_are_read_first() -> None:
    plain = "".join(f'<script src="/s{i}.js"></script>' for i in range(30))
    script = 'fetch("https://boards-api.greenhouse.io/v1/boards/acme/jobs")'
    result = await _level(
        plain + '<script src="/module_greenhouse_api.js"></script>',
        {"https://acme.example/module_greenhouse_api.js": (script, JS)},
    )
    assert result.level == EvidenceLevel.strong


async def test_own_links_with_three_board_job_ids_are_strong() -> None:
    html = _links(
        "https://careers.acme.example/role/account-manager-8358586/",
        "https://careers.acme.example/role/developer-8353637/",
        "/role/designer-8358847/",
    )
    result = await _level(html)
    assert result.level == EvidenceLevel.strong


async def test_two_job_ids_are_not_enough_on_a_larger_board() -> None:
    html = _links("/role/a-8358586/", "/role/b-8353637/")
    result = await _level(html)
    assert result.level == EvidenceLevel.weak


async def test_the_same_job_id_three_times_counts_once() -> None:
    html = _links("/role/a-8358586/", "/role/a-8358586/?x=1", "/apply/8358586")
    result = await _level(html)
    assert result.level == EvidenceLevel.weak


async def test_every_job_id_is_required_on_a_small_board() -> None:
    html = _links("/role/only-8358586/")
    result = await _level(html, job_ids=("8358586",))
    assert result.level == EvidenceLevel.strong


async def test_job_ids_on_links_to_other_sites_prove_nothing() -> None:
    html = _links(
        "https://aggregator.example/8358586",
        "https://aggregator.example/8353637",
        "https://notacme.example/8358847",
    )
    result = await _level(html)
    assert result.level == EvidenceLevel.weak


async def test_a_lookalike_domain_is_not_the_company() -> None:
    html = _links(
        "https://weareacme.example/8358586",
        "https://weareacme.example/8353637",
        "https://weareacme.example/8358847",
    )
    result = await _level(html)
    assert result.level == EvidenceLevel.weak


async def test_part_of_a_longer_number_is_not_a_job_id() -> None:
    html = _links("/a/183585861", "/b/283536372", "/c/383588473")
    result = await _level(html)
    assert result.level == EvidenceLevel.weak


async def test_gateway_reads_javascript_as_text() -> None:
    gateway = _gateway({"https://acme.example/jobs.js": ("const a = 1;", "text/javascript; charset=utf-8")})
    result = await gateway.fetch(
        FetchRequest(url="https://acme.example/jobs.js", kind="page", official_domain="acme.example")
    )
    await gateway.aclose()
    assert isinstance(result, FetchResponse)
    assert result.body == b"const a = 1;"

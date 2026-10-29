"""Detection and parsing tests for every adapter, offline against saved fixtures."""

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app.db.models import utcnow
from app.scraping.adapters.ashby import AshbyAdapter
from app.scraping.adapters.css_selector import CssSelectorAdapter
from app.scraping.adapters.greenhouse import GreenhouseAdapter
from app.scraping.adapters.heuristic import HeuristicAdapter
from app.scraping.adapters.jsonld import JsonLdAdapter
from app.scraping.adapters.lever import LeverAdapter
from app.scraping.adapters.smartrecruiters import SmartRecruitersAdapter
from app.scraping.adapters.workday import WorkdayAdapter, parse_posted_on
from app.scraping.base import AdapterConfig, FetchContext, FetchError
from app.scraping.http import PoliteClient
from tests.scraping_helpers import fixture_json, fixture_text, make_client, make_ctx


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=True) as router:
        yield router


@pytest.fixture
async def client() -> AsyncIterator[PoliteClient]:
    async with make_client() as client:
        yield client


@pytest.fixture
def ctx(client: PoliteClient) -> FetchContext:
    return make_ctx(client)


# --- Greenhouse -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/examplecorp",
        "https://job-boards.greenhouse.io/examplecorp/jobs/4012345007",
        "https://boards.greenhouse.io/embed/job_board?for=examplecorp",
    ],
)
def test_greenhouse_detects_board_urls(url: str) -> None:
    assert GreenhouseAdapter().detect(url, None) == AdapterConfig(
        "greenhouse", {"token": "examplecorp"}
    )


def test_greenhouse_detects_embed_script() -> None:
    cfg = GreenhouseAdapter().detect(
        "https://example.com/careers", fixture_text("greenhouse_embed.html")
    )
    assert cfg == AdapterConfig("greenhouse", {"token": "examplecorp"})


def test_greenhouse_ignores_unrelated_pages() -> None:
    assert GreenhouseAdapter().detect("https://example.com/careers", "<p>hi</p>") is None
    assert GreenhouseAdapter().detect("https://boards.greenhouse.io/", None) is None


async def test_greenhouse_parses_jobs(mock: respx.MockRouter, ctx: FetchContext) -> None:
    mock.get("https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs?content=true").respond(
        200, json=fixture_json("greenhouse_jobs.json")
    )
    postings = await GreenhouseAdapter().fetch(
        AdapterConfig("greenhouse", {"token": "examplecorp"}), ctx
    )
    assert len(postings) == 2  # the untitled job is skipped
    first, second = postings
    assert first.title == "Senior Backend Engineer (Python)"
    assert first.url == "https://job-boards.greenhouse.io/examplecorp/jobs/4012345007"
    assert first.external_id == "4012345007"
    assert first.location == "Remote - US"
    assert first.remote is True
    assert first.department == "Engineering"
    assert first.posted_at == datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
    assert first.description_text == "About the role Build & run our Python services."
    assert second.posted_at is None
    assert second.department is None
    assert second.remote is None


async def test_greenhouse_rejects_unexpected_shape(
    mock: respx.MockRouter, ctx: FetchContext
) -> None:
    mock.get(url__startswith="https://boards-api.greenhouse.io/").respond(200, json={"error": 1})
    with pytest.raises(FetchError, match="no job list"):
        await GreenhouseAdapter().fetch(AdapterConfig("greenhouse", {"token": "x"}), ctx)


# --- Lever ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "api_host"),
    [
        ("https://jobs.lever.co/examplecorp", "api.lever.co"),
        ("https://jobs.lever.co/examplecorp/5f2c1b9e-1111", "api.lever.co"),
        ("https://jobs.eu.lever.co/examplecorp", "api.eu.lever.co"),
    ],
)
def test_lever_detects_board_urls(url: str, api_host: str) -> None:
    assert LeverAdapter().detect(url, None) == AdapterConfig(
        "lever", {"company": "examplecorp", "api_host": api_host}
    )


def test_lever_detects_links() -> None:
    cfg = LeverAdapter().detect("https://example.com/work", fixture_text("lever_links.html"))
    assert cfg is not None
    assert cfg.params["company"] == "examplecorp"


async def test_lever_parses_postings(mock: respx.MockRouter, ctx: FetchContext) -> None:
    mock.get("https://api.lever.co/v0/postings/examplecorp?mode=json&limit=100&skip=0").respond(
        200, json=fixture_json("lever_postings.json")
    )
    postings = await LeverAdapter().fetch(
        AdapterConfig("lever", {"company": "examplecorp", "api_host": "api.lever.co"}), ctx
    )
    sre, analyst = postings
    assert sre.title == "Site Reliability Engineer"
    assert sre.external_id == "5f2c1b9e-1111-4c8a-9d2e-000000000001"
    assert sre.location == "San Francisco, CA"
    assert sre.department == "Platform"
    assert sre.employment_type == "Full-time"
    assert sre.remote is None  # hybrid
    assert sre.posted_at == datetime.fromtimestamp(1758030000, UTC)
    assert sre.description_text == "You will run the platform that runs everything else."
    assert analyst.remote is True


async def test_lever_paginates(mock: respx.MockRouter, ctx: FetchContext) -> None:
    def page(start: int, count: int) -> list[dict[str, object]]:
        return [
            {"id": f"id-{i}", "text": f"Job {i}", "hostedUrl": f"https://jobs.lever.co/x/{i}"}
            for i in range(start, start + count)
        ]

    base = "https://api.lever.co/v0/postings/x?mode=json&limit=100"
    mock.get(f"{base}&skip=0").respond(200, json=page(0, 100))
    mock.get(f"{base}&skip=100").respond(200, json=page(100, 7))
    postings = await LeverAdapter().fetch(AdapterConfig("lever", {"company": "x"}), ctx)
    assert len(postings) == 107


# --- Ashby ----------------------------------------------------------------------------------


def test_ashby_detects() -> None:
    expected = AdapterConfig("ashby", {"org": "examplecorp"})
    assert AshbyAdapter().detect("https://jobs.ashbyhq.com/examplecorp", None) == expected
    html = '<script src="https://jobs.ashbyhq.com/examplecorp/embed?version=2"></script>'
    assert AshbyAdapter().detect("https://example.com/careers", html) == expected


async def test_ashby_parses_jobs(mock: respx.MockRouter, ctx: FetchContext) -> None:
    mock.get(
        "https://api.ashbyhq.com/posting-api/job-board/examplecorp?includeCompensation=true"
    ).respond(200, json=fixture_json("ashby_board.json"))
    postings = await AshbyAdapter().fetch(AdapterConfig("ashby", {"org": "examplecorp"}), ctx)
    assert [p.title for p in postings] == ["Machine Learning Engineer", "Customer Success Manager"]
    ml, csm = postings
    assert ml.location == "New York, NY; Remote (US)"
    assert ml.department == "Research"
    assert ml.employment_type == "Full time"
    assert ml.remote is True  # "Remote (US)" is one of its locations
    assert ml.posted_at == datetime(2026, 10, 2, 15, 4, 5, tzinfo=UTC)
    assert csm.remote is True
    assert csm.employment_type == "Part time"


# --- SmartRecruiters ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.smartrecruiters.com/ExampleCorp",
        "https://careers.smartrecruiters.com/ExampleCorp/",
    ],
)
def test_smartrecruiters_detects(url: str) -> None:
    assert SmartRecruitersAdapter().detect(url, None) == AdapterConfig(
        "smartrecruiters", {"company": "ExampleCorp"}
    )


async def test_smartrecruiters_paginates(mock: respx.MockRouter, ctx: FetchContext) -> None:
    base = "https://api.smartrecruiters.com/v1/companies/ExampleCorp/postings?limit=100"
    mock.get(f"{base}&offset=0").respond(200, json=fixture_json("smartrecruiters_page1.json"))
    mock.get(f"{base}&offset=2").respond(200, json=fixture_json("smartrecruiters_page2.json"))
    postings = await SmartRecruitersAdapter().fetch(
        AdapterConfig("smartrecruiters", {"company": "ExampleCorp"}), ctx
    )
    assert [p.title for p in postings] == ["Senior Accountant", "Payroll Specialist", "Controller"]
    first = postings[0]
    assert first.url == "https://jobs.smartrecruiters.com/ExampleCorp/7440000900001"
    assert first.location == "Denver, CO, United States"
    assert first.department == "Finance"
    assert first.employment_type == "Full-time"
    assert first.remote is None
    assert postings[1].remote is True


# --- Workday --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "params"),
    [
        (
            "https://examplecorp.wd5.myworkdayjobs.com/en-US/External_Careers",
            {
                "host": "examplecorp.wd5.myworkdayjobs.com",
                "tenant": "examplecorp",
                "site": "External_Careers",
                "base_url": "https://examplecorp.wd5.myworkdayjobs.com/en-US/External_Careers",
            },
        ),
        (
            "https://examplecorp.wd1.myworkdayjobs.com/Careers/job/Denver/Engineer_R1?q=x",
            {
                "host": "examplecorp.wd1.myworkdayjobs.com",
                "tenant": "examplecorp",
                "site": "Careers",
                "base_url": "https://examplecorp.wd1.myworkdayjobs.com/Careers",
            },
        ),
        (
            "https://wd3.myworkdaysite.com/recruiting/examplecorp/Ext/details/x",
            {
                "host": "wd3.myworkdaysite.com",
                "tenant": "examplecorp",
                "site": "Ext",
                "base_url": "https://wd3.myworkdaysite.com/recruiting/examplecorp/Ext",
            },
        ),
    ],
)
def test_workday_detects(url: str, params: dict[str, str]) -> None:
    assert WorkdayAdapter().detect(url, None) == AdapterConfig("workday", params)


def test_workday_detects_links_and_ignores_others() -> None:
    cfg = WorkdayAdapter().detect("https://example.com/careers", fixture_text("workday_links.html"))
    assert cfg is not None
    assert cfg.params["site"] == "External_Careers"
    assert WorkdayAdapter().detect("https://examplecorp.wd5.myworkdayjobs.com/", None) is None


async def test_workday_paginates_with_total_from_first_page(
    mock: respx.MockRouter, ctx: FetchContext
) -> None:
    endpoint = (
        "https://examplecorp.wd5.myworkdayjobs.com/wday/cxs/examplecorp/External_Careers/jobs"
    )
    route = mock.post(endpoint).mock(
        side_effect=[
            httpx.Response(200, json=fixture_json("workday_page1.json")),
            httpx.Response(200, json=fixture_json("workday_page2.json")),
        ]
    )
    cfg = WorkdayAdapter().detect(
        "https://examplecorp.wd5.myworkdayjobs.com/en-US/External_Careers", None
    )
    assert cfg is not None
    postings = await WorkdayAdapter().fetch(cfg, ctx)

    assert [json.loads(c.request.content)["offset"] for c in route.calls] == [0, 2]
    assert json.loads(route.calls[0].request.content)["limit"] == 20
    assert [p.title for p in postings] == [
        "Software Engineer II",
        "Staff Data Engineer",
        "Technical Writer",
    ]
    first = postings[0]
    assert first.url == (
        "https://examplecorp.wd5.myworkdayjobs.com/en-US/External_Careers"
        "/job/Denver-CO/Software-Engineer-II_JR-0001234"
    )
    assert first.external_id == "JR-0001234"
    assert postings[1].remote is True


def test_workday_posted_on() -> None:
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    assert parse_posted_on("Posted Today") == today
    assert parse_posted_on("Posted Yesterday") == today - timedelta(days=1)
    assert parse_posted_on("Posted 3 Days Ago") == today - timedelta(days=3)
    assert parse_posted_on("Posted 30+ Days Ago") is None
    assert parse_posted_on(None) is None


# --- JSON-LD --------------------------------------------------------------------------------


def test_jsonld_parses_item_list_in_graph() -> None:
    html = fixture_text("jsonld_listing.html")
    adapter = JsonLdAdapter()
    cfg = adapter.detect("https://careers.example.com/", html)
    assert cfg == AdapterConfig("jsonld")
    assert cfg is not None
    warehouse, support = adapter.parse(cfg, html, "https://careers.example.com/")
    assert warehouse.title == "Warehouse Associate"
    assert warehouse.url == "https://careers.example.com/jobs/wa-17"
    assert warehouse.external_id == "WA-17"
    assert warehouse.location == "Aurora, CO, US"
    assert warehouse.employment_type == "Full time, Temporary"
    assert warehouse.posted_at == datetime(2026, 10, 12, tzinfo=UTC)
    assert warehouse.description_text == "Pick & pack orders."
    assert support.external_id == "SE-3"
    assert support.remote is True
    assert support.location == "Remote"
    assert support.posted_at == datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


def test_jsonld_single_posting_uses_page_url() -> None:
    adapter = JsonLdAdapter()
    (posting,) = adapter.parse(
        AdapterConfig("jsonld"), fixture_text("jsonld_single.html"), "https://diner.example/jobs"
    )
    assert posting.url == "https://diner.example/jobs"
    assert posting.location == "123 Main St, Golden, CO"


def test_jsonld_not_detected_without_job_postings() -> None:
    html = '<script type="application/ld+json">{"@type": "Organization"}</script>'
    assert JsonLdAdapter().detect("https://x.example/", html) is None


# --- CSS selectors --------------------------------------------------------------------------


def test_css_selector_extracts_items() -> None:
    cfg = AdapterConfig(
        "css_selector",
        {
            "item_selector": "li.opening",
            "title_selector": ".role",
            "link_selector": "a.details",
            "location_selector": ".where",
        },
    )
    postings = CssSelectorAdapter().parse(
        cfg, fixture_text("css_page.html"), "https://shop.example.com/jobs"
    )
    assert [(p.title, p.url, p.location) for p in postings] == [
        ("Barista", "/apply?role=barista", "Boulder, CO"),
        ("Shift Lead (Remote training)", "https://shop.example.com/apply?role=lead", "Remote"),
    ]
    assert postings[1].remote is True


def test_css_selector_item_can_be_the_link() -> None:
    cfg = AdapterConfig("css_selector", {"item_selector": "a.opening-link"})
    (posting,) = CssSelectorAdapter().parse(
        cfg, fixture_text("css_page.html"), "https://s.example/"
    )
    assert (posting.title, posting.url) == ("Plain link opening", "/jobs/1")


@pytest.mark.parametrize(
    "params", [{}, {"item_selector": "li[["}, {"item_selector": "li", "title_selector": "::bad("}]
)
def test_css_selector_errors(params: dict[str, str]) -> None:
    with pytest.raises(FetchError):
        CssSelectorAdapter().parse(
            AdapterConfig("css_selector", params), fixture_text("css_page.html"), "https://s/"
        )


def test_css_selector_is_never_auto_detected() -> None:
    assert CssSelectorAdapter().detect("https://x/", fixture_text("css_page.html")) is None


# --- Heuristic links ------------------------------------------------------------------------


def test_heuristic_finds_job_links() -> None:
    base = "https://hospital.example.org/careers"
    adapter = HeuristicAdapter()
    html = fixture_text("heuristic_page.html")
    assert adapter.detect(base, html) == AdapterConfig("heuristic")
    postings = adapter.parse(AdapterConfig("heuristic"), html, base)
    assert [(p.title, p.url) for p in postings] == [
        (
            "Registered Nurse - ICU",
            "https://hospital.example.org/careers/registered-nurse-icu-2041",
        ),
        ("Respiratory Therapist", "https://hospital.example.org/jobs/2042/"),
        ("Pharmacist", "https://jobs.examplehealth.org/job/Pharmacist/1234"),
        ("Unit Clerk", "https://hospital.example.org/positions?gh_jid=55501"),
    ]
    assert postings[-1].external_id == "55501"


def test_heuristic_finds_nothing_on_plain_page() -> None:
    html = '<a href="/about">About</a><a href="/careers">Careers</a>'
    assert HeuristicAdapter().detect("https://x.example/careers", html) is None


async def test_workday_keeps_first_page_total_across_many_pages(
    mock: respx.MockRouter, ctx: FetchContext
) -> None:
    """Pages after the first report total=0; the adapter must keep going until it has them all."""

    def page(start: int, count: int, total: int) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total": total,
                "jobPostings": [
                    {"title": f"Role {i}", "externalPath": f"/job/X/Role_{i}"}
                    for i in range(start, start + count)
                ],
            },
        )

    mock.post(url__startswith="https://acme.wd1.myworkdayjobs.com/wday/cxs/").mock(
        side_effect=[page(0, 20, 45), page(20, 20, 0), page(40, 5, 0)]
    )
    cfg = WorkdayAdapter().detect("https://acme.wd1.myworkdayjobs.com/Careers", None)
    assert cfg is not None
    postings = await WorkdayAdapter().fetch(cfg, ctx)
    assert len(postings) == 45

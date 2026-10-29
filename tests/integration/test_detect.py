"""Auto-detection and fetch orchestration, offline with respx and a fake browser."""

from collections.abc import AsyncIterator, Iterator

import pytest
import respx

from app.scraping import base as base_module
from app.scraping.base import (
    AdapterConfig,
    BlockedError,
    BrowserUnavailableError,
    FetchContext,
    FetchError,
    RawPosting,
)
from app.scraping.detect import NothingFoundError, detect
from app.scraping.fetch import fetch_postings, finalize
from app.scraping.http import PoliteClient
from tests.scraping_helpers import fixture_json, fixture_text, make_client, make_ctx

SITE = "https://careers.example.com"
JS_PAGE = """<html><body><div id="app"></div>
<a href="/jobs/101">Night Shift Supervisor</a><a href="/jobs/102">Forklift Operator</a>
</body></html>"""


class FakeBrowser:
    def __init__(self, html: str = JS_PAGE) -> None:
        self.html = html
        self.calls: list[str] = []

    @staticmethod
    def installed() -> bool:
        return True

    async def render(
        self, url: str, *, respect_robots: bool, wait_selector: str | None = None
    ) -> tuple[str, str]:
        self.calls.append(url)
        return self.html, url


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=True) as router:
        yield router


@pytest.fixture
async def client() -> AsyncIterator[PoliteClient]:
    async with make_client() as client:
        yield client


async def test_board_url_goes_straight_to_api(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.get(url__startswith="https://boards-api.greenhouse.io/v1/boards/examplecorp/").respond(
        200, json=fixture_json("greenhouse_jobs.json")
    )
    outcome = await detect("https://boards.greenhouse.io/examplecorp", make_ctx(client))
    assert outcome.config == AdapterConfig("greenhouse", {"token": "examplecorp"})
    assert len(outcome.postings) == 2
    assert not outcome.rendered


async def test_embedded_board_found_on_company_page(
    mock: respx.MockRouter, client: PoliteClient
) -> None:
    mock.get(f"{SITE}/").respond(200, html=fixture_text("greenhouse_embed.html"))
    mock.get(url__startswith="https://boards-api.greenhouse.io/").respond(
        200, json=fixture_json("greenhouse_jobs.json")
    )
    outcome = await detect(f"{SITE}/", make_ctx(client))
    assert outcome.config.adapter == "greenhouse"


async def test_workday_link_on_company_page(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.get(f"{SITE}/").respond(200, html=fixture_text("workday_links.html"))
    mock.post(url__startswith="https://examplecorp.wd5.myworkdayjobs.com/wday/cxs/").respond(
        200, json={"total": 1, "jobPostings": fixture_json("workday_page1.json")["jobPostings"][:1]}
    )
    outcome = await detect(f"{SITE}/", make_ctx(client))
    assert outcome.config.adapter == "workday"
    assert [p.title for p in outcome.postings] == ["Software Engineer II"]


async def test_jsonld_page(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.get(f"{SITE}/").respond(200, html=fixture_text("jsonld_listing.html"))
    outcome = await detect(f"{SITE}/", make_ctx(client))
    assert outcome.config.adapter == "jsonld"
    assert len(outcome.postings) == 2


async def test_heuristic_fallback(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.get(f"{SITE}/careers").respond(200, html=fixture_text("heuristic_page.html"))
    outcome = await detect(f"{SITE}/careers", make_ctx(client))
    assert outcome.config.adapter == "heuristic"
    assert len(outcome.postings) == 4


async def test_css_selectors_take_priority(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.get("https://shop.example.com/jobs").respond(200, html=fixture_text("css_page.html"))
    outcome = await detect(
        "https://shop.example.com/jobs",
        make_ctx(client),
        css={"item_selector": "li.opening", "title_selector": ".role", "location_selector": ""},
    )
    assert outcome.config == AdapterConfig(
        "css_selector", {"item_selector": "li.opening", "title_selector": ".role"}
    )
    # Relative links are resolved against the page.
    assert outcome.postings[0].url == "https://shop.example.com/apply?role=barista"


async def test_nothing_found(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.get(f"{SITE}/").respond(200, html="<p>We are not hiring.</p>")
    with pytest.raises(NothingFoundError, match="has no browser"):
        await detect(f"{SITE}/", make_ctx(client, render_mode="auto"))


async def test_refusal_is_not_worked_around(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.get(f"{SITE}/").respond(403)
    browser = FakeBrowser()
    with pytest.raises(BlockedError):
        await detect(f"{SITE}/", make_ctx(client, render_mode="auto", browser=browser))
    assert browser.calls == []


async def test_robots_respected_during_detection(
    mock: respx.MockRouter, client: PoliteClient
) -> None:
    mock.get(f"{SITE}/robots.txt").respond(200, text="User-agent: *\nDisallow: /\n")
    with pytest.raises(BlockedError, match=r"robots\.txt"):
        await detect(f"{SITE}/", make_ctx(client, respect_robots=True))


async def test_private_url_rejected_before_fetching(client: PoliteClient) -> None:
    with pytest.raises(BlockedError, match="non-public"):
        await detect("http://192.168.1.1/admin", make_ctx(client))


# --- rendering ------------------------------------------------------------------------------


async def test_auto_mode_falls_back_to_browser(
    mock: respx.MockRouter, client: PoliteClient
) -> None:
    mock.get(f"{SITE}/jobs").respond(
        200, html='<div id="app"></div><script src="/app.js"></script>'
    )
    browser = FakeBrowser()
    outcome = await detect(f"{SITE}/jobs", make_ctx(client, render_mode="auto", browser=browser))
    assert outcome.rendered
    assert outcome.config.adapter == "heuristic"
    assert [p.title for p in outcome.postings] == ["Night Shift Supervisor", "Forklift Operator"]
    assert browser.calls == [f"{SITE}/jobs"]


async def test_static_mode_never_renders(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.get(f"{SITE}/jobs").respond(200, html="<div id=app></div>")
    browser = FakeBrowser()
    with pytest.raises(NothingFoundError):
        await detect(f"{SITE}/jobs", make_ctx(client, render_mode="static", browser=browser))
    assert browser.calls == []


async def test_browser_mode_skips_static_fetch(client: PoliteClient) -> None:
    browser = FakeBrowser()
    outcome = await detect(f"{SITE}/jobs", make_ctx(client, render_mode="browser", browser=browser))
    assert outcome.rendered  # respx not active: any static request would have failed


async def test_browser_mode_without_browser(client: PoliteClient) -> None:
    with pytest.raises(BrowserUnavailableError):
        await fetch_postings(
            AdapterConfig("heuristic"), f"{SITE}/jobs", make_ctx(client, render_mode="browser")
        )


async def test_fetch_postings_for_page_adapter_auto_mode(
    mock: respx.MockRouter, client: PoliteClient
) -> None:
    mock.get(f"{SITE}/jobs").respond(200, html="<p>loading</p>")
    browser = FakeBrowser()
    ctx: FetchContext = make_ctx(client, render_mode="auto", browser=browser)
    outcome = await fetch_postings(AdapterConfig("heuristic"), f"{SITE}/jobs", ctx)
    assert outcome.rendered
    assert len(outcome.postings) == 2


async def test_unknown_adapter(client: PoliteClient) -> None:
    with pytest.raises(FetchError, match="unknown adapter"):
        await fetch_postings(AdapterConfig("nope"), SITE, make_ctx(client))


# --- finalize -------------------------------------------------------------------------------


def test_finalize_cleans_dedupes_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = [
        RawPosting(title="  Senior\n  Engineer ", url="/jobs/1?utm_source=x"),
        RawPosting(title="Senior Engineer (dupe)", url="https://x.example/jobs/1"),
        RawPosting(title="", url="/jobs/2"),
        RawPosting(title="No URL", url=""),
        RawPosting(title="Bad scheme", url="javascript:void(0)"),
        RawPosting(title="Second", url="/jobs/3"),
        RawPosting(title="Third", url="/jobs/4"),
    ]
    result = finalize("heuristic", raw, "https://x.example/careers")
    assert [(p.title, p.url) for p in result] == [
        ("Senior Engineer", "https://x.example/jobs/1?utm_source=x"),
        ("Second", "https://x.example/jobs/3"),
        ("Third", "https://x.example/jobs/4"),
    ]
    monkeypatch.setattr("app.scraping.fetch.MAX_POSTINGS", 2)
    assert len(finalize("heuristic", raw, "https://x.example/careers")) == 2


def test_finalize_truncates_long_fields() -> None:
    long = RawPosting(title="T" * 900, url="https://x.example/1", description_text="d" * 50_000)
    (posting,) = finalize("jsonld", [long], "https://x.example/")
    assert len(posting.title) == base_module.MAX_TITLE
    assert posting.description_text is not None
    assert len(posting.description_text) == base_module.MAX_DESCRIPTION

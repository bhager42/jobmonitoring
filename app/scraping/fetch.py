"""Fetch postings for a configured adapter, handling static vs. browser rendering."""

import logging
from dataclasses import dataclass

from app.scraping.adapters import ADAPTERS, API_ADAPTERS
from app.scraping.base import (
    MAX_POSTINGS,
    AdapterConfig,
    BrowserUnavailableError,
    FetchContext,
    FetchError,
    PageAdapter,
    RawPosting,
    normalize,
)
from app.scraping.urls import fingerprint

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Page:
    html: str
    url: str
    rendered: bool


@dataclass(frozen=True)
class FetchOutcome:
    config: AdapterConfig
    postings: list[RawPosting]
    rendered: bool = False


def finalize(adapter: str, postings: list[RawPosting], base_url: str) -> list[RawPosting]:
    """Normalize, drop unusable entries, de-duplicate by fingerprint, apply the cap."""
    seen: set[str] = set()
    result: list[RawPosting] = []
    for posting in postings:
        cleaned = normalize(posting, base_url)
        if cleaned is None:
            continue
        key = fingerprint(adapter, cleaned.external_id, cleaned.url)
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        if len(result) >= MAX_POSTINGS:
            log.warning("Stopping at %d postings from %s", MAX_POSTINGS, base_url)
            break
    return result


def browser_usable(ctx: FetchContext) -> bool:
    return ctx.browser is not None and ctx.browser.installed()


async def load_page(url: str, ctx: FetchContext, *, render: bool) -> Page:
    if render:
        if ctx.browser is None or not ctx.browser.installed():
            raise BrowserUnavailableError(
                "this site needs the browser, but this image was built without it"
            )
        html, final_url = await ctx.browser.render(
            url, respect_robots=ctx.respect_robots, wait_selector=ctx.wait_selector
        )
        return Page(html, final_url, rendered=True)
    response = await ctx.client.get_text(url, respect_robots=ctx.respect_robots)
    return Page(response.text, response.url, rendered=False)


async def fetch_postings(cfg: AdapterConfig, url: str, ctx: FetchContext) -> FetchOutcome:
    adapter = ADAPTERS.get(cfg.adapter)
    if adapter is None:
        raise FetchError(f"unknown adapter {cfg.adapter!r}")
    if adapter in API_ADAPTERS:
        raw = await adapter.fetch(cfg, ctx)  # type: ignore[union-attr]
        return FetchOutcome(cfg, finalize(cfg.adapter, raw, url))

    page_adapter: PageAdapter = adapter  # type: ignore[assignment]
    if ctx.render_mode == "browser":
        page = await load_page(url, ctx, render=True)
        return _parse(page_adapter, cfg, page)

    page = await load_page(url, ctx, render=False)
    outcome = _parse(page_adapter, cfg, page)
    if outcome.postings or ctx.render_mode == "static" or not browser_usable(ctx):
        return outcome
    log.info("No postings in static HTML of %s; trying the browser", url)
    return _parse(page_adapter, cfg, await load_page(url, ctx, render=True))


def _parse(adapter: PageAdapter, cfg: AdapterConfig, page: Page) -> FetchOutcome:
    raw = adapter.parse(cfg, page.html, page.url)
    return FetchOutcome(cfg, finalize(cfg.adapter, raw, page.url), rendered=page.rendered)

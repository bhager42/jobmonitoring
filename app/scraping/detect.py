"""Work out which adapter fits a careers page URL, and preview what it extracts."""

import logging

from app.scraping.adapters import API_ADAPTERS
from app.scraping.adapters.css_selector import NAME as CSS_NAME
from app.scraping.adapters.heuristic import HeuristicAdapter, job_list_link
from app.scraping.adapters.jsonld import JsonLdAdapter
from app.scraping.base import AdapterConfig, BlockedError, FetchContext, FetchError
from app.scraping.fetch import (
    FetchOutcome,
    Page,
    browser_usable,
    fetch_postings,
    finalize,
    load_page,
)

log = logging.getLogger(__name__)

PAGE_DETECTORS = (JsonLdAdapter(), HeuristicAdapter())


class NothingFoundError(FetchError):
    """No adapter found any postings on the page."""


async def detect(url: str, ctx: FetchContext, *, css: dict[str, str] | None = None) -> FetchOutcome:
    """Return the first adapter that yields postings, with those postings as a preview."""
    await ctx.client.check_url(url)

    if css and css.get("item_selector"):
        params = {k: v for k, v in css.items() if v}
        return await fetch_postings(AdapterConfig(CSS_NAME, params), url, ctx)

    # 1. The URL itself is an ATS board.
    for adapter in API_ADAPTERS:
        if cfg := adapter.detect(url, None):
            return await fetch_postings(cfg, url, ctx)

    # 2. The page embeds or links to an ATS board, or carries postings itself.
    static_error: FetchError | None = None
    suggestion: str | None = None
    if ctx.render_mode != "browser":
        try:
            page = await load_page(url, ctx, render=False)
        except BlockedError:
            raise  # never try to get around a refusal
        except FetchError as exc:
            static_error = exc
        else:
            if outcome := await _from_page(page, ctx):
                return outcome
            suggestion = job_list_link(page.html, page.url)

    # 3. Try again with JavaScript.
    if ctx.render_mode != "static" and browser_usable(ctx):
        page = await load_page(url, ctx, render=True)
        if outcome := await _from_page(page, ctx):
            return outcome
        suggestion = suggestion or job_list_link(page.html, page.url)

    if static_error is not None:
        raise static_error
    hint = "" if browser_usable(ctx) else " This image has no browser, so JavaScript was not run."
    if suggestion:
        where = f"This page links to a list of jobs at {suggestion}: try that address."
    else:
        where = "Try the URL of the page that lists the jobs."
    raise NothingFoundError(
        f"No job postings found on that page. {where} You can also set CSS selectors under "
        "Advanced." + hint
    )


async def _from_page(page: Page, ctx: FetchContext) -> FetchOutcome | None:
    for adapter in API_ADAPTERS:
        if cfg := adapter.detect(page.url, page.html):
            log.info("Found %s board embedded in %s", cfg.adapter, page.url)
            return await fetch_postings(cfg, page.url, ctx)
    for page_adapter in PAGE_DETECTORS:
        cfg = page_adapter.detect(page.url, page.html)
        if cfg is None:
            continue
        postings = finalize(cfg.adapter, page_adapter.parse(cfg, page.html, page.url), page.url)
        if postings:
            return FetchOutcome(cfg, postings, rendered=page.rendered)
    return None

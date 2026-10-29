"""Lever postings: https://api.lever.co/v0/postings/{company}?mode=json"""

import re
from typing import cast
from urllib.parse import urlsplit

from app.scraping.adapters._util import (
    JSON,
    SLUG,
    dicts,
    get_id,
    get_str,
    parse_datetime,
    path_segments,
)
from app.scraping.base import (
    MAX_PAGES,
    MAX_POSTINGS,
    AdapterConfig,
    FetchContext,
    FetchError,
    RawPosting,
    looks_remote,
)

NAME = "lever"
PAGE_SIZE = 100
API_HOSTS = {"jobs.lever.co": "api.lever.co", "jobs.eu.lever.co": "api.eu.lever.co"}
LINK_RE = re.compile(rf"(jobs(?:\.eu)?\.lever\.co)/({SLUG})")


class LeverAdapter:
    name = NAME

    def detect(self, url: str, html: str | None) -> AdapterConfig | None:
        host = (urlsplit(url).hostname or "").lower()
        segments = path_segments(url)
        if host in API_HOSTS and segments:
            return AdapterConfig(NAME, {"company": segments[0], "api_host": API_HOSTS[host]})
        if html and (match := LINK_RE.search(html)):
            return AdapterConfig(
                NAME, {"company": match.group(2), "api_host": API_HOSTS[match.group(1)]}
            )
        return None

    async def fetch(self, cfg: AdapterConfig, ctx: FetchContext) -> list[RawPosting]:
        api_host = cfg.params.get("api_host", "api.lever.co")
        base = f"https://{api_host}/v0/postings/{cfg.params['company']}"
        postings: list[RawPosting] = []
        for page in range(MAX_PAGES):
            data: object = await ctx.client.get_json(
                f"{base}?mode=json&limit={PAGE_SIZE}&skip={page * PAGE_SIZE}"
            )
            if not isinstance(data, list):
                raise FetchError("Lever response is not a list of postings")
            items = dicts(cast(object, data))
            postings += [p for item in items if (p := _posting(item)) is not None]
            if len(items) < PAGE_SIZE or len(postings) >= MAX_POSTINGS:
                break
        return postings


def _posting(item: JSON) -> RawPosting | None:
    title = get_str(item, "text")
    url = get_str(item, "hostedUrl")
    if not title or not url:
        return None
    location = get_str(item, "categories", "location")
    workplace = (get_str(item, "workplaceType") or "").lower()
    remote = True if workplace == "remote" else (False if workplace == "on-site" else None)
    return RawPosting(
        title=title,
        url=url,
        external_id=get_id(item),
        location=location,
        department=get_str(item, "categories", "team") or get_str(item, "categories", "department"),
        employment_type=get_str(item, "categories", "commitment"),
        remote=remote if remote is not None else looks_remote(location),
        posted_at=parse_datetime(item.get("createdAt")),
        description_text=get_str(item, "descriptionPlain"),
    )

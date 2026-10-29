"""SmartRecruiters: https://api.smartrecruiters.com/v1/companies/{company}/postings"""

import re

from app.scraping.adapters._util import (
    JSON,
    SLUG,
    as_dict,
    dicts,
    get_id,
    get_str,
    host,
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

NAME = "smartrecruiters"
HOSTS = frozenset({"jobs.smartrecruiters.com", "careers.smartrecruiters.com"})
LINK_RE = re.compile(rf"(?:jobs|careers)\.smartrecruiters\.com/({SLUG})")
PAGE_SIZE = 100


class SmartRecruitersAdapter:
    name = NAME

    def detect(self, url: str, html: str | None) -> AdapterConfig | None:
        segments = path_segments(url)
        if host(url) in HOSTS and segments:
            return AdapterConfig(NAME, {"company": segments[0]})
        if html and (match := LINK_RE.search(html)):
            return AdapterConfig(NAME, {"company": match.group(1)})
        return None

    async def fetch(self, cfg: AdapterConfig, ctx: FetchContext) -> list[RawPosting]:
        company = cfg.params["company"]
        postings: list[RawPosting] = []
        offset = 0
        for _ in range(MAX_PAGES):
            data = as_dict(
                await ctx.client.get_json(
                    f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
                    f"?limit={PAGE_SIZE}&offset={offset}"
                )
            )
            if data is None or not isinstance(data.get("content"), list):
                raise FetchError("SmartRecruiters response has no postings")
            items = dicts(data["content"])
            postings += [p for item in items if (p := _posting(item, company)) is not None]
            offset += len(items)
            total = data.get("totalFound")
            if not items or (isinstance(total, int) and offset >= total):
                break
            if len(postings) >= MAX_POSTINGS:
                break
        return postings


def _posting(item: JSON, company: str) -> RawPosting | None:
    title = get_str(item, "name")
    posting_id = get_id(item)
    if not title or not posting_id:
        return None
    location_obj = as_dict(item.get("location")) or {}
    location = get_str(location_obj, "fullLocation") or (
        ", ".join(
            part for key in ("city", "region", "country") if (part := get_str(location_obj, key))
        )
        or None
    )
    remote = True if location_obj.get("remote") is True else looks_remote(location)
    return RawPosting(
        title=title,
        url=f"https://jobs.smartrecruiters.com/{company}/{posting_id}",
        external_id=posting_id,
        location=location,
        department=get_str(item, "department", "label"),
        employment_type=get_str(item, "typeOfEmployment", "label"),
        remote=remote,
        posted_at=parse_datetime(item.get("releasedDate")),
    )

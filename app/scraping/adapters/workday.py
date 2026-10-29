"""Workday career sites: POST https://{host}/wday/cxs/{tenant}/{site}/jobs"""

import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from app.db.models import utcnow
from app.scraping.adapters._util import JSON, as_dict, as_list, dicts, get_str
from app.scraping.base import (
    MAX_PAGES,
    MAX_POSTINGS,
    AdapterConfig,
    FetchContext,
    FetchError,
    RawPosting,
    looks_remote,
)

NAME = "workday"
PAGE_SIZE = 20  # Workday rejects larger pages
TENANT_HOST = re.compile(r"^([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com$")
SITE_HOST = re.compile(r"^wd\d+\.myworkdaysite\.com$")
LOCALE = re.compile(r"^[a-z]{2}(?:-[A-Z]{2})?$")
LINK_RE = re.compile(r"https://[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/[A-Za-z0-9_/-]+")
POSTED_DAYS = re.compile(r"(\d+)\+?\s+days?\s+ago", re.IGNORECASE)


def parse_config(url: str) -> AdapterConfig | None:
    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower()
    segments = [s for s in parts.path.split("/") if s]
    if match := TENANT_HOST.match(hostname):
        tenant = match.group(1)
    elif SITE_HOST.match(hostname) and len(segments) >= 3 and segments[0] == "recruiting":
        tenant = segments[1]
        segments = segments[2:]
    else:
        return None
    locale = segments[0] if segments and LOCALE.match(segments[0]) else None
    if locale:
        segments = segments[1:]
    if not segments or segments[0] in ("wday", "job"):
        return None
    site = segments[0]
    prefix = f"/recruiting/{tenant}" if SITE_HOST.match(hostname) else ""
    base = f"https://{hostname}{prefix}/{locale + '/' if locale else ''}{site}"
    return AdapterConfig(NAME, {"host": hostname, "tenant": tenant, "site": site, "base_url": base})


class WorkdayAdapter:
    name = NAME

    def detect(self, url: str, html: str | None) -> AdapterConfig | None:
        if cfg := parse_config(url):
            return cfg
        if html:
            for match in LINK_RE.finditer(html):
                if cfg := parse_config(match.group(0)):
                    return cfg
        return None

    async def fetch(self, cfg: AdapterConfig, ctx: FetchContext) -> list[RawPosting]:
        p = cfg.params
        endpoint = f"https://{p['host']}/wday/cxs/{p['tenant']}/{p['site']}/jobs"
        postings: list[RawPosting] = []
        total: int | None = None
        offset = 0
        for _ in range(MAX_PAGES):
            data = as_dict(
                await ctx.client.post_json(
                    endpoint,
                    {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": ""},
                )
            )
            if data is None or not isinstance(data.get("jobPostings"), list):
                raise FetchError("Workday response has no job postings")
            items = dicts(data["jobPostings"])
            # Workday only reports the total on the first page; later pages say 0.
            if total is None and isinstance(data.get("total"), int):
                total = data["total"]
            postings += [x for item in items if (x := _posting(item, p["base_url"])) is not None]
            offset += len(items)
            if not items or (total is not None and offset >= total):
                break
            if len(postings) >= MAX_POSTINGS:
                break
        return postings


def _posting(item: JSON, base_url: str) -> RawPosting | None:
    title = get_str(item, "title")
    path = get_str(item, "externalPath")
    if not title or not path:
        return None
    bullets = [b for b in as_list(item.get("bulletFields")) if isinstance(b, str) and b.strip()]
    location = get_str(item, "locationsText")
    return RawPosting(
        title=title,
        url=base_url + path,
        external_id=bullets[0] if bullets else path,
        location=location,
        remote=looks_remote(location, get_str(item, "remoteType")),
        posted_at=parse_posted_on(get_str(item, "postedOn")),
    )


def parse_posted_on(text: str | None) -> datetime | None:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago'. '30+ Days Ago' is too vague."""
    if not text:
        return None
    lowered = text.lower()
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    if "today" in lowered:
        return today
    if "yesterday" in lowered:
        return today - timedelta(days=1)
    match = POSTED_DAYS.search(lowered)
    if match and "+" not in match.group(0):
        return today - timedelta(days=int(match.group(1)))
    return None

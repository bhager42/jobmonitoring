"""Greenhouse job boards: https://boards-api.greenhouse.io/v1/boards/{token}/jobs"""

import re
from urllib.parse import parse_qs, urlsplit

from app.scraping.adapters._util import (
    JSON,
    SLUG,
    as_dict,
    dicts,
    get_id,
    get_str,
    html_to_text,
    parse_datetime,
)
from app.scraping.base import AdapterConfig, FetchContext, FetchError, RawPosting, looks_remote

NAME = "greenhouse"
HOSTS = frozenset({"boards.greenhouse.io", "job-boards.greenhouse.io"})
NOT_TOKENS = frozenset({"embed", "v1"})
EMBED_RE = re.compile(rf"boards\.greenhouse\.io/embed/job_board(?:/js)?\?for=({SLUG})")
LINK_RE = re.compile(rf"(?:job-)?boards\.greenhouse\.io/({SLUG})")


class GreenhouseAdapter:
    name = NAME

    def detect(self, url: str, html: str | None) -> AdapterConfig | None:
        parts = urlsplit(url)
        if (parts.hostname or "").lower() in HOSTS:
            token = parse_qs(parts.query).get("for", [None])[0]
            if token is None:
                segments = [s for s in parts.path.split("/") if s]
                token = segments[0] if segments and segments[0] not in NOT_TOKENS else None
            if token:
                return AdapterConfig(NAME, {"token": token})
        if html:
            match = EMBED_RE.search(html)
            if match:
                return AdapterConfig(NAME, {"token": match.group(1)})
            for match in LINK_RE.finditer(html):
                if match.group(1) not in NOT_TOKENS:
                    return AdapterConfig(NAME, {"token": match.group(1)})
        return None

    async def fetch(self, cfg: AdapterConfig, ctx: FetchContext) -> list[RawPosting]:
        token = cfg.params["token"]
        data = await ctx.client.get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        )
        body = as_dict(data)
        if body is None or not isinstance(body.get("jobs"), list):
            raise FetchError("Greenhouse response has no job list")
        return [p for job in dicts(body["jobs"]) if (p := _posting(job)) is not None]


def _posting(job: JSON) -> RawPosting | None:
    title = get_str(job, "title")
    url = get_str(job, "absolute_url")
    if not title or not url:
        return None
    location = get_str(job, "location", "name")
    departments = dicts(job.get("departments"))
    department = get_str(departments[0], "name") if departments else None
    return RawPosting(
        title=title,
        url=url,
        external_id=get_id(job),
        location=location,
        department=department,
        remote=looks_remote(location),
        posted_at=parse_datetime(job.get("first_published")),
        description_text=html_to_text(job.get("content")),
    )

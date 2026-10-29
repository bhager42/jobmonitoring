"""Ashby job boards: https://api.ashbyhq.com/posting-api/job-board/{org}"""

import re

from app.scraping.adapters._util import (
    JSON,
    SLUG,
    as_dict,
    dicts,
    get_id,
    get_str,
    host,
    humanize,
    parse_datetime,
    path_segments,
)
from app.scraping.base import AdapterConfig, FetchContext, FetchError, RawPosting, looks_remote

NAME = "ashby"
HOST = "jobs.ashbyhq.com"
LINK_RE = re.compile(rf"jobs\.ashbyhq\.com/({SLUG})")


class AshbyAdapter:
    name = NAME

    def detect(self, url: str, html: str | None) -> AdapterConfig | None:
        segments = path_segments(url)
        if host(url) == HOST and segments:
            return AdapterConfig(NAME, {"org": segments[0]})
        if html and (match := LINK_RE.search(html)):
            return AdapterConfig(NAME, {"org": match.group(1)})
        return None

    async def fetch(self, cfg: AdapterConfig, ctx: FetchContext) -> list[RawPosting]:
        data = await ctx.client.get_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{cfg.params['org']}"
            "?includeCompensation=true"
        )
        body = as_dict(data)
        if body is None or not isinstance(body.get("jobs"), list):
            raise FetchError("Ashby response has no job list")
        return [p for job in dicts(body["jobs"]) if (p := _posting(job)) is not None]


def _posting(job: JSON) -> RawPosting | None:
    if job.get("isListed") is False:
        return None
    title = get_str(job, "title")
    url = get_str(job, "jobUrl")
    if not title or not url:
        return None
    locations = [get_str(job, "location")] + [
        get_str(extra, "location") for extra in dicts(job.get("secondaryLocations"))
    ]
    location = "; ".join(loc for loc in locations if loc) or None
    workplace = (get_str(job, "workplaceType") or "").lower()
    if job.get("isRemote") is True or workplace == "remote":
        remote: bool | None = True
    elif workplace == "onsite":
        remote = False
    else:
        remote = looks_remote(location)
    return RawPosting(
        title=title,
        url=url,
        external_id=get_id(job),
        location=location,
        department=get_str(job, "department") or get_str(job, "team"),
        employment_type=humanize(get_str(job, "employmentType")),
        remote=remote,
        posted_at=parse_datetime(job.get("publishedAt")),
        description_text=get_str(job, "descriptionPlain"),
    )

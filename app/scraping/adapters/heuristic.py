"""Last resort: links that look like individual job postings."""

import re
from urllib.parse import parse_qs, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from app.scraping.base import AdapterConfig, RawPosting, clean_text
from app.scraping.urls import canonical_url

NAME = "heuristic"
JOB_LINK = re.compile(
    r"(/jobs?/|/careers?/.+|/positions?/|/openings?/|/vacanc|/requisitions?/|/job-|"
    r"/postings?/|/roles?/|/opportunit|[?&](gh_jid|jobid|job_id|jid|reqid)=)",
    re.IGNORECASE,
)
ID_PARAMS = ("gh_jid", "jobid", "job_id", "jid", "reqid")
GENERIC_TEXT = frozenset(
    {
        "jobs", "careers", "career", "all jobs", "view all jobs", "see all jobs", "view jobs",
        "open positions", "open roles", "openings", "apply", "apply now", "learn more",
        "read more", "more", "search jobs", "job search", "careers home", "explore jobs",
        "back", "next", "previous", "home", "join us", "work with us", "see open roles",
        "view openings", "view all openings", "browse jobs", "job alerts",
    }
)  # fmt: skip
SOCIAL_HOSTS = (
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "tiktok.com", "glassdoor.com",
)  # fmt: skip
MIN_TITLE = 4
MAX_TITLE = 200


def _external_id(url: str) -> str | None:
    query = parse_qs(urlsplit(url).query)
    for key in ID_PARAMS:
        if values := query.get(key):
            return values[0]
    return None


def _is_social(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower()
    return any(hostname == h or hostname.endswith("." + h) for h in SOCIAL_HOSTS)


def find_job_links(html: str, base_url: str) -> list[RawPosting]:
    page = canonical_url(base_url)
    seen: set[str] = set()
    postings: list[RawPosting] = []
    for anchor in LexborHTMLParser(html).css("a[href]"):
        href = (anchor.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href)
        if not url.startswith(("http://", "https://")) or _is_social(url):
            continue
        canonical = canonical_url(url)
        if canonical == page or canonical in seen:
            continue
        if not JOB_LINK.search(urlsplit(url).path + "?" + urlsplit(url).query):
            continue
        title = clean_text(anchor.text(deep=True, separator=" "))
        if not title or not (MIN_TITLE <= len(title) <= MAX_TITLE):
            continue
        if title.lower().strip(" .:>\u203a\u00bb\u2192") in GENERIC_TEXT:
            continue
        seen.add(canonical)
        postings.append(RawPosting(title=title, url=url, external_id=_external_id(url)))
    return postings


class HeuristicAdapter:
    name = NAME

    def detect(self, url: str, html: str | None) -> AdapterConfig | None:
        return AdapterConfig(NAME) if html and find_job_links(html, url) else None

    def parse(self, cfg: AdapterConfig, html: str, base_url: str) -> list[RawPosting]:
        return find_job_links(html, base_url)

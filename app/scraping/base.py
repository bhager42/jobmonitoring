"""Shared types for fetching job postings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urljoin

if TYPE_CHECKING:
    from app.scraping.browser import BrowserManager
    from app.scraping.http import PoliteClient

MAX_TITLE = 500
MAX_DESCRIPTION = 20_000
MAX_PAGES = 50
MAX_POSTINGS = 2_000

RenderMode = Literal["static", "browser", "auto"]


class FetchError(Exception):
    """A site could not be fetched or parsed."""


class BlockedError(FetchError):
    """The site refused us (bot protection, 401/403, or robots.txt)."""


class BrowserUnavailableError(FetchError):
    """Rendering was required but this image has no browser."""


@dataclass(frozen=True)
class RawPosting:
    title: str
    url: str
    external_id: str | None = None
    location: str | None = None
    department: str | None = None
    employment_type: str | None = None
    remote: bool | None = None
    posted_at: datetime | None = None
    description_text: str | None = None


@dataclass(frozen=True)
class AdapterConfig:
    """Which adapter to use, and its parameters (stored as monitor.adapter and .adapter_config)."""

    adapter: str
    params: dict[str, str] = field(default_factory=dict[str, str])

    def to_json(self) -> dict[str, Any]:
        return dict(self.params)


@dataclass
class FetchContext:
    client: PoliteClient
    browser: BrowserManager | None = None
    respect_robots: bool = True
    render_mode: RenderMode = "auto"
    wait_selector: str | None = None


class ApiAdapter(Protocol):
    """Reads an ATS's public JSON API. Ignores render mode and robots.txt (documented APIs)."""

    name: str

    def detect(self, url: str, html: str | None) -> AdapterConfig | None: ...

    async def fetch(self, cfg: AdapterConfig, ctx: FetchContext) -> list[RawPosting]: ...


class PageAdapter(Protocol):
    """Extracts postings from a page's HTML (fetched statically or rendered in the browser)."""

    name: str

    def detect(self, url: str, html: str | None) -> AdapterConfig | None: ...

    def parse(self, cfg: AdapterConfig, html: str, base_url: str) -> list[RawPosting]: ...


_WS = re.compile(r"\s+")


def clean_text(value: object, limit: int | None = None) -> str | None:
    if not isinstance(value, str):
        return None
    text = _WS.sub(" ", value).strip()
    if not text:
        return None
    return text[:limit] if limit else text


def looks_remote(*values: str | None) -> bool | None:
    joined = " ".join(v for v in values if v).lower()
    if not joined:
        return None
    return (
        True if re.search(r"\b(remote|anywhere|work from home|wfh|telecommute)\b", joined) else None
    )


def normalize(posting: RawPosting, base_url: str) -> RawPosting | None:
    """Clean whitespace, resolve the URL, enforce length limits. None if unusable."""
    title = clean_text(posting.title, MAX_TITLE)
    url = urljoin(base_url, posting.url.strip()) if posting.url else ""
    if not title or not url.startswith(("http://", "https://")):
        return None
    return replace(
        posting,
        title=title,
        url=url,
        external_id=clean_text(posting.external_id, 200),
        location=clean_text(posting.location, 500),
        department=clean_text(posting.department, 200),
        employment_type=clean_text(posting.employment_type, 100),
        description_text=clean_text(posting.description_text, MAX_DESCRIPTION),
    )

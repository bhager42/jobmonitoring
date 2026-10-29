"""Short-lived server-side store for the detection preview shown while editing a monitor."""

import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from app.scraping.base import AdapterConfig, RawPosting

TTL_SECONDS = 30 * 60
MAX_ENTRIES = 50


@dataclass(frozen=True)
class Preview:
    url: str
    css: dict[str, str]
    config: AdapterConfig
    postings: list[RawPosting]
    rendered: bool
    created: float = field(default_factory=time.monotonic)

    def same_source(self, url: str, css: dict[str, str]) -> bool:
        return self.url == url and self.css == css


class PreviewCache:
    def __init__(self, ttl: float = TTL_SECONDS, max_entries: int = MAX_ENTRIES) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._entries: OrderedDict[str, Preview] = OrderedDict()

    def put(self, preview: Preview) -> str:
        self._expire()
        token = secrets.token_urlsafe(16)
        self._entries[token] = preview
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return token

    def get(self, token: str | None) -> Preview | None:
        if not token:
            return None
        self._expire()
        return self._entries.get(token)

    def _expire(self) -> None:
        cutoff = time.monotonic() - self.ttl
        for token in [t for t, p in self._entries.items() if p.created < cutoff]:
            del self._entries[token]

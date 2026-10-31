"""Polite HTTP client: identifies itself, honours robots.txt, spaces out requests per host,
retries transient failures, caps response sizes, and refuses non-public addresses."""

import asyncio
import email.utils
import logging
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app import __version__
from app.scraping.base import BlockedError, FetchError
from app.security.ssrf import (
    GuardedNetworkBackend,
    Resolver,
    SSRFError,
    system_resolver,
    validate_url,
)

log = logging.getLogger(__name__)

USER_AGENT = f"JobMonitor/{__version__} (+self-hosted; personal job alerts)"
ROBOTS_AGENT = "JobMonitor"
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_ROBOTS_BYTES = 512 * 1024
MAX_REDIRECTS = 5
MAX_RETRY_AFTER = 60.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
ROBOTS_TTL = 24 * 3600.0
ROBOTS_ERROR_TTL = 3600.0
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


@dataclass(frozen=True)
class FetchResponse:
    url: str
    status: int
    headers: httpx.Headers
    content: bytes

    def _decoded(self) -> httpx.Response:
        # `content` was already decompressed while streaming. Drop the headers that describe the
        # compressed form, or httpx would try to decompress it again (and fail on real sites).
        headers = httpx.Headers(
            [
                (key, value)
                for key, value in self.headers.multi_items()
                if key.lower() not in ("content-encoding", "content-length")
            ]
        )
        return httpx.Response(self.status, headers=headers, content=self.content)

    @property
    def text(self) -> str:
        return self._decoded().text

    def json(self) -> Any:
        try:
            return self._decoded().json()
        except ValueError as exc:
            raise FetchError(f"{self.url} did not return valid JSON") from exc


@dataclass
class _HostState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_request: float | None = None


@dataclass
class _Robots:
    parser: RobotFileParser | None  # None means "allow everything"
    disallow_all: bool
    expires: float


def build_transport(
    *,
    allow_private: bool,
    resolver: Resolver,
    proxy: str | None = None,
    ca_bundle: str | None = None,
) -> httpx.AsyncBaseTransport:
    verify: ssl.SSLContext | bool = (
        ssl.create_default_context(cafile=ca_bundle) if ca_bundle else True
    )
    if proxy:
        # Connections go to the proxy, so only the pre-flight DNS check protects destinations.
        return httpx.AsyncHTTPTransport(proxy=proxy, verify=verify, http2=True)
    transport = httpx.AsyncHTTPTransport(verify=verify, http2=True)
    # httpx has no public hook for the network backend; replace the pool's. A test guards this.
    transport._pool._network_backend = GuardedNetworkBackend(  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
        allow_private=allow_private, resolver=resolver
    )
    return transport


def _retry_after(response: httpx.Response, now: float) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    if value.strip().isdigit():
        return min(float(value), MAX_RETRY_AFTER)
    try:
        when = email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None
    return min(max(when - now, 0.0), MAX_RETRY_AFTER)


def _is_bot_challenge(response: httpx.Response, body: bytes) -> bool:
    if response.headers.get("cf-mitigated") == "challenge":
        return True
    return response.status_code in (403, 503) and b"challenge-platform" in body[:200_000]


class PoliteClient:
    def __init__(
        self,
        *,
        allow_private: bool = False,
        min_host_interval: float = 2.0,
        max_retries: int = 3,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        ca_bundle: str | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self.allow_private = allow_private
        self.min_host_interval = min_host_interval
        self.max_retries = max_retries
        self.resolver = resolver
        self._sleep = sleep
        self._clock = clock
        self._hosts: dict[str, _HostState] = {}
        self._robots: dict[str, _Robots] = {}
        self._client = httpx.AsyncClient(
            transport=transport
            or build_transport(
                allow_private=allow_private, resolver=resolver, proxy=proxy, ca_bundle=ca_bundle
            ),
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.8",
            },
            timeout=TIMEOUT,
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "PoliteClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # --- public API -------------------------------------------------------------------------

    async def get_text(self, url: str, *, respect_robots: bool) -> FetchResponse:
        return await self.request(
            "GET",
            url,
            respect_robots=respect_robots,
            headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"},
        )

    async def get_json(self, url: str, *, respect_robots: bool = False) -> Any:
        response = await self.request(
            "GET", url, respect_robots=respect_robots, headers={"Accept": "application/json"}
        )
        return response.json()

    async def post_json(self, url: str, body: Any, *, respect_robots: bool = False) -> Any:
        response = await self.request(
            "POST",
            url,
            respect_robots=respect_robots,
            json_body=body,
            headers={"Accept": "application/json"},
        )
        return response.json()

    async def request(
        self,
        method: str,
        url: str,
        *,
        respect_robots: bool,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> FetchResponse:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            await self.check_url(current)
            if respect_robots and not await self.allowed_by_robots(current):
                raise BlockedError(f"robots.txt disallows fetching {current}")
            response, body = await self._send_with_retries(method, current, headers, json_body)
            if response.is_redirect and "location" in response.headers:
                current = urljoin(current, response.headers["location"])
                if response.status_code in (301, 302, 303) and method == "POST":
                    method, json_body = "GET", None
                continue
            self._raise_for_status(current, response, body)
            return FetchResponse(current, response.status_code, response.headers, body)
        raise FetchError(f"too many redirects starting at {url}")

    async def allowed_by_robots(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        entry = self._robots.get(origin)
        if entry is None or entry.expires <= self._clock():
            entry = await self._load_robots(origin)
            self._robots[origin] = entry
        if entry.disallow_all:
            return False
        return entry.parser is None or entry.parser.can_fetch(ROBOTS_AGENT, url)

    # --- internals --------------------------------------------------------------------------

    async def check_url(self, url: str) -> None:
        try:
            await validate_url(url, allow_private=self.allow_private, resolver=self.resolver)
        except SSRFError as exc:
            raise BlockedError(f"refusing to fetch {url}: {exc}") from exc

    async def _load_robots(self, origin: str) -> _Robots:
        robots_url = origin + "/robots.txt"
        now = self._clock()
        try:
            await self.check_url(robots_url)
            response, body = await self._send(
                "GET", robots_url, {"Accept": "text/plain"}, None, MAX_ROBOTS_BYTES
            )
        except (FetchError, httpx.HTTPError) as exc:
            log.info("robots.txt unreachable at %s (%s); treating as disallow-all", origin, exc)
            return _Robots(None, True, now + ROBOTS_ERROR_TTL)
        if response.status_code >= 500:
            return _Robots(None, True, now + ROBOTS_ERROR_TTL)
        if response.status_code >= 400 or response.is_redirect:
            return _Robots(None, False, now + ROBOTS_TTL)  # no robots.txt: everything allowed
        parser = RobotFileParser()
        parser.parse(body.decode("utf-8", errors="replace").splitlines())
        return _Robots(parser, False, now + ROBOTS_TTL)

    async def _send_with_retries(
        self, method: str, url: str, headers: dict[str, str] | None, json_body: Any
    ) -> tuple[httpx.Response, bytes]:
        attempt = 0
        while True:
            try:
                response, body = await self._send(
                    method, url, headers, json_body, MAX_RESPONSE_BYTES
                )
            except httpx.TransportError as exc:
                if "non-public address" in str(exc):
                    raise BlockedError(f"refusing to fetch {url}: {exc}") from exc
                if attempt >= self.max_retries:
                    raise FetchError(f"could not reach {url}: {exc!r}") from exc
                delay = 2.0**attempt
            else:
                if response.status_code not in RETRY_STATUSES or _is_bot_challenge(response, body):
                    return response, body
                if attempt >= self.max_retries:
                    return response, body
                delay = _retry_after(response, time.time())
                if delay is None:
                    delay = 2.0**attempt
            attempt += 1
            log.info("Retrying %s %s in %.1fs (attempt %d)", method, url, delay, attempt + 1)
            await self._sleep(delay)

    async def _send(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None,
        json_body: Any,
        limit: int,
    ) -> tuple[httpx.Response, bytes]:
        host = urlsplit(url).netloc.lower()
        state = self._hosts.setdefault(host, _HostState())
        async with state.lock:
            if state.last_request is not None:
                wait = state.last_request + self.min_host_interval - self._clock()
                if wait > 0:
                    await self._sleep(wait)
            try:
                request = self._client.build_request(method, url, headers=headers, json=json_body)
                response = await self._client.send(request, stream=True)
                try:
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > limit:
                        raise FetchError(f"{url} is larger than {limit // 1024 // 1024 or 1} MB")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise FetchError(
                                f"{url} is larger than {limit // 1024 // 1024 or 1} MB"
                            )
                        chunks.append(chunk)
                finally:
                    await response.aclose()
            finally:
                state.last_request = self._clock()
        return response, b"".join(chunks)

    @staticmethod
    def _raise_for_status(url: str, response: httpx.Response, body: bytes) -> None:
        status = response.status_code
        if _is_bot_challenge(response, body):
            raise BlockedError(f"{url} is behind bot protection (HTTP {status})")
        if status in (401, 403):
            raise BlockedError(f"{url} refused access (HTTP {status})")
        if status == 429:
            raise BlockedError(f"{url} is rate limiting requests (HTTP 429)")
        if status >= 400:
            raise FetchError(f"{url} returned HTTP {status}")

"""Optional headless Chromium (Playwright) for pages that need JavaScript.

One shared browser, launched on first use and closed after it has been idle for a while.
Every request the page makes passes the SSRF check; images, media and fonts are skipped.
"""

import asyncio
import contextlib
import importlib.util
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from app.scraping.base import BlockedError, BrowserUnavailableError, FetchError
from app.scraping.http import USER_AGENT, PoliteClient
from app.security.ssrf import SSRFError, validate_url

if TYPE_CHECKING:
    from playwright.async_api import Browser, Playwright, Route

log = logging.getLogger(__name__)

SKIPPED_RESOURCES = frozenset({"image", "media", "font"})
LAUNCH_ARGS = (
    "--disable-dev-shm-usage",  # /dev/shm is small in containers
    # No traffic of Chromium's own: updates, metrics, safe browsing, sync, etc.
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-default-browser-check",
    "--no-first-run",
    "--no-pings",
    "--safebrowsing-disable-auto-update",
)


def playwright_installed() -> bool:
    return importlib.util.find_spec("playwright") is not None


class BrowserManager:
    def __init__(
        self,
        client: PoliteClient,
        *,
        idle_timeout: float = 300.0,
        max_pages: int = 2,
        page_timeout: float = 30.0,
        executable_path: str | None = None,
    ) -> None:
        self.client = client
        self.idle_timeout = idle_timeout
        self.page_timeout = page_timeout
        self.executable_path = executable_path
        self._pages = asyncio.Semaphore(max_pages)
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._active = 0

    @staticmethod
    def installed() -> bool:
        return playwright_installed()

    async def render(
        self, url: str, *, respect_robots: bool, wait_selector: str | None = None
    ) -> tuple[str, str]:
        """Load `url` in Chromium and return (html, final_url)."""
        await self.client.check_url(url)
        if respect_robots and not await self.client.allowed_by_robots(url):
            raise BlockedError(f"robots.txt disallows fetching {url}")
        async with self._pages:
            self._active += 1
            try:
                browser = await self._ensure_browser()
                context = await browser.new_context(
                    user_agent=USER_AGENT, service_workers="block", accept_downloads=False
                )
                try:
                    await context.route("**/*", self._guard)
                    page = await context.new_page()
                    timeout_ms = self.page_timeout * 1000
                    try:
                        await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                        if wait_selector:
                            await page.wait_for_selector(wait_selector, timeout=timeout_ms)
                    except Exception as exc:
                        if "Timeout" not in type(exc).__name__:
                            raise FetchError(f"browser could not load {url}: {exc}") from exc
                        log.info("Timed out waiting for %s; using what has loaded", url)
                    final_url = page.url
                    await self.client.check_url(final_url)
                    return await page.content(), final_url
                finally:
                    await context.close()
            finally:
                self._active -= 1
                self._schedule_idle_close()

    async def close(self) -> None:
        async with self._lock:
            if self._browser is not None:
                with contextlib.suppress(Exception):
                    await self._browser.close()
            if self._playwright is not None:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()
            self._browser = None
            self._playwright = None

    async def _ensure_browser(self) -> "Browser":
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if not playwright_installed():
                raise BrowserUnavailableError("this image was built without the browser")
            from playwright.async_api import async_playwright

            try:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True, args=list(LAUNCH_ARGS), executable_path=self.executable_path
                )
            except Exception as exc:
                if self._playwright is not None:
                    with contextlib.suppress(Exception):
                        await self._playwright.stop()
                    self._playwright = None
                raise BrowserUnavailableError(f"could not start Chromium: {exc}") from exc
            log.info("Started headless Chromium")
            return self._browser

    async def _guard(self, route: "Route") -> None:
        request = route.request
        scheme = urlsplit(request.url).scheme
        if request.resource_type in SKIPPED_RESOURCES:
            await route.abort("blockedbyclient")
            return
        if scheme in ("data", "blob"):
            await route.continue_()
            return
        try:
            await validate_url(
                request.url, allow_private=self.client.allow_private, resolver=self.client.resolver
            )
        except SSRFError as exc:
            log.warning("Browser request blocked: %s", exc)
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    def _schedule_idle_close(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
        self._idle_task = asyncio.get_running_loop().create_task(self._close_when_idle())

    async def _close_when_idle(self) -> None:
        await asyncio.sleep(self.idle_timeout)
        if self._active == 0:
            log.info("Closing idle Chromium")
            await self.close()

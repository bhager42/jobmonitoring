"""Real headless Chromium tests. Skipped when Playwright or Chromium is unavailable."""

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.scraping.browser import BrowserManager, playwright_installed
from app.scraping.detect import detect
from tests.scraping_helpers import make_client, make_ctx

pytestmark = pytest.mark.skipif(not playwright_installed(), reason="playwright not installed")

PAGE = b"""<!doctype html><html><body><h1>Jobs</h1><ul id="list"></ul>
<script>
  setTimeout(() => {
    for (const [id, title] of [[7, "Night Nurse"], [8, "Day Nurse"]]) {
      const a = document.createElement("a");
      a.href = "/jobs/" + id; a.textContent = title;
      document.getElementById("list").append(a);
    }
  }, 100);
</script></body></html>"""


def chromium_executable() -> str | None:
    if path := os.environ.get("CHROMIUM_EXECUTABLE"):
        return path
    found = sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux*/chrome"))
    return str(found[-1]) if found else None


@pytest.fixture
async def site() -> AsyncIterator[str]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        path = request.split(b" ", 2)[1]
        body, status = (PAGE, b"200 OK") if path == b"/jobs" else (b"", b"404 Not Found")
        writer.write(
            b"HTTP/1.1 "
            + status
            + b"\r\nContent-Type: text/html\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    server.close()
    await server.wait_closed()


async def test_renders_javascript_page(site: str) -> None:
    async with make_client(allow_private=True) as client:
        browser = BrowserManager(client, executable_path=chromium_executable(), idle_timeout=60)
        try:
            outcome = await detect(
                f"{site}/jobs", make_ctx(client, render_mode="auto", browser=browser)
            )
        except Exception as exc:  # pragma: no cover - environment without a usable Chromium
            if "could not start Chromium" in str(exc):
                pytest.skip(str(exc))
            raise
        finally:
            await browser.close()
    assert outcome.rendered
    assert [p.title for p in outcome.postings] == ["Night Nurse", "Day Nurse"]
    assert outcome.postings[0].url == f"{site}/jobs/7"


async def test_browser_refuses_private_page_without_allow() -> None:
    async with make_client() as client:
        browser = BrowserManager(client, executable_path=chromium_executable())
        from app.scraping.base import BlockedError

        with pytest.raises(BlockedError, match="non-public"):
            await browser.render("http://127.0.0.1:9/jobs", respect_robots=False)
        await browser.close()


# --- request guard, with stand-in Playwright objects -----------------------------------------


@dataclass
class FakeRequest:
    url: str
    resource_type: str = "document"


class FakeRoute:
    def __init__(self, url: str, resource_type: str = "document") -> None:
        self.request = FakeRequest(url, resource_type)
        self.outcome: str | None = None

    async def abort(self, *_args: Any) -> None:
        self.outcome = "aborted"

    async def continue_(self) -> None:
        self.outcome = "continued"


@pytest.mark.parametrize(
    ("url", "resource_type", "expected"),
    [
        ("https://cdn.example.com/app.js", "script", "continued"),
        ("https://api.example.com/jobs.json", "fetch", "continued"),
        ("http://169.254.169.254/latest/meta-data/", "fetch", "aborted"),
        ("http://10.0.0.8:8080/admin", "xhr", "aborted"),
        ("http://[::1]/", "document", "aborted"),
        ("https://cdn.example.com/logo.png", "image", "aborted"),
        ("https://fonts.example.com/a.woff2", "font", "aborted"),
        ("data:text/plain,hi", "other", "continued"),
        ("file:///etc/passwd", "document", "aborted"),
    ],
)
async def test_request_guard(url: str, resource_type: str, expected: str) -> None:
    async with make_client() as client:
        browser = BrowserManager(client)
        route = FakeRoute(url, resource_type)
        await browser._guard(route)  # type: ignore[arg-type]
        assert route.outcome == expected

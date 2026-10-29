"""End-to-end fixtures: a fake careers site, the real app in a subprocess, and headless Chromium."""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

PASSWORD = "e2e correct horse battery"
AXE = Path(__file__).parent / "vendor" / "axe.min.js"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class JobsSite:
    """A hospital careers page listing whatever is in `jobs` (id -> title)."""

    jobs: dict[int, str] = field(default_factory=dict[int, str])
    url: str = ""

    def html(self) -> bytes:
        links = "\n".join(
            f'<li><a href="/careers/jobs/{job_id}">{title}</a></li>'
            for job_id, title in self.jobs.items()
        )
        return (
            "<!doctype html><html><head><title>Careers at Example Hospital</title></head><body>"
            f'<h1>Open positions</h1><nav><a href="/">Home</a></nav><ul>{links}</ul></body></html>'
        ).encode()


@pytest.fixture
def jobs_site() -> Iterator[JobsSite]:
    site = JobsSite(
        {101: "Registered Nurse - ICU", 102: "Respiratory Therapist", 103: "Unit Clerk"}
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.rstrip("/") == "/careers":
                body, status = site.html(), 200
            else:
                body, status = b"not found", 404
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", free_port()), Handler)
    site.url = f"http://127.0.0.1:{server.server_address[1]}/careers"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield site
    server.shutdown()
    server.server_close()


@dataclass
class Webhook:
    """Receives Apprise json:// notifications."""

    url: str = ""
    received: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def wait_for(self, count: int, timeout: float = 20.0) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while len(self.received) < count and time.monotonic() < deadline:
            time.sleep(0.1)
        return self.received


@pytest.fixture
def webhook() -> Iterator[Webhook]:
    hook = Webhook()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            hook.received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", free_port()), Handler)
    hook.url = f"json://127.0.0.1:{server.server_address[1]}/hook"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield hook
    server.shutdown()
    server.server_close()


@dataclass
class AppServer:
    url: str
    log: Path

    def setup_token(self) -> str:
        match = re.search(r"Setup token: (\S+)", self.log.read_text())
        assert match, "no setup token in the app log"
        return match.group(1)


@pytest.fixture
def app_server(tmp_path: Path) -> Iterator[AppServer]:
    port = free_port()
    log = tmp_path / "app.log"
    env = {
        **os.environ,
        "DATA_DIR": str(tmp_path / "data"),
        "PORT": str(port),
        "ALLOW_PRIVATE_URLS": "true",  # the fake careers site is on 127.0.0.1
        "LOG_LEVEL": "info",
    }
    env.pop("APP_PASSWORD", None)
    with log.open("w") as out:
        process = subprocess.Popen(
            [sys.executable, "-m", "app.cli", "serve"],
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            cwd=Path(__file__).resolve().parents[2],
        )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"{url}/healthz", timeout=1):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("app did not start:\n" + log.read_text())
        yield AppServer(url, log)
    finally:
        process.terminate()
        process.wait(timeout=15)


def chromium_executable() -> str | None:
    if path := os.environ.get("CHROMIUM_EXECUTABLE"):
        return path
    found = sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux*/chrome"))
    return str(found[-1]) if found else None


@pytest.fixture
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(executable_path=chromium_executable())
        except Exception as exc:  # pragma: no cover - no usable Chromium here
            pytest.skip(f"Chromium unavailable: {exc}")
        yield browser
        browser.close()


SERIOUS = ("serious", "critical")


def axe_violations(page: Page) -> list[str]:
    """Run axe-core (WCAG 2.2 A/AA + best practices); return serious or critical violations."""
    if not page.evaluate("typeof window.axe !== 'undefined'"):
        page.evaluate(AXE.read_text())
    result = page.evaluate(
        """async () => {
            const tags = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];
            const r = await axe.run(document, {runOnly: {type: "tag", values: tags}});
            return r.violations.map(v => ({id: v.id, impact: v.impact, help: v.help,
                nodes: v.nodes.slice(0, 3).map(n => n.target.join(" "))}));
        }"""
    )
    return [
        f"{v['impact']}: {v['id']} ({v['help']}) at {v['nodes']}"
        for v in result
        if v["impact"] in SERIOUS
    ]


def horizontal_overflow(page: Page) -> int:
    return page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )

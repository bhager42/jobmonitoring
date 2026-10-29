"""Build the shared HTTP client, browser and per-check fetch contexts from settings."""

from typing import cast

from app.config import Settings
from app.scraping.base import FetchContext, RenderMode
from app.scraping.browser import BrowserManager
from app.scraping.http import PoliteClient
from app.services.checks import ContextFactory, MonitorSnapshot


def create_http_client(settings: Settings) -> PoliteClient:
    return PoliteClient(
        allow_private=settings.allow_private_urls,
        proxy=settings.outbound_proxy,
        ca_bundle=str(settings.outbound_ca_bundle) if settings.outbound_ca_bundle else None,
    )


def create_browser(settings: Settings, client: PoliteClient) -> BrowserManager | None:
    if not BrowserManager.installed():
        return None
    return BrowserManager(client, executable_path=settings.chromium_executable)


def make_context(
    client: PoliteClient,
    browser: BrowserManager | None,
    *,
    respect_robots: bool,
    render_mode: str,
    wait_selector: str | None = None,
) -> FetchContext:
    return FetchContext(
        client=client,
        browser=browser,
        respect_robots=respect_robots,
        render_mode=cast(RenderMode, render_mode),
        wait_selector=wait_selector,
    )


def context_factory(client: PoliteClient, browser: BrowserManager | None) -> ContextFactory:
    def build(snapshot: MonitorSnapshot) -> FetchContext:
        return make_context(
            client,
            browser,
            respect_robots=snapshot.respect_robots,
            render_mode=snapshot.render_mode,
            wait_selector=snapshot.adapter_config.get("wait_selector") or None,
        )

    return build

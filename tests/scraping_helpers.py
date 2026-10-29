"""Shared helpers for scraping tests: offline DNS and a client that records sleeps."""

import ipaddress
import json
from pathlib import Path
from typing import Any

from app.scraping.base import FetchContext
from app.scraping.http import PoliteClient

FIXTURES = Path(__file__).parent / "fixtures"
PUBLIC_IP = "93.184.215.14"


async def fake_resolver(host: str, port: int) -> list[str]:
    """IP literals resolve to themselves; every name resolves to a public address."""
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return [PUBLIC_IP]
    return [host.strip("[]")]


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_client(**kwargs: Any) -> PoliteClient:
    kwargs.setdefault("min_host_interval", 0)
    kwargs.setdefault("resolver", fake_resolver)
    kwargs.setdefault("sleep", SleepRecorder())
    return PoliteClient(**kwargs)


def make_ctx(client: PoliteClient, **kwargs: Any) -> FetchContext:
    kwargs.setdefault("respect_robots", False)
    kwargs.setdefault("render_mode", "static")
    return FetchContext(client=client, **kwargs)

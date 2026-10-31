from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import respx

from app.scraping import http as http_module
from app.scraping.base import BlockedError, FetchError
from app.scraping.http import MAX_RETRY_AFTER, USER_AGENT, PoliteClient
from tests.scraping_helpers import SleepRecorder, make_client

SITE = "https://careers.example.com"


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def sleeps() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
async def client(sleeps: SleepRecorder) -> AsyncIterator[PoliteClient]:
    async with make_client(sleep=sleeps) as client:
        yield client


async def test_identifies_itself(mock: respx.MockRouter, client: PoliteClient) -> None:
    route = mock.get(f"{SITE}/jobs").respond(200, text="ok")
    await client.get_text(f"{SITE}/jobs", respect_robots=False)
    assert route.calls.last.request.headers["user-agent"] == USER_AGENT
    assert USER_AGENT.startswith("JobMonitor/")


# --- robots.txt -----------------------------------------------------------------------------


async def test_robots_disallow_blocks(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.get(f"{SITE}/robots.txt").respond(200, text="User-agent: *\nDisallow: /jobs\n")
    page = mock.get(f"{SITE}/jobs").respond(200, text="ok")
    with pytest.raises(BlockedError, match=r"robots\.txt"):
        await client.get_text(f"{SITE}/jobs", respect_robots=True)
    assert not page.called


async def test_robots_specific_agent(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.get(f"{SITE}/robots.txt").respond(
        200, text="User-agent: JobMonitor\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    )
    with pytest.raises(BlockedError):
        await client.get_text(f"{SITE}/jobs", respect_robots=True)


async def test_robots_allow_and_cache(mock: respx.MockRouter, client: PoliteClient) -> None:
    robots = mock.get(f"{SITE}/robots.txt").respond(200, text="User-agent: *\nDisallow: /admin\n")
    mock.get(f"{SITE}/jobs").respond(200, text="ok")
    await client.get_text(f"{SITE}/jobs", respect_robots=True)
    await client.get_text(f"{SITE}/jobs", respect_robots=True)
    assert robots.call_count == 1


async def test_missing_robots_allows(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.get(f"{SITE}/robots.txt").respond(404)
    mock.get(f"{SITE}/jobs").respond(200, text="ok")
    assert (await client.get_text(f"{SITE}/jobs", respect_robots=True)).text == "ok"


async def test_robots_server_error_disallows(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.get(f"{SITE}/robots.txt").respond(503)
    with pytest.raises(BlockedError):
        await client.get_text(f"{SITE}/jobs", respect_robots=True)


async def test_ignoring_robots_skips_the_request(
    mock: respx.MockRouter, client: PoliteClient
) -> None:
    robots = mock.get(f"{SITE}/robots.txt").respond(200, text="User-agent: *\nDisallow: /\n")
    mock.get(f"{SITE}/jobs").respond(200, text="ok")
    await client.get_text(f"{SITE}/jobs", respect_robots=False)
    assert not robots.called


# --- retries --------------------------------------------------------------------------------


async def test_retries_server_errors_with_backoff(
    mock: respx.MockRouter, client: PoliteClient, sleeps: SleepRecorder
) -> None:
    mock.get(f"{SITE}/jobs").mock(
        side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200, text="ok")]
    )
    assert (await client.get_text(f"{SITE}/jobs", respect_robots=False)).text == "ok"
    assert sleeps.calls == [1.0, 2.0]


async def test_honours_retry_after(
    mock: respx.MockRouter, client: PoliteClient, sleeps: SleepRecorder
) -> None:
    mock.get(f"{SITE}/jobs").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(429, headers={"Retry-After": "86400"}),
            httpx.Response(200, text="ok"),
        ]
    )
    await client.get_text(f"{SITE}/jobs", respect_robots=False)
    assert sleeps.calls == [7.0, MAX_RETRY_AFTER]


async def test_gives_up_after_max_retries(
    mock: respx.MockRouter, client: PoliteClient, sleeps: SleepRecorder
) -> None:
    route = mock.get(f"{SITE}/jobs").respond(503)
    with pytest.raises(FetchError, match="HTTP 503"):
        await client.get_text(f"{SITE}/jobs", respect_robots=False)
    assert route.call_count == 4  # first try + 3 retries
    assert sleeps.calls == [1.0, 2.0, 4.0]


async def test_persistent_rate_limit_is_blocked(
    mock: respx.MockRouter, client: PoliteClient
) -> None:
    mock.get(f"{SITE}/jobs").respond(429)
    with pytest.raises(BlockedError, match="rate limiting"):
        await client.get_text(f"{SITE}/jobs", respect_robots=False)


async def test_retries_network_errors(
    mock: respx.MockRouter, client: PoliteClient, sleeps: SleepRecorder
) -> None:
    mock.get(f"{SITE}/jobs").mock(
        side_effect=[httpx.ConnectTimeout("slow"), httpx.Response(200, text="ok")]
    )
    assert (await client.get_text(f"{SITE}/jobs", respect_robots=False)).text == "ok"
    assert sleeps.calls == [1.0]


async def test_network_errors_eventually_fail(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.get(f"{SITE}/jobs").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(FetchError, match="could not reach"):
        await client.get_text(f"{SITE}/jobs", respect_robots=False)


# --- refusals and errors ------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_access_refused(mock: respx.MockRouter, client: PoliteClient, status: int) -> None:
    mock.get(f"{SITE}/jobs").respond(status)
    with pytest.raises(BlockedError, match=f"HTTP {status}"):
        await client.get_text(f"{SITE}/jobs", respect_robots=False)


async def test_bot_challenge_is_not_retried(
    mock: respx.MockRouter, client: PoliteClient, sleeps: SleepRecorder
) -> None:
    route = mock.get(f"{SITE}/jobs").respond(503, headers={"cf-mitigated": "challenge"})
    with pytest.raises(BlockedError, match="bot protection"):
        await client.get_text(f"{SITE}/jobs", respect_robots=False)
    assert route.call_count == 1
    assert sleeps.calls == []


async def test_not_found(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.get(f"{SITE}/jobs").respond(404)
    with pytest.raises(FetchError, match="HTTP 404"):
        await client.get_text(f"{SITE}/jobs", respect_robots=False)


async def test_invalid_json(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.get(f"{SITE}/api").respond(200, text="<html>")
    with pytest.raises(FetchError, match="valid JSON"):
        await client.get_json(f"{SITE}/api")


# --- size limit ---------------------------------------------------------------------------


async def test_declared_size_limit(
    mock: respx.MockRouter, client: PoliteClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(http_module, "MAX_RESPONSE_BYTES", 100)
    mock.get(f"{SITE}/big").respond(200, content=b"x" * 101)
    with pytest.raises(FetchError, match="larger than"):
        await client.get_text(f"{SITE}/big", respect_robots=False)


async def test_streamed_size_limit(
    mock: respx.MockRouter, client: PoliteClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(http_module, "MAX_RESPONSE_BYTES", 100)

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(5):
            yield b"x" * 30

    mock.get(f"{SITE}/big").mock(return_value=httpx.Response(200, content=chunks()))
    with pytest.raises(FetchError, match="larger than"):
        await client.get_text(f"{SITE}/big", respect_robots=False)


# --- redirects ----------------------------------------------------------------------------


async def test_follows_redirects(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.get(f"{SITE}/old").respond(301, headers={"Location": "/new"})
    mock.get(f"{SITE}/new").respond(200, text="moved")
    response = await client.get_text(f"{SITE}/old", respect_robots=False)
    assert response.url == f"{SITE}/new"
    assert response.text == "moved"


async def test_redirect_to_private_address_blocked(
    mock: respx.MockRouter, client: PoliteClient
) -> None:
    mock.get(f"{SITE}/old").respond(302, headers={"Location": "http://169.254.169.254/latest"})
    metadata = mock.get("http://169.254.169.254/latest").respond(200, text="secrets")
    with pytest.raises(BlockedError, match="non-public"):
        await client.get_text(f"{SITE}/old", respect_robots=False)
    assert not metadata.called


async def test_redirect_checks_robots_of_new_host(
    mock: respx.MockRouter, client: PoliteClient
) -> None:
    mock.get(f"{SITE}/robots.txt").respond(404)
    mock.get(f"{SITE}/old").respond(302, headers={"Location": "https://other.example.org/jobs"})
    mock.get("https://other.example.org/robots.txt").respond(200, text="User-agent: *\nDisallow: /")
    with pytest.raises(BlockedError, match=r"robots\.txt"):
        await client.get_text(f"{SITE}/old", respect_robots=True)


async def test_too_many_redirects(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.get(f"{SITE}/loop").respond(302, headers={"Location": "/loop"})
    with pytest.raises(FetchError, match="too many redirects"):
        await client.get_text(f"{SITE}/loop", respect_robots=False)


async def test_post_303_becomes_get(mock: respx.MockRouter, client: PoliteClient) -> None:
    mock.post(f"{SITE}/api").respond(303, headers={"Location": "/result"})
    result = mock.get(f"{SITE}/result").respond(200, json={"ok": True})
    assert await client.post_json(f"{SITE}/api", {"q": 1}) == {"ok": True}
    assert result.called


# --- per-host spacing ---------------------------------------------------------------------


async def test_spaces_requests_to_the_same_host(mock: respx.MockRouter) -> None:
    sleeps = SleepRecorder()
    now = [100.0]
    async with make_client(min_host_interval=2.0, sleep=sleeps, clock=lambda: now[0]) as client:
        mock.get(f"{SITE}/a").respond(200)
        mock.get(f"{SITE}/b").respond(200)
        mock.get("https://other.example.org/c").respond(200)
        await client.get_text(f"{SITE}/a", respect_robots=False)
        await client.get_text("https://other.example.org/c", respect_robots=False)
        assert sleeps.calls == []  # different hosts don't wait for each other
        now[0] += 0.5
        await client.get_text(f"{SITE}/b", respect_robots=False)
        assert sleeps.calls == [1.5]


# --- compressed responses (most real sites) ---------------------------------------------------


def _compress(encoding: str, data: bytes) -> bytes:
    import gzip
    import zlib

    return {"gzip": gzip.compress, "deflate": zlib.compress}[encoding](data)


# httpx only asks for brotli/zstd when those libraries are installed, so gzip and deflate are what
# servers send to this app.
@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_compressed_pages_are_decoded_once(
    mock: respx.MockRouter, client: PoliteClient, encoding: str
) -> None:
    """A real bug: text was decompressed a second time and failed on every compressed site."""
    page = "<h1>Careers</h1><a href='/jobs/1'>Ingénieur</a>".encode()
    mock.get(f"{SITE}/careers").respond(
        200,
        content=_compress(encoding, page),
        headers={"Content-Encoding": encoding, "Content-Type": "text/html; charset=utf-8"},
    )
    response = await client.get_text(f"{SITE}/careers", respect_robots=False)
    assert response.text == page.decode()


async def test_compressed_json_is_decoded_once(
    mock: respx.MockRouter, client: PoliteClient
) -> None:
    import gzip
    import json

    mock.get(f"{SITE}/api/jobs").respond(
        200,
        content=gzip.compress(json.dumps({"jobs": [{"id": 1}]}).encode()),
        headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
    )
    assert await client.get_json(f"{SITE}/api/jobs") == {"jobs": [{"id": 1}]}

import asyncio
import ipaddress

import pytest

from app.scraping.base import BlockedError
from app.scraping.http import build_transport
from app.security.ssrf import (
    GuardedNetworkBackend,
    SSRFError,
    is_public_ip,
    system_resolver,
    validate_url,
)
from tests.scraping_helpers import PUBLIC_IP, fake_resolver, make_client

NON_PUBLIC = [
    "127.0.0.1",
    "127.255.255.254",
    "10.1.2.3",
    "172.16.0.1",
    "192.168.1.10",
    "169.254.169.254",  # cloud metadata
    "100.64.0.1",  # carrier-grade NAT / Tailscale
    "0.0.0.0",  # noqa: S104 - a test value, not a bind address
    "255.255.255.255",
    "224.0.0.251",
    "::1",
    "::",
    "fe80::1",
    "fc00::1",
    "::ffff:127.0.0.1",  # IPv4-mapped loopback
    "::ffff:10.0.0.1",
    "2002:7f00:1::",  # 6to4 wrapping 127.0.0.1
    "64:ff9b::a00:1",  # NAT64 wrapping 10.0.0.1
    "ff02::1",
]


@pytest.mark.parametrize("address", NON_PUBLIC)
def test_non_public_addresses(address: str) -> None:
    assert not is_public_ip(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", [PUBLIC_IP, "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses(address: str) -> None:
    assert is_public_ip(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/admin",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://localhost/",
        # Alternative spellings of 127.0.0.1 that the system resolver understands.
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0177.0.0.1/",
        "http://127.1/",
    ],
)
async def test_real_resolver_blocks_local_targets(url: str) -> None:
    with pytest.raises(SSRFError):
        await validate_url(url, resolver=system_resolver)


@pytest.mark.parametrize(
    ("host", "addresses"),
    [
        ("127.0.0.1.nip.io", ["127.0.0.1"]),
        ("localtest.me", ["127.0.0.1"]),
        ("metadata.attacker.example", ["169.254.169.254"]),
        ("mixed.attacker.example", [PUBLIC_IP, "10.0.0.1"]),  # any private address is enough
        ("v6.attacker.example", ["::1"]),
        ("zone.attacker.example", ["fe80::1%eth0"]),
    ],
)
async def test_hostnames_resolving_to_private_addresses(host: str, addresses: list[str]) -> None:
    async def resolver(_host: str, _port: int) -> list[str]:
        return addresses

    with pytest.raises(SSRFError, match="non-public"):
        await validate_url(f"https://{host}/careers", resolver=resolver)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/",
        "gopher://example.com/",
        "javascript:alert(1)",
        "https:///nohost",
        "https://user:pass@example.com/",
    ],
)
async def test_rejects_bad_urls(url: str) -> None:
    with pytest.raises(SSRFError):
        await validate_url(url, resolver=fake_resolver)


async def test_unresolvable_host() -> None:
    async def resolver(_host: str, _port: int) -> list[str]:
        raise OSError("Name or service not known")

    with pytest.raises(SSRFError, match="cannot resolve"):
        await validate_url("https://nope.invalid/", resolver=resolver)


async def test_allow_private_skips_address_checks() -> None:
    await validate_url("http://127.0.0.1:8080/", allow_private=True, resolver=fake_resolver)
    with pytest.raises(SSRFError):
        await validate_url("file:///etc/passwd", allow_private=True)


async def test_public_url_passes() -> None:
    await validate_url("https://boards.example.com/jobs", resolver=fake_resolver)


def test_transport_uses_guarded_backend() -> None:
    """Guards against an httpx/httpcore upgrade silently dropping the connect-time check."""
    transport = build_transport(allow_private=False, resolver=fake_resolver)
    assert isinstance(transport._pool._network_backend, GuardedNetworkBackend)  # type: ignore[attr-defined]


# --- connect-time guard against a real local server ----------------------------------------


@pytest.fixture
async def local_server():  # type: ignore[no-untyped-def]
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        host = next(
            line.split(b":", 1)[1].strip()
            for line in request.split(b"\r\n")
            if line.lower().startswith(b"host:")
        )
        body = b"secret from " + host
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield port
    server.close()
    await server.wait_closed()


async def test_dns_rebinding_blocked_at_connect_time(local_server: int) -> None:
    """The pre-flight lookup says public, the lookup at connect time says 127.0.0.1."""
    answers = iter([[PUBLIC_IP], ["127.0.0.1"]])

    async def rebinding_resolver(_host: str, _port: int) -> list[str]:
        return next(answers)

    async with make_client(resolver=rebinding_resolver) as client:
        with pytest.raises(BlockedError, match="non-public"):
            await client.get_text(
                f"http://rebind.attacker.example:{local_server}/", respect_robots=False
            )


async def test_guarded_backend_connects_to_checked_ip(local_server: int) -> None:
    """With private targets allowed, the request reaches the server with its Host header intact."""

    async def resolver(_host: str, _port: int) -> list[str]:
        return ["127.0.0.1"]

    async with make_client(resolver=resolver, allow_private=True) as client:
        response = await client.get_text(
            f"http://intranet.test:{local_server}/", respect_robots=False
        )
    assert response.text == f"secret from intranet.test:{local_server}"


async def test_private_name_blocked_without_allow(local_server: int) -> None:
    async def resolver(_host: str, _port: int) -> list[str]:
        return ["127.0.0.1"]

    async with make_client(resolver=resolver) as client:
        with pytest.raises(BlockedError, match="non-public"):
            await client.get_text(f"http://intranet.test:{local_server}/", respect_robots=False)

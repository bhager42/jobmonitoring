"""Server-side request forgery guard.

Two layers:
* `validate_url` rejects bad schemes, and hosts that resolve to non-public addresses, before
  fetching.
* `GuardedNetworkBackend` re-resolves at connect time, checks every address, and connects to the
  checked IP itself. A hostname that flips to a private address between the two (DNS rebinding)
  is still refused.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from urllib.parse import urlsplit

import httpcore
from httpcore import AsyncNetworkBackend, AsyncNetworkStream
from httpcore._backends.anyio import AnyIOBackend  # pyright: ignore[reportPrivateUsage]

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

NAT64_PREFIXES = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


class SSRFError(ValueError):
    """The URL points somewhere this app must not fetch."""


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def is_public_ip(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_ip(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return is_public_ip(ip.sixtofour)
        if ip.teredo is not None:
            return is_public_ip(ip.teredo[1])
        if any(ip in net for net in NAT64_PREFIXES):
            return False
    return ip.is_global and not ip.is_multicast


def check_addresses(host: str, addresses: Iterable[str]) -> list[str]:
    checked = list(addresses)
    if not checked:
        raise SSRFError(f"{host} did not resolve to any address")
    for address in checked:
        ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop IPv6 zone ids
        if not is_public_ip(ip):
            raise SSRFError(f"{host} resolves to a non-public address ({ip})")
    return checked


async def validate_url(
    url: str, *, allow_private: bool = False, resolver: Resolver = system_resolver
) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise SSRFError("only http and https URLs are allowed")
    if not parts.hostname:
        raise SSRFError("URL has no host")
    if parts.username or parts.password:
        raise SSRFError("URLs with credentials are not allowed")
    if allow_private:
        return
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = await resolver(parts.hostname, port)
    except OSError as exc:
        raise SSRFError(f"cannot resolve {parts.hostname}: {exc}") from exc
    check_addresses(parts.hostname, addresses)


class GuardedNetworkBackend(AsyncNetworkBackend):
    """httpcore backend that only opens TCP connections to public addresses."""

    def __init__(
        self,
        *,
        allow_private: bool = False,
        resolver: Resolver = system_resolver,
        inner: AsyncNetworkBackend | None = None,
    ) -> None:
        self.allow_private = allow_private
        self.resolver = resolver
        self.inner: AsyncNetworkBackend = inner or AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's interface
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        try:
            addresses = await self.resolver(host, port)
        except OSError as exc:
            raise httpcore.ConnectError(f"cannot resolve {host}: {exc}") from exc
        if self.allow_private:
            checked = addresses
        else:
            try:
                checked = check_addresses(host, addresses)
            except SSRFError as exc:
                raise httpcore.ConnectError(str(exc)) from exc
        last_error: Exception | None = None
        for address in checked:
            try:
                return await self.inner.connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        raise last_error or httpcore.ConnectError(f"cannot connect to {host}")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's interface
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)

"""DNS-pinned public-web egress for HTTP acquisition and browser connections.

This deliberately does not govern trusted local integrations such as Ollama.
"""

import asyncio
import ipaddress
import socket
from contextlib import suppress
from urllib.parse import SplitResult, urlsplit

from kronos.security.egress import EgressBlockedError, check_url

# Chromium otherwise bypasses explicit proxies for loopback/link-local targets.
BROWSER_PROXY_ARGS = [
    "--proxy-bypass-list=<-loopback>",
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
]


class PublicWebBlockedError(Exception):
    """A destination failed the public-network security check."""


def is_public_address(address: str) -> bool:
    """Reject private, special-use and IPv6 transition addresses."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if not ip.is_global or ip.is_multicast or "%" in address:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in ipaddress.ip_network("::/96") or ip in ipaddress.ip_network("::ffff:0:0:0/96"):
            return False
        if ip.ipv4_mapped or ip.sixtofour or ip.teredo:
            return False
        if ip in ipaddress.ip_network("64:ff9b::/96") or ip in ipaddress.ip_network("64:ff9b:1::/48"):
            return False
    return True


def validate_public_url(url: str) -> SplitResult:
    """Validate syntax and literal targets; DNS is checked at connection time."""
    try:
        if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
            raise ValueError("control characters or ambiguous URL")
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host or parsed.username is not None:
            raise ValueError("only absolute HTTP(S) URLs without credentials are allowed")
        if parsed.port == 0 or "%" in host:
            raise ValueError("invalid port or scoped address")
        if host == "localhost" or host.endswith(".localhost") or host == "metadata.google.internal":
            raise ValueError("internal hostname")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            host.encode("idna")
        else:
            if not is_public_address(host):
                raise ValueError("non-public address")
    except (ValueError, UnicodeError) as exc:
        raise PublicWebBlockedError(str(exc)) from exc
    try:
        check_url(url, tool="public_web")
    except EgressBlockedError as exc:
        raise PublicWebBlockedError(str(exc)) from exc
    return parsed


async def open_public_connection(host: str, port: int):
    """Connect only to numeric addresses from one fully validated DNS answer."""
    records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not records or any(not is_public_address(record[4][0]) for record in records):
        raise PublicWebBlockedError("DNS answer contains a non-public address")
    last_error: OSError | None = None
    for family, _, _, _, address in records:
        try:
            return await asyncio.open_connection(address[0], port, family=family)
        except OSError as exc:
            last_error = exc
    raise last_error or OSError("no reachable public address")


class PublicWebProxy:
    """Ephemeral loopback proxy with checked, DNS-pinned outbound sockets.

    A blocked request poisons the session, so callers cannot accept HTML after a
    rejected redirect/subrequest. HTTPS stays end-to-end encrypted.
    """

    def __init__(self):
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self._blocked: PublicWebBlockedError | None = None
        self._closing = False

    @property
    def url(self) -> str:
        """Return the address of the running proxy."""
        if self._server is None:
            raise RuntimeError("public-web proxy has not started")
        return f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    def raise_if_blocked(self) -> None:
        """Surface denied requests rather than silently accepting partial HTML."""
        if self._blocked is not None:
            raise self._blocked

    async def start(self):
        """Listen locally without opening any outbound connection."""
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=64 * 1024)
        return self

    async def close(self) -> None:
        """Close the listener and all active tunnels, including on cancellation."""
        self._closing = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.difference_update(tasks)

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *_):
        await self.close()

    def _accept(self, reader, writer):
        if self._closing:
            writer.close()
            return
        task = asyncio.create_task(self._serve(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve(self, reader, writer):
        upstream = None
        try:
            self.raise_if_blocked()
            async with asyncio.timeout(30):
                raw = await reader.readuntil(b"\r\n\r\n")
                first, *headers = raw.decode("latin1").split("\r\n")
                method, target, version = first.split(" ")
                if version not in {"HTTP/1.0", "HTTP/1.1"} or not method.isalpha():
                    raise ValueError("invalid proxy request")
                parsed = validate_public_url("https://" + target if method == "CONNECT" else target)
                if method == "CONNECT" and (parsed.path or parsed.query or parsed.fragment):
                    raise ValueError("invalid CONNECT target")
                if method != "CONNECT" and parsed.scheme != "http":
                    raise ValueError("HTTPS requires CONNECT")
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                remote, upstream = await open_public_connection(parsed.hostname, port)
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                forwarded = [f"{method} {path} {version}", f"Host: {parsed.netloc}", "Connection: close"]
                for header in headers:
                    if not header:
                        continue
                    name, value = header.split(":", 1)
                    if name.lower() not in {"host", "connection", "proxy-connection", "proxy-authorization"}:
                        forwarded.append(f"{name}:{value}")
                upstream.write(("\r\n".join(forwarded) + "\r\n\r\n").encode("latin1"))
                await upstream.drain()
            await self._relay(reader, writer, remote, upstream)
        except PublicWebBlockedError as exc:
            self._blocked = exc
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        finally:
            for stream in (upstream, writer):
                if stream is not None:
                    stream.close()
                    with suppress(OSError):
                        await stream.wait_closed()

    @staticmethod
    async def _relay(reader, writer, remote, upstream):
        async def copy(source, destination):
            while chunk := await source.read(64 * 1024):
                destination.write(chunk)
                await destination.drain()

        tasks = [asyncio.create_task(copy(reader, upstream)), asyncio.create_task(copy(remote, writer))]
        try:
            done, _ = await asyncio.wait(tasks, timeout=120, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

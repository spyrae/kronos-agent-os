"""Exercise the actual acquisition boundary, not just URL string filtering."""

import asyncio
import socket
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

from kronos.security import public_web
from kronos.security.public_web import PublicWebBlockedError, PublicWebProxy, validate_public_url
from kronos.tools import acquire
from kronos.tools.browser import engine


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost./",
        "http://foo.localhost/",
        "http://169.254.169.254/",
        "http://10.0.0.1/",
        "http://192.168.0.1/",
        "http://[::1]/",
        "http://[::127.0.0.1]/",
        "http://[::ffff:0:127.0.0.1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[64:ff9b::7f00:1]/",
        "http://100.64.0.1/",
        "http://224.0.0.1/",
        "http://0.0.0.0/",
        "file:///etc/passwd",
        "//example.com/path",
        "https://user:pass@example.com/",
        "https://example.com:0/",
        "https://example.com:bad/",
        "http://[fe80::1%25en0]/",
        "https://example.com\\@localhost/",
        "\nhttp://example.com/",
    ],
)
def test_static_rejections(url):
    with pytest.raises(PublicWebBlockedError):
        validate_public_url(url)


@pytest.mark.parametrize("url", ["https://example.com/a?b=1", "http://8.8.8.8/", "https://[2606:4700:4700::1111]/"])
def test_public_urls(url):
    validate_public_url(url)


def records(*addresses):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80)) for ip in addresses]


@pytest.mark.parametrize("host", ["127.1", "2130706433", "0x7f000001", "public.example"])
async def test_private_dns_and_alternate_ip_spellings_never_connect(monkeypatch, host):
    resolver = AsyncMock(return_value=records("127.0.0.1"))
    connect = AsyncMock()
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    monkeypatch.setattr(asyncio, "open_connection", connect)
    with pytest.raises(PublicWebBlockedError):
        await public_web.open_public_connection(host, 80)
    connect.assert_not_called()


async def test_mixed_public_private_dns_answer_is_rejected(monkeypatch):
    monkeypatch.setattr(
        asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=records("8.8.8.8", "10.0.0.1"))
    )
    connect = AsyncMock()
    monkeypatch.setattr(asyncio, "open_connection", connect)
    with pytest.raises(PublicWebBlockedError):
        await public_web.open_public_connection("mixed.example", 80)
    connect.assert_not_called()


async def test_connect_uses_the_checked_ip_not_a_second_hostname_resolution(monkeypatch):
    resolver = AsyncMock(side_effect=[records("8.8.8.8"), records("127.0.0.1")])
    connect = AsyncMock(return_value=("reader", "writer"))
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    monkeypatch.setattr(asyncio, "open_connection", connect)
    assert await public_web.open_public_connection("rebind.example", 443) == ("reader", "writer")
    resolver.assert_awaited_once()
    connect.assert_awaited_once_with("8.8.8.8", 443, family=socket.AF_INET)


@pytest.mark.parametrize("tier", ["fetch_plain", "fetch_stealth", "fetch_browser"])
async def test_security_failure_never_falls_through_to_another_backend(monkeypatch, tier):
    plain = AsyncMock(return_value=(403, "blocked"))
    stealth = AsyncMock(side_effect=acquire.FetchBlockedError("unavailable"))
    browser = AsyncMock(return_value=(200, "<p>secret</p>"))
    mocks = {"fetch_plain": plain, "fetch_stealth": stealth, "fetch_browser": browser}
    mocks[tier].side_effect = PublicWebBlockedError("private target")
    for name, mock in mocks.items():
        monkeypatch.setattr(acquire, name, mock)
    with pytest.raises(PublicWebBlockedError):
        await acquire.fetch_tiered("https://example.com/")
    if tier == "fetch_plain":
        stealth.assert_not_called()
    if tier != "fetch_browser":
        browser.assert_not_called()


@pytest.mark.parametrize("result", ["Navigation failed: timeout", "Navigation blocked: private target"])
async def test_failed_navigation_cannot_return_previous_authenticated_page(monkeypatch, result):
    monkeypatch.setattr(engine, "navigate", AsyncMock(return_value=result))
    html = AsyncMock(return_value="<html>previous private account</html>")
    monkeypatch.setattr(engine, "page_html", html)
    with pytest.raises((acquire.FetchBlockedError, PublicWebBlockedError)):
        await acquire.fetch_browser("https://example.com/")
    html.assert_not_called()


async def test_unsupported_stealth_command_cannot_bypass_the_proxy(monkeypatch):
    monkeypatch.setattr(acquire.settings, "stealth_fetch_command", "curl {url}")
    run = AsyncMock()
    monkeypatch.setattr(acquire, "_run_stealth_command", run)
    with pytest.raises(acquire.FetchBlockedError, match="unsupported stealth backend"):
        await acquire.fetch_stealth("https://example.com/")
    run.assert_not_called()


@pytest.mark.parametrize(
    "wire_request",
    [
        b"GET http://127.0.0.1/private HTTP/1.1\r\nHost: public.example\r\n\r\n",
        b"CONNECT 169.254.169.254:80 HTTP/1.1\r\n\r\n",
    ],
)
async def test_proxy_blocks_http_and_connect_without_outbound_socket(monkeypatch, wire_request):
    connect = AsyncMock()
    monkeypatch.setattr(public_web, "open_public_connection", connect)
    async with PublicWebProxy() as proxy:
        reader, writer = await asyncio.open_connection("127.0.0.1", urlsplit(proxy.url).port)
        try:
            writer.write(wire_request)
            await writer.drain()
            assert b"403 Forbidden" in await reader.read()
            with pytest.raises(PublicWebBlockedError):
                proxy.raise_if_blocked()
        finally:
            writer.close()
            await writer.wait_closed()
    connect.assert_not_called()
    assert not proxy._tasks


async def test_real_http_redirect_to_private_address_is_terminal(monkeypatch):
    seen = []

    async def website(reader, writer):
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/secret\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(website, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def connect(host, target_port):
        assert host == "public.example", "the private redirect must never reach the connector"
        return await asyncio.open_connection("127.0.0.1", port)

    monkeypatch.setattr(public_web, "open_public_connection", connect)
    try:
        with pytest.raises(PublicWebBlockedError):
            await acquire.fetch_plain("http://public.example/start")
        assert len(seen) == 1
        assert seen[0].startswith(b"GET /start HTTP/1.1")
    finally:
        server.close()
        await server.wait_closed()


async def test_plain_get_returns_real_html_through_guard(monkeypatch):
    async def website(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\nConnection: close\r\n\r\n<p>hello</p>")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(website, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def connect(host, target_port):
        assert host == "public.example"
        return await asyncio.open_connection("127.0.0.1", port)

    monkeypatch.setattr(public_web, "open_public_connection", connect)
    try:
        assert await acquire.fetch_plain("http://public.example/") == (200, "<p>hello</p>")
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("profile", [None, "/tmp/test-browser-profile"])
async def test_browser_launch_enforces_proxy_for_profiles_and_temporary_contexts(monkeypatch, profile):
    import sys
    from types import SimpleNamespace

    page = SimpleNamespace(is_closed=lambda: False)
    browser = SimpleNamespace(new_context=AsyncMock(), close=AsyncMock())
    context = SimpleNamespace(browser=browser, pages=[page], new_page=AsyncMock(return_value=page), close=AsyncMock())
    page.context = context
    browser.new_context.return_value = context
    chromium = SimpleNamespace(
        launch=AsyncMock(return_value=browser),
        launch_persistent_context=AsyncMock(return_value=context),
    )
    playwright = SimpleNamespace(chromium=chromium, stop=AsyncMock())
    module = SimpleNamespace(async_playwright=lambda: SimpleNamespace(start=AsyncMock(return_value=playwright)))
    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    for name in ("_pw", "_browser", "_page", "_profile_dir", "_proxy"):
        monkeypatch.setattr(engine, name, None)
    try:
        assert await engine._ensure_browser(profile) is page
        call = chromium.launch_persistent_context.call_args if profile else chromium.launch.call_args
        assert call.kwargs["proxy"] == {"server": engine._proxy.url, "bypass": "<-loopback>"}
        assert set(public_web.BROWSER_PROXY_ARGS) <= set(call.kwargs["args"])
    finally:
        await engine.close()
    assert engine._proxy is None
    playwright.stop.assert_awaited_once()


async def test_proxy_tunnels_public_connections_and_closes_them(monkeypatch):
    async def echo(reader, writer):
        data = await reader.read(4)
        writer.write(data)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def connect(host, target_port):
        assert (host, target_port) == ("public.example", 443)
        return await asyncio.open_connection("127.0.0.1", port)

    monkeypatch.setattr(public_web, "open_public_connection", connect)
    try:
        async with PublicWebProxy() as proxy:
            reader, writer = await asyncio.open_connection("127.0.0.1", urlsplit(proxy.url).port)
            try:
                writer.write(b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
                await writer.drain()
                assert b"200 Connection Established" in await reader.readuntil(b"\r\n\r\n")
                writer.write(b"test")
                await writer.drain()
                assert await reader.readexactly(4) == b"test"
            finally:
                writer.close()
                await writer.wait_closed()
        assert not proxy._tasks
    finally:
        server.close()
        await server.wait_closed()

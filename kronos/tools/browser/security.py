"""SSRF protection for browser navigation.

Blocks navigation to private/internal networks to prevent
server-side request forgery attacks.
"""

import ipaddress
import logging
import re
from urllib.parse import urlparse

log = logging.getLogger("kronos.tools.browser.security")

# Blocked URL patterns
_BLOCKED_SCHEMES = {"file", "ftp", "javascript", "data", "blob"}

# Private IP ranges (RFC 1918 + loopback + link-local)
_PRIVATE_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_BLOCKED_HOSTS = {
    "localhost",
    "metadata.google.internal",
    "169.254.169.254",  # cloud metadata
}


def is_url_safe(url: str) -> tuple[bool, str]:
    """Check if URL is safe to navigate to.

    Returns (is_safe, reason).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL"

    # Check scheme
    scheme = (parsed.scheme or "").lower()
    if scheme in _BLOCKED_SCHEMES:
        return False, f"Blocked scheme: {scheme}"
    if scheme not in ("http", "https", ""):
        return False, f"Unsupported scheme: {scheme}"

    # Check host
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "No hostname"

    if host in _BLOCKED_HOSTS:
        return False, f"Blocked host: {host}"

    # Check if host is an IP in private range
    try:
        ip = ipaddress.ip_address(host)
        for network in _PRIVATE_RANGES:
            if ip in network:
                return False, f"Private IP: {host}"
    except ValueError:
        pass  # Not an IP, it's a hostname — OK

    return True, ""

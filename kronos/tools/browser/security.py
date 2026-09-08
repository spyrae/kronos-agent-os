"""Static navigation guard; the public-web proxy enforces DNS and subrequests."""

from kronos.security.public_web import PublicWebBlockedError, validate_public_url


def is_url_safe(url: str) -> tuple[bool, str]:
    """Validate a URL before navigation; DNS is checked at connection time."""
    try:
        validate_public_url(url)
    except PublicWebBlockedError as exc:
        return False, str(exc)
    return True, ""

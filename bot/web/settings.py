"""Validation and URL helpers for the WEB_BASE_URL setting."""

from urllib.parse import urlsplit

LOCAL_HTTP_HOSTS = ("localhost", "127.0.0.1")


def validate_web_config(base_url: str, allowed_user_ids: set[int]) -> str | None:
    """Return an error text when the board must not start, else None."""
    if not allowed_user_ids:
        return "The web board requires ALLOWED_USER_IDS to be set."
    try:
        parts = urlsplit(base_url)
        hostname = parts.hostname
        parts.port  # raises ValueError for a malformed port
    except ValueError:
        return f"WEB_BASE_URL is not a valid URL: {base_url!r}"
    if parts.scheme not in ("http", "https") or not hostname:
        return f"WEB_BASE_URL is not a valid URL: {base_url!r}"
    if parts.scheme == "http" and hostname not in LOCAL_HTTP_HOSTS:
        return "WEB_BASE_URL must use https (http is allowed only for localhost and 127.0.0.1)."
    if parts.path or parts.query or parts.fragment:
        return "WEB_BASE_URL must not have a path, query or fragment."
    return None


def origin_of(base_url: str) -> str:
    """Scheme plus host[:port], the value browsers send in the Origin header."""
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}"


def build_login_url(base_url: str, token: str) -> str:
    return f"{base_url}/login?t={token}"

"""Validation and URL helpers for the WEB_BASE_URL setting."""

from urllib.parse import urlsplit

LOCAL_HTTP_HOSTS = ("localhost", "127.0.0.1")
_DEFAULT_PORTS = {"http": 80, "https": 443}


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
    if "@" in parts.netloc:
        return "WEB_BASE_URL must not contain a user name or password."
    if parts.scheme == "http" and hostname not in LOCAL_HTTP_HOSTS:
        return "WEB_BASE_URL must use https (http is allowed only for localhost and 127.0.0.1)."
    if parts.path or parts.query or parts.fragment:
        return "WEB_BASE_URL must not have a path, query or fragment."
    try:
        _ascii_host(hostname)
    except UnicodeError:
        return (
            f"WEB_BASE_URL host {hostname!r} has no punycode (IDNA) form: "
            "every part between dots must be 1 to 63 characters long."
        )
    return None


def _ascii_host(host: str) -> str:
    """A non-ASCII (international) host in its IDNA punycode form; ASCII hosts are returned as is.

    Raises UnicodeError when the host cannot be encoded (an empty label or one over 63 characters).
    """
    if host.isascii():
        return host
    return host.encode("idna").decode("ascii")


def origin_of(base_url: str) -> str:
    """The value browsers send in the Origin header for pages on `base_url`.

    Lowercase scheme and host (an international host in its punycode form),
    the port only when it is not the scheme's default, and never a user name
    or password.
    """
    parts = urlsplit(base_url)
    host = _ascii_host(parts.hostname or "")
    if ":" in host:  # an IPv6 literal keeps its brackets
        host = f"[{host}]"
    port = parts.port
    if port is not None and port != _DEFAULT_PORTS.get(parts.scheme):
        host = f"{host}:{port}"
    return f"{parts.scheme}://{host}"


def build_login_url(base_url: str, token: str) -> str:
    return f"{base_url}/login?t={token}"

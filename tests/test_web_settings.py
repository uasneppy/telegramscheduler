"""Web board configuration validation and URL helpers."""

from bot.web.settings import build_login_url, origin_of, validate_web_config


def test_validate_requires_allowlist():
    error = validate_web_config("https://b.example.com", set())
    assert error is not None
    assert "ALLOWED_USER_IDS" in error


def test_validate_requires_https():
    error = validate_web_config("http://b.example.com", {1})
    assert error is not None
    assert "https" in error


def test_validate_allows_local_http():
    assert validate_web_config("http://localhost:8080", {1}) is None
    assert validate_web_config("http://127.0.0.1:8080", {1}) is None


def test_validate_rejects_path_and_garbage():
    assert validate_web_config("https://b.example.com/board", {1}) is not None
    assert validate_web_config("not a url", {1}) is not None


def test_validate_ok():
    assert validate_web_config("https://b.example.com", {1}) is None


def test_validate_rejects_userinfo():
    for url in (
        "https://user:pw@b.example.com",
        "https://user@b.example.com",
        "https://@b.example.com",
        "http://user:pw@localhost:8080",
    ):
        error = validate_web_config(url, {1})
        assert error is not None, url
        assert "user name or password" in error


def test_origin_of():
    assert origin_of("https://b.example.com:8443") == "https://b.example.com:8443"
    assert origin_of("http://localhost:8080") == "http://localhost:8080"


def test_origin_of_matches_browser_origin():
    # Browsers send a lowercase host and omit the scheme's default port.
    assert origin_of("HTTPS://Board.Example.COM") == "https://board.example.com"
    assert origin_of("https://b.example.com:443") == "https://b.example.com"
    assert origin_of("http://LOCALHOST:80") == "http://localhost"
    # A port is only dropped when it is the default for that scheme.
    assert origin_of("https://b.example.com:80") == "https://b.example.com:80"
    assert origin_of("http://127.0.0.1:443") == "http://127.0.0.1:443"
    assert origin_of("https://[2001:DB8::1]:443") == "https://[2001:db8::1]"
    assert origin_of("https://[2001:db8::1]:8443") == "https://[2001:db8::1]:8443"


def test_origin_of_unicode_host_is_punycode():
    # Browsers send the IDNA (punycode) form of a Unicode host in the Origin header.
    assert origin_of("https://пример.укр") == "https://xn--e1afmkfd.xn--j1amh"
    assert origin_of("https://Board.ПРИМЕР.Укр") == "https://board.xn--e1afmkfd.xn--j1amh"
    assert origin_of("https://пример.укр:443") == "https://xn--e1afmkfd.xn--j1amh"
    assert origin_of("https://пример.укр:8443") == "https://xn--e1afmkfd.xn--j1amh:8443"
    assert origin_of("https://xn--e1afmkfd.xn--j1amh") == "https://xn--e1afmkfd.xn--j1amh"


def test_validate_accepts_unicode_host():
    assert validate_web_config("https://пример.укр", {1}) is None
    assert validate_web_config("https://board.пример.укр:8443", {1}) is None


def test_validate_rejects_unicode_host_without_an_ascii_form():
    for url in ("https://пример..укр", f"https://{'я' * 64}.укр"):
        error = validate_web_config(url, {1})
        assert error is not None, url
        assert "punycode" in error and "1 to 63 characters" in error


def test_origin_of_never_includes_userinfo():
    assert origin_of("https://user:pw@b.example.com:8443") == "https://b.example.com:8443"
    assert origin_of("https://user@b.example.com") == "https://b.example.com"


def test_build_login_url():
    assert build_login_url("https://b.example.com", "abc") == "https://b.example.com/login?t=abc"

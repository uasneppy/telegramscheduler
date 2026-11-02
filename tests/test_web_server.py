"""Web board server: security headers, CSRF gates, sign-in, sessions, errors, static files, /api/me."""

import asyncio
import html
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta

from aiohttp import web
from aiohttp.http_exceptions import ContentLengthError, TransferEncodingError
from yarl import URL

from bot.database import Database, parse_dt
from bot.utils import get_current_kyiv_time
from bot.web import auth, server
from bot.web.auth import FAIL_LIMIT, SESSION_TTL_SECONDS
from bot.web.errors import WebError, conflict, not_found
from bot.web.server import CSP, SESSION_COOKIE, create_app
from bot.web.settings import origin_of
from web_helpers import FakeScheduler, add_channel, app_client, board_client, insert_post, sign_in

BASE = "http://localhost:8080"
ORIGIN = {"Origin": BASE}
API_WRITE = {"Origin": BASE, "X-Board": "1"}
EXPECTED_CSP = (
    "default-src 'self'; img-src 'self' data:; media-src 'self'; script-src 'self'; "
    "style-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)
# The board's front-end ES modules, served under /static/.
STATIC_MODULES = ("api.js", "list.js", "main.js", "panel.js", "state.js", "time.js")
IMPORT_FROM = re.compile(r"""\bfrom\s+['"]\./([\w.-]+)['"]""")


def _app(allowed=frozenset({1}), base_url=BASE, on_error=None):
    return create_app(FakeScheduler(), base_url, allowed, on_error=on_error)


def _session_rows():
    conn = Database.get_connection()
    try:
        return conn.execute("SELECT user_id FROM web_sessions").fetchall()
    finally:
        conn.close()


def _assert_security_headers(response):
    assert response.headers["Content-Security-Policy"] == EXPECTED_CSP
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def _cookie_attributes(set_cookie: str) -> dict[str, str]:
    """The attributes of one Set-Cookie header, lowercased name -> value (name=value pair excluded)."""
    attributes = {}
    for part in set_cookie.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        attributes[name.lower()] = value
    return attributes


def _assert_no_inline_code(page: str):
    # The CSP forbids inline scripts and styles, so the pages must not rely on them.
    assert INLINE_SCRIPT.search(page) is None
    assert "<style" not in page.lower() and "style=" not in page.lower()


# --- headers ----------------------------------------------------------------------


def test_security_headers_on_every_response():
    assert CSP == EXPECTED_CSP

    async def scenario():
        async with app_client(_app()) as client:
            checks = [
                (await client.get("/"), 200),
                (await client.get("/login?t=abc"), 200),
                (await client.get("/login"), 400),
                (await client.get("/api/me"), 401),
                (await client.get("/no-such-page"), 404),
                (await client.post("/login", data={"t": "abc"}), 403),
                (await client.post("/login", data={"t": "abc"}, headers=ORIGIN), 401),
            ]
            for response, status in checks:
                assert response.status == status, response.url
                _assert_security_headers(response)
            assert checks[3][0].headers["Cache-Control"] == "no-store"

        async with board_client() as client:
            me = await client.get("/api/me")
            assert me.status == 200
            _assert_security_headers(me)
            assert me.headers["Cache-Control"] == "no-store"
            index = await client.get("/")
            assert index.status == 200
            _assert_security_headers(index)

    asyncio.run(scenario())


# --- sign-in ----------------------------------------------------------------------


def test_get_login_does_not_consume_token():
    async def scenario():
        token = auth.create_login_token(1)
        async with app_client(_app()) as client:
            page = await client.get(f"/login?t={token}")
            assert page.status == 200
            assert page.content_type == "text/html"
            assert page.headers["Cache-Control"] == "no-store"
            body = await page.text()
            assert '<form method="post" action="/login">' in body
            assert f'<input type="hidden" name="t" value="{token}">' in body
            # Under Referrer-Policy: no-referrer a browser sends "Origin: null" with a
            # form POST; this document-level policy keeps the real Origin (and no path).
            assert '<meta name="referrer" content="strict-origin">' in body
            _assert_no_inline_code(body)

            # A second GET (a link preview, a scanner) does not burn the token either.
            assert (await client.get(f"/login?t={token}")).status == 200
            response = await client.post("/login", data={"t": token}, headers=ORIGIN, allow_redirects=False)
            assert response.status == 303

            for url in ("/login", "/login?t="):
                missing = await client.get(url)
                assert missing.status == 400
                assert "/web" in await missing.text()

    asyncio.run(scenario())


def test_login_page_escapes_token():
    hostile = '"><script>alert(1)</script>'

    async def scenario():
        async with app_client(_app()) as client:
            page = await client.get("/login", params={"t": hostile})
            assert page.status == 200
            return await page.text()

    body = asyncio.run(scenario())
    assert html.escape(hostile, quote=True) in body
    assert "<script>" not in body


def test_post_login_sets_cookie():
    async def login(base_url):
        async with app_client(_app(base_url=base_url)) as client:
            response = await sign_in(client, 1, origin_of(base_url))
            assert response.status == 303
            assert response.headers["Location"] == "/"
            raw = response.headers.getall("Set-Cookie")
            assert len(raw) == 1
            assert raw[0].startswith(f"{SESSION_COOKIE}=")
            assert auth.get_session_user(response.cookies[SESSION_COOKIE].value) == 1
            return _cookie_attributes(raw[0])

    http_attributes = asyncio.run(login(BASE))
    https_attributes = asyncio.run(login("https://board.example.com"))
    for attributes in (http_attributes, https_attributes):
        assert "httponly" in attributes
        assert attributes["samesite"] == "Lax"
        assert attributes["path"] == "/"
        assert attributes["max-age"] == str(SESSION_TTL_SECONDS)
    assert "secure" not in http_attributes
    assert "secure" in https_attributes


def test_post_login_invalid_and_garbage():
    async def scenario():
        used = auth.create_login_token(1)
        assert auth.consume_login_token(used) == 1
        async with app_client(_app()) as client:
            for value in (secrets.token_urlsafe(32), used, "x" * 10_000, "токен", ""):
                response = await client.post("/login", data={"t": value}, headers=ORIGIN, allow_redirects=False)
                assert response.status == 401, value[:20]
                assert SESSION_COOKIE not in response.cookies
                assert "/web" in await response.text()
            no_field = await client.post("/login", data={"other": "1"}, headers=ORIGIN)
            assert no_field.status == 401
            # Still serving normally afterwards.
            assert (await client.get("/")).status == 200
        assert _session_rows() == []

    asyncio.run(scenario())


def test_post_login_malformed_body_is_a_failed_attempt():
    # Origin is trivial to forge outside a browser, so anyone can send these;
    # they must not become 500s that page the admin through on_error.
    reported = []

    async def on_error(exc):
        reported.append(exc)

    bodies = [
        ("multipart/form-data", b"garbage"),
        ("multipart/form-data; boundary=xyz", b"garbage"),
        ("multipart/form-data; boundary=xyz", b"--xyz\r\nContent-Disposition: form-data\r\n\r\nv\r\n--xyz--\r\n"),
        ("application/x-www-form-urlencoded; charset=no-such-charset", b"t=abc"),
        ("application/x-www-form-urlencoded", b"t=\xff\xfe"),
    ]

    async def scenario():
        async with app_client(_app(on_error=on_error)) as client:
            for content_type, body in bodies:
                response = await client.post(
                    "/login", data=body, headers={**ORIGIN, "Content-Type": content_type}, allow_redirects=False
                )
                assert response.status == 401, content_type

    asyncio.run(scenario())
    assert reported == []


def test_login_rate_limit():
    async def scenario():
        async with app_client(_app()) as client:
            for _ in range(FAIL_LIMIT):
                response = await client.post("/login", data={"t": secrets.token_urlsafe(32)}, headers=ORIGIN)
                assert response.status == 401
            token = auth.create_login_token(1)
            blocked = await client.post("/login", data={"t": token}, headers=ORIGIN, allow_redirects=False)
            assert blocked.status == 429
            assert SESSION_COOKIE not in blocked.cookies
            return token

    token = asyncio.run(scenario())
    # The limiter is checked before the token is read, so the valid token is still unused.
    assert auth.consume_login_token(token) == 1
    assert _session_rows() == []


def test_login_requires_matching_origin():
    async def scenario():
        token = auth.create_login_token(1)
        async with app_client(_app()) as client:
            for headers in (
                {},
                {"Origin": "http://evil.example"},
                {"Origin": "null"},
                {"Origin": "http://localhost:8081"},
                {"Origin": "https://localhost:8080"},
            ):
                response = await client.post("/login", data={"t": token}, headers=headers, allow_redirects=False)
                assert response.status == 403, headers
                assert (await response.json())["error"] == "forbidden"
        return token

    token = asyncio.run(scenario())
    assert auth.consume_login_token(token) == 1  # refused before the token was read
    assert _session_rows() == []


def test_login_origin_compares_normalized_base_url():
    # An operator may write the base URL with capitals or the default port;
    # browsers send the lowercase origin without it.
    async def scenario():
        async with app_client(_app(base_url="https://Board.Example.com:443")) as client:
            response = await sign_in(client, 1, "https://board.example.com")
            assert response.status == 303

    asyncio.run(scenario())


def test_login_user_not_allowed():
    async def scenario():
        async with app_client(_app(allowed={1})) as client:
            response = await sign_in(client, 99, BASE)
            assert response.status == 403
            assert SESSION_COOKIE not in response.cookies

    asyncio.run(scenario())
    assert _session_rows() == []


def test_token_and_cookie_never_logged(caplog):
    async def scenario():
        async with app_client(_app()) as client:
            bad = secrets.token_urlsafe(32)
            assert (await client.post("/login", data={"t": bad}, headers=ORIGIN)).status == 401
            token = auth.create_login_token(1)
            assert (await client.get(f"/login?t={token}")).status == 200
            response = await client.post("/login", data={"t": token}, headers=ORIGIN, allow_redirects=False)
            assert response.status == 303
            session_id = response.cookies[SESSION_COOKIE].value
            assert (await client.get("/api/me")).status == 200
            assert (await client.post("/logout", headers=ORIGIN, allow_redirects=False)).status == 303
            return [bad, token, session_id]

    with caplog.at_level(logging.DEBUG):
        secret_values = asyncio.run(scenario())
    assert "user 1" in caplog.text  # sign-in and sign-out are logged ...
    for value in secret_values:
        assert value not in caplog.text  # ... without the token or the session id


# --- sessions ---------------------------------------------------------------------


def test_api_requires_session():
    expired = auth.create_session(1, now=get_current_kyiv_time() - timedelta(days=8))

    async def scenario():
        async with app_client(_app()) as client:
            for cookie in (None, "garbage", secrets.token_urlsafe(32), expired):
                headers = {} if cookie is None else {"Cookie": f"{SESSION_COOKIE}={cookie}"}
                response = await client.get("/api/me", headers=headers)
                assert response.status == 401, cookie
                assert response.content_type == "application/json"
                body = await response.json()
                assert body["error"] == "unauthorized"
                assert body["message"]

    asyncio.run(scenario())


def test_allowlist_rechecked_each_request():
    allowed = {1}

    async def scenario():
        async with board_client(allowed=allowed) as client:
            # Kept by reference, so a change to the caller's set takes effect at once.
            assert client.app["allowed"] is allowed
            assert (await client.get("/api/me")).status == 200
            client.app["allowed"].discard(1)
            for path in ("/api/me", "/", "/login?t=abc"):
                response = await client.get(path)
                assert response.status == 403, path
                assert (await response.json())["error"] == "forbidden"
            client.app["allowed"].add(1)
            assert (await client.get("/api/me")).status == 200

    asyncio.run(scenario())


def test_csrf_on_api_posts():
    async def scenario():
        async with board_client() as client:
            refused = [
                ("POST", {"Origin": "http://evil.example", "X-Board": "1"}),
                ("POST", {"Origin": BASE}),
                ("POST", {"Origin": BASE, "X-Board": "0"}),
                ("POST", {"X-Board": "1"}),
                ("DELETE", {"Origin": BASE}),
                ("PATCH", {"Origin": "null", "X-Board": "1"}),
            ]
            for method, headers in refused:
                response = await client.request(method, "/api/me", headers=headers)
                assert response.status == 403, (method, headers)
                assert (await response.json())["error"] == "forbidden"
            allowed = await client.post("/api/me", headers=API_WRITE)
            assert allowed.status == 405
            assert allowed.content_type == "application/json"
            assert (await client.get("/api/me")).status == 200

    asyncio.run(scenario())


def test_logout_ends_session():
    async def scenario():
        async with board_client() as client:
            session_id = client.session.cookie_jar.filter_cookies(client.make_url("/"))[SESSION_COOKIE].value
            # Without a matching Origin the session survives.
            assert (await client.post("/logout")).status == 403
            assert (await client.get("/api/me")).status == 200

            response = await client.post("/logout", headers=ORIGIN, allow_redirects=False)
            assert response.status == 303
            assert response.headers["Location"] == "/"
            assert response.cookies[SESSION_COOKIE].value == ""
            assert (await client.get("/api/me")).status == 401
            # The row is gone, so replaying the old cookie does not help.
            replay = await client.get("/api/me", headers={"Cookie": f"{SESSION_COOKIE}={session_id}"})
            assert replay.status == 401
            return session_id

    session_id = asyncio.run(scenario())
    assert auth.get_session_user(session_id) is None


# --- errors -----------------------------------------------------------------------


def test_unexpected_error_is_json_500_and_reported(caplog):
    reported = []

    async def on_error(exc):
        reported.append(exc)

    async def broken_reporter(exc):
        raise RuntimeError("reporter down")

    async def boom(request):
        raise RuntimeError("secret detail")

    async def scenario(reporter):
        app = _app(on_error=reporter)
        app.router.add_get("/boom", boom)
        async with app_client(app) as client:
            response = await client.get("/boom")
            assert response.status == 500
            _assert_security_headers(response)
            body = await response.json()
            assert body["error"] == "internal"
            assert "secret detail" not in body["message"]

    with caplog.at_level(logging.ERROR, logger="bot.web.server"):
        asyncio.run(scenario(on_error))
    assert len(reported) == 1
    assert isinstance(reported[0], RuntimeError) and str(reported[0]) == "secret detail"
    assert "/boom" in caplog.text

    # A failing reporter, or none at all, still yields the same 500.
    asyncio.run(scenario(broken_reporter))
    asyncio.run(scenario(None))


# --- client disconnects and unreadable bodies ---------------------------------------

LOGIN_HEAD = "POST /login HTTP/1.1\r\nHost: localhost\r\nOrigin: http://localhost:8080\r\n"
FORM = "Content-Type: application/x-www-form-urlencoded\r\n"


@asynccontextmanager
async def _probed_client(app):
    """A client for `app` plus a queue that an outermost probe middleware fills with
    "started" when a handler begins and "finished" when it returns or raises."""
    events = asyncio.Queue()

    @web.middleware
    async def probe(request, handler):
        events.put_nowait("started")
        try:
            return await handler(request)
        finally:
            events.put_nowait("finished")

    app.middlewares.insert(0, probe)
    async with app_client(app) as client:
        yield client, events


async def _abort_mid_body(client, events, raw: str, more: str = ""):
    """Send a request whose body never completes over a raw socket, then close it.

    The close waits until the handler is running, so it cannot race the dispatch.
    """
    _, writer = await asyncio.open_connection(client.server.host, client.server.port)
    writer.write(raw.encode())
    await writer.drain()
    assert await asyncio.wait_for(events.get(), 5) == "started"
    if more:
        writer.write(more.encode())
        await writer.drain()
    writer.close()
    await writer.wait_closed()
    assert await asyncio.wait_for(events.get(), 5) == "finished"


def _assert_disconnects_are_quiet(
    caplog, raw: str, more: str = "", times: int = 1, add_routes=None, aiohttp_may_log=False
):
    """Aborted requests reach neither on_error nor an ERROR log, and are not failed sign-ins."""
    reported = []

    async def on_error(exc):
        reported.append(exc)

    async def scenario():
        app = _app(on_error=on_error)
        if add_routes is not None:
            add_routes(app)
        async with _probed_client(app) as (client, events):
            for _ in range(times):
                await _abort_mid_body(client, events, raw, more)
            # Not counted by the failed-login limiter: a valid token still signs in.
            assert (await sign_in(client, 1, BASE)).status == 303

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())  # returns after the server has shut down
    assert reported == []
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    if aiohttp_may_log:
        # With the pure-Python parser, aiohttp's own lingering drain logs the bad
        # body again after the board has answered; that log is aiohttp's, not ours.
        errors = [record for record in errors if record.name != "aiohttp.server"]
    assert errors == []


def test_client_disconnect_mid_body_is_not_reported(caplog):
    raw = LOGIN_HEAD + FORM + "Content-Length: 100\r\n\r\nt=abc"
    _assert_disconnects_are_quiet(caplog, raw, times=FAIL_LIMIT)
    gone = [r for r in caplog.records if r.getMessage().startswith("Client went away during POST /login")]
    assert len(gone) == FAIL_LIMIT
    assert all(r.levelno == logging.INFO and r.exc_info is None for r in gone)  # no traceback


def test_truncated_multipart_body_is_not_reported(caplog):
    raw = (
        LOGIN_HEAD
        + "Content-Type: multipart/form-data; boundary=xyz\r\nContent-Length: 500\r\n\r\n"
        + '--xyz\r\nContent-Disposition: form-data; name="t"\r\n\r\nabc'
    )
    _assert_disconnects_are_quiet(caplog, raw)


def test_incomplete_or_bad_chunked_body_then_close_is_not_reported(caplog):
    head = LOGIN_HEAD + FORM + "Transfer-Encoding: chunked\r\n\r\n"
    # A chunk shorter than its announced size, then the client goes away.
    _assert_disconnects_are_quiet(caplog, head + "10\r\nt=ab")
    caplog.clear()
    # A valid chunk, then an invalid chunk size once the handler is reading, then a close.
    # The C parser surfaces this as the disconnect, the pure-Python one as a bad body.
    _assert_disconnects_are_quiet(caplog, head + "3\r\nt=a\r\n", more="zz\r\nhello\r\n", aiohttp_may_log=True)


async def _until_client_gone(request):
    for _ in range(500):
        if request.transport is None or request.transport.is_closing():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the client never went away")


def test_every_disconnect_error_type_is_quiet(caplog):
    # aiohttp 3.14 reports a lost connection as ConnectionResetError, but the other
    # ways a gone peer can surface in a handler are treated the same.
    raised = {
        "reset": lambda: ConnectionResetError("Connection lost"),
        "pipe": lambda: BrokenPipeError(32, "Broken pipe"),
        "aborted": lambda: ConnectionAbortedError("aborted"),
        "incomplete": lambda: asyncio.IncompleteReadError(b"", 100),
    }

    async def raise_after_gone(request):
        await _until_client_gone(request)
        raise raised[request.match_info["kind"]]()

    async def write_after_gone(request):
        response = web.StreamResponse()
        await response.prepare(request)
        await _until_client_gone(request)
        await response.write(b"too late")  # aiohttp's ClientConnectionResetError
        return response

    def add_routes(app):
        app.router.add_get("/gone/write", write_after_gone)
        app.router.add_get("/gone/{kind}", raise_after_gone)

    for kind in (*raised, "write"):
        caplog.clear()
        raw = f"GET /gone/{kind} HTTP/1.1\r\nHost: localhost\r\n\r\n"
        _assert_disconnects_are_quiet(caplog, raw, add_routes=add_routes)
        assert f"Client went away during GET /gone/{kind}" in caplog.text, kind


def test_connection_error_with_client_still_connected_is_reported(caplog):
    # Control: a connection error is only a client disconnect when the client is gone.
    # Raised while the browser is still waiting (say, a broken upstream), it is a real fault.
    reported = []

    async def on_error(exc):
        reported.append(exc)

    async def upstream_down(request):
        raise ConnectionRefusedError("upstream down")

    async def scenario():
        app = _app(on_error=on_error)
        app.router.add_get("/upstream", upstream_down)
        async with app_client(app) as client:
            response = await client.get("/upstream")
            assert response.status == 500
            assert (await response.json())["error"] == "internal"

    with caplog.at_level(logging.ERROR, logger="bot.web.server"):
        asyncio.run(scenario())
    assert len(reported) == 1 and isinstance(reported[0], ConnectionRefusedError)
    assert "Unexpected web board error on GET /upstream" in caplog.text


def test_undecodable_body_is_400_and_not_reported(caplog):
    # A body that aiohttp cannot decode is the client's fault, sent with the
    # connection still open; it must not page the admin either.
    reported = []

    async def on_error(exc):
        reported.append(exc)

    # The pure-Python HTTP parser (no C extension, or AIOHTTP_NO_EXTENSIONS=1)
    # hands a bad chunked or short body to the handler as these, unwrapped.
    parser_errors = {
        "chunk": lambda: TransferEncodingError("zz"),
        "length": lambda: ContentLengthError("Not enough data to satisfy content length header."),
    }

    async def read_bad_body(request):
        raise parser_errors[request.match_info["kind"]]()

    async def scenario():
        app = _app(on_error=on_error)
        app.router.add_get("/bad-body/{kind}", read_bad_body)
        async with app_client(app) as client:
            for encoding in ("gzip", "deflate"):
                response = await client.post(
                    "/login",
                    data=b"t=this is not compressed",
                    headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded",
                             "Content-Encoding": encoding},
                )
                assert response.status == 400, encoding
                assert (await response.json())["error"] == "invalid_request"
            for kind in parser_errors:
                response = await client.get(f"/bad-body/{kind}")
                assert response.status == 400, kind
                assert (await response.json())["error"] == "invalid_request"

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())
    assert reported == []
    board_errors = [r for r in caplog.records if r.name.startswith("bot") and r.levelno >= logging.ERROR]
    assert board_errors == []


def test_webeerror_maps_to_status():
    errors = {
        "/e/conflict": conflict("The post is being sent", status="sending"),
        "/e/missing": not_found("Post not found"),
        "/e/large": WebError(413, "too_large", "File too large"),
        "/e/invalid": WebError(400, "invalid_request", "Bad time", {"error": "ignored", "field": "time"}),
    }

    async def raise_it(request):
        raise errors[request.path]

    async def scenario():
        app = _app()
        for path in errors:
            app.router.add_get(path, raise_it)
        async with app_client(app) as client:
            bodies = {}
            for path, error in errors.items():
                response = await client.get(path)
                assert response.status == error.status, path
                _assert_security_headers(response)
                bodies[path] = await response.json()
            return bodies

    bodies = asyncio.run(scenario())
    assert bodies["/e/conflict"] == {"error": "conflict", "message": "The post is being sent", "status": "sending"}
    assert bodies["/e/missing"] == {"error": "not_found", "message": "Post not found"}
    assert bodies["/e/large"] == {"error": "too_large", "message": "File too large"}
    # extra cannot replace the code or the message.
    assert bodies["/e/invalid"] == {"error": "invalid_request", "message": "Bad time", "field": "time"}


# --- pages and static files -------------------------------------------------------


def test_index_signed_in_vs_out():
    index = (server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    signed_out = (server.STATIC_DIR / "signed_out.html").read_text(encoding="utf-8")
    assert "/web" in signed_out
    for page in (index, signed_out):
        assert page.lstrip().lower().startswith("<!doctype html>")
        _assert_no_inline_code(page)

    async def scenario():
        async with app_client(_app()) as client:
            for headers in ({}, {"Cookie": f"{SESSION_COOKIE}=garbage"}):
                response = await client.get("/", headers=headers)
                assert response.status == 200
                assert response.content_type == "text/html"
                assert response.headers["Cache-Control"] == "no-store"
                assert await response.text() == signed_out
        async with board_client() as client:
            response = await client.get("/")
            assert response.status == 200
            assert response.content_type == "text/html"
            assert response.headers["Cache-Control"] == "no-store"
            assert await response.text() == index

    asyncio.run(scenario())


def test_static_allowlist_and_traversal(tmp_path, monkeypatch):
    for name in (*STATIC_MODULES, "app.js", "other.js"):
        (tmp_path / name).write_text(f"console.log({name!r});\n", encoding="utf-8")
    (tmp_path / "index.html").write_text("<p>index</p>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(server, "STATIC_DIR", tmp_path)

    async def scenario():
        async with app_client(_app()) as client:
            # Every module is served with a JavaScript type (a module script with any other type is refused).
            for name in STATIC_MODULES:
                js = await client.get(f"/static/{name}")
                assert js.status == 200, name
                assert js.content_type == "text/javascript", name
                assert js.charset == "utf-8", name
                assert await js.text() == f"console.log({name!r});\n"
                _assert_security_headers(js)

            # Allowlisted but not on disk (yet): the usual JSON 404, not 500.
            missing = await client.get("/static/style.css")
            assert missing.status == 404
            assert (await missing.json())["error"] == "not_found"
            (tmp_path / "style.css").write_text("body {}\n", encoding="utf-8")
            css = await client.get("/static/style.css")
            assert css.status == 200
            assert css.content_type == "text/css"

            # Raw paths, sent without client-side normalisation.
            root = str(client.make_url("/"))
            for raw in (
                "static/app.js",  # the retired single script: on disk here, still not served
                "static/other.js",
                "static/index.html",
                "static/signed_out.html",
                "static/secret.txt",
                "static/../config.py",
                "static/..%2Fconfig.py",
                "static/%2e%2e/config.py",
                "static/%2e%2e%2fconfig.py",
                "static/MAIN.JS",
                "static/main.js%00",
                "static/",
            ):
                async with client.session.get(URL(root + raw, encoded=True)) as response:
                    assert response.status == 404, raw
                    body = await response.read()
                    assert b"secret" not in body and b"BOT_TOKEN" not in body, raw

    asyncio.run(scenario())


def test_static_allowlist_covers_the_board_modules():
    """Every module on disk is allowlisted, every import resolves, and the page loads main.js."""
    on_disk = {path.name for path in server.STATIC_DIR.glob("*.js")}
    assert on_disk == set(STATIC_MODULES)
    assert on_disk == {name for name in server._STATIC_FILES if name.endswith(".js")}
    for name in sorted(on_disk):
        source = (server.STATIC_DIR / name).read_text(encoding="utf-8")
        for imported in IMPORT_FROM.findall(source):
            assert imported in on_disk, f"{name} imports ./{imported}"
    index = (server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert index.count("<script") == 1
    assert '<script type="module" src="/static/main.js"></script>' in index


# --- /api/me ----------------------------------------------------------------------


def test_me_payload():
    add_channel(1, "-1001", "Alpha", signature="— Alpha")
    add_channel(1, "-1002", "Beta")
    add_channel(1, "-1009", "Gone")
    assert Database.remove_user_channel(1, "-1009")
    add_channel(2, "-1003", "Other")
    future = get_current_kyiv_time() + timedelta(days=1)
    insert_post(1, "pending", scheduled=future, channel_id="-1001")
    insert_post(1, "pending")
    insert_post(1, "failed")
    insert_post(1, "posted")
    insert_post(1, "posted")
    insert_post(2, "pending", scheduled=future)

    async def scenario():
        async with board_client() as client:
            response = await client.get("/api/me")
            assert response.status == 200
            assert response.content_type == "application/json"
            return await response.json()

    before = get_current_kyiv_time()
    body = asyncio.run(scenario())
    assert body["user_id"] == 1
    assert abs((parse_dt(body["now"]) - before).total_seconds()) < 60
    # Active channels only (Ruling 11).
    assert body["channels"] == [
        {"channel_id": "-1001", "channel_name": "Alpha", "signature": "— Alpha", "is_active": True},
        {"channel_id": "-1002", "channel_name": "Beta", "signature": None, "is_active": True},
    ]
    assert body["counts"] == {"scheduled": 1, "queued": 1, "failed": 1, "posted": 2}

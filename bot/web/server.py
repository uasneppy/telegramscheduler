"""The web board's aiohttp application: sign-in, sessions, security middlewares, /api/me.

Middlewares, outermost first:

1. errors: WebError -> its status with JSON `{"error", "message", **extra}`;
   aiohttp's own HTTP errors (404, 405, 413, ...) -> the same JSON shape;
   a body aiohttp cannot decode -> 400 `invalid_request`; a client that went
   away mid-request -> an info line only. Neither is reported, so a client
   cannot page the admin. Anything else -> logged with a traceback, reported
   through `on_error`, JSON 500 `internal`.
2. security headers: CSP, nosniff and no-referrer on every response, and
   `Cache-Control: no-store` on /api unless the route set its own. The errors
   middleware sits outside it, so it adds the same headers to the responses it builds.
3. origin: every request other than GET/HEAD needs `Origin` equal to the board's
   origin; such requests to /api also need `X-Board: 1`.
4. session: resolves the `tgs_session` cookie to `request["user_id"]` (None when
   signed out). A user no longer in the allowlist gets 403 on every request; /api
   without a session gets 401.

The login token (`?t=`, form field `t`) and the session cookie are never logged:
log lines use `request.path`, which has no query string. The access log is
turned off by the runner (`access_log=None`), and `start_web` puts a redacting
filter on aiohttp's own `aiohttp.server` logger, which prints the raw request
line when it rejects a malformed request. Blocking auth and database calls
run through `asyncio.to_thread`.

`start_web` / `stop_web` run the app on the bot's event loop (called from
`main.post_init` / `post_shutdown`). Starting is opt-in and never raises: a
board that cannot start is logged and the bot carries on without it. The only
path to the admin chat is `on_error`, rate limited to one message per 30 s.
"""

import asyncio
import html
import logging
import re
import time
import traceback
import warnings
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from aiohttp import web
from aiohttp.http_exceptions import HttpProcessingError
from aiohttp.web_protocol import RequestPayloadError

import config
from bot.web import api, auth, queries
from bot.web.errors import WebError, not_found
from bot.web.service import SchedulerLike
from bot.web.settings import origin_of, validate_web_config

logger = logging.getLogger(__name__)

SESSION_COOKIE = "tgs_session"
CSP = (
    "default-src 'self'; img-src 'self' data:; media-src 'self'; script-src 'self'; "
    "style-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
STATIC_DIR = Path(__file__).resolve().parent / "static"
# The only files served under /static/, with their content types. The board's script
# is a set of ES modules (main.js imports the rest); a module script with any content
# type other than JavaScript is refused by the browser.
_JS = "text/javascript; charset=utf-8"
_STATIC_FILES = {
    "api.js": _JS,
    "list.js": _JS,
    "main.js": _JS,
    "panel.js": _JS,
    "state.js": _JS,
    "time.js": _JS,
    "style.css": "text/css; charset=utf-8",
}
_HTML = "text/html; charset=utf-8"
_SAFE_METHODS = ("GET", "HEAD")
# Codes for aiohttp's own HTTP errors (unknown route, wrong method, body too large).
_HTTP_ERROR_CODES = {
    400: "invalid_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "too_large",
    429: "rate_limited",
}

# A form POST from a page served with `Referrer-Policy: no-referrer` carries
# `Origin: null` (Fetch standard), which the origin check must refuse. The
# confirm page therefore sets its own document policy: the real Origin is sent
# and a Referer never carries more than the bare origin, so `?t=` cannot leak.
_REFERRER_META = '<meta name="referrer" content="strict-origin">'

ErrorReporter = Callable[[BaseException], Awaitable[None]]

# What a handler sees when the request body is malformed: the C parser wraps the
# fault in RequestPayloadError, the pure-Python parser raises its own
# HttpProcessingError (TransferEncodingError, ContentLengthError, ...).
_BAD_BODY_ERRORS = (RequestPayloadError, HttpProcessingError)
# What a handler sees when the client goes away mid-request: aiohttp sets
# ConnectionResetError on the request body, a write to the closed transport
# raises a ConnectionResetError subclass, readexactly raises IncompleteReadError.
_DISCONNECT_ERRORS = (ConnectionError, asyncio.IncompleteReadError)


def _set_key(mapping, key: str, value) -> None:
    """Store a plain string key on an Application or Request.

    The board's interface uses string keys (`app["allowed"]`, `request["user_id"]`);
    aiohttp >= 3.9 warns on each such assignment, so the warning is silenced here only.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", web.NotAppKeyWarning)
        mapping[key] = value


def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def _client_gone(request: web.Request) -> bool:
    transport = request.transport
    return transport is None or transport.is_closing()


def _json_error(status: int, code: str, message: str, extra: dict | None = None) -> web.Response:
    # extra sits next to error and message but can never replace them.
    return web.json_response({**(extra or {}), "error": code, "message": message}, status=status)


def _apply_security_headers(request: web.Request, response: web.StreamResponse) -> None:
    headers = response.headers
    # A route may set its own CSP (media responses use `sandbox`).
    headers.setdefault("Content-Security-Policy", CSP)
    headers["X-Content-Type-Options"] = "nosniff"
    headers["Referrer-Policy"] = "no-referrer"
    if _is_api(request.path):
        headers.setdefault("Cache-Control", "no-store")


# --- middlewares --------------------------------------------------------------------


def _errors_middleware(on_error: ErrorReporter | None):
    @web.middleware
    async def errors(request: web.Request, handler):
        try:
            return await handler(request)
        except WebError as exc:
            response = _json_error(exc.status, exc.code, exc.message, exc.extra)
        except web.HTTPException as exc:
            if exc.status < 400:
                _apply_security_headers(request, exc)
                raise
            code = _HTTP_ERROR_CODES.get(exc.status, "invalid_request" if exc.status < 500 else "internal")
            response = _json_error(exc.status, code, exc.reason)
            if "Allow" in exc.headers:
                response.headers["Allow"] = exc.headers["Allow"]
        except _BAD_BODY_ERRORS as exc:
            # The body could not be decoded (bad Content-Encoding data, bad chunking):
            # the client's fault, so no traceback and no report. Only the type is
            # logged, since the message may quote request bytes.
            logger.info(
                "Unreadable request body on %s %s (%s)", request.method, request.path, type(exc).__name__
            )
            response = _json_error(400, "invalid_request", "The request body could not be read.")
            # aiohttp drops the connection after a body it cannot drain; say so to the client.
            response.force_close()
        except Exception as exc:
            if isinstance(exc, _DISCONNECT_ERRORS) and _client_gone(request):
                # Nobody is left to answer, and a client that aborts must not page the
                # admin. Returning (not raising) keeps aiohttp from logging it as an error;
                # it drops the response on the closed connection.
                logger.info(
                    "Client went away during %s %s (%s)", request.method, request.path, type(exc).__name__
                )
                return _json_error(400, "invalid_request", "The request did not complete.")
            logger.exception(
                "Unexpected web board error on %s %s (user %s)",
                request.method, request.path, request.get("user_id"),
            )
            if on_error is not None:
                try:
                    await on_error(exc)
                except Exception:
                    logger.exception("Reporting a web board error failed")
            response = _json_error(500, "internal", "Something went wrong on the server.")
        _apply_security_headers(request, response)
        return response

    return errors


@web.middleware
async def _security_headers_middleware(request: web.Request, handler):
    response = await handler(request)
    _apply_security_headers(request, response)
    return response


@web.middleware
async def _origin_middleware(request: web.Request, handler):
    if request.method not in _SAFE_METHODS:
        if request.headers.get("Origin") != request.app["origin"]:
            raise WebError(403, "forbidden", "Cross-site request refused.")
        if _is_api(request.path) and request.headers.get("X-Board") != "1":
            raise WebError(403, "forbidden", "Missing X-Board header.")
    return await handler(request)


@web.middleware
async def _session_middleware(request: web.Request, handler):
    session_id = request.cookies.get(SESSION_COOKIE)
    user_id = await asyncio.to_thread(auth.get_session_user, session_id) if session_id else None
    if user_id is not None and user_id not in request.app["allowed"]:
        raise WebError(403, "forbidden", "This account may no longer use the board.")
    if user_id is None and _is_api(request.path):
        raise WebError(401, "unauthorized", "Not signed in. Send /web to the bot in Telegram.")
    _set_key(request, "user_id", user_id)
    return await handler(request)


# --- pages --------------------------------------------------------------------------


def _html_page(title: str, body: str, status: int = 200, head: str = "") -> web.Response:
    """A small self-contained page (no inline script or style: the CSP forbids them)."""
    page = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{head}"
        f"<title>{html.escape(title)}</title>\n"
        '<link rel="stylesheet" href="/static/style.css">\n'
        "</head>\n"
        "<body>\n"
        "<main>\n"
        f"<h1>{html.escape(title)}</h1>\n"
        f"{body}\n"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )
    return web.Response(status=status, text=page, headers={"Content-Type": _HTML, "Cache-Control": "no-store"})


def _message_page(status: int, title: str, text: str) -> web.Response:
    return _html_page(title, f"<p>{html.escape(text)}</p>", status=status)


def _confirm_page(token: str) -> web.Response:
    body = (
        "<p>Continue to sign in to the post board.</p>\n"
        '<form method="post" action="/login">\n'
        f'<input type="hidden" name="t" value="{html.escape(token, quote=True)}">\n'
        '<button type="submit">Continue</button>\n'
        "</form>"
    )
    return _html_page("Sign in", body, head=f"{_REFERRER_META}\n")


# --- handlers -----------------------------------------------------------------------


async def _index(request: web.Request) -> web.StreamResponse:
    name = "index.html" if request["user_id"] is not None else "signed_out.html"
    return web.FileResponse(STATIC_DIR / name, headers={"Content-Type": _HTML, "Cache-Control": "no-store"})


async def _static(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    content_type = _STATIC_FILES.get(name)
    if content_type is None:
        raise not_found()
    path = STATIC_DIR / name
    if not await asyncio.to_thread(path.is_file):
        raise not_found()
    return web.FileResponse(path, headers={"Content-Type": content_type, "Cache-Control": "no-cache"})


async def _login_page(request: web.Request) -> web.Response:
    """Show a Continue button; never consumes the token (link previews and scanners GET it)."""
    token = request.query.get("t", "")
    if not token:
        return _message_page(
            400, "Sign-in link incomplete",
            "This sign-in link is missing its code. Send /web to the bot in Telegram to get a new link.",
        )
    return _confirm_page(token)


async def _login(request: web.Request) -> web.Response:
    limiter: auth.LoginRateLimiter = request.app["limiter"]
    if limiter.is_blocked():
        return _message_page(
            429, "Too many attempts",
            "Too many failed sign-in attempts. Wait a few minutes, then send /web to the bot again.",
        )
    try:
        token = (await request.post()).get("t")
    except (ValueError, LookupError):
        # A body aiohttp cannot parse (bad multipart, unknown charset, bad bytes)
        # is a client error like a wrong token, not a server fault to report.
        token = None
    user_id = await asyncio.to_thread(auth.consume_login_token, token)
    if user_id is None:
        limiter.register_failure()
        logger.warning("Web board sign-in failed: invalid, used or expired link")
        return _message_page(
            401, "Sign-in link not valid",
            "This sign-in link is invalid, already used or expired. "
            "Send /web to the bot in Telegram to get a new link.",
        )
    if user_id not in request.app["allowed"]:
        logger.warning("Web board sign-in refused: user %s is not allowed", user_id)
        return _message_page(403, "Not allowed", "This Telegram account may not use the board.")

    session_id = await asyncio.to_thread(auth.create_session, user_id)
    response = web.Response(status=303, headers={"Location": "/"})
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=auth.SESSION_TTL_SECONDS,
        path="/",
        httponly=True,
        samesite="Lax",
        secure=request.app["origin"].startswith("https://"),
    )
    logger.info("Web board sign-in: user %s", user_id)
    return response


async def _logout(request: web.Request) -> web.Response:
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        await asyncio.to_thread(auth.delete_session, session_id)
    if request["user_id"] is not None:
        logger.info("Web board sign-out: user %s", request["user_id"])
    response = web.Response(status=303, headers={"Location": "/"})
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


async def _me(request: web.Request) -> web.Response:
    return web.json_response(await asyncio.to_thread(queries.get_me, request["user_id"]))


# --- factory ------------------------------------------------------------------------


def create_app(
    scheduler: SchedulerLike,
    base_url: str,
    allowed_user_ids: set[int],
    on_error: ErrorReporter | None = None,
) -> web.Application:
    """The board application. `allowed_user_ids` is kept by reference, not copied."""
    app = web.Application(
        middlewares=[
            _errors_middleware(on_error),
            _security_headers_middleware,
            _origin_middleware,
            _session_middleware,
        ]
    )
    _set_key(app, "scheduler", scheduler)
    _set_key(app, "allowed", allowed_user_ids)
    _set_key(app, "origin", origin_of(base_url))
    _set_key(app, "limiter", auth.LoginRateLimiter())

    app.router.add_get("/", _index)
    app.router.add_get("/static/{name}", _static)
    app.router.add_get("/login", _login_page)
    app.router.add_post("/login", _login)
    app.router.add_post("/logout", _logout)
    app.router.add_get("/api/me", _me)
    api.register_routes(app)
    return app


# --- log redaction ------------------------------------------------------------------

_REDACTED = "[redacted]"
# `t=` as a query key (not the tail of another key such as `limit=`) and the session cookie value.
_TOKEN_QUERY = re.compile(r"(?<![A-Za-z0-9_])(t=)[^\s&#'\"\\]*")
_SESSION_VALUE = re.compile(rf"({re.escape(SESSION_COOKIE)}=)[^\s;,'\"\\]*")


def _redact(text: str) -> str:
    """`text` with every login token and session id value replaced."""
    return _SESSION_VALUE.sub(rf"\1{_REDACTED}", _TOKEN_QUERY.sub(rf"\1{_REDACTED}", text))


class _LogRedactionFilter(logging.Filter):
    """Removes login tokens and session ids from log records.

    aiohttp's own logger prints the raw request line (query string included)
    when it rejects a malformed request, usually inside the exception text. The
    message is formatted with its args here and the traceback is rendered here, so
    nothing is left for a handler to print unredacted. Never raises: a record that
    cannot be redacted is withheld instead.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg, record.args = _redact(record.getMessage()), ()
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = _redact(record.exc_text)
            if record.stack_info:
                record.stack_info = _redact(record.stack_info)
        except Exception:
            record.msg, record.args = "<log record withheld: it could not be redacted>", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


def _install_log_redaction() -> None:
    """Put the redacting filter on aiohttp's `aiohttp.server` logger, once."""
    target = logging.getLogger("aiohttp.server")
    if not any(isinstance(f, _LogRedactionFilter) for f in target.filters):
        target.addFilter(_LogRedactionFilter())


# --- admin error reports ------------------------------------------------------------

ADMIN_REPORT_INTERVAL_SECONDS = 30
_ADMIN_REPORT_CHARS = 1500  # the last lines of a traceback carry the actual error


class _AdminBot(Protocol):
    async def send_message(self, chat_id, text): ...


def _admin_reporter(bot: _AdminBot, chat_id, clock: Callable[[], float] = time.monotonic) -> ErrorReporter:
    """An `on_error` that sends the end of the traceback to the admin chat.

    At most one message per `ADMIN_REPORT_INTERVAL_SECONDS`; faults in between are
    only logged, so a fault loop cannot flood the chat or hit Telegram's flood
    limits (which would delay scheduled posts). The window starts when a send is
    attempted, so a failing Telegram is not hammered either.
    """
    last_sent: float | None = None

    async def report(exc: BaseException) -> None:
        nonlocal last_sent
        now = clock()
        if last_sent is not None and now - last_sent < ADMIN_REPORT_INTERVAL_SECONDS:
            logger.warning(
                "Web board error not sent to the admin chat (rate limited): %s", type(exc).__name__
            )
            return
        last_sent = now
        text = "".join(traceback.format_exception(None, exc, exc.__traceback__))
        # Redact before cutting, so a cut can never leave half a token behind.
        summary = _redact(text)[-_ADMIN_REPORT_CHARS:]
        await bot.send_message(chat_id=chat_id, text=f"🚨 Web board error:\n\n{summary}")

    return report


# --- start / stop -------------------------------------------------------------------


async def start_web(
    bot: _AdminBot,
    scheduler: SchedulerLike,
    base_url: str | None = None,
    host: str | None = None,
    port: int | None = None,
    allowed_user_ids: set[int] | None = None,
) -> web.AppRunner | None:
    """Start the board on the running loop; None when it is off or cannot start.

    Arguments default to the `config` values (an explicit `port=0` or empty
    allowlist is honored, not replaced). No `base_url` means the board is
    disabled (one INFO line). An invalid configuration or a port that
    cannot be bound is logged and the bot carries on without the board; this
    function does not raise.
    """
    base_url = config.WEB_BASE_URL if base_url is None else base_url
    if not base_url:
        logger.info("Web board disabled: WEB_BASE_URL is not set.")
        return None
    host = config.WEB_HOST if host is None else host
    port = config.WEB_PORT if port is None else port
    allowed_user_ids = config.ALLOWED_USER_IDS if allowed_user_ids is None else allowed_user_ids

    problem = validate_web_config(base_url, allowed_user_ids)
    if problem:
        logger.error("Web board not started: %s", problem)
        return None

    runner = None
    try:
        _install_log_redaction()
        chat_id = config.ADMIN_CHAT_ID
        app = create_app(
            scheduler, base_url, allowed_user_ids,
            on_error=_admin_reporter(bot, chat_id) if chat_id else None,
        )
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
    except Exception as exc:
        # OSError: port in use or bad bind address. Anything else is a bug; keep its traceback.
        logger.error(
            "Web board could not start on %s:%s: %s", host, port, exc, exc_info=not isinstance(exc, OSError)
        )
        await stop_web(runner)
        return None
    logger.info("Web board listening on %s:%s (%s)", host, port, base_url)
    return runner


async def stop_web(runner: web.AppRunner | None) -> None:
    """Stop the board (a no-op for None). Never raises, so shutdown always reaches the scheduler."""
    if runner is None:
        return
    try:
        await runner.cleanup()
    except Exception:
        logger.exception("Web board did not shut down cleanly")
    else:
        logger.info("Web board stopped")

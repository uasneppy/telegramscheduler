"""The web board's aiohttp application: sign-in, sessions, security middlewares, /api/me.

Middlewares, outermost first:

1. errors: WebError -> its status with JSON `{"error", "message", **extra}`;
   aiohttp's own HTTP errors (404, 405, 413, ...) -> the same JSON shape;
   anything else -> logged, reported through `on_error`, JSON 500 `internal`.
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
turned off by the runner (`access_log=None`). Blocking auth and database calls
run through `asyncio.to_thread`.
"""

import asyncio
import html
import logging
import warnings
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web

from bot.web import auth, queries
from bot.web.errors import WebError, not_found
from bot.web.service import SchedulerLike
from bot.web.settings import origin_of

logger = logging.getLogger(__name__)

SESSION_COOKIE = "tgs_session"
CSP = (
    "default-src 'self'; img-src 'self' data:; media-src 'self'; script-src 'self'; "
    "style-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
STATIC_DIR = Path(__file__).resolve().parent / "static"
# The only files served under /static/, with their content types.
_STATIC_FILES = {
    "app.js": "text/javascript; charset=utf-8",
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
        except Exception as exc:
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
    return app

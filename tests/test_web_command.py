"""Web board wiring: /web command, start_web / stop_web, log redaction, housekeeping, main.py hooks."""

import asyncio
import logging
import os
import re
import socket
import sys
import time
from datetime import timedelta
from types import SimpleNamespace

import aiohttp
import pytest

import config
from bot.database import Database
from bot.scheduler import PostScheduler
from bot.utils import get_current_kyiv_time
from bot.web import auth, media, queries, server
from bot.web.auth import consume_login_token, create_login_token
from bot.web.command import web_command
from bot.web.housekeeping import purge_web_data
from bot.web.server import start_web, stop_web
from web_helpers import FakeScheduler

BASE = "https://board.example.com"
LINK = re.compile(r"https://board\.example\.com/login\?t=([A-Za-z0-9_-]+)")


# --- fakes ---------------------------------------------------------------------------


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class FakeBot:
    def __init__(self):
        self.sent = []
        self.commands = None

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))

    async def set_my_commands(self, commands):
        self.commands = [command.command for command in commands]


def _update(user_id=1, chat_type="private"):
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(type=chat_type),
        message=message,
    )
    return update, message


def _context(runner):
    return SimpleNamespace(application=SimpleNamespace(bot_data={"web_runner": runner}))


def _count(table):
    conn = Database.get_connection()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def board_url(monkeypatch):
    monkeypatch.setattr(config, "WEB_BASE_URL", BASE)


# --- /web command ---------------------------------------------------------------------


def test_command_sends_valid_single_use_link(board_url):
    update, message = _update(user_id=7)
    asyncio.run(web_command(update, _context(runner=object())))

    assert len(message.replies) == 1
    text, kwargs = message.replies[0]
    match = LINK.search(text)
    assert match, text
    assert "single-use" in text and "5 minutes" in text
    assert kwargs["disable_web_page_preview"] is True
    token = match.group(1)
    assert consume_login_token(token) == 7
    assert consume_login_token(token) is None


def test_command_refused_in_group_creates_no_token(board_url):
    update, message = _update(chat_type="supergroup")
    asyncio.run(web_command(update, _context(runner=object())))

    assert _count("web_login_tokens") == 0
    assert len(message.replies) == 1
    assert "private chat" in message.replies[0][0]
    assert "/login" not in message.replies[0][0]


def test_command_when_disabled(board_url):
    update, message = _update()
    asyncio.run(web_command(update, _context(runner=None)))

    assert [text for text, _ in message.replies] == ["The web board is not enabled on this server."]
    assert _count("web_login_tokens") == 0


# --- /help ------------------------------------------------------------------------------


def _help_text(web_runner):
    from bot.handlers.menu import help_handler

    update, message = _update()
    asyncio.run(help_handler(update, _context(runner=web_runner)))
    assert len(message.replies) == 1
    return message.replies[0][0]


def test_help_lists_web_command_only_when_board_runs():
    enabled = _help_text(web_runner=object())
    disabled = _help_text(web_runner=None)

    assert "• `/web` - Open the web board\n" in enabled
    assert "/web" not in disabled
    # The line sits in the Management Commands list and nothing else differs.
    assert enabled.index("Management Commands") < enabled.index("/web") < enabled.index("/reset")
    assert enabled.replace("• `/web` - Open the web board\n", "") == disabled


# --- start_web / stop_web --------------------------------------------------------------


def _start(**kwargs):
    kwargs.setdefault("host", "127.0.0.1")
    kwargs.setdefault("port", 0)
    return asyncio.run(start_web(FakeBot(), FakeScheduler(), **kwargs))


def test_start_web_disabled_or_invalid_returns_none(monkeypatch, caplog):
    monkeypatch.setattr(config, "WEB_BASE_URL", None)
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {5})

    # Not configured: nothing started, exactly one INFO line says why, nothing at WARNING or above.
    for kwargs in ({}, {"base_url": ""}):
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert _start(**kwargs) is None
        assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
            (logging.INFO, "Web board disabled: WEB_BASE_URL is not set.")
        ], kwargs

    invalid = [
        ({"base_url": BASE, "allowed_user_ids": set()}, "ALLOWED_USER_IDS"),
        ({"base_url": "http://example.com", "allowed_user_ids": {1}}, "https"),
        ({"base_url": BASE + "/board", "allowed_user_ids": {1}}, "path"),
    ]
    for kwargs, reason in invalid:
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert _start(**kwargs) is None
        assert any(r.levelno == logging.ERROR and reason in r.getMessage() for r in caplog.records), kwargs


def test_start_stop_serves_index():
    async def scenario():
        runner = await start_web(
            FakeBot(), FakeScheduler(),
            base_url="http://localhost:8080", host="127.0.0.1", port=0, allowed_user_ids={1},
        )
        assert runner is not None
        port = runner.addresses[0][1]
        assert port != config.WEB_PORT  # port=0 asked the OS for a free port; it was not replaced by the default
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/") as response:
                assert response.status == 200
                assert "Content-Security-Policy" in response.headers
        await stop_web(runner)
        with pytest.raises(aiohttp.ClientConnectionError):
            async with aiohttp.ClientSession() as session:
                await session.get(f"http://127.0.0.1:{port}/")

    asyncio.run(scenario())


def _start_and_stop(**kwargs):
    """Start the board on a free port, check it answers, stop it; returns whether a runner came back."""
    async def scenario():
        runner = await start_web(FakeBot(), FakeScheduler(), host="127.0.0.1", port=0, **kwargs)
        if runner is None:
            return False
        port = runner.addresses[0][1]
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/") as response:
                assert response.status == 200
        await stop_web(runner)
        return True

    return asyncio.run(scenario())


def _pillow_records(caplog):
    return [r for r in caplog.records if "illow" in r.getMessage()]


def test_start_web_warns_once_and_still_starts_when_pillow_is_missing(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setattr(media, "_pillow_warned", False)

    with caplog.at_level(logging.INFO):
        assert _start_and_stop(base_url="http://localhost:8080", allowed_user_ids={1}) is True

    records = _pillow_records(caplog)
    assert [(r.levelno, r.getMessage()) for r in records] == [(logging.WARNING, media.PILLOW_MISSING_WARNING)]
    assert any("Web board listening" in r.getMessage() for r in caplog.records)  # non-fatal: it started


def test_start_web_says_nothing_about_pillow_when_it_is_installed(monkeypatch, caplog):
    monkeypatch.setattr(media, "_pillow_warned", False)

    with caplog.at_level(logging.DEBUG):
        assert _start_and_stop(base_url="http://localhost:8080", allowed_user_ids={1}) is True

    assert _pillow_records(caplog) == []
    assert media._pillow_warned is False


def test_start_web_does_not_check_pillow_when_the_board_is_off_or_misconfigured(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setattr(media, "_pillow_warned", False)
    monkeypatch.setattr(config, "WEB_BASE_URL", None)

    with caplog.at_level(logging.DEBUG):
        assert _start() is None  # board off
        assert _start(base_url=BASE, allowed_user_ids=set()) is None  # refused: empty allowlist

    assert _pillow_records(caplog) == []


def test_stop_web_accepts_none():
    asyncio.run(stop_web(None))


def test_start_web_port_in_use_returns_none(caplog):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        with caplog.at_level(logging.INFO):
            runner = _start(base_url="http://localhost:8080", allowed_user_ids={1}, port=port)
    assert runner is None
    assert any(r.levelno == logging.ERROR and str(port) in r.getMessage() for r in caplog.records)


def test_start_web_unexpected_failure_returns_none(monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("factory broke")

    monkeypatch.setattr(server, "create_app", broken)
    with caplog.at_level(logging.INFO):
        assert _start(base_url="http://localhost:8080", allowed_user_ids={1}) is None
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)


def test_start_web_reports_faults_to_admin_chat_once_per_interval(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_CHAT_ID", "777")

    def boom(user_id):
        raise RuntimeError("boom ?t=SECRET")

    monkeypatch.setattr(queries, "get_me", boom)
    bot = FakeBot()
    session_id = auth.create_session(1)

    async def scenario():
        runner = await start_web(
            bot, FakeScheduler(),
            base_url="http://localhost:8080", host="127.0.0.1", port=0, allowed_user_ids={1},
        )
        port = runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                for _ in range(3):
                    async with session.get(
                        f"http://127.0.0.1:{port}/api/me", headers={"Cookie": f"tgs_session={session_id}"}
                    ) as response:
                        assert response.status == 500
        finally:
            await stop_web(runner)

    asyncio.run(scenario())
    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == "777"
    assert "RuntimeError: boom" in text and "SECRET" not in text


def test_start_web_without_admin_chat_reports_nothing(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_CHAT_ID", None)
    monkeypatch.setattr(queries, "get_me", lambda user_id: 1 / 0)
    bot = FakeBot()
    session_id = auth.create_session(1)

    async def scenario():
        runner = await start_web(
            bot, FakeScheduler(),
            base_url="http://localhost:8080", host="127.0.0.1", port=0, allowed_user_ids={1},
        )
        port = runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://127.0.0.1:{port}/api/me", headers={"Cookie": f"tgs_session={session_id}"}
                ) as response:
                    assert response.status == 500
        finally:
            await stop_web(runner)

    asyncio.run(scenario())
    assert bot.sent == []


def test_start_web_honors_explicit_empty_allowlist(monkeypatch):
    # `allowed_user_ids=set()` is a value, not "use the default".
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {5})
    assert _start(base_url=BASE, allowed_user_ids=set()) is None


# --- admin error reporter ---------------------------------------------------------------


def _raised(message):
    try:
        raise RuntimeError(message)
    except RuntimeError as exc:
        return exc


def test_admin_reporter_rate_limit(caplog):
    now = [1000.0]
    bot = FakeBot()
    report = server._admin_reporter(bot, "777", clock=lambda: now[0])

    async def attempt(at):
        now[0] = at
        await report(_raised("fault"))

    with caplog.at_level(logging.INFO):
        for at in (1000.0, 1010.0, 1029.9, 1030.0, 1031.0):
            asyncio.run(attempt(at))

    assert len(bot.sent) == 2  # first, then the first after 30 s have passed
    dropped = [r for r in caplog.records if "rate limited" in r.getMessage()]
    assert len(dropped) == 3


def test_admin_reporter_sends_redacted_tail_of_traceback():
    bot = FakeBot()
    report = server._admin_reporter(bot, "777", clock=lambda: 0.0)
    asyncio.run(report(_raised("A" * 3000 + " /login?t=SECRET end")))

    chat_id, text = bot.sent[0]
    assert chat_id == "777"
    body = text.split("\n\n", 1)[1]
    assert len(body) <= 1500
    assert "SECRET" not in text
    assert body.rstrip().endswith("t=[redacted] end")


# --- log redaction ----------------------------------------------------------------------


@pytest.fixture
def aiohttp_server_log(caplog):
    server._install_log_redaction()
    caplog.set_level(logging.DEBUG, logger="aiohttp.server")
    return logging.getLogger("aiohttp.server")


def test_log_filter_redacts_token_and_session_in_message_and_args(aiohttp_server_log, caplog):
    aiohttp_server_log.warning("Rejected GET /login?t=SECRET HTTP/1.1")
    aiohttp_server_log.warning("Bad request line %s from %s", "GET /login?a=1&t=SECRET2 HTTP/1.1", "10.0.0.1")
    aiohttp_server_log.warning("Bad request line %r", b"GET /login?t=SECRET3 HTTP/1.1\r\n")
    aiohttp_server_log.warning("Cookie: tgs_session=COOKIESECRET; theme=dark")
    aiohttp_server_log.warning("%d%% of %s", 50, "format=json&limit=5")

    for secret in ("SECRET", "SECRET2", "SECRET3", "COOKIESECRET"):
        assert secret not in caplog.text
    messages = [r.getMessage() for r in caplog.records]
    assert messages[0] == "Rejected GET /login?t=[redacted] HTTP/1.1"
    assert messages[1] == "Bad request line GET /login?a=1&t=[redacted] HTTP/1.1 from 10.0.0.1"
    assert messages[3] == "Cookie: tgs_session=[redacted]; theme=dark"
    assert messages[4] == "50% of format=json&limit=5"  # other parameters ending in "t" are left alone


def test_log_filter_redacts_exception_text(aiohttp_server_log, caplog):
    try:
        raise ValueError("Invalid HTTP version: b'GET /login?t=SECRET HTTP/9.9'")
    except ValueError:
        aiohttp_server_log.error("Error handling request", exc_info=True)

    assert "ValueError" in caplog.text
    assert "SECRET" not in caplog.text


def test_log_filter_installed_once():
    for _ in range(2):
        runner = _start(base_url="http://localhost:8080", allowed_user_ids={1})
        asyncio.run(stop_web(runner))
    installed = [f for f in logging.getLogger("aiohttp.server").filters if isinstance(f, server._LogRedactionFilter)]
    assert len(installed) == 1


def test_malformed_request_line_never_logs_the_token(caplog):
    """aiohttp's own logger prints the raw request line when it rejects a request."""

    async def scenario():
        runner = await start_web(
            FakeBot(), FakeScheduler(),
            base_url="http://localhost:8080", host="127.0.0.1", port=0, allowed_user_ids={1},
        )
        port = runner.addresses[0][1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /login?t=SECRET HTTP/9.9\r\nHost: x\r\n\r\n")
            await writer.drain()
            answer = await reader.read()
            writer.close()
            assert answer.startswith(b"HTTP/1.")
            assert b" 400 " in answer.split(b"\r\n", 1)[0]
        finally:
            await stop_web(runner)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())
    assert any(r.name == "aiohttp.server" for r in caplog.records)  # aiohttp did log the rejection
    assert "SECRET" not in caplog.text


# --- housekeeping -----------------------------------------------------------------------


@pytest.fixture
def thumbs():
    os.makedirs(config.THUMBS_DIR, exist_ok=True)
    created = []

    def make(name, age_days):
        path = os.path.join(config.THUMBS_DIR, name)
        with open(path, "wb") as f:
            f.write(b"RIFF")
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        created.append(path)
        return path

    yield make
    for path in created:
        if os.path.exists(path):
            os.remove(path)


def _seed_web_data(thumbs):
    create_login_token(1, now=get_current_kyiv_time() - timedelta(hours=1))  # long expired
    live = create_login_token(2)
    return live, thumbs("old.webp", age_days=15), thumbs("fresh.webp", age_days=1)


def test_purge_web_data(thumbs):
    live, old, fresh = _seed_web_data(thumbs)

    assert purge_web_data() == {"auth_rows": 1, "thumbnails": 1}
    assert not os.path.exists(old) and os.path.exists(fresh)
    assert _count("web_login_tokens") == 1
    assert consume_login_token(live) == 2
    assert purge_web_data() == {"auth_rows": 1, "thumbnails": 0}  # the consumed token is now stale too


def test_daily_cleanup_purges_web_data(thumbs, caplog):
    live, old, fresh = _seed_web_data(thumbs)

    with caplog.at_level(logging.INFO):
        # _daily_cleanup uses no instance state; skipping __init__ avoids building APScheduler
        # (which needs the system time zone database).
        asyncio.run(PostScheduler.__new__(PostScheduler)._daily_cleanup())

    assert not [r for r in caplog.records if "Daily cleanup failed" in r.getMessage()]
    assert not os.path.exists(old) and os.path.exists(fresh)
    assert _count("web_login_tokens") == 1
    assert consume_login_token(live) == 2


# --- main.py wiring ---------------------------------------------------------------------


@pytest.fixture
def main_module():
    import main

    return main


class FakeSchedulerService:
    def __init__(self, bot=None, events=None):
        self.events = events if events is not None else []

    def start(self):
        self.events.append("scheduler.start")

    def stop(self):
        self.events.append("scheduler.stop")


def _application():
    return SimpleNamespace(bot=FakeBot(), bot_data={})


def test_post_init_adds_web_menu_entry_only_when_board_started(main_module, monkeypatch):
    runner = object()
    started = []

    async def fake_start_web(bot, scheduler):
        started.append((bot, scheduler))
        return runner

    monkeypatch.setattr(main_module, "PostScheduler", FakeSchedulerService)
    monkeypatch.setattr(config, "WEB_BASE_URL", BASE)
    monkeypatch.setattr(server, "start_web", fake_start_web)  # post_init imports it from here when the board is on
    application = _application()
    asyncio.run(main_module.post_init(application))

    assert application.bot_data["web_runner"] is runner
    assert started == [(application.bot, application.bot_data["scheduler"])]
    assert "web" in application.bot.commands
    assert "help" in application.bot.commands

    async def could_not_start(bot, scheduler):
        return None

    monkeypatch.setattr(server, "start_web", could_not_start)
    application = _application()
    asyncio.run(main_module.post_init(application))

    assert application.bot_data["web_runner"] is None
    assert "web" not in application.bot.commands
    assert "start" in application.bot.commands


def test_post_init_with_board_off_does_not_start_it(main_module, monkeypatch, caplog):
    async def must_not_run(bot, scheduler):
        raise AssertionError("start_web must not be called when WEB_BASE_URL is unset")

    monkeypatch.setattr(main_module, "PostScheduler", FakeSchedulerService)
    monkeypatch.setattr(config, "WEB_BASE_URL", None)
    monkeypatch.setattr(server, "start_web", must_not_run)
    application = _application()
    with caplog.at_level(logging.INFO):
        asyncio.run(main_module.post_init(application))

    assert application.bot_data["web_runner"] is None
    assert "web" not in application.bot.commands and "help" in application.bot.commands
    assert "Web board disabled: WEB_BASE_URL is not set." in caplog.text


def test_post_init_carries_on_when_the_web_server_module_cannot_be_imported(main_module, monkeypatch, caplog):
    import builtins

    real_import = builtins.__import__

    def refuse_server(name, *args, **kwargs):
        if name == "bot.web.server":
            raise ModuleNotFoundError("No module named 'aiohttp'", name="aiohttp")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(main_module, "PostScheduler", FakeSchedulerService)
    monkeypatch.setattr(config, "WEB_BASE_URL", BASE)
    monkeypatch.setattr(builtins, "__import__", refuse_server)
    application = _application()
    with caplog.at_level(logging.ERROR):
        asyncio.run(main_module.post_init(application))
    monkeypatch.undo()

    assert application.bot_data["web_runner"] is None
    assert "web" not in application.bot.commands and "start" in application.bot.commands
    assert "Web board not started" in caplog.text and "aiohttp" in caplog.text


def test_post_shutdown_stops_board_before_scheduler(main_module, monkeypatch):
    events = []
    runner = object()

    async def fake_stop_web(stopping):
        assert stopping is runner
        events.append("web.stop")

    monkeypatch.setattr(server, "stop_web", fake_stop_web)  # post_shutdown imports it from here
    application = _application()
    application.bot_data.update(web_runner=runner, scheduler=FakeSchedulerService(events=events))
    asyncio.run(main_module.post_shutdown(application))

    assert events == ["web.stop", "scheduler.stop"]


def test_post_shutdown_without_board(main_module, monkeypatch):
    async def must_not_run(stopping):
        raise AssertionError("stop_web must not be called without a runner")

    monkeypatch.setattr(server, "stop_web", must_not_run)
    events = []
    application = _application()
    application.bot_data.update(web_runner=None, scheduler=FakeSchedulerService(events=events))
    asyncio.run(main_module.post_shutdown(application))
    assert events == ["scheduler.stop"]


class FakeApplication:
    """Records what main() registers; a real Application is not needed to see which handlers it adds."""

    def __init__(self):
        self.handlers = []
        self.polling = []

    def add_handler(self, handler, group=0):
        self.handlers.append(handler)

    def add_error_handler(self, callback):
        pass

    def run_polling(self, **kwargs):
        self.polling.append(kwargs)


class FakeBuilder:
    def __init__(self, application):
        self.application = application

    def token(self, token):
        return self

    def request(self, request):
        return self

    def build(self):
        return self.application


def test_main_registers_web_command_always(main_module, monkeypatch):
    from telegram.ext import CommandHandler

    application = FakeApplication()
    monkeypatch.setattr(main_module.Application, "builder", lambda: FakeBuilder(application))
    main_module.main()

    web = [h for h in application.handlers if isinstance(h, CommandHandler) and "web" in h.commands]
    assert len(web) == 1 and web[0].callback is web_command
    assert application.polling  # main() reached run_polling

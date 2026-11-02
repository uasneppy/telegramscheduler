"""A deployment that leaves WEB_BASE_URL unset needs neither aiohttp nor Pillow.

Each case runs in a fresh interpreter in which every import of aiohttp or PIL
raises ImportError, so what the bot imports with the board off is checked for
real, not through the modules this test process has already loaded.
"""

import os
import subprocess
import sys
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Runs first in every subprocess: a finder in front of the normal ones that refuses the named packages.
BLOCKER_TEMPLATE = textwrap.dedent(
    """
    import sys

    BLOCKED = __NAMES__

    class Blocker:
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ModuleNotFoundError(f"blocked for this test: {name}", name=name)
            return None

    sys.meta_path.insert(0, Blocker())
    """
)


def blocker(*names):
    return BLOCKER_TEMPLATE.replace("__NAMES__", repr(names))


BLOCKER = blocker("aiohttp", "PIL")

BOARD_OFF = BLOCKER + textwrap.dedent(
    """
    import asyncio
    from types import SimpleNamespace

    import config
    assert config.WEB_BASE_URL is None, config.WEB_BASE_URL

    import main
    import bot.scheduler
    import bot.handlers
    import bot.web.command
    import bot.web.housekeeping

    # The board-off start-up and shutdown never touch the web server module.
    class Bot:
        async def set_my_commands(self, commands):
            self.commands = [command.command for command in commands]

    class Scheduler:
        def __init__(self, bot=None):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    main.PostScheduler = Scheduler
    application = SimpleNamespace(bot=Bot(), bot_data={})
    asyncio.run(main.post_init(application))
    assert application.bot_data["web_runner"] is None
    assert "web" not in application.bot.commands and "help" in application.bot.commands
    asyncio.run(main.post_shutdown(application))

    # The nightly cleanup of the board's data needs no Pillow either.
    from bot.database import init_database
    init_database()
    assert bot.web.housekeeping.purge_web_data() == {"auth_rows": 0, "thumbnails": 0}

    # Asking for a thumbnail without Pillow gives "no thumbnail", not an error.
    import os
    from bot.web import media
    os.makedirs(config.UPLOADS_DIR, exist_ok=True)
    picture = os.path.join(config.UPLOADS_DIR, "picture.png")
    with open(picture, "wb") as handle:
        handle.write(b"not decoded: Pillow is blocked")
    assert media.get_thumbnail(picture) is None

    loaded = sorted(name for name in sys.modules if name.split(".")[0] in BLOCKED or name == "bot.web.server")
    assert not loaded, loaded
    print("ok")
    """
)


def run_python(code, tmp_path, **extra_env):
    env = {key: value for key, value in os.environ.items() if key not in ("WEB_BASE_URL", "BOT_TOKEN")}
    env.update(DATA_DIR=str(tmp_path), PYTHONPATH=ROOT, **extra_env)
    return subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
    )


def test_board_off_needs_neither_aiohttp_nor_pillow(tmp_path):
    result = run_python(BOARD_OFF, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_the_blocker_really_blocks_the_board_itself(tmp_path):
    # Without this the case above could pass because nothing is blocked at all.
    checks = {
        "bot.web.server": "aiohttp",
        "bot.web.api": "aiohttp",
        "PIL.Image": "PIL",
        "aiohttp": "aiohttp",
    }
    for module, blocked in checks.items():
        code = BLOCKER + f"\ntry:\n    import {module}\nexcept ModuleNotFoundError as error:\n    print(error.name)\n"
        result = run_python(code, tmp_path)

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == blocked, (module, result.stdout, result.stderr)


# Only Pillow is missing and the board is on: it starts, and says so once.
BOARD_ON_WITHOUT_PILLOW = blocker("PIL") + textwrap.dedent(
    """
    import asyncio
    import io
    import logging

    stream = io.StringIO()
    logging.basicConfig(stream=stream, level=logging.INFO, format="%(levelname)s %(message)s")

    from bot.database import init_database
    from bot.web.server import start_web, stop_web

    init_database()

    class Bot:
        async def send_message(self, chat_id, text):
            pass

    class Scheduler:
        async def schedule_single_post(self, post_id, scheduled_time):
            pass

        async def cancel_post_job(self, post_id):
            pass

    async def main():
        runner = await start_web(
            Bot(), Scheduler(), base_url="http://localhost:8080", host="127.0.0.1", port=0, allowed_user_ids={1}
        )
        assert runner is not None, stream.getvalue()
        await stop_web(runner)

    asyncio.run(main())

    log = stream.getvalue().splitlines()
    pillow = [line for line in log if "illow" in line]
    assert pillow == [
        "WARNING Pillow is not installed: web board thumbnails are disabled (pip install pillow)."
    ], log
    assert any("Web board listening" in line for line in log), log
    assert "PIL" not in sys.modules
    print("ok")
    """
)


def test_board_on_without_pillow_warns_once_and_starts(tmp_path):
    result = run_python(BOARD_ON_WITHOUT_PILLOW, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"

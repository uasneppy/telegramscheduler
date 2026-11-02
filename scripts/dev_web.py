#!/usr/bin/env python3
"""Run the web board locally on seeded demo data (development only).

    python scripts/dev_web.py                       # board with demo posts on http://localhost:8765
    python scripts/dev_web.py --empty --port 8766   # user 1 only: no channels, no posts

Everything lives in a fresh temporary DATA_DIR that is deleted on exit, so real
data is never touched and every start begins from the same seed. The printed
sign-in link works once and expires after 5 minutes, like the one /web sends in
Telegram: restart the script for a new link. Scheduler calls are logged instead
of creating jobs, and nothing is ever sent to Telegram.

Seed (user 1, channels -1001 Alpha with a caption signature, -1002 Beta without):
- 130 scheduled posts from tomorrow 10:00 Kyiv, every 2 h up to 18:00, over 14
  days. Alpha and Beta share their slots; Beta skips the first slot and the last
  nine, so each 50-row page of the Scheduled list ends in the middle of a pair
  of posts with the same time. Among them: a 3-image album, a recurring post, a
  PNG sent as a file (document_image), a PDF document and a GIF animation.
- 8 queued posts (the last one with its file gone from disk, so replacing the
  media of a post whose file is missing can be tried), 3 failed (with reasons)
  and 12 posted posts (3 of them with their file already deleted, like
  published media after the 7-day cleanup).
- Captions: plain, formatted (bold, a link and a custom emoji), Ukrainian
  ("Привіт, світ"), "AT&T <3", an empty one and one longer than two lines.
"""

import argparse
import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# config.py reads DATA_DIR at import time, so it is set before any bot module loads.
DATA_DIR = tempfile.mkdtemp(prefix="tgs_dev_web_")
os.environ["DATA_DIR"] = DATA_DIR
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web  # noqa: E402
from PIL import Image, ImageDraw, ImageFont, ImageOps  # noqa: E402

from bot.database import Database, init_database, serialize_dt  # noqa: E402
from bot.utils import get_current_kyiv_time, get_kyiv_timezone, get_organized_media_path  # noqa: E402
from bot.web import auth  # noqa: E402
from bot.web.captions import entities_to_json, tagged_to_caption  # noqa: E402
from bot.web.server import create_app  # noqa: E402

logger = logging.getLogger("dev_web")

HOST = "localhost"
PORT = 8765  # the default; --port picks another, e.g. for an --empty board beside this one
USER_ID = 1
ALPHA, BETA = "-1001", "-1002"
CHANNELS = ((ALPHA, "Alpha"), (BETA, "Beta"))
ALPHA_SIGNATURE = "Alpha News · @alpha_news"

DAYS = 14
SLOT_HOURS = (10, 12, 14, 16, 18)
# Slot indexes (0..69, in time order) that also get a Beta post: all but the
# first and the last nine, which gives 70 Alpha + 60 Beta = 130 scheduled posts.
BETA_SLOTS = range(1, 61)

FORMATTED_CAPTION = (
    '<b>Big news:</b> the new site is live. '
    '<a href="https://example.com">Take a look</a> '
    '<tg-emoji emoji-id="5368324170671202286">🎉</tg-emoji>'
)
LONG_CAPTION = (
    "Weekly digest: three new arrivals in the shop, a change to the opening hours on "
    "Saturday, the winners of last week's photo contest, and a reminder that orders placed "
    "before Thursday evening still arrive before the weekend. Thanks for reading!"
)
PLAIN_CAPTIONS = (
    "Morning coffee and a quiet street.",
    "New arrivals are in. Come and see them this week.",
    "Behind the scenes at the studio.",
    "Tip of the day: back up your photos.",
    "Sunset over the river, straight from the camera.",
    "Our team says thank you for 10,000 followers!",
    "Sale ends Sunday at midnight.",
    "Fresh bread, every morning from 8.",
    "Question of the week: which colour should we stock next?",
    "Rainy day playlist, link in the bio.",
    "Workshop seats are almost gone.",
    "The market is open late on Friday.",
)
# Captions for chosen scheduled slots: (slot index, channel) -> tagged caption.
SPECIAL_CAPTIONS = {
    (1, ALPHA): FORMATTED_CAPTION,
    (1, BETA): "Привіт, світ! Ранкові новини каналу.",
    (2, ALPHA): "AT&T <3",
    (3, BETA): LONG_CAPTION,
    (4, ALPHA): "",
}
ALBUM_SLOT = (2, BETA)
RECURRING_SLOT = (3, ALPHA)
DOCUMENT_IMAGE_SLOT = (5, ALPHA)
DOCUMENT_SLOT = (5, BETA)
ANIMATION_SLOT = (6, ALPHA)

FAILURES = (
    ("Forbidden: bot is not a member of the channel chat", 3),
    ("Bad Request: wrong file identifier/HTTP URL specified", 1),
    ("Timed out while uploading the photo", 2),
)
# Hues for the generated pictures (start colour, end colour).
PALETTE = (
    ("#1f4e79", "#4f9bd9"), ("#6b2d5c", "#d16ba5"), ("#205e3b", "#6cc28f"),
    ("#7a4b12", "#e3a857"), ("#3d3a8c", "#8f8be6"), ("#6e2323", "#e07070"),
    ("#1e5f66", "#5fc3cc"), ("#4a4a4a", "#a8a8a8"),
)


class LoggingScheduler:
    """Stands in for PostScheduler: logs each call instead of scheduling a job."""

    async def schedule_single_post(self, post_id: int, scheduled_time: datetime) -> None:
        logger.info("scheduler: schedule post %s at %s", post_id, scheduled_time.isoformat())

    async def cancel_post_job(self, post_id: int) -> None:
        logger.info("scheduler: cancel the job of post %s", post_id)


# --- media ------------------------------------------------------------------------


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError):  # Pillow without FreeType: fixed-size bitmap font
        return ImageFont.load_default()


def _picture(lines: list[str], index: int, size=(480, 360)) -> Image.Image:
    start, end = PALETTE[index % len(PALETTE)]
    gradient = Image.linear_gradient("L").rotate(90 * (index % 4)).resize(size)
    image = ImageOps.colorize(gradient, start, end)
    draw = ImageDraw.Draw(image)
    draw.text((28, 28), lines[0], font=_font(44), fill="white")
    for offset, line in enumerate(lines[1:]):
        draw.text((28, 92 + offset * 34), line, font=_font(26), fill="white")
    return image


def _save(image: Image.Image, media_type: str, extension: str, **params) -> str:
    """Store an image in the user's media folder; returns its absolute path."""
    folder, _relative = get_organized_media_path(USER_ID, media_type)
    path = os.path.join(folder, f"seed-{os.urandom(6).hex()}{extension}")
    image.save(path, **params)
    return path


def _photo(lines: list[str], index: int, media_type: str = "photo") -> str:
    return _save(_picture(lines, index), media_type, ".png")


def _pdf(lines: list[str], index: int) -> str:
    return _save(_picture(lines, index, size=(420, 594)), "document", ".pdf")


def _gif(lines: list[str], index: int) -> str:
    frames = [_picture(lines + [f"frame {n + 1}"], index + n) for n in range(3)]
    return _save(frames[0], "animation", ".gif", save_all=True, append_images=frames[1:], duration=400, loop=0)


# --- rows -------------------------------------------------------------------------


def _insert_post(**columns) -> int:
    fields = {"user_id": USER_ID, "mode": 1, "status": "pending", **columns}
    conn = Database.get_connection()
    try:
        with conn:
            cursor = conn.execute(
                f"INSERT INTO posts ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                list(fields.values()),
            )
        return cursor.lastrowid
    finally:
        conn.close()


def _caption(tagged: str) -> dict:
    text, entities = tagged_to_caption(tagged)
    return {"description": text, "caption_entities": entities_to_json(entities)}


def _kyiv(day, hour: int) -> datetime:
    return get_kyiv_timezone().localize(datetime(day.year, day.month, day.day, hour))


def _label(when: datetime, channel_name: str) -> list[str]:
    return [when.strftime("%a %d.%m %H:%M"), channel_name]


def _seed_scheduled(names: dict) -> int:
    tomorrow = get_current_kyiv_time().date() + timedelta(days=1)
    slots = [_kyiv(tomorrow + timedelta(days=d), hour) for d in range(DAYS) for hour in SLOT_HOURS]
    count = 0
    for slot, when in enumerate(slots):
        channels = (ALPHA, BETA) if slot in BETA_SLOTS else (ALPHA,)
        for channel in channels:
            key = (slot, channel)
            label = _label(when, names[channel])
            columns = {"channel_id": channel, "scheduled_time": serialize_dt(when)}
            columns.update(_caption(SPECIAL_CAPTIONS.get(key, PLAIN_CAPTIONS[count % len(PLAIN_CAPTIONS)])))
            if key == ALBUM_SLOT:
                files = [_photo(label + [f"album {n + 1} of 3"], count + n) for n in range(3)]
                bundle = [{"file_path": path, "media_type": "photo"} for path in files]
                columns.update(file_path=files[0], media_type="album", media_bundle_json=json.dumps(bundle))
                columns.update(_caption("Weekend gallery: three shots from the market"))
            elif key == RECURRING_SLOT:
                columns.update(file_path=_photo(label + ["every 24 h"], count), media_type="photo",
                               is_recurring=1, recurring_interval_hours=24)
                columns.update(_caption("Daily reminder: the shop opens at 9:00"))
            elif key == DOCUMENT_IMAGE_SLOT:
                columns.update(file_path=_photo(label + ["sent as a file"], count, "document_image"),
                               media_type="document_image")
            elif key == DOCUMENT_SLOT:
                columns.update(file_path=_pdf(label + ["price list"], count), media_type="document")
                columns.update(_caption("Price list for November (PDF)"))
            elif key == ANIMATION_SLOT:
                columns.update(file_path=_gif(label, count), media_type="animation")
            else:
                columns.update(file_path=_photo(label, count), media_type="photo")
            _insert_post(**columns)
            count += 1
    return count


def _seed_queued(names: dict) -> int:
    for n in range(8):
        channel = (ALPHA, BETA)[n % 2]
        path = _photo([f"Queued #{n + 1}", names[channel]], n)
        if n == 7:  # a file that went missing; the post can still get new media
            os.remove(path)
        _insert_post(
            channel_id=channel, scheduled_time=None, media_type="photo", file_path=path,
            **_caption(f"Queued idea #{n + 1}: {PLAIN_CAPTIONS[(n + 5) % len(PLAIN_CAPTIONS)]}"),
        )
    return 8


def _seed_failed(names: dict) -> int:
    today = get_current_kyiv_time().date()
    for n, (reason, retries) in enumerate(FAILURES):
        channel = (ALPHA, BETA)[n % 2]
        when = _kyiv(today - timedelta(days=1), 10 + 2 * n)
        _insert_post(
            channel_id=channel, status="failed", scheduled_time=serialize_dt(when), media_type="photo",
            file_path=_photo(_label(when, names[channel]), n + 3), failure_reason=reason, retry_count=retries,
            **_caption(f"Failed post #{n + 1}: {PLAIN_CAPTIONS[n]}"),
        )
    return len(FAILURES)


def _seed_posted(names: dict) -> int:
    today = get_current_kyiv_time().date()
    for n in range(12):
        channel = (ALPHA, BETA)[n % 2]
        when = _kyiv(today - timedelta(days=1 + n // 3), (10, 14, 18)[n % 3])
        path = _photo(_label(when, names[channel]), n + 1)
        if n in (1, 5, 9):  # published media is cleaned up after 7 days
            os.remove(path)
        _insert_post(
            channel_id=channel, status="posted", scheduled_time=serialize_dt(when),
            posted_at=serialize_dt(when + timedelta(seconds=2)), media_type="photo", file_path=path,
            **_caption(f"Published: {PLAIN_CAPTIONS[(n + 2) % len(PLAIN_CAPTIONS)]}"),
        )
    return 12


def seed(empty: bool) -> str:
    """Create the database and the demo data; returns a summary line."""
    init_database()
    if empty:
        return "user 1 only: no channels, no posts"
    for channel_id, name in CHANNELS:
        assert Database.add_user_channel(USER_ID, channel_id, name)
    assert Database.set_channel_signature(USER_ID, ALPHA, ALPHA_SIGNATURE)
    names = dict(CHANNELS)
    counts = (_seed_scheduled(names), _seed_queued(names), _seed_failed(names), _seed_posted(names))
    return "{} scheduled, {} queued, {} failed, {} posted".format(*counts)


# --- server -----------------------------------------------------------------------


async def serve(summary: str, port: int) -> None:
    base_url = f"http://{HOST}:{port}"
    app = create_app(LoggingScheduler(), base_url, {USER_ID})
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, HOST, port).start()
        token = await asyncio.to_thread(auth.create_login_token, USER_ID)
        print(f"Web board dev server on {base_url} ({summary})", flush=True)
        print(f"Data dir (deleted on exit): {DATA_DIR}", flush=True)
        print("Sign-in link (works once, expires in 5 minutes):", flush=True)
        print(f"{base_url}/login?t={token}", flush=True)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        await stop.wait()
    finally:
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--empty", action="store_true", help="seed only the user: no channels, no posts")
    parser.add_argument("--port", type=int, default=PORT, help=f"port on {HOST} (default {PORT})")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Seeding adds rows one by one; their INFO lines would bury the sign-in link.
    logging.getLogger("bot.database").setLevel(logging.WARNING)
    try:
        asyncio.run(serve(seed(args.empty), args.port))
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()

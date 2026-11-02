"""Shared helpers for the web board tests: a fake scheduler, row builders, HTTP clients and a real-clock time."""

from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta

import pytz
from aiohttp.test_utils import TestClient, TestServer

from bot.database import Database, serialize_dt
from bot.web import auth
from bot.web.server import create_app
from bot.web.settings import origin_of


KYIV = pytz.timezone("Europe/Kiev")


def future_noon(days: int = 30) -> datetime:
    """Noon Kyiv time `days` days after the real today, so always ahead of the real clock.

    For tests whose code compares against the real clock (a new time needs at
    least 60 s of lead). Noon is never inside a DST gap or overlap.
    """
    day = datetime.now(KYIV).date() + timedelta(days=days)
    return KYIV.localize(datetime.combine(day, time(12, 0)))


class FakeScheduler:
    """Records scheduler calls instead of touching APScheduler."""

    def __init__(self):
        self.calls = []

    async def schedule_single_post(self, post_id, scheduled_time):
        self.calls.append(("schedule", post_id, scheduled_time))

    async def cancel_post_job(self, post_id):
        self.calls.append(("cancel", post_id))


def insert_post(user_id=1, status="pending", scheduled=None, **cols) -> int:
    """Insert a post row directly; extra columns (even a raw scheduled_time string) pass through."""
    conn = Database.get_connection()
    c = conn.cursor()
    fields = {"user_id": user_id, "file_path": "f.jpg", "mode": 1, "status": status,
              "scheduled_time": serialize_dt(scheduled) if scheduled else None, **cols}
    keys = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    c.execute(f"INSERT INTO posts ({keys}) VALUES ({marks})", list(fields.values()))
    post_id = c.lastrowid
    conn.commit()
    conn.close()
    return post_id


def add_channel(user_id=1, channel_id="-1001", name="Alpha", signature=None):
    """Give a user an active channel, optionally with a caption signature."""
    assert Database.add_user_channel(user_id, channel_id, name)
    if signature is not None:
        assert Database.set_channel_signature(user_id, channel_id, signature)


class _BoardTestServer(TestServer):
    """TestServer with the production runner settings: no handler cancellation, no access log.

    aiohttp's TestServer always asks for handler_cancellation=True; the board never enables it.
    """

    async def _make_runner(self, **kwargs):
        kwargs.update(handler_cancellation=False, access_log=None)
        return await super()._make_runner(**kwargs)


def app_client(app) -> TestClient:
    """A not-signed-in TestClient for `app`; use it with `async with`."""
    return TestClient(_BoardTestServer(app))


async def sign_in(client: TestClient, user_id: int, origin: str):
    """POST a fresh login token for `user_id`; returns the response (not followed)."""
    token = auth.create_login_token(user_id)
    return await client.post("/login", data={"t": token}, headers={"Origin": origin}, allow_redirects=False)


@asynccontextmanager
async def board_client(user_id=1, scheduler=None, allowed=frozenset({1}), base_url="http://localhost:8080"):
    """A TestClient for a fresh board app, already signed in as `user_id`.

    `allowed` is handed to create_app as is, so a test that passes a set can
    change the allowlist afterwards through `client.app["allowed"]`.
    """
    app = create_app(scheduler if scheduler is not None else FakeScheduler(), base_url, allowed)
    async with app_client(app) as client:
        response = await sign_in(client, user_id, origin_of(base_url))
        assert response.status == 303, await response.text()
        yield client

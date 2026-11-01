"""Shared helpers for the web board tests: a fake scheduler and row builders."""

from bot.database import Database, serialize_dt


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

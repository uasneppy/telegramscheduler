"""The web board's single write path for one post (spec 6.3).

Every mutation reads the caller's post and checks what its status allows,
validates the whole input, then writes with one conditional UPDATE scoped to
the user and the statuses the action may start from. If the publish job
claimed the post in the meantime, nothing changes and the caller gets 409 with
the post's current status. Only after the commit is the post's APScheduler job
brought in line with the stored row. SQLite work and caption parsing run
through asyncio.to_thread; scheduler calls stay on the event loop.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Protocol

from bot.database import Database, serialize_dt
from bot.utils import get_current_kyiv_time
from bot.web.captions import entities_to_json, parse_entities_json, tagged_to_caption
from bot.web.errors import WebError, conflict, invalid_request
from bot.web.media import is_album
from bot.web.queries import get_owned_post, get_post_detail
from bot.web.rules import board_status, can_flags
from bot.web.timeutil import ensure_min_lead, parse_local_time

logger = logging.getLogger(__name__)

POST_NOW_DELAY = timedelta(seconds=60)
RETRY_DELAY = timedelta(seconds=10)

_CHANGE_KEYS = ("caption", "scheduled_time", "channel_id")
# The posts columns this write path may set (the spec 6.3 sync table).
_COLUMNS = frozenset({
    "description", "caption_entities", "scheduled_time", "channel_id", "file_path", "media_type",
    "status", "deleted_from_status", "retry_count", "failure_reason",
})
# Stored statuses an edit or a delete may start from (spec 6.2). A board delete
# records one of these in deleted_from_status, so they are also what restore returns to.
_EDITABLE = ("pending", "failed")
_DELETABLE = ("pending", "failed", "posted")

_STATE = {
    "queued": "is queued",
    "scheduled": "is scheduled",
    "sending": "is being sent right now",
    "failed": "has failed",
    "posted": "has already been posted",
    "deleted": "is deleted",
    "unknown": "has an unknown status",
}


class SchedulerLike(Protocol):
    """The part of PostScheduler the board uses."""

    async def schedule_single_post(self, post_id: int, scheduled_time: datetime) -> None: ...

    async def cancel_post_job(self, post_id: int) -> None: ...


# --- status gates ------------------------------------------------------------------


def _refusal(action: str, row: dict) -> WebError:
    """409 for an action the post's current status does not allow; carries that status."""
    status = board_status(row)
    return conflict(f"Cannot {action} this post: it {_STATE[status]}.", status=status)


def _require(action: str, flag: str, row: dict) -> None:
    """Raise 409 unless the post's status allows `flag` (spec 6.2, via rules.can_flags)."""
    if not can_flags(board_status(row), is_album(row))[flag]:
        raise _refusal(action, row)


def _changed(user_id: int, post_id: int, action: str, include_deleted: bool = False) -> WebError:
    """The error for a conditional UPDATE that changed nothing (raises not_found if the post is gone)."""
    return _refusal(action, get_owned_post(user_id, post_id, include_deleted))


def _log(user_id: int, action: str, post_id: int, detail: str = "") -> None:
    logger.info("Web board: user %s %s post %s%s", user_id, action, post_id, detail)


# --- blocking writes (run through asyncio.to_thread) ---------------------------------


def _apply_update(user_id: int, post_id: int, fields: dict, allowed_statuses: tuple[str, ...]) -> bool:
    """Set `fields` on the user's post in one conditional UPDATE while its status is allowed.

    Datetime values are stored as Kyiv ISO strings. Returns whether a row changed.
    """
    if not fields or not allowed_statuses:
        raise ValueError("fields and allowed_statuses must not be empty")
    unknown = set(fields) - _COLUMNS
    if unknown:
        raise ValueError(f"Not a column the board writes: {', '.join(sorted(unknown))}")
    assignments = ", ".join(f"{column} = ?" for column in fields)
    marks = ", ".join("?" * len(allowed_statuses))
    values = [serialize_dt(value) if isinstance(value, datetime) else value for value in fields.values()]
    conn = Database.get_connection()
    try:
        cursor = conn.execute(
            f"UPDATE posts SET {assignments} WHERE id = ? AND user_id = ? AND status IN ({marks})",
            [*values, post_id, user_id, *allowed_statuses],
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def _store(user_id: int, post_id: int, fields: dict, status: str, action: str,
           include_deleted: bool = False) -> None:
    """Write `fields` while the post still has `status`; raise not_found/conflict if it moved on."""
    if not _apply_update(user_id, post_id, fields, (status,)):
        raise _changed(user_id, post_id, action, include_deleted)


# The two writes below try one starting status per UPDATE, so they know which
# status the write actually matched: an edit syncs the job only for a pending
# post, and a delete records that exact status for restore.


def _store_edit(user_id: int, post_id: int, fields: dict) -> str:
    """Write an edit to a pending or failed post; returns the status it had."""
    for status in _EDITABLE:
        if _apply_update(user_id, post_id, fields, (status,)):
            return status
    raise _changed(user_id, post_id, "edit")


def _store_delete(user_id: int, post_id: int) -> None:
    """Soft-delete the post, recording the status it had in deleted_from_status."""
    for status in _DELETABLE:
        if _apply_update(user_id, post_id, {"status": "deleted", "deleted_from_status": status}, (status,)):
            return
    raise _changed(user_id, post_id, "delete")


# --- edits -----------------------------------------------------------------------------


async def _edit(user_id: int, post_id: int, fields: dict, scheduler: SchedulerLike, now: datetime) -> None:
    """Store an edit in one conditional UPDATE, then move the job of a pending post whose time changed.

    A failed post only stores its new time; Retry gives it a job (spec 6.3).
    """
    new_time = fields.get("scheduled_time")
    if new_time is not None:
        # The scheduler silently skips past times, which would leave a pending post without a job.
        ensure_min_lead(new_time, now)
    status = await asyncio.to_thread(_store_edit, user_id, post_id, fields)
    if status == "pending" and "scheduled_time" in fields:
        if new_time is None:
            await scheduler.cancel_post_job(post_id)
        else:
            await scheduler.schedule_single_post(post_id, new_time)
    _log(user_id, "edit", post_id, f" ({', '.join(fields)})")


async def _set_scheduled_time(
    user_id: int, post_id: int, new_time: datetime | None, scheduler: SchedulerLike, now: datetime
) -> None:
    """Set a post's time (None unschedules it) and sync its job: the time-only form of update_post's write.

    Pending posts get their job scheduled or cancelled, failed posts only store
    the time; a time under the minimum lead is invalid_request and any other
    status is conflict. Bulk shift uses it.
    """
    await _edit(user_id, post_id, {"scheduled_time": new_time}, scheduler, now)


def _parse_time(value, now: datetime) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_local_time(value, now)
    except OverflowError:  # localizing a year at the edge of the datetime range
        raise invalid_request("Time is out of range") from None


async def _owned_channel(user_id: int, channel_id) -> str:
    if not isinstance(channel_id, str) or not channel_id:
        raise invalid_request("channel_id must be a non-empty string")
    if not await asyncio.to_thread(Database.user_has_channel, user_id, channel_id):
        raise invalid_request("Unknown channel")
    return channel_id


async def update_post(
    user_id: int, post_id: int, changes: dict, scheduler: SchedulerLike, now: datetime | None = None
) -> dict:
    """Change any of a post's caption (tagged text), scheduled_time (YYYY-MM-DDTHH:MM or None) and channel_id.

    Everything is validated before the single write, so an invalid field stores
    nothing. Returns the post detail.
    """
    if now is None:
        now = get_current_kyiv_time()
    if not isinstance(changes, dict) or not changes:
        raise invalid_request("Nothing to change")
    unknown = sorted(str(key) for key in changes if key not in _CHANGE_KEYS)
    if unknown:
        raise invalid_request(f"Unknown field: {', '.join(unknown)}")

    row = await asyncio.to_thread(get_owned_post, user_id, post_id)
    _require("edit", "edit", row)
    fields = {}
    if "scheduled_time" in changes:
        fields["scheduled_time"] = _parse_time(changes["scheduled_time"], now)
    if "channel_id" in changes:
        fields["channel_id"] = await _owned_channel(user_id, changes["channel_id"])
    if "caption" in changes:
        old_entities = parse_entities_json(row["caption_entities"])
        text, entities = await asyncio.to_thread(
            tagged_to_caption, changes["caption"], row["description"], old_entities
        )
        fields["description"] = text
        fields["caption_entities"] = entities_to_json(entities)

    await _edit(user_id, post_id, fields, scheduler, now)
    return await asyncio.to_thread(get_post_detail, user_id, post_id, now)


# --- delete and restore ------------------------------------------------------------------


async def delete_post(user_id: int, post_id: int, scheduler: SchedulerLike) -> dict:
    """Soft-delete a post (remembering its status for restore) and cancel its job."""
    row = await asyncio.to_thread(get_owned_post, user_id, post_id)
    _require("delete", "delete", row)
    await asyncio.to_thread(_store_delete, user_id, post_id)
    await scheduler.cancel_post_job(post_id)
    _log(user_id, "delete", post_id)
    return {"id": post_id, "status": "deleted"}


async def restore_post(
    user_id: int, post_id: int, scheduler: SchedulerLike, now: datetime | None = None
) -> dict:
    """Undo a board delete: the post gets back the status it was deleted from.

    A pending post keeps its time only if it is still ahead; otherwise it goes
    back to the queue instead of turning overdue and being sent by the monitor.
    Failed and posted posts keep their status and times, so a restore never
    re-sends anything. A post the bot deleted (no recorded status) is conflict.
    """
    if now is None:
        now = get_current_kyiv_time()
    row = await asyncio.to_thread(get_owned_post, user_id, post_id, True)
    if row["status"] != "deleted":
        raise _refusal("restore", row)
    previous = row["deleted_from_status"]
    if previous not in _DELETABLE:
        raise conflict("Cannot restore this post: it was not deleted on the board.", status="deleted")

    # Clearing deleted_from_status keeps a later delete by the bot unrestorable.
    fields = {"status": previous, "deleted_from_status": None}
    rearm = None
    if previous == "pending":
        kept = row["scheduled_time"]
        rearm = kept if kept is not None and kept > now else None
        fields["scheduled_time"] = rearm
    await asyncio.to_thread(_store, user_id, post_id, fields, "deleted", "restore", True)
    if rearm is not None:
        await scheduler.schedule_single_post(post_id, rearm)
    _log(user_id, "restore", post_id)
    return await asyncio.to_thread(get_post_detail, user_id, post_id, now)


# --- post now and retry -------------------------------------------------------------------


async def post_now(user_id: int, post_id: int, scheduler: SchedulerLike, now: datetime | None = None) -> dict:
    """Schedule a queued or scheduled post for now + 60 s."""
    if now is None:
        now = get_current_kyiv_time()
    row = await asyncio.to_thread(get_owned_post, user_id, post_id)
    _require("post now", "post_now", row)
    when = now + POST_NOW_DELAY
    await asyncio.to_thread(_store, user_id, post_id, {"scheduled_time": when}, "pending", "post now")
    await scheduler.schedule_single_post(post_id, when)
    _log(user_id, "post now", post_id)
    return await asyncio.to_thread(get_post_detail, user_id, post_id, now)


async def retry_post(user_id: int, post_id: int, scheduler: SchedulerLike, now: datetime | None = None) -> dict:
    """Return a failed post to pending with a fresh retry count and schedule it.

    It keeps its time if that is still ahead, otherwise it goes out at now + 10 s.
    """
    if now is None:
        now = get_current_kyiv_time()
    row = await asyncio.to_thread(get_owned_post, user_id, post_id)
    _require("retry", "retry", row)
    kept = row["scheduled_time"]
    when = kept if kept is not None and kept > now else now + RETRY_DELAY
    fields = {"status": "pending", "retry_count": 0, "failure_reason": None, "scheduled_time": when}
    await asyncio.to_thread(_store, user_id, post_id, fields, "failed", "retry")
    await scheduler.schedule_single_post(post_id, when)
    _log(user_id, "retry", post_id)
    return await asyncio.to_thread(get_post_detail, user_id, post_id, now)

"""The web board's single write path for one post (spec 6.3).

Every mutation reads the caller's post and checks what its status allows,
validates the whole input, then writes with one conditional UPDATE scoped to
the user and the statuses the action may start from. If the publish job
claimed the post in the meantime, nothing changes and the caller gets 409 with
the post's current status. Only after the commit is the post's APScheduler job
brought in line with the stored row. SQLite work and caption parsing run
through asyncio.to_thread; scheduler calls stay on the event loop.

Bulk actions (spec 6.7) loop over these same single-post functions, so every
status rule and scheduler sync is the one the single-post path already has.

Replacing media (spec 6.6) follows the repo's rows-then-files order: the row
is committed first and the old file is removed only afterwards, and a failed
replace removes the new file and leaves the old state untouched.
"""

import asyncio
import functools
import logging
import os
from datetime import datetime, timedelta
from typing import Protocol

from bot.database import Database, serialize_dt
from bot.utils import delete_media_file, get_current_kyiv_time
from bot.web.captions import entities_to_json, parse_entities_json, tagged_to_caption
from bot.web.errors import WebError, conflict, invalid_request
from bot.web.media import StoredUpload, is_album, resolve_media_path
from bot.web.queries import get_owned_post, get_post_detail
from bot.web.rules import board_status, can_flags
from bot.web.timeutil import ensure_min_lead, parse_local_time, shift_local

logger = logging.getLogger(__name__)

POST_NOW_DELAY = timedelta(seconds=60)
RETRY_DELAY = timedelta(seconds=10)
BULK_MAX = 500
_BULK_ACTIONS = ("delete", "restore", "move_channel", "shift")
_SHIFT_UNITS = ("hours", "days")

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


# --- replace media ---------------------------------------------------------------------------


def check_media_replaceable(user_id: int, post_id: int) -> dict:
    """The user's post as a raw row if its media may be replaced; blocking.

    not_found for a post that is not the user's. conflict (naming the board
    status) for an album, which is view-only, and for any status that allows no
    media change. The route calls it before reading the upload body, and
    replace_media runs it again once the file is stored.
    """
    row = get_owned_post(user_id, post_id)
    if is_album(row):
        raise conflict("Cannot replace the media of an album post.", status=board_status(row))
    _require("replace the media of", "media", row)
    return row


def _store_media(user_id: int, post_id: int, stored: StoredUpload) -> str:
    """Point the post at the stored upload in one conditional UPDATE; returns the file path it had."""
    old_path = check_media_replaceable(user_id, post_id)["file_path"]
    fields = {"file_path": stored.file_path, "media_type": stored.media_type}
    if not _apply_update(user_id, post_id, fields, _EDITABLE):
        raise _changed(user_id, post_id, "replace the media of")
    return old_path


def _remove_replaced_file(old_path: str, new_path: str) -> None:
    """Delete the file a post no longer uses; silent when it is already gone or not an upload.

    The stored value is resolved against DATA_DIR first (legacy rows are
    relative to it), so the file removed is the one the scheduler would have
    read. The new file is never removed here, whatever the old row said.
    """
    real = resolve_media_path(old_path)
    if real is not None and real != os.path.realpath(new_path):
        delete_media_file(real)


async def replace_media(user_id: int, post_id: int, stored: StoredUpload) -> dict:
    """Replace a queued, scheduled or failed single-media post's file with an upload already on disk.

    The row is updated first (one conditional UPDATE); only after the commit
    is the old file removed. If the post cannot take the media any more (it
    went to sending, was deleted, ...) or the write fails, the new file is
    removed and the post is untouched. A cancelled call leaves both files in
    place: the write may or may not have committed, and a stray file is safer
    than a row that points at nothing. No scheduler call is needed, the post's
    time does not change. Returns the post detail.
    """
    try:
        old_path = await asyncio.to_thread(_store_media, user_id, post_id, stored)
    except Exception:
        await asyncio.to_thread(delete_media_file, stored.file_path)
        raise
    await asyncio.to_thread(_remove_replaced_file, old_path, stored.file_path)
    _log(user_id, "replace media", post_id)
    return await asyncio.to_thread(get_post_detail, user_id, post_id)


# --- bulk -----------------------------------------------------------------------------------


def _bulk_ids(ids) -> list[int]:
    """The request's post ids without repeats, in first-seen order."""
    if not isinstance(ids, list) or not ids:
        raise invalid_request("ids must be a non-empty list")
    if len(ids) > BULK_MAX:
        raise invalid_request(f"At most {BULK_MAX} posts per request")
    if not all(isinstance(post_id, int) and not isinstance(post_id, bool) for post_id in ids):
        raise invalid_request("ids must be integers")
    return list(dict.fromkeys(ids))


def _bulk_shift(shift) -> tuple[str, int]:
    """The (unit, amount) of a shift request; the one-year bound is checked per post by shift_local."""
    if not isinstance(shift, dict):
        raise invalid_request("shift must be an object with unit and amount")
    unit, amount = shift.get("unit"), shift.get("amount")
    if not isinstance(unit, str) or unit not in _SHIFT_UNITS:
        raise invalid_request("Unit must be 'hours' or 'days'")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise invalid_request("Amount must be a whole number")
    if amount == 0:
        raise invalid_request("Amount must not be zero")
    return unit, amount


async def _shift_post(
    user_id: int, post_id: int, unit: str, amount: int, scheduler: SchedulerLike, now: datetime
) -> None:
    """Move one Scheduled post's time on the Kyiv wall clock and its job with it.

    Only Scheduled posts shift: a queued post has no time, and a failed post
    would otherwise be accepted by the shared time write.
    """
    row = await asyncio.to_thread(get_owned_post, user_id, post_id)
    if board_status(row) != "scheduled":
        raise _refusal("shift", row)
    try:
        new_time = shift_local(row["scheduled_time"], unit, amount)
    except OverflowError:  # a time at the edge of the datetime range
        raise invalid_request("Time is out of range") from None
    ensure_min_lead(new_time, now)
    await _set_scheduled_time(user_id, post_id, new_time, scheduler, now)


async def bulk(
    user_id: int,
    action: str,
    ids: list[int],
    scheduler: SchedulerLike,
    channel_id: str | None = None,
    shift: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Apply `action` (delete, restore, move_channel or shift) to up to BULK_MAX of the user's posts.

    A malformed request, or a target channel the user does not own, is
    invalid_request and changes nothing. Otherwise every distinct id is tried in
    first-seen order and a post that cannot be changed (not found, wrong status,
    a shifted time that is too soon or does not exist) is reported as
    {"id", "ok": False, "error": text} without stopping the rest.
    Returns {"results": [{"id", "ok", "error"}, ...]}.
    """
    if action not in _BULK_ACTIONS:
        raise invalid_request(f"action must be one of: {', '.join(_BULK_ACTIONS)}")
    post_ids = _bulk_ids(ids)
    if now is None:
        now = get_current_kyiv_time()

    # `apply(post_id)` is the single-post call for this action.
    if action == "delete":
        apply = functools.partial(delete_post, user_id, scheduler=scheduler)
    elif action == "restore":
        apply = functools.partial(restore_post, user_id, scheduler=scheduler, now=now)
    elif action == "move_channel":
        target = await _owned_channel(user_id, channel_id)
        apply = functools.partial(
            update_post, user_id, changes={"channel_id": target}, scheduler=scheduler, now=now
        )
    else:
        unit, amount = _bulk_shift(shift)
        apply = functools.partial(
            _shift_post, user_id, unit=unit, amount=amount, scheduler=scheduler, now=now
        )

    results = []
    for post_id in post_ids:
        try:
            await apply(post_id)
        except WebError as err:
            results.append({"id": post_id, "ok": False, "error": err.message})
        else:
            results.append({"id": post_id, "ok": True, "error": None})
    return {"results": results}

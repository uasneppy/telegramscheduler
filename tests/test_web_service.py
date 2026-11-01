"""Web board single-post mutations: status gates, one conditional write, scheduler sync."""

import asyncio
import json
import logging
from datetime import datetime, timedelta

import pytest
import pytz

from bot.database import Database, serialize_dt
from bot.web import service
from bot.web.errors import WebError, conflict
from bot.web.queries import get_owned_post
from web_helpers import FakeScheduler, add_channel, insert_post

KYIV = pytz.timezone("Europe/Kiev")
NOW = KYIV.localize(datetime(2030, 6, 1, 12, 0))
FUTURE = KYIV.localize(datetime(2030, 9, 1, 12, 0))
FUTURE_INPUT = "2030-09-01T12:00"
LATER = KYIV.localize(datetime(2030, 10, 1, 9, 30))
LATER_INPUT = "2030-10-01T09:30"
PAST = KYIV.localize(datetime(2030, 5, 1, 12, 0))


def _row(post_id, user_id=1):
    """The stored row, deleted or not."""
    return get_owned_post(user_id, post_id, include_deleted=True)


def _fails(code, coro) -> WebError:
    with pytest.raises(WebError) as exc:
        asyncio.run(coro)
    assert exc.value.code == code, exc.value.message
    return exc.value


def _update(post_id, changes, scheduler, user_id=1):
    return service.update_post(user_id, post_id, changes, scheduler, now=NOW)


# --- update_post ------------------------------------------------------------------


def test_update_caption_stores_text_and_entities_no_scheduler_call():
    old_text = "Visit https://example.com today"
    url = {"type": "url", "offset": 6, "length": 19}
    pid = insert_post(scheduled=FUTURE, description=old_text, caption_entities=json.dumps([url]))
    scheduler = FakeScheduler()

    tagged = "<b>Visit</b> https://example.com now"
    detail = asyncio.run(_update(pid, {"caption": tagged}, scheduler))

    row = _row(pid)
    assert row["description"] == "Visit https://example.com now"
    # The bold entity comes from the tag; the auto-detected URL is carried over.
    assert json.loads(row["caption_entities"]) == [{"type": "bold", "offset": 0, "length": 5}, url]
    assert row["status"] == "pending" and row["scheduled_time"] == FUTURE
    assert scheduler.calls == []
    assert detail["id"] == pid and detail["caption_tagged"] == tagged

    error = _fails("invalid_request", _update(pid, {"caption": "x" * 1025}, scheduler))
    assert "1024" in error.message
    assert _row(pid)["description"] == "Visit https://example.com now"

    asyncio.run(_update(pid, {"caption": "  "}, scheduler))
    row = _row(pid)
    assert row["description"] is None and row["caption_entities"] is None
    assert scheduler.calls == []


def test_update_time_on_queued_schedules():
    pid = insert_post()
    scheduler = FakeScheduler()

    detail = asyncio.run(_update(pid, {"scheduled_time": FUTURE_INPUT}, scheduler))

    row = _row(pid)
    assert row["status"] == "pending" and row["scheduled_time"] == FUTURE
    assert scheduler.calls == [("schedule", pid, FUTURE)]
    assert detail["status"] == "scheduled"
    assert detail["scheduled_time"] == serialize_dt(FUTURE)


def test_update_time_changes_scheduled_post_job():
    pid = insert_post(scheduled=FUTURE)
    scheduler = FakeScheduler()

    asyncio.run(_update(pid, {"scheduled_time": LATER_INPUT}, scheduler))

    assert _row(pid)["scheduled_time"] == LATER
    assert scheduler.calls == [("schedule", pid, LATER)]


@pytest.mark.parametrize("value, reason", [
    ("2030-06-01T12:00", "60 seconds"),        # now: under the minimum lead
    ("2030-05-01T12:00", "60 seconds"),        # in the past
    ("2030-09-01 12:00", "YYYY-MM-DDTHH:MM"),  # wrong format
    ("2030-02-30T12:00", "not a valid"),       # no such day
    ("0001-01-01T00:00", "out of range"),      # localizing overflows
    (1893456000, "YYYY-MM-DDTHH:MM"),          # not a string
])
def test_update_rejects_bad_time(value, reason):
    pid = insert_post(scheduled=FUTURE)
    scheduler = FakeScheduler()

    error = _fails("invalid_request", _update(pid, {"scheduled_time": value}, scheduler))

    assert reason in error.message
    assert _row(pid)["scheduled_time"] == FUTURE
    assert scheduler.calls == []


def test_update_time_null_unschedules():
    pid = insert_post(scheduled=FUTURE)
    scheduler = FakeScheduler()

    detail = asyncio.run(_update(pid, {"scheduled_time": None}, scheduler))

    row = _row(pid)
    assert row["status"] == "pending" and row["scheduled_time"] is None
    assert scheduler.calls == [("cancel", pid)]
    assert detail["status"] == "queued"


def test_update_time_on_failed_stores_without_job():
    pid = insert_post(status="failed", failure_reason="boom")
    scheduler = FakeScheduler()

    detail = asyncio.run(_update(pid, {"scheduled_time": FUTURE_INPUT}, scheduler))
    row = _row(pid)
    assert row["status"] == "failed" and row["scheduled_time"] == FUTURE
    assert detail["status"] == "failed"

    asyncio.run(_update(pid, {"scheduled_time": None}, scheduler))
    assert _row(pid)["scheduled_time"] is None
    assert scheduler.calls == []


def test_update_channel_ownership():
    add_channel(1, "-1001", "Alpha")
    add_channel(1, "-1002", "Beta")
    add_channel(2, "-2001", "Other")
    pid = insert_post(scheduled=FUTURE, channel_id="-1001")
    scheduler = FakeScheduler()

    detail = asyncio.run(_update(pid, {"channel_id": "-1002"}, scheduler))
    assert _row(pid)["channel_id"] == "-1002"
    assert detail["channel_name"] == "Beta"

    for channel_id in ("-2001", "-9999", "", None, -1001):
        _fails("invalid_request", _update(pid, {"channel_id": channel_id}, scheduler))
    assert _row(pid)["channel_id"] == "-1002"
    assert scheduler.calls == []


def test_update_is_atomic():
    add_channel(1, "-1001", "Alpha")
    add_channel(1, "-1002", "Beta")
    pid = insert_post(scheduled=FUTURE, description="old", channel_id="-1001")
    scheduler = FakeScheduler()

    for bad in (
        {"caption": "new", "scheduled_time": "2030-01-01T10:00"},  # valid caption, past time
        {"caption": "new", "channel_id": "-9999"},                  # valid caption, unknown channel
        {"scheduled_time": LATER_INPUT, "caption": "<b>new"},       # valid time, unclosed tag
    ):
        _fails("invalid_request", _update(pid, bad, scheduler))
        row = _row(pid)
        assert (row["description"], row["scheduled_time"], row["channel_id"]) == ("old", FUTURE, "-1001")
    assert scheduler.calls == []

    asyncio.run(_update(pid, {"caption": "new", "scheduled_time": LATER_INPUT, "channel_id": "-1002"}, scheduler))
    row = _row(pid)
    assert (row["description"], row["scheduled_time"], row["channel_id"]) == ("new", LATER, "-1002")
    assert scheduler.calls == [("schedule", pid, LATER)]


def test_update_rejects_unknown_or_empty_changes():
    pid = insert_post(scheduled=FUTURE, description="old")
    scheduler = FakeScheduler()

    for changes in ({}, {"status": "posted"}, {"caption": "new", "media_type": "video"}, None, ["caption"]):
        _fails("invalid_request", _update(pid, changes, scheduler))

    row = _row(pid)
    assert row["description"] == "old" and row["status"] == "pending"
    assert scheduler.calls == []


def test_update_status_gates():
    scheduler = FakeScheduler()
    for status in ("sending", "posted"):
        pid = insert_post(status=status, scheduled=FUTURE, description="old")
        error = _fails("conflict", _update(pid, {"caption": "new"}, scheduler))
        assert error.extra == {"status": status}
        assert _row(pid)["description"] == "old"
        # Ownership and status are checked before the input is validated.
        _fails("conflict", _update(pid, {"caption": "<b>bad", "scheduled_time": "2030-01-01T10:00"}, scheduler))

    deleted = insert_post(status="deleted", deleted_from_status="pending")
    others = insert_post(user_id=2, scheduled=FUTURE, description="old")
    for user_id, pid in ((1, deleted), (1, others), (1, 999_999), (1, "1"), (1, 2 ** 70)):
        _fails("not_found", _update(pid, {"caption": "new"}, scheduler, user_id=user_id))
    _fails("not_found", _update(others, {"caption": "<b>bad"}, scheduler))
    assert _row(others, user_id=2)["description"] == "old"
    assert scheduler.calls == []


# --- _apply_update and _set_scheduled_time ---------------------------------------


def test_apply_update_loses_race():
    pid = insert_post(scheduled=FUTURE, description="old")
    assert Database.claim_post_for_sending(pid)

    assert service._apply_update(1, pid, {"description": "new"}, ("pending", "failed")) is False

    row = _row(pid)
    assert row["status"] == "sending" and row["description"] == "old"


def test_apply_update_is_scoped_to_owner_and_status():
    pid = insert_post(status="failed", description="old")

    assert service._apply_update(2, pid, {"description": "x"}, ("failed",)) is False
    assert service._apply_update(1, pid, {"description": "x"}, ("pending",)) is False
    assert _row(pid)["description"] == "old"

    assert service._apply_update(1, pid, {"description": "new", "scheduled_time": FUTURE}, ("failed",)) is True
    row = _row(pid)
    assert row["description"] == "new" and row["scheduled_time"] == FUTURE

    with pytest.raises(ValueError):
        service._apply_update(1, pid, {"user_id": 2}, ("failed",))
    assert _row(pid)["user_id"] == 1


def test_set_scheduled_time_syncs_job_by_status():
    pending = insert_post(scheduled=FUTURE)
    failed = insert_post(status="failed", scheduled=FUTURE)
    scheduler = FakeScheduler()

    asyncio.run(service._set_scheduled_time(1, pending, LATER, scheduler, NOW))
    asyncio.run(service._set_scheduled_time(1, failed, LATER, scheduler, NOW))
    assert _row(pending)["scheduled_time"] == LATER and _row(failed)["scheduled_time"] == LATER
    assert scheduler.calls == [("schedule", pending, LATER)]

    asyncio.run(service._set_scheduled_time(1, pending, None, scheduler, NOW))
    assert _row(pending)["scheduled_time"] is None
    assert scheduler.calls[-1] == ("cancel", pending)

    calls = list(scheduler.calls)
    _fails("invalid_request", service._set_scheduled_time(1, failed, NOW + timedelta(seconds=59), scheduler, NOW))
    sending = insert_post(status="sending", scheduled=FUTURE)
    error = _fails("conflict", service._set_scheduled_time(1, sending, LATER, scheduler, NOW))
    assert error.extra == {"status": "sending"}
    _fails("not_found", service._set_scheduled_time(2, failed, FUTURE, scheduler, NOW))
    assert _row(failed)["scheduled_time"] == LATER and _row(sending)["scheduled_time"] == FUTURE
    assert scheduler.calls == calls


# --- races with the publish job ----------------------------------------------------

RACES = {
    # action: (starting status, what the bot does just after the board read the row, the call)
    "update": (
        "pending", Database.claim_post_for_sending,
        lambda pid, s: service.update_post(1, pid, {"caption": "new", "scheduled_time": LATER_INPUT}, s, now=NOW),
    ),
    "post_now": ("pending", Database.claim_post_for_sending, lambda pid, s: service.post_now(1, pid, s, now=NOW)),
    "delete": ("pending", Database.claim_post_for_sending, lambda pid, s: service.delete_post(1, pid, s)),
    "retry": (
        "failed", lambda pid: Database.retry_failed_post(pid, 1),
        lambda pid, s: service.retry_post(1, pid, s, now=NOW),
    ),
}


@pytest.mark.parametrize("action", sorted(RACES))
def test_status_change_after_read_conflicts_without_scheduler_call(monkeypatch, action):
    status, interfere, call = RACES[action]
    pid = insert_post(status=status, scheduled=FUTURE, description="old")
    before = _row(pid)
    real_read = service.get_owned_post

    def read_then_interfere(*args, **kwargs):
        row = real_read(*args, **kwargs)
        interfere(pid)
        return row

    monkeypatch.setattr(service, "get_owned_post", read_then_interfere)
    scheduler = FakeScheduler()

    error = _fails("conflict", call(pid, scheduler))

    after = _row(pid)
    assert error.extra == {"status": service.board_status(after)}
    assert after["status"] != before["status"]
    assert (after["description"], after["scheduled_time"]) == ("old", FUTURE)
    assert after["deleted_from_status"] is None
    assert scheduler.calls == []


# --- delete and restore ------------------------------------------------------------


def test_delete_records_previous_status(caplog):
    caplog.set_level(logging.INFO, logger="bot.web.service")
    scheduler = FakeScheduler()
    for status in ("pending", "failed", "posted"):
        pid = insert_post(status=status, scheduled=FUTURE)

        assert asyncio.run(service.delete_post(1, pid, scheduler)) == {"id": pid, "status": "deleted"}

        row = _row(pid)
        assert row["status"] == "deleted" and row["deleted_from_status"] == status
        assert scheduler.calls[-1] == ("cancel", pid)
    assert any("user 1" in r.getMessage() and f"delete post {pid}" in r.getMessage() for r in caplog.records)

    calls = list(scheduler.calls)
    sending = insert_post(status="sending", scheduled=FUTURE)
    error = _fails("conflict", service.delete_post(1, sending, scheduler))
    assert error.extra == {"status": "sending"}
    assert _row(sending)["status"] == "sending"
    _fails("not_found", service.delete_post(1, pid, scheduler))  # already deleted
    _fails("not_found", service.delete_post(2, sending, scheduler))
    assert scheduler.calls == calls


def _deleted(status="pending", **cols):
    """A post the board deleted from `status`."""
    pid = insert_post(status=status, **cols)
    asyncio.run(service.delete_post(1, pid, FakeScheduler()))
    return pid


def test_restore_pending_future_time_rearms():
    pid = _deleted(scheduled=FUTURE)
    scheduler = FakeScheduler()

    detail = asyncio.run(service.restore_post(1, pid, scheduler, now=NOW))

    row = _row(pid)
    assert row["status"] == "pending" and row["scheduled_time"] == FUTURE
    assert row["deleted_from_status"] is None
    assert scheduler.calls == [("schedule", pid, FUTURE)]
    assert detail["status"] == "scheduled"


def test_restore_pending_past_time_goes_to_queue():
    pid = _deleted(scheduled=PAST)
    queued = _deleted()
    scheduler = FakeScheduler()

    detail = asyncio.run(service.restore_post(1, pid, scheduler, now=NOW))
    asyncio.run(service.restore_post(1, queued, scheduler, now=NOW))

    for post_id in (pid, queued):
        row = _row(post_id)
        assert row["status"] == "pending" and row["scheduled_time"] is None
    assert detail["status"] == "queued" and detail["overdue"] is False
    assert scheduler.calls == []


def test_restore_failed_and_posted_keep_status_and_times():
    failed_past = _deleted("failed", scheduled=PAST, failure_reason="boom", retry_count=2)
    failed_future = _deleted("failed", scheduled=FUTURE, failure_reason="boom")
    posted = _deleted("posted", scheduled=PAST, posted_at=serialize_dt(PAST))
    scheduler = FakeScheduler()

    for pid in (failed_past, failed_future, posted):
        asyncio.run(service.restore_post(1, pid, scheduler, now=NOW))

    row = _row(failed_past)
    assert (row["status"], row["scheduled_time"], row["failure_reason"], row["retry_count"]) == (
        "failed", PAST, "boom", 2)
    row = _row(failed_future)
    assert (row["status"], row["scheduled_time"]) == ("failed", FUTURE)
    row = _row(posted)
    assert (row["status"], row["scheduled_time"], row["posted_at"]) == ("posted", PAST, PAST)
    assert scheduler.calls == []


def test_restore_without_previous_status_conflicts():
    pid = insert_post(scheduled=FUTURE)
    assert Database.delete_post(pid, 1)
    # Restoring clears deleted_from_status, so a later bot delete is not restorable either.
    again = _deleted(scheduled=FUTURE)
    asyncio.run(service.restore_post(1, again, FakeScheduler(), now=NOW))
    assert Database.delete_post(again, 1)
    scheduler = FakeScheduler()

    for post_id in (pid, again):
        error = _fails("conflict", service.restore_post(1, post_id, scheduler, now=NOW))
        assert error.extra == {"status": "deleted"}
        assert _row(post_id)["status"] == "deleted"
    assert scheduler.calls == []


def test_restore_non_deleted_conflicts():
    pid = insert_post(scheduled=FUTURE)
    others = _deleted()  # user 1's deleted post, restored below by user 2
    scheduler = FakeScheduler()

    error = _fails("conflict", service.restore_post(1, pid, scheduler, now=NOW))
    assert error.extra == {"status": "scheduled"}
    _fails("not_found", service.restore_post(2, others, scheduler, now=NOW))
    _fails("not_found", service.restore_post(1, 999_999, scheduler, now=NOW))
    assert _row(others)["status"] == "deleted"
    assert scheduler.calls == []


# --- post now and retry --------------------------------------------------------------


def test_post_now():
    queued = insert_post()
    scheduled = insert_post(scheduled=FUTURE)
    scheduler = FakeScheduler()
    soon = NOW + timedelta(seconds=60)

    detail = asyncio.run(service.post_now(1, queued, scheduler, now=NOW))
    asyncio.run(service.post_now(1, scheduled, scheduler, now=NOW))

    for pid in (queued, scheduled):
        row = _row(pid)
        assert row["status"] == "pending" and row["scheduled_time"] == soon
    assert scheduler.calls == [("schedule", queued, soon), ("schedule", scheduled, soon)]
    assert detail["status"] == "scheduled"

    for status in ("failed", "posted", "sending"):
        pid = insert_post(status=status, scheduled=FUTURE)
        error = _fails("conflict", service.post_now(1, pid, scheduler, now=NOW))
        assert error.extra == {"status": status}
        assert _row(pid)["scheduled_time"] == FUTURE
    _fails("not_found", service.post_now(2, queued, scheduler, now=NOW))
    assert len(scheduler.calls) == 2


def test_retry():
    future = insert_post(status="failed", scheduled=FUTURE, retry_count=3, failure_reason="boom")
    past = insert_post(status="failed", scheduled=PAST, retry_count=1, failure_reason="boom")
    unset = insert_post(status="failed", failure_reason="boom")
    scheduler = FakeScheduler()
    soon = NOW + timedelta(seconds=10)

    detail = asyncio.run(service.retry_post(1, future, scheduler, now=NOW))
    for pid in (past, unset):
        asyncio.run(service.retry_post(1, pid, scheduler, now=NOW))

    for pid, when in ((future, FUTURE), (past, soon), (unset, soon)):
        row = _row(pid)
        assert (row["status"], row["retry_count"], row["failure_reason"]) == ("pending", 0, None)
        assert row["scheduled_time"] == when
    assert scheduler.calls == [("schedule", future, FUTURE), ("schedule", past, soon), ("schedule", unset, soon)]
    assert detail["status"] == "scheduled" and detail["failure_reason"] is None

    for status, scheduled_time in (("pending", None), ("pending", FUTURE), ("posted", PAST), ("sending", FUTURE)):
        pid = insert_post(status=status, scheduled=scheduled_time)
        _fails("conflict", service.retry_post(1, pid, scheduler, now=NOW))
        assert _row(pid)["status"] == status
    assert len(scheduler.calls) == 3


# --- errors ----------------------------------------------------------------------------


def test_conflict_extra_may_name_status():
    error = conflict("Busy", status="sending")

    assert (error.status, error.code, error.message) == (409, "conflict", "Busy")
    assert error.extra == {"status": "sending"}
    assert WebError(400, "invalid_request", "Bad").extra == {}

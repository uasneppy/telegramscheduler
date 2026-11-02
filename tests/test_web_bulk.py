"""Web board bulk actions: request validation, per-post results, scheduler sync."""

import asyncio
from datetime import datetime, timedelta

import pytest
import pytz

from bot.web import service
from bot.web.errors import WebError
from bot.web.queries import get_owned_post
from web_helpers import FakeScheduler, add_channel, insert_post

KYIV = pytz.timezone("Europe/Kiev")
NOW = KYIV.localize(datetime(2030, 6, 1, 12, 0))
FUTURE = KYIV.localize(datetime(2030, 9, 1, 12, 0))
LATER = KYIV.localize(datetime(2030, 10, 1, 9, 30))

# Kyiv clocks jump 03:00 -> 04:00 on 2030-03-31 and go back 04:00 -> 03:00 on 2030-10-27.
SPRING_NOW = KYIV.localize(datetime(2030, 3, 1, 12, 0))
AUTUMN_NOW = KYIV.localize(datetime(2030, 10, 1, 12, 0))


def _local(*parts):
    return KYIV.localize(datetime(*parts))


def _row(post_id, user_id=1):
    """The stored row, deleted or not."""
    return get_owned_post(user_id, post_id, include_deleted=True)


def _bulk(action, ids, scheduler, now=NOW, user_id=1, **kwargs):
    return asyncio.run(service.bulk(user_id, action, ids, scheduler, now=now, **kwargs))


def _fails(action, ids, scheduler, **kwargs) -> WebError:
    with pytest.raises(WebError) as exc:
        _bulk(action, ids, scheduler, **kwargs)
    assert exc.value.code == "invalid_request", exc.value.message
    return exc.value


def _ok(post_id):
    return {"id": post_id, "ok": True, "error": None}


def _skipped(post_id, error):
    return {"id": post_id, "ok": False, "error": error}


# --- request validation ----------------------------------------------------------------


@pytest.mark.parametrize("action, ids, kwargs, reason", [
    ("archive", [1], {}, "action must be one of"),
    (None, [1], {}, "action must be one of"),
    (["delete"], [1], {}, "action must be one of"),
    ("delete", [], {}, "non-empty list"),
    ("delete", None, {}, "non-empty list"),
    ("delete", "1,2", {}, "non-empty list"),
    ("delete", {"1": 1}, {}, "non-empty list"),
    ("delete", list(range(1, service.BULK_MAX + 2)), {}, "At most 500"),
    ("delete", [1, "2"], {}, "integers"),
    ("delete", [1.5], {}, "integers"),
    ("delete", [True], {}, "integers"),
    ("delete", [None], {}, "integers"),
    ("delete", [[1]], {}, "integers"),
    ("move_channel", [1], {}, "channel_id"),
    ("move_channel", [1], {"channel_id": ""}, "channel_id"),
    ("move_channel", [1], {"channel_id": -1001}, "channel_id"),
    ("shift", [1], {}, "object"),
    ("shift", [1], {"shift": []}, "object"),
    ("shift", [1], {"shift": {}}, "Unit"),
    ("shift", [1], {"shift": {"unit": "weeks", "amount": 1}}, "Unit"),
    ("shift", [1], {"shift": {"unit": ["hours"], "amount": 1}}, "Unit"),
    ("shift", [1], {"shift": {"unit": None, "amount": 1}}, "Unit"),
    ("shift", [1], {"shift": {"unit": "days"}}, "Amount must be a whole number"),
    ("shift", [1], {"shift": {"unit": "days", "amount": "1"}}, "Amount must be a whole number"),
    ("shift", [1], {"shift": {"unit": "days", "amount": 1.5}}, "Amount must be a whole number"),
    ("shift", [1], {"shift": {"unit": "days", "amount": True}}, "Amount must be a whole number"),
    ("shift", [1], {"shift": {"unit": "days", "amount": 0}}, "not be zero"),
])
def test_request_validation(action, ids, kwargs, reason):
    add_channel(1, "-1001", "Alpha")
    pid = insert_post(scheduled=FUTURE, channel_id="-1001")
    scheduler = FakeScheduler()

    error = _fails(action, ids, scheduler, **kwargs)

    assert reason in error.message
    row = _row(pid)
    assert (row["status"], row["scheduled_time"], row["channel_id"]) == ("pending", FUTURE, "-1001")
    assert scheduler.calls == []


def test_request_with_max_ids_is_accepted():
    scheduler = FakeScheduler()

    result = _bulk("delete", list(range(1, service.BULK_MAX + 1)), scheduler)

    assert len(result["results"]) == service.BULK_MAX
    assert all(entry["error"] == "Post not found" for entry in result["results"])


def test_the_request_is_checked_before_any_post_is_touched():
    pid = insert_post(scheduled=FUTURE)
    scheduler = FakeScheduler()

    # The first id is a valid scheduled post, the last one is not an integer.
    _fails("delete", [pid, "x"], scheduler)

    assert _row(pid)["status"] == "pending"
    assert scheduler.calls == []


# --- results --------------------------------------------------------------------------


def test_mixed_results_do_not_fail_batch():
    first = insert_post(scheduled=FUTURE)
    sending = insert_post(status="sending", scheduled=FUTURE)
    queued = insert_post()
    foreign = insert_post(user_id=2, scheduled=FUTURE)
    missing = foreign + 100
    posted = insert_post(status="posted")
    scheduler = FakeScheduler()

    result = _bulk("delete", [first, sending, queued, foreign, missing, posted], scheduler)

    assert result == {"results": [
        _ok(first),
        _skipped(sending, "Cannot delete this post: it is being sent right now."),
        _ok(queued),
        _skipped(foreign, "Post not found"),
        _skipped(missing, "Post not found"),
        _ok(posted),
    ]}
    assert [_row(pid)["status"] for pid in (first, sending, queued, posted)] == [
        "deleted", "sending", "deleted", "deleted",
    ]
    assert _row(foreign, user_id=2)["status"] == "pending"
    # A job is cancelled for exactly the posts that were deleted.
    assert scheduler.calls == [("cancel", first), ("cancel", queued), ("cancel", posted)]


def test_duplicates_deduped():
    first = insert_post(scheduled=FUTURE)
    second = insert_post(scheduled=FUTURE)
    scheduler = FakeScheduler()

    result = _bulk("delete", [second, first, second, second, first], scheduler)

    # First-seen order, one result per post; a repeat would have been a conflict.
    assert result == {"results": [_ok(second), _ok(first)]}
    assert scheduler.calls == [("cancel", second), ("cancel", first)]


def test_delete_then_restore_round_trip():
    scheduled = insert_post(scheduled=FUTURE)
    queued = insert_post()
    failed = insert_post(status="failed", scheduled=FUTURE, failure_reason="boom", retry_count=3)
    posted = insert_post(status="posted", scheduled=LATER)
    live = insert_post(scheduled=FUTURE)
    ids = [scheduled, queued, failed, posted]
    scheduler = FakeScheduler()

    deleted = _bulk("delete", ids, scheduler)
    assert deleted == {"results": [_ok(pid) for pid in ids]}
    assert [_row(pid)["status"] for pid in ids] == ["deleted"] * 4
    assert [_row(pid)["deleted_from_status"] for pid in ids] == ["pending", "pending", "failed", "posted"]
    scheduler.calls.clear()

    restored = _bulk("restore", [*ids, live], scheduler)

    assert restored == {"results": [
        *[_ok(pid) for pid in ids],
        _skipped(live, "Cannot restore this post: it is scheduled."),
    ]}
    assert [_row(pid)["status"] for pid in ids] == ["pending", "pending", "failed", "posted"]
    assert [_row(pid)["scheduled_time"] for pid in ids] == [FUTURE, None, FUTURE, LATER]
    assert all(_row(pid)["deleted_from_status"] is None for pid in ids)
    assert _row(failed)["failure_reason"] == "boom" and _row(failed)["retry_count"] == 3
    # Only the post that is pending with a time ahead gets its job back.
    assert scheduler.calls == [("schedule", scheduled, FUTURE)]


def test_restore_skips_posts_the_bot_deleted():
    pid = insert_post(status="deleted", scheduled=FUTURE)  # deleted_from_status was never set
    scheduler = FakeScheduler()

    result = _bulk("restore", [pid], scheduler)

    assert result == {"results": [_skipped(pid, "Cannot restore this post: it was not deleted on the board.")]}
    assert _row(pid)["status"] == "deleted"
    assert scheduler.calls == []


# --- move_channel ---------------------------------------------------------------------


def test_move_channel_changes_editable_posts_only():
    add_channel(1, "-1001", "Alpha")
    add_channel(1, "-1002", "Beta")
    scheduled = insert_post(scheduled=FUTURE, channel_id="-1001")
    queued = insert_post(channel_id="-1001")
    failed = insert_post(status="failed", scheduled=FUTURE, channel_id="-1001")
    posted = insert_post(status="posted", channel_id="-1001")
    sending = insert_post(status="sending", scheduled=FUTURE, channel_id="-1001")
    scheduler = FakeScheduler()

    result = _bulk("move_channel", [scheduled, queued, failed, posted, sending], scheduler, channel_id="-1002")

    assert result == {"results": [
        _ok(scheduled),
        _ok(queued),
        _ok(failed),
        _skipped(posted, "Cannot edit this post: it has already been posted."),
        _skipped(sending, "Cannot edit this post: it is being sent right now."),
    ]}
    assert [_row(pid)["channel_id"] for pid in (scheduled, queued, failed, posted, sending)] == [
        "-1002", "-1002", "-1002", "-1001", "-1001",
    ]
    assert _row(scheduled)["scheduled_time"] == FUTURE
    assert scheduler.calls == []  # a channel change leaves every job as it is


def test_move_to_unowned_channel_rejects_whole_request():
    add_channel(1, "-1001", "Alpha")
    add_channel(2, "-2001", "Other")
    pids = [insert_post(scheduled=FUTURE, channel_id="-1001"), insert_post(channel_id="-1001")]
    scheduler = FakeScheduler()

    for channel_id in ("-2001", "-9999"):
        error = _fails("move_channel", pids, scheduler, channel_id=channel_id)
        assert error.message == "Unknown channel"

    assert [_row(pid)["channel_id"] for pid in pids] == ["-1001", "-1001"]
    assert scheduler.calls == []


# --- shift ----------------------------------------------------------------------------


def test_shift_moves_scheduled_posts_and_their_jobs():
    first = insert_post(scheduled=FUTURE)
    second = insert_post(scheduled=LATER)
    scheduler = FakeScheduler()

    result = _bulk("shift", [first, second], scheduler, shift={"unit": "hours", "amount": 5})

    assert result == {"results": [_ok(first), _ok(second)]}
    assert _row(first)["scheduled_time"] == FUTURE + timedelta(hours=5)
    assert _row(second)["scheduled_time"] == LATER + timedelta(hours=5)
    assert scheduler.calls == [
        ("schedule", first, FUTURE + timedelta(hours=5)),
        ("schedule", second, LATER + timedelta(hours=5)),
    ]


def test_shift_only_scheduled_posts():
    scheduled = insert_post(scheduled=FUTURE)
    queued = insert_post()
    failed = insert_post(status="failed", scheduled=FUTURE)
    sending = insert_post(status="sending", scheduled=FUTURE)
    posted = insert_post(status="posted", scheduled=FUTURE)
    deleted = insert_post(status="deleted", scheduled=FUTURE)
    foreign = insert_post(user_id=2, scheduled=FUTURE)
    scheduler = FakeScheduler()

    result = _bulk(
        "shift", [scheduled, queued, failed, sending, posted, deleted, foreign], scheduler,
        shift={"unit": "days", "amount": 1},
    )

    assert result == {"results": [
        _ok(scheduled),
        _skipped(queued, "Cannot shift this post: it is queued."),
        _skipped(failed, "Cannot shift this post: it has failed."),
        _skipped(sending, "Cannot shift this post: it is being sent right now."),
        _skipped(posted, "Cannot shift this post: it has already been posted."),
        _skipped(deleted, "Post not found"),
        _skipped(foreign, "Post not found"),
    ]}
    new_time = FUTURE + timedelta(days=1)
    assert _row(scheduled)["scheduled_time"] == new_time
    assert _row(queued)["scheduled_time"] is None
    for pid in (failed, sending, posted):
        assert _row(pid)["scheduled_time"] == FUTURE
    assert _row(foreign, user_id=2)["scheduled_time"] == FUTURE
    assert scheduler.calls == [("schedule", scheduled, new_time)]


@pytest.mark.parametrize("unit, amount, start, expected", [
    ("days", 1, _local(2030, 3, 30, 10, 0), _local(2030, 3, 31, 10, 0)),
    ("hours", 24, _local(2030, 3, 30, 10, 0), _local(2030, 3, 31, 10, 0)),
    ("days", -1, _local(2030, 3, 31, 10, 0), _local(2030, 3, 30, 10, 0)),
])
def test_shift_across_dst_keeps_wall_clock(unit, amount, start, expected):
    pid = insert_post(scheduled=start)
    scheduler = FakeScheduler()

    result = _bulk("shift", [pid], scheduler, now=SPRING_NOW, shift={"unit": unit, "amount": amount})

    assert result == {"results": [_ok(pid)]}
    stored = _row(pid)["scheduled_time"]
    assert stored == expected
    assert stored.replace(tzinfo=None) == expected.replace(tzinfo=None)
    # The UTC offset changed with the clocks, so this is not a plain 24-hour jump.
    assert stored.utcoffset() != start.utcoffset()
    assert scheduler.calls == [("schedule", pid, expected)]


def test_shift_into_past_or_gap_skipped():
    ahead = insert_post(scheduled=NOW + timedelta(days=3))
    too_close = insert_post(scheduled=NOW + timedelta(days=1, seconds=59))
    past = insert_post(scheduled=NOW + timedelta(hours=1))
    scheduler = FakeScheduler()

    result = _bulk("shift", [ahead, too_close, past], scheduler, shift={"unit": "days", "amount": -1})

    lead_error = "Scheduled time must be at least 60 seconds in the future"
    assert result == {"results": [_ok(ahead), _skipped(too_close, lead_error), _skipped(past, lead_error)]}
    assert _row(ahead)["scheduled_time"] == NOW + timedelta(days=2)
    assert _row(too_close)["scheduled_time"] == NOW + timedelta(days=1, seconds=59)
    assert _row(past)["scheduled_time"] == NOW + timedelta(hours=1)
    assert scheduler.calls == [("schedule", ahead, NOW + timedelta(days=2))]

    # Wall-clock results in the skipped hour (spring) and in the repeated hour (autumn).
    fine = insert_post(scheduled=_local(2030, 3, 30, 10, 0))
    gap = insert_post(scheduled=_local(2030, 3, 30, 3, 30))
    scheduler = FakeScheduler()
    result = _bulk("shift", [fine, gap], scheduler, now=SPRING_NOW, shift={"unit": "days", "amount": 1})
    assert result["results"][0] == _ok(fine)
    assert result["results"][1]["id"] == gap and not result["results"][1]["ok"]
    assert "does not exist in Kyiv time" in result["results"][1]["error"]
    assert _row(gap)["scheduled_time"] == _local(2030, 3, 30, 3, 30)
    assert scheduler.calls == [("schedule", fine, _local(2030, 3, 31, 10, 0))]

    overlap = insert_post(scheduled=_local(2030, 10, 26, 3, 30))
    scheduler = FakeScheduler()
    result = _bulk("shift", [overlap], scheduler, now=AUTUMN_NOW, shift={"unit": "days", "amount": 1})
    assert not result["results"][0]["ok"]
    assert "happens twice in Kyiv time" in result["results"][0]["error"]
    assert _row(overlap)["scheduled_time"] == _local(2030, 10, 26, 3, 30)
    assert scheduler.calls == []


def test_shift_out_of_range_is_reported_per_post():
    edge = insert_post(scheduled=_local(9999, 12, 30, 10, 0))
    normal = insert_post(scheduled=FUTURE)
    scheduler = FakeScheduler()

    result = _bulk("shift", [edge, normal], scheduler, shift={"unit": "days", "amount": 5})

    assert result == {"results": [_skipped(edge, "Time is out of range"), _ok(normal)]}
    assert _row(edge)["scheduled_time"] == _local(9999, 12, 30, 10, 0)
    assert scheduler.calls == [("schedule", normal, FUTURE + timedelta(days=5))]


def test_shift_over_one_year_is_reported_per_post():
    pid = insert_post(scheduled=FUTURE)
    scheduler = FakeScheduler()

    result = _bulk("shift", [pid], scheduler, shift={"unit": "days", "amount": 366})

    assert result == {"results": [_skipped(pid, "Amount must be at most 365 days")]}
    assert _row(pid)["scheduled_time"] == FUTURE
    assert scheduler.calls == []

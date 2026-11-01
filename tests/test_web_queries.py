"""Web board status rules and read queries: tabs, ordering, paging, search, ownership."""

import base64
import json
import os
import shutil
import uuid
from datetime import datetime, timedelta

import pytest
import pytz

from config import UPLOADS_DIR
from bot.database import serialize_dt
from bot.web.errors import WebError
from bot.web.queries import get_me, get_owned_post, get_post_detail, list_posts
from bot.web.rules import TABS, board_status, can_flags
from web_helpers import add_channel, insert_post

KYIV = pytz.timezone("Europe/Kiev")
NOW = KYIV.localize(datetime(2030, 6, 1, 12, 0))
FUTURE = KYIV.localize(datetime(2030, 9, 1, 12, 0))
PAST = KYIV.localize(datetime(2030, 5, 1, 12, 0))

ACTIONS = ("edit", "media", "post_now", "retry", "delete")
NO_ACTIONS = dict.fromkeys(ACTIONS, False)
ITEM_KEYS = {
    "id", "status", "channel_id", "channel_name", "scheduled_time", "posted_at", "caption_text",
    "media_type", "media_count", "is_recurring", "recurring_interval_hours", "overdue",
    "failure_reason", "retry_count",
}
DETAIL_KEYS = {
    "caption_tagged", "caption_html", "media", "signature", "signature_fits", "batch_id", "mode",
    "created_at", "can",
}
RAW_KEYS = {
    "id", "user_id", "file_path", "media_type", "description", "scheduled_time", "mode", "channel_id",
    "is_recurring", "recurring_interval_hours", "media_bundle_json", "status", "created_at", "posted_at",
    "batch_id", "retry_count", "failure_reason", "caption_entities", "deleted_from_status",
}


@pytest.fixture
def media_file():
    """Create real files inside UPLOADS_DIR (the only place media may be served from)."""
    folder = os.path.join(UPLOADS_DIR, "test_web_queries")
    os.makedirs(folder, exist_ok=True)

    def make(name="photo.jpg"):
        path = os.path.join(folder, f"{uuid.uuid4().hex}-{name}")
        with open(path, "wb") as handle:
            handle.write(b"data")
        return path

    yield make
    shutil.rmtree(folder, ignore_errors=True)


def _ids(page):
    return [item["id"] for item in page["items"]]


def _pages(user_id, status, **kwargs):
    """Every page of a listing, following next_cursor until it is None."""
    pages, cursor = [], None
    for _ in range(50):
        page = list_posts(user_id, status, cursor=cursor, now=NOW, **kwargs)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages
    pytest.fail("next_cursor never ended")


def _flat(pages):
    return [post_id for page in pages for post_id in _ids(page)]


def _encode(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _assert_error(call, status, code):
    with pytest.raises(WebError) as exc:
        call()
    assert (exc.value.status, exc.value.code) == (status, code)
    return exc.value


# --- rules ----------------------------------------------------------------------


def test_board_status_and_can_flags():
    assert TABS == ("scheduled", "queued", "failed", "posted")

    # 6.1 status model
    assert board_status({"status": "pending", "scheduled_time": None}) == "queued"
    assert board_status({"status": "pending", "scheduled_time": FUTURE}) == "scheduled"
    assert board_status({"status": "sending", "scheduled_time": FUTURE}) == "sending"
    assert board_status({"status": "failed", "scheduled_time": FUTURE}) == "failed"
    assert board_status({"status": "posted", "scheduled_time": PAST}) == "posted"
    assert board_status({"status": "deleted", "scheduled_time": None}) == "deleted"
    assert board_status({"status": "archived", "scheduled_time": None}) == "unknown"
    assert board_status({"status": None, "scheduled_time": None}) == "unknown"

    # 6.2: one assertion per row of the table
    editable = {"edit": True, "media": True, "post_now": True, "retry": False, "delete": True}
    failed = {"edit": True, "media": True, "post_now": False, "retry": True, "delete": True}
    posted = {"edit": False, "media": False, "post_now": False, "retry": False, "delete": True}
    assert can_flags("queued", False) == editable
    assert can_flags("scheduled", False) == editable
    assert can_flags("sending", False) == NO_ACTIONS
    assert can_flags("failed", False) == failed
    assert can_flags("posted", False) == posted
    assert can_flags("deleted", False) == NO_ACTIONS
    assert can_flags("unknown", False) == NO_ACTIONS

    # an album's media cannot be replaced; nothing else changes
    assert can_flags("queued", True) == dict(editable, media=False)
    assert can_flags("scheduled", True) == dict(editable, media=False)
    assert can_flags("failed", True) == dict(failed, media=False)
    assert can_flags("posted", True) == posted
    assert can_flags("sending", True) == NO_ACTIONS

    # every call hands out its own dict
    flags = can_flags("queued", False)
    flags["edit"] = False
    assert can_flags("queued", False)["edit"] is True


# --- tabs, counts, ordering -------------------------------------------------------


def test_counts_and_tabs():
    queued = [insert_post(), insert_post()]
    scheduled = insert_post(scheduled=FUTURE)
    sending = insert_post(status="sending", scheduled=FUTURE)
    failed = insert_post(status="failed", scheduled=FUTURE)
    posted = insert_post(status="posted", scheduled=PAST, posted_at=serialize_dt(PAST))
    insert_post(status="deleted", scheduled=FUTURE, deleted_from_status="pending")
    deleted = insert_post(status="deleted", deleted_from_status="pending")

    me = get_me(1, now=NOW)
    assert me["user_id"] == 1
    assert me["now"] == "2030-06-01T12:00:00+03:00"
    assert me["counts"] == {"scheduled": 2, "queued": 2, "failed": 1, "posted": 1}

    by_tab = {tab: list_posts(1, tab, now=NOW) for tab in TABS}
    assert _ids(by_tab["queued"]) == queued
    assert _ids(by_tab["scheduled"]) == [scheduled, sending]
    assert _ids(by_tab["failed"]) == [failed]
    assert _ids(by_tab["posted"]) == [posted]
    assert all(page["next_cursor"] is None for page in by_tab.values())
    statuses = {item["id"]: item["status"] for page in by_tab.values() for item in page["items"]}
    assert statuses == {
        queued[0]: "queued", queued[1]: "queued", scheduled: "scheduled",
        sending: "sending", failed: "failed", posted: "posted",
    }

    # deleted rows are hidden unless a caller asks for them explicitly
    _assert_error(lambda: get_post_detail(1, deleted, now=NOW), 404, "not_found")
    _assert_error(lambda: get_owned_post(1, deleted), 404, "not_found")
    assert get_owned_post(1, deleted, include_deleted=True)["status"] == "deleted"


def test_scheduled_order_is_dst_safe():
    # 2030-10-27: Kyiv clocks go back at 04:00 EEST, so 03:xx happens twice.
    later = insert_post(scheduled_time="2030-10-27T03:10:00+02:00")    # 01:10 UTC
    earlier = insert_post(scheduled_time="2030-10-27T03:30:00+03:00")  # 00:30 UTC
    assert "2030-10-27T03:10:00+02:00" < "2030-10-27T03:30:00+03:00"   # string order is wrong

    page = list_posts(1, "scheduled", now=NOW)
    assert _ids(page) == [earlier, later]
    assert [item["scheduled_time"] for item in page["items"]] == [
        "2030-10-27T03:30:00+03:00", "2030-10-27T03:10:00+02:00",
    ]
    # the cursor compares the same numeric key
    assert [_ids(p) for p in _pages(1, "scheduled", limit=1)] == [[earlier], [later]]


def test_pagination_ties_same_time():
    # The bot puts every channel on the same slots, so identical times are normal.
    ids = [insert_post(scheduled=FUTURE, channel_id=("-1001", "-1002")[i % 2]) for i in range(5)]

    pages = _pages(1, "scheduled", limit=2)
    assert [_ids(p) for p in pages] == [ids[0:2], ids[2:4], ids[4:]]
    assert [p["next_cursor"] is None for p in pages] == [False, False, True]

    # a page boundary exactly on the last item ends the listing there
    full = list_posts(1, "scheduled", limit=5, now=NOW)
    assert _ids(full) == ids and full["next_cursor"] is None
    assert [_ids(p) for p in _pages(1, "scheduled", limit=4)] == [ids[:4], ids[4:]]
    only_b = _pages(1, "scheduled", channel_id="-1002", limit=2)
    assert [_ids(p) for p in only_b] == [[ids[1], ids[3]]]

    # ties between other times: the earliest post has the highest id
    early = insert_post(scheduled=FUTURE - timedelta(hours=1))
    late = insert_post(scheduled=FUTURE + timedelta(hours=1))
    expected = [early, *ids, late]
    for limit in (1, 2, 3):
        assert _flat(_pages(1, "scheduled", limit=limit)) == expected


def test_tab_ordering_and_paging():
    # queued: COALESCE(sort_key, id), as set by "Shuffle queue"
    a = insert_post(sort_key=2)
    b = insert_post(sort_key=0)
    c = insert_post(sort_key=1)
    assert [_ids(p) for p in _pages(1, "queued", limit=1)] == [[b], [c], [a]]

    # failed: newest first
    failed = [insert_post(status="failed") for _ in range(3)]
    assert [_ids(p) for p in _pages(1, "failed", limit=2)] == [failed[:0:-1], failed[:1]]

    # posted: by posted_at descending, ties by id descending
    oldest = insert_post(status="posted", posted_at=serialize_dt(PAST - timedelta(days=2)))
    newer = insert_post(status="posted", posted_at=serialize_dt(PAST))
    tie = insert_post(status="posted", posted_at=serialize_dt(PAST))
    assert _ids(list_posts(1, "posted", now=NOW)) == [tie, newer, oldest]
    assert _flat(_pages(1, "posted", limit=1)) == [tie, newer, oldest]
    assert _flat(_pages(1, "posted", limit=2)) == [tie, newer, oldest]


# --- search -------------------------------------------------------------------------


def test_search_cyrillic_case_insensitive():
    hit = insert_post(description="Привіт, світ")
    hello = insert_post(description="Hello, world")
    insert_post(description=None)

    for q in ("привіт", "ПРИВІТ", "Світ"):
        assert _ids(list_posts(1, "queued", q=q, now=NOW)) == [hit]
    assert _ids(list_posts(1, "queued", q="HELLO", now=NOW)) == [hello]
    assert _ids(list_posts(1, "queued", q="nothing like it", now=NOW)) == []


def test_search_wildcards_are_literal():
    percent = insert_post(description="100% done")
    underscore = insert_post(description="a_b")
    insert_post(description="axb")
    backslash = insert_post(description="C:\\temp")

    assert _ids(list_posts(1, "queued", q="%", now=NOW)) == [percent]
    assert _ids(list_posts(1, "queued", q="a_b", now=NOW)) == [underscore]
    assert _ids(list_posts(1, "queued", q="_", now=NOW)) == [underscore]
    assert _ids(list_posts(1, "queued", q="\\", now=NOW)) == [backslash]
    assert _ids(list_posts(1, "queued", q="\\t", now=NOW)) == [backslash]
    # an empty search is no search
    assert len(list_posts(1, "queued", q="", now=NOW)["items"]) == 4


# --- parameters ---------------------------------------------------------------------


def test_param_validation():
    insert_post(scheduled=FUTURE)

    def invalid(**kwargs):
        kwargs.setdefault("status", "scheduled")
        _assert_error(lambda: list_posts(1, now=NOW, **kwargs), 400, "invalid_request")

    for status in ("bogus", "deleted", "sending", "", None):
        invalid(status=status)
    for limit in (0, 101, -1, True, "10", None, 2.5):
        invalid(limit=limit)
    for cursor in (
        "!!!",
        _encode("[" * 100_000 + "]" * 100_000),  # json.loads would raise RecursionError
        _encode("not json"),
        _encode('{"a": 1}'),
        _encode("[1]"),
        _encode("[1, 2, 3]"),
        _encode('[1, "x"]'),
        _encode("[true, 1]"),
        _encode("[1.5, 1]"),
        _encode("[null, 1]"),
        _encode(f"[{2 ** 63}, 1]"),   # does not fit an SQLite integer
        _encode("[" * 60),
        base64.urlsafe_b64encode(b"\xff\xfe").decode(),
        12345,
    ):
        invalid(cursor=cursor)

    # the boundaries themselves are fine, and an empty cursor means the first page
    assert len(list_posts(1, "scheduled", limit=1, now=NOW)["items"]) == 1
    assert len(list_posts(1, "scheduled", limit=100, now=NOW)["items"]) == 1
    assert len(list_posts(1, "scheduled", cursor="", now=NOW)["items"]) == 1


def test_channel_filter_and_names():
    add_channel(1, "-1001", "Alpha")
    add_channel(1, "-1003", "Gamma", signature="— G")
    add_channel(2, "-1002", "Beta")  # another user's channel with an id user 1 also posts to
    alpha = insert_post(scheduled=FUTURE, channel_id="-1001")
    unnamed = insert_post(scheduled=FUTURE, channel_id="-1002")

    page = list_posts(1, "scheduled", now=NOW)
    names = {item["id"]: (item["channel_id"], item["channel_name"]) for item in page["items"]}
    assert names == {alpha: ("-1001", "Alpha"), unnamed: ("-1002", "-1002")}
    assert get_post_detail(1, alpha, now=NOW)["channel_name"] == "Alpha"
    assert get_post_detail(1, unnamed, now=NOW)["channel_name"] == "-1002"

    assert _ids(list_posts(1, "scheduled", channel_id="-1001", now=NOW)) == [alpha]
    assert _ids(list_posts(1, "scheduled", channel_id="-1002", now=NOW)) == [unnamed]
    assert _ids(list_posts(1, "scheduled", channel_id="-1999", now=NOW)) == []
    assert _ids(list_posts(1, "scheduled", channel_id="", now=NOW)) == [alpha, unnamed]

    assert get_me(1, now=NOW)["channels"] == [
        {"channel_id": "-1001", "channel_name": "Alpha", "signature": None, "is_active": True},
        {"channel_id": "-1003", "channel_name": "Gamma", "signature": "— G", "is_active": True},
    ]


def test_overdue_flag():
    overdue = insert_post(scheduled=NOW - timedelta(hours=1))
    in_flight = insert_post(status="sending", scheduled=NOW - timedelta(minutes=5))
    future = insert_post(scheduled=NOW + timedelta(hours=1))
    insert_post(status="failed", scheduled=NOW - timedelta(hours=2))

    page = list_posts(1, "scheduled", now=NOW)
    assert _ids(page) == [overdue, in_flight, future]
    assert [item["overdue"] for item in page["items"]] == [True, False, False]
    assert list_posts(1, "failed", now=NOW)["items"][0]["overdue"] is False
    assert get_post_detail(1, overdue, now=NOW)["overdue"] is True

    # without an explicit now the real clock is used
    old = insert_post(scheduled=KYIV.localize(datetime(2020, 1, 1, 10, 0)))
    assert get_post_detail(1, old)["overdue"] is True
    assert get_post_detail(1, future)["overdue"] is False


# --- detail ---------------------------------------------------------------------------


def test_detail_shape(media_file):
    add_channel(1, "-1001", "Alpha", signature="— Alpha")
    path = media_file()
    pid = insert_post(
        scheduled=FUTURE, channel_id="-1001", file_path=path, media_type="photo", mode=2,
        description="Hello bold", caption_entities=json.dumps([{"type": "bold", "offset": 6, "length": 4}]),
        created_at="2030-01-01 10:00:00",  # CURRENT_TIMESTAMP form: naive UTC
        retry_count=1, is_recurring=1, recurring_interval_hours=24,
    )

    detail = get_post_detail(1, pid, now=NOW)
    assert set(detail) == ITEM_KEYS | DETAIL_KEYS
    assert detail["status"] == "scheduled"
    assert detail["scheduled_time"] == "2030-09-01T12:00:00+03:00"
    assert detail["posted_at"] is None
    assert detail["created_at"] == "2030-01-01T12:00:00+02:00"
    assert detail["caption_text"] == "Hello bold"
    assert detail["caption_tagged"] == "Hello <b>bold</b>"
    assert detail["caption_html"] == "Hello <b>bold</b>"
    assert detail["media_type"] == "photo" and detail["media_count"] == 1
    assert detail["media"] == [{"index": 0, "media_type": "photo", "exists": True}]
    assert detail["signature"] == "— Alpha"
    assert detail["signature_fits"] is True
    assert detail["can"] == {"edit": True, "media": True, "post_now": True, "retry": False, "delete": True}
    assert (detail["mode"], detail["batch_id"]) == (2, None)
    assert detail["is_recurring"] is True
    assert (detail["recurring_interval_hours"], detail["retry_count"]) == (24, 1)
    assert detail["failure_reason"] is None and detail["overdue"] is False
    json.dumps(detail)  # the API returns it as is

    # a list item is the detail without the detail-only keys
    item = list_posts(1, "scheduled", now=NOW)["items"][0]
    assert item == {key: detail[key] for key in ITEM_KEYS}

    # the raw row keeps stored values and parses scheduled_time
    row = get_owned_post(1, pid)
    assert set(row) == RAW_KEYS
    assert row["scheduled_time"] == FUTURE and isinstance(row["scheduled_time"], datetime)
    assert row["caption_entities"] == json.dumps([{"type": "bold", "offset": 6, "length": 4}])

    # the file is gone (published media is cleaned up): still opens, with a placeholder
    os.remove(path)
    assert get_post_detail(1, pid, now=NOW)["media"] == [{"index": 0, "media_type": "photo", "exists": False}]

    # signature that would push the caption over the limit
    long_caption = insert_post(channel_id="-1001", description="x" * 1020)
    assert get_post_detail(1, long_caption, now=NOW)["signature_fits"] is False

    # stored media types pass through unchanged
    image_doc = insert_post(file_path=media_file("scan.png"), media_type="document_image")
    image_detail = get_post_detail(1, image_doc, now=NOW)
    assert image_detail["media_type"] == "document_image"
    assert image_detail["media"] == [{"index": 0, "media_type": "document_image", "exists": True}]

    # album: one entry per item, and its media cannot be replaced
    first = media_file("one.jpg")
    album = insert_post(
        status="failed", media_type="album", file_path=first,
        media_bundle_json=json.dumps([
            {"file_path": first, "media_type": "photo"},
            {"file_path": os.path.join(UPLOADS_DIR, "gone.mp4"), "media_type": "video"},
        ]),
    )
    album_detail = get_post_detail(1, album, now=NOW)
    assert album_detail["media_type"] == "album" and album_detail["media_count"] == 2
    assert album_detail["media"] == [
        {"index": 0, "media_type": "photo", "exists": True},
        {"index": 1, "media_type": "video", "exists": False},
    ]
    assert album_detail["can"] == {
        "edit": True, "media": False, "post_now": False, "retry": True, "delete": True,
    }


def test_legacy_rows_do_not_crash():
    legacy = insert_post(
        channel_id=None, caption_entities="{not json", media_type=None, description="old <post>",
        retry_count=None, is_recurring=None, file_path="uploads/photos/gone.jpg",
    )
    [item] = list_posts(1, "queued", now=NOW)["items"]
    assert item["id"] == legacy
    assert (item["channel_id"], item["channel_name"]) == (None, None)
    assert (item["media_type"], item["media_count"]) == ("photo", 1)
    assert (item["caption_text"], item["is_recurring"], item["retry_count"]) == ("old <post>", False, 0)

    detail = get_post_detail(1, legacy, now=NOW)
    assert detail["caption_tagged"] == "old &lt;post&gt;"
    assert detail["caption_html"] == "old &lt;post&gt;"
    assert detail["media"] == [{"index": 0, "media_type": "photo", "exists": False}]
    assert (detail["signature"], detail["signature_fits"]) == (None, None)

    # an album whose bundle is corrupt, with no caption at all
    broken = insert_post(media_type="album", media_bundle_json="not json")
    broken_detail = get_post_detail(1, broken, now=NOW)
    assert (broken_detail["media"], broken_detail["media_count"]) == ([], 0)
    assert [broken_detail[key] for key in ("caption_text", "caption_tagged", "caption_html")] == ["", "", ""]
    assert broken_detail["can"]["media"] is False

    # a posted row without posted_at still lists exactly once, after the dated ones
    dated = insert_post(status="posted", posted_at=serialize_dt(PAST))
    undated = insert_post(status="posted")
    for limit in (1, 2):
        assert _flat(_pages(1, "posted", limit=limit)) == [dated, undated]
    assert get_post_detail(1, undated, now=NOW)["posted_at"] is None

    # a row without a status opens read-only and is in no tab
    no_status = insert_post(status=None)
    no_status_detail = get_post_detail(1, no_status, now=NOW)
    assert (no_status_detail["status"], no_status_detail["can"]) == ("unknown", NO_ACTIONS)
    assert all(no_status not in _ids(list_posts(1, tab, now=NOW)) for tab in TABS)


# --- ownership --------------------------------------------------------------------------


def test_cross_user_isolation():
    add_channel(1, "-1001", "Alpha")
    mine = [
        insert_post(),
        insert_post(scheduled=FUTURE, channel_id="-1001"),
        insert_post(status="sending", scheduled=FUTURE),
        insert_post(status="failed"),
        insert_post(status="posted", posted_at=serialize_dt(PAST)),
        insert_post(status="deleted", deleted_from_status="pending"),
    ]

    missing = _assert_error(lambda: get_owned_post(2, 999999), 404, "not_found")
    for pid in mine:
        for call in (
            lambda: get_owned_post(2, pid),
            lambda: get_owned_post(2, pid, include_deleted=True),
            lambda: get_post_detail(2, pid, now=NOW),
        ):
            error = _assert_error(call, 404, "not_found")
            assert error.message == missing.message  # someone else's post looks like no post

    for tab in TABS:
        assert list_posts(2, tab, now=NOW)["items"] == []
    me = get_me(2, now=NOW)
    assert me["counts"] == {"scheduled": 0, "queued": 0, "failed": 0, "posted": 0}
    assert me["channels"] == []

    # ids SQLite cannot hold, or that never existed, are a plain not_found
    for pid in (2 ** 70, -1, 0):
        _assert_error(lambda: get_owned_post(1, pid), 404, "not_found")

    # search never crosses users
    theirs = insert_post(user_id=2, description="shared words")
    ours = insert_post(user_id=1, description="shared words")
    assert _ids(list_posts(1, "queued", q="shared", now=NOW)) == [ours]
    assert _ids(list_posts(2, "queued", q="shared", now=NOW)) == [theirs]

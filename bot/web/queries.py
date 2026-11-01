"""Read queries for the web board, always scoped to the signed-in user.

Every query filters on the caller's user id in SQL, so another user's post and a
post that does not exist look the same (`not_found`). List items and details
are JSON-ready: times are Kyiv ISO strings. Everything here blocks (SQLite and,
for details, the disk); callers run it through `asyncio.to_thread`.
"""

import base64
import json
from datetime import datetime

from bot.database import Database, parse_dt, serialize_dt
from bot.utils import get_current_kyiv_time
from bot.web.captions import entities_to_tagged, parse_entities_json, render_html, signature_fits
from bot.web.errors import invalid_request, not_found
from bot.web.media import is_album, media_items, resolve_media_path
from bot.web.rules import TABS, board_status, can_flags

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
CAPTION_TEXT_MAX = 200

_COLUMNS = (
    "id", "user_id", "file_path", "media_type", "description", "scheduled_time", "mode",
    "channel_id", "is_recurring", "recurring_interval_hours", "media_bundle_json", "status",
    "created_at", "posted_at", "batch_id", "retry_count", "failure_reason", "caption_entities",
    "deleted_from_status",
)
_SELECT = ", ".join(f"p.{column}" for column in _COLUMNS)

# The channel's name from the post owner's own channels, else the raw channel id.
_CHANNEL_NAME = "COALESCE(NULLIF(c.channel_name, ''), p.channel_id)"
_CHANNEL_JOIN = "LEFT JOIN user_channels c ON c.user_id = p.user_id AND c.channel_id = p.channel_id"

# The rows of each tab (spec 6.1). Deleted rows match none of them.
_TAB_WHERE = {
    "scheduled": "(p.status = 'pending' AND p.scheduled_time IS NOT NULL) OR p.status = 'sending'",
    "queued": "p.status = 'pending' AND p.scheduled_time IS NULL",
    "failed": "p.status = 'failed'",
    "posted": "p.status = 'posted'",
}


def _epoch(column: str) -> str:
    # SQLite applies the stored UTC offset, so this orders correctly across DST
    # changes (string order does not). A missing or unreadable time sorts as 0
    # so that every row has a sort value and the keyset never skips a row.
    return f"COALESCE(CAST(strftime('%s', {column}) AS INTEGER), 0)"


# Sort value and direction of each tab (spec 7.2); p.id breaks ties in the same direction.
_TAB_ORDER = {
    "scheduled": (_epoch("p.scheduled_time"), "ASC"),
    "queued": ("COALESCE(p.sort_key, p.id)", "ASC"),
    "failed": ("p.id", "DESC"),
    "posted": (_epoch("p.posted_at"), "DESC"),
}

_SQLITE_INT_MIN, _SQLITE_INT_MAX = -(2 ** 63), 2 ** 63 - 1
# A cursor this module issues is at most 58 characters.
_CURSOR_MAX_LENGTH = 100


def _is_sqlite_int(value) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool)
        and _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX
    )


# --- cursor ---------------------------------------------------------------------


def _encode_cursor(sort_value: int, post_id: int) -> str:
    raw = json.dumps([sort_value, post_id], separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor) -> tuple[int, int]:
    """The (sort_value, id) a cursor holds; anything else is invalid_request."""
    if not isinstance(cursor, str) or len(cursor) > _CURSOR_MAX_LENGTH:
        raise invalid_request("Invalid cursor")
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw.decode("utf-8"))
    except ValueError:  # also binascii.Error and UnicodeDecodeError
        raise invalid_request("Invalid cursor") from None
    if not (isinstance(value, list) and len(value) == 2 and all(map(_is_sqlite_int, value))):
        raise invalid_request("Invalid cursor")
    return value[0], value[1]


# --- rows -------------------------------------------------------------------------


def _post_from_row(values) -> dict:
    """A posts row as a dict with parsed times."""
    row = dict(zip(_COLUMNS, values))
    row["scheduled_time"] = parse_dt(row["scheduled_time"])
    # Values written by CURRENT_TIMESTAMP are naive UTC (same rule as the dt migration).
    row["created_at"] = parse_dt(row["created_at"], naive_tz="utc")
    row["posted_at"] = parse_dt(row["posted_at"], naive_tz="utc")
    row["is_recurring"] = bool(row["is_recurring"])
    return row


def _list_item(row: dict, channel_name: str | None, now: datetime) -> dict:
    """The list shape of a post (spec 7.1)."""
    scheduled_time = row["scheduled_time"]
    return {
        "id": row["id"],
        "status": board_status(row),
        "channel_id": row["channel_id"],
        "channel_name": channel_name,
        "scheduled_time": serialize_dt(scheduled_time),
        "posted_at": serialize_dt(row["posted_at"]),
        "caption_text": (row["description"] or "")[:CAPTION_TEXT_MAX],
        "media_type": row["media_type"] or "photo",
        "media_count": len(media_items(row)),
        "is_recurring": row["is_recurring"],
        "recurring_interval_hours": row["recurring_interval_hours"],
        "overdue": row["status"] == "pending" and scheduled_time is not None and scheduled_time < now,
        "failure_reason": row["failure_reason"],
        "retry_count": row["retry_count"] or 0,
    }


def _fetch_post(user_id: int, post_id: int, include_deleted: bool) -> tuple[dict, str | None]:
    """The user's post and its channel name; raises not_found."""
    if not _is_sqlite_int(post_id):
        raise not_found("Post not found")
    sql = f"SELECT {_SELECT}, {_CHANNEL_NAME} FROM posts p {_CHANNEL_JOIN} WHERE p.id = ? AND p.user_id = ?"
    if not include_deleted:
        sql += " AND p.status IS NOT 'deleted'"
    conn = Database.get_connection()
    try:
        values = conn.execute(sql, (post_id, user_id)).fetchone()
    finally:
        conn.close()
    if values is None:
        raise not_found("Post not found")
    return _post_from_row(values[:-1]), values[-1]


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _lower(value):
    # SQLite's own lower() and LIKE fold ASCII only; this folds Cyrillic too.
    return value.lower() if value is not None else None


# --- public -----------------------------------------------------------------------


def get_owned_post(user_id: int, post_id: int, include_deleted: bool = False) -> dict:
    """The user's post as a raw row (times parsed to datetimes); raises not_found.

    A deleted post is not_found unless include_deleted is set.
    """
    return _fetch_post(user_id, post_id, include_deleted)[0]


def get_me(user_id: int, now: datetime | None = None) -> dict:
    """The signed-in user's clock, active channels and per-tab post counts."""
    if now is None:
        now = get_current_kyiv_time()
    counts_sql = ", ".join(f"COUNT(CASE WHEN {_TAB_WHERE[tab]} THEN 1 END)" for tab in TABS)
    conn = Database.get_connection()
    try:
        counts = conn.execute(f"SELECT {counts_sql} FROM posts p WHERE p.user_id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    channels = [
        {key: channel[key] for key in ("channel_id", "channel_name", "signature", "is_active")}
        for channel in Database.get_user_channels(user_id)
    ]
    return {
        "user_id": user_id,
        "now": serialize_dt(now),
        "channels": channels,
        "counts": dict(zip(TABS, counts)),
    }


def list_posts(
    user_id: int,
    status: str,
    channel_id: str | None = None,
    q: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    now: datetime | None = None,
) -> dict:
    """One page of a tab as {"items": [...], "next_cursor": str | None}.

    `q` is a case-insensitive substring of the caption with % and _ taken
    literally. An empty channel_id, q or cursor is the same as none. A bad
    status, a limit outside 1..MAX_LIMIT or an unreadable cursor raises
    invalid_request.
    """
    if status not in TABS:
        raise invalid_request(f"status must be one of: {', '.join(TABS)}")
    if not _is_sqlite_int(limit) or not 1 <= limit <= MAX_LIMIT:
        raise invalid_request(f"limit must be an integer from 1 to {MAX_LIMIT}")
    after = None if cursor is None or cursor == "" else _decode_cursor(cursor)
    if now is None:
        now = get_current_kyiv_time()

    sort, direction = _TAB_ORDER[status]
    where = ["p.user_id = ?", f"({_TAB_WHERE[status]})"]
    params: list = [user_id]
    if channel_id:
        where.append("p.channel_id = ?")
        params.append(channel_id)
    if q:
        where.append("u_lower(p.description) LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(q.lower())}%")
    if after is not None:
        where.append(f"({sort}, p.id) {'>' if direction == 'ASC' else '<'} (?, ?)")
        params.extend(after)
    params.append(limit + 1)  # one extra row tells whether another page exists
    sql = (
        f"SELECT {_SELECT}, {_CHANNEL_NAME}, {sort} FROM posts p {_CHANNEL_JOIN}"
        f" WHERE {' AND '.join(where)}"
        f" ORDER BY {sort} {direction}, p.id {direction} LIMIT ?"
    )

    conn = Database.get_connection()
    try:
        conn.create_function("u_lower", 1, _lower, deterministic=True)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    page = rows[:limit]
    items = [_list_item(_post_from_row(values[:-2]), values[-2], now) for values in page]
    next_cursor = _encode_cursor(page[-1][-1], page[-1][0]) if len(rows) > limit else None
    return {"items": items, "next_cursor": next_cursor}


def get_post_detail(user_id: int, post_id: int, now: datetime | None = None) -> dict:
    """The full post (spec 7.1): the list item plus caption, media, signature and allowed actions.

    Deleted posts are not_found. Checks the disk for every media item.
    """
    row, channel_name = _fetch_post(user_id, post_id, include_deleted=False)
    if now is None:
        now = get_current_kyiv_time()
    signature = Database.get_channel_signature(user_id, row["channel_id"]) if row["channel_id"] else None
    entities = parse_entities_json(row["caption_entities"])
    detail = _list_item(row, channel_name, now)
    detail.update(
        caption_tagged=entities_to_tagged(row["description"], entities),
        caption_html=render_html(row["description"], entities),
        media=[
            {
                "index": item["index"],
                "media_type": item["media_type"],
                "exists": resolve_media_path(item["file_path"]) is not None,
            }
            for item in media_items(row)
        ],
        signature=signature,
        signature_fits=signature_fits(row["description"], signature),
        batch_id=row["batch_id"],
        mode=row["mode"],
        created_at=serialize_dt(row["created_at"]),
        can=can_flags(detail["status"], is_album(row)),
    )
    return detail

"""Login tokens, browser sessions and the failed-login rate limiter.

Only SHA-256 hashes of tokens and session ids are ever stored. A login token
is single use and short lived; a session lasts a fixed seven days. Expiry is
always decided by parsing the stored timestamp, never by comparing strings.
"""

import hashlib
import re
import secrets
import time
from collections import deque
from contextlib import contextmanager
from datetime import timedelta

from bot.database import Database, parse_dt, serialize_dt
from bot.utils import get_current_kyiv_time

TOKEN_TTL_SECONDS = 300
SESSION_TTL_SECONDS = 7 * 24 * 3600
FAIL_LIMIT = 20
FAIL_WINDOW_SECONDS = 300

# secrets.token_urlsafe output is 43 characters of [A-Za-z0-9_-]. Anything else
# cannot be one of ours, so it is rejected before it reaches the database.
_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _hash_key(value) -> str | None:
    """SHA-256 hex of a token or session id, or None when it is malformed."""
    if not isinstance(value, str) or not _KEY_PATTERN.fullmatch(value):
        return None
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _resolve_now(now):
    return get_current_kyiv_time() if now is None else parse_dt(now)


def _is_expired(expires_at: str, now) -> bool:
    """True once `now` reaches the expiry; an unreadable expiry counts as expired."""
    expires = parse_dt(expires_at)
    return expires is None or now >= expires


@contextmanager
def _connection():
    """A connection that commits on success, rolls back on error, always closes."""
    conn = Database.get_connection()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _select_token(conn, token_hash: str):
    return conn.execute(
        "SELECT user_id, expires_at, used_at FROM web_login_tokens WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()


def create_login_token(user_id: int, now=None) -> str:
    """Create a single-use login token and return the raw value (shown once)."""
    now = _resolve_now(now)
    token = secrets.token_urlsafe(32)
    with _connection() as conn:
        conn.execute(
            "INSERT INTO web_login_tokens (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (_hash_key(token), user_id, serialize_dt(now + timedelta(seconds=TOKEN_TTL_SECONDS))),
        )
    return token


def consume_login_token(token: str, now=None) -> int | None:
    """Mark a token used and return its user id; None if unknown, used, expired or malformed.

    The expiry is immutable, so it is checked on the parsed datetime first. The
    winner is then decided by one conditional UPDATE (`used_at IS NULL`), so two
    concurrent consumers can never both succeed.
    """
    token_hash = _hash_key(token)
    if token_hash is None:
        return None
    now = _resolve_now(now)
    with _connection() as conn:
        row = _select_token(conn, token_hash)
        if row is None:
            return None
        user_id, expires_at, used_at = row
        if used_at is not None or _is_expired(expires_at, now):
            return None
        claimed = conn.execute(
            "UPDATE web_login_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
            (serialize_dt(now), token_hash),
        )
        return user_id if claimed.rowcount == 1 else None


def create_session(user_id: int, now=None) -> str:
    """Create a browser session and return the raw session id (cookie value)."""
    now = _resolve_now(now)
    session_id = secrets.token_urlsafe(32)
    with _connection() as conn:
        conn.execute(
            "INSERT INTO web_sessions (session_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (
                _hash_key(session_id),
                user_id,
                serialize_dt(now),
                serialize_dt(now + timedelta(seconds=SESSION_TTL_SECONDS)),
            ),
        )
    return session_id


def get_session_user(session_id: str | None, now=None) -> int | None:
    """User id for a live session, None for a missing, malformed or expired one."""
    session_hash = _hash_key(session_id)
    if session_hash is None:
        return None
    now = _resolve_now(now)
    with _connection() as conn:
        row = conn.execute(
            "SELECT user_id, expires_at FROM web_sessions WHERE session_hash = ?",
            (session_hash,),
        ).fetchone()
    if row is None or _is_expired(row[1], now):
        return None
    return row[0]


def delete_session(session_id: str) -> None:
    session_hash = _hash_key(session_id)
    if session_hash is None:
        return
    with _connection() as conn:
        conn.execute("DELETE FROM web_sessions WHERE session_hash = ?", (session_hash,))


def purge_expired(now=None) -> int:
    """Delete expired or used login tokens and expired sessions; return the rows removed."""
    now = _resolve_now(now)
    with _connection() as conn:
        stale_tokens = [
            (token_hash,)
            for token_hash, expires_at, used_at in conn.execute(
                "SELECT token_hash, expires_at, used_at FROM web_login_tokens"
            ).fetchall()
            if used_at is not None or _is_expired(expires_at, now)
        ]
        stale_sessions = [
            (session_hash,)
            for session_hash, expires_at in conn.execute(
                "SELECT session_hash, expires_at FROM web_sessions"
            ).fetchall()
            if _is_expired(expires_at, now)
        ]
        removed = 0
        for sql, keys in (
            ("DELETE FROM web_login_tokens WHERE token_hash = ?", stale_tokens),
            ("DELETE FROM web_sessions WHERE session_hash = ?", stale_sessions),
        ):
            removed += conn.executemany(sql, keys).rowcount
    return removed


class LoginRateLimiter:
    """Global failed-login limiter: `limit` failures within `window_seconds` block sign-in.

    Only the most recent `limit` failure times are kept, so memory stays bounded
    however many attempts arrive. Failures registered while blocked extend the
    block. Not thread safe; call it from the event loop.
    """

    def __init__(self, limit: int = FAIL_LIMIT, window_seconds: float = FAIL_WINDOW_SECONDS):
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self.limit = limit
        self.window_seconds = window_seconds
        self._failures: deque[float] = deque(maxlen=limit)

    def register_failure(self, now: float | None = None) -> None:
        self._failures.append(time.time() if now is None else now)

    def is_blocked(self, now: float | None = None) -> bool:
        if len(self._failures) < self.limit:
            return False
        now = time.time() if now is None else now
        return now - self._failures[0] < self.window_seconds

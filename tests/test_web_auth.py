"""Web board login tokens, sessions and the failed-login rate limiter."""

import hashlib
import re
import threading
from datetime import datetime, timedelta, timezone

import pytest

from bot.database import Database, parse_dt
from bot.utils import get_kyiv_timezone
from bot.web import auth
from bot.web.auth import (
    FAIL_LIMIT,
    FAIL_WINDOW_SECONDS,
    SESSION_TTL_SECONDS,
    TOKEN_TTL_SECONDS,
    LoginRateLimiter,
    consume_login_token,
    create_login_token,
    create_session,
    delete_session,
    get_session_user,
    purge_expired,
)

HEX64 = re.compile(r"[0-9a-f]{64}")


@pytest.fixture
def t0():
    """A fixed, Kyiv-aware 'now' so expiry maths is deterministic."""
    return get_kyiv_timezone().localize(datetime(2026, 3, 1, 12, 0, 0))


def _rows(table):
    conn = Database.get_connection()
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        conn.close()


def test_constants():
    assert TOKEN_TTL_SECONDS == 300
    assert SESSION_TTL_SECONDS == 7 * 24 * 3600
    assert FAIL_LIMIT == 20
    assert FAIL_WINDOW_SECONDS == 300


def test_token_roundtrip_returns_user_id(t0):
    token = create_login_token(42, now=t0)
    assert consume_login_token(token, now=t0) == 42


def test_token_default_now_is_current_time():
    token = create_login_token(7)
    assert consume_login_token(token) == 7


def test_token_single_use(t0):
    token = create_login_token(42, now=t0)
    assert consume_login_token(token, now=t0) == 42
    assert consume_login_token(token, now=t0) is None


def test_token_expiry(t0):
    live = create_login_token(42, now=t0)
    late = create_login_token(42, now=t0)
    assert consume_login_token(live, now=t0 + timedelta(seconds=299)) == 42
    assert consume_login_token(late, now=t0 + timedelta(seconds=301)) is None


def test_token_expires_exactly_at_ttl(t0):
    token = create_login_token(42, now=t0)
    assert consume_login_token(token, now=t0 + timedelta(seconds=TOKEN_TTL_SECONDS)) is None


def test_expired_token_is_not_marked_used(t0):
    token = create_login_token(42, now=t0)
    assert consume_login_token(token, now=t0 + timedelta(seconds=400)) is None
    used = [row[3] for row in _rows("web_login_tokens")]
    assert used == [None]


def test_unknown_token_returns_none(t0):
    create_login_token(42, now=t0)
    assert consume_login_token("a" * 43, now=t0) is None


def test_second_consume_loses_the_race(t0, monkeypatch):
    """The conditional UPDATE is the gate: a consumer that read the row as
    unused but lost the write must get None, not the user id."""
    token = create_login_token(42, now=t0)
    real_select = auth._select_token

    def select_then_lose_race(conn, token_hash):
        row = real_select(conn, token_hash)
        # A concurrent consumer marks the token used after our read.
        other = Database.get_connection()
        try:
            other.execute(
                "UPDATE web_login_tokens SET used_at = ? WHERE token_hash = ?",
                ("2026-03-01T12:00:00+02:00", token_hash),
            )
            other.commit()
        finally:
            other.close()
        return row

    monkeypatch.setattr(auth, "_select_token", select_then_lose_race)
    assert consume_login_token(token, now=t0) is None


def test_concurrent_consumes_have_exactly_one_winner(t0):
    token = create_login_token(42, now=t0)
    barrier = threading.Barrier(8)
    results = []

    def worker():
        barrier.wait()
        results.append(consume_login_token(token, now=t0))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results, key=lambda value: value is None) == [42] + [None] * 7


def test_only_hashes_stored(t0):
    token = create_login_token(42, now=t0)
    session_id = create_session(42, now=t0)

    for table in ("web_login_tokens", "web_sessions"):
        rows = _rows(table)
        assert len(rows) == 1
        for row in rows:
            for value in row:
                assert token not in str(value)
                assert session_id not in str(value)
            assert HEX64.fullmatch(row[0])

    assert _rows("web_login_tokens")[0][0] == hashlib.sha256(token.encode()).hexdigest()
    assert _rows("web_sessions")[0][0] == hashlib.sha256(session_id.encode()).hexdigest()


def test_tokens_and_session_ids_are_distinct_and_long(t0):
    tokens = {create_login_token(1, now=t0) for _ in range(20)}
    sessions = {create_session(1, now=t0) for _ in range(20)}
    assert len(tokens) == 20 and len(sessions) == 20
    assert all(len(value) >= 43 for value in tokens | sessions)


@pytest.mark.parametrize(
    "garbage",
    ["", "x" * 10000, "../etc", "a b", "tok\x00en", "\ud800", "é" * 43, " " * 43, None, 12345, b"bytes"],
)
def test_garbage_tokens(garbage, t0):
    create_login_token(42, now=t0)
    assert consume_login_token(garbage, now=t0) is None


def test_session_lifecycle(t0):
    session_id = create_session(42, now=t0)
    assert get_session_user(session_id, now=t0) == 42
    assert get_session_user(session_id, now=t0 + timedelta(days=7) - timedelta(seconds=1)) == 42
    assert get_session_user(session_id, now=t0 + timedelta(days=7, seconds=1)) is None

    other = create_session(43, now=t0)
    delete_session(session_id)
    assert get_session_user(session_id, now=t0) is None
    assert get_session_user(other, now=t0) == 43

    assert get_session_user(None) is None


def test_session_expires_exactly_at_ttl(t0):
    session_id = create_session(42, now=t0)
    assert get_session_user(session_id, now=t0 + timedelta(seconds=SESSION_TTL_SECONDS)) is None


def test_session_default_now_is_current_time():
    session_id = create_session(9)
    assert get_session_user(session_id) == 9


def test_session_lifetime_is_absolute_across_dst_change():
    """Kyiv leaves DST on 2026-10-25; seven real days must stay seven real days."""
    start = datetime(2026, 10, 20, 9, 0, 0, tzinfo=timezone.utc)
    session_id = create_session(42, now=start)
    end = start + timedelta(days=7)
    assert get_session_user(session_id, now=end - timedelta(seconds=1)) == 42
    assert get_session_user(session_id, now=end) is None


@pytest.mark.parametrize("garbage", ["", "x" * 10000, "../etc", "\ud800", 12345, b"bytes"])
def test_garbage_session_ids(garbage, t0):
    create_session(42, now=t0)
    assert get_session_user(garbage, now=t0) is None
    delete_session(garbage)
    assert len(_rows("web_sessions")) == 1


def test_delete_unknown_session_is_noop():
    delete_session("a" * 43)


def test_sessions_with_unparseable_expiry_are_rejected(t0):
    session_id = create_session(42, now=t0)
    conn = Database.get_connection()
    try:
        conn.execute("UPDATE web_sessions SET expires_at = 'not-a-date'")
        conn.commit()
    finally:
        conn.close()
    assert get_session_user(session_id, now=t0) is None


def test_purge_expired(t0):
    used = create_login_token(1, now=t0)
    expired = create_login_token(2, now=t0 - timedelta(seconds=1000))
    live = create_login_token(3, now=t0)
    assert consume_login_token(used, now=t0) == 1

    old_session = create_session(1, now=t0 - timedelta(days=8))
    live_session = create_session(2, now=t0)

    removed = purge_expired(now=t0)
    assert removed == 3

    assert len(_rows("web_login_tokens")) == 1  # only the live, unused token is left
    assert len(_rows("web_sessions")) == 1
    assert purge_expired(now=t0) == 0

    assert consume_login_token(live, now=t0) == 3
    assert consume_login_token(expired, now=t0) is None
    assert get_session_user(old_session, now=t0) is None
    assert get_session_user(live_session, now=t0) == 2


def test_purge_removes_rows_with_unparseable_expiry(t0):
    create_login_token(1, now=t0)
    create_session(1, now=t0)
    conn = Database.get_connection()
    try:
        conn.execute("UPDATE web_login_tokens SET expires_at = 'garbage'")
        conn.execute("UPDATE web_sessions SET expires_at = 'garbage'")
        conn.commit()
    finally:
        conn.close()
    assert purge_expired(now=t0) == 2
    assert _rows("web_login_tokens") == []
    assert _rows("web_sessions") == []


def test_purge_default_now(t0):
    create_login_token(1)
    create_session(1)
    assert purge_expired() == 0


def test_rate_limiter():
    limiter = LoginRateLimiter()
    start = 1_000_000.0
    assert not limiter.is_blocked(now=start)
    for i in range(19):
        limiter.register_failure(now=start + i)
    assert not limiter.is_blocked(now=start + 19)
    limiter.register_failure(now=start + 19)
    assert limiter.is_blocked(now=start + 19)
    assert limiter.is_blocked(now=start + FAIL_WINDOW_SECONDS - 1)
    # The oldest failure (start) leaves the window at start + 300 and the block lifts.
    assert not limiter.is_blocked(now=start + FAIL_WINDOW_SECONDS)
    assert not limiter.is_blocked(now=start + FAIL_WINDOW_SECONDS + 100)


def test_rate_limiter_block_clears_only_after_whole_window():
    limiter = LoginRateLimiter(limit=3, window_seconds=10)
    for _ in range(3):
        limiter.register_failure(now=100.0)
    assert limiter.is_blocked(now=109.9)
    assert not limiter.is_blocked(now=110.0)


def test_rate_limiter_old_failures_do_not_accumulate():
    limiter = LoginRateLimiter(limit=3, window_seconds=10)
    limiter.register_failure(now=0.0)
    limiter.register_failure(now=5.0)
    limiter.register_failure(now=11.0)  # the failure at 0.0 has aged out
    assert not limiter.is_blocked(now=11.0)
    limiter.register_failure(now=12.0)
    assert limiter.is_blocked(now=12.0)


def test_rate_limiter_failures_while_blocked_extend_the_block_without_growing_memory():
    limiter = LoginRateLimiter(limit=3, window_seconds=10)
    for i in range(1000):
        limiter.register_failure(now=100.0 + i * 0.01)
    assert len(limiter._failures) == 3
    assert limiter.is_blocked(now=109.9)
    assert limiter.is_blocked(now=112.0)  # last failures at ~109.99 keep it blocked
    assert not limiter.is_blocked(now=120.0)


def test_rate_limiter_defaults_use_wall_clock():
    limiter = LoginRateLimiter(limit=2, window_seconds=60)
    assert not limiter.is_blocked()
    limiter.register_failure()
    limiter.register_failure()
    assert limiter.is_blocked()


def test_stored_expiry_is_canonical_kyiv_iso(t0):
    create_login_token(42, now=t0)
    expires_at = _rows("web_login_tokens")[0][2]
    parsed = parse_dt(expires_at)
    assert parsed == t0 + timedelta(seconds=TOKEN_TTL_SECONDS)
    assert expires_at.endswith(("+02:00", "+03:00"))

"""Canonical datetime handling: serialize/parse helpers and the legacy migration."""

from datetime import datetime

import pytz

from bot.database import Database, init_database, serialize_dt, parse_dt, kyiv_now_iso

KYIV = pytz.timezone('Europe/Kiev')


def test_serialize_roundtrip():
    aware = KYIV.localize(datetime(2026, 9, 1, 12, 0))
    s = serialize_dt(aware)
    assert s == '2026-09-01T12:00:00+03:00'
    assert parse_dt(s) == aware


def test_serialize_naive_assumed_kyiv():
    assert serialize_dt(datetime(2026, 9, 1, 12, 0)) == '2026-09-01T12:00:00+03:00'


def test_serialize_accepts_strings_and_none():
    assert serialize_dt(None) is None
    assert serialize_dt('2026-09-01 12:00:00') == '2026-09-01T12:00:00+03:00'
    assert serialize_dt('not a date') is None


def test_parse_naive_utc_vs_kyiv():
    # Winter: Kyiv is UTC+2
    assert parse_dt('2026-01-15 10:00:00', naive_tz='utc').hour == 12
    assert parse_dt('2026-01-15 10:00:00').hour == 10
    # Summer: Kyiv is UTC+3
    assert parse_dt('2026-07-15 10:00:00', naive_tz='utc').hour == 13


def test_parse_garbage_returns_none():
    assert parse_dt('garbage') is None
    assert parse_dt(None) is None


def test_kyiv_now_iso_is_aware_kyiv():
    parsed = parse_dt(kyiv_now_iso())
    assert parsed is not None and parsed.tzinfo is not None


def test_legacy_migration_rewrites_mixed_formats():
    conn = Database.get_connection()
    c = conn.cursor()
    # Legacy naive Kyiv wall time (old sqlite adapter) + naive UTC (CURRENT_TIMESTAMP)
    c.execute(
        "INSERT INTO posts (user_id, file_path, mode, status, scheduled_time, posted_at)"
        " VALUES (1, 'legacy.jpg', 1, 'posted', '2025-07-12 10:00:00', '2025-07-12 07:00:00')"
    )
    c.execute("DELETE FROM schema_meta WHERE key='dt_normalized_v1'")
    conn.commit()
    conn.close()

    init_database()  # runs the migration again

    conn = Database.get_connection()
    c = conn.cursor()
    c.execute("SELECT scheduled_time, posted_at FROM posts WHERE file_path='legacy.jpg'")
    scheduled, posted = c.fetchone()
    conn.close()

    assert scheduled == '2025-07-12T10:00:00+03:00'  # naive Kyiv kept as wall time
    assert posted == '2025-07-12T10:00:00+03:00'     # naive UTC shifted to Kyiv


def test_migration_is_idempotent():
    init_database()
    init_database()

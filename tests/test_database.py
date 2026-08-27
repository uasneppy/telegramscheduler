"""Database layer: user scoping, send-claim machinery, cleanup, settings."""

from datetime import datetime, timedelta

import pytz

from bot.database import Database, serialize_dt

KYIV = pytz.timezone('Europe/Kiev')
AWARE = KYIV.localize(datetime(2030, 9, 1, 12, 0))


def _insert_post(user_id=1, status='pending', scheduled=None, **cols):
    conn = Database.get_connection()
    c = conn.cursor()
    fields = {'user_id': user_id, 'file_path': 'f.jpg', 'mode': 1, 'status': status,
              'scheduled_time': serialize_dt(scheduled) if scheduled else None, **cols}
    keys = ', '.join(fields)
    marks = ', '.join('?' * len(fields))
    c.execute(f"INSERT INTO posts ({keys}) VALUES ({marks})", list(fields.values()))
    post_id = c.lastrowid
    conn.commit()
    conn.close()
    return post_id


def _insert_batch(user_id=1, name='b1', channel='ch'):
    conn = Database.get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO post_batches (user_id, batch_name, channel_id) VALUES (?, ?, ?)",
              (user_id, name, channel))
    batch_id = c.lastrowid
    conn.commit()
    conn.close()
    return batch_id


# --- user scoping -----------------------------------------------------------

def test_retry_failed_post_scoped_and_race_free():
    pid = _insert_post(status='failed')
    assert Database.retry_failed_post(pid, 999) is False   # wrong user
    assert Database.retry_failed_post(pid, 1) is True      # owner
    assert Database.retry_failed_post(pid, 1) is False     # already pending


def test_delete_batch_requires_ownership():
    bid = _insert_batch()
    assert Database.delete_batch(bid, 999) is False
    assert Database.delete_batch(bid, 1) is True


def test_schedule_batch_requires_ownership():
    bid = _insert_batch()
    assert Database.schedule_batch(bid, [AWARE], user_id=999) is False
    assert Database.schedule_batch(bid, [AWARE], user_id=1) is True


def test_update_post_functions_scoped():
    pid = _insert_post()
    assert Database.update_post_schedule(pid, AWARE, user_id=999) is False
    assert Database.update_post_schedule(pid, AWARE, user_id=1) is True
    assert Database.update_post_description(pid, 'd', user_id=999) is False
    assert Database.update_post_description(pid, 'd', user_id=1) is True
    assert Database.update_post_media(pid, 'g.jpg', 'photo', user_id=999) is False
    assert Database.update_post_media(pid, 'g.jpg', 'photo', user_id=1) is True
    # Trusted internal call without user_id still works (scheduler path)
    assert Database.update_post_schedule(pid, AWARE) is True


def test_get_batch_posts_scoped():
    bid = _insert_batch()
    _insert_post(batch_id=bid)
    assert len(Database.get_batch_posts(bid, user_id=1)) == 1
    assert len(Database.get_batch_posts(bid, user_id=999)) == 0


# --- send-claim machinery ---------------------------------------------------

def test_claim_prevents_double_send():
    pid = _insert_post(scheduled=AWARE)
    assert Database.claim_post_for_sending(pid) is True
    assert Database.claim_post_for_sending(pid) is False


def test_stale_sending_posts_are_rescued():
    pid = _insert_post(scheduled=AWARE)
    Database.claim_post_for_sending(pid)
    assert Database.reset_stale_sending_posts(max_age_minutes=0) == 1
    assert Database.claim_post_for_sending(pid) is True


def test_reschedule_recurring_returns_to_pending():
    pid = _insert_post(scheduled=AWARE)
    Database.claim_post_for_sending(pid)
    Database.reschedule_recurring_post(pid, AWARE + timedelta(hours=1))
    assert Database.claim_post_for_sending(pid) is True


# --- cleanup ----------------------------------------------------------------

def test_clear_queued_preserves_scheduled():
    _insert_post(user_id=7)                       # queued
    _insert_post(user_id=7, scheduled=AWARE)      # scheduled
    assert Database.clear_queued_posts(7) == 1
    assert Database.get_dashboard_stats(7)['scheduled'] == 1


def test_clear_scheduled_preserves_queue():
    _insert_post(user_id=7)
    _insert_post(user_id=7, scheduled=AWARE)
    assert Database.clear_scheduled_posts(7) == 1
    assert Database.get_dashboard_stats(7)['queued'] == 1


def test_purge_deleted_posts():
    pid = _insert_post(status='deleted')
    assert Database.purge_deleted_posts() == 1
    assert Database.get_post_by_id(pid) is None


# --- channels ---------------------------------------------------------------

def test_channel_upsert_keeps_default_and_id():
    Database.add_user_channel(1, '@c', 'Name', is_default=True)
    row_id = Database.get_user_channels(1)[0]['id']
    Database.add_user_channel(1, '@c', 'Renamed')
    chs = Database.get_user_channels(1)
    assert len(chs) == 1
    assert chs[0]['channel_name'] == 'Renamed'
    assert chs[0]['is_default'] is True
    assert chs[0]['id'] == row_id


def test_channel_signature_roundtrip():
    Database.add_user_channel(1, '@c', 'Name')
    assert Database.get_channel_signature(1, '@c') is None
    assert Database.set_channel_signature(1, '@c', 'follow @c') is True
    assert Database.get_channel_signature(1, '@c') == 'follow @c'
    Database.set_channel_signature(1, '@c', None)
    assert Database.get_channel_signature(1, '@c') is None
    # Other users can't see/set it
    assert Database.get_channel_signature(999, '@c') is None
    assert Database.set_channel_signature(999, '@c', 'x') is False


# --- shuffle ----------------------------------------------------------------

def test_shuffle_covers_queue_and_keeps_content():
    ids = [_insert_post(user_id=5) for _ in range(20)]
    count = Database.shuffle_queued_posts(5)
    assert count == 20
    shuffled = [p['id'] for p in Database.get_pending_posts(5, unscheduled_only=True)]
    assert sorted(shuffled) == sorted(ids)


# --- settings / notifications ----------------------------------------------

def test_notify_mode_defaults_and_cycle():
    assert Database.get_notify_mode(1) == 'all'
    Database.set_notify_mode(1, 'digest')
    assert Database.get_notify_mode(1) == 'digest'
    Database.set_notify_mode(1, 'errors')
    assert Database.get_notify_mode(1) == 'errors'


def test_digest_data_counts():
    Database.set_notify_mode(9, 'digest')
    conn = Database.get_connection()
    c = conn.cursor()
    now = datetime.now(KYIV).isoformat()
    c.execute("INSERT INTO posts (user_id, file_path, mode, status, posted_at) VALUES (9,'a',1,'posted',?)", (now,))
    c.execute("INSERT INTO posts (user_id, file_path, mode, status) VALUES (9,'b',1,'failed')")
    conn.commit()
    conn.close()
    data = Database.get_digest_data()
    assert data[9] == {'posted': 1, 'failed': 1}


def test_reminder_settings_roundtrip():
    enabled, threshold, last = Database.get_reminder_settings(1)
    assert enabled and threshold == 5 and last is None
    Database.update_reminder_settings(1, enabled=False, threshold=3)
    enabled, threshold, _ = Database.get_reminder_settings(1)
    assert not enabled and threshold == 3


def test_dashboard_stats():
    Database.add_user_channel(3, '@c', 'C')
    _insert_post(user_id=3)
    _insert_post(user_id=3, scheduled=AWARE, channel_id='@c')
    _insert_post(user_id=3, status='failed')
    _insert_post(user_id=3, scheduled=KYIV.localize(datetime(2020, 1, 1)))
    s = Database.get_dashboard_stats(3)
    assert (s['channels'], s['queued'], s['scheduled'], s['failed'], s['overdue']) == (1, 1, 2, 1, 1)
    assert s['next_post']['channel_name'] == 'C'

"""Pure UI builders: menus, settings, retry, schedule options, pickers."""

from datetime import datetime

import pytz

from bot import handlers
from bot.database import Database, serialize_dt

KYIV = pytz.timezone('Europe/Kiev')


def _kb_str(markup):
    return str(markup.to_dict())


def test_main_menu_onboarding_without_channels(user):
    msg, kb = handlers.build_main_menu(user)
    assert 'first channel' in msg
    assert 'channels_add' in _kb_str(kb)


def test_main_menu_dashboard_with_data(user):
    Database.add_user_channel(user.id, '@c', 'Chan')
    conn = Database.get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO posts (user_id, file_path, mode, status) VALUES (?, 'q', 1, 'pending')", (user.id,))
    c.execute("INSERT INTO posts (user_id, file_path, mode, status) VALUES (?, 'f', 1, 'failed')", (user.id,))
    c.execute("INSERT INTO posts (user_id, file_path, mode, status, channel_id, scheduled_time) VALUES (?, 's', 1, 'pending', '@c', ?)",
              (user.id, serialize_dt(KYIV.localize(datetime(2030, 1, 1, 12, 0)))))
    conn.commit()
    conn.close()

    msg, kb = handlers.build_main_menu(user)
    kb_str = _kb_str(kb)
    assert 'queued' in msg and 'Next post' in msg
    assert '1 failed' in kb_str and 'main_retry' in kb_str
    assert 'main_settings' in kb_str and 'main_multibatch' in kb_str
    # Markdown-hostile first name must be escaped
    assert 'Test\\_' in msg


def test_retry_menu_states(user):
    msg, kb = handlers.build_retry_menu(user)
    assert 'No Failed Posts' in msg
    conn = Database.get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO posts (user_id, file_path, mode, status) VALUES (?, 'f', 1, 'failed')", (user.id,))
    conn.commit()
    conn.close()
    msg, kb = handlers.build_retry_menu(user)
    assert 'retry_post_' in _kb_str(kb)


def test_settings_view_shows_notify_mode(user):
    Database.set_notify_mode(user.id, 'digest')
    msg, kb = handlers.build_settings_view(user)
    assert 'Daily digest' in _kb_str(kb)
    assert 'settings_notify_cycle' in _kb_str(kb)


def test_schedule_options_empty_and_full(user):
    msg, kb = handlers.build_schedule_options(user)
    assert 'No posts to schedule' in msg

    Database.add_user_channel(user.id, '@c', 'Chan')
    conn = Database.get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO posts (user_id, file_path, mode, status, channel_id) VALUES (?, 'q', 1, 'pending', '@c')", (user.id,))
    conn.commit()
    conn.close()
    msg, kb = handlers.build_schedule_options(user)
    kb_str = _kb_str(kb)
    assert 'schedule_current' in kb_str
    assert 'queue_shuffle' in kb_str


def test_hour_grid():
    grid = handlers._hour_grid(list(range(0, 24)), 'cfg_s_')
    assert len(grid) == 4 and len(grid[0]) == 6
    assert grid[0][0].callback_data == 'cfg_s_0'
    assert grid[3][5].callback_data == 'cfg_s_23'

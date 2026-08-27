"""Schedule calculation, DST handling, formatting, and input parsing."""

from datetime import datetime

import pytz

from bot.utils import (
    calculate_schedule_times, calculate_evenly_distributed_schedule,
    format_schedule_summary, escape_markdown, parse_schedule_input,
)

KYIV = pytz.timezone('Europe/Kiev')


def test_schedule_times_respect_window_and_count():
    times = calculate_schedule_times(10, 20, 2, 8)
    assert len(times) == 8
    assert all(10 <= t.hour < 20 for t in times)
    assert all(t.tzinfo is not None for t in times)
    assert times == sorted(times)


def test_schedule_times_cross_dst_keep_wall_clock():
    # Ukraine leaves DST on the last Sunday of October 2026 (Oct 25)
    start = KYIV.localize(datetime(2026, 10, 24))
    times = calculate_schedule_times(10, 20, 2, 12, start_date=start)
    assert all(10 <= t.hour < 20 for t in times)
    offsets = {t.strftime('%z') for t in times}
    assert offsets == {'+0300', '+0200'}  # correct offset on each side


def test_evenly_distributed_respects_window():
    times = calculate_evenly_distributed_schedule(9, 18, 7)
    assert len(times) == 7
    assert all(t.tzinfo is not None for t in times)
    assert all(9 <= t.hour <= 18 for t in times)


def test_format_summary_day_grid():
    times = calculate_schedule_times(10, 20, 2, 12)
    s = format_schedule_summary(times)
    assert '12 posts' in s
    assert '·' in s  # inline per-day times
    assert format_schedule_summary([]) == 'No posts scheduled.'


def test_format_summary_caps_days():
    times = calculate_schedule_times(10, 12, 2, 30)  # 1 post/day -> 30 days
    s = format_schedule_summary(times, max_days=7)
    assert 'more posts' in s


def test_escape_markdown_legacy_charset():
    # Escapes the legacy-Markdown specials...
    assert escape_markdown('a_b*c`d[e') == 'a\\_b\\*c\\`d\\[e'
    # ...but NOT MarkdownV2-only chars (would render visible backslashes)
    assert escape_markdown('a.b-c!d') == 'a.b-c!d'
    assert escape_markdown('') == ''


def test_parse_schedule_input():
    valid, start, end, interval, _msg = parse_schedule_input('10 20 2')
    assert valid and (start, end, interval) == (10, 20, 2)
    valid, *_ = parse_schedule_input('20 10 2')  # start after end
    assert not valid
    valid, *_ = parse_schedule_input('banana')
    assert not valid

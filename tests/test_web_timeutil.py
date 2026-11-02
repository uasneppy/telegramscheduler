"""Tests for the web board's Kyiv time rules."""

from datetime import datetime, timedelta

import pytest

from bot.utils import get_kyiv_timezone
from bot.web import timeutil
from bot.web.errors import WebError
from bot.web.timeutil import (
    MIN_LEAD_SECONDS,
    ensure_min_lead,
    parse_local_time,
    shift_local,
)

TZ = get_kyiv_timezone()
NOW = TZ.localize(datetime(2030, 1, 1, 12, 0))


def kyiv(*args) -> datetime:
    return TZ.localize(datetime(*args))


def assert_invalid(func, *args):
    with pytest.raises(WebError) as excinfo:
        func(*args)
    assert excinfo.value.status == 400
    assert excinfo.value.code == "invalid_request"
    assert excinfo.value.message


def test_min_lead_constant():
    assert MIN_LEAD_SECONDS == 60


@pytest.mark.parametrize(
    "value", ["2030-01-01 10:00", "2030-13-01T10:00", "", "tomorrow"]
)
def test_format_errors(value):
    assert_invalid(parse_local_time, value, NOW)


def test_past_and_min_lead():
    assert_invalid(parse_local_time, "2030-01-01T11:00", NOW)  # in the past
    assert_invalid(parse_local_time, "2030-01-01T12:00", NOW)  # now

    # Inputs have minute precision, so the 59 s / 61 s edges need a `now`
    # that is not on a minute boundary.
    assert_invalid(
        parse_local_time, "2030-01-01T12:01", kyiv(2030, 1, 1, 12, 0, 1)
    )  # 59 s ahead
    assert parse_local_time(
        "2030-01-01T12:01", kyiv(2030, 1, 1, 11, 59, 59)
    ) == kyiv(2030, 1, 1, 12, 1)  # 61 s ahead


def test_ensure_min_lead_boundary():
    ensure_min_lead(NOW + timedelta(seconds=61), NOW)
    ensure_min_lead(NOW + timedelta(seconds=60), NOW)
    assert_invalid(ensure_min_lead, NOW + timedelta(seconds=59), NOW)
    assert_invalid(ensure_min_lead, NOW - timedelta(hours=1), NOW)


def test_now_defaults_to_current_kyiv_time(monkeypatch):
    monkeypatch.setattr(timeutil, "get_current_kyiv_time", lambda: NOW)
    assert parse_local_time("2030-01-01T13:00") == kyiv(2030, 1, 1, 13, 0)
    assert_invalid(parse_local_time, "2030-01-01T11:00")
    ensure_min_lead(NOW + timedelta(seconds=61))
    assert_invalid(ensure_min_lead, NOW + timedelta(seconds=59))


def test_dst_gap_rejected():
    assert_invalid(parse_local_time, "2030-03-31T03:30", NOW)


def test_dst_overlap_rejected():
    assert_invalid(parse_local_time, "2030-10-27T03:30", NOW)


def test_offsets():
    winter = parse_local_time("2030-01-15T10:00", NOW)
    summer = parse_local_time("2030-07-15T10:00", NOW)
    assert winter.utcoffset() == timedelta(hours=2)
    assert summer.utcoffset() == timedelta(hours=3)
    assert winter.tzinfo.zone == "Europe/Kiev"
    assert winter.strftime("%Y-%m-%dT%H:%M") == "2030-01-15T10:00"


def test_shift_keeps_wall_clock_across_dst():
    start = kyiv(2030, 3, 30, 10, 0)
    assert start.utcoffset() == timedelta(hours=2)
    for unit, amount in (("days", 1), ("hours", 24)):
        shifted = shift_local(start, unit, amount)
        assert shifted.replace(tzinfo=None) == datetime(2030, 3, 31, 10, 0)
        assert shifted.utcoffset() == timedelta(hours=3)


def test_shift_negative():
    start = kyiv(2030, 3, 31, 10, 0)
    shifted = shift_local(start, "days", -1)
    assert shifted.replace(tzinfo=None) == datetime(2030, 3, 30, 10, 0)
    assert shifted.utcoffset() == timedelta(hours=2)


def test_shift_into_gap_rejected():
    assert_invalid(shift_local, kyiv(2030, 3, 30, 3, 30), "days", 1)


def test_shift_into_overlap_rejected():
    assert_invalid(shift_local, kyiv(2030, 10, 26, 3, 30), "days", 1)


def test_shift_bounds():
    start = kyiv(2030, 1, 1, 12, 0)
    assert_invalid(shift_local, start, "hours", 0)
    assert_invalid(shift_local, start, "days", 0)
    assert_invalid(shift_local, start, "hours", 8761)
    assert_invalid(shift_local, start, "hours", -8761)
    assert_invalid(shift_local, start, "days", 366)
    assert_invalid(shift_local, start, "days", -366)
    assert_invalid(shift_local, start, "weeks", 1)

    # The limits themselves are allowed.
    assert shift_local(start, "hours", 8760).replace(tzinfo=None) == datetime(
        2030, 1, 1, 12, 0
    ) + timedelta(hours=8760)
    assert shift_local(start, "days", 365).replace(tzinfo=None) == datetime(
        2031, 1, 1, 12, 0
    )


@pytest.mark.parametrize("amount", [1.5, "3", None, True])
def test_shift_rejects_non_integer_amount(amount):
    assert_invalid(shift_local, kyiv(2030, 1, 1, 12, 0), "hours", amount)

"""Kyiv wall-clock rules for the web board: parsing, minimum lead and shifting."""

import re
from datetime import datetime, timedelta

import pytz

from bot.utils import get_current_kyiv_time, get_kyiv_timezone
from bot.web.errors import invalid_request

MIN_LEAD_SECONDS = 60

# Largest accepted shift magnitude per unit (one year).
_MAX_SHIFT = {"hours": 8760, "days": 365}

_LOCAL_TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}")


def _localize(naive: datetime) -> datetime:
    """Attach the Kyiv zone to a naive wall-clock time, rejecting DST edge cases."""
    try:
        return get_kyiv_timezone().localize(naive, is_dst=None)
    except pytz.NonExistentTimeError:
        raise invalid_request(
            f"{naive:%Y-%m-%d %H:%M} does not exist in Kyiv time "
            "(clocks skip forward); choose another time"
        ) from None
    except pytz.AmbiguousTimeError:
        raise invalid_request(
            f"{naive:%Y-%m-%d %H:%M} happens twice in Kyiv time "
            "(clocks go back); choose another time"
        ) from None


def ensure_min_lead(dt: datetime, now: datetime | None = None) -> None:
    """Raise unless `dt` is at least MIN_LEAD_SECONDS after `now` (default: Kyiv now)."""
    if now is None:
        now = get_current_kyiv_time()
    if (dt - now).total_seconds() < MIN_LEAD_SECONDS:
        raise invalid_request(
            f"Scheduled time must be at least {MIN_LEAD_SECONDS} seconds in the future"
        )


def parse_local_time(value: str, now: datetime | None = None) -> datetime:
    """Parse `YYYY-MM-DDTHH:MM` as Kyiv wall-clock time and enforce the minimum lead."""
    if not isinstance(value, str) or not _LOCAL_TIME_RE.fullmatch(value):
        raise invalid_request("Time must be in the format YYYY-MM-DDTHH:MM")
    try:
        naive = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise invalid_request("Time is not a valid date and time") from None
    dt = _localize(naive)
    ensure_min_lead(dt, now)
    return dt


def shift_local(dt: datetime, unit: str, amount: int) -> datetime:
    """Move a Kyiv-aware `dt` by `amount` hours or days on the wall clock.

    The offset is recomputed after the shift, so 10:00 stays 10:00 across a DST
    change. A result that lands in a DST gap or overlap is rejected.
    """
    if unit not in _MAX_SHIFT:
        raise invalid_request("Unit must be 'hours' or 'days'")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise invalid_request("Amount must be a whole number")
    if amount == 0:
        raise invalid_request("Amount must not be zero")
    if abs(amount) > _MAX_SHIFT[unit]:
        raise invalid_request(f"Amount must be at most {_MAX_SHIFT[unit]} {unit}")
    naive = dt.replace(tzinfo=None) + timedelta(**{unit: amount})
    return _localize(naive)

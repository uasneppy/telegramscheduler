"""Daily cleanup of web board data, called from the scheduler's `_daily_cleanup`."""

from bot.web import auth, media


def purge_web_data(now=None) -> dict:
    """Delete expired login tokens and sessions and stale cached thumbnails.

    Blocking (database and disk): callers run it through `asyncio.to_thread`.
    `now` (a datetime or stored timestamp) only moves the clock for the auth rows.
    """
    return {
        "auth_rows": auth.purge_expired(now),
        "thumbnails": media.purge_old_thumbnails(),
    }

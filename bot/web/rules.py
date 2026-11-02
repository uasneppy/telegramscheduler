"""Board status of a post and what each status allows (spec 6.1 and 6.2). Pure module."""

# The only listable tabs; "sending" posts show under "scheduled", deleted rows never show.
TABS = ("scheduled", "queued", "failed", "posted")

_ACTIONS = ("edit", "media", "post_now", "retry", "delete")

# Spec 6.2. "media" is further switched off for albums. Every other status
# (sending, deleted, unknown) allows nothing.
_ALLOWED = {
    "queued": {"edit", "media", "post_now", "delete"},
    "scheduled": {"edit", "media", "post_now", "delete"},
    "failed": {"edit", "media", "retry", "delete"},
    "posted": {"delete"},
}


def board_status(row: dict) -> str:
    """The board label of a post row: queued, scheduled, sending, failed, posted, deleted or unknown."""
    status = row.get("status")
    if status == "pending":
        return "queued" if row.get("scheduled_time") is None else "scheduled"
    if status in ("sending", "failed", "posted", "deleted"):
        return status
    return "unknown"


def can_flags(status: str, is_album: bool) -> dict:
    """The actions a board status allows, as {edit, media, post_now, retry, delete} booleans."""
    allowed = _ALLOWED.get(status, set())
    flags = {action: action in allowed for action in _ACTIONS}
    if is_album:
        flags["media"] = False
    return flags

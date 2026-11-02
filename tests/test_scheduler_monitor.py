"""The 5-minute post monitor: it re-arms overdue posts without undoing a delete or unschedule.

The web board writes from worker threads, so its delete or unschedule can
commit at any moment of a monitor pass, including while the monitor awaits a
Telegram message for an earlier post. Whatever the board stored must survive.
"""

import asyncio
import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest

from bot.database import Database
from bot.scheduler import PostScheduler
from bot.utils import get_current_kyiv_time
from bot.web import service
from web_helpers import insert_post

MONITOR_ERRORS = ("Error during post monitoring", "Error fetching overdue posts", "Error rescheduling post")


class FakeJobs:
    """The APScheduler calls the monitor makes, recorded instead of run."""

    running = True

    def __init__(self):
        self.jobs = {}

    def get_jobs(self):
        return list(self.jobs.values())

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def add_job(self, func, trigger=None, args=None, id=None, **kwargs):
        self.jobs[id] = SimpleNamespace(id=id, run_date=trigger.run_date, args=args)

    def remove_job(self, job_id):
        del self.jobs[job_id]


class FakeBot:
    """Records messages; `on_send` runs while a message is "in flight" (the monitor's await)."""

    def __init__(self, on_send=None):
        self.sent = []
        self.on_send = on_send

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        if self.on_send:
            self.on_send(text)


def _monitor(bot=None):
    # Skipping __init__ avoids building APScheduler and a real Bot.
    monitor = PostScheduler.__new__(PostScheduler)
    monitor.scheduler = FakeJobs()
    monitor.bot = bot or FakeBot()
    return monitor


def _run(monitor, caplog):
    with caplog.at_level(logging.INFO):
        asyncio.run(monitor._monitor_scheduled_posts())
    assert not [r for r in caplog.records if r.getMessage().startswith(MONITOR_ERRORS)]


def _overdue_post(retry_count=0):
    return insert_post(scheduled=get_current_kyiv_time() - timedelta(minutes=10), retry_count=retry_count)


def _board_delete(post_id):
    service._store_delete(1, post_id)


def _board_unschedule(post_id):
    service._store_edit(1, post_id, {"scheduled_time": None})


BOARD_WRITES = {"deleted": _board_delete, "unscheduled": _board_unschedule}


def _after(monkeypatch, name, action):
    """Run `action` right after Database.<name> returns, as a board write landing at that moment."""
    original = getattr(Database, name)

    def wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        action()
        return result

    monkeypatch.setattr(Database, name, staticmethod(wrapped))


def _assert_left_as_board_stored(monitor, post_id, before, board_write):
    row = Database.get_post_by_id(post_id)
    if board_write == "deleted":
        assert (row["status"], row["scheduled_time"]) == ("deleted", before["scheduled_time"])
    else:
        assert (row["status"], row["scheduled_time"]) == ("pending", None)
    assert monitor.scheduler.get_job(f"post_{post_id}") is None
    assert not [text for _, text in monitor.bot.sent if f"#{post_id} " in text]


# --- the normal path ---------------------------------------------------------------


def test_overdue_post_without_job_is_rescheduled_in_ten_seconds(caplog):
    post_id = _overdue_post()
    monitor = _monitor()
    start = get_current_kyiv_time()

    _run(monitor, caplog)
    end = get_current_kyiv_time()

    row = Database.get_post_by_id(post_id)
    assert row["status"] == "pending"
    assert row["retry_count"] == 1
    assert start + timedelta(seconds=10) <= row["scheduled_time"] <= end + timedelta(seconds=10)
    job = monitor.scheduler.get_job(f"post_{post_id}")
    assert job.args == [post_id] and job.run_date == row["scheduled_time"]
    assert monitor.bot.sent == [(1, f"⚠️ Post #{post_id} was delayed. Rescheduling for immediate posting.")]


def test_later_reschedules_send_no_second_notice(caplog):
    post_id = _overdue_post(retry_count=2)
    monitor = _monitor()

    _run(monitor, caplog)

    assert Database.get_post_by_id(post_id)["retry_count"] == 3
    assert monitor.scheduler.get_job(f"post_{post_id}") is not None
    assert monitor.bot.sent == []


def test_post_over_the_reschedule_cap_is_marked_failed(caplog):
    post_id = _overdue_post(retry_count=5)
    monitor = _monitor()

    _run(monitor, caplog)

    row = Database.get_post_by_id(post_id)
    assert row["status"] == "failed"
    assert row["failure_reason"].startswith("Exceeded max reschedule attempts (5)")
    assert monitor.scheduler.get_job(f"post_{post_id}") is None
    assert len(monitor.bot.sent) == 1
    assert monitor.bot.sent[0][1].startswith(f"❌ Post #{post_id} failed after 6 retry attempts.")


# --- a board delete or unschedule during the pass ------------------------------------------


@pytest.mark.parametrize("board_write", BOARD_WRITES)
def test_board_write_after_the_overdue_list_was_read_is_kept(monkeypatch, caplog, board_write):
    post_id = _overdue_post()
    before = Database.get_post_by_id(post_id)
    _after(monkeypatch, "get_overdue_posts", lambda: BOARD_WRITES[board_write](post_id))
    monitor = _monitor()

    _run(monitor, caplog)

    _assert_left_as_board_stored(monitor, post_id, before, board_write)
    assert Database.get_post_by_id(post_id)["retry_count"] == 0


@pytest.mark.parametrize("board_write", BOARD_WRITES)
def test_board_write_after_the_retry_count_was_raised_is_kept(monkeypatch, caplog, board_write):
    post_id = _overdue_post()
    before = Database.get_post_by_id(post_id)
    _after(monkeypatch, "increment_retry_count", lambda: BOARD_WRITES[board_write](post_id))
    monitor = _monitor()

    _run(monitor, caplog)

    _assert_left_as_board_stored(monitor, post_id, before, board_write)


@pytest.mark.parametrize("board_write", BOARD_WRITES)
def test_board_write_while_notifying_about_an_earlier_post_is_kept(caplog, board_write):
    first = _overdue_post()
    later = insert_post(scheduled=get_current_kyiv_time() - timedelta(minutes=5))
    before = Database.get_post_by_id(later)

    def board_acts_during_the_await(text):
        if f"#{first} " in text:
            BOARD_WRITES[board_write](later)

    monitor = _monitor(FakeBot(on_send=board_acts_during_the_await))

    _run(monitor, caplog)

    assert monitor.scheduler.get_job(f"post_{first}") is not None
    _assert_left_as_board_stored(monitor, later, before, board_write)
    assert Database.get_post_by_id(later)["retry_count"] == 0


@pytest.mark.parametrize("board_write", BOARD_WRITES)
def test_board_write_before_giving_up_is_kept(monkeypatch, caplog, board_write):
    post_id = _overdue_post(retry_count=5)
    before = Database.get_post_by_id(post_id)
    _after(monkeypatch, "increment_retry_count", lambda: BOARD_WRITES[board_write](post_id))
    monitor = _monitor()

    _run(monitor, caplog)

    _assert_left_as_board_stored(monitor, post_id, before, board_write)
    assert Database.get_post_by_id(post_id)["failure_reason"] is None


def test_post_claimed_by_its_job_after_the_list_was_read_is_not_sent_again(monkeypatch, caplog):
    post_id = _overdue_post()
    _after(monkeypatch, "get_overdue_posts", lambda: Database.claim_post_for_sending(post_id))
    monitor = _monitor()

    _run(monitor, caplog)

    assert Database.get_post_by_id(post_id)["status"] == "sending"
    assert monitor.scheduler.get_job(f"post_{post_id}") is None
    assert monitor.bot.sent == []

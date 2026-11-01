"""Web board media replacement: rows-then-files, and a failed replace leaves the old state alone."""

import asyncio
import json
import os
import shutil
import sqlite3
from datetime import datetime

import pytest
import pytz

from bot.database import Database
from bot.web import service
from bot.web.errors import WebError
from bot.web.media import StoredUpload
from bot.web.queries import get_owned_post
from config import DATA_DIR, UPLOADS_DIR
from web_helpers import insert_post

USER_ID = 1
KYIV = pytz.timezone("Europe/Kiev")
FUTURE = KYIV.localize(datetime(2030, 9, 1, 12, 0))


@pytest.fixture(autouse=True)
def clean_uploads():
    """Start and end every test with an empty uploads folder."""
    shutil.rmtree(UPLOADS_DIR, ignore_errors=True)
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    yield
    shutil.rmtree(UPLOADS_DIR, ignore_errors=True)
    os.makedirs(UPLOADS_DIR, exist_ok=True)


def _upload_file(name, media_type="photo", user_id=USER_ID, data=b"data") -> str:
    folder = os.path.join(UPLOADS_DIR, "2030-06", "01", str(user_id), media_type)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def _stored(name="new.mp4", media_type="video", user_id=USER_ID) -> StoredUpload:
    path = _upload_file(name, media_type, user_id, data=b"new media")
    return StoredUpload(file_path=path, media_type=media_type, size=9, downgraded=False)


def _row(post_id, user_id=USER_ID):
    return get_owned_post(user_id, post_id, include_deleted=True)


def _set_status(post_id, status):
    conn = Database.get_connection()
    try:
        conn.execute("UPDATE posts SET status = ? WHERE id = ?", (status, post_id))
        conn.commit()
    finally:
        conn.close()


def _replace(post_id, stored, user_id=USER_ID):
    return asyncio.run(service.replace_media(user_id, post_id, stored))


def _fails(code, call) -> WebError:
    with pytest.raises(WebError) as exc:
        call()
    assert exc.value.code == code, exc.value.message
    return exc.value


# --- replace_media ----------------------------------------------------------------------


@pytest.mark.parametrize("status, scheduled", [
    ("pending", None),      # queued
    ("pending", FUTURE),    # scheduled
    ("failed", None),
])
def test_replace_updates_row_then_removes_old_file(monkeypatch, status, scheduled):
    old = _upload_file("old.jpg")
    pid = insert_post(status=status, scheduled=scheduled, file_path=old, media_type="photo")
    stored = _stored()
    before = _row(pid)

    # The old file may only go once the new path is committed.
    seen = []
    real_delete = service.delete_media_file

    def spy(path):
        seen.append((path, _row(pid)["file_path"]))
        return real_delete(path)

    monkeypatch.setattr(service, "delete_media_file", spy)

    detail = _replace(pid, stored)

    row = _row(pid)
    assert row["file_path"] == stored.file_path and row["media_type"] == "video"
    assert seen == [(os.path.realpath(old), stored.file_path)]
    assert not os.path.exists(old)
    assert os.path.exists(stored.file_path)
    # Nothing else about the post changed (the job needs no sync).
    for column in ("status", "scheduled_time", "description", "channel_id", "retry_count"):
        assert row[column] == before[column]
    assert detail["id"] == pid
    assert detail["media"] == [{"index": 0, "media_type": "video", "exists": True}]


def test_replace_when_old_file_already_missing():
    # The 7-day cleanup may have removed it: the row still names a file that is gone.
    gone = os.path.join(UPLOADS_DIR, "2030-06", "01", str(USER_ID), "photo", "gone.jpg")
    pid = insert_post(file_path=gone, media_type="photo")
    stored = _stored()

    detail = _replace(pid, stored)

    assert _row(pid)["file_path"] == stored.file_path
    assert os.path.exists(stored.file_path)
    assert detail["media"][0]["exists"] is True


def test_replace_removes_legacy_relative_old_file():
    # Legacy rows store the path relative to DATA_DIR; the working directory is not DATA_DIR.
    old = _upload_file("legacy.jpg")
    relative = os.path.relpath(old, DATA_DIR)
    assert not os.path.isabs(relative) and os.getcwd() != DATA_DIR
    pid = insert_post(file_path=relative, media_type="photo")
    stored = _stored()

    _replace(pid, stored)

    assert _row(pid)["file_path"] == stored.file_path
    assert not os.path.exists(old)
    assert os.path.exists(stored.file_path)


def test_replace_never_deletes_a_file_outside_uploads():
    outside = os.path.join(DATA_DIR, "outside.txt")
    with open(outside, "w") as handle:
        handle.write("keep me")
    try:
        pid = insert_post(file_path=outside, media_type="document")
        stored = _stored()

        _replace(pid, stored)

        assert _row(pid)["file_path"] == stored.file_path
        assert os.path.exists(outside)
    finally:
        os.remove(outside)


def test_replace_keeps_the_new_file_if_it_is_the_old_one():
    # Cannot happen with fresh upload names; the new file must still never be deleted.
    stored = _stored("same.mp4", "video")
    pid = insert_post(file_path=stored.file_path, media_type="video")

    _replace(pid, stored)

    assert _row(pid)["file_path"] == stored.file_path
    assert os.path.exists(stored.file_path)


def test_conflict_removes_new_file():
    old = _upload_file("old.jpg")
    pid = insert_post(scheduled=FUTURE, file_path=old, media_type="photo")
    _set_status(pid, "sending")  # the publish job claimed it
    stored = _stored()

    error = _fails("conflict", lambda: _replace(pid, stored))

    assert error.extra == {"status": "sending"}
    assert not os.path.exists(stored.file_path)
    assert os.path.exists(old)
    row = _row(pid)
    assert row["file_path"] == old and row["media_type"] == "photo" and row["status"] == "sending"


def test_claimed_between_check_and_write_removes_new_file(monkeypatch):
    old = _upload_file("old.jpg")
    pid = insert_post(scheduled=FUTURE, file_path=old, media_type="photo")
    stored = _stored()

    real_check = service.check_media_replaceable

    def claimed_after_check(user_id, post_id):
        row = real_check(user_id, post_id)
        _set_status(post_id, "sending")  # the publish job wins the race
        return row

    monkeypatch.setattr(service, "check_media_replaceable", claimed_after_check)

    error = _fails("conflict", lambda: _replace(pid, stored))

    assert error.extra == {"status": "sending"}
    assert "being sent" in error.message
    assert not os.path.exists(stored.file_path)
    assert os.path.exists(old)
    assert _row(pid)["file_path"] == old and _row(pid)["media_type"] == "photo"


def test_post_vanishing_between_check_and_write_is_not_found(monkeypatch):
    old = _upload_file("old.jpg")
    pid = insert_post(file_path=old, media_type="photo")
    stored = _stored()

    real_check = service.check_media_replaceable

    def removed_after_check(user_id, post_id):
        row = real_check(user_id, post_id)
        conn = Database.get_connection()
        try:
            conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
            conn.commit()
        finally:
            conn.close()
        return row

    monkeypatch.setattr(service, "check_media_replaceable", removed_after_check)

    _fails("not_found", lambda: _replace(pid, stored))

    assert not os.path.exists(stored.file_path)
    assert os.path.exists(old)


def test_database_error_removes_new_file_and_keeps_old(monkeypatch):
    old = _upload_file("old.jpg")
    pid = insert_post(file_path=old, media_type="photo")
    stored = _stored()

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service, "_apply_update", locked)

    with pytest.raises(sqlite3.OperationalError):
        _replace(pid, stored)

    assert not os.path.exists(stored.file_path)
    assert os.path.exists(old)
    assert _row(pid)["file_path"] == old


def test_failure_after_commit_keeps_the_new_file(monkeypatch):
    old = _upload_file("old.jpg")
    pid = insert_post(file_path=old, media_type="photo")
    stored = _stored()

    def broken(*args, **kwargs):
        raise RuntimeError("detail failed")

    monkeypatch.setattr(service, "get_post_detail", broken)

    with pytest.raises(RuntimeError):
        _replace(pid, stored)

    # The row already points at the new file, so it must stay.
    assert _row(pid)["file_path"] == stored.file_path
    assert os.path.exists(stored.file_path)


def test_replace_refused_post_removes_new_file():
    old = _upload_file("old.jpg")
    album = json.dumps([{"file_path": old, "media_type": "photo"}])
    pid = insert_post(file_path=old, media_type="album", media_bundle_json=album)
    stored = _stored()

    _fails("conflict", lambda: _replace(pid, stored))

    assert not os.path.exists(stored.file_path)
    assert os.path.exists(old)
    assert _row(pid)["media_type"] == "album"


def test_replace_cross_user_not_found():
    old = _upload_file("old.jpg", user_id=2)
    pid = insert_post(user_id=2, file_path=old, media_type="photo")
    stored = _stored()  # user 1's upload, aimed at user 2's post

    _fails("not_found", lambda: _replace(pid, stored, user_id=1))

    assert not os.path.exists(stored.file_path)
    assert os.path.exists(old)
    assert _row(pid, user_id=2)["file_path"] == old


# --- check_media_replaceable --------------------------------------------------------------


@pytest.mark.parametrize("status, scheduled", [
    ("pending", None),
    ("pending", FUTURE),
    ("failed", None),
])
def test_check_returns_the_raw_row(status, scheduled):
    pid = insert_post(status=status, scheduled=scheduled, file_path="f.jpg")

    row = service.check_media_replaceable(USER_ID, pid)

    assert row["id"] == pid and row["file_path"] == "f.jpg" and row["status"] == status


def test_album_and_posted_not_replaceable():
    album = json.dumps([{"file_path": "a.jpg", "media_type": "photo"}])
    album_id = insert_post(media_type="album", media_bundle_json=album)
    error = _fails("conflict", lambda: service.check_media_replaceable(USER_ID, album_id))
    assert error.extra == {"status": "queued"}
    assert "album" in error.message

    posted_id = insert_post(status="posted")
    error = _fails("conflict", lambda: service.check_media_replaceable(USER_ID, posted_id))
    assert error.extra == {"status": "posted"}

    sending_id = insert_post(scheduled=FUTURE, status="sending")
    error = _fails("conflict", lambda: service.check_media_replaceable(USER_ID, sending_id))
    assert error.extra == {"status": "sending"}
    assert "being sent" in error.message


def test_check_cross_user_and_missing_not_found():
    pid = insert_post(user_id=2)

    _fails("not_found", lambda: service.check_media_replaceable(1, pid))
    _fails("not_found", lambda: service.check_media_replaceable(1, 99999))


def test_check_deleted_post_not_found():
    pid = insert_post(status="deleted", deleted_from_status="pending")

    _fails("not_found", lambda: service.check_media_replaceable(USER_ID, pid))

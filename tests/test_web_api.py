"""Web board post API: thin routes over queries, service, media and captions, scoped to the session user."""

import asyncio
import io
import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta

import aiohttp
import pytest
import pytz
from PIL import Image

from bot.database import Database, parse_dt
from bot.utils import get_current_kyiv_time
from bot.web import media
from bot.web.queries import get_owned_post
from bot.web.server import SESSION_COOKIE
from config import THUMBS_DIR, UPLOADS_DIR
from web_helpers import FakeScheduler, add_channel, board_client, insert_post

BASE = "http://localhost:8080"
WRITE = {"Origin": BASE, "X-Board": "1"}
KYIV = pytz.timezone("Europe/Kiev")
FUTURE = KYIV.localize(datetime(2030, 9, 1, 12, 0))
SANDBOX = "sandbox"
BOTH_USERS = frozenset({1, 2})


@pytest.fixture(autouse=True)
def clean_dirs():
    """Start and end every test with empty uploads and thumbnail folders."""
    for folder in (UPLOADS_DIR, THUMBS_DIR):
        shutil.rmtree(folder, ignore_errors=True)
        os.makedirs(folder, exist_ok=True)
    yield
    for folder in (UPLOADS_DIR, THUMBS_DIR):
        shutil.rmtree(folder, ignore_errors=True)
        os.makedirs(folder, exist_ok=True)


# --- helpers ------------------------------------------------------------------------


def _upload_file(name, data=b"data", user_id=1, media_type="photo") -> str:
    folder = os.path.join(UPLOADS_DIR, "2030-06", "01", str(user_id), media_type)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def _png_bytes(size=(400, 200), color=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def _read(path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _files_under(folder) -> list[str]:
    return sorted(os.path.join(root, name) for root, _dirs, files in os.walk(folder) for name in files)


def _row(post_id, user_id=1) -> dict:
    return get_owned_post(user_id, post_id, include_deleted=True)


def _set_status(post_id, status):
    conn = Database.get_connection()
    try:
        conn.execute("UPDATE posts SET status = ? WHERE id = ?", (status, post_id))
        conn.commit()
    finally:
        conn.close()


async def _expect(response, status) -> dict:
    body = await response.json()
    assert response.status == status, body
    return body


def _form(data=b"new media", filename="new.png", as_file=None, as_file_after=False) -> aiohttp.FormData:
    form = aiohttp.FormData()
    if as_file is not None and not as_file_after:
        form.add_field("as_file", as_file)
    form.add_field("file", data, filename=filename, content_type="application/octet-stream")
    if as_file is not None and as_file_after:
        form.add_field("as_file", as_file)
    return form


def _multipart(body: bytes, boundary="xyz") -> dict:
    return {"data": body, "headers": {**WRITE, "Content-Type": f"multipart/form-data; boundary={boundary}"}}


def _near(when, expected, slack=timedelta(seconds=30)) -> bool:
    return abs(when - expected) <= slack


# --- list ---------------------------------------------------------------------------


def test_list_posts_happy_path():
    add_channel(1, "-1001", "Alpha")
    add_channel(1, "-1002", "Beta")
    first = insert_post(scheduled=FUTURE, channel_id="-1001", description="Morning coffee")
    second = insert_post(scheduled=FUTURE + timedelta(hours=1), channel_id="-1002", description="Evening tea")
    third = insert_post(scheduled=FUTURE + timedelta(hours=2), channel_id="-1001", description="Coffee again")
    insert_post(user_id=2, scheduled=FUTURE, channel_id="-1001", description="Coffee of user 2")
    queued = insert_post(channel_id="-1001")

    async def scenario():
        async with board_client() as client:
            body = await _expect(await client.get("/api/posts?status=scheduled"), 200)
            assert [item["id"] for item in body["items"]] == [first, second, third]
            assert body["next_cursor"] is None
            item = body["items"][0]
            assert item["status"] == "scheduled" and item["channel_name"] == "Alpha"
            assert item["caption_text"] == "Morning coffee"
            assert parse_dt(item["scheduled_time"]) == FUTURE

            # Empty channel, q and cursor values are the same as leaving them out.
            same = await _expect(await client.get("/api/posts?status=scheduled&channel=&q=&cursor="), 200)
            assert same == body

            by_channel = await _expect(await client.get("/api/posts?status=scheduled&channel=-1001"), 200)
            assert [item["id"] for item in by_channel["items"]] == [first, third]
            searched = await _expect(await client.get("/api/posts", params={"status": "scheduled", "q": "COFFEE"}), 200)
            assert [item["id"] for item in searched["items"]] == [first, third]

            page = await _expect(await client.get("/api/posts?status=scheduled&limit=2"), 200)
            assert [item["id"] for item in page["items"]] == [first, second]
            assert page["next_cursor"]
            rest = await _expect(
                await client.get("/api/posts", params={"status": "scheduled", "limit": "2", "cursor": page["next_cursor"]}),
                200,
            )
            assert [item["id"] for item in rest["items"]] == [third]
            assert rest["next_cursor"] is None

            queue = await _expect(await client.get("/api/posts?status=queued"), 200)
            assert [item["id"] for item in queue["items"]] == [queued]

    asyncio.run(scenario())


@pytest.mark.parametrize("query", [
    "",                                 # status is required
    "status=nope",
    "status=deleted",
    "status=queued&limit=abc",
    "status=queued&limit=1.5",
    "status=queued&limit=",
    "status=queued&limit=+5",
    "status=queued&limit=%205",
    "status=queued&limit=1_0",
    "status=queued&limit=" + "9" * 5000,
    "status=queued&limit=0",
    "status=queued&limit=101",
    "status=queued&limit=-3",
    "status=queued&cursor=garbage",
])
def test_list_posts_parameter_errors(query):
    async def scenario():
        async with board_client() as client:
            body = await _expect(await client.get(f"/api/posts?{query}"), 400)
            assert body["error"] == "invalid_request" and body["message"]

    asyncio.run(scenario())


# --- detail -------------------------------------------------------------------------


def test_post_detail():
    add_channel(1, "-1001", "Alpha", signature="— Alpha")
    path = _upload_file("pic.png", _png_bytes())
    pid = insert_post(scheduled=FUTURE, channel_id="-1001", description="Hello", file_path=path, media_type="photo")
    deleted = insert_post(status="deleted", deleted_from_status="pending")

    async def scenario():
        async with board_client() as client:
            body = await _expect(await client.get(f"/api/posts/{pid}"), 200)
            assert body["id"] == pid and body["status"] == "scheduled"
            assert body["caption_tagged"] == "Hello" and body["caption_html"] == "Hello"
            assert body["media"] == [{"index": 0, "media_type": "photo", "exists": True}]
            assert body["signature"] == "— Alpha" and body["signature_fits"] is True
            assert body["can"] == {"edit": True, "media": True, "post_now": True, "retry": False, "delete": True}

            for path_ in (f"/api/posts/{deleted}", "/api/posts/999999", "/api/posts/" + "9" * 20):
                assert (await _expect(await client.get(path_), 404))["error"] == "not_found", path_
            # A non-numeric or absurdly long id never reaches a handler.
            for path_ in ("/api/posts/abc", "/api/posts/1.5", "/api/posts/" + "9" * 21, "/api/posts/%D9%A3"):
                assert (await _expect(await client.get(path_), 404))["error"] == "not_found", path_

    asyncio.run(scenario())


# --- PATCH --------------------------------------------------------------------------


def test_patch_post_happy_path():
    scheduler = FakeScheduler()
    pid = insert_post(description="Old caption")

    async def scenario():
        async with board_client(scheduler=scheduler) as client:
            response = await client.patch(
                f"/api/posts/{pid}",
                json={"caption": "<b>New</b> caption", "scheduled_time": "2030-09-01T12:00"},
                headers=WRITE,
            )
            body = await _expect(response, 200)
            assert body["id"] == pid and body["status"] == "scheduled"
            assert body["caption_tagged"] == "<b>New</b> caption"
            assert parse_dt(body["scheduled_time"]) == FUTURE

            # scheduled_time null unschedules: the post goes back to the queue.
            back = await _expect(await client.patch(f"/api/posts/{pid}", json={"scheduled_time": None}, headers=WRITE), 200)
            assert back["status"] == "queued" and back["scheduled_time"] is None

    asyncio.run(scenario())
    assert scheduler.calls == [("schedule", pid, FUTURE), ("cancel", pid)]
    row = _row(pid)
    assert row["description"] == "New caption" and row["scheduled_time"] is None


def test_patch_sending_post_is_conflict():
    scheduler = FakeScheduler()
    pid = insert_post(scheduled=FUTURE, status="sending", description="Going out")

    async def scenario():
        async with board_client(scheduler=scheduler) as client:
            response = await client.patch(f"/api/posts/{pid}", json={"caption": "Too late"}, headers=WRITE)
            body = await _expect(response, 409)
            assert body["error"] == "conflict" and body["status"] == "sending"

    asyncio.run(scenario())
    assert scheduler.calls == []
    assert _row(pid)["description"] == "Going out"


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'"caption"', b"null", b"[" * 100_000, b"\xff\xfe"])
def test_json_bodies_must_be_objects(raw):
    pid = insert_post(description="Untouched")

    async def scenario():
        async with board_client() as client:
            headers = {**WRITE, "Content-Type": "application/json"}
            for method, path in (
                ("PATCH", f"/api/posts/{pid}"),
                ("POST", "/api/posts/bulk"),
                ("POST", "/api/caption/preview"),
            ):
                response = await client.request(method, path, data=raw, headers=headers)
                body = await _expect(response, 400)
                assert body["error"] == "invalid_request", (method, path)

    asyncio.run(scenario())
    assert _row(pid)["description"] == "Untouched"


def test_patch_unknown_field_is_invalid():
    pid = insert_post(description="Untouched")

    async def scenario():
        async with board_client() as client:
            for changes in ({}, {"status": "posted"}, {"caption": "<b>open"}):
                body = await _expect(await client.patch(f"/api/posts/{pid}", json=changes, headers=WRITE), 400)
                assert body["error"] == "invalid_request", changes

    asyncio.run(scenario())
    assert _row(pid)["description"] == "Untouched"


# --- single-post actions --------------------------------------------------------------


def test_post_now_flow():
    scheduler = FakeScheduler()
    pid = insert_post()
    posted = insert_post(status="posted")

    async def scenario():
        async with board_client(scheduler=scheduler) as client:
            body = await _expect(await client.post(f"/api/posts/{pid}/post-now", headers=WRITE), 200)
            assert body["id"] == pid and body["status"] == "scheduled"
            refused = await _expect(await client.post(f"/api/posts/{posted}/post-now", headers=WRITE), 409)
            assert refused["status"] == "posted"

    before = get_current_kyiv_time()
    asyncio.run(scenario())
    [(kind, post_id, when)] = scheduler.calls
    assert (kind, post_id) == ("schedule", pid)
    assert _near(when, before + timedelta(seconds=60))
    assert _row(pid)["scheduled_time"] == when


def test_retry_flow():
    scheduler = FakeScheduler()
    pid = insert_post(status="failed", failure_reason="Flood wait", retry_count=3)
    queued = insert_post()

    async def scenario():
        async with board_client(scheduler=scheduler) as client:
            body = await _expect(await client.post(f"/api/posts/{pid}/retry", headers=WRITE), 200)
            assert body["status"] == "scheduled" and body["failure_reason"] is None and body["retry_count"] == 0
            refused = await _expect(await client.post(f"/api/posts/{queued}/retry", headers=WRITE), 409)
            assert refused["status"] == "queued"

    before = get_current_kyiv_time()
    asyncio.run(scenario())
    [(kind, post_id, when)] = scheduler.calls
    assert (kind, post_id) == ("schedule", pid)
    assert _near(when, before + timedelta(seconds=10))
    assert _row(pid)["status"] == "pending"


def test_delete_and_restore_flow():
    scheduler = FakeScheduler()
    pid = insert_post(scheduled=FUTURE)

    async def scenario():
        async with board_client(scheduler=scheduler) as client:
            deleted = await _expect(await client.delete(f"/api/posts/{pid}", headers=WRITE), 200)
            assert deleted == {"id": pid, "status": "deleted"}
            assert (await client.get(f"/api/posts/{pid}")).status == 404
            again = await _expect(await client.delete(f"/api/posts/{pid}", headers=WRITE), 404)
            assert again["error"] == "not_found"

            restored = await _expect(await client.post(f"/api/posts/{pid}/restore", headers=WRITE), 200)
            assert restored["id"] == pid and restored["status"] == "scheduled"
            assert parse_dt(restored["scheduled_time"]) == FUTURE
            twice = await _expect(await client.post(f"/api/posts/{pid}/restore", headers=WRITE), 409)
            assert twice["status"] == "scheduled"

    asyncio.run(scenario())
    assert scheduler.calls == [("cancel", pid), ("schedule", pid, FUTURE)]
    assert _row(pid)["status"] == "pending"


def test_post_routes_need_session_and_board_header():
    pid = insert_post(description="Untouched")

    async def scenario():
        async with board_client() as client:
            # No X-Board header: the CSRF gate refuses before the route runs.
            refused = await _expect(await client.delete(f"/api/posts/{pid}", headers={"Origin": BASE}), 403)
            assert refused["error"] == "forbidden"
            client.session.cookie_jar.clear()
            for method, path in (("GET", "/api/posts?status=queued"), ("GET", f"/api/posts/{pid}"),
                                 ("DELETE", f"/api/posts/{pid}")):
                body = await _expect(await client.request(method, path, headers=WRITE), 401)
                assert body["error"] == "unauthorized", path

    asyncio.run(scenario())
    assert _row(pid)["status"] == "pending"


# --- bulk ---------------------------------------------------------------------------


def test_bulk_happy_path_and_per_post_failure():
    scheduler = FakeScheduler()
    add_channel(1, "-1001", "Alpha")
    add_channel(2, "-1009", "Not yours")
    a = insert_post(scheduled=FUTURE)
    b = insert_post(scheduled=FUTURE + timedelta(hours=1))
    posted = insert_post(status="posted")
    foreign = insert_post(user_id=2, scheduled=FUTURE)

    async def scenario():
        async with board_client(scheduler=scheduler) as client:
            async def bulk(payload, status=200):
                return await _expect(await client.post("/api/posts/bulk", json=payload, headers=WRITE), status)

            body = await bulk({"action": "delete", "ids": [a, b, foreign, 999999, a]})
            assert body["results"] == [
                {"id": a, "ok": True, "error": None},
                {"id": b, "ok": True, "error": None},
                {"id": foreign, "ok": False, "error": "Post not found"},
                {"id": 999999, "ok": False, "error": "Post not found"},
            ]
            body = await bulk({"action": "restore", "ids": [a, b]})
            assert [result["ok"] for result in body["results"]] == [True, True]

            body = await bulk({"action": "shift", "ids": [a, posted], "shift": {"unit": "hours", "amount": 2}})
            assert body["results"][0] == {"id": a, "ok": True, "error": None}
            assert body["results"][1]["id"] == posted and body["results"][1]["ok"] is False
            assert "posted" in body["results"][1]["error"]

            body = await bulk({"action": "move_channel", "ids": [b], "channel_id": "-1001"})
            assert body["results"] == [{"id": b, "ok": True, "error": None}]

            # The service validates the request as a whole; nothing changes.
            for payload in (
                {"action": "explode", "ids": [a]},
                {"action": "delete", "ids": str(a)},
                {"action": "delete", "ids": []},
                {"action": "delete", "ids": [True]},
                {"action": "delete", "ids": list(range(1, 502))},
                {"action": "shift", "ids": [a], "shift": "2h"},
                {"action": "shift", "ids": [a], "shift": {"unit": "weeks", "amount": 1}},
                {"action": "move_channel", "ids": [b], "channel_id": "-1009"},
                {"ids": [a]},
            ):
                assert (await bulk(payload, 400))["error"] == "invalid_request", payload

    asyncio.run(scenario())
    assert scheduler.calls == [
        ("cancel", a), ("cancel", b),
        ("schedule", a, FUTURE), ("schedule", b, FUTURE + timedelta(hours=1)),
        ("schedule", a, FUTURE + timedelta(hours=2)),
    ]
    assert _row(a)["status"] == "pending" and _row(a)["scheduled_time"] == FUTURE + timedelta(hours=2)
    assert _row(b)["channel_id"] == "-1001"
    assert _row(posted)["status"] == "posted"
    assert _row(foreign, user_id=2)["status"] == "pending"


# --- ownership ----------------------------------------------------------------------


def test_ownership_everywhere(monkeypatch):
    scheduler = FakeScheduler()
    path = _upload_file("pic.png", _png_bytes())
    pid = insert_post(scheduled=FUTURE, description="Mine", file_path=path, media_type="photo")
    failed = insert_post(status="failed", description="Mine too")
    deleted = insert_post(status="deleted", deleted_from_status="pending")
    before = {post_id: _row(post_id) for post_id in (pid, failed, deleted)}

    stored = []

    async def spy_store_upload(*args, **kwargs):
        stored.append(args)
        raise AssertionError("the body of a foreign post's upload must not be read")

    monkeypatch.setattr(media, "store_upload", spy_store_upload)

    async def scenario():
        async with board_client(user_id=2, scheduler=scheduler, allowed=BOTH_USERS) as client:
            requests = [
                ("GET", f"/api/posts/{pid}", {}),
                ("GET", f"/api/posts/{pid}/media", {}),
                ("GET", f"/api/posts/{pid}/thumb", {}),
                ("PATCH", f"/api/posts/{pid}", {"json": {"caption": "Hijacked"}, "headers": WRITE}),
                ("DELETE", f"/api/posts/{pid}", {"headers": WRITE}),
                ("POST", f"/api/posts/{pid}/post-now", {"headers": WRITE}),
                ("POST", f"/api/posts/{failed}/retry", {"headers": WRITE}),
                ("POST", f"/api/posts/{deleted}/restore", {"headers": WRITE}),
                ("PUT", f"/api/posts/{pid}/media", {"data": _form(), "headers": WRITE}),
            ]
            for method, url, kwargs in requests:
                body = await _expect(await client.request(method, url, **kwargs), 404)
                assert body["error"] == "not_found", (method, url)

            listed = await _expect(await client.get("/api/posts?status=scheduled"), 200)
            assert listed["items"] == []
            bulk = await _expect(
                await client.post("/api/posts/bulk", json={"action": "delete", "ids": [pid, failed]}, headers=WRITE), 200
            )
            assert [result["ok"] for result in bulk["results"]] == [False, False]

    asyncio.run(scenario())
    assert scheduler.calls == []
    assert stored == []
    assert {post_id: _row(post_id) for post_id in before} == before
    assert _files_under(UPLOADS_DIR) == [path]


# --- media and thumbnails -----------------------------------------------------------


def test_media_missing_file_is_404_not_500():
    # Review Focus 3: the 7-day cleanup (or anything else) removed the file the row names.
    gone = os.path.join(UPLOADS_DIR, "2030-06", "01", "1", "photo", "gone.jpg")
    pid = insert_post(description="Lost file", file_path=gone, media_type="photo")
    new_png = _png_bytes()

    async def scenario():
        async with board_client() as client:
            listed = await _expect(await client.get("/api/posts?status=queued"), 200)
            assert [item["id"] for item in listed["items"]] == [pid]
            detail = await _expect(await client.get(f"/api/posts/{pid}"), 200)
            assert detail["media"] == [{"index": 0, "media_type": "photo", "exists": False}]
            assert detail["can"]["media"] is True

            for url in (f"/api/posts/{pid}/media", f"/api/posts/{pid}/thumb"):
                body = await _expect(await client.get(url), 404)
                assert body["error"] == "not_found", url

            # Replacing the media still works and brings the post back to a real file.
            replaced = await _expect(
                await client.put(f"/api/posts/{pid}/media", data=_form(new_png, "fresh.png"), headers=WRITE), 200
            )
            assert replaced["media"] == [{"index": 0, "media_type": "photo", "exists": True}]
            served = await client.get(f"/api/posts/{pid}/media")
            assert served.status == 200 and await served.read() == new_png
            assert (await client.get(f"/api/posts/{pid}/thumb")).status == 200

    asyncio.run(scenario())


def test_html_document_served_as_attachment_with_sandbox():
    page = b"<html><script>alert(document.cookie)</script></html>"
    cases = [
        ("page.html", "document"),
        ("drawing.svg", "photo"),     # the stored media_type does not make it inline
        ("report.pdf", "document"),
        ("clip.mov", "video"),
        ("noextension", "document"),
    ]
    posts = [(name, insert_post(file_path=_upload_file(name, page, media_type=kind), media_type=kind))
             for name, kind in cases]

    async def scenario():
        async with board_client() as client:
            for name, pid in posts:
                response = await client.get(f"/api/posts/{pid}/media")
                assert response.status == 200, name
                assert response.headers["Content-Disposition"].startswith("attachment"), name
                assert response.headers["Content-Type"] == "application/octet-stream", name
                assert response.headers["Content-Security-Policy"] == SANDBOX, name
                assert response.headers["X-Content-Type-Options"] == "nosniff", name
                assert await response.read() == page, name
            html_pid = posts[0][1]
            disposition = (await client.get(f"/api/posts/{html_pid}/media")).headers["Content-Disposition"]
            assert disposition == f'attachment; filename="post-{html_pid}-0.html"'

    asyncio.run(scenario())


def test_image_served_inline():
    png = _png_bytes()
    second = _png_bytes(color=(10, 200, 10))
    path = _upload_file("pic.png", png)
    pid = insert_post(file_path=path, media_type="photo")
    album = json.dumps([
        {"file_path": path, "media_type": "photo"},
        {"file_path": _upload_file("two.png", second), "media_type": "photo"},
    ])
    album_id = insert_post(file_path=path, media_type="album", media_bundle_json=album)
    inline = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
              ".mp4": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
              ".m4a": "audio/mp4", ".PNG": "image/png"}
    typed = {ext: insert_post(file_path=_upload_file(f"file{ext}", b"bytes"), media_type="document") for ext in inline}

    async def scenario():
        async with board_client() as client:
            response = await client.get(f"/api/posts/{pid}/media")
            assert response.status == 200
            assert response.headers["Content-Type"] == "image/png"
            assert "Content-Disposition" not in response.headers
            assert response.headers["Content-Security-Policy"] == SANDBOX
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert await response.read() == png

            # Range requests (video seeking) keep the media headers.
            ranged = await client.get(f"/api/posts/{pid}/media", headers={"Range": "bytes=0-9"})
            assert ranged.status == 206
            assert await ranged.read() == png[:10]
            assert ranged.headers["Content-Range"] == f"bytes 0-9/{len(png)}"
            assert ranged.headers["Content-Security-Policy"] == SANDBOX

            head = await client.head(f"/api/posts/{pid}/media")
            assert head.status == 200 and head.headers["Content-Security-Policy"] == SANDBOX

            assert await (await client.get(f"/api/posts/{album_id}/media?i=1")).read() == second
            assert await (await client.get(f"/api/posts/{album_id}/media?i=0")).read() == png
            for query, status in (("i=2", 404), ("i=-1", 404), ("i=x", 400), ("i=", 400)):
                body = await _expect(await client.get(f"/api/posts/{album_id}/media?{query}"), status)
                assert body["error"] == ("not_found" if status == 404 else "invalid_request"), query
            assert (await client.get(f"/api/posts/{pid}/media?i=1")).status == 404

            for ext, content_type in inline.items():
                response = await client.get(f"/api/posts/{typed[ext]}/media")
                assert response.status == 200, ext
                assert response.headers["Content-Type"] == content_type, ext
                assert "Content-Disposition" not in response.headers, ext
                assert response.headers["Content-Security-Policy"] == SANDBOX, ext

    asyncio.run(scenario())


def test_thumbnail_route():
    photo = insert_post(file_path=_upload_file("pic.png", _png_bytes((400, 200))), media_type="photo")
    # Ruling 10: no media_type gate; get_thumbnail decides from the file's content.
    png_document = insert_post(file_path=_upload_file("scan.png", _png_bytes(), media_type="document"),
                               media_type="document")
    text_photo = insert_post(file_path=_upload_file("notes.png", b"not an image"), media_type="photo")
    video = insert_post(file_path=_upload_file("clip.mp4", b"\x00\x00\x00 ftypisom"), media_type="video")

    async def scenario():
        async with board_client() as client:
            response = await client.get(f"/api/posts/{photo}/thumb")
            assert response.status == 200
            assert response.headers["Content-Type"] == "image/webp"
            assert response.headers["Cache-Control"] == "private, max-age=3600"
            assert response.headers["Content-Security-Policy"] == SANDBOX
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            with Image.open(io.BytesIO(await response.read())) as image:
                assert image.format == "WEBP" and image.size == (160, 80)

            small = await client.get(f"/api/posts/{photo}/thumb?w=64&i=0")
            with Image.open(io.BytesIO(await small.read())) as image:
                assert image.size == (64, 32)

            assert (await client.get(f"/api/posts/{png_document}/thumb")).status == 200
            for pid in (text_photo, video):
                assert (await _expect(await client.get(f"/api/posts/{pid}/thumb"), 404))["error"] == "not_found"
            for query in ("w=abc", "w=1.5", "i=x"):
                body = await _expect(await client.get(f"/api/posts/{photo}/thumb?{query}"), 400)
                assert body["error"] == "invalid_request", query
            assert (await client.get(f"/api/posts/{photo}/thumb?i=1")).status == 404

    asyncio.run(scenario())


# --- PUT media ----------------------------------------------------------------------


def test_put_media_happy_path_and_as_file():
    old = _upload_file("old.jpg", b"old bytes")
    pid = insert_post(scheduled=FUTURE, file_path=old, media_type="photo")
    png = _png_bytes()

    async def scenario():
        async with board_client() as client:
            response = await client.put(f"/api/posts/{pid}/media", data=_form(png, "holiday.png"), headers=WRITE)
            body = await _expect(response, 200)
            assert body["id"] == pid and body["status"] == "scheduled"
            assert body["media_type"] == "photo"
            assert body["media"] == [{"index": 0, "media_type": "photo", "exists": True}]
            first = _row(pid)["file_path"]
            assert _read(first) == png and first.endswith(".png") and "holiday" not in first
            assert not os.path.exists(old)  # rows-then-files: the old file went after the commit

            # as_file=1, sent before the file, stores the same picture as a document.
            response = await client.put(f"/api/posts/{pid}/media", data=_form(png, "b.png", as_file="1"), headers=WRITE)
            body = await _expect(response, 200)
            assert body["media_type"] == "document"
            assert body["media"] == [{"index": 0, "media_type": "document", "exists": True}]
            second = _row(pid)["file_path"]
            assert os.sep + "document" + os.sep in second and _read(second) == png
            assert not os.path.exists(first)

            # as_file=0 is an explicit "no".
            response = await client.put(f"/api/posts/{pid}/media", data=_form(png, "c.png", as_file="0"), headers=WRITE)
            assert (await _expect(response, 200))["media_type"] == "photo"

    asyncio.run(scenario())
    row = _row(pid)
    assert row["media_type"] == "photo" and row["scheduled_time"] == FUTURE
    assert _files_under(UPLOADS_DIR) == [row["file_path"]]


def test_put_media_notice_when_photo_stored_as_file(monkeypatch):
    """Spec 6.6 step 4: a photo over Telegram's photo limit is stored as a document, with a notice."""
    monkeypatch.setattr(media, "PHOTO_MAX_BYTES", 1024)
    pid = insert_post(scheduled=FUTURE, file_path=_upload_file("old.jpg"), media_type="photo")
    small = _png_bytes(size=(4, 4))
    buffer = io.BytesIO()
    Image.frombytes("RGB", (64, 64), os.urandom(64 * 64 * 3)).save(buffer, "PNG")  # noise: no compression
    large = buffer.getvalue()
    assert len(small) <= 1024 < len(large)

    async def scenario():
        async with board_client() as client:
            response = await client.put(f"/api/posts/{pid}/media", data=_form(large, "big.png"), headers=WRITE)
            body = await _expect(response, 200)
            assert body["media_type"] == "document"
            assert "photo limit" in body["notice"] and "original quality" in body["notice"], body["notice"]
            # Otherwise the answer is the plain post detail, without the key.
            detail = await _expect(await client.get(f"/api/posts/{pid}"), 200)
            assert {key: value for key, value in body.items() if key != "notice"} == detail

            response = await client.put(f"/api/posts/{pid}/media", data=_form(small, "small.png"), headers=WRITE)
            body = await _expect(response, 200)
            assert body["media_type"] == "photo" and "notice" not in body
            # A file the person chose to send as a file was not downgraded: no notice either.
            response = await client.put(
                f"/api/posts/{pid}/media", data=_form(large, "chosen.png", as_file="1"), headers=WRITE
            )
            body = await _expect(response, 200)
            assert body["media_type"] == "document" and "notice" not in body

    asyncio.run(scenario())


async def _raw_put_with_unfinished_body(client, post_id) -> tuple[int, dict]:
    """PUT media announcing a 10 MB body but sending only part of its first boundary, then read the answer.

    Reading any of the multipart body (even the first part's headers) before
    answering would block here until the timeout.
    """
    session = client.session.cookie_jar.filter_cookies(client.make_url("/"))[SESSION_COOKIE].value
    head = (
        f"PUT /api/posts/{post_id}/media HTTP/1.1\r\n"
        "Host: localhost\r\n"
        f"Origin: {BASE}\r\n"
        "X-Board: 1\r\n"
        f"Cookie: {SESSION_COOKIE}={session}\r\n"
        "Content-Type: multipart/form-data; boundary=xyz\r\n"
        "Content-Length: 10000000\r\n"
        "\r\n"
        "--xy"
    )
    reader, writer = await asyncio.open_connection(client.server.host, client.server.port)
    try:
        writer.write(head.encode())
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), 5)
        headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        length = int(re.search(rb"(?im)^content-length: *([0-9]+)", headers).group(1))
        body = json.loads(await asyncio.wait_for(reader.readexactly(length), 5))
    finally:
        writer.close()
        await writer.wait_closed()
    return int(status_line.split()[1]), body


@pytest.mark.parametrize("kind", ["album", "sending", "posted"])
def test_put_media_album_conflicts_before_reading_body(monkeypatch, kind):
    old = _upload_file("old.jpg")
    if kind == "album":
        bundle = json.dumps([{"file_path": old, "media_type": "photo"}, {"file_path": old, "media_type": "photo"}])
        pid = insert_post(file_path=old, media_type="album", media_bundle_json=bundle)
        expected_status = "queued"
    else:
        pid = insert_post(scheduled=FUTURE, status=kind, file_path=old, media_type="photo")
        expected_status = kind
    before = _row(pid)

    stored = []

    async def spy_store_upload(*args, **kwargs):
        stored.append(args)
        raise AssertionError("the upload must not be read")

    monkeypatch.setattr(media, "store_upload", spy_store_upload)

    async def scenario():
        async with board_client() as client:
            status, body = await _raw_put_with_unfinished_body(client, pid)
            assert status == 409, body
            assert body["error"] == "conflict" and body["status"] == expected_status
            # The board keeps serving afterwards.
            assert (await client.get(f"/api/posts/{pid}")).status == 200

    asyncio.run(scenario())
    assert stored == []
    assert _row(pid) == before
    assert _files_under(UPLOADS_DIR) == [old]


def test_put_media_too_large(monkeypatch):
    old = _upload_file("old.jpg", b"old bytes")
    pid = insert_post(file_path=old, media_type="photo")
    monkeypatch.setattr(media, "MAX_UPLOAD_BYTES", 1024)

    async def scenario():
        async with board_client() as client:
            response = await client.put(f"/api/posts/{pid}/media", data=_form(b"x" * 4096, "big.jpg"), headers=WRITE)
            body = await _expect(response, 413)
            assert body["error"] == "too_large" and "1024 bytes" in body["message"]
            # At the limit it fits.
            response = await client.put(f"/api/posts/{pid}/media", data=_form(b"x" * 1024, "ok.jpg"), headers=WRITE)
            assert (await _expect(response, 200))["media"][0]["exists"] is True

    asyncio.run(scenario())
    row = _row(pid)
    assert row["file_path"] != old and _read(row["file_path"]) == b"x" * 1024
    assert _files_under(UPLOADS_DIR) == [row["file_path"]]  # the refused upload left nothing behind


def _part(name, value: bytes, filename=None) -> bytes:
    disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
    return f"--xyz\r\nContent-Disposition: {disposition}\r\n\r\n".encode() + value + b"\r\n"


def test_put_media_malformed_requests():
    old = _upload_file("old.jpg", b"old bytes")
    pid = insert_post(file_path=old, media_type="photo")
    before = _row(pid)
    end = b"--xyz--\r\n"
    bad = [
        {"json": {"file": "x"}, "headers": WRITE},                                    # not multipart
        {"data": b"file=x", "headers": {**WRITE, "Content-Type": "application/x-www-form-urlencoded"}},
        {"data": b"--xyz\r\n", "headers": {**WRITE, "Content-Type": "multipart/form-data"}},  # no boundary
        _multipart(b"garbage without any boundary"),
        _multipart(b""),
        _multipart(_part("as_file", b"1") + end),                                    # no file field
        _multipart(_part("note", b"hello") + _part("file", b"data", "a.jpg") + end),  # unexpected field first
        _multipart(_part("as_file", b"yes please") + _part("file", b"data", "a.jpg") + end),
        _multipart(_part("as_file", b"1" * 100_000) + _part("file", b"data", "a.jpg") + end),
        _multipart(_part("file", b"", "empty.jpg") + end),                            # empty upload
        _multipart(b'--xyz\r\nContent-Disposition: form-data; name="file"; filename="a.jpg"\r\n\r\nno end'),
    ]

    async def scenario():
        async with board_client() as client:
            for kwargs in bad:
                response = await client.put(f"/api/posts/{pid}/media", **kwargs)
                body = await _expect(response, 400)
                assert body["error"] == "invalid_request", kwargs

            # as_file after the file is too late: the route stops reading at the file.
            late = await client.put(f"/api/posts/{pid}/media", data=_form(b"data", "a.jpg", "1", True), headers=WRITE)
            assert (await _expect(late, 200))["media_type"] == "photo"

    asyncio.run(scenario())
    row = _row(pid)
    assert {k: v for k, v in row.items() if k not in ("file_path",)} == \
        {k: v for k, v in before.items() if k not in ("file_path",)}
    assert _files_under(UPLOADS_DIR) == [row["file_path"]]  # only the last, accepted upload


# --- caption preview ----------------------------------------------------------------


def test_preview_endpoint(monkeypatch):
    add_channel(1, "-1001", "Alpha", signature="— Alpha")
    add_channel(1, "-1002", "Long", signature="s" * 1000)
    add_channel(2, "-1003", "Theirs", signature="t" * 1000)

    # The signature is looked up only for a channel user_has_channel confirms.
    looked_up = []
    real_signature = Database.get_channel_signature

    def spy_signature(user_id, channel_id):
        looked_up.append(channel_id)
        return real_signature(user_id, channel_id)

    monkeypatch.setattr(Database, "get_channel_signature", staticmethod(spy_signature))

    async def scenario():
        async with board_client() as client:
            async def preview(payload):
                return await _expect(await client.post("/api/caption/preview", json=payload, headers=WRITE), 200)

            body = await preview({"caption": "<b>hi</b> & <i>x</i>"})
            assert body == {"ok": True, "html": "<b>hi</b> &amp; <i>x</i>", "length": 6, "limit": 1024,
                            "signature_fits": None, "error": None}

            invalid = await preview({"caption": "<b>hi"})
            assert invalid["ok"] is False and invalid["html"] is None and invalid["error"]
            assert (await preview({}))["ok"] is False  # no caption at all is still a 200

            caption = "c" * 100
            assert (await preview({"caption": caption, "channel_id": "-1001"}))["signature_fits"] is True
            assert (await preview({"caption": caption, "channel_id": "-1002"}))["signature_fits"] is False
            # Another user's channel, an unknown one or a malformed id supplies no signature.
            for channel_id in ("-1003", "-9999", "", 5, ["-1002"], None):
                body = await preview({"caption": caption, "channel_id": channel_id})
                assert body["ok"] is True and body["signature_fits"] is None, channel_id

    asyncio.run(scenario())
    assert looked_up == ["-1001", "-1002"]


# --- logging ------------------------------------------------------------------------


def test_state_changes_are_logged_without_captions(caplog):
    pid = insert_post(scheduled=FUTURE, description="Old secret words")
    other = insert_post(scheduled=FUTURE)

    async def scenario():
        async with board_client() as client:
            patched = await client.patch(f"/api/posts/{pid}", json={"caption": "Brand new secret words"},
                                         headers=WRITE)
            assert patched.status == 200
            assert (await client.post("/api/caption/preview", json={"caption": "Preview secret"},
                                      headers=WRITE)).status == 200
            assert (await client.delete(f"/api/posts/{pid}", headers=WRITE)).status == 200
            bulk = await client.post("/api/posts/bulk", json={"action": "delete", "ids": [other, 999999]},
                                     headers=WRITE)
            assert bulk.status == 200

    with caplog.at_level(logging.INFO, logger="bot.web.api"):
        asyncio.run(scenario())
    api_lines = [record.getMessage() for record in caplog.records if record.name == "bot.web.api"]
    assert all(record.levelno == logging.INFO for record in caplog.records if record.name == "bot.web.api")
    assert f"user 1 edit post {pid}" in api_lines[0]
    assert any(f"user 1 delete post {pid}" in line for line in api_lines)
    assert any(f"user 1 bulk delete posts [{other}, 999999]" in line for line in api_lines)
    assert "secret" not in caplog.text

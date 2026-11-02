"""Web board media helpers: path safety, thumbnails and uploads."""

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image

import config
from config import DATA_DIR, THUMBS_DIR, UPLOADS_DIR
from bot.web import media
from bot.web.errors import WebError
from bot.web.media import (
    MAX_UPLOAD_BYTES,
    PHOTO_MAX_BYTES,
    StoredUpload,
    get_thumbnail,
    is_album,
    media_items,
    purge_old_thumbnails,
    resolve_media_path,
    store_upload,
)

USER_ID = 4242
UUID_NAME = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


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


def _make_image(path, size=(2000, 1000), fmt="PNG"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", size, (200, 30, 30)).save(path, fmt)
    return path


def _upload_path(name="a.png", user_id=USER_ID):
    return os.path.join(UPLOADS_DIR, "2026-11", "01", str(user_id), "photo", name)


def _files_under(folder):
    return [os.path.join(root, f) for root, _dirs, files in os.walk(folder) for f in files]


async def _chunks(data: bytes, size: int = 8192):
    for start in range(0, len(data), size):
        yield data[start:start + size]


def _store(filename, data, **kwargs):
    return asyncio.run(store_upload(USER_ID, filename, _chunks(data), **kwargs))


# ---------------------------------------------------------------- constants


def test_constants():
    assert PHOTO_MAX_BYTES == 10 * 1024 * 1024
    assert MAX_UPLOAD_BYTES == 50 * 1024 * 1024


# ------------------------------------------------------- resolve_media_path


def test_resolve_media_path(tmp_path):
    inside = _make_image(_upload_path("inside.png"), size=(10, 10))
    assert resolve_media_path(inside) == os.path.realpath(inside)

    # A path that climbs out with ../ and lands on a real file outside uploads.
    outside = os.path.join(DATA_DIR, "secret.txt")
    with open(outside, "w") as f:
        f.write("secret")
    try:
        assert resolve_media_path(os.path.join(UPLOADS_DIR, "..", "secret.txt")) is None
        assert resolve_media_path(os.path.join(os.path.dirname(inside), "..", "..", "..", "..", "..", "secret.txt")) is None
    finally:
        os.remove(outside)

    # A symlink inside uploads that points outside is refused.
    target = tmp_path / "elsewhere.png"
    Image.new("RGB", (4, 4)).save(target)
    link = os.path.join(UPLOADS_DIR, "link.png")
    os.symlink(target, link)
    assert resolve_media_path(link) is None

    # A symlink that stays inside uploads resolves to its real target.
    inner_link = os.path.join(UPLOADS_DIR, "inner.png")
    os.symlink(inside, inner_link)
    assert resolve_media_path(inner_link) == os.path.realpath(inside)

    assert resolve_media_path(_upload_path("missing.png")) is None
    assert resolve_media_path(None) is None
    assert resolve_media_path("") is None


def test_resolve_media_path_edge_cases():
    inside = _make_image(_upload_path("inside.png"), size=(10, 10))

    # A directory is not a servable file.
    assert resolve_media_path(os.path.dirname(inside)) is None
    assert resolve_media_path(UPLOADS_DIR) is None

    # A sibling directory sharing the uploads name as a prefix is outside.
    evil_dir = UPLOADS_DIR + "_evil"
    os.makedirs(evil_dir, exist_ok=True)
    try:
        evil = _make_image(os.path.join(evil_dir, "x.png"), size=(4, 4))
        assert resolve_media_path(evil) is None
    finally:
        shutil.rmtree(evil_dir, ignore_errors=True)

    # Garbage that is not a usable path.
    assert resolve_media_path("bad\x00name.png") is None
    assert resolve_media_path(12345) is None


def test_resolve_media_path_legacy_relative():
    """Legacy rows store 'uploads/...' relative to DATA_DIR (see scheduler.py)."""
    inside = _make_image(_upload_path("legacy.png"), size=(10, 10))
    relative = os.path.relpath(inside, DATA_DIR)
    assert not os.path.isabs(relative)
    assert resolve_media_path(relative) == os.path.realpath(inside)

    # Relative paths never resolve against the working directory.
    assert resolve_media_path(os.path.join("..", "etc", "passwd")) is None
    assert resolve_media_path("../uploads/../../etc/passwd") is None


# -------------------------------------------------------------- media_items


def test_media_items():
    single = {"file_path": "/u/a.jpg", "media_type": "video", "media_bundle_json": None}
    assert media_items(single) == [{"index": 0, "file_path": "/u/a.jpg", "media_type": "video"}]
    assert media_items({"file_path": "/u/a.jpg"})[0]["media_type"] == "photo"
    assert media_items({"file_path": "/u/a.jpg", "media_type": None})[0]["media_type"] == "photo"
    assert media_items({"file_path": None, "media_type": "photo"}) == []
    assert media_items({"file_path": "", "media_type": "photo"}) == []

    bundle = [
        {"file_path": "/u/a.jpg", "media_type": "photo"},
        {"file_path": "/u/b.mp4"},  # type missing: derived from the extension
        {"file_path": "/u/c.png", "media_type": "document_image"},
    ]
    album = {"file_path": "/u/a.jpg", "media_type": "album", "media_bundle_json": json.dumps(bundle)}
    assert media_items(album) == [
        {"index": 0, "file_path": "/u/a.jpg", "media_type": "photo"},
        {"index": 1, "file_path": "/u/b.mp4", "media_type": "video"},
        {"index": 2, "file_path": "/u/c.png", "media_type": "document_image"},
    ]


def test_media_items_album_skips_entries_without_file_path():
    bundle = [
        {"file_path": "/u/a.jpg", "media_type": "photo"},
        {"media_type": "photo"},
        {"file_path": ""},
        None,
        "junk",
        {"file_path": "/u/c.jpg"},
    ]
    album = {"file_path": "/u/a.jpg", "media_type": "album", "media_bundle_json": json.dumps(bundle)}
    items = media_items(album)
    # The index is the position in the returned list, so items[i] is item i.
    assert items == [
        {"index": 0, "file_path": "/u/a.jpg", "media_type": "photo"},
        {"index": 1, "file_path": "/u/c.jpg", "media_type": "photo"},
    ]


def test_media_items_album_with_unusable_bundle():
    for raw in ("not json", "{}", "42", "[]"):
        album = {"file_path": "/u/a.jpg", "media_type": "album", "media_bundle_json": raw}
        assert media_items(album) == []

    # A bundle on a non-album post is ignored.
    photo = {"file_path": "/u/a.jpg", "media_type": "photo",
             "media_bundle_json": json.dumps([{"file_path": "/u/z.jpg"}])}
    assert media_items(photo) == [{"index": 0, "file_path": "/u/a.jpg", "media_type": "photo"}]


def test_is_album():
    bundle = json.dumps([{"file_path": "/u/a.jpg"}])
    assert is_album({"media_type": "album", "media_bundle_json": bundle}) is True
    assert is_album({"media_type": "album", "media_bundle_json": None}) is False
    assert is_album({"media_type": "album", "media_bundle_json": ""}) is False
    assert is_album({"media_type": "album"}) is False
    assert is_album({"media_type": "photo", "media_bundle_json": bundle}) is False
    assert is_album({}) is False


# --------------------------------------------------------------- thumbnails


def test_thumbnail_generated_and_cached():
    source = _make_image(_upload_path("big.png"), size=(2000, 1000))

    thumb = get_thumbnail(source, 160)
    assert thumb is not None
    assert os.path.dirname(thumb) == THUMBS_DIR
    assert thumb.endswith(".webp")
    with Image.open(thumb) as img:
        assert img.format == "WEBP"
        assert img.width <= 160
        assert img.height <= 160

    # Pin the cached file to an old mtime: a rewrite would move it.
    old = time.time() - 3600
    os.utime(thumb, (old, old))
    stamp = os.stat(thumb).st_mtime_ns
    assert get_thumbnail(source, 160) == thumb
    assert os.stat(thumb).st_mtime_ns == stamp
    assert os.listdir(THUMBS_DIR) == [os.path.basename(thumb)]  # no temp files left


def test_thumbnail_width_clamped_and_part_of_key():
    source = _make_image(_upload_path("big.png"), size=(2000, 1000))

    tiny = get_thumbnail(source, 1)
    huge = get_thumbnail(source, 100000)
    default = get_thumbnail(source)
    assert len({tiny, huge, default}) == 3
    with Image.open(tiny) as img:
        assert img.width == 32
    with Image.open(huge) as img:
        assert img.width == 512
    with Image.open(default) as img:
        assert img.width == 160

    # Different requested widths that clamp to the same size share a file.
    assert get_thumbnail(source, 0) == tiny
    assert get_thumbnail(source, 100000) == huge


def test_thumbnail_key_changes_when_source_changes():
    source = _make_image(_upload_path("pic.png"), size=(400, 400))
    first = get_thumbnail(source, 160)

    _make_image(source, size=(300, 300))
    later = time.time() + 5
    os.utime(source, (later, later))
    second = get_thumbnail(source, 160)
    assert second is not None and second != first


def test_thumbnail_handles_non_rgb_modes():
    path = _upload_path("palette.png")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("P", (300, 200)).save(path)
    thumb = get_thumbnail(path, 160)
    assert thumb is not None
    with Image.open(thumb) as img:
        assert img.format == "WEBP"

    gray = _upload_path("gray.png")
    Image.new("L", (300, 200), 128).save(gray)
    assert get_thumbnail(gray, 160) is not None

    rgba = _upload_path("alpha.png")
    Image.new("RGBA", (300, 200), (1, 2, 3, 4)).save(rgba)
    assert get_thumbnail(rgba, 160) is not None


def test_thumbnail_failures(tmp_path):
    assert get_thumbnail(_upload_path("missing.jpg")) is None
    assert get_thumbnail(None) is None

    corrupt = _upload_path("corrupt.jpg")
    os.makedirs(os.path.dirname(corrupt), exist_ok=True)
    with open(corrupt, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0 this is not a jpeg at all")
    assert get_thumbnail(corrupt) is None

    text = _upload_path("notes.txt")
    with open(text, "w") as f:
        f.write("just some text")
    assert get_thumbnail(text) is None

    # Truncated image data fails while decoding, not while opening.
    good = _make_image(_upload_path("whole.png"), size=(600, 600))
    with open(good, "rb") as f:
        data = f.read()
    truncated = _upload_path("cut.png")
    with open(truncated, "wb") as f:
        f.write(data[: len(data) // 2])
    assert get_thumbnail(truncated) is None

    # A readable image outside the uploads folder is never touched.
    outside = _make_image(str(tmp_path / "outside.png"), size=(50, 50))
    assert get_thumbnail(outside) is None

    # Nothing, not even a temp file, is left behind by the failures.
    assert os.listdir(THUMBS_DIR) == []


def _block_pillow(monkeypatch):
    """Make Pillow unimportable (import and find_spec both see it missing) and its warning due again."""
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setattr(media, "_pillow_warned", False)


def _pillow_warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_thumbnail_without_pillow_gives_none_and_warns_once(monkeypatch, caplog):
    path = _make_image(_upload_path("whole.png"), size=(600, 600))
    _block_pillow(monkeypatch)

    assert media.pillow_available() is False
    with caplog.at_level(logging.DEBUG):
        assert get_thumbnail(path) is None
        assert get_thumbnail(path) is None  # a second request: still no thumbnail, no second warning

    assert _pillow_warnings(caplog) == [media.PILLOW_MISSING_WARNING]
    assert "Pillow" in media.PILLOW_MISSING_WARNING and "pip install pillow" in media.PILLOW_MISSING_WARNING
    assert os.listdir(THUMBS_DIR) == []  # nothing left behind


def test_pillow_warning_is_shared_between_start_up_and_thumbnails(monkeypatch, caplog):
    path = _make_image(_upload_path("whole.png"), size=(600, 600))
    _block_pillow(monkeypatch)

    with caplog.at_level(logging.INFO):
        media.warn_pillow_missing()  # what start_web does
        assert get_thumbnail(path) is None

    assert _pillow_warnings(caplog) == [media.PILLOW_MISSING_WARNING]


def test_other_thumbnail_failures_are_not_blamed_on_pillow(monkeypatch, caplog):
    monkeypatch.setattr(media, "_pillow_warned", False)
    corrupt = _upload_path("corrupt.jpg")
    os.makedirs(os.path.dirname(corrupt), exist_ok=True)
    with open(corrupt, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0 this is not a jpeg at all")

    assert media.pillow_available() is True
    with caplog.at_level(logging.DEBUG):
        assert get_thumbnail(corrupt) is None
        assert get_thumbnail(_upload_path("missing.jpg")) is None

    assert _pillow_warnings(caplog) == []
    assert media._pillow_warned is False


def test_thumbnail_decompression_bomb(monkeypatch):
    """A warning-sized image (over the limit) and an error-sized one (over 2x) both give None."""
    filters_before = list(warnings.filters)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    warn_band = _make_image(_upload_path("warn.png"), size=(40, 40))     # 1600 px: warning
    error_band = _make_image(_upload_path("error.png"), size=(100, 100))  # 10000 px: error
    assert get_thumbnail(warn_band, 160) is None
    assert get_thumbnail(error_band, 160) is None
    assert os.listdir(THUMBS_DIR) == []

    # Within the limit the same image works, and no warnings filter leaks out.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10000)
    assert get_thumbnail(warn_band, 160) is not None
    assert warnings.filters == filters_before


def test_thumbnail_decoding_runs_at_most_two_at_once(monkeypatch):
    sources = [_make_image(_upload_path(f"p{i}.png"), size=(64, 64)) for i in range(6)]
    lock = threading.Lock()
    running, peak = [0], [0]
    write = media._write_thumbnail

    def slow_write(real_path, width, temp):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        try:
            time.sleep(0.05)  # long enough for every thread to be waiting at once
            write(real_path, width, temp)
        finally:
            with lock:
                running[0] -= 1

    monkeypatch.setattr(media, "_write_thumbnail", slow_write)
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        thumbs = list(pool.map(get_thumbnail, sources))

    assert all(thumbs)
    assert 1 <= peak[0] <= 2, f"{peak[0]} decodes ran at once"
    assert media.THUMB_DECODE_LIMIT == 2


class _CountingSlot:
    """Stands in for the decode semaphore and counts the slots taken."""

    def __init__(self):
        self.taken = 0

    def __enter__(self):
        self.taken += 1

    def __exit__(self, *exc):
        return False


def test_cache_hits_path_checks_and_missing_pillow_take_no_decode_slot(monkeypatch, tmp_path):
    source = _make_image(_upload_path("pic.png"), size=(300, 300))
    outside = _make_image(str(tmp_path / "outside.png"), size=(50, 50))
    not_cached = _make_image(_upload_path("new.png"), size=(60, 60))
    slot = _CountingSlot()
    monkeypatch.setattr(media, "_decode_slots", slot)
    cached = get_thumbnail(source, 160)
    assert cached is not None and slot.taken == 1  # a real decode takes one

    assert get_thumbnail(source, 160) == cached
    assert get_thumbnail(_upload_path("missing.png")) is None
    assert get_thumbnail(outside) is None
    _block_pillow(monkeypatch)
    assert get_thumbnail(not_cached) is None
    assert slot.taken == 1


def test_thumbnail_refuses_non_photo_formats():
    """Content is sniffed, not the extension: only JPEG, PNG and WebP are decoded."""
    path = _upload_path("sneaky.png")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n")
    with Image.open(path) as img:
        assert img.format == "EPS"  # Pillow would happily open this (and call Ghostscript)
    assert get_thumbnail(path, 160) is None

    bmp = _make_image(_upload_path("picture.jpg"), size=(30, 30), fmt="BMP")
    assert get_thumbnail(bmp, 160) is None

    jpeg_as_png = _make_image(_upload_path("really_jpeg.png"), size=(30, 30), fmt="JPEG")
    assert get_thumbnail(jpeg_as_png, 160) is not None


# ------------------------------------------------------------ store_upload


@pytest.mark.parametrize("filename, kwargs, expected_type, expected_ext", [
    ("holiday.jpg", {}, "photo", ".jpg"),
    ("Clip.MP4", {}, "video", ".mp4"),
    ("data.bin", {}, "document", ".bin"),
    ("holiday.jpg", {"as_file": True}, "document", ".jpg"),
])
def test_store_upload_types(filename, kwargs, expected_type, expected_ext):
    result = _store(filename, b"x" * 100, **kwargs)

    assert isinstance(result, StoredUpload)
    assert result.media_type == expected_type
    assert result.size == 100
    assert result.downgraded is False
    assert os.path.isfile(result.file_path)
    assert os.path.getsize(result.file_path) == 100

    # uploads/YYYY-MM/DD/<user_id>/<type>/<uuid><ext>
    parts = os.path.relpath(result.file_path, UPLOADS_DIR).split(os.sep)
    assert len(parts) == 5
    assert re.fullmatch(r"\d{4}-\d{2}", parts[0])
    assert re.fullmatch(r"\d{2}", parts[1])
    assert parts[2] == str(USER_ID)
    assert parts[3] == expected_type
    stem, ext = os.path.splitext(parts[4])
    assert UUID_NAME.fullmatch(stem)
    assert ext == expected_ext


def test_store_upload_streams_in_small_chunks():
    data = os.urandom(300 * 1024 + 17)
    result = asyncio.run(store_upload(USER_ID, "big.bin", _chunks(data, size=1000)))
    assert result.size == len(data)
    with open(result.file_path, "rb") as f:
        assert f.read() == data


def test_store_upload_photo_downgrade(monkeypatch):
    monkeypatch.setattr(media, "PHOTO_MAX_BYTES", 1024)

    result = _store("big.jpg", b"j" * 2048)
    assert result.media_type == "document"
    assert result.downgraded is True
    assert result.size == 2048
    assert os.path.isfile(result.file_path)
    assert os.path.basename(os.path.dirname(result.file_path)) == "document"
    assert os.path.getsize(result.file_path) == 2048
    # Moved, not copied: nothing is left in the photo folder.
    leftovers = [p for p in _files_under(UPLOADS_DIR) if os.sep + "photo" + os.sep in p]
    assert leftovers == []
    assert _files_under(UPLOADS_DIR) == [result.file_path]

    # Exactly at the limit is still a photo; videos are never downgraded.
    at_limit = _store("edge.jpg", b"j" * 1024)
    assert at_limit.media_type == "photo" and at_limit.downgraded is False
    video = _store("clip.mp4", b"v" * 4096)
    assert video.media_type == "video" and video.downgraded is False
    as_file = _store("chosen.jpg", b"j" * 4096, as_file=True)
    assert as_file.media_type == "document" and as_file.downgraded is False


def test_store_upload_limits(monkeypatch):
    with pytest.raises(WebError) as exc:
        _store("big.bin", b"z" * 20, max_bytes=10)
    assert exc.value.status == 413 and exc.value.code == "too_large"
    assert _files_under(UPLOADS_DIR) == []

    # Exactly max_bytes is accepted.
    assert _store("ok.bin", b"z" * 10, max_bytes=10).size == 10
    os.remove(_files_under(UPLOADS_DIR)[0])

    monkeypatch.setattr(config, "MAX_USER_STORAGE_MB", 1)
    with pytest.raises(WebError) as exc:
        _store("huge.bin", b"q" * (2 * 1024 * 1024))
    assert exc.value.status == 413 and exc.value.code == "too_large"
    assert "quota" in exc.value.message.lower()
    assert _files_under(UPLOADS_DIR) == []

    with pytest.raises(WebError) as exc:
        _store("empty.jpg", b"")
    assert exc.value.status == 400 and exc.value.code == "invalid_request"
    assert _files_under(UPLOADS_DIR) == []


def test_store_upload_quota_counts_new_file_once(monkeypatch):
    """The streamed file is already on disk when the quota is checked; it must not count twice."""
    monkeypatch.setattr(config, "MAX_USER_STORAGE_MB", 1)
    first = _store("a.bin", b"a" * 600 * 1024)  # 0.59 MB of a 1 MB quota
    assert os.path.isfile(first.file_path)

    with pytest.raises(WebError) as exc:
        _store("b.bin", b"b" * 600 * 1024)  # would reach 1.17 MB
    assert exc.value.code == "too_large"
    assert _files_under(UPLOADS_DIR) == [first.file_path]


def test_store_upload_removes_partial_file_on_stream_error():
    async def failing():
        yield b"a" * 5000
        raise ConnectionResetError("client went away")

    with pytest.raises(ConnectionResetError):
        asyncio.run(store_upload(USER_ID, "x.jpg", failing()))
    assert _files_under(UPLOADS_DIR) == []


def test_store_upload_removes_partial_file_on_cancel():
    async def stalls():
        yield b"a" * 5000
        await asyncio.sleep(30)
        yield b"never"

    async def run():
        await asyncio.wait_for(store_upload(USER_ID, "x.jpg", stalls()), timeout=0.3)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run())
    assert _files_under(UPLOADS_DIR) == []


@pytest.mark.parametrize("hostile", [
    "../../etc/passwd.jpg",
    "x.j/p g",
    "..\\..\\evil.JPG",
    "a.php\x00.jpg",
    "x.verylongextension",
    "trailing.",
    "",
])
def test_store_upload_hostile_filename(hostile):
    result = _store(hostile, b"data")

    real = os.path.realpath(result.file_path)
    assert real.startswith(os.path.realpath(UPLOADS_DIR) + os.sep)
    assert os.path.isfile(real)
    name = os.path.basename(result.file_path)
    stem, ext = os.path.splitext(name)
    assert UUID_NAME.fullmatch(stem)
    assert re.fullmatch(r"[.a-z0-9]{0,10}", ext)
    assert not os.path.exists(os.path.join(UPLOADS_DIR, "etc"))


def test_store_upload_hostile_filename_keeps_a_clean_extension():
    assert _store("../../etc/passwd.jpg", b"d").media_type == "photo"
    assert os.path.splitext(_store("..\\..\\evil.JPG", b"d").file_path)[1] == ".jpg"
    assert _store("x.j/p g", b"d").media_type == "document"
    assert _store("x.verylongextension", b"d").media_type == "document"


# ----------------------------------------------------- purge_old_thumbnails


def test_purge_old_thumbnails():
    old = os.path.join(THUMBS_DIR, "old.webp")
    middle = os.path.join(THUMBS_DIR, "middle.webp")
    fresh = os.path.join(THUMBS_DIR, "fresh.webp")
    for path in (old, middle, fresh):
        with open(path, "wb") as f:
            f.write(b"RIFF")
    now = time.time()
    os.utime(old, (now - 15 * 86400, now - 15 * 86400))
    os.utime(middle, (now - 13 * 86400, now - 13 * 86400))

    assert purge_old_thumbnails() == 1
    assert not os.path.exists(old)
    assert os.path.exists(middle) and os.path.exists(fresh)

    assert purge_old_thumbnails(max_age_days=7) == 1
    assert not os.path.exists(middle) and os.path.exists(fresh)
    assert purge_old_thumbnails() == 0


def test_purge_old_thumbnails_only_removes_plain_files(tmp_path):
    keep_target = tmp_path / "precious.txt"
    keep_target.write_text("keep me")
    link = os.path.join(THUMBS_DIR, "old-link.webp")
    os.symlink(keep_target, link)
    subdir = os.path.join(THUMBS_DIR, "nested")
    os.makedirs(subdir)
    old = time.time() - 30 * 86400
    os.utime(subdir, (old, old))
    os.utime(link, (old, old), follow_symlinks=False)
    os.utime(keep_target, (old, old))

    assert purge_old_thumbnails() == 0
    assert keep_target.exists()
    assert os.path.islink(link)
    assert os.path.isdir(subdir)


def test_purge_old_thumbnails_missing_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "THUMBS_DIR", str(tmp_path / "does-not-exist"))
    assert purge_old_thumbnails() == 0

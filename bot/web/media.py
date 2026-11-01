"""Media helpers for the web board: which files may be served, thumbnails, uploads.

This module decides which files on disk the web layer may serve and where an
uploaded file may be written, so it is deliberately strict:

* a stored path is served only when its real path (symlinks resolved) is a
  regular file inside the real uploads folder;
* thumbnails decode only JPEG, PNG and WebP (Pillow sniffs the content, not the
  extension) and refuse decompression bombs;
* an upload's stored name is a fresh uuid plus a whitelisted extension, so no
  part of a client-supplied file name ever reaches the file system.

Everything here blocks (disk, Pillow). Callers run `get_thumbnail`,
`resolve_media_path` and `purge_old_thumbnails` through `asyncio.to_thread`;
`store_upload` is a coroutine and moves its own disk work into threads.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
import warnings
from collections.abc import AsyncIterator
from dataclasses import dataclass

from PIL import Image

from bot.utils import (
    check_storage_quota,
    generate_unique_filename,
    get_media_type_from_extension,
    get_organized_media_path,
)
from bot.web.errors import invalid_request, too_large
from config import DATA_DIR, THUMBS_DIR, UPLOADS_DIR

logger = logging.getLogger(__name__)

# Telegram rejects photos above 10 MB; a larger one is stored as a document.
PHOTO_MAX_BYTES = 10 * 1024 * 1024
# Hard cap on one upload.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

THUMB_MIN_WIDTH = 32
THUMB_MAX_WIDTH = 512
# The only formats decoded for thumbnails. Pillow identifies a file by its
# content, so without this a file named x.png could be an EPS (rendered through
# Ghostscript) or any other exotic format.
_THUMB_FORMATS = ("JPEG", "PNG", "WEBP")

# warnings.catch_warnings() swaps the process-wide filter list and is not
# thread safe, and thumbnails are generated on worker threads.
_OPEN_LOCK = threading.Lock()

# Written to disk in blocks this big: aiohttp hands out small chunks and every
# disk write costs a thread hop.
_FLUSH_BYTES = 256 * 1024

_EXTENSION = re.compile(r"\.[a-z0-9]{1,9}")


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ------------------------------------------------------------------ items


def is_album(row: dict) -> bool:
    """True for an album post that carries a bundle."""
    return row.get("media_type") == "album" and bool(row.get("media_bundle_json"))


def _bundle_entries(row: dict) -> list:
    try:
        bundle = json.loads(row["media_bundle_json"])
    except (ValueError, TypeError):
        return []
    return bundle if isinstance(bundle, list) else []


def media_items(row: dict) -> list[dict]:
    """The media of a post as `{"index", "file_path", "media_type"}` items.

    `index` is the position in the returned list, so `items[i]` is item `i`
    even when bundle entries without a file path were skipped.
    """
    if is_album(row):
        items = []
        for entry in _bundle_entries(row):
            file_path = entry.get("file_path") if isinstance(entry, dict) else None
            if not file_path or not isinstance(file_path, str):
                continue
            media_type = entry.get("media_type")
            if not media_type or not isinstance(media_type, str):
                media_type = get_media_type_from_extension(file_path)
            items.append({"index": len(items), "file_path": file_path, "media_type": media_type})
        return items

    file_path = row.get("file_path")
    if not file_path:
        return []
    return [{"index": 0, "file_path": file_path, "media_type": row.get("media_type") or "photo"}]


# ------------------------------------------------------------------- paths


def resolve_media_path(file_path: str | None) -> str | None:
    """The real path of a stored media file, or None when it must not be served.

    The file must exist as a regular file and its real path (every symlink
    resolved) must lie inside the real uploads folder. Legacy rows store paths
    relative to DATA_DIR (the scheduler resolves them the same way); a relative
    path is never resolved against the working directory.
    """
    if not isinstance(file_path, str) or not file_path:
        return None
    candidate = file_path if os.path.isabs(file_path) else os.path.join(DATA_DIR, file_path)
    try:
        real = os.path.realpath(candidate)
        base = os.path.realpath(UPLOADS_DIR)
        if os.path.commonpath([real, base]) != base:
            return None
        if not os.path.isfile(real):
            return None
    except (OSError, ValueError):  # NUL byte, unreadable component, mixed drives
        return None
    return real


# -------------------------------------------------------------- thumbnails


def _open_image(real_path: str) -> Image.Image:
    """Open an image header; a decompression-bomb warning becomes an error.

    Image.open() is where Pillow checks the pixel count (a warning past
    MAX_IMAGE_PIXELS, DecompressionBombError past twice that), before any pixel
    is decoded.
    """
    with _OPEN_LOCK, warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        return Image.open(real_path, formats=_THUMB_FORMATS)


def get_thumbnail(file_path: str, width: int = 160) -> str | None:
    """Path of a cached WebP thumbnail for a stored image, or None.

    None means "no thumbnail": a missing or foreign file, something that is not
    a JPEG/PNG/WebP, a corrupt or truncated image, or a decompression bomb.
    """
    real = resolve_media_path(file_path)
    if real is None:
        return None
    width = max(THUMB_MIN_WIDTH, min(THUMB_MAX_WIDTH, int(width)))

    try:
        mtime_ns = os.stat(real).st_mtime_ns
    except OSError:
        return None
    key = hashlib.sha1(
        f"{real}\0{mtime_ns}\0{width}".encode("utf-8", "surrogateescape"), usedforsecurity=False
    ).hexdigest()
    cached = os.path.join(THUMBS_DIR, f"{key}.webp")
    if os.path.isfile(cached):
        return cached

    os.makedirs(THUMBS_DIR, exist_ok=True)
    temp = os.path.join(THUMBS_DIR, f"{key}.{uuid.uuid4().hex}.tmp")
    try:
        source = _open_image(real)
        try:
            source.thumbnail((width, width), Image.Resampling.LANCZOS)
            thumb = source
            if source.mode not in ("RGB", "RGBA"):
                has_alpha = source.mode in ("LA", "PA") or "transparency" in source.info
                thumb = source.convert("RGBA" if has_alpha else "RGB")
            thumb.save(temp, "WEBP", quality=80)
        finally:
            source.close()
        os.replace(temp, cached)
    except Exception as exc:  # corrupt data raises many types; any failure means "no thumbnail"
        logger.debug("No thumbnail for %s: %s: %s", real, type(exc).__name__, exc)
        _remove_quietly(temp)
        return None
    return cached


def purge_old_thumbnails(max_age_days: int = 14) -> int:
    """Delete cached thumbnails not modified for `max_age_days`; return how many."""
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    try:
        with os.scandir(THUMBS_DIR) as entries:
            for entry in entries:
                try:
                    # Plain files only: a symlink or folder is never followed or removed.
                    if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.remove(entry.path)
                        removed += 1
                except OSError:
                    continue
    except FileNotFoundError:
        return 0
    return removed


# ----------------------------------------------------------------- uploads


@dataclass
class StoredUpload:
    file_path: str
    media_type: str
    size: int
    downgraded: bool  # a photo too big for Telegram was stored as a document


def _safe_extension(filename) -> str:
    """The extension of a client file name, or "" unless it is plain `.[a-z0-9]{1,9}`."""
    name = os.path.basename(str(filename or ""))
    extension = os.path.splitext(name)[1].lower()
    return extension if _EXTENSION.fullmatch(extension) else ""


def _format_limit(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):g} MB"
    return f"{num_bytes} bytes"


def _finish_upload(handle, tail, path: str, user_id: int, downgrade: bool) -> str:
    """Flush and close the stream, enforce the quota, apply a photo downgrade."""
    if tail:
        handle.write(tail)
    handle.close()
    try:
        # The new file is already on disk and counted in the user's usage, so
        # nothing more is incoming; passing its size again would count it twice.
        check_storage_quota(user_id, 0)
    except ValueError as exc:
        raise too_large(str(exc)) from exc
    if not downgrade:
        return path
    folder, _relative = get_organized_media_path(user_id, "document")
    moved = os.path.join(folder, os.path.basename(path))
    os.replace(path, moved)
    return moved


async def store_upload(
    user_id: int,
    filename: str,
    chunks: AsyncIterator[bytes],
    as_file: bool = False,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> StoredUpload:
    """Stream an upload into the user's media folder.

    The type comes from the file name's extension (`as_file` forces `document`).
    More than `max_bytes` raises `too_large` (413), an empty upload
    `invalid_request` (400), exceeding the storage quota `too_large` with the
    quota message. The partial file is removed on any error, including
    cancellation when the client disconnects.
    """
    user_id = int(user_id)
    stored_name = generate_unique_filename("upload" + _safe_extension(filename))
    media_type = "document" if as_file else get_media_type_from_extension(stored_name)

    folder, _relative = await asyncio.to_thread(get_organized_media_path, user_id, media_type)
    path = os.path.join(folder, stored_name)

    handle = None
    try:
        handle = await asyncio.to_thread(open, path, "wb")
        size = 0
        pending = bytearray()
        async for chunk in chunks:
            size += len(chunk)
            if size > max_bytes:
                raise too_large(f"Upload is larger than the {_format_limit(max_bytes)} limit.")
            pending += chunk
            if len(pending) >= _FLUSH_BYTES:
                await asyncio.to_thread(handle.write, pending)
                pending.clear()
        if size == 0:
            raise invalid_request("The uploaded file is empty.")

        downgrade = media_type == "photo" and size > PHOTO_MAX_BYTES
        final_path = await asyncio.to_thread(_finish_upload, handle, bytes(pending), path, user_id, downgrade)
    except BaseException:
        # Includes CancelledError. Plain blocking calls on purpose: a second
        # cancellation must not be able to interrupt the cleanup.
        if handle is not None:
            try:
                handle.close()
            except (OSError, ValueError):
                pass
        _remove_quietly(path)
        raise

    if downgrade:
        media_type = "document"
    return StoredUpload(file_path=final_path, media_type=media_type, size=size, downgraded=downgrade)

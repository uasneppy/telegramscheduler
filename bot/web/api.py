"""HTTP routes for the board's posts (spec 7): thin handlers over queries, service, media and captions.

Each handler takes the user id from the session (`request["user_id"]`; the
session middleware answers 401 on /api without one), parses and validates the
request, calls the module that owns the rule and serializes the result. The
server's middlewares already enforce sign-in, `Origin` and `X-Board`, and turn
WebError into JSON. Blocking work (SQLite, the disk, Pillow, caption parsing)
runs through asyncio.to_thread; the service's scheduler-touching functions are
coroutines and run on the loop.

Media is served by post id and item index only, from the real path that
media.resolve_media_path returns, never from the stored value. Only a fixed
set of image, video and audio types is shown inline; anything else is an
attachment, and every media response carries `Content-Security-Policy:
sandbox`, so an uploaded HTML or SVG file never runs script on the board's
origin. web.FileResponse provides range requests.
"""

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator

from aiohttp import BodyPartReader, web

from bot.database import Database
from bot.web import captions, media, queries, service
from bot.web.errors import invalid_request, not_found

logger = logging.getLogger(__name__)

# ASCII digits only (a Unicode digit would pass int()), and short enough for int().
_POST = "/api/posts/{id:[0-9]{1,20}}"
# A query integer: no sign other than "-", no spaces or underscores (int() takes both).
_INTEGER = re.compile(r"-?[0-9]{1,18}")
_EXTENSION = re.compile(r"\.[a-z0-9]{1,9}")

_SANDBOX = "sandbox"
# The only types shown inline, by the served file's extension; anything else is an attachment.
_INLINE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".m4a": "audio/mp4",
}
_ATTACHMENT_TYPE = "application/octet-stream"
_THUMB_WIDTH = 160
_THUMB_CACHE = "private, max-age=3600"

_READ_CHUNK = 64 * 1024
# The as_file field holds a word, never more.
_FLAG_MAX_BYTES = 16
_FLAG_TRUE = ("1", "true", "on")
_FLAG_FALSE = ("", "0", "false", "off")
_MALFORMED_UPLOAD = "The upload is malformed."


# --- request parsing ------------------------------------------------------------------


def _post_id(request: web.Request) -> int:
    return int(request.match_info["id"])


def _query_int(request: web.Request, name: str, default: int) -> int:
    raw = request.query.get(name)
    if raw is None:
        return default
    if not _INTEGER.fullmatch(raw):
        raise invalid_request(f"{name} must be an integer")
    return int(raw)


def _query_text(request: web.Request, name: str) -> str | None:
    """A query value; an empty one counts as absent."""
    return request.query.get(name) or None


async def _json_object(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, LookupError, RecursionError):
        # Not JSON, not decodable in its charset, or nested too deeply to parse.
        body = None
    if not isinstance(body, dict):
        raise invalid_request("The request body must be a JSON object.")
    return body


def _log(request: web.Request, action: str, post_id: int) -> None:
    logger.info("Web board API: user %s %s post %s", request["user_id"], action, post_id)


# --- reads ----------------------------------------------------------------------------


async def _list_posts(request: web.Request) -> web.Response:
    page = await asyncio.to_thread(
        queries.list_posts,
        request["user_id"],
        request.query.get("status"),
        channel_id=_query_text(request, "channel"),
        q=_query_text(request, "q"),
        limit=_query_int(request, "limit", queries.DEFAULT_LIMIT),
        cursor=_query_text(request, "cursor"),
    )
    return web.json_response(page)


async def _post_detail(request: web.Request) -> web.Response:
    detail = await asyncio.to_thread(queries.get_post_detail, request["user_id"], _post_id(request))
    return web.json_response(detail)


def _media_item(user_id: int, post_id: int, index: int) -> dict:
    """Item `index` of the user's post; not_found for a missing post or item. Blocking."""
    items = media.media_items(queries.get_owned_post(user_id, post_id))
    if not 0 <= index < len(items):
        raise not_found("Media not found")
    return items[index]


def _media_file(user_id: int, post_id: int, index: int) -> str:
    """The real path to serve for a media item; not_found when it is gone or not servable. Blocking."""
    real = media.resolve_media_path(_media_item(user_id, post_id, index)["file_path"])
    if real is None:
        raise not_found("Media file not found")
    return real


def _thumbnail_file(user_id: int, post_id: int, index: int, width: int) -> str:
    """The cached thumbnail of a media item; not_found when it has none. Blocking."""
    thumb = media.get_thumbnail(_media_item(user_id, post_id, index)["file_path"], width)
    if thumb is None:
        raise not_found("No thumbnail for this media")
    return thumb


async def _media(request: web.Request) -> web.StreamResponse:
    post_id = _post_id(request)
    index = _query_int(request, "i", 0)
    real = await asyncio.to_thread(_media_file, request["user_id"], post_id, index)
    extension = os.path.splitext(real)[1].lower()
    headers = {"Content-Security-Policy": _SANDBOX}
    content_type = _INLINE_TYPES.get(extension)
    if content_type is None:
        content_type = _ATTACHMENT_TYPE
        suffix = extension if _EXTENSION.fullmatch(extension) else ""
        headers["Content-Disposition"] = f'attachment; filename="post-{post_id}-{index}{suffix}"'
    headers["Content-Type"] = content_type
    return web.FileResponse(real, headers=headers)


async def _thumb(request: web.Request) -> web.StreamResponse:
    # No media_type gate: get_thumbnail decodes only real JPEG/PNG/WebP content.
    index = _query_int(request, "i", 0)
    width = _query_int(request, "w", _THUMB_WIDTH)
    thumb = await asyncio.to_thread(_thumbnail_file, request["user_id"], _post_id(request), index, width)
    headers = {"Content-Type": "image/webp", "Cache-Control": _THUMB_CACHE, "Content-Security-Policy": _SANDBOX}
    return web.FileResponse(thumb, headers=headers)


# --- single-post changes ----------------------------------------------------------------


async def _update_post(request: web.Request) -> web.Response:
    post_id = _post_id(request)
    changes = await _json_object(request)
    _log(request, "edit", post_id)
    detail = await service.update_post(request["user_id"], post_id, changes, request.app["scheduler"])
    return web.json_response(detail)


async def _delete_post(request: web.Request) -> web.Response:
    post_id = _post_id(request)
    _log(request, "delete", post_id)
    return web.json_response(await service.delete_post(request["user_id"], post_id, request.app["scheduler"]))


async def _restore_post(request: web.Request) -> web.Response:
    post_id = _post_id(request)
    _log(request, "restore", post_id)
    return web.json_response(await service.restore_post(request["user_id"], post_id, request.app["scheduler"]))


async def _post_now(request: web.Request) -> web.Response:
    post_id = _post_id(request)
    _log(request, "post now", post_id)
    return web.json_response(await service.post_now(request["user_id"], post_id, request.app["scheduler"]))


async def _retry_post(request: web.Request) -> web.Response:
    post_id = _post_id(request)
    _log(request, "retry", post_id)
    return web.json_response(await service.retry_post(request["user_id"], post_id, request.app["scheduler"]))


# --- media upload -------------------------------------------------------------------


def _is_field(part, name: str) -> bool:
    return isinstance(part, BodyPartReader) and part.name == name


async def _part_chunks(part: BodyPartReader) -> AsyncIterator[bytes]:
    """The raw bytes of one multipart part; a malformed part is invalid_request."""
    while not part.at_eof():
        try:
            chunk = await part.read_chunk(_READ_CHUNK)
        except ValueError:  # no closing boundary, data after the end, ...
            raise invalid_request(_MALFORMED_UPLOAD) from None
        if chunk:
            yield chunk


async def _read_flag(part: BodyPartReader) -> bool:
    refused = invalid_request("as_file must be 1 or 0.")
    value = bytearray()
    async for chunk in _part_chunks(part):
        value += chunk
        if len(value) > _FLAG_MAX_BYTES:
            raise refused
    word = value.decode("utf-8", "replace").strip().lower()
    if word not in _FLAG_TRUE + _FLAG_FALSE:
        raise refused
    return word in _FLAG_TRUE


async def _upload_part(request: web.Request) -> tuple[BodyPartReader, bool]:
    """The `file` part of a multipart upload and the `as_file` flag sent before it."""
    if request.content_type != "multipart/form-data":
        raise invalid_request("Send the file as multipart/form-data.")
    try:
        reader = await request.multipart()
        part = await reader.next()
        as_file = False
        if _is_field(part, "as_file"):
            as_file = await _read_flag(part)
            part = await reader.next()
    except (ValueError, RuntimeError):
        # aiohttp's multipart reader reports a missing or bad boundary as ValueError and
        # an over-long `_charset_` field as RuntimeError; both are the client's fault.
        raise invalid_request(_MALFORMED_UPLOAD) from None
    if not _is_field(part, "file"):
        raise invalid_request("The upload needs a file field (an as_file field goes before it).")
    return part, as_file


async def _replace_media(request: web.Request) -> web.Response:
    user_id, post_id = request["user_id"], _post_id(request)
    _log(request, "replace media", post_id)
    # Before the body is read: a foreign post, an album or a post that cannot take
    # new media is refused without an upload landing on disk.
    await asyncio.to_thread(service.check_media_replaceable, user_id, post_id)
    part, as_file = await _upload_part(request)
    # store_upload removes its partial file on any failure, including a client that goes away.
    stored = await media.store_upload(
        user_id, part.filename, _part_chunks(part), as_file=as_file, max_bytes=media.MAX_UPLOAD_BYTES
    )
    # Nothing runs between the two calls, and replace_media removes stored.file_path
    # itself on any failure it raises, so the route has no file of its own to clean up.
    detail = await service.replace_media(user_id, post_id, stored)
    return web.json_response(detail)


# --- bulk and preview ----------------------------------------------------------------


async def _bulk(request: web.Request) -> web.Response:
    body = await _json_object(request)
    # The service validates action, ids and shift (it receives them untouched).
    result = await service.bulk(
        request["user_id"],
        body.get("action"),
        body.get("ids"),
        request.app["scheduler"],
        channel_id=body.get("channel_id"),
        shift=body.get("shift"),
    )
    results = result["results"]
    # Logged once the action and ids are known to be well formed; they come from the client.
    logger.info(
        "Web board API: user %s bulk %s posts %s (%d not changed)",
        request["user_id"], body["action"], [item["id"] for item in results],
        sum(not item["ok"] for item in results),
    )
    return web.json_response(result)


def _preview_signature(user_id: int, channel_id) -> str | None:
    """The signature of a channel the caller owns, else None. Blocking."""
    if not isinstance(channel_id, str) or not Database.user_has_channel(user_id, channel_id):
        return None
    return Database.get_channel_signature(user_id, channel_id)


async def _caption_preview(request: web.Request) -> web.Response:
    # Always 200 for a JSON object: invalid tags come back as ok=false with an error.
    body = await _json_object(request)
    signature = await asyncio.to_thread(_preview_signature, request["user_id"], body.get("channel_id"))
    return web.json_response(await asyncio.to_thread(captions.preview, body.get("caption"), signature))


# --- registration --------------------------------------------------------------------


def register_routes(app: web.Application) -> None:
    """Add the post routes of spec section 7 to the board application."""
    router = app.router
    router.add_get("/api/posts", _list_posts)
    router.add_post("/api/posts/bulk", _bulk)
    router.add_get(_POST, _post_detail)
    router.add_patch(_POST, _update_post)
    router.add_delete(_POST, _delete_post)
    router.add_get(_POST + "/media", _media)
    router.add_put(_POST + "/media", _replace_media)
    router.add_get(_POST + "/thumb", _thumb)
    router.add_post(_POST + "/post-now", _post_now)
    router.add_post(_POST + "/retry", _retry_post)
    router.add_post(_POST + "/restore", _restore_post)
    router.add_post("/api/caption/preview", _caption_preview)

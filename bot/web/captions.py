"""Caption conversion between stored Telegram entities and editable tagged text.

Storage keeps Telegram's form: plain text plus a JSON list of entities
``{type, offset, length, url?, language?, custom_emoji_id?}`` whose offsets
and lengths are UTF-16 code units. The editor works on tagged text in
Telegram's HTML syntax, and the preview is HTML built only from whitelisted
tags. Pure module: no aiohttp, no database.
"""

import html
import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser

from bot.web.errors import WebError

CAPTION_LIMIT = 1024

_SIMPLE_TAGS = {
    "bold": "b",
    "italic": "i",
    "underline": "u",
    "strikethrough": "s",
    "code": "code",
    "spoiler": "tg-spoiler",
    "blockquote": "blockquote",
}
_TYPE_OF_SIMPLE_TAG = {tag: kind for kind, tag in _SIMPLE_TAGS.items()}
_KNOWN_TAGS = frozenset(_TYPE_OF_SIMPLE_TAG) | {"pre", "a", "tg-emoji"}
# Entity types written as tags. Every other type (url, mention, hashtag, ...)
# is detected by Telegram and only carried over from the old caption.
_TAG_TYPES = frozenset(_SIMPLE_TAGS) | {"pre", "text_link", "expandable_blockquote", "custom_emoji"}
_OPTIONAL_KEYS = ("url", "language", "custom_emoji_id")

# A bare & (not one of the references Telegram's HTML accepts) and a < that
# cannot start a tag are literal text, so they are escaped before parsing.
_BARE_AMP = re.compile(r"&(?!(?:lt|gt|amp|quot|#[0-9]+|#[xX][0-9a-fA-F]+);)")
_BARE_LT = re.compile(r"<(?!/?[a-zA-Z])")
_LANGUAGE_PREFIX = "language-"
_SAFE_HREF = re.compile(r"(?:https?|tg)://", re.IGNORECASE)


class CaptionError(WebError):
    """Invalid caption markup or an over-long caption (400 invalid_request)."""

    def __init__(self, message: str):
        super().__init__(400, "invalid_request", message)


# --- entities -----------------------------------------------------------------


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _clean_entity(item) -> dict | None:
    """The entity in its documented shape, or None when it is malformed."""
    if not isinstance(item, dict):
        return None
    kind, offset, length = item.get("type"), item.get("offset"), item.get("length")
    if not (isinstance(kind, str) and kind and _is_int(offset) and _is_int(length)):
        return None
    if offset < 0 or length <= 0:
        return None
    entity = {"type": kind, "offset": offset, "length": length}
    for key in _OPTIONAL_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value:
            entity[key] = value
    return entity


def parse_entities_json(raw: str | None) -> list[dict]:
    """Stored caption_entities JSON as a list of entities; bad data gives []."""
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    return [entity for entity in map(_clean_entity, items) if entity is not None]


def entities_to_json(entities: list[dict] | None) -> str | None:
    """Entities as caption_entities JSON; None when there are none."""
    return json.dumps(entities) if entities else None


def _utf16_offsets(text: str) -> list[int]:
    """The UTF-16 offset of every code-point index of text, plus its end."""
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + (2 if ord(char) > 0xFFFF else 1))
    return offsets


@dataclass
class _Span:
    start: int  # code-point index
    end: int
    entity: dict


def _spans(text: str, entities: list[dict] | None) -> list[_Span]:
    """Entities as code-point spans of text, skipping corrupt ones."""
    index_of = {offset: index for index, offset in enumerate(_utf16_offsets(text))}
    spans = []
    for item in entities or ():
        entity = _clean_entity(item)
        if entity is None:
            continue
        start = index_of.get(entity["offset"])
        end = index_of.get(entity["offset"] + entity["length"])
        if start is not None and end is not None:  # inside the text, not mid-character
            spans.append(_Span(start, end, entity))
    return spans


# --- entities -> tagged text / preview HTML ------------------------------------


def _emit(text: str, spans: list[_Span], tags_for, text_for) -> str:
    """Text with a tag pair around each span, always well nested.

    Where spans overlap, the inner tag is closed before the outer one ends and
    reopened after it: bold "abcde" with italic "defgh" gives
    ``<b>abc<i>de</i></b><i>fgh</i>``.
    """
    tagged = [(span, pair) for span in spans if (pair := tags_for(span.entity))]
    points = sorted({0, len(text)}.union(*((span.start, span.end) for span, _ in tagged)))
    out: list[str] = []
    stack: list[int] = []
    for point, next_point in zip(points, points[1:]):
        active = {i for i, (span, _) in enumerate(tagged) if span.start <= point < span.end}
        keep = 0
        while keep < len(stack) and stack[keep] in active:
            keep += 1
        out.extend(tagged[i][1][1] for i in reversed(stack[keep:]))
        del stack[keep:]
        # Spans that last longer open first (outside), so they split less.
        opening = sorted(active.difference(stack), key=lambda i: (-tagged[i][0].end, i))
        out.extend(tagged[i][1][0] for i in opening)
        stack.extend(opening)
        in_pre = any(tagged[i][0].entity["type"] == "pre" for i in stack)
        out.append(text_for(text[point:next_point], in_pre))
    out.extend(tagged[i][1][1] for i in reversed(stack))
    return "".join(out)


def _attr(value: str) -> str:
    return html.escape(value, quote=True)


def _tagged_pair(entity: dict) -> tuple[str, str] | None:
    kind = entity["type"]
    if kind in _SIMPLE_TAGS:
        tag = _SIMPLE_TAGS[kind]
        return f"<{tag}>", f"</{tag}>"
    if kind == "pre":
        if "language" in entity:
            return f'<pre><code class="{_LANGUAGE_PREFIX}{_attr(entity["language"])}">', "</code></pre>"
        return "<pre>", "</pre>"
    if kind == "expandable_blockquote":
        return "<blockquote expandable>", "</blockquote>"
    if kind == "text_link" and "url" in entity:
        return f'<a href="{_attr(entity["url"])}">', "</a>"
    if kind == "custom_emoji" and "custom_emoji_id" in entity:
        return f'<tg-emoji emoji-id="{_attr(entity["custom_emoji_id"])}">', "</tg-emoji>"
    return None


def _html_pair(entity: dict) -> tuple[str, str] | None:
    kind = entity["type"]
    if kind == "spoiler":
        return '<span class="tg-spoiler">', "</span>"
    if kind == "custom_emoji":
        # Browsers cannot draw Telegram custom emoji: mark the fallback emoji.
        return '<span class="tg-emoji" title="Custom emoji">', "</span>"
    if kind in _SIMPLE_TAGS:
        tag = _SIMPLE_TAGS[kind]
        return f"<{tag}>", f"</{tag}>"
    if kind == "pre":
        return "<pre>", "</pre>"
    if kind == "expandable_blockquote":
        return '<blockquote class="tg-expandable">', "</blockquote>"
    if kind == "text_link" and _SAFE_HREF.match(entity.get("url", "")):
        return f'<a href="{_attr(entity["url"])}" rel="noopener noreferrer" target="_blank">', "</a>"
    return None


def _tagged_text(segment: str, in_pre: bool) -> str:
    return html.escape(segment, quote=False)


def _html_text(segment: str, in_pre: bool) -> str:
    escaped = html.escape(segment, quote=False)
    return escaped if in_pre else escaped.replace("\n", "<br>")


def entities_to_tagged(text: str | None, entities: list[dict] | None) -> str:
    """Stored caption as tagged text for the editor."""
    if not text:
        return ""
    return _emit(text, _spans(text, entities), _tagged_pair, _tagged_text)


def render_html(text: str | None, entities: list[dict] | None) -> str:
    """Stored caption as sanitized preview HTML (whitelisted tags only)."""
    if not text:
        return ""
    return _emit(text, _spans(text, entities), _html_pair, _html_text)


# --- tagged text -> plain text + entities -------------------------------------


def _check_attributes(tag: str, attributes: dict, required: str | None = None, optional: tuple = ()) -> None:
    for name in attributes:
        if name != required and name not in optional:
            raise CaptionError(f"<{tag}> does not take the attribute {name!r}.")
    if required and not (attributes.get(required) or "").strip():
        raise CaptionError(f'<{tag}> needs a non-empty {required}="..." attribute.')


def _entity_for(tag: str, attributes: dict) -> dict:
    """The entity (without offset and length) that a start tag opens."""
    if tag == "blockquote":
        _check_attributes(tag, attributes, optional=("expandable",))
        return {"type": "expandable_blockquote" if "expandable" in attributes else "blockquote"}
    if tag in _TYPE_OF_SIMPLE_TAG:
        _check_attributes(tag, attributes)
        return {"type": _TYPE_OF_SIMPLE_TAG[tag]}
    if tag == "pre":
        _check_attributes(tag, attributes)
        return {"type": "pre"}
    if tag == "a":
        _check_attributes(tag, attributes, required="href")
        return {"type": "text_link", "url": attributes["href"].strip()}
    if tag == "tg-emoji":
        _check_attributes(tag, attributes, required="emoji-id")
        return {"type": "custom_emoji", "custom_emoji_id": attributes["emoji-id"].strip()}
    raise CaptionError(f"<{tag}> is not a supported tag.")


@dataclass
class _Open:
    tag: str
    start: int  # code-point index
    order: int
    entity: dict | None  # None for the <code> that carries a <pre> language


class _TaggedParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.position = 0
        self.stack: list[_Open] = []
        self.closed: list[tuple[int, int, int, dict]] = []  # (start, order, end, entity)
        self._opened = 0
        self._pre_opened_last = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if len(attributes) != len(attrs):
            raise CaptionError(f"<{tag}> repeats an attribute.")
        carries_language = tag == "code" and self._pre_opened_last
        self._pre_opened_last = tag == "pre"
        entity = None
        if carries_language:
            _check_attributes(tag, attributes, optional=("class",))
            css_class = attributes.get("class")
            if css_class is not None:
                language = css_class.removeprefix(_LANGUAGE_PREFIX)
                if language == css_class or not language:
                    raise CaptionError('A code block language is written <pre><code class="language-NAME">.')
                self.stack[-1].entity["language"] = language
        else:
            entity = _entity_for(tag, attributes)
        self.stack.append(_Open(tag, self.position, self._opened, entity))
        self._opened += 1

    def handle_startendtag(self, tag, attrs):
        if tag not in _KNOWN_TAGS:
            raise CaptionError(f"<{tag}> is not a supported tag.")
        raise CaptionError(f"<{tag}/> is empty; write <{tag}>text</{tag}>.")

    def handle_endtag(self, tag):
        self._pre_opened_last = False
        if tag not in _KNOWN_TAGS:
            raise CaptionError(f"</{tag}> is not a supported tag.")
        if not self.stack:
            raise CaptionError(f"</{tag}> has no opening tag.")
        opened = self.stack.pop()
        if opened.tag != tag:
            raise CaptionError(f"Tags are crossed or unbalanced: </{tag}> while <{opened.tag}> is open.")
        if opened.entity is not None and self.position > opened.start:
            self.closed.append((opened.start, opened.order, self.position, opened.entity))

    def handle_data(self, data):
        self._pre_opened_last = False
        self.chunks.append(data)
        self.position += len(data)

    def _reject_markup(self, data):
        # Unreachable while _BARE_LT escapes "<!" and "<?"; kept so that markup
        # is never dropped silently.
        raise CaptionError("Comments and declarations are not supported.")

    handle_comment = handle_decl = handle_pi = unknown_decl = _reject_markup


def _parse_tagged(tagged: str) -> tuple[str | None, list[dict]]:
    """Plain text (None when blank) and tag entities of tagged text."""
    if not isinstance(tagged, str):
        raise CaptionError("The caption must be a string.")
    parser = _TaggedParser()
    parser.feed(_BARE_LT.sub("&lt;", _BARE_AMP.sub("&amp;", tagged)))
    if parser.rawdata:
        # html.parser holds back an unterminated tag, and close() would drop it.
        raise CaptionError("A tag is missing its closing '>'. Write &lt; for a literal '<'.")
    parser.close()
    if parser.stack:
        raise CaptionError(f"<{parser.stack[-1].tag}> is not closed.")
    text = "".join(parser.chunks)
    if not text.strip():
        return None, []
    offsets = _utf16_offsets(text)
    entities = []
    for start, _order, end, opened in sorted(parser.closed, key=lambda item: item[:2]):
        entity = {"type": opened["type"], "offset": offsets[start], "length": offsets[end] - offsets[start]}
        entity.update((key, value) for key, value in opened.items() if key != "type")
        entities.append(entity)
    return text, entities


def _too_long(length: int) -> str | None:
    if length <= CAPTION_LIMIT:
        return None
    return f"The caption is {length} characters long; the limit is {CAPTION_LIMIT}."


def _carried_entities(old_text: str | None, old_entities: list[dict] | None, text: str) -> list[dict]:
    """Old entities without a tag whose exact text span survived the edit, moved to the new text."""
    if not old_text or not old_entities:
        return []
    spans = [span for span in _spans(old_text, old_entities) if span.entity["type"] not in _TAG_TYPES]
    if not spans:
        return []
    blocks = SequenceMatcher(None, old_text, text, autojunk=False).get_matching_blocks()
    offsets = _utf16_offsets(text)
    carried = []
    for span in spans:
        for old_start, new_start, size in blocks:
            if old_start <= span.start and span.end <= old_start + size:
                start = span.start - old_start + new_start
                end = span.end - old_start + new_start
                carried.append(dict(span.entity, offset=offsets[start], length=offsets[end] - offsets[start]))
                break
    return carried


def tagged_to_caption(
    tagged: str, old_text: str | None = None, old_entities: list[dict] | None = None
) -> tuple[str | None, list[dict] | None]:
    """Tagged text as (plain text, entities) for storage; (None, None) when blank.

    Raises CaptionError for unknown tags, bad nesting, unclosed tags or a
    caption over CAPTION_LIMIT characters.
    """
    text, entities = _parse_tagged(tagged)
    if text is None:
        return None, None
    error = _too_long(len(text))
    if error:
        raise CaptionError(error)
    entities += _carried_entities(old_text, old_entities, text)
    entities.sort(key=lambda entity: entity["offset"])
    return text, entities or None


def signature_fits(text: str | None, signature: str | None) -> bool | None:
    """Whether the publisher will append the signature (same rule as _post_to_channel)."""
    if not signature:
        return None
    combined = f"{text}\n\n{signature}" if text else signature
    return len(combined) <= CAPTION_LIMIT


def preview(tagged: str, signature: str | None = None) -> dict:
    """Live preview for the editor; reports invalid input in "error" and never raises."""
    try:
        text, entities = _parse_tagged(tagged)
    except CaptionError as error:
        return {
            "ok": False,
            "html": None,
            "length": None,
            "limit": CAPTION_LIMIT,
            "signature_fits": None,
            "error": error.message,
        }
    length = len(text) if text else 0
    error = _too_long(length)
    return {
        "ok": error is None,
        "html": render_html(text, entities),
        "length": length,
        "limit": CAPTION_LIMIT,
        "signature_fits": signature_fits(text, signature),
        "error": error,
    }

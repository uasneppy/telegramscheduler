"""Caption conversion between Telegram entities and tagged text, and the preview."""

import re
import time

import pytest

from bot.web import captions
from bot.web.captions import (
    CAPTION_LIMIT,
    CaptionError,
    entities_to_json,
    entities_to_tagged,
    parse_entities_json,
    preview,
    render_html,
    signature_fits,
    tagged_to_caption,
)
from bot.web.errors import WebError

ROUND_TRIP_CASES = {
    "bold": ("Hello world", {"type": "bold", "offset": 6, "length": 5}, "Hello <b>world</b>"),
    "italic": ("Hello world", {"type": "italic", "offset": 0, "length": 5}, "<i>Hello</i> world"),
    "underline": ("Hello world", {"type": "underline", "offset": 0, "length": 11}, "<u>Hello world</u>"),
    "strikethrough": ("Hello world", {"type": "strikethrough", "offset": 6, "length": 5}, "Hello <s>world</s>"),
    "code": ("run ls -la", {"type": "code", "offset": 4, "length": 6}, "run <code>ls -la</code>"),
    "pre": (
        "x = 1 < 2",
        {"type": "pre", "offset": 0, "length": 9, "language": "python"},
        '<pre><code class="language-python">x = 1 &lt; 2</code></pre>',
    ),
    "pre-plain": ("a\nb", {"type": "pre", "offset": 0, "length": 3}, "<pre>a\nb</pre>"),
    "text_link": (
        "read more",
        {"type": "text_link", "offset": 5, "length": 4, "url": "https://x.co/?a=1&b=2"},
        'read <a href="https://x.co/?a=1&amp;b=2">more</a>',
    ),
    "blockquote": ("quoted", {"type": "blockquote", "offset": 0, "length": 6}, "<blockquote>quoted</blockquote>"),
    "expandable_blockquote": (
        "long quote",
        {"type": "expandable_blockquote", "offset": 0, "length": 10},
        "<blockquote expandable>long quote</blockquote>",
    ),
    "spoiler": ("the end", {"type": "spoiler", "offset": 4, "length": 3}, "the <tg-spoiler>end</tg-spoiler>"),
    "custom_emoji": (
        "Hi 😀",
        {"type": "custom_emoji", "offset": 3, "length": 2, "custom_emoji_id": "5368324170671202286"},
        'Hi <tg-emoji emoji-id="5368324170671202286">😀</tg-emoji>',
    ),
}


@pytest.mark.parametrize("text, entity, tagged", ROUND_TRIP_CASES.values(), ids=ROUND_TRIP_CASES.keys())
def test_round_trip_each_type(text, entity, tagged):
    assert entities_to_tagged(text, [entity]) == tagged
    assert tagged_to_caption(tagged) == (text, [entity])


def test_emoji_offsets_utf16():
    bold = {"type": "bold", "offset": 3, "length": 4}
    assert entities_to_tagged("😀 bold", [bold]) == "😀 <b>bold</b>"
    assert tagged_to_caption("😀 <b>bold</b>") == ("😀 bold", [bold])


def _coverage(entities, kind):
    return {unit for e in entities if e["type"] == kind for unit in range(e["offset"], e["offset"] + e["length"])}


def test_overlapping_entities_become_well_nested_tags():
    entities = [{"type": "bold", "offset": 0, "length": 5}, {"type": "italic", "offset": 3, "length": 5}]
    tagged = entities_to_tagged("abcdefgh", entities)
    assert tagged == "<b>abc<i>de</i></b><i>fgh</i>"
    text, parsed = tagged_to_caption(tagged)  # raises on unbalanced or crossed tags
    assert text == "abcdefgh"
    for kind in ("bold", "italic"):
        assert _coverage(parsed, kind) == _coverage(entities, kind)


def test_literal_angle_and_ampersand_round_trip():
    assert tagged_to_caption("I <3 you & AT&T, 5 < 10") == ("I <3 you & AT&T, 5 < 10", None)
    assert tagged_to_caption("5 &lt; 10 &amp; more") == ("5 < 10 & more", None)
    assert entities_to_tagged("a < b & c", None) == "a &lt; b &amp; c"
    # Only Telegram's references are decoded; any other &-sequence is literal text.
    assert tagged_to_caption("&notes &copy; &#65; &quot;x&quot;") == ('&notes &copy; A "x"', None)
    # A < that cannot start a supported tag is literal, never silently dropped.
    assert tagged_to_caption("a </> b <!-- c --> <?d?>") == ("a </> b <!-- c --> <?d?>", None)
    text = 'if a<b && c > d: print("<b>")'
    assert tagged_to_caption(entities_to_tagged(text, None)) == (text, None)


def test_auto_entities_carried_when_span_unchanged():
    old_text = "see https://x.co now"
    url = {"type": "url", "offset": 4, "length": 12}
    assert tagged_to_caption("see https://x.co soon", old_text, [url]) == ("see https://x.co soon", [url])
    assert tagged_to_caption("see now", old_text, [url]) == ("see now", None)
    assert tagged_to_caption("hi see https://x.co now", old_text, [url]) == (
        "hi see https://x.co now",
        [{"type": "url", "offset": 7, "length": 12}],
    )
    # Offsets of carried entities are UTF-16 too; tag entities come from the tags only.
    bold = {"type": "bold", "offset": 0, "length": 3}
    assert tagged_to_caption("😀 <b>see</b> https://x.co now", old_text, [url, bold]) == (
        "😀 see https://x.co now",
        [{"type": "bold", "offset": 3, "length": 3}, {"type": "url", "offset": 7, "length": 12}],
    )


@pytest.mark.parametrize(
    "tagged",
    [
        "<b>x",
        "x</b>",
        "<foo>x</foo>",
        "<b><i>x</b></i>",
        "<a>x</a>",
        '<a href="">x</a>',
        "<b/>x",
        '<b class="x">y</b>',
        "<tg-emoji>x</tg-emoji>",
        '<code class="language-py">x</code>',
        "<script>alert(1)</script>",
        "x <b",
    ],
)
def test_rejects_bad_tags(tagged):
    with pytest.raises(CaptionError) as info:
        tagged_to_caption(tagged)
    assert isinstance(info.value, WebError)
    assert (info.value.status, info.value.code) == (400, "invalid_request")
    assert info.value.message


@pytest.mark.parametrize("tagged", ["", "  \n", "<b> </b>"])
def test_empty_is_none(tagged):
    assert tagged_to_caption(tagged) == (None, None)


def test_limit():
    assert CAPTION_LIMIT == 1024
    assert tagged_to_caption("a" * 1024) == ("a" * 1024, None)
    assert tagged_to_caption("&amp;" * 1024) == ("&" * 1024, None)
    assert tagged_to_caption("<b>" + "a" * 1024 + "</b>")[0] == "a" * 1024
    with pytest.raises(CaptionError):
        tagged_to_caption("a" * 1025)


def _link(url):
    return [{"type": "text_link", "offset": 0, "length": 5, "url": url}]


def test_render_html_escapes_and_whitelists():
    assert render_html("<script>alert(1)</script>", None) == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert render_html("click", _link("javascript:alert(1)")) == "click"
    assert render_html("click", _link(" JavaScript:alert(1)")) == "click"
    assert '<a href="tg://user?id=1"' in render_html("click", _link("tg://user?id=1"))
    assert '<a href="https://x.co"' in render_html("click", _link("https://x.co"))
    assert 'href="https://x.co/&quot;onclick=&quot;x"' in render_html("click", _link('https://x.co/"onclick="x'))
    spoiler = [{"type": "spoiler", "offset": 4, "length": 3}]
    assert render_html("the end", spoiler) == 'the <span class="tg-spoiler">end</span>'
    emoji = [{"type": "custom_emoji", "offset": 3, "length": 2, "custom_emoji_id": "5"}]
    html = render_html("Hi 😀", emoji)
    assert html.startswith('Hi <span class="tg-emoji"') and html.endswith(">😀</span>")
    assert render_html("a\nb", None) == "a<br>b"
    assert render_html(None, None) == ""


def test_tolerates_corrupt_entities():
    corrupt = [
        {"type": "bold", "offset": 100, "length": 3},
        {"type": "italic", "offset": 0, "length": -2},
        {"offset": 0, "length": 2},
        {"type": "bold"},
        "garbage",
        {"type": "strikethrough", "offset": "0", "length": 2},
        {"type": "bold", "offset": 1, "length": 2},  # starts inside the emoji's surrogate pair
        {"type": "underline", "offset": 2, "length": 1},
    ]
    assert entities_to_tagged("😀ab", corrupt) == "😀<u>a</u>b"
    assert render_html("😀ab", corrupt) == "😀<u>a</u>b"
    assert parse_entities_json("{bad") == []
    assert parse_entities_json(None) == []
    assert parse_entities_json('{"type": "bold"}') == []
    raw = '[{"type": "bold", "offset": 0, "length": 2, "url": null, "user": 7}, 3, {"type": "x"}]'
    assert parse_entities_json(raw) == [{"type": "bold", "offset": 0, "length": 2}]


def test_entities_json_round_trip():
    assert entities_to_json(None) is None
    assert entities_to_json([]) is None
    entities = [{"type": "text_link", "offset": 0, "length": 2, "url": "https://x.co"}]
    assert parse_entities_json(entities_to_json(entities)) == entities


def test_preview_reports_errors_without_raising():
    assert preview("<b>hi</b> &amp; bye", "sig") == {
        "ok": True,
        "html": "<b>hi</b> &amp; bye",
        "length": 8,
        "limit": 1024,
        "signature_fits": True,
        "error": None,
    }
    bad = preview("<b>hi")
    assert bad["ok"] is False and bad["error"] and bad["html"] is None and bad["limit"] == 1024
    too_long = preview("<b>" + "a" * 1025 + "</b>")
    assert too_long["ok"] is False and too_long["length"] == 1025 and "1024" in too_long["error"]
    assert too_long["html"] is None  # over-limit text is not rendered
    assert preview(42)["ok"] is False
    empty = preview("  ")
    assert empty["ok"] is True and empty["html"] == "" and empty["length"] == 0


HUGE_CHARREF = "&#" + "1" * 5000 + ";"  # int() refuses more than 4300 digits


def test_huge_character_reference_is_literal_text():
    with pytest.raises(CaptionError):  # literal text, 5003 characters: over the limit
        tagged_to_caption(HUGE_CHARREF)
    result = preview(HUGE_CHARREF)
    assert result["ok"] is False and result["length"] == len(HUGE_CHARREF)
    assert tagged_to_caption(f'<a href="{HUGE_CHARREF}">x</a>') == (
        "x",
        [{"type": "text_link", "offset": 0, "length": 1, "url": HUGE_CHARREF}],
    )
    assert preview(f'<a href="{HUGE_CHARREF}">x</a>')["ok"] is True


def test_parser_value_error_becomes_caption_error(monkeypatch):
    # Defence in depth: even with the reference bound removed, no ValueError escapes.
    monkeypatch.setattr(captions, "_BARE_AMP", re.compile(r"&(?!#[0-9]+;)"))
    with pytest.raises(CaptionError):
        tagged_to_caption(HUGE_CHARREF)
    with pytest.raises(CaptionError):
        tagged_to_caption(f'<a href="{HUGE_CHARREF}">x</a>')
    assert preview(HUGE_CHARREF)["ok"] is False


def test_oversized_markup_rejected_quickly():
    assert tagged_to_caption(" " * (16 * CAPTION_LIMIT)) == (None, None)
    with pytest.raises(CaptionError):
        tagged_to_caption(" " * (16 * CAPTION_LIMIT + 1))
    tagged = "<b>a</b>" * 16000  # 128 KB of markup
    started = time.perf_counter()
    with pytest.raises(CaptionError):
        tagged_to_caption(tagged)
    result = preview(tagged)
    assert time.perf_counter() - started < 0.5
    assert result["ok"] is False and result["html"] is None and result["error"]


def test_signature_fits_matches_publisher():
    assert signature_fits("a" * 1000, "s" * 30) is False
    assert signature_fits("a" * 1000, "s" * 10) is True
    assert signature_fits("a" * 1000, None) is None
    assert signature_fits("a" * 1000, "") is None
    assert signature_fits("a" * 1012, "s" * 10) is True  # 1012 + "\n\n" + 10 == 1024
    assert signature_fits("a" * 1013, "s" * 10) is False
    assert signature_fits(None, "s" * 1024) is True

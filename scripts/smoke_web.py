#!/usr/bin/env python3
"""Browser smoke test for the web board (manual; not part of the pytest suite).

Setup, once per virtualenv:  pip install playwright
(Pillow, which draws the upload test images, comes with the project's requirements.)
Start the seeded dev server and an empty one beside it, then run the checks
against the links they print:

    python scripts/dev_web.py &
    python scripts/dev_web.py --empty --port 8766 &
    SMOKE_URL='http://localhost:8765/login?t=...' \
    SMOKE_EMPTY_URL='http://localhost:8766/login?t=...' python scripts/smoke_web.py

SMOKE_EMPTY_URL is only used by check_empty_states (a user with no channels and
no posts); without it that check fails and says so. CHROMIUM_PATH names a
Chromium binary (default: Playwright's own browser from `playwright install
chromium`). SMOKE_SHOTS=<folder> also saves screenshots of the board, the edit
panel, the bulk bar, the Undo toast and the empty states: desktop 1280x800 and
phone 360x740, light and dark.

The sign-in links work once, so restart both servers before every run; that also
gives each run the same fresh seed (the checks expect it, and the edit panel and
bulk checks change posts, so they run in order after the read-only list checks).
Every browser context runs in a time zone far from Kyiv (Pacific/Kiritimati,
UTC+14, unless a check picks another), so a time read from the browser's own
clock instead of the server's Kyiv strings shows up as a wrong hour or day.
Prints `PASS name` or `FAIL name: reason` for each check and exits 1 when any
check fails.
"""

import io
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageDraw
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import expect, sync_playwright

TABS = ("scheduled", "queued", "failed", "posted")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SEEDED_SCHEDULED = 130
PAGE_SIZE = 50  # the API's default page, which the board uses
TIMEOUT_MS = 10_000
DESKTOP = {"viewport": {"width": 1280, "height": 800}}
PHONE = {"viewport": {"width": 360, "height": 740}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True}
# UTC+14: a time taken from the browser's zone instead of Kyiv's is off by 12 hours or a day.
DEFAULT_TIMEZONE = "Pacific/Kiritimati"
ALPHA, BETA = "-1001", "-1002"
ALPHA_SIGNATURE = "Alpha News · @alpha_news"  # seeded by dev_web.py; Beta has none
CAPTION_LIMIT = 1024
DISCARD_PROMPT = "unsaved"  # the Esc confirmation names the unsaved changes
POSTED_DELETE_PROMPT = "Removes this record only; the message stays in the channel"
DOWNGRADE_NOTICE = "This photo is larger than Telegram's photo limit, so it was stored as a file at its original quality."

# Every list row and day header, in document order.
LIST_NODES_JS = """() => [...document.querySelectorAll('#post-list .day-header, #post-list .post-row')].map(
    (node) => node.classList.contains('day-header')
        ? {header: node.textContent}
        : {id: Number(node.dataset.id), time: node.querySelector('.time').textContent,
           channel: node.querySelector('.channel-chip').textContent})"""
ROW_COUNT_JS = "() => document.querySelectorAll('#post-list .post-row').length"
# The list has finished loading and shows rows or its empty state.
LIST_READY_JS = """() => {
    const list = document.querySelector('#post-list');
    const empty = document.querySelector('#empty-state');
    return list !== null && list.getAttribute('aria-busy') === 'false'
        && (list.querySelector('.post-row') !== null || (empty !== null && !empty.hidden));
}"""
# Problems with rows that do not fit: a channel name cut short, or a chip or badge
# running into the row's buttons.
ROW_FIT_JS = """() => {
    const problems = [];
    for (const row of document.querySelectorAll('#post-list .post-row')) {
        const buttons = row.querySelector('.row-actions').getBoundingClientRect();
        for (const chip of row.querySelectorAll('.channel-chip')) {
            if (chip.scrollWidth > chip.clientWidth + 1) {
                problems.push(`row ${row.dataset.id}: channel '${chip.textContent}' is cut to ${chip.clientWidth}px of ${chip.scrollWidth}px`);
            }
        }
        for (const node of row.querySelectorAll('.channel-chip, .badge')) {
            const right = node.getBoundingClientRect().right;
            if (right > buttons.left + 0.5) {
                problems.push(`row ${row.dataset.id}: '${node.textContent}' ends at ${right}px, the buttons start at ${buttons.left}px`);
            }
        }
    }
    return problems;
}"""
NO_COUNT = "–"  # shown in a tab until /api/me has answered


class Smoke:
    """The browser, the signed-in context and the errors seen on every page."""

    def __init__(self, browser, base: str, shots: Path | None = None):
        self.browser = browser
        self.base = base
        self.shots = shots
        self.context = None
        self.storage = None
        self.empty_base = None  # the --empty board, once check_empty_states has signed in to it
        self.empty_storage = None
        self.page_errors: list[str] = []
        self.console_errors: list[str] = []

    def shot(self, page, name: str) -> None:
        """Save a screenshot when SMOKE_SHOTS names a folder."""
        if self.shots is None:
            return
        self.shots.mkdir(parents=True, exist_ok=True)
        path = self.shots / f"{name}.png"
        page.screenshot(path=str(path))
        print(f"SHOT {path}", flush=True)

    def new_context(self, **options):
        options.setdefault("timezone_id", DEFAULT_TIMEZONE)
        context = self.browser.new_context(storage_state=self.storage, **options)
        context.set_default_timeout(TIMEOUT_MS)
        return context

    def watch(self, page, expect_failed_requests: bool = False) -> None:
        """Record uncaught exceptions and console errors (a failed resource load only when unexpected)."""
        page.on("pageerror", lambda error: self.page_errors.append(f"{page.url}: {error}"))

        def on_console(message):
            if message.type != "error":
                return
            if message.text.startswith("Failed to load resource") and (
                expect_failed_requests or "status of 404" in message.text
            ):
                return
            self.console_errors.append(f"{page.url}: {message.text}")

        page.on("console", on_console)

    @contextmanager
    def page(self, hash_: str | None = "#tab=scheduled", context=None, expect_failed_requests=False):
        """A new page on the board; navigated to `hash_` and loaded unless it is None."""
        page = (context or self.context).new_page()
        self.watch(page, expect_failed_requests)
        try:
            if hash_ is not None:
                page.goto(f"{self.base}/{hash_}")
                wait_for_list(page)
            yield page
        finally:
            page.close()

    def api(self, path: str, **params):
        response = self.context.request.get(self.base + path, params=params or None)
        assert response.ok, f"GET {path} answered {response.status}"
        return response.json()

    def all_items(self, status: str, **filters) -> list[dict]:
        """Every item of a tab, paged with the default page size like the board."""
        items, cursor = [], None
        while True:
            params = {"status": status, **filters}
            if cursor:
                params["cursor"] = cursor
            page = self.api("/api/posts", **params)
            items += page["items"]
            cursor = page["next_cursor"]
            if not cursor:
                return items


# --- helpers ------------------------------------------------------------------------


def wait_for_list(page) -> None:
    page.wait_for_function(LIST_READY_JS)


def row(page, post_id: int):
    return page.locator(f'#post-list .post-row[data-id="{post_id}"]')


def row_ids(page) -> list[int]:
    return page.evaluate("() => [...document.querySelectorAll('#post-list .post-row')].map((r) => Number(r.dataset.id))")


def scroll_to_end(page, total: int) -> None:
    """Scroll until the list holds `total` rows; fails when loading stops short."""
    while (count := page.evaluate(ROW_COUNT_JS)) < total:
        page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
        try:
            page.wait_for_function(f"() => ({ROW_COUNT_JS})() > {count}", timeout=5000)
        except PlaywrightTimeout:
            raise AssertionError(f"scrolling stopped loading at {count} of {total} rows") from None
    wait_for_list(page)


def day_label(iso_day: str) -> str:
    """`Mon 02.11` for 2026-11-02."""
    return f"{WEEKDAYS[date.fromisoformat(iso_day).weekday()]} {iso_day[8:10]}.{iso_day[5:7]}"


def hash_params(page) -> dict:
    return {key: values[0] for key, values in parse_qs(urlsplit(page.url).fragment).items()}


def first_page(smoke: Smoke, status: str = "scheduled") -> list[dict]:
    return smoke.api("/api/posts", status=status)["items"]


# --- checks ---------------------------------------------------------------------------


def check_tab_counts_match_api(smoke: Smoke):
    with smoke.page() as page:
        counts = smoke.api("/api/me")["counts"]
        for tab in TABS:
            expect(page.locator(f'[data-tab="{tab}"] .count')).to_have_text(str(counts[tab]))
        expect(page.locator('[data-tab="scheduled"]')).to_have_attribute("aria-selected", "true")


def check_scheduled_grouped_and_sorted(smoke: Smoke):
    items = {item["id"]: item for item in smoke.all_items("scheduled")}
    with smoke.page() as page:
        scroll_to_end(page, len(items))
        nodes = page.evaluate(LIST_NODES_JS)
    assert nodes and "header" in nodes[0], "the list does not start with a day header"
    groups: list[tuple[str, list[dict]]] = []
    for node in nodes:
        if "header" in node:
            groups.append((node["header"], []))
        else:
            groups[-1][1].append(node)
    headers = [header for header, _rows in groups]
    assert len(headers) == len(set(headers)), f"a day header appears twice: {headers}"
    previous_day = ""
    for header, rows in groups:
        assert re.fullmatch(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d\d\.\d\d", header), f"header {header!r} is not like 'Mon 02.11'"
        assert rows, f"day header {header!r} has no rows"
        days = {items[r["id"]]["scheduled_time"][:10] for r in rows}
        assert len(days) == 1, f"{header!r} holds rows of several days: {sorted(days)}"
        day = days.pop()
        assert header == day_label(day), f"rows of {day} sit under {header!r}, expected {day_label(day)!r}"
        assert day > previous_day, f"day {day} comes after {previous_day}"
        previous_day = day
        times = [r["time"] for r in rows]
        assert times == [items[r["id"]]["scheduled_time"][11:16] for r in rows], f"{header}: times {times} differ from the API"
        assert times == sorted(times), f"{header}: times are not ascending: {times}"


def check_infinite_scroll_loads_all(smoke: Smoke):
    items = smoke.all_items("scheduled")
    expected = [item["id"] for item in items]
    assert len(expected) == SEEDED_SCHEDULED, f"the API lists {len(expected)} scheduled posts, the seed has {SEEDED_SCHEDULED}"
    # The seed puts Alpha and Beta on the same slot across each page boundary.
    for boundary in range(PAGE_SIZE, len(items), PAGE_SIZE):
        before, after = items[boundary - 1], items[boundary]
        assert before["scheduled_time"] == after["scheduled_time"] and before["channel_id"] != after["channel_id"], (
            f"rows {boundary} and {boundary + 1} are not a same-time pair; the seed changed"
        )
    with smoke.page() as page:
        first = page.evaluate(ROW_COUNT_JS)
        assert first == PAGE_SIZE, f"the first load shows {first} rows, expected one page of {PAGE_SIZE}"
        scroll_to_end(page, len(expected))
        page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
        page.wait_for_timeout(500)  # nothing more may arrive once the list is complete
        ids = row_ids(page)
        nodes = page.evaluate(LIST_NODES_JS)
        expect(page.locator("#list-status")).to_have_text("End of list")
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"rows shown twice: {duplicates}"
    assert ids == expected, f"the list shows {len(ids)} rows that differ from the API's {len(expected)}"
    slots: dict[tuple[str, str], list[str]] = {}
    day = None
    for node in nodes:
        if "header" in node:
            day = node["header"]
        else:
            slots.setdefault((day, node["time"]), []).append(node["channel"])
    pairs = [key for key, channels in slots.items() if sorted(channels) == ["Alpha", "Beta"]]
    assert len(pairs) == 60, f"{len(pairs)} same-time Alpha/Beta pairs shown, the seed has 60"


def check_channel_filter(smoke: Smoke):
    with smoke.page() as page:
        options = page.locator("#channel-filter option").all_inner_texts()
        assert options == ["All channels", "Alpha", "Beta"], f"channel options are {options}"
        page.select_option("#channel-filter", label="Beta")
        wait_for_list(page)
        assert hash_params(page).get("channel") == "-1002", f"the hash is {urlsplit(page.url).fragment!r}"
        chips = page.locator("#post-list .post-row .channel-chip").all_inner_texts()
        assert chips and set(chips) == {"Beta"}, f"chips after choosing Beta: {sorted(set(chips))}"
        expected = [item["id"] for item in smoke.api("/api/posts", status="scheduled", channel="-1002")["items"]]
        assert row_ids(page) == expected, "the Beta rows differ from the API's first page"
        # The view state lives in the hash, so a reload keeps the filter.
        page.reload()
        wait_for_list(page)
        expect(page.locator("#channel-filter")).to_have_value("-1002")
        chips = page.locator("#post-list .post-row .channel-chip").all_inner_texts()
        assert set(chips) == {"Beta"}, f"chips after a reload: {sorted(set(chips))}"


def check_search_cyrillic_case(smoke: Smoke):
    expected = [item["id"] for item in smoke.all_items("scheduled", q="привіт")]
    assert expected, "the API finds no scheduled post for 'привіт'; the seed changed"
    with smoke.page() as page:
        page.fill("#search", "привіт")
        page.wait_for_function(
            """() => {
                const list = document.querySelector('#post-list');
                const rows = [...list.querySelectorAll('.post-row')];
                return list.getAttribute('aria-busy') === 'false' && rows.length > 0
                    && rows.every((r) => r.querySelector('.snippet').textContent.toLowerCase().includes('привіт'));
            }"""
        )
        snippets = page.locator("#post-list .post-row .snippet").all_inner_texts()
        assert any("Привіт, світ" in text for text in snippets), f"no 'Привіт, світ' row: {snippets}"
        assert row_ids(page) == expected, f"search shows {row_ids(page)}, the API {expected}"
        assert hash_params(page).get("q") == "привіт", f"the hash is {urlsplit(page.url).fragment!r}"


def check_caption_shown_literally(smoke: Smoke):
    items = first_page(smoke)
    plain = next(item for item in items if item["caption_text"] == "AT&T <3")
    formatted = next(item for item in items if item["caption_text"].startswith("Big news:"))
    empty = next(item for item in items if not item["caption_text"])
    with smoke.page() as page:
        expect(row(page, plain["id"]).locator(".snippet")).to_have_text("AT&T <3")
        snippet = row(page, formatted["id"]).locator(".snippet")
        expect(snippet).to_have_text(formatted["caption_text"])
        assert snippet.evaluate("node => node.children.length") == 0, "caption text was parsed as markup"
        expect(row(page, empty["id"]).locator(".snippet")).to_have_text("No caption")


def check_api_text_is_not_markup(smoke: Smoke):
    """Captions, channel names and failure reasons are written as text, never parsed as HTML."""
    markup = '<img src="/x" alt="injected"><b>bold</b> & <3'

    def inject(route):
        response = route.fetch()
        body = response.json()
        for item in body["items"]:
            item.update(caption_text=markup, channel_name=markup, failure_reason=markup)
        route.fulfill(response=response, json=body)

    with smoke.page(None) as page:
        page.route(lambda url: "/api/posts?" in url, inject)
        page.goto(f"{smoke.base}/#tab=failed")
        wait_for_list(page)
        first = page.locator("#post-list .post-row").first
        expect(first.locator(".snippet")).to_have_text(markup)
        expect(first.locator(".channel-chip")).to_have_text(markup)
        expect(first.locator(".failure-reason")).to_contain_text(markup)
        injected = page.locator('#post-list img[alt="injected"], #post-list b').count()
        assert injected == 0, f"{injected} elements were created from API text"


def check_thumbnail_fallback(smoke: Smoke):
    items = smoke.all_items("posted")
    missing, present = [], []
    for item in items:
        exists = smoke.api(f"/api/posts/{item['id']}")["media"][0]["exists"]
        (present if exists else missing).append(item["id"])
    assert len(missing) == 3 and present, f"posted rows without a file: {missing}; the seed has 3"
    with smoke.page("#tab=posted") as page:
        for post_id in missing:
            expect(row(page, post_id).locator(".type-icon")).to_be_visible()
            expect(row(page, post_id).locator("img")).to_have_count(0)
        for post_id in present:
            image = row(page, post_id).locator("img")
            expect(image).to_have_attribute("loading", "lazy")
            expect(image).to_have_attribute("src", f"/api/posts/{post_id}/thumb?i=0&w=160")
            page.wait_for_function("img => img.complete && img.naturalWidth > 0", arg=image.element_handle())


def check_album_and_recurring_badges(smoke: Smoke):
    items = first_page(smoke)
    album = next(item for item in items if item["media_type"] == "album")
    recurring = next(item for item in items if item["is_recurring"])
    pdf = next(item for item in items if item["media_type"] == "document")
    with smoke.page() as page:
        expect(row(page, album["id"]).locator(".badge", has_text=f"Album ×{album['media_count']}")).to_have_count(1)
        expect(row(page, album["id"]).locator(".type-icon")).to_be_visible()
        expect(row(page, recurring["id"]).locator(".badge", has_text="Recurring")).to_have_count(1)
        expect(row(page, pdf["id"]).locator(".type-icon")).to_be_visible()


def check_read_only_statuses(smoke: Smoke):
    """Rows the board cannot change (sending, unknown) render read-only but can still be
    opened (the panel then shows them read-only); overdue gets its badge."""
    changed: dict[str, int] = {}

    def relabel(route):
        response = route.fetch()
        body = response.json()
        if not changed and len(body["items"]) >= 3:
            unknown, sending, overdue = body["items"][:3]
            unknown["status"], sending["status"], overdue["overdue"] = "unknown", "sending", True
            changed.update(unknown=unknown["id"], sending=sending["id"], overdue=overdue["id"])
        route.fulfill(response=response, json=body)

    with smoke.page(None) as page:
        page.route(lambda url: "/api/posts?" in url, relabel)
        page.goto(f"{smoke.base}/#tab=scheduled")
        wait_for_list(page)
        for status, label in (("unknown", "Unknown status"), ("sending", "Sending")):
            target = row(page, changed[status])
            expect(target.locator(".badge", has_text=label)).to_have_count(1)
            for control in (".row-select", ".row-delete"):
                expect(target.locator(control)).to_be_disabled()
            opener = target.locator(".row-edit")
            expect(opener).to_be_enabled()
            expect(opener).to_have_attribute("aria-label", re.compile(r"^Open post, "))
        target = row(page, changed["overdue"])
        expect(target.locator(".badge", has_text="Overdue")).to_have_count(1)
        for control in (".row-select", ".row-edit", ".row-delete"):
            expect(target.locator(control)).to_be_enabled()


def check_error_banner_and_retry(smoke: Smoke):
    failure = {"mode": "500"}

    def flaky(route):
        if failure["mode"] == "500":
            route.fulfill(status=500, json={"error": "internal", "message": "Something went wrong on the server."})
        elif failure["mode"] == "network":
            route.abort()
        else:
            route.continue_()

    with smoke.page(None, expect_failed_requests=True) as page:
        page.route(lambda url: "/api/posts?" in url, flaky)
        page.goto(f"{smoke.base}/#tab=scheduled")
        banner = page.locator("#banner")
        expect(banner).to_be_visible()
        failure["mode"] = None
        banner.get_by_role("button", name="Retry").click()
        expect(banner).to_be_hidden()
        wait_for_list(page)
        expect(page.locator("#post-list .post-row")).to_have_count(PAGE_SIZE)
        # A network error on refresh keeps the rows and shows the banner again.
        failure["mode"] = "network"
        page.click("#refresh")
        expect(banner).to_be_visible()
        expect(page.locator("#post-list .post-row")).to_have_count(PAGE_SIZE)
        failure["mode"] = None
        banner.get_by_role("button", name="Retry").click()
        expect(banner).to_be_hidden()
        # After another failure, a successful load (here a tab switch) clears the banner
        # and infinite scroll works again.
        failure["mode"] = "network"
        page.click("#refresh")
        expect(banner).to_be_visible()
        failure["mode"] = None
        page.click('[data-tab="posted"]')
        expect(banner).to_be_hidden()
        page.click('[data-tab="scheduled"]')
        wait_for_list(page)
        scroll_to_end(page, 2 * PAGE_SIZE)


def check_me_failure_shows_banner(smoke: Smoke):
    """A failed /api/me keeps the banner up (a list success does not hide it), counts read '–',
    the list waits for the channel check, and Retry restores counts, channel filter and list."""
    failures = {"me": 2}

    def flaky_me(route):
        if failures["me"] > 0:
            failures["me"] -= 1
            route.fulfill(status=500, json={"error": "internal", "message": "Something went wrong on the server."})
        else:
            route.continue_()

    counts = smoke.api("/api/me")["counts"]
    count = {tab: f'[data-tab="{tab}"] .count' for tab in TABS}
    with smoke.page(None, expect_failed_requests=True) as page:
        page.route(lambda url: urlsplit(url).path == "/api/me", flaky_me)
        page.goto(f"{smoke.base}/#tab=scheduled&channel=-1002")
        banner = page.locator("#banner")
        expect(banner).to_be_visible()
        for tab in TABS:
            expect(page.locator(count[tab])).to_have_text(NO_COUNT)
        page.wait_for_timeout(500)
        expect(banner).to_be_visible()
        expect(page.locator("#post-list .post-row")).to_have_count(0)
        expect(page.locator("#channel-filter")).to_be_disabled()
        expect(page.locator("#channel-filter")).to_have_value("")
        # Asking for another tab tries /api/me again; while it still fails, no list loads
        # (it would be filtered by a channel the select does not show).
        with page.expect_response(lambda r: urlsplit(r.url).path == "/api/me"):
            page.click('[data-tab="queued"]')
        page.wait_for_timeout(500)
        expect(banner).to_be_visible()
        expect(page.locator("#post-list .post-row")).to_have_count(0)
        banner.get_by_role("button", name="Retry").click()
        expect(banner).to_be_hidden()
        wait_for_list(page)
        for tab in TABS:
            expect(page.locator(count[tab])).to_have_text(str(counts[tab]))
        expect(page.locator("#channel-filter")).to_be_enabled()
        expect(page.locator("#channel-filter")).to_have_value("-1002")
        chips = page.locator("#post-list .post-row .channel-chip").all_inner_texts()
        assert chips and set(chips) == {"Beta"}, f"chips after Retry: {sorted(set(chips))}"
        # On refresh the two requests run side by side: whichever finishes last, a
        # failed /api/me keeps the banner up and the last counts stay.
        failures["me"] = 1
        with page.expect_response(lambda r: urlsplit(r.url).path == "/api/me"), page.expect_response(
            lambda r: "/api/posts?" in r.url
        ):
            page.click("#refresh")
        wait_for_list(page)
        page.wait_for_timeout(300)
        expect(banner).to_be_visible()
        expect(page.locator(count["scheduled"])).to_have_text(str(counts["scheduled"]))
        banner.get_by_role("button", name="Retry").click()
        expect(banner).to_be_hidden()


def check_refresh_keeps_place(smoke: Smoke):
    """#refresh and coming back to the browser tab refetch counts and rows, keeping the rows loaded."""

    def refetched(page, trigger) -> None:
        with page.expect_response(lambda r: "/api/me" in r.url), page.expect_response(lambda r: "/api/posts?" in r.url):
            trigger()
        wait_for_list(page)

    with smoke.page() as page:
        scroll_to_end(page, 2 * PAGE_SIZE)
        before = row_ids(page)
        refetched(page, lambda: page.click("#refresh"))
        assert row_ids(page)[: len(before)] == before, "refresh dropped or reordered the rows already loaded"
        page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight / 2)")
        position = page.evaluate("() => window.scrollY")
        refetched(page, lambda: page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))"))
        assert row_ids(page)[: len(before)] == before, "the refetch on visibilitychange dropped rows"
        moved = abs(page.evaluate("() => window.scrollY") - position)
        assert moved < 2, f"the refetch moved the page by {moved}px"


def check_unauthorized_shows_run_web(smoke: Smoke):
    def signed_out(route):
        route.fulfill(status=401, json={"error": "unauthorized", "message": "Not signed in. Send /web to the bot in Telegram."})

    with smoke.page(None, expect_failed_requests=True) as page:
        page.route(lambda url: "/api/" in urlsplit(url).path, signed_out)
        page.goto(f"{smoke.base}/#tab=scheduled")
        view = page.locator("#signed-out")
        expect(view).to_be_visible()
        expect(view).to_contain_text("/web")
        expect(view).to_contain_text("Telegram")
        expect(page.locator("#app")).to_be_hidden()


def check_list_fits_phone_width(smoke: Smoke):
    context = smoke.new_context(**PHONE)
    try:
        with smoke.page(context=context) as page:
            for tab in TABS:
                page.click(f'[data-tab="{tab}"]')
                wait_for_list(page)
                width = page.evaluate("() => document.documentElement.scrollWidth")
                assert width <= 360, f"{tab}: the page is {width}px wide at a 360px viewport"
                for name in TABS:
                    box = page.locator(f'[data-tab="{name}"]').bounding_box()
                    assert box and box["x"] >= 0 and box["x"] + box["width"] <= 360, f"tab {name} sticks out: {box}"
                problems = page.evaluate(ROW_FIT_JS)
                assert not problems, f"{tab}: {problems[0]} ({len(problems)} problems)"
    finally:
        context.close()


def check_failed_tab_readable_on_phone(smoke: Smoke):
    """At 360 px a failed row keeps its channel name whole and its badges clear of the
    buttons, even a failed recurring album on the longest seeded channel name."""

    def worst_case(route):
        response = route.fetch()
        body = response.json()
        if body["items"]:
            body["items"][0].update(
                channel_id="-1001", channel_name="Alpha", media_type="album", media_count=3,
                is_recurring=True, recurring_interval_hours=24,
            )
        route.fulfill(response=response, json=body)

    context = smoke.new_context(**PHONE)
    try:
        with smoke.page(None, context=context) as page:
            page.route(lambda url: "/api/posts?" in url and "status=failed" in url, worst_case)
            page.goto(f"{smoke.base}/#tab=failed")
            wait_for_list(page)
            first = page.locator("#post-list .post-row").first
            for label in ("Failed", "Album ×3", "Recurring"):
                expect(first.locator(".badge", has_text=label)).to_have_count(1)
            problems = page.evaluate(ROW_FIT_JS)
            assert not problems, f"{problems[0]} ({len(problems)} problems)"
            smoke.shot(page, "failed-mobile-worst-case-light")
    finally:
        context.close()


# --- edit panel helpers -----------------------------------------------------------------

# The panel shows post `id`, its detail has loaded and no caption preview is in flight.
PANEL_READY_JS = """(id) => {
    const panel = document.querySelector('#edit-panel');
    const preview = document.querySelector('#caption-preview');
    return panel !== null && !panel.hidden && panel.dataset.state === 'ready'
        && panel.dataset.postId === String(id)
        && preview !== null && preview.getAttribute('aria-busy') === 'false';
}"""
# Problems with an open panel that does not fit the viewport: the page scrolls sideways,
# or a visible panel control reaches past the left or right edge.
PANEL_FIT_JS = """() => {
    const problems = [];
    const root = document.documentElement;
    if (root.scrollWidth > root.clientWidth) problems.push(`the page is ${root.scrollWidth}px wide`);
    const controls = '#edit-panel button, #edit-panel input, #edit-panel select, #edit-panel textarea, #edit-panel label';
    for (const node of document.querySelectorAll(controls)) {
        const box = node.getBoundingClientRect();
        if (box.width === 0 || node.classList.contains('visually-hidden')) continue;
        if (box.left < -0.5 || box.right > root.clientWidth + 0.5) {
            problems.push(`${node.id || node.tagName} spans ${Math.round(box.left)}..${Math.round(box.right)}px`);
        }
    }
    return problems;
}"""


def detail(smoke: Smoke, post_id: int) -> dict:
    return smoke.api(f"/api/posts/{post_id}")


def kyiv(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def pick(smoke: Smoke, status="scheduled", caption=None, startswith=None, media_type=None, channel=None) -> dict:
    """The first item on the first page of a tab that has all the given properties."""
    for item in first_page(smoke, status):
        if caption is not None and item["caption_text"] != caption:
            continue
        if startswith is not None and not item["caption_text"].startswith(startswith):
            continue
        if media_type is not None and item["media_type"] != media_type:
            continue
        if channel is not None and item["channel_id"] != channel:
            continue
        return item
    wanted = {"caption": caption, "startswith": startswith, "media_type": media_type, "channel": channel}
    raise AssertionError(f"no {status} post on the first page has {wanted}; the seed changed")


def wait_for_panel(page, post_id: int) -> None:
    page.wait_for_function(PANEL_READY_JS, arg=post_id)
    # Let the opening animation finish, so boxes and screenshots show the panel in place.
    page.locator("#edit-panel").evaluate("node => Promise.all(node.getAnimations().map((a) => a.finished))")


def open_panel(page, post_id: int) -> None:
    row(page, post_id).locator(".row-edit").click()
    wait_for_panel(page, post_id)


def board_call(page, method: str, path: str):
    """Waits for the page's `method` request to `path` (use with `with ... as info`)."""
    return page.expect_response(lambda r: r.request.method == method and urlsplit(r.url).path == path)


def assert_board_call(smoke: Smoke, response, expected: int = 200) -> None:
    """The page's call got `expected` and carried the board's Origin and X-Board headers."""
    request = response.request
    headers = request.all_headers()
    where = f"{request.method} {urlsplit(response.url).path}"
    assert response.status == expected, f"{where} answered {response.status}: {response.text()[:200]}"
    assert headers.get("origin") == smoke.base, f"{where} sent Origin {headers.get('origin')!r}"
    assert headers.get("x-board") == "1", f"{where} did not send X-Board: 1"


def board_post(smoke: Smoke, path: str) -> dict:
    """A change made outside the page (another tab, say), with the headers the board requires."""
    response = smoke.context.request.post(smoke.base + path, headers={"Origin": smoke.base, "X-Board": "1"})
    assert response.ok, f"POST {path} answered {response.status}"
    return response.json()


def select_caption_text(page, text: str) -> None:
    """Select the first `text` in the caption editor, as a person dragging over it would."""
    caption = page.locator("#caption-input")
    start = caption.input_value().index(text)
    caption.evaluate(
        "(node, [start, end]) => { node.focus(); node.setSelectionRange(start, end); }", [start, start + len(text)]
    )


def png_bytes(label: str) -> bytes:
    image = Image.new("RGB", (480, 320), "#2f6f4f")
    ImageDraw.Draw(image).text((24, 24), label, fill="white")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def choose_upload(page, label: str) -> None:
    page.set_input_files(
        "#replace-media-input", files=[{"name": f"{label}.png", "mimeType": "image/png", "buffer": png_bytes(label)}]
    )


# Records the fields of every FormData the page sends, in order. Chromium does not hand
# Playwright a multipart body that holds a file, so the order is read in the page.
RECORD_UPLOADS_JS = """() => {
    window.__uploads = [];
    const send = window.fetch;
    window.fetch = (input, init = {}) => {
        if (init.body instanceof FormData) {
            window.__uploads.push([...init.body.entries()].map(([name, value]) => [name, typeof value === 'string' ? value : value.name]));
        }
        return send(input, init);
    };
}"""


def last_upload_as_file(page) -> str:
    """The as_file value of the page's last upload, checking that it comes before the file."""
    fields = page.evaluate("() => window.__uploads.at(-1)")
    names = [name for name, _value in fields]
    assert names == ["as_file", "file"], f"the upload's fields are {names}; as_file must come before file"
    return fields[0][1]


def image_loaded(page, image) -> None:
    page.wait_for_function("img => img.complete && img.naturalWidth > 0", arg=image.element_handle())


def discard_and_close(page) -> None:
    page.once("dialog", lambda dialog: dialog.accept())
    page.keyboard.press("Escape")
    expect(page.locator("#edit-panel")).to_be_hidden()


# Ctrl + a letter on the Ukrainian layout: the letter's (event.key, event.code, Windows
# virtual key code). Cyrillic letters sit on the keys of the Latin ones (И on B, Я on Z);
# a non-Latin layout reports the US key's virtual code, which is what Chromium's own
# editing shortcuts (Ctrl+Z undoing in a text field) go by.
UKRAINIAN = {
    "b": ("и", "KeyB", 66), "i": ("ш", "KeyI", 73), "u": ("г", "KeyU", 85), "k": ("л", "KeyK", 75),
    "z": ("я", "KeyZ", 90),
}
# Records every keydown as the page's handlers left it: a listener on window runs after the
# board's own (on the element and on document), so `prevented` says whether the board took the key.
WATCH_KEYS_JS = """() => {
    window.__keys = [];
    window.addEventListener('keydown', (event) => window.__keys.push(
        {key: event.key, code: event.code, repeat: event.repeat, prevented: event.defaultPrevented}));
}"""


def press_on_layout(cdp, key: str, code: str, vk: int, repeats: int = 0) -> None:
    """Press Ctrl+<key> as the keyboard sends it: trusted events with this key, code and virtual
    key code (Playwright's own keyboard always types the US layout). `repeats` adds the
    auto-repeat keydowns of a key held down."""
    event = {"modifiers": 2, "key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
    cdp.send("Input.dispatchKeyEvent", {"type": "rawKeyDown", **event, "autoRepeat": False})
    for _ in range(repeats):
        cdp.send("Input.dispatchKeyEvent", {"type": "rawKeyDown", **event, "autoRepeat": True})
    cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", **event})


def keys_seen(page, key: str) -> list[dict]:
    """The keydowns of `key` recorded by WATCH_KEYS_JS, in order."""
    return page.evaluate("(key) => window.__keys.filter((k) => k.key === key)", key)


# --- edit panel checks ----------------------------------------------------------------------


def check_panel_dialog_and_focus(smoke: Smoke):
    """A named dialog: focus moves in on open and back to the opener on close; Enter on a
    focused row opens it too."""
    item = pick(smoke, caption="Behind the scenes at the studio.")
    with smoke.page() as page:
        opener = row(page, item["id"]).locator(".row-edit")
        opener.click()
        wait_for_panel(page, item["id"])
        panel = page.locator("#edit-panel")
        expect(panel).to_have_attribute("role", "dialog")
        expect(panel).to_have_accessible_name("Edit post")
        inside = page.evaluate("() => document.querySelector('#edit-panel').contains(document.activeElement)")
        assert inside, "focus did not move into the panel"
        expect(page.locator("#caption-input")).to_have_value(detail(smoke, item["id"])["caption_tagged"])
        page.keyboard.press("Escape")  # nothing changed, so it closes without asking
        expect(panel).to_be_hidden()
        expect(opener).to_be_focused()
        target = row(page, item["id"])
        target.focus()
        page.keyboard.press("Enter")
        wait_for_panel(page, item["id"])
        page.click("#panel-close")
        expect(panel).to_be_hidden()
        expect(target).to_be_focused()


def check_time_is_kyiv_not_browser_zone(smoke: Smoke):
    """Review Focus 5: in a New York browser the panel shows and saves Kyiv wall-clock times."""
    earliest = first_page(smoke)[0]
    assert earliest["scheduled_time"][11:16] == "10:00", f"the earliest post is at {earliest['scheduled_time']}; the seed changed"
    next_day = date.fromisoformat(earliest["scheduled_time"][:10]) + timedelta(days=1)
    wanted = f"{next_day.isoformat()}T11:30"
    path = f"/api/posts/{earliest['id']}"
    context = smoke.new_context(timezone_id="America/New_York", **DESKTOP)
    try:
        with smoke.page(context=context) as page:
            zone = page.evaluate("() => Intl.DateTimeFormat().resolvedOptions().timeZone")
            assert zone == "America/New_York", f"the browser runs in {zone}"
            expect(row(page, earliest["id"]).locator(".time")).to_have_text("10:00")
            open_panel(page, earliest["id"])
            expect(page.locator("#time-input")).to_have_value(earliest["scheduled_time"][:16])
            expect(page.locator("#panel-meta")).to_contain_text("10:00")
            label = page.locator('label[for="time-input"]').inner_text()
            assert "Kyiv time" in label, f"the time label reads {label!r}"
            page.fill("#time-input", wanted)
            with board_call(page, "PATCH", path) as info:
                page.click("#save")
            assert_board_call(smoke, info.value)
            assert info.value.request.post_data_json == {"scheduled_time": wanted}, info.value.request.post_data
            saved = detail(smoke, earliest["id"])
            assert saved["scheduled_time"][:16] == wanted, f"saved {saved['scheduled_time']}, wanted {wanted} Kyiv"
            assert kyiv(saved["scheduled_time"]).utcoffset() == kyiv(earliest["scheduled_time"]).utcoffset(), (
                f"saved {saved['scheduled_time']} is not in Kyiv's offset"
            )
            expect(row(page, earliest["id"]).locator(".time")).to_have_text("11:30")
    finally:
        context.close()


def check_caption_bold_round_trip(smoke: Smoke):
    item = pick(smoke, caption="Tip of the day: back up your photos.")
    path = f"/api/posts/{item['id']}"
    bold = "Tip of the day: <b>back up</b> your photos."
    linked = 'Tip of the day: <b>back up</b> your <a href="https://example.com/tips">photos</a>.'
    final = '<i>Tip</i> of the day: <b>back up</b> your <a href="https://example.com/tips">photos</a>.'
    with smoke.page() as page:
        open_panel(page, item["id"])
        caption = page.locator("#caption-input")
        preview = page.locator("#caption-preview")
        select_caption_text(page, "back up")
        page.click('[data-fmt="b"]')
        expect(caption).to_have_value(bold)
        expect(preview.locator("b")).to_have_text("back up")
        select_caption_text(page, "photos")
        page.click('[data-fmt="link"]')
        page.fill("#link-url", "example.com/tips")
        page.click("#link-apply")
        expect(caption).to_have_value(linked)
        expect(preview.locator("a")).to_have_attribute("href", "https://example.com/tips")
        # The same button again takes the tag off the selected text.
        select_caption_text(page, "your")
        page.click('[data-fmt="b"]')
        expect(caption).to_have_value(linked.replace("your", "<b>your</b>"))
        page.click('[data-fmt="b"]')
        expect(caption).to_have_value(linked)
        select_caption_text(page, "Tip")
        caption.press("Control+i")
        expect(caption).to_have_value(final)
        expect(preview.locator("i")).to_have_text("Tip")
        with board_call(page, "PATCH", path) as info:
            page.click("#save")
        assert_board_call(smoke, info.value)
        assert info.value.request.post_data_json == {"caption": final}, info.value.request.post_data
        expect(page.locator("#edit-panel")).to_be_hidden()
        saved = detail(smoke, item["id"])
        assert saved["caption_tagged"] == final, f"saved {saved['caption_tagged']!r}"
        assert saved["caption_text"] == item["caption_text"], f"the plain text changed: {saved['caption_text']!r}"
        page.reload()
        wait_for_list(page)
        open_panel(page, item["id"])
        expect(caption).to_have_value(final)
        expect(preview.locator("b")).to_have_text("back up")


def check_formatted_caption_keeps_entities(smoke: Smoke):
    """Changing another field leaves the caption alone, and a typo fix keeps every tag."""
    item = pick(smoke, startswith="Big news:")
    path = f"/api/posts/{item['id']}"
    original = detail(smoke, item["id"])["caption_tagged"]
    for tag in ("<b>Big news:</b>", '<a href="https://example.com">', '<tg-emoji emoji-id="5368324170671202286">'):
        assert tag in original, f"the seeded formatted caption lacks {tag}: {original!r}"
    other = BETA if item["channel_id"] == ALPHA else ALPHA
    with smoke.page() as page:
        open_panel(page, item["id"])
        preview = page.locator("#caption-preview")
        expect(preview.locator(".tg-emoji")).to_have_count(1)
        expect(preview.locator("a")).to_have_attribute("href", "https://example.com")
        page.select_option("#channel-select", other)
        with board_call(page, "PATCH", path) as info:
            page.click("#save")
        assert_board_call(smoke, info.value)
        assert info.value.request.post_data_json == {"channel_id": other}, info.value.request.post_data
        saved = detail(smoke, item["id"])
        assert saved["channel_id"] == other and saved["caption_tagged"] == original, saved
        expect(row(page, item["id"]).locator(".channel-chip")).to_have_text(saved["channel_name"])
        # A typo fix elsewhere in the text, with the channel moved back: every tag stays.
        open_panel(page, item["id"])
        edited = original.replace("is live", "is live now")
        assert edited != original, "the seeded caption no longer says 'is live'"
        page.fill("#caption-input", edited)
        page.select_option("#channel-select", item["channel_id"])
        with board_call(page, "PATCH", path) as info:
            page.click("#save")
        assert_board_call(smoke, info.value)
        sent = info.value.request.post_data_json
        assert sent == {"caption": edited, "channel_id": item["channel_id"]}, info.value.request.post_data
    saved = detail(smoke, item["id"])
    assert saved["caption_tagged"] == edited, f"the typo fix lost formatting: {saved['caption_tagged']!r}"
    assert saved["channel_id"] == item["channel_id"], saved["channel_id"]


def check_invalid_tag_blocks_save(smoke: Smoke):
    item = pick(smoke, caption="Fresh bread, every morning from 8.")
    with smoke.page() as page:
        open_panel(page, item["id"])
        caption = page.locator("#caption-input")
        error = page.locator("#panel-error")
        save = page.locator("#save")
        caption.fill("<b>x")
        expect(error).to_contain_text("<b> is not closed")
        expect(save).to_be_disabled()
        expect(caption).to_have_attribute("aria-invalid", "true")
        caption.fill("<b>x</b>")
        expect(error).to_be_hidden()
        expect(save).to_be_enabled()
        expect(page.locator("#caption-preview b")).to_have_text("x")
        caption.fill("x" * (CAPTION_LIMIT + 1))
        expect(error).to_contain_text(f"the limit is {CAPTION_LIMIT}")
        expect(page.locator("#caption-counter")).to_have_text(f"{CAPTION_LIMIT + 1} / {CAPTION_LIMIT}")
        expect(save).to_be_disabled()
        discard_and_close(page)
    assert detail(smoke, item["id"])["caption_text"] == item["caption_text"], "a refused caption was saved"


def check_literal_angle_and_ampersand(smoke: Smoke):
    item = pick(smoke, caption="Sale ends Sunday at midnight.")
    path = f"/api/posts/{item['id']}"
    typed = "I <3 you & AT&T"
    with smoke.page() as page:
        open_panel(page, item["id"])
        page.fill("#caption-input", typed)
        expect(page.locator("#caption-preview")).to_have_text(typed)
        expect(page.locator("#caption-counter")).to_have_text(f"{len(typed)} / {CAPTION_LIMIT}")
        expect(page.locator("#panel-error")).to_be_hidden()
        with board_call(page, "PATCH", path) as info:
            page.click("#save")
        assert_board_call(smoke, info.value)
        saved = detail(smoke, item["id"])
        assert saved["caption_text"] == typed, f"saved {saved['caption_text']!r}"
        expect(row(page, item["id"]).locator(".snippet")).to_have_text(typed)
        page.reload()
        wait_for_list(page)
        open_panel(page, item["id"])
        # The editor holds tagged text, so the reloaded caption shows its characters escaped.
        expect(page.locator("#caption-input")).to_have_value(saved["caption_tagged"])
        expect(page.locator("#caption-preview")).to_have_text(typed)
        expect(page.locator("#save")).to_be_disabled()  # nothing changed since the save


def check_preview_debounced_and_stale_ignored(smoke: Smoke):
    item = pick(smoke, caption="Our team says thank you for 10,000 followers!")
    sent: list[str] = []
    held: list = []
    hold = {"next": False}

    def control(route):
        sent.append(route.request.post_data_json["caption"])
        if hold["next"]:
            hold["next"] = False
            held.append(route)
        else:
            route.continue_()

    with smoke.page() as page:
        open_panel(page, item["id"])
        page.route(lambda url: urlsplit(url).path == "/api/caption/preview", control)
        caption = page.locator("#caption-input")
        preview = page.locator("#caption-preview")
        caption.fill("")
        caption.press_sequentially("typed quickly", delay=30)
        page.wait_for_timeout(700)
        assert sent == ["typed quickly"], f"preview requests while typing: {sent}"
        expect(preview).to_have_text("typed quickly")
        # An answer that arrives after a newer one is dropped.
        hold["next"] = True
        caption.fill("first <b>draft</b>")
        page.wait_for_timeout(500)
        assert len(held) == 1, f"the first preview request was not held: {sent}"
        caption.fill("second")
        expect(preview).to_have_text("second")
        held[0].fulfill(json={"ok": True, "html": "<b>stale</b>", "length": 5, "limit": CAPTION_LIMIT,
                              "signature_fits": None, "error": None})
        page.wait_for_timeout(400)
        expect(preview).to_have_text("second")
        expect(page.locator("#caption-counter")).to_have_text(f"6 / {CAPTION_LIMIT}")
        discard_and_close(page)


def check_signature_note(smoke: Smoke):
    item = pick(smoke, caption="New arrivals are in. Come and see them this week.", channel=ALPHA)
    with smoke.page() as page:
        open_panel(page, item["id"])
        note = page.locator("#signature-note")
        suffix = page.locator("#preview-signature")
        expect(note).to_be_visible()
        expect(note).to_contain_text("signature")
        expect(suffix).to_have_text(ALPHA_SIGNATURE)
        # Caption and signature together no longer fit: the signature is left out.
        page.fill("#caption-input", "x" * (CAPTION_LIMIT - len(ALPHA_SIGNATURE)))
        expect(note).to_contain_text("left out")
        expect(suffix).to_be_hidden()
        expect(page.locator("#save")).to_be_enabled()
        page.select_option("#channel-select", BETA)  # Beta has no signature
        expect(note).to_be_hidden()
        discard_and_close(page)


def check_unschedule_moves_to_queued(smoke: Smoke):
    item = pick(smoke, caption="Workshop seats are almost gone.")
    path = f"/api/posts/{item['id']}"
    before = smoke.api("/api/me")["counts"]
    with smoke.page() as page:
        open_panel(page, item["id"])
        page.click("#unschedule")
        expect(page.locator("#time-input")).to_have_value("")
        with board_call(page, "PATCH", path) as info:
            page.click("#save")
        assert_board_call(smoke, info.value)
        assert info.value.request.post_data_json == {"scheduled_time": None}, info.value.request.post_data
        expect(row(page, item["id"])).to_have_count(0)
        saved = detail(smoke, item["id"])
        assert saved["status"] == "queued" and saved["scheduled_time"] is None, saved
        after = smoke.api("/api/me")["counts"]
        assert (after["scheduled"], after["queued"]) == (before["scheduled"] - 1, before["queued"] + 1), (before, after)
        for tab in ("scheduled", "queued"):
            expect(page.locator(f'[data-tab="{tab}"] .count')).to_have_text(str(after[tab]))
        page.click('[data-tab="queued"]')
        wait_for_list(page)
        expect(row(page, item["id"])).to_have_count(1)


def check_post_now_sets_one_minute_ahead(smoke: Smoke):
    """Post now sets now + 60 s; unsaved changes are saved first, never lost."""
    item = pick(smoke, status="queued", startswith="Queued idea #1:")
    path = f"/api/posts/{item['id']}"
    new_caption = "Queued idea #1, ready to go"
    with smoke.page("#tab=queued") as page:
        open_panel(page, item["id"])
        post_now = page.locator("#post-now")
        expect(page.locator("#retry")).to_be_hidden()
        expect(post_now).to_have_text("Post now")
        page.fill("#caption-input", new_caption)
        expect(post_now).to_have_text("Save & post now")
        before = kyiv(smoke.api("/api/me")["now"])
        with board_call(page, "PATCH", path) as edit, board_call(page, "POST", f"{path}/post-now") as info:
            post_now.click()
        after = kyiv(smoke.api("/api/me")["now"])
        assert_board_call(smoke, edit.value)
        assert edit.value.request.post_data_json == {"caption": new_caption}, edit.value.request.post_data
        assert_board_call(smoke, info.value)
        saved = detail(smoke, item["id"])
        assert saved["status"] == "scheduled" and saved["caption_text"] == new_caption, saved
        when = kyiv(saved["scheduled_time"])
        assert before + timedelta(seconds=59) <= when <= after + timedelta(seconds=61), (
            f"post now set {saved['scheduled_time']}; the server clock read {before} .. {after}"
        )
        expect(page.locator("#edit-panel")).to_be_hidden()
        expect(page.locator("#toast")).to_contain_text(saved["scheduled_time"][11:16])
        expect(row(page, item["id"])).to_have_count(0)


def check_failed_retry(smoke: Smoke):
    item = next(i for i in smoke.all_items("failed") if i["failure_reason"].startswith("Forbidden"))
    path = f"/api/posts/{item['id']}"
    with smoke.page("#tab=failed") as page:
        open_panel(page, item["id"])
        retry = page.locator("#retry")
        expect(page.locator("#edit-panel")).to_contain_text(item["failure_reason"])
        expect(retry).to_be_visible()
        expect(retry).to_be_enabled()
        expect(page.locator("#post-now")).to_be_hidden()
        expect(page.locator("#save")).to_be_visible()
        before = kyiv(smoke.api("/api/me")["now"])
        with board_call(page, "POST", f"{path}/retry") as info:
            retry.click()
        after = kyiv(smoke.api("/api/me")["now"])
        assert_board_call(smoke, info.value)
        saved = detail(smoke, item["id"])
        assert saved["status"] == "scheduled" and saved["retry_count"] == 0 and saved["failure_reason"] is None, saved
        when = kyiv(saved["scheduled_time"])
        assert before + timedelta(seconds=9) <= when <= after + timedelta(seconds=11), (
            f"retry set {saved['scheduled_time']}; the server clock read {before} .. {after}"
        )
        expect(page.locator("#edit-panel")).to_be_hidden()
        expect(row(page, item["id"])).to_have_count(0)
        expect(page.locator("#toast")).to_contain_text(saved["scheduled_time"][11:16])
        counts = smoke.api("/api/me")["counts"]
        expect(page.locator('[data-tab="failed"] .count')).to_have_text(str(counts["failed"]))


def check_conflict_reloads_panel(smoke: Smoke):
    """A 409 shows its message, reloads the panel and refreshes the row."""
    item = next(i for i in smoke.all_items("failed") if i["failure_reason"].startswith("Bad Request"))
    path = f"/api/posts/{item['id']}"
    with smoke.page("#tab=failed", expect_failed_requests=True) as page:
        open_panel(page, item["id"])
        board_post(smoke, f"{path}/retry")  # retried from somewhere else meanwhile
        with board_call(page, "POST", f"{path}/retry") as info, board_call(page, "GET", path) as reload:
            page.click("#retry")
        assert_board_call(smoke, info.value, expected=409)
        body = info.value.json()
        assert body["error"] == "conflict" and body["status"] == "scheduled", body
        assert reload.value.ok, f"the reload answered {reload.value.status}"
        expect(page.locator("#toast")).to_contain_text(body["message"])
        wait_for_panel(page, item["id"])
        expect(page.locator("#panel-meta")).to_contain_text("Scheduled")
        expect(page.locator("#retry")).to_be_hidden()
        expect(page.locator("#post-now")).to_be_visible()
        expect(row(page, item["id"])).to_have_count(0)  # no longer a failed post


def check_replace_media_and_as_file(smoke: Smoke):
    item = pick(smoke, caption="Rainy day playlist, link in the bio.", media_type="photo")
    path = f"/api/posts/{item['id']}/media"
    with smoke.page() as page:
        page.evaluate(RECORD_UPLOADS_JS)
        open_panel(page, item["id"])
        as_file = page.locator("#as-file")
        expect(as_file).to_be_visible()
        as_file.check()
        choose_upload(page, "sent-as-file")
        expect(page.locator("#panel-media")).to_contain_text("sent-as-file.png")
        with board_call(page, "PUT", path) as info:
            page.click("#save")
        assert_board_call(smoke, info.value)
        assert last_upload_as_file(page) == "1"
        # The server reads as_file only before the file, so "document" proves the order too.
        saved = detail(smoke, item["id"])
        assert saved["media_type"] == "document" and saved["media"][0]["exists"], saved["media"]
        expect(row(page, item["id"]).locator(".type-icon")).to_be_visible()
        open_panel(page, item["id"])
        expect(as_file).to_be_checked()  # the post is a file now, so new media would be one too
        as_file.uncheck()  # back to a photo
        choose_upload(page, "new-photo")

        def with_notice(route):
            # The server's notice for a photo it stored as a file (a real one needs a 10 MB photo).
            if route.request.method != "PUT":
                route.continue_()
                return
            response = route.fetch()
            route.fulfill(response=response, json={**response.json(), "notice": DOWNGRADE_NOTICE})

        def media_url(url):
            return urlsplit(url).path == path

        # The route stays until the page closes. Removing it turns request interception
        # off, and a request in flight at that moment (here, often the new thumbnail the
        # row asks for right after the save) can hang for good: image_loaded() below then
        # timed out in about 1 of 25-70 runs. Other requests do not match it and go through.
        page.route(media_url, with_notice)
        with board_call(page, "PUT", path) as info:
            page.click("#save")
        assert_board_call(smoke, info.value)
        assert last_upload_as_file(page) == "0"
        expect(page.locator("#toast")).to_contain_text(DOWNGRADE_NOTICE)
        saved = detail(smoke, item["id"])
        assert saved["media_type"] == "photo" and saved["media"][0]["exists"], saved["media"]
        # The row shows the new picture, not a cached copy of the old one.
        image = row(page, item["id"]).locator("img.thumb")
        expect(image).to_have_attribute("src", re.compile(r"&v=\d+$"))
        image_loaded(page, image)
        open_panel(page, item["id"])
        image_loaded(page, page.locator("#panel-media img"))


def check_replace_media_on_post_with_missing_file(smoke: Smoke):
    """Review Focus 3: a post whose file is gone opens with a placeholder and takes new media."""
    queued = smoke.all_items("queued")
    missing = [item for item in queued if not detail(smoke, item["id"])["media"][0]["exists"]]
    assert len(missing) == 1, f"queued posts without a file: {[i['id'] for i in missing]}; the seed has 1"
    item = missing[0]
    with smoke.page("#tab=queued") as page:
        expect(row(page, item["id"]).locator(".type-icon")).to_be_visible()
        open_panel(page, item["id"])
        media = page.locator("#panel-media")
        expect(media).to_contain_text("File no longer available")
        expect(media.locator("img")).to_have_count(0)
        choose_upload(page, "replacement")
        with board_call(page, "PUT", f"/api/posts/{item['id']}/media") as info:
            page.click("#save")
        assert_board_call(smoke, info.value)
        saved = detail(smoke, item["id"])
        assert saved["media"][0]["exists"] and saved["media_type"] == "photo", saved["media"]
        image = row(page, item["id"]).locator("img.thumb")
        image_loaded(page, image)


def check_send_as_file_starts_from_the_post(smoke: Smoke):
    """"Send as a file" starts ticked for a post that is a file now (an image, a video or any other
    file sent as a file), so new media keeps that form unless it is unticked; a photo starts
    unticked. The uploads are answered in the page with the post as stored, so no post changes."""
    items = first_page(smoke)
    photo = pick(smoke, media_type="photo")
    image_file = pick(smoke, media_type="document_image")
    document = pick(smoke, media_type="document")
    video_file = next(i for i in items if i["media_type"] == "photo" and i["id"] != photo["id"])

    def as_video_file(route):
        # The seed has no video sent as a file: this photo post's detail says it is one.
        response = route.fetch()
        body = response.json()
        body["media_type"] = "document_video"
        for media in body["media"]:
            media["media_type"] = "document_video"
        route.fulfill(response=response, json=body)

    def answered_upload(route):
        if route.request.method != "PUT":
            route.continue_()
            return
        route.fulfill(json=detail(smoke, int(urlsplit(route.request.url).path.split("/")[3])))

    with smoke.page() as page:
        page.evaluate(RECORD_UPLOADS_JS)
        # Both routes stay until the page closes (see check_replace_media_and_as_file).
        page.route(lambda url: re.fullmatch(r"/api/posts/\d+/media", urlsplit(url).path) is not None, answered_upload)
        page.route(lambda url: urlsplit(url).path == f"/api/posts/{video_file['id']}", as_video_file)
        panel = page.locator("#edit-panel")
        as_file = page.locator("#as-file")
        for item, ticked in ((photo, False), (document, True), (video_file, True)):
            open_panel(page, item["id"])
            if ticked:
                expect(as_file).to_be_checked()
            else:
                expect(as_file).not_to_be_checked()
            page.keyboard.press("Escape")  # nothing changed, so it closes without asking
            expect(panel).to_be_hidden()
        # New media for an image sent as a file goes as a file too, without touching the box...
        open_panel(page, image_file["id"])
        expect(as_file).to_be_checked()
        choose_upload(page, "still-a-file")
        with board_call(page, "PUT", f"/api/posts/{image_file['id']}/media") as info:
            page.click("#save")
        assert_board_call(smoke, info.value)
        assert last_upload_as_file(page) == "1", "new media for a post sent as a file was sent as a photo"
        expect(panel).to_be_hidden()
        # ...and unticking the box sends it as a photo.
        open_panel(page, document["id"])
        as_file.uncheck()
        choose_upload(page, "now-a-photo")
        with board_call(page, "PUT", f"/api/posts/{document['id']}/media"):
            page.click("#save")
        assert last_upload_as_file(page) == "0", "an unticked box still sent the media as a file"
    for item in (image_file, document):
        assert detail(smoke, item["id"])["media_type"] == item["media_type"], f"post {item['id']} changed"


def check_unschedule_hint_by_status(smoke: Smoke):
    """After Unschedule the hint says what saving does: a scheduled post moves to Queued, a failed
    post stays in Failed (spec 6.1), where Retry sends it. Nothing is saved."""
    failed = first_page(smoke, "failed")[0]
    assert failed["scheduled_time"], "the failed post has no time to clear; the seed changed"
    with smoke.page("#tab=failed") as page:
        open_panel(page, failed["id"])
        page.click("#unschedule")
        hint = page.locator("#time-hint")
        expect(hint).to_contain_text("No time: saving keeps the post in Failed. Retry sends it right away.")
        expect(hint).not_to_contain_text("Queued")
        discard_and_close(page)
    scheduled = [item for item in first_page(smoke) if not item["overdue"]][1]
    with smoke.page() as page:
        open_panel(page, scheduled["id"])
        page.click("#unschedule")
        expect(page.locator("#time-hint")).to_contain_text("No time: saving moves the post to Queued.")
        discard_and_close(page)
    assert detail(smoke, failed["id"])["scheduled_time"] == failed["scheduled_time"], "a discarded change was saved"


def check_caption_shortcuts_on_other_layouts(smoke: Smoke):
    """Ctrl+B, I, U and K format the caption on a Ukrainian layout too (Ctrl+И, Ш, Г, Л: the same
    keys), and the browser's own action for them (Ctrl+U is view-source) does not run; the
    Latin keys still work. Nothing is saved."""
    item = pick(smoke, caption="Behind the scenes at the studio.")
    with smoke.page() as page:
        page.evaluate(WATCH_KEYS_JS)
        cdp = page.context.new_cdp_session(page)
        open_panel(page, item["id"])
        caption = page.locator("#caption-input")
        expected = item["caption_text"]
        for word, letter in (("Behind", "b"), ("scenes", "i"), ("studio", "u")):
            key, code, vk = UKRAINIAN[letter]
            select_caption_text(page, word)
            press_on_layout(cdp, key, code, vk)
            pressed = keys_seen(page, key)[-1]
            assert pressed["prevented"], f"Ctrl+{key.upper()} (Ctrl+{letter.upper()} on a Ukrainian layout) was left to the browser"
            expected = expected.replace(word, f"<{letter}>{word}</{letter}>", 1)
            expect(caption).to_have_value(expected)
        key, code, vk = UKRAINIAN["k"]
        select_caption_text(page, "the")
        press_on_layout(cdp, key, code, vk)
        assert keys_seen(page, key)[-1]["prevented"], "Ctrl+Л (Ctrl+K) was left to the browser"
        expect(page.locator("#link-row")).to_be_visible()
        expect(page.locator("#link-url")).to_be_focused()
        page.keyboard.press("Escape")  # closes just the link field
        expect(page.locator("#link-row")).to_be_hidden()
        expect(caption).to_be_focused()
        # The Latin keys, as before.
        select_caption_text(page, "at")
        caption.press("Control+b")
        expected = expected.replace(" at ", " <b>at</b> ", 1)
        expect(caption).to_have_value(expected)
        discard_and_close(page)
    assert detail(smoke, item["id"])["caption_text"] == item["caption_text"], "a discarded caption was saved"


def check_write_in_flight_keeps_panel(smoke: Smoke):
    """While a save is in flight Esc, the scrim and Close do nothing; a failed save leaves the
    panel on the same post with every input intact; the save sends what the form held when
    Save was pressed, whatever happens to the form meanwhile."""
    item = pick(smoke, caption="Fresh bread, every morning from 8.")
    path = f"/api/posts/{item['id']}"
    typed = "Fresh bread, every morning from 7."
    held: list = []
    prompts: list[str] = []

    def hold_patch(route):
        if route.request.method == "PATCH":
            held.append(route)
        else:
            route.continue_()

    with smoke.page(expect_failed_requests=True) as page:
        page.evaluate(RECORD_UPLOADS_JS)
        page.on("dialog", lambda dialog: (prompts.append(dialog.message), dialog.dismiss()))
        open_panel(page, item["id"])
        page.route(lambda url: urlsplit(url).path == path, hold_patch)
        panel = page.locator("#edit-panel")
        caption = page.locator("#caption-input")
        caption.fill(typed)
        page.check("#as-file")
        choose_upload(page, "original")
        page.click("#save")
        page.wait_for_function("() => document.querySelector('#save').textContent === 'Saving…'")
        page.wait_for_timeout(200)
        assert len(held) == 1, "the PATCH was not held"
        page.keyboard.press("Escape")
        page.mouse.click(40, 400)  # the scrim beside the side panel
        page.wait_for_timeout(200)
        assert panel.is_visible(), "the panel closed while its save was in flight"
        assert not prompts, f"closing during a save asked: {prompts}"
        expect(page.locator("#panel-close")).to_be_disabled()
        # The save fails: the message shows in the panel, which still holds this post and its input.
        held.pop().fulfill(status=400, json={"error": "invalid_request",
                                             "message": "Scheduled time must be at least 60 seconds in the future"})
        expect(page.locator("#panel-error")).to_contain_text("at least 60 seconds")
        expect(panel).to_have_attribute("data-post-id", str(item["id"]))
        expect(caption).to_have_value(typed)
        expect(page.locator("#as-file")).to_be_checked()
        expect(page.locator("#panel-media")).to_contain_text("original.png")
        expect(page.locator("#panel-close")).to_be_enabled()
        expect(page.locator("#save")).to_be_enabled()
        # Saved again; the form changes while the PATCH is out, the upload still sends what was chosen.
        page.click("#save")
        page.wait_for_timeout(200)
        assert len(held) == 1, "the second PATCH was not held"
        page.locator("#as-file").evaluate("box => { box.checked = false; }")
        with board_call(page, "PUT", f"{path}/media") as info:
            held.pop().continue_()
        assert_board_call(smoke, info.value)
        assert last_upload_as_file(page) == "1", "the upload read the form after the save had started"
        expect(panel).to_be_hidden()
    saved = detail(smoke, item["id"])
    assert saved["caption_text"] == typed and saved["media_type"] == "document", saved
    assert not prompts, f"prompts: {prompts}"


def check_album_media_read_only(smoke: Smoke):
    album = pick(smoke, media_type="album")
    with smoke.page() as page:
        open_panel(page, album["id"])
        items = page.locator("#panel-media .media-strip .media-item")
        expect(items).to_have_count(album["media_count"])
        for index in range(album["media_count"]):
            image = items.nth(index).locator("img")
            expect(image).to_have_attribute("src", re.compile(rf"^/api/posts/{album['id']}/thumb\?i={index}&"))
            image_loaded(page, image)
        for control in ("#media-replace", "#replace-media-input", "#as-file"):
            expect(page.locator(control)).to_be_hidden()
        expect(page.locator("#panel-media")).to_contain_text("Album")
        expect(page.locator("#caption-input")).to_be_editable()
        expect(page.locator("#save")).to_be_visible()


def check_read_only_panel(smoke: Smoke):
    """Posted and sending posts open read-only; only what `can` allows is offered."""
    posted = first_page(smoke, "posted")[0]
    edit_only = ("#save", "#post-now", "#retry", "#unschedule", "#media-replace", '[data-fmt="b"]')
    with smoke.page("#tab=posted") as page:
        open_panel(page, posted["id"])
        for control in edit_only:
            expect(page.locator(control)).to_be_hidden()
        expect(page.locator("#delete")).to_be_visible()
        expect(page.locator("#caption-input")).not_to_be_editable()
        expect(page.locator("#channel-select")).to_be_disabled()
        expect(page.locator("#time-input")).to_be_disabled()
        expect(page.locator("#edit-panel")).to_contain_text("stays in the channel")
        expect(page.locator("#edit-panel")).to_have_accessible_name("Post details")
    item = pick(smoke, caption="Morning coffee and a quiet street.")

    def sending(route):
        response = route.fetch()
        body = response.json()
        body.update(status="sending", can={action: False for action in body["can"]})
        route.fulfill(response=response, json=body)

    with smoke.page() as page:
        page.route(lambda url: urlsplit(url).path == f"/api/posts/{item['id']}", sending)
        open_panel(page, item["id"])
        for control in (*edit_only, "#delete"):
            expect(page.locator(control)).to_be_hidden()
        expect(page.locator("#caption-input")).not_to_be_editable()
        expect(page.locator("#edit-panel")).to_contain_text("being sent")


def check_delete_from_panel(smoke: Smoke):
    """Deleting a posted post asks first (only the record goes); other deletes do not ask."""
    prompts: list[tuple[str, str]] = []
    posted = first_page(smoke, "posted")[0]
    path = f"/api/posts/{posted['id']}"
    with smoke.page("#tab=posted") as page:
        open_panel(page, posted["id"])
        page.once("dialog", lambda dialog: (prompts.append((dialog.type, dialog.message)), dialog.dismiss()))
        page.click("#delete")
        expect(page.locator("#edit-panel")).to_be_visible()
        assert prompts and prompts[0][0] == "confirm" and POSTED_DELETE_PROMPT in prompts[0][1], prompts
        assert smoke.context.request.get(smoke.base + path).status == 200, "a declined delete went through"
        page.once("dialog", lambda dialog: dialog.accept())
        with board_call(page, "DELETE", path) as info:
            page.click("#delete")
        assert_board_call(smoke, info.value)
        expect(page.locator("#edit-panel")).to_be_hidden()
        expect(row(page, posted["id"])).to_have_count(0)
        expect(page.locator("#toast")).to_contain_text("deleted")
        assert smoke.context.request.get(smoke.base + path).status == 404, "the deleted post still opens"
        counts = smoke.api("/api/me")["counts"]
        expect(page.locator('[data-tab="posted"] .count')).to_have_text(str(counts["posted"]))
    item = pick(smoke, caption="Question of the week: which colour should we stock next?")
    with smoke.page() as page:
        page.on("dialog", lambda dialog: (prompts.append((dialog.type, dialog.message)), dialog.dismiss()))
        open_panel(page, item["id"])
        with board_call(page, "DELETE", f"/api/posts/{item['id']}") as info:
            page.click("#delete")
        assert_board_call(smoke, info.value)
        expect(row(page, item["id"])).to_have_count(0)
    assert len(prompts) == 1, f"a scheduled post's delete asked: {prompts[1:]}"


def check_escape_prompts_when_dirty(smoke: Smoke):
    item = pick(smoke, caption="Sunset over the river, straight from the camera.")
    prompts: list[str] = []
    with smoke.page() as page:
        panel = page.locator("#edit-panel")
        open_panel(page, item["id"])
        caption = page.locator("#caption-input")
        caption.press("End")
        caption.press_sequentially(" Draft")
        page.once("dialog", lambda dialog: (prompts.append(dialog.message), dialog.dismiss()))
        page.keyboard.press("Escape")
        expect(panel).to_be_visible()
        expect(caption).to_have_value(item["caption_text"] + " Draft")
        assert prompts and DISCARD_PROMPT in prompts[0].lower(), f"prompts: {prompts}"
        # Back to the saved text: nothing to lose, so no question.
        caption.fill(item["caption_text"])
        page.keyboard.press("Escape")
        expect(panel).to_be_hidden()
        assert len(prompts) == 1, f"prompts: {prompts}"
        open_panel(page, item["id"])
        page.select_option("#channel-select", BETA if item["channel_id"] == ALPHA else ALPHA)
        page.once("dialog", lambda dialog: (prompts.append(dialog.message), dialog.accept()))
        page.keyboard.press("Escape")
        expect(panel).to_be_hidden()
        assert len(prompts) == 2, f"a changed channel did not ask: {prompts}"
    saved = detail(smoke, item["id"])
    assert (saved["caption_text"], saved["channel_id"]) == (item["caption_text"], item["channel_id"]), "a discarded edit was saved"


def check_panel_layout(smoke: Smoke):
    """A side panel from 900 px wide, a bottom sheet below (centred, at most 720 px wide);
    it fits a 360 px phone."""
    item = pick(smoke, startswith="Big news:")
    layouts = (
        ("side", DESKTOP),
        ("side", {"viewport": {"width": 900, "height": 700}}),
        ("sheet", {"viewport": {"width": 899, "height": 700}}),
        ("sheet", PHONE),
    )
    for kind, device in layouts:
        width, height = device["viewport"]["width"], device["viewport"]["height"]
        context = smoke.new_context(**device)
        try:
            with smoke.page(context=context) as page:
                open_panel(page, item["id"])
                box = page.locator("#edit-panel").bounding_box()
                where = f"{width}x{height}: panel at {box}"
                assert abs(box["y"] + box["height"] - height) < 1, f"{where} does not reach the bottom"
                if kind == "side":
                    assert abs(box["x"] + box["width"] - width) < 1 and box["y"] < 1, f"{where} is not a side panel"
                    assert 360 <= box["width"] <= 560, f"{where} has an odd width"
                else:
                    centred = abs(box["x"] - (width - box["width"]) / 2) < 1
                    assert centred and abs(box["width"] - min(width, 720)) < 1 and box["y"] > 0, f"{where} is not a bottom sheet"
                problems = page.evaluate(PANEL_FIT_JS)
                assert not problems, f"{width}px: {problems[0]} ({len(problems)} problems)"
                save = page.locator("#save").bounding_box()
                assert save and save["y"] + save["height"] <= height, f"{width}px: Save is off screen at {save}"
        finally:
            context.close()


# --- bulk actions, undo, empty states, accessibility -------------------------------------

EMPTY_NO_CHANNELS = "No channels yet. Add one in Telegram with /start."
EMPTY_TAB = "Nothing here yet."
NO_MATCH = "No posts match these filters."
BULK_MAX = 500  # the server's limit per bulk request, so also the board's per selection
UNDO_MS = 10_000
SHIFT_CONTROLS = ("#bulk-shift-amount", "#bulk-shift-unit", "#bulk-shift-apply")
# The day ("2026-11-04") of the header a row sits under, or null.
ROW_DAY_JS = """(id) => {
    let node = document.querySelector(`#post-list .post-row[data-id="${id}"]`);
    while (node && !node.classList.contains('day-header')) node = node.previousElementSibling;
    return node ? node.dataset.day : null;
}"""
# Problems with the bulk bar at this width: the page scrolls sideways, a control reaches
# past an edge or out of the bar, a button's or the count's text is cut, or the bar is
# taller than its layout (then toasts, which sit above that height, would cover it).
BAR_FIT_JS = """() => {
    const problems = [];
    const root = document.documentElement;
    const bar = document.querySelector('#bulk-bar');
    const edge = bar.getBoundingClientRect();
    if (root.scrollWidth > root.clientWidth) problems.push(`the page is ${root.scrollWidth}px wide`);
    const planned = parseFloat(getComputedStyle(bar).minHeight);
    if (edge.height > planned + 0.5) problems.push(`the bar is ${edge.height}px tall, its layout ${planned}px`);
    for (const node of document.querySelectorAll('#bulk-bar button, #bulk-bar input, #bulk-bar select, #bulk-bar label, #bulk-count')) {
        const box = node.getBoundingClientRect();
        if (box.width === 0 || node.classList.contains('visually-hidden')) continue;
        if (box.left < Math.max(0, edge.left) - 0.5 || box.right > Math.min(root.clientWidth, edge.right) + 0.5) {
            problems.push(`${node.id || node.tagName} spans ${Math.round(box.left)}..${Math.round(box.right)}px`);
        }
        if ((node.tagName === 'BUTTON' || node.id === 'bulk-count') && node.scrollWidth > node.clientWidth + 1) {
            problems.push(`${node.id || node.tagName} is cut to ${node.clientWidth}px of ${node.scrollWidth}px`);
        }
    }
    return problems;
}"""


def select_rows(page, ids) -> None:
    for post_id in ids:
        row(page, post_id).locator(".row-select").check()


def bulk_call(page):
    return board_call(page, "POST", "/api/posts/bulk")


def tab_count(page, tab: str) -> int:
    return int(page.locator(f'[data-tab="{tab}"] .count').inner_text())


def board_patch(smoke: Smoke, path: str, body: dict) -> dict:
    """An edit made outside the page (another tab, say), with the headers the board requires."""
    response = smoke.context.request.patch(smoke.base + path, data=body, headers={"Origin": smoke.base, "X-Board": "1"})
    assert response.ok, f"PATCH {path} answered {response.status}"
    return response.json()


def kyiv_today(smoke: Smoke) -> date:
    return date.fromisoformat(smoke.api("/api/me")["now"][:10])


def posts_on(smoke: Smoke, day: date) -> list[dict]:
    """The scheduled posts of one Kyiv day, in list order."""
    return [item for item in smoke.all_items("scheduled") if item["scheduled_time"][:10] == day.isoformat()]


def fake_item(post_id: int, caption: str, status: str = "queued") -> dict:
    """A list item the server never stored, for list states the seed cannot reach."""
    return {
        "id": post_id, "status": status, "channel_id": ALPHA, "channel_name": "Alpha", "scheduled_time": None,
        "posted_at": None, "caption_text": caption, "media_type": "document", "media_count": 1,
        "is_recurring": False, "recurring_interval_hours": None, "overdue": False, "failure_reason": None,
        "retry_count": 0,
    }


def sign_in_storage(smoke: Smoke, login_url: str) -> dict:
    """Sign in with a link in a context of its own; returns that context's storage state."""
    context = smoke.browser.new_context(timezone_id=DEFAULT_TIMEZONE)
    try:
        page = context.new_page()
        smoke.watch(page)
        page.goto(login_url)
        page.get_by_role("button", name="Continue").click()
        page.wait_for_load_state()
        if urlsplit(page.url).path != "/":
            raise AssertionError(f"sign-in at {login_url.split('?')[0]} failed; restart its dev server for a fresh link")
        return context.storage_state()
    finally:
        context.close()


def check_bulk_select_shows_bar(smoke: Smoke):
    """Checking rows shows the bar with the count; unchecking, Clear and a tab change hide it;
    Select all shown checks every selectable row; Shift works on the Scheduled tab only; the
    selection survives a refresh."""
    with smoke.page() as page:
        bar = page.locator("#bulk-bar")
        count = page.locator("#bulk-count")
        expect(bar).to_be_hidden()
        ids = row_ids(page)[:3]
        select_rows(page, ids[:2])
        expect(bar).to_be_visible()
        expect(count).to_have_text("2 selected")
        expect(bar).to_have_accessible_name("Selected posts")
        row(page, ids[0]).locator(".row-select").uncheck()
        expect(count).to_have_text("1 selected")
        for control in ("#bulk-delete", "#bulk-move", *SHIFT_CONTROLS):
            expect(page.locator(control)).to_be_enabled()
        options = page.locator("#bulk-move option").all_inner_texts()
        assert options[1:] == ["Alpha", "Beta"], f"the move choices are {options}"
        expect(page.locator("#bulk-move-apply")).to_be_disabled()  # until a channel is chosen
        page.click("#bulk-clear")
        expect(bar).to_be_hidden()
        expect(page.locator("#post-list .row-select:checked")).to_have_count(0)
        select_rows(page, ids[2:3])
        page.click("#bulk-select-all")
        selectable = page.locator("#post-list .row-select:enabled").count()
        assert selectable >= PAGE_SIZE - 5, f"only {selectable} rows can be selected"
        expect(count).to_have_text(f"{selectable} selected")
        expect(page.locator("#post-list .row-select:checked")).to_have_count(selectable)
        # Every layout of the bar fits: one line, two lines from 999 px, three from 699 px.
        for width in (1280, 1000, 999, 700, 699, 360):
            page.set_viewport_size({"width": width, "height": 720})
            problems = page.evaluate(BAR_FIT_JS)
            assert not problems, f"{width}px: {problems[0]} ({len(problems)} problems)"
        page.set_viewport_size({"width": 1280, "height": 720})
        # Another tab starts with nothing selected, and Shift is for Scheduled posts only.
        page.click('[data-tab="queued"]')
        wait_for_list(page)
        expect(bar).to_be_hidden()
        queued = row_ids(page)[0]
        select_rows(page, [queued])
        expect(count).to_have_text("1 selected")
        for control in SHIFT_CONTROLS:
            expect(page.locator(control)).to_be_disabled()
        for control in ("#bulk-delete", "#bulk-move"):
            expect(page.locator(control)).to_be_enabled()
        with page.expect_response(lambda r: "/api/posts?" in r.url):
            page.click("#refresh")
        wait_for_list(page)
        expect(row(page, queued).locator(".row-select")).to_be_checked()
        expect(count).to_have_text("1 selected")


def check_bulk_shift_moves_selected(smoke: Smoke):
    """Three posts of one day shifted by +1 day land under the next day's header at the same
    times, and the API agrees."""
    day = kyiv_today(smoke) + timedelta(days=3)
    chosen = posts_on(smoke, day)[:3]
    assert len(chosen) == 3, f"{len(chosen)} posts on {day}; the seed changed"
    ids = [item["id"] for item in chosen]
    with smoke.page() as page:
        select_rows(page, ids)
        page.fill("#bulk-shift-amount", "1")
        page.select_option("#bulk-shift-unit", "days")
        with bulk_call(page) as info:
            page.click("#bulk-shift-apply")
        assert_board_call(smoke, info.value)
        sent = info.value.request.post_data_json
        assert sent == {"action": "shift", "ids": ids, "shift": {"unit": "days", "amount": 1}}, sent
        assert all(result["ok"] for result in info.value.json()["results"]), info.value.json()
        expect(page.locator("#bulk-bar")).to_be_hidden()
        expect(page.locator("#toast")).to_contain_text("Shifted 3 posts")
        expect(page.locator("#bulk-summary")).to_be_hidden()
        next_day = (day + timedelta(days=1)).isoformat()
        for item in chosen:
            saved = detail(smoke, item["id"])
            wanted = next_day + item["scheduled_time"][10:16]
            assert saved["scheduled_time"][:16] == wanted, f"post {item['id']} is at {saved['scheduled_time']}, wanted {wanted}"
            page.wait_for_function(f"(id) => ({ROW_DAY_JS})(id) === '{next_day}'", arg=item["id"])
            expect(row(page, item["id"]).locator(".time")).to_have_text(item["scheduled_time"][11:16])
            expect(row(page, item["id"]).locator(".row-select")).not_to_be_checked()


def check_bulk_partial_failure_summary(smoke: Smoke):
    """A post that turned queued after it was selected is skipped by the shift; the summary names
    it with the server's reason, and the other posts still move."""
    chosen = posts_on(smoke, kyiv_today(smoke) + timedelta(days=2))[3:6]
    assert len(chosen) == 3, "the seed changed"
    stale = chosen[1]
    with smoke.page() as page:
        select_rows(page, [item["id"] for item in chosen])
        board_patch(smoke, f"/api/posts/{stale['id']}", {"scheduled_time": None})  # unscheduled elsewhere
        page.fill("#bulk-shift-amount", "2")
        page.select_option("#bulk-shift-unit", "hours")
        with bulk_call(page) as info:
            page.click("#bulk-shift-apply")
        assert_board_call(smoke, info.value)
        results = {result["id"]: result for result in info.value.json()["results"]}
        reason = results[stale["id"]]["error"]
        assert not results[stale["id"]]["ok"] and "queued" in reason, results[stale["id"]]
        summary = page.locator("#bulk-summary")
        expect(summary).to_be_visible()
        expect(summary).to_contain_text("Shifted 2 posts")
        expect(summary).to_contain_text("1 post was skipped")
        skipped = summary.locator("li")
        expect(skipped).to_have_count(1)
        expect(skipped).to_contain_text(stale["caption_text"][:30])
        expect(skipped).to_contain_text(reason)
        for item in (chosen[0], chosen[2]):
            saved = kyiv(detail(smoke, item["id"])["scheduled_time"])
            assert saved == kyiv(item["scheduled_time"]) + timedelta(hours=2), f"post {item['id']} is at {saved}"
            expect(row(page, item["id"]).locator(".time")).to_have_text(saved.strftime("%H:%M"))
        expect(row(page, stale["id"])).to_have_count(0)  # it is queued now
        smoke.shot(page, "bulk-summary-skipped-desktop-light")
        # The bar went away with focus in it: focus is on the summary, and Esc dismisses it.
        expect(summary).to_be_focused()
        page.keyboard.press("Escape")
        expect(summary).to_be_hidden()
        expect(page.locator('[data-tab="scheduled"]')).to_be_focused()


def check_bulk_move_to_channel(smoke: Smoke):
    """Two Alpha posts moved to Beta leave the Alpha-filtered list and show up under Beta."""
    chosen = smoke.api("/api/posts", status="scheduled", channel=ALPHA)["items"][20:22]
    ids = [item["id"] for item in chosen]
    with smoke.page(f"#tab=scheduled&channel={ALPHA}") as page:
        select_rows(page, ids)
        page.select_option("#bulk-move", BETA)
        with bulk_call(page) as info:
            page.click("#bulk-move-apply")
        assert_board_call(smoke, info.value)
        assert info.value.request.post_data_json == {"action": "move_channel", "ids": ids, "channel_id": BETA}
        for post_id in ids:
            expect(row(page, post_id)).to_have_count(0)
            assert detail(smoke, post_id)["channel_id"] == BETA, f"post {post_id} did not move"
        expect(page.locator("#toast")).to_contain_text("Moved 2 posts to Beta")
        page.select_option("#channel-filter", BETA)
        wait_for_list(page)
        for post_id in ids:
            expect(row(page, post_id).locator(".channel-chip")).to_have_text("Beta")


def check_single_delete_and_undo(smoke: Smoke):
    """The row's delete button removes the post at once and offers Undo, which restores it in
    place; an overdue post comes back to Queued; a posted post asks first; the panel's Delete
    offers Undo too; the Undo toast stays 10 seconds and waits while the pointer is on it."""
    prompts: list[str] = []
    upcoming = [item for item in first_page(smoke) if not item["overdue"]]
    item = upcoming[12]
    path = f"/api/posts/{item['id']}"
    with smoke.page() as page:
        page.on("dialog", lambda dialog: (prompts.append(dialog.message), dialog.dismiss()))
        order = row_ids(page)
        before = tab_count(page, "scheduled")
        with board_call(page, "DELETE", path) as info:
            row(page, item["id"]).locator(".row-delete").click()
        assert_board_call(smoke, info.value)
        expect(row(page, item["id"])).to_have_count(0)
        toast = page.locator("#toast")
        expect(toast).to_contain_text("Post deleted")
        expect(page.locator("#announcer")).to_contain_text("Post deleted")
        expect(page.locator('[data-tab="scheduled"] .count')).to_have_text(str(before - 1))
        with board_call(page, "POST", f"{path}/restore") as info:
            toast.locator(".undo").click()
        assert_board_call(smoke, info.value)
        expect(row(page, item["id"])).to_have_count(1)
        expect(page.locator('[data-tab="scheduled"] .count')).to_have_text(str(before))
        expect(toast).to_contain_text("restored")
        assert row_ids(page)[: len(order)] == order, "the restored row is not back in its place"
        assert detail(smoke, item["id"])["status"] == "scheduled"
    assert not prompts, f"deleting a scheduled post asked: {prompts}"

    # A pending post whose time has passed goes back to Queued, not to Scheduled.
    overdue = None
    for _ in range(30):  # the retried posts turn overdue 10 s after their retry
        overdue = next((i for i in first_page(smoke) if i["overdue"]), None)
        if overdue:
            break
        time.sleep(0.5)
    assert overdue, "no overdue post to delete; the earlier checks changed"
    with smoke.page() as page:
        counts = {tab: tab_count(page, tab) for tab in ("scheduled", "queued")}
        row(page, overdue["id"]).locator(".row-delete").click()
        expect(row(page, overdue["id"])).to_have_count(0)
        with board_call(page, "POST", f"/api/posts/{overdue['id']}/restore"):
            page.locator("#toast .undo").click()
        expect(page.locator("#toast")).to_contain_text("Queued")
        expect(row(page, overdue["id"])).to_have_count(0)
        expect(page.locator('[data-tab="scheduled"] .count')).to_have_text(str(counts["scheduled"] - 1))
        expect(page.locator('[data-tab="queued"] .count')).to_have_text(str(counts["queued"] + 1))
        saved = detail(smoke, overdue["id"])
        assert saved["status"] == "queued" and saved["scheduled_time"] is None, saved

    # A posted post: the record goes only after a yes, and Undo keeps it posted.
    posted = first_page(smoke, "posted")[0]
    with smoke.page("#tab=posted") as page:
        page.once("dialog", lambda dialog: (prompts.append(dialog.message), dialog.dismiss()))
        row(page, posted["id"]).locator(".row-delete").click()
        page.wait_for_timeout(300)
        assert prompts and POSTED_DELETE_PROMPT in prompts[-1], prompts
        expect(row(page, posted["id"])).to_have_count(1)
        assert smoke.context.request.get(f"{smoke.base}/api/posts/{posted['id']}").status == 200, "a declined delete went through"
        page.once("dialog", lambda dialog: dialog.accept())
        with board_call(page, "DELETE", f"/api/posts/{posted['id']}"):
            row(page, posted["id"]).locator(".row-delete").click()
        expect(row(page, posted["id"])).to_have_count(0)
        page.locator("#toast .undo").click()
        expect(row(page, posted["id"])).to_have_count(1)
        assert detail(smoke, posted["id"])["status"] == "posted"

    # The edit panel's Delete offers Undo as well.
    item = upcoming[14]
    with smoke.page() as page:
        open_panel(page, item["id"])
        with board_call(page, "DELETE", f"/api/posts/{item['id']}"):
            page.click("#delete")
        expect(page.locator("#edit-panel")).to_be_hidden()
        expect(row(page, item["id"])).to_have_count(0)
        with board_call(page, "POST", f"/api/posts/{item['id']}/restore"):
            page.locator("#toast .undo").click()
        expect(row(page, item["id"])).to_have_count(1)

    # 10 seconds, on a fake clock; the delete is answered in the page, so nothing changes.
    item = upcoming[16]

    def fake_delete(route):
        if route.request.method == "DELETE":
            route.fulfill(json={"id": item["id"], "status": "deleted"})
        else:
            route.continue_()

    with smoke.page(None) as page:
        page.clock.install()
        page.route(lambda url: urlsplit(url).path == f"/api/posts/{item['id']}", fake_delete)
        page.goto(f"{smoke.base}/#tab=scheduled")
        wait_for_list(page)
        row(page, item["id"]).locator(".row-delete").click()
        toast = page.locator("#toast")
        expect(toast.locator(".undo")).to_be_visible()
        page.mouse.move(5, 300)
        page.clock.run_for(UNDO_MS - 1000)
        expect(toast.locator(".undo")).to_be_visible()  # a plain toast goes after 5 s
        toast.hover()
        page.clock.run_for(5000)
        expect(toast).to_be_visible()  # the countdown waits while the pointer is on the toast
        page.mouse.move(5, 300)
        page.clock.run_for(UNDO_MS + 500)
        expect(toast).to_be_hidden()


def check_bulk_delete_and_undo(smoke: Smoke):
    """Bulk delete removes the rows and offers Undo, which sends bulk restore with the same ids
    and brings the rows back in order; deleting a whole tab shows its empty state; posted
    posts are deleted only after a yes, and cannot be moved."""
    queued = smoke.all_items("queued")
    chosen = [item["id"] for item in queued[1:4]]
    with smoke.page("#tab=queued") as page:
        order = row_ids(page)
        before = tab_count(page, "queued")
        select_rows(page, chosen)
        with bulk_call(page) as info:
            page.click("#bulk-delete")
        assert_board_call(smoke, info.value)
        assert info.value.request.post_data_json == {"action": "delete", "ids": chosen}, info.value.request.post_data
        for post_id in chosen:
            expect(row(page, post_id)).to_have_count(0)
        expect(page.locator("#bulk-bar")).to_be_hidden()
        expect(page.locator('[data-tab="queued"] .count')).to_have_text(str(before - 3))
        toast = page.locator("#toast")
        expect(toast).to_contain_text("Deleted 3 posts")
        expect(toast.locator(".undo")).to_be_focused()  # the bar went away; Undo is one key away
        smoke.shot(page, "undo-toast-bulk-desktop-light")
        with bulk_call(page) as info:
            toast.locator(".undo").click()
        assert_board_call(smoke, info.value)
        assert info.value.request.post_data_json == {"action": "restore", "ids": chosen}, info.value.request.post_data
        for post_id in chosen:
            expect(row(page, post_id)).to_have_count(1)
        expect(page.locator('[data-tab="queued"] .count')).to_have_text(str(before))
        assert row_ids(page) == order, f"the rows came back as {row_ids(page)}, were {order}"
    failed = [item["id"] for item in smoke.all_items("failed")]
    assert failed, "no failed post left; the earlier checks changed"
    with smoke.page("#tab=failed") as page:
        select_rows(page, failed[:1])
        if len(failed) > 1:
            page.click("#bulk-select-all")
        else:
            expect(page.locator("#bulk-select-all")).to_be_disabled()  # nothing left to add
        expect(page.locator("#bulk-count")).to_have_text(f"{len(failed)} selected")
        with bulk_call(page):
            page.click("#bulk-delete")
        expect(page.locator("#empty-state")).to_have_text(EMPTY_TAB)
        expect(page.locator('[data-tab="failed"] .count')).to_have_text("0")
        with bulk_call(page):
            page.locator("#toast .undo").click()
        expect(page.locator("#post-list .post-row")).to_have_count(len(failed))
        expect(page.locator("#empty-state")).to_be_hidden()
    # Posted posts: the board's records only, so it asks first.
    posted = [item["id"] for item in first_page(smoke, "posted")[:2]]
    prompts: list[str] = []
    with smoke.page("#tab=posted") as page:
        select_rows(page, posted)
        expect(page.locator("#bulk-move")).to_be_disabled()  # a posted post stays in its channel
        page.once("dialog", lambda dialog: (prompts.append(dialog.message), dialog.dismiss()))
        page.click("#bulk-delete")
        page.wait_for_timeout(300)
        assert prompts and "2 posted posts" in prompts[0] and "stay in the channel" in prompts[0], prompts
        expect(page.locator("#bulk-count")).to_have_text("2 selected")
        page.once("dialog", lambda dialog: dialog.accept())
        with bulk_call(page) as info:
            page.click("#bulk-delete")
        assert info.value.request.post_data_json == {"action": "delete", "ids": posted}, info.value.request.post_data
        for post_id in posted:
            expect(row(page, post_id)).to_have_count(0)
        with bulk_call(page):
            page.locator("#toast .undo").click()
        for post_id in posted:
            expect(row(page, post_id)).to_have_count(1)
            assert detail(smoke, post_id)["status"] == "posted"


def check_bulk_selection_capped_at_500(smoke: Smoke):
    """Select all over more than 500 rows selects the first 500 and says why; a 501st is refused;
    the bulk request carries at most 500 ids."""
    fake = [fake_item(9_000_000 + n, f"Generated row {n + 1}") for n in range(BULK_MAX + 20)]
    sent: list[dict] = []

    def long_list(route):
        route.fulfill(json={"items": fake, "next_cursor": None})

    def answer_bulk(route):
        body = route.request.post_data_json
        sent.append(body)
        route.fulfill(json={"results": [{"id": post_id, "ok": True, "error": None} for post_id in body["ids"]]})

    with smoke.page(None) as page:
        page.route(lambda url: "/api/posts?" in url and "status=queued" in url, long_list)
        page.route(lambda url: urlsplit(url).path == "/api/posts/bulk", answer_bulk)
        page.goto(f"{smoke.base}/#tab=queued")
        wait_for_list(page)
        expect(page.locator("#post-list .post-row")).to_have_count(len(fake))
        select_rows(page, [fake[0]["id"]])
        page.click("#bulk-select-all")
        expect(page.locator("#post-list .row-select:checked")).to_have_count(BULK_MAX)
        expect(page.locator("#bulk-count")).to_have_text(f"{BULK_MAX} selected")
        expect(page.locator("#bulk-select-all")).to_have_text(f"Up to {BULK_MAX} at once")
        expect(page.locator("#bulk-select-all")).to_be_disabled()
        expect(page.locator("#toast")).to_contain_text(str(BULK_MAX))
        for width in (1280, 999, 699, 360):
            page.set_viewport_size({"width": width, "height": 720})
            problems = page.evaluate(BAR_FIT_JS)
            assert not problems, f"{width}px at the limit: {problems[0]} ({len(problems)} problems)"
        page.set_viewport_size({"width": 1280, "height": 720})
        extra = row(page, fake[-1]["id"]).locator(".row-select")
        extra.click()
        expect(extra).not_to_be_checked()
        expect(page.locator("#bulk-count")).to_have_text(f"{BULK_MAX} selected")
        expect(page.locator("#toast")).to_contain_text(str(BULK_MAX))
        smoke.shot(page, "bulk-cap-desktop-light")
        with page.expect_request(lambda r: urlsplit(r.url).path == "/api/posts/bulk"):
            page.click("#bulk-delete")
        expect(page.locator("#post-list .post-row")).to_have_count(len(fake) - BULK_MAX)
    assert len(sent) == 1 and sent[0]["ids"] == [item["id"] for item in fake[:BULK_MAX]], (
        f"the bulk delete sent {len(sent[0]['ids']) if sent else 0} ids"
    )


def check_keyboard_open_and_focus_return(smoke: Smoke):
    """Rows take focus with a visible ring, Enter opens the panel, Tab stays inside it, and
    closing puts focus back where it came from; Space on a row's checkbox selects it; every
    icon button is labelled and every badge has text."""
    with smoke.page() as page:
        first = page.locator("#post-list .post-row").first
        post_id = int(first.get_attribute("data-id"))
        page.locator('[data-tab="scheduled"]').focus()
        page.keyboard.press("Tab")
        expect(first).to_be_focused()
        ring = first.evaluate("node => getComputedStyle(node).outlineStyle")
        assert ring not in ("none", ""), f"the focused row shows no focus ring (outline-style {ring!r})"
        smoke.shot(page, "focus-row-desktop-light")
        page.keyboard.press("Enter")
        wait_for_panel(page, post_id)
        for _ in range(30):
            page.keyboard.press("Tab")
            where = page.evaluate(
                "() => { const a = document.activeElement;"
                " return a === document.body || a.closest('#edit-panel, .dock') !== null ? '' : (a.id || a.className); }"
            )
            assert not where, f"Tab reached {where!r} behind the open panel"
        page.keyboard.press("Escape")
        expect(page.locator("#edit-panel")).to_be_hidden()
        expect(first).to_be_focused()
        page.keyboard.press("Tab")
        select = first.locator(".row-select")
        expect(select).to_be_focused()
        page.keyboard.press("Space")
        expect(select).to_be_checked()
        expect(page.locator("#bulk-count")).to_have_text("1 selected")
        page.keyboard.press("Tab")
        opener = first.locator(".row-edit")
        expect(opener).to_be_focused()
        page.keyboard.press("Enter")
        wait_for_panel(page, post_id)
        page.keyboard.press("Escape")
        expect(opener).to_be_focused()
        expect(select).to_be_checked()  # the selection outlived the panel
        unlabelled = page.evaluate(
            "() => [...document.querySelectorAll('.icon-button')]"
            ".filter((b) => !(b.getAttribute('aria-label') || '').trim()).map((b) => b.id || b.className)"
        )
        assert not unlabelled, f"icon buttons without a label: {unlabelled}"
        blank = page.evaluate("() => [...document.querySelectorAll('.badge')].filter((b) => !b.textContent.trim()).length")
        assert blank == 0, f"{blank} badges carry no text"
        page.click("#bulk-clear")


def check_mobile_layout(smoke: Smoke):
    """At 360x740 with the bulk bar shown: no sideways scroll, every bar control fits, the bar
    stays at the bottom while scrolling and never covers the last row, a toast sits above it,
    and the edit panel is a bottom sheet over it."""
    context = smoke.new_context(**PHONE)
    try:
        with smoke.page("#tab=queued", context=context) as page:  # a short list, loaded whole
            ids = row_ids(page)
            select_rows(page, ids[:2])
            bar = page.locator("#bulk-bar")
            expect(bar).to_be_visible()
            problems = page.evaluate(BAR_FIT_JS)
            assert not problems, f"queued: {problems[0]} ({len(problems)} problems)"
            page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
            page.wait_for_timeout(200)
            last, box = row(page, ids[-1]).bounding_box(), bar.bounding_box()
            assert last["y"] + last["height"] <= box["y"] + 0.5, f"the bar at {box} covers the last row at {last}"
            assert box["y"] + box["height"] <= 740.5, f"the bar at {box} reaches past the screen"
        with smoke.page(context=context) as page:
            ids = row_ids(page)
            select_rows(page, ids[:2])
            bar = page.locator("#bulk-bar")
            problems = page.evaluate(BAR_FIT_JS)
            assert not problems, f"scheduled: {problems[0]} ({len(problems)} problems)"
            page.evaluate("() => window.scrollTo(0, 1500)")
            page.wait_for_timeout(200)
            box = bar.bounding_box()
            assert abs(box["y"] + box["height"] - 740) < 1, f"the bar at {box} is not at the bottom while scrolling"
            # A toast (here from a delete the page answers itself) sits above the bar.
            target = ids[10]
            page.route(
                lambda url: urlsplit(url).path == f"/api/posts/{target}",
                lambda route: route.fulfill(json={"id": target, "status": "deleted"})
                if route.request.method == "DELETE" else route.continue_(),
            )
            row(page, target).locator(".row-delete").click()
            toast = page.locator("#toast")
            expect(toast.locator(".undo")).to_be_visible()
            toast_box, box = toast.bounding_box(), bar.bounding_box()
            assert toast_box["y"] + toast_box["height"] <= box["y"] + 0.5, f"the toast at {toast_box} covers the bar at {box}"
            assert not page.evaluate(BAR_FIT_JS), "the bar does not fit with a toast shown"
            smoke.shot(page, "bulk-bar-toast-mobile-light")
            open_panel(page, ids[3])
            panel = page.locator("#edit-panel").bounding_box()
            assert panel["x"] < 1 and abs(panel["width"] - 360) < 1 and abs(panel["y"] + panel["height"] - 740) < 1, (
                f"the panel at {panel} is not a bottom sheet"
            )
            on_top = page.evaluate("() => document.elementFromPoint(180, 730).closest('#edit-panel') !== null")
            assert on_top, "the bottom sheet does not cover the bar"
            width = page.evaluate("() => document.documentElement.scrollWidth")
            assert width <= 360, f"the page is {width}px wide with the panel open"
    finally:
        context.close()


def check_empty_states(smoke: Smoke):
    """A user without channels is told how to add one, on every tab; a tab without posts says so."""
    login_url = os.environ.get("SMOKE_EMPTY_URL", "")
    parts = urlsplit(login_url)
    assert parts.scheme and parts.netloc, (
        "set SMOKE_EMPTY_URL to the sign-in link of `python scripts/dev_web.py --empty --port 8766`"
    )
    smoke.empty_base = f"{parts.scheme}://{parts.netloc}"
    smoke.empty_storage = sign_in_storage(smoke, login_url)
    context = smoke.browser.new_context(storage_state=smoke.empty_storage, timezone_id=DEFAULT_TIMEZONE)
    context.set_default_timeout(TIMEOUT_MS)
    try:
        with smoke.page(None, context=context) as page:
            page.goto(f"{smoke.empty_base}/#tab=scheduled")
            wait_for_list(page)
            for tab in TABS:
                page.click(f'[data-tab="{tab}"]')
                wait_for_list(page)
                expect(page.locator("#empty-state")).to_have_text(EMPTY_NO_CHANNELS)
                expect(page.locator("#empty-state code")).to_have_text("/start")
                expect(page.locator(f'[data-tab="{tab}"] .count')).to_have_text("0")
                expect(page.locator("#post-list .post-row")).to_have_count(0)
                expect(page.locator("#list-status")).to_have_text("")
            options = page.locator("#channel-filter option").all_inner_texts()
            assert options == ["All channels"], f"channel options are {options}"
            expect(page.locator("#bulk-bar")).to_be_hidden()
    finally:
        context.close()

    def no_failed_posts(route):
        route.fulfill(json={"items": [], "next_cursor": None})

    with smoke.page(None) as page:
        page.route(lambda url: "/api/posts?" in url and "status=failed" in url, no_failed_posts)
        page.goto(f"{smoke.base}/#tab=failed")
        wait_for_list(page)
        expect(page.locator("#empty-state")).to_have_text(EMPTY_TAB)
        page.fill("#search", "no caption says this")
        expect(page.locator("#empty-state")).to_have_text(NO_MATCH)


def check_unauthorized_with_panel_open(smoke: Smoke):
    """A 401 while the edit panel is open swaps in the signed-out view; the panel goes with it."""
    item = first_page(smoke)[5]

    def signed_out(route):
        route.fulfill(status=401, json={"error": "unauthorized", "message": "Not signed in."})

    with smoke.page(expect_failed_requests=True) as page:
        open_panel(page, item["id"])
        page.route(lambda url: "/api/" in urlsplit(url).path, signed_out)
        with page.expect_response(lambda r: urlsplit(r.url).path == "/api/caption/preview"):
            page.fill("#caption-input", "typed after the session ended")
        expect(page.locator("#signed-out")).to_be_visible()
        expect(page.locator("#app")).to_be_hidden()
        # Hidden inside #app either way, so the panel's own state is what tells.
        expect(page.locator("#edit-panel")).to_have_attribute("hidden", "")
        expect(page.locator("#panel-scrim")).to_have_attribute("hidden", "")
        assert not page.evaluate("() => document.body.classList.contains('panel-open')"), "the page stays locked"
        assert page.evaluate("() => document.querySelector('#edit-panel').dataset.state") == "closed"

def check_bulk_bar_first_in_tab_order(smoke: Smoke):
    """With posts selected, the bar's controls are the first Tab stops after the tabs, though
    the bar is drawn at the bottom of the screen; the rows come after it."""
    with smoke.page() as page:
        select_rows(page, row_ids(page)[3:5])
        page.locator('[data-tab="scheduled"]').focus()
        stops = []
        for _ in range(8):
            page.keyboard.press("Tab")
            stops.append(page.evaluate(
                "() => { const a = document.activeElement;"
                " return a.closest('#post-list .post-row') ? 'row' : (a.id || a.tagName); }"
            ))
            if len(stops) == 1:
                smoke.shot(page, "bulk-bar-keyboard-desktop-light")
        bar = ["bulk-clear", "bulk-select-all", "bulk-delete", "bulk-move",
               "bulk-shift-amount", "bulk-shift-unit", "bulk-shift-apply"]  # Move waits for a channel
        assert stops == [*bar, "row"], f"Tab stops after the tabs: {stops}"
        box = page.locator("#bulk-bar").bounding_box()
        assert abs(box["y"] + box["height"] - (720 - 12)) < 1, f"the bar at {box} is not at the bottom"
        page.click("#bulk-clear")


def check_bulk_undo_more_than_a_page(smoke: Smoke):
    """Undo of a bulk delete bigger than a page brings every row back and never says posts went
    to another tab when they are simply further down the list. The list and bulk calls are
    answered in the page by a small fake board with keyset paging."""
    fake = [fake_item(9_100_000 + n, f"Queued row {n + 1}") for n in range(3 * PAGE_SIZE)]
    deleted: set[int] = set()

    def fake_list(route):
        query = parse_qs(urlsplit(route.request.url).query)
        after = int(query["cursor"][0]) if "cursor" in query else 0
        live = [item for item in fake if item["id"] not in deleted and item["id"] > after]
        items = live[:PAGE_SIZE]
        more = len(live) > PAGE_SIZE
        route.fulfill(json={"items": items, "next_cursor": str(items[-1]["id"]) if more else None})

    def fake_bulk(route):
        body = route.request.post_data_json
        if body["action"] == "delete":
            deleted.update(body["ids"])
        else:
            deleted.difference_update(body["ids"])
        route.fulfill(json={"results": [{"id": post_id, "ok": True, "error": None} for post_id in body["ids"]]})

    with smoke.page(None) as page:
        page.route(lambda url: "/api/posts?" in url and "status=queued" in url, fake_list)
        page.route(lambda url: urlsplit(url).path == "/api/posts/bulk", fake_bulk)
        page.goto(f"{smoke.base}/#tab=queued")
        wait_for_list(page)
        scroll_to_end(page, 2 * PAGE_SIZE)
        select_rows(page, [fake[0]["id"]])
        page.click("#bulk-select-all")
        chosen = page.locator("#post-list .row-select:checked").count()
        assert chosen >= 2 * PAGE_SIZE, f"only {chosen} rows were selected"
        with bulk_call(page):
            page.click("#bulk-delete")
        wait_for_list(page)
        expect(page.locator("#post-list .post-row")).to_have_count(len(fake) - chosen)
        with bulk_call(page):
            page.locator("#toast .undo").click()
        expect(page.locator("#toast")).to_have_text(f"Restored {chosen} posts.")
        expect(page.locator("#post-list .post-row")).to_have_count(len(fake))
        assert row_ids(page) == [item["id"] for item in fake], "the rows did not come back in order"


def check_undo_shortcut(smoke: Smoke):
    """Ctrl+Z undoes the last delete while its toast is up, except while typing, where it is the
    editor's own undo; the announcement says so."""
    upcoming = [item for item in first_page(smoke) if not item["overdue"]]
    item, other = upcoming[30], upcoming[31]
    restores: list[str] = []
    with smoke.page() as page:
        page.on("request", lambda r: r.method == "POST" and r.url.endswith("/restore") and restores.append(r.url))
        row(page, item["id"]).locator(".row-delete").click()
        expect(row(page, item["id"])).to_have_count(0)
        expect(page.locator("#announcer")).to_contain_text("Ctrl+Z")
        undo = page.locator("#toast .undo")
        expect(undo).to_have_attribute("aria-keyshortcuts", re.compile("Control\\+Z"))
        open_panel(page, other["id"])
        caption = page.locator("#caption-input")
        caption.press("End")
        caption.press_sequentially(" draft")
        caption.press("Control+z")
        page.wait_for_timeout(500)
        assert not restores, f"Ctrl+Z in the caption editor restored the post: {restores}"
        expect(undo).to_be_visible()
        discard_and_close(page)
        with board_call(page, "POST", f"/api/posts/{item['id']}/restore") as info:
            page.keyboard.press("Control+z")
        assert_board_call(smoke, info.value)
        expect(row(page, item["id"])).to_have_count(1)
        assert len(restores) == 1, restores


def check_undo_shortcut_on_other_layouts(smoke: Smoke):
    """Ctrl+Z undoes a delete on a Ukrainian layout too (Ctrl+Я, the same key), once however long
    it is held; in the caption editor it stays the editor's own undo. The key printed Z counts,
    not the key's place: AZERTY's W and Dvorak's ; (where QWERTY has Z) do nothing, AZERTY's Z does."""
    upcoming = [item for item in first_page(smoke) if not item["overdue"]]
    item, other, again = upcoming[33], upcoming[34], upcoming[35]
    restores: list[str] = []
    ya, ya_code, ya_vk = UKRAINIAN["z"]
    with smoke.page() as page:
        page.evaluate(WATCH_KEYS_JS)
        cdp = page.context.new_cdp_session(page)
        page.on("request", lambda r: r.method == "POST" and r.url.endswith("/restore") and restores.append(r.url))
        row(page, item["id"]).locator(".row-delete").click()
        expect(row(page, item["id"])).to_have_count(0)
        expect(page.locator("#toast .undo")).to_be_visible()
        # In the caption editor Ctrl+Я is the editor's undo: the board leaves it alone.
        open_panel(page, other["id"])
        caption = page.locator("#caption-input")
        caption.press("End")
        caption.press_sequentially(" draft")
        typed = caption.input_value()
        press_on_layout(cdp, ya, ya_code, ya_vk)
        page.wait_for_timeout(300)
        assert not restores, f"Ctrl+Я in the caption editor restored the post: {restores}"
        assert not keys_seen(page, ya)[-1]["prevented"], "Ctrl+Я in the caption editor was blocked"
        expect(caption).not_to_have_value(typed)  # the editor's own undo took the last word back
        discard_and_close(page)
        # AZERTY W and Dvorak ;. The W goes without its virtual key code: Ctrl+W (87) closes
        # the tab before any page sees it, and the board reads only key and code.
        for key, code, vk in (("w", "KeyZ", 0), (";", "KeyZ", 186)):
            press_on_layout(cdp, key, code, vk)
        page.wait_for_timeout(300)
        assert not restores, f"Ctrl+W (AZERTY) or Ctrl+; (Dvorak) restored the post: {restores}"
        expect(page.locator("#toast .undo")).to_be_enabled()
        # Held down: one restore, not one per repeated keydown.
        with board_call(page, "POST", f"/api/posts/{item['id']}/restore") as info:
            press_on_layout(cdp, ya, ya_code, ya_vk, repeats=3)
        assert_board_call(smoke, info.value)
        expect(row(page, item["id"])).to_have_count(1)
        page.wait_for_timeout(300)
        assert len(restores) == 1, f"a held Ctrl+Я sent {len(restores)} restores"
        assert keys_seen(page, ya)[-4]["prevented"], "the Ctrl+Я that pressed Undo was left to the browser"
        # AZERTY's Z key sits where QWERTY has W: it is Ctrl+Z all the same.
        row(page, again["id"]).locator(".row-delete").click()
        expect(row(page, again["id"])).to_have_count(0)
        with board_call(page, "POST", f"/api/posts/{again['id']}/restore"):
            press_on_layout(cdp, "z", "KeyW", 90)
        expect(row(page, again["id"])).to_have_count(1)
    for post in (item, again):
        assert detail(smoke, post["id"])["status"] == "scheduled", f"post {post['id']} did not come back"


def check_keyboard_row_delete_focus(smoke: Smoke):
    """A row deleted from the keyboard hands focus to the next row (the previous one at the end
    of the list), a failed delete hands it back to the Delete button, and the panel's Delete
    hands it to the next row as well."""
    with smoke.page("#tab=queued", expect_failed_requests=True) as page:  # one delete is answered 500
        ids = row_ids(page)
        row(page, ids[1]).locator(".row-delete").focus()
        with board_call(page, "DELETE", f"/api/posts/{ids[1]}"):
            page.keyboard.press("Enter")
        expect(row(page, ids[1])).to_have_count(0)
        expect(row(page, ids[2])).to_be_focused()
        with board_call(page, "POST", f"/api/posts/{ids[1]}/restore"):
            page.keyboard.press("Control+z")
        expect(row(page, ids[1])).to_have_count(1)
        # The last row: focus goes to the row before it.
        ids = row_ids(page)
        row(page, ids[-1]).locator(".row-delete").focus()
        with board_call(page, "DELETE", f"/api/posts/{ids[-1]}"):
            page.keyboard.press("Enter")
        expect(row(page, ids[-2])).to_be_focused()
        with board_call(page, "POST", f"/api/posts/{ids[-1]}/restore"):
            page.keyboard.press("Control+z")
        expect(row(page, ids[-1])).to_have_count(1)
        # A delete that fails: focus is back on the Delete button, which works again.
        failing = ids[3]
        page.route(
            lambda url: urlsplit(url).path == f"/api/posts/{failing}",
            lambda route: route.fulfill(status=500, json={"error": "internal", "message": "Internal error"})
            if route.request.method == "DELETE" else route.continue_(),
        )
        button = row(page, failing).locator(".row-delete")
        button.focus()
        page.keyboard.press("Enter")
        expect(page.locator("#toast")).to_contain_text("error 500")
        expect(button).to_be_enabled()
        expect(button).to_be_focused()
        expect(row(page, failing)).to_have_count(1)
    with smoke.page("#tab=queued") as page:
        ids = row_ids(page)
        row(page, ids[4]).focus()
        page.keyboard.press("Enter")
        wait_for_panel(page, ids[4])
        page.locator("#delete").focus()
        with board_call(page, "DELETE", f"/api/posts/{ids[4]}"):
            page.keyboard.press("Enter")
        expect(page.locator("#edit-panel")).to_be_hidden()
        expect(row(page, ids[5])).to_be_focused()
        with board_call(page, "POST", f"/api/posts/{ids[4]}/restore"):
            page.keyboard.press("Control+z")
        expect(row(page, ids[4])).to_have_count(1)


def check_bulk_unknown_outcome_reloads(smoke: Smoke):
    """A bulk call that gets a server error or no answer may have changed posts already (the server
    commits post by post): the page reloads the list, says to check it before trying again, sends
    nothing again by itself and keeps the selection that is still shown. Each call below is
    applied by the server and its answer replaced, as when a connection drops."""
    upcoming = [item for item in first_page(smoke) if not item["overdue"]]
    sent: list[dict] = []

    def lost_answer(action: str, outcome: str):
        def handler(route):
            body = route.request.post_data_json
            if body["action"] != action:
                route.continue_()
                return
            sent.append(body)
            route.fetch()  # the server applies it...
            if outcome == "500":  # ...and the page gets an error instead of the results
                route.fulfill(status=500, json={"error": "internal", "message": "Internal error"})
            else:
                route.abort()
        return handler

    def bulk_url(url):
        return urlsplit(url).path == "/api/posts/bulk"

    # Shift + 500: the rows show the posts moved once, and nothing is sent again.
    chosen = upcoming[20:22]
    with smoke.page(expect_failed_requests=True) as page:
        page.route(bulk_url, lost_answer("shift", "500"))
        select_rows(page, [item["id"] for item in chosen])
        page.fill("#bulk-shift-amount", "1")
        page.select_option("#bulk-shift-unit", "hours")
        page.click("#bulk-shift-apply")
        toast = page.locator("#toast")
        expect(toast).to_contain_text("error 500")
        expect(toast).to_contain_text("reloaded")
        for item in chosen:
            saved = kyiv(detail(smoke, item["id"])["scheduled_time"])
            assert saved == kyiv(item["scheduled_time"]) + timedelta(hours=1), f"post {item['id']} is at {saved}"
            expect(row(page, item["id"]).locator(".time")).to_have_text(saved.strftime("%H:%M"))
            expect(row(page, item["id"]).locator(".row-select")).to_be_checked()
        expect(page.locator("#bulk-count")).to_have_text("2 selected")
        expect(page.locator("#bulk-shift-apply")).to_be_focused()  # back on the button pressed
        page.wait_for_timeout(300)
        assert len(sent) == 1, f"{len(sent)} shift requests were sent"
        smoke.shot(page, "bulk-unknown-outcome-desktop-light")
        page.click("#bulk-clear")

    # Delete + no answer: the deleted rows go after the reload.
    chosen = upcoming[23:25]
    with smoke.page(expect_failed_requests=True) as page:
        page.route(bulk_url, lost_answer("delete", "abort"))
        before = tab_count(page, "scheduled")
        select_rows(page, [item["id"] for item in chosen])
        page.click("#bulk-delete")
        expect(page.locator("#toast")).to_contain_text("could not be reached")
        expect(page.locator("#toast")).to_contain_text("reloaded")
        for item in chosen:
            expect(row(page, item["id"])).to_have_count(0)
        expect(page.locator('[data-tab="scheduled"] .count')).to_have_text(str(before - 2))
        expect(page.locator("#bulk-bar")).to_be_hidden()
        assert len(sent) == 2, f"{len(sent)} requests so far"
    for item in chosen:  # put them back for the checks after this one
        board_post(smoke, f"/api/posts/{item['id']}/restore")

    # Undo (bulk restore) + 500: the restored rows show after the reload.
    chosen = upcoming[26:28]
    with smoke.page(expect_failed_requests=True) as page:
        page.route(bulk_url, lost_answer("restore", "500"))
        select_rows(page, [item["id"] for item in chosen])
        with bulk_call(page):
            page.click("#bulk-delete")
        for item in chosen:
            expect(row(page, item["id"])).to_have_count(0)
        page.locator("#toast .undo").click()
        expect(page.locator("#toast")).to_contain_text("error 500")
        expect(page.locator("#toast")).to_contain_text("reloaded")
        for item in chosen:
            expect(row(page, item["id"])).to_have_count(1)
            assert detail(smoke, item["id"])["status"] == "scheduled"
        page.wait_for_timeout(300)
        assert len(sent) == 3 and sent[-1]["action"] == "restore", f"requests: {[b['action'] for b in sent]}"


# Every control of the bulk bar that acts on the selection (Move waits for a channel anyway).
BAR_ACTIONS = ("#bulk-select-all", "#bulk-delete", "#bulk-move", *SHIFT_CONTROLS)


def first_list_page(url: str) -> bool:
    """A list request for the first page: a reload, not the next page of infinite scroll."""
    return "/api/posts?" in url and "cursor=" not in urlsplit(url).query


def wait_until(page, done, what: str, timeout_ms: int = TIMEOUT_MS) -> None:
    """Let the page run until `done()` (checked here, in Python) is true."""
    deadline = time.monotonic() + timeout_ms / 1000
    while not done():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        page.wait_for_timeout(50)


def check_bulk_bar_waits_for_the_reload(smoke: Smoke):
    """After a bulk call with an unknown outcome the bar stays disabled until the list is reloaded
    and the message is up, so a second press cannot send the shift (or anything else) again from
    the old rows; the same after an Undo (bulk restore); a reload that fails frees the bar too.
    Each lost answer is applied by the server first; the reload is held in the page."""
    upcoming = [item for item in first_page(smoke) if not item["overdue"]]
    sent: list[dict] = []
    held: list = []
    hold = {"list": False}
    lose = {"shift", "restore"}

    def lost_answer(route):
        body = route.request.post_data_json
        sent.append(body)
        if body["action"] not in lose:
            route.continue_()
            return
        route.fetch()  # the server applies it, the answer is lost
        route.fulfill(status=500, json={"error": "internal", "message": "Internal error"})

    def list_reload(route):
        if hold["list"]:
            held.append(route)
        else:
            route.continue_()

    def actions() -> list[str]:
        return [body["action"] for body in sent]

    def watch(page) -> None:
        page.route(lambda url: urlsplit(url).path == "/api/posts/bulk", lost_answer)
        page.route(first_list_page, list_reload)

    # Shift with its answer lost: while the list reloads, nothing in the bar works.
    chosen = upcoming[36:38]
    toast = None
    with smoke.page(expect_failed_requests=True) as page:
        watch(page)
        toast = page.locator("#toast")
        select_rows(page, [item["id"] for item in chosen])
        page.fill("#bulk-shift-amount", "1")
        page.select_option("#bulk-shift-unit", "hours")
        wait_for_list(page)
        hold["list"] = True
        page.click("#bulk-shift-apply")
        wait_until(page, lambda: len(held) == 1, "the reload after the lost answer")
        page.locator("#bulk-shift-apply").click(force=True)  # a second press while the old rows show
        page.wait_for_timeout(300)
        assert actions() == ["shift"], f"a second press during the reload sent {actions()}"
        for control in BAR_ACTIONS:
            expect(page.locator(control)).to_be_disabled()
        expect(toast).not_to_contain_text("check it")  # said once the list is back
        hold["list"] = False
        held.pop().continue_()
        expect(toast).to_contain_text("The list was reloaded: check it before trying again.")
        for control in BAR_ACTIONS[1:]:
            expect(page.locator(control)).to_be_enabled()
        for item in chosen:
            saved = kyiv(detail(smoke, item["id"])["scheduled_time"])
            assert saved == kyiv(item["scheduled_time"]) + timedelta(hours=1), f"post {item['id']} is at {saved}"
            expect(row(page, item["id"]).locator(".time")).to_have_text(saved.strftime("%H:%M"))
            expect(row(page, item["id"]).locator(".row-select")).to_be_checked()
        expect(page.locator("#bulk-shift-apply")).to_be_focused()  # back on the button pressed

        # The same, but the reload fails as well: the bar is free again, never stuck disabled.
        hold["list"] = True
        page.click("#bulk-shift-apply")
        wait_until(page, lambda: len(held) == 1, "the second reload")
        expect(page.locator("#bulk-delete")).to_be_disabled()
        hold["list"] = False
        held.pop().abort()
        expect(toast).to_contain_text("Refresh the list and check it before trying again.")
        for control in BAR_ACTIONS[1:]:
            expect(page.locator(control)).to_be_enabled()
        assert actions() == ["shift", "shift"], f"requests: {actions()}"
        page.click("#bulk-clear")

    # Undo (bulk restore) with its answer lost, while another post is selected: the bar waits too.
    chosen, other = upcoming[38:40], upcoming[40]
    with smoke.page(expect_failed_requests=True) as page:
        watch(page)
        toast = page.locator("#toast")
        select_rows(page, [item["id"] for item in chosen])
        with bulk_call(page):
            page.click("#bulk-delete")
        for item in chosen:
            expect(row(page, item["id"])).to_have_count(0)
        select_rows(page, [other["id"]])
        expect(page.locator("#bulk-delete")).to_be_enabled()
        wait_for_list(page)
        hold["list"] = True
        toast.locator(".undo").click()
        wait_until(page, lambda: len(held) == 1, "the reload after the lost restore")
        page.locator("#bulk-delete").click(force=True)
        page.wait_for_timeout(300)
        assert actions()[-2:] == ["delete", "restore"], f"a press during the reload sent {actions()[-1]!r}"
        for control in BAR_ACTIONS:
            expect(page.locator(control)).to_be_disabled()
        hold["list"] = False
        held.pop().continue_()
        expect(toast).to_contain_text("The list was reloaded: check it before trying again.")
        expect(page.locator("#bulk-delete")).to_be_enabled()
        for item in chosen:
            expect(row(page, item["id"])).to_have_count(1)
        expect(row(page, other["id"]).locator(".row-select")).to_be_checked()
        page.click("#bulk-clear")
    for item in (*chosen, other):
        assert detail(smoke, item["id"])["status"] == "scheduled", f"post {item['id']} is not scheduled"


def check_bulk_unreadable_answer_reloads(smoke: Smoke):
    """A 2xx whose body breaks off (the connection dropped mid-answer) means the server acted but
    the page cannot tell how: like no answer, the list is reloaded and the page says to check it,
    instead of offering to try again."""
    upcoming = [item for item in first_page(smoke) if not item["overdue"]]
    chosen = upcoming[41:43]
    sent: list[dict] = []

    def cut_off(route):
        body = route.request.post_data_json
        if body["action"] != "shift":
            route.continue_()
            return
        sent.append(body)
        text = route.fetch().text()  # the server applies it...
        # ...and only the first half of its JSON answer arrives.
        route.fulfill(status=200, content_type="application/json", body=text[: len(text) // 2])

    with smoke.page(expect_failed_requests=True) as page:
        page.route(lambda url: urlsplit(url).path == "/api/posts/bulk", cut_off)
        select_rows(page, [item["id"] for item in chosen])
        page.fill("#bulk-shift-amount", "1")
        page.select_option("#bulk-shift-unit", "hours")
        page.click("#bulk-shift-apply")
        toast = page.locator("#toast")
        expect(toast).to_contain_text("The list was reloaded: check it before trying again.")
        expect(toast).not_to_contain_text("Try again")
        for item in chosen:
            saved = kyiv(detail(smoke, item["id"])["scheduled_time"])
            assert saved == kyiv(item["scheduled_time"]) + timedelta(hours=1), f"post {item['id']} is at {saved}"
            expect(row(page, item["id"]).locator(".time")).to_have_text(saved.strftime("%H:%M"))
            expect(row(page, item["id"]).locator(".row-select")).to_be_checked()
        expect(page.locator("#bulk-shift-apply")).to_be_enabled()
        page.wait_for_timeout(300)
        assert len(sent) == 1, f"{len(sent)} shift requests were sent"
        page.click("#bulk-clear")


def check_shift_amount_ignores_wheel(smoke: Smoke):
    """The mouse wheel over the focused shift amount (the board's only number field) never changes
    the number; the page scrolls instead."""
    with smoke.page() as page:
        select_rows(page, row_ids(page)[:1])
        amount = page.locator("#bulk-shift-amount")
        amount.fill("3")
        expect(amount).to_be_focused()
        box = amount.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        for delta in (-100, -100, 100, -100):
            page.mouse.wheel(0, delta)
            page.wait_for_timeout(100)
        expect(amount).to_have_value("3")
        expect(page.locator("#bulk-shift-apply")).to_be_enabled()
        page.click("#bulk-clear")


def check_signout_post_succeeds(smoke: Smoke):
    """Sign-out is a fetch POST from the page; the server's Origin check must accept it."""
    with smoke.page() as page:
        with page.expect_response(lambda r: r.url == f"{smoke.base}/logout" and r.request.method == "POST") as info:
            page.click("#signout")
        response = info.value
        headers = response.request.all_headers()
        assert response.status == 303, f"POST /logout answered {response.status} (Origin sent: {headers.get('origin')!r})"
        assert headers.get("origin") == smoke.base, f"sign-out sent Origin {headers.get('origin')!r}"
        assert headers.get("x-board") == "1", "sign-out did not send X-Board: 1"
        page.wait_for_url(f"{smoke.base}/")
        expect(page.locator("h1")).to_have_text("You are not signed in")
    status = smoke.context.request.get(f"{smoke.base}/api/me").status
    assert status == 401, f"/api/me answers {status} after sign-out"


def check_no_js_errors(smoke: Smoke):
    problems = smoke.page_errors + smoke.console_errors
    assert not problems, "; ".join(problems[:5])


CHECKS = [
    check_tab_counts_match_api,
    check_scheduled_grouped_and_sorted,
    check_infinite_scroll_loads_all,
    check_channel_filter,
    check_search_cyrillic_case,
    check_caption_shown_literally,
    check_api_text_is_not_markup,
    check_thumbnail_fallback,
    check_album_and_recurring_badges,
    check_read_only_statuses,
    check_error_banner_and_retry,
    check_me_failure_shows_banner,
    check_refresh_keeps_place,
    check_unauthorized_shows_run_web,
    check_list_fits_phone_width,
    check_failed_tab_readable_on_phone,
    # Edit panel. From the time check on, checks change posts: each picks posts no
    # earlier check changed in a way that matters to it, so keep this order.
    check_panel_dialog_and_focus,
    check_panel_layout,
    check_time_is_kyiv_not_browser_zone,  # needs the seeded earliest post at 10:00
    check_caption_bold_round_trip,
    check_caption_shortcuts_on_other_layouts,
    check_formatted_caption_keeps_entities,
    check_invalid_tag_blocks_save,
    check_literal_angle_and_ampersand,
    check_preview_debounced_and_stale_ignored,
    check_signature_note,
    check_unschedule_moves_to_queued,
    check_post_now_sets_one_minute_ahead,
    check_failed_retry,
    check_conflict_reloads_panel,
    check_unschedule_hint_by_status,
    check_replace_media_and_as_file,
    check_replace_media_on_post_with_missing_file,
    check_send_as_file_starts_from_the_post,
    check_write_in_flight_keeps_panel,
    check_album_media_read_only,
    check_read_only_panel,
    check_escape_prompts_when_dirty,
    check_delete_from_panel,
    # Selection, bulk actions, Undo, empty states, accessibility. Up to the 401 check
    # they only look (selections are cleared, writes are answered in the page); from
    # the shift check on they change posts, each picking posts no earlier check needs.
    check_bulk_select_shows_bar,
    check_bulk_bar_first_in_tab_order,
    check_shift_amount_ignores_wheel,
    check_keyboard_open_and_focus_return,
    check_mobile_layout,
    check_bulk_selection_capped_at_500,
    check_bulk_undo_more_than_a_page,
    check_empty_states,
    check_unauthorized_with_panel_open,
    check_bulk_shift_moves_selected,
    check_bulk_partial_failure_summary,
    check_bulk_move_to_channel,
    check_single_delete_and_undo,
    check_undo_shortcut,
    check_undo_shortcut_on_other_layouts,
    check_keyboard_row_delete_focus,
    check_bulk_delete_and_undo,
    check_bulk_unknown_outcome_reloads,
    check_bulk_bar_waits_for_the_reload,
    check_bulk_unreadable_answer_reloads,
]
# Sign-out ends the session, so it runs after everything else, before the error tally.
FINAL_CHECKS = [check_signout_post_succeeds, check_no_js_errors]


# --- screenshots ------------------------------------------------------------------------

SHOTS = (
    ("scheduled-desktop-light", DESKTOP, "light", "#tab=scheduled"),
    ("scheduled-desktop-dark", DESKTOP, "dark", "#tab=scheduled"),
    ("scheduled-mobile-light", PHONE, "light", "#tab=scheduled"),
    ("scheduled-mobile-dark", PHONE, "dark", "#tab=scheduled"),
    ("failed-desktop-light", DESKTOP, "light", "#tab=failed"),
    ("failed-mobile-light", PHONE, "light", "#tab=failed"),
    ("failed-mobile-dark", PHONE, "dark", "#tab=failed"),
    ("posted-mobile-dark", PHONE, "dark", "#tab=posted"),
    ("queued-desktop-dark", DESKTOP, "dark", "#tab=queued"),
)


# The open panel: (name, device, colour scheme, post, what to show).
PANEL_SHOTS = (
    ("panel-desktop-light", DESKTOP, "light", "formatted", None),
    ("panel-desktop-dark", DESKTOP, "dark", "formatted", None),
    ("panel-mobile-light", PHONE, "light", "formatted", None),
    ("panel-mobile-light-scrolled", PHONE, "light", "formatted", "scrolled"),
    ("panel-mobile-dark", PHONE, "dark", "formatted", None),
    ("panel-failed-retry-mobile-light", PHONE, "light", "failed", None),
    ("panel-failed-retry-desktop-dark", DESKTOP, "dark", "failed", None),
    ("panel-invalid-tag-mobile-light", PHONE, "light", "formatted", "invalid"),
    ("panel-invalid-tag-desktop-light", DESKTOP, "light", "formatted", "invalid"),
    ("panel-album-desktop-light", DESKTOP, "light", "album", None),
    ("panel-posted-mobile-dark", PHONE, "dark", "posted", None),
    ("panel-focus-desktop-light", DESKTOP, "light", "formatted", "focus"),
)


# The bulk bar, its result summary, the Undo toast and the empty states:
# (name, device, colour scheme, tab, what to show).
BULK_SHOTS = (
    ("bulk-bar-desktop-light", DESKTOP, "light", "scheduled", "bar"),
    ("bulk-bar-desktop-dark", DESKTOP, "dark", "scheduled", "bar"),
    ("bulk-bar-queued-desktop-light", DESKTOP, "light", "queued", "bar"),
    ("bulk-bar-tablet-light", {"viewport": {"width": 800, "height": 900}}, "light", "scheduled", "bar"),
    ("bulk-bar-mobile-light", PHONE, "light", "scheduled", "bar"),
    ("bulk-bar-mobile-dark", PHONE, "dark", "scheduled", "bar"),
    ("bulk-bar-mobile-end-light", PHONE, "light", "queued", "end"),
    ("bulk-bar-keyboard-mobile-light", PHONE, "light", "scheduled", "keyboard"),
    ("bulk-summary-desktop-dark", DESKTOP, "dark", "scheduled", "summary"),
    ("bulk-summary-mobile-light", PHONE, "light", "scheduled", "summary"),
    ("undo-toast-desktop-light", DESKTOP, "light", "scheduled", "undo"),
    ("undo-toast-mobile-dark", PHONE, "dark", "scheduled", "undo"),
    ("empty-tab-desktop-light", DESKTOP, "light", "failed", "empty"),
    ("empty-no-channels-desktop-light", DESKTOP, "light", "scheduled", "no-channels"),
    ("empty-no-channels-mobile-dark", PHONE, "dark", "scheduled", "no-channels"),
)
SKIP_REASONS = ("Cannot shift this post: it is queued.", "Scheduled time must be at least 60 seconds in the future")


def show_bulk_state(smoke: Smoke, page, state: str) -> None:
    """Bring the page into one BULK_SHOTS state. Every change is answered in the page, so no post changes."""
    if state == "empty":
        return
    ids = row_ids(page)
    if state == "undo":
        target = ids[4]
        page.route(
            lambda url: urlsplit(url).path == f"/api/posts/{target}",
            lambda route: route.fulfill(json={"id": target, "status": "deleted"})
            if route.request.method == "DELETE" else route.continue_(),
        )
        row(page, target).locator(".row-delete").click()
        expect(page.locator("#toast .undo")).to_be_visible()
        return
    if state == "end":
        page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
    elif state == "bar":
        page.evaluate("() => window.scrollTo(0, 900)")
    in_view = page.evaluate(
        "() => [...document.querySelectorAll('#post-list .post-row')].filter((r) => {"
        " const box = r.getBoundingClientRect(); return box.top > 120 && box.bottom < innerHeight - 160; })"
        ".map((r) => Number(r.dataset.id))"
    )
    select_rows(page, ids[2:5] if state == "summary" else (in_view or ids)[1:3])
    if state == "keyboard":  # the first Tab after the tabs lands in the bar
        page.locator(".tab[aria-selected='true']").focus()
        page.keyboard.press("Tab")
    elif state == "summary":
        def answer(route):
            wanted = route.request.post_data_json["ids"]
            results = [{"id": post_id, "ok": n == 0, "error": None if n == 0 else SKIP_REASONS[(n - 1) % 2]}
                       for n, post_id in enumerate(wanted)]
            route.fulfill(json={"results": results})

        page.route(lambda url: urlsplit(url).path == "/api/posts/bulk", answer)
        page.click("#bulk-shift-apply")
        expect(page.locator("#bulk-summary")).to_be_visible()
        wait_for_list(page)
    page.wait_for_timeout(200)


def zero_count(tab: str):
    """A route handler that answers /api/me with no posts in `tab`."""

    def handler(route):
        response = route.fetch()
        body = response.json()
        body["counts"][tab] = 0
        route.fulfill(response=response, json=body)

    return handler


def take_bulk_screenshots(smoke: Smoke) -> None:
    for name, device, scheme, tab, state in BULK_SHOTS:
        if state == "no-channels":
            if smoke.empty_storage is None:
                continue
            context = smoke.browser.new_context(storage_state=smoke.empty_storage, timezone_id=DEFAULT_TIMEZONE,
                                                color_scheme=scheme, **device)
            base = smoke.empty_base
        else:
            context = smoke.new_context(color_scheme=scheme, **device)
            base = smoke.base
        context.set_default_timeout(TIMEOUT_MS)
        try:
            with smoke.page(None, context=context) as page:
                if state == "empty":  # the tab's list and count answered as empty in the page
                    page.route(lambda url: "/api/posts?" in url and f"status={tab}" in url,
                               lambda route: route.fulfill(json={"items": [], "next_cursor": None}))
                    page.route(lambda url: urlsplit(url).path == "/api/me", zero_count(tab))
                page.goto(f"{base}/#tab={tab}")
                wait_for_list(page)
                page.wait_for_load_state("networkidle")
                show_bulk_state(smoke, page, state)
                smoke.shot(page, name)
        finally:
            context.close()


def take_screenshots(smoke: Smoke) -> None:
    for name, device, scheme, hash_ in SHOTS:
        context = smoke.new_context(color_scheme=scheme, **device)
        try:
            with smoke.page(hash_, context=context) as page:
                page.wait_for_load_state("networkidle")
                smoke.shot(page, name)
        finally:
            context.close()
    posts = {
        "formatted": ("#tab=scheduled", pick(smoke, startswith="Big news:")["id"]),
        "album": ("#tab=scheduled", pick(smoke, media_type="album")["id"]),
        "failed": ("#tab=failed", first_page(smoke, "failed")[0]["id"]),
        "posted": ("#tab=posted", first_page(smoke, "posted")[0]["id"]),
    }
    for name, device, scheme, post, state in PANEL_SHOTS:
        hash_, post_id = posts[post]
        context = smoke.new_context(color_scheme=scheme, **device)
        try:
            with smoke.page(hash_, context=context) as page:
                open_panel(page, post_id)
                page.wait_for_load_state("networkidle")
                if state == "invalid":
                    page.fill("#caption-input", "<b>Big news: the new site is live.")
                    expect(page.locator("#panel-error")).to_be_visible()
                elif state == "scrolled":
                    page.locator(".panel-body").evaluate("node => node.scrollTo(0, node.scrollHeight)")
                elif state == "focus":
                    page.locator("#caption-input").focus()
                    page.keyboard.press("Shift+Tab")
                smoke.shot(page, name)
        finally:
            context.close()
    take_bulk_screenshots(smoke)


# --- runner -----------------------------------------------------------------------------


def sign_in(smoke: Smoke, login_url: str) -> None:
    smoke.context = smoke.new_context()
    page = smoke.context.new_page()
    smoke.watch(page)
    page.goto(login_url)
    page.get_by_role("button", name="Continue").click()
    page.wait_for_load_state()
    if urlsplit(page.url).path != "/":
        heading = page.locator("h1").inner_text()
        raise AssertionError(f"sign-in failed ({heading!r}); restart dev_web.py for a fresh link")
    smoke.storage = smoke.context.storage_state()
    page.close()


def run(name: str, check, smoke: Smoke) -> bool:
    try:
        check(smoke)
    except Exception as exc:  # report every failure and carry on with the next check
        reason = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        print(f"FAIL {name}: {reason}", flush=True)
        return False
    print(f"PASS {name}", flush=True)
    return True


def main() -> int:
    login_url = os.environ.get("SMOKE_URL", "")
    parts = urlsplit(login_url)
    if not parts.scheme or not parts.netloc:
        print("Set SMOKE_URL to the sign-in link that scripts/dev_web.py prints.", file=sys.stderr)
        return 2
    base = f"{parts.scheme}://{parts.netloc}"
    shots = os.environ.get("SMOKE_SHOTS")
    expect.set_options(timeout=TIMEOUT_MS)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        smoke = Smoke(browser, base, Path(shots) if shots else None)
        if not run("sign_in", lambda s: sign_in(s, login_url), smoke):
            return 1
        results = [run(check.__name__, check, smoke) for check in CHECKS]
        if shots:
            take_screenshots(smoke)
        results += [run(check.__name__, check, smoke) for check in FINAL_CHECKS]
        browser.close()
    print(f"{sum(results)}/{len(results)} checks passed", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Browser smoke test for the web board (manual; not part of the pytest suite).

Setup, once per virtualenv:  pip install playwright
Start the seeded dev server, then run the checks against the link it prints:

    python scripts/dev_web.py &
    SMOKE_URL='http://localhost:8765/login?t=...' python scripts/smoke_web.py

CHROMIUM_PATH names a Chromium binary (default: Playwright's own browser from
`playwright install chromium`). SMOKE_SHOTS=<folder> also saves screenshots of
the board: desktop 1280x800 and phone 360x740, light and dark.

The sign-in link works once, so restart dev_web.py before every run; that also
gives each run the same fresh seed (some checks expect it). Prints `PASS name`
or `FAIL name: reason` for each check and exits 1 when any check fails.
"""

import os
import re
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import expect, sync_playwright

TABS = ("scheduled", "queued", "failed", "posted")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SEEDED_SCHEDULED = 130
PAGE_SIZE = 50  # the API's default page, which the board uses
TIMEOUT_MS = 10_000
DESKTOP = {"viewport": {"width": 1280, "height": 800}}
PHONE = {"viewport": {"width": 360, "height": 740}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True}

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
    """Rows the board cannot change (sending, unknown) render read-only; overdue gets its badge."""
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
            for control in (".row-select", ".row-edit", ".row-delete"):
                expect(target.locator(control)).to_be_disabled()
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


def take_screenshots(smoke: Smoke) -> None:
    for name, device, scheme, hash_ in SHOTS:
        context = smoke.new_context(color_scheme=scheme, **device)
        try:
            with smoke.page(hash_, context=context) as page:
                page.wait_for_load_state("networkidle")
                smoke.shot(page, name)
        finally:
            context.close()


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

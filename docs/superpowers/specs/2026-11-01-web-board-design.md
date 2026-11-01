# Web board for the Telegram scheduler bot - design

- **Date:** 2026-11-01
- **Status:** draft, awaiting review
- **Scope:** sub-project 1 of a larger "control everything from the web" effort: the web foundation plus the **posts board** (view, edit, delete).

## 1. Goal and success criteria

The bot is currently controlled only through Telegram buttons. The owner wants a web board, reachable on their own domain behind a private tunnel, where they can see all scheduled posts and edit or delete any of them.

Success means:

- Open the board and see every post by status (Scheduled, Queued, Failed, Posted), grouped by day, with thumbnail, caption, channel and time.
- Change a post's caption (formatting preserved), time, channel or media, or delete it, in one or two clicks.
- Apply bulk actions (delete, move to channel, shift time) to many posts.
- The bot publishes exactly what the board shows, with no restart. Moving or deleting a post on the board moves or cancels its live scheduler job.
- Only users on `ALLOWED_USER_IDS` can sign in, and each sees only their own channels and posts (same isolation as the bot).
- Existing deployments behave exactly as before unless the web board is explicitly enabled.

## 2. Decisions

| Topic | Decision | Why |
|---|---|---|
| Access | Own domain, HTTPS terminated by a private tunnel | Stated by the owner. The app still has its own sign-in as defense in depth. |
| Main view | Day-grouped list with channel/status filters; side panel (bottom sheet on phones) to edit | Scales to hundreds of posts; works on a phone. |
| Caption editing | Tagged text (`<b>`, `<i>`, `<a href>` ...) with a toolbar and live preview | Round-trips Telegram entities without loss; far simpler than a WYSIWYG editor. |
| Architecture | aiohttp server **inside the bot process**, same event loop | The scheduler keeps one in-memory job per post. Web edits must go through the live `PostScheduler` or a moved/deleted post would still publish at its old time. |
| Sign-in | `/web` command returns a single-use link; server issues a session cookie | No domain registration with BotFather, no passwords, identity comes from the Telegram ID the allowlist already trusts. |
| Scope cut | Nothing cut from the posts board: replace-media and bulk actions are in | Owner decision. |

Rejected: a separate web container (the scheduler lives in another process, so edits need a polled change queue and a stale job can publish at the old time) and a Telegram Mini App (only works inside Telegram; it would still need the in-process server for scheduler sync).

### Why in-process is required (verified in code)

`PostScheduler._post_to_channel(post_id)` claims the post by id (`pending -> sending`) and publishes it. It never compares the job's fire time with the row's `scheduled_time`. `_monitor_scheduled_posts` only creates jobs that are *missing* and handles *overdue* posts; it never removes or moves an existing job. A database-only time change would therefore leave the old job firing at the old time.

## 3. Architecture

### 3.1 Components

```text
bot/web/
├── __init__.py
├── server.py     # builds the aiohttp app, middlewares, start_web()/stop_web()
├── auth.py       # login tokens, sessions, rate limit, allowlist re-check
├── command.py    # /web command handler (registered from main.py)
├── api.py        # JSON routes under /api; thin: parse, call service, serialize
├── service.py    # all post mutations: ownership, conditional SQL, scheduler sync
├── captions.py   # Telegram entities <-> tagged text, sanitized HTML preview
├── media.py      # media streaming, thumbnails, replace-media upload
└── static/
    ├── index.html
    ├── app.js     # vanilla JS, no build step
    └── style.css
```

- `api.py` holds no business rules. `service.py` is the single write path (see 6), so rules are testable without HTTP.
- `captions.py`, `service.py` and `auth.py` have no aiohttp imports, so they are unit-testable in isolation.

### 3.2 Wiring

- `main.py` `post_init`: after creating `PostScheduler`, call `start_web(application, scheduler)`. `post_shutdown`: call `stop_web()` before stopping the scheduler.
- `start_web` creates an `aiohttp.web.AppRunner` + `TCPSite` on the running loop. The app holds a reference to the scheduler. Only the `/web` command handler sends Telegram messages.
- If startup validation fails or the port is in use, the error is logged and the bot continues **without** the board. A web failure never prevents the bot from running.
- `/web` is registered in `main.py` as a `CommandHandler`. It is always registered; when the board is disabled it replies "The web board is not enabled on this server." When enabled, `BotCommand("web", "Open the web board")` is added to the command menu and a line is added to `/help`.

### 3.3 Configuration

| Variable | Default | Meaning |
|---|---|---|
| `WEB_BASE_URL` | unset | Public URL of the board, e.g. `https://board.example.com`. **Setting it enables the board.** Used to build sign-in links and as the allowed `Origin`. |
| `WEB_PORT` | `8080` | Port the server listens on. |
| `WEB_HOST` | `0.0.0.0` | Bind address (inside the container). |

Startup validation (the board is not started, with a logged reason, if any fails):

- `ALLOWED_USER_IDS` must be non-empty. Otherwise anyone who can message the bot could obtain a board session.
- `WEB_BASE_URL` must be `https://...`, except `http://localhost` / `http://127.0.0.1` for local development.

`docker-compose.yml` passes `WEB_BASE_URL` and `WEB_PORT` and publishes `127.0.0.1:${WEB_PORT:-8080}:${WEB_PORT:-8080}` (loopback only). A tunnel on the host targets `127.0.0.1:8080`; a tunnel container can instead join the compose network and target `bot:8080`. `.env.example` documents both variables.

### 3.4 Dependencies

- Add `aiohttp` (pin a current 3.x release) to `Dockerfile`, `requirements.txt`, `pyproject.toml`, the CI install line, and regenerate `uv.lock`.
- Pillow is already declared (`pyproject.toml`, `Dockerfile`) and is used for thumbnails.

### 3.5 Threading

`Database` opens a new SQLite connection per call, so it is safe to call from worker threads. All blocking `Database`, filesystem and Pillow work in the web layer runs via `asyncio.to_thread` so a slow query or large upload never stalls publishing on the shared event loop. Scheduler calls (`schedule_single_post`, `cancel_post_job`) are in-memory and run on the loop.

## 4. Authentication and security

### 4.1 Sign-in flow

1. The user sends `/web` in a **private** chat (group chats are refused). The existing `auth_gate` has already enforced `ALLOWED_USER_IDS`.
2. The bot generates `secrets.token_urlsafe(32)`, stores only its SHA-256 hash in `web_login_tokens` with a 5-minute expiry, and replies with `WEB_BASE_URL/login?t=<token>` (link preview disabled). The message states that the link is single-use and expires in 5 minutes.
3. `GET /login?t=` renders a small page with a **Continue** button (a form that POSTs the token). It does **not** consume the token, so link-preview fetchers and scanners cannot burn it. The page sets `Referrer-Policy: no-referrer`.
4. `POST /login` validates the token (exists, unused, unexpired), marks it used, creates a session, sets the cookie, and redirects (303) to `/`.
5. Session: random 32-byte id, only its SHA-256 hash stored in `web_sessions`. Cookie `tgs_session`: `HttpOnly`, `SameSite=Lax`, `Secure` when `WEB_BASE_URL` is https, `Path=/`. Absolute lifetime 7 days. `POST /logout` deletes the session.
6. A request without a valid session gets `401` from `/api`; `GET /` shows a static "run /web in Telegram" page.

### 4.2 Protections

- **Allowlist re-check:** the session middleware verifies on every request that the session's user is still in `ALLOWED_USER_IDS`. Removing a user from the env locks them out immediately.
- **Per-user scoping:** `user_id` comes only from the session. Every post id and channel id from the browser is ownership-checked in `service.py` (including the target channel of a move). A post that belongs to someone else and a post that does not exist both return `404`.
- **CSRF:** every non-GET request must carry an `Origin` header equal to the origin of `WEB_BASE_URL`, and every non-GET `/api` request must also carry the custom header `X-Board: 1` (not settable cross-site without a CORS preflight, which the server never grants). `SameSite=Lax` is the third layer.
- **XSS:** caption previews are produced server-side (`captions.py`): all text is HTML-escaped, only the tags `b i u s code pre a blockquote span(spoiler) tg-emoji` are emitted, and `href` is restricted to `http`, `https`, `tg`. The front end writes API strings with `textContent` / `setAttribute`, never `innerHTML` on raw data. Strict headers on every response: `Content-Security-Policy: default-src 'self'; img-src 'self' data:; media-src 'self'; script-src 'self'; style-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`; `Cache-Control: no-store` on `/api`.
- **Media serving:** by post id only, never by client-supplied path. The resolved real path must lie inside the real `UPLOADS_DIR`, otherwise `404`.
- **Brute force:** failed token attempts are limited globally to 20 per 5 minutes (`429` beyond). Tokens are 256-bit, so this is a guardrail rather than the defense.
- **Logging:** the aiohttp access log is disabled or redacted so `?t=` never reaches logs. Every state-changing request is logged (user id, action, post ids).
- **Secrets:** the bot token is never exposed to the web layer's responses; tokens and sessions are stored hashed.

## 5. Data model

Two additive tables, created with `CREATE TABLE IF NOT EXISTS` in `init_database` (the repo's migration style). Timestamps use the repo's canonical Kyiv ISO strings (`serialize_dt` / `kyiv_now_iso`).

```sql
CREATE TABLE IF NOT EXISTS web_login_tokens (
    token_hash TEXT PRIMARY KEY,      -- sha256 hex of the token
    user_id    INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);
CREATE TABLE IF NOT EXISTS web_sessions (
    session_hash TEXT PRIMARY KEY,    -- sha256 hex of the session id
    user_id      INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);
```

The 03:00 `_daily_cleanup` also deletes expired/used login tokens, expired sessions, and cached thumbnails older than 14 days. No changes to the `posts` table.

## 6. Post semantics

### 6.1 Status model

| Board label | Condition in `posts` |
|---|---|
| Queued | `status='pending'` and `scheduled_time IS NULL` |
| Scheduled | `status='pending'` and `scheduled_time IS NOT NULL`, or `status='sending'` (shown with a "sending" badge) |
| Overdue (badge on Scheduled) | pending, `scheduled_time` in the past |
| Failed | `status='failed'` |
| Posted | `status='posted'` |
| (hidden) | `status='deleted'` - soft-deleted, awaiting the 03:00 purge |

### 6.2 What each status allows

| Status | Caption / time / channel | Replace media | Post now | Retry | Delete |
|---|---|---|---|---|---|
| Queued | yes (setting a time schedules it) | yes | yes | no | yes |
| Scheduled | yes | yes | yes | no | yes |
| Sending | locked (`409`) | locked | no | no | locked |
| Failed | yes (stored; no job until Retry) | yes | no | yes | yes |
| Posted | read-only | no | no | no | yes - removes the record only; the channel message stays |

The post-detail response includes a `can` object (`edit`, `media`, `post_now`, `retry`, `delete`) derived from this table so the UI never hardcodes it. Recurring posts are a single row: they can be edited like any other, and deleting one ends the series. Editing the recurring interval/end condition is out of scope here.

### 6.3 The single write path (`service.py`)

Every mutation:

1. Verifies ownership of the post (and of the target channel, if any) for the session's `user_id`.
2. Validates input fully **before** writing (so the scheduler step cannot fail halfway for a validation reason).
3. Runs one conditional `UPDATE ... WHERE id=? AND user_id=? AND status IN (<allowed statuses>)`. If the publish job claimed the post first, zero rows change and the caller gets `409` with the current status; the panel refreshes. This resolves the edit-vs-publish race in SQL.
4. Synchronises the scheduler after the commit.

Scheduler synchronisation:

| Operation | Database effect | Scheduler effect |
|---|---|---|
| Set / change time (future) | `scheduled_time = t` | `schedule_single_post(id, t)` (replaces the job) - only while the post is pending; a failed post just stores the time and gets its job on Retry |
| Clear time (unschedule) | `scheduled_time = NULL` | `cancel_post_job(id)` |
| Delete | `status = 'deleted'` | `cancel_post_job(id)` |
| Restore | `status = 'pending'`; if the old time is still in the future keep it, otherwise `scheduled_time = NULL` (back to **Queued**) | re-arm job only if the time was kept |
| Post now | `scheduled_time = now + 60 s` | `schedule_single_post` |
| Retry (failed only) | `status = 'pending'`, `retry_count = 0`, `failure_reason = NULL`; keep `scheduled_time` if it is in the future, else `now + 10 s` | `schedule_single_post` |
| Move channel | `channel_id = c` | none (the job reads the row at send time) |
| Caption / media change | `description`, `caption_entities` / `file_path`, `media_type` | none (the job reads the row at send time) |

A restored post can never publish unexpectedly: if its time passed while it was deleted it returns to the queue instead of becoming overdue (the 5-minute monitor would otherwise send it within minutes).

If the scheduler step raises after the commit (it is in-memory and runs on validated input, so this is not expected), the API returns `500`, logs it, and the board refetches stored state. A restart rebuilds all jobs from the database.

The channel's signature is applied at publish time, so moving a post to another channel automatically uses the new channel's signature.

### 6.4 Time rules

- Input format `YYYY-MM-DDTHH:MM` (the value of an HTML `datetime-local` input), interpreted as **Kyiv wall-clock** time and localized with `pytz` using `is_dst=None`. Nonexistent (spring-forward) and ambiguous (fall-back) local times are rejected with `400` and a clear message.
- A scheduled time must be at least 60 seconds in the future. The scheduler silently skips past times (`_schedule_single_post` returns without creating a job), which would leave a pending post with no job, so the API rejects them instead.
- Server-set times (post now, retry) bypass the 60-second minimum.
- Responses return times as Kyiv ISO strings (`2026-11-02T10:00:00+02:00`).

### 6.5 Captions (`captions.py`)

- Storage is unchanged: plain `description` text plus `caption_entities` (JSON list of `{type, offset, length, url?, language?, custom_emoji_id?}` with UTF-16 offsets, as Telegram reports them and as `_post_to_channel` restores them).
- **Read:** entities -> tagged text. Tags: `<b> <i> <u> <s> <code> <pre>` (with optional language), `<a href>`, `<blockquote>`, `<tg-spoiler>`, `<tg-emoji emoji-id="...">` (Telegram's own HTML syntax). Special characters in text are escaped. In the rendered preview, spoilers become a styled `<span>` and custom emoji show as a placeholder glyph (the web cannot render Telegram custom emoji); the stored entity is untouched.
- **Write:** tagged text -> `(plain text, entities)`. Offsets are computed in **UTF-16 code units** so emoji and other astral characters do not shift formatting. Unknown tags, bad nesting or unbalanced tags are rejected with `400`; nothing is saved.
- **Auto-detected entities** (URL, mention, hashtag, cashtag, email, phone, bot command) have no tag. When the caption is edited, each original auto entity is carried over to the new text if the exact span of text it covered is still present (mapped with a sequence diff), so fixing a typo elsewhere never strips a link or hashtag.
- An empty caption is stored as `NULL` for both columns.
- Limit: 1024 characters for media captions. The editor shows a live counter; the API rejects longer captions with `400`. The channel signature is appended at publish only if the result still fits in 1024, so the panel shows the signature as a grey read-only suffix and warns when it will not fit.
- The sanitized HTML preview is returned by the server (`caption_html`); the browser only inserts it into a dedicated preview container.

### 6.6 Media

- **List/detail:** a single-media post has one media item (`file_path`, `media_type`). An album post (`media_bundle_json`) lists each bundle item's `file_path`; a missing per-item type falls back to `get_media_type_from_extension`. `GET .../media?i=<n>` streams item *n* (HTTP range supported for video). A missing file returns `404` and the UI shows "file no longer available" (published media is deleted after 7 days).
- **Thumbnails:** `GET .../thumb?i=<n>&w=<px>` for `photo` items only (the UI shows an icon for video, audio, animation and document). Generated with Pillow, cached as WebP under `DATA_DIR/thumbs/`, keyed by `sha1(path + mtime + width)`. Oversized or corrupt images (including `DecompressionBombError`) yield `404` and the UI falls back to an icon.
- **Replace media** (`PUT /api/posts/{id}/media`, multipart, single-media posts in Queued/Scheduled/Failed only; albums are view-only in v1):
  1. Status and ownership are checked before reading the body.
  2. The body is read with `request.multipart()` and streamed to disk in chunks (no whole-file buffering), enforcing a hard maximum of 50 MB (`413` beyond).
  3. `check_storage_quota(user_id, size)` is applied (a `ValueError` becomes `413` with its message).
  4. `media_type` comes from `get_media_type_from_extension`; the optional `as_file=1` form field forces `document` (original quality), mirroring the bot's "send as file". Photos above Telegram's photo size limit are stored as `document` with a notice in the response.
  5. The file is written to `get_organized_media_path(user_id, media_type)` with `generate_unique_filename`.
  6. The row is updated with the conditional `UPDATE` (6.3); only after commit is the old file removed with `delete_media_file`. On any failure the new file is removed and the old state is untouched (the repo's rows-then-files convention).

### 6.7 Delete, restore, bulk

- **Delete** is a soft delete (`status='deleted'`) plus `cancel_post_job`. Media is removed by the existing nightly `purge_deleted_posts`. The UI shows an Undo toast; `restore` works until the purge runs.
- **Bulk** (`POST /api/posts/bulk`): `action` is `delete`, `restore`, `move_channel` or `shift`; up to 500 ids per call. The result is a per-post list `{id, ok, error?}`. Posts that cannot be changed (sending, wrong status for the action, shift landing in the past, not owned) are reported as skipped and never fail the rest of the batch. The target channel of `move_channel` is ownership-checked once.
- **Shift** is `{unit: "hours"|"days", amount: int}` (negative allowed, bounded to one year). It applies to Scheduled posts only, uses **Kyiv wall-clock arithmetic** (as `reschedule_all_posts_from_today` does, to stay correct across DST), and re-checks the 60-second minimum per post. The matching scheduler job is moved for each changed post.
- Bulk delete's Undo calls bulk `restore` with the same ids.

## 7. API

All routes require a session except `GET /login`, `POST /login` and the static assets. Request and response bodies are JSON unless noted. Errors are `{"error": "<code>", "message": "<text>"}` with codes `invalid_request` (400), `unauthorized` (401), `forbidden` (403), `not_found` (404), `conflict` (409), `too_large` (413), `rate_limited` (429), `internal` (500).

| Route | Purpose |
|---|---|
| `GET /` , `GET /static/*` | Single-page app and assets |
| `GET /login`, `POST /login`, `POST /logout` | Sign-in confirm page, token exchange, sign-out |
| `GET /api/me` | `{user_id, now, channels:[{channel_id, channel_name, signature, is_active}], counts:{scheduled, queued, failed, posted}}` |
| `GET /api/posts?status=&channel=&q=&limit=&cursor=` | `status` is one of `scheduled`, `queued`, `failed`, `posted`. `q` is a case-insensitive substring match on the caption (LIKE with escaping). `limit` default 50, max 100. Returns `{items:[...], next_cursor}`. |
| `GET /api/posts/{id}` | Full post (see 7.1) |
| `GET /api/posts/{id}/media?i=` | Original file |
| `GET /api/posts/{id}/thumb?i=&w=` | Cached thumbnail (photos) |
| `PATCH /api/posts/{id}` | Any subset of `{caption, scheduled_time, channel_id}`; `scheduled_time: null` unschedules. One atomic update. Returns the updated post. |
| `PUT /api/posts/{id}/media` | Multipart: `file`, optional `as_file` |
| `POST /api/posts/{id}/post-now` | Queued/Scheduled to now + 60 s |
| `POST /api/posts/{id}/retry` | Failed posts |
| `DELETE /api/posts/{id}` | Soft delete |
| `POST /api/posts/{id}/restore` | Undo a delete |
| `POST /api/posts/bulk` | `{action, ids, channel_id?, shift?}` -> `{results:[...]}` |

### 7.1 Post shape

List items: `id, status (queued|scheduled|sending|failed|posted), channel_id, channel_name, scheduled_time, posted_at, caption_text (plain, max 200 chars), media_type, media_count, is_recurring, recurring_interval_hours, overdue, failure_reason, retry_count`.

Detail adds: `caption_tagged, caption_html, media:[{index, media_type, exists}], signature, signature_fits, batch_id, mode, created_at, can:{edit, media, post_now, retry, delete}`.

### 7.2 Ordering and pagination

- Scheduled: ascending by scheduled time. Queued: by `COALESCE(sort_key, id)`. Failed: newest first. Posted: by `posted_at` descending.
- Time ordering uses `CAST(strftime('%s', scheduled_time) AS INTEGER)`, which SQLite computes in UTC from the stored offset, so ordering stays correct across DST changes (plain string comparison is off by up to an hour around a transition). The keyset cursor is `(sort_value, id)` encoded opaquely. At the expected scale (thousands of rows) the lack of index support for this expression is acceptable; revisit past ~50k posts.
- Only the 4 tabs are queryable; `deleted` rows never appear.

## 8. User interface

- **Layout:** header with channel filter, search box, refresh button, sign-out; tabs `Scheduled (n) | Queued (n) | Failed (n) | Posted (n)`. Scheduled and Posted are grouped under day headers in Kyiv time (`Mon 02.11`), Queued is flat in queue order, Failed shows its reason inline. Infinite scroll via the cursor.
- **Row:** thumbnail or type icon, two-line caption snippet, channel chip, time, badges (recurring, album xN, overdue, sending, failed), quick edit and delete buttons, and a selection checkbox.
- **Edit panel:** side panel >= 900 px wide, bottom sheet below. Large media preview (album: per-item strip), tagged-text editor with B / I / U / link buttons, live preview and 1024 counter with signature note, channel select, `datetime-local` time input with an Unschedule button, replace-media control with the "send as file" checkbox, and the actions allowed by `can` (Save, Post now, Retry, Delete). `Esc` closes the panel; unsaved changes prompt before closing.
- **Bulk:** selecting rows reveals a sticky bar with Delete, Move to channel, and Shift (+/- N hours or days). Results show a per-post summary of any skipped posts.
- **Feedback:** toasts for success; delete shows an Undo toast; a `409` toast says the post changed (for example "it was just published") and refreshes that row. Data is refetched on tab focus and via the refresh button; there are no push updates in v1.
- **Quality bar:** responsive from phone width, light/dark via `prefers-color-scheme`, keyboard-operable controls with visible focus and labelled buttons, status never conveyed by colour alone.
- **Implementation:** vanilla JS modules and one CSS file, no build step, so the Docker image stays Python-only. Dates are displayed from the server's Kyiv ISO strings; the browser's local zone is never used for scheduling.

## 9. Error handling

- A global middleware turns unexpected exceptions into `500`, logs them with the request context, and forwards a summary to `ADMIN_CHAT_ID` when set (as the bot's `error_handler` does).
- Validation failures return all problems in `message`; the panel keeps the user's input.
- Network errors in the UI show a retry banner; an expired session shows "run /web in Telegram again".
- Known limitation: Telegram menus that are already open are snapshots and are not live-updated when something changes on the board.

## 10. Testing

Extends the existing pytest suite (fresh temporary `DATA_DIR` and database per test, no network).

- **`captions`:** round-trip for every supported entity type; emoji and other astral characters keep correct UTF-16 offsets; auto-detected entities survive edits that leave their text intact and are dropped when their text is removed; malformed, unknown and unbalanced tags are rejected; XSS payloads in text and in `href` are neutralised; 1024 limit.
- **`service` with a fake scheduler** that records `schedule_single_post` / `cancel_post_job`: every status x operation from 6.2; losing the race (post claimed between read and write) returns conflict; time rules (past, under 60 s, DST gap, DST ambiguity, valid); restore with a passed time returns to Queued; retry keeps a future time and otherwise uses now + 10 s; bulk partial results; shift across a DST boundary; user B cannot read, change or move to user A's post or channel (`404`).
- **`auth`:** token is single-use and expires; GET `/login` does not consume it; only hashes are stored; session expiry; allowlist re-check on every request; startup refuses an empty allowlist or a non-https `WEB_BASE_URL`; failed-attempt rate limit; CSRF (missing/foreign `Origin`, missing `X-Board`).
- **API** (aiohttp's test client driven by a small `asyncio.run` helper, so no extra pytest plugin is needed): status codes and shapes for each route; pagination and DST-safe ordering; media route rejects paths outside `UPLOADS_DIR`.
- **Media upload:** quota exceeded, size cap, type mapping and `as_file`, old file removed only after commit, new file removed on failure, album and sending posts rejected.
- **CI:** add the `aiohttp` install to the existing workflow; the compile check already covers `bot/`; the Docker job validates the new dependency.
- **Manual verification** (not CI): a Playwright smoke run against a local instance - sign in with a token, list, edit caption/time, delete and undo, bulk shift.

The publish path itself is unchanged, so it is covered by the existing behaviour; the new tests focus on keeping scheduler jobs consistent with rows.

## 11. Documentation and housekeeping

- README: replace the "no web panel" wording, add a **Web board** section (setup behind a tunnel, `/web`, the new variables, security notes), update the commands table, database tables list, and project structure.
- `.env.example` and `docker-compose.yml` as in 3.3.
- Log lines for web start/stop and each state-changing request.

## 12. Out of scope (later cycles)

- Creating or uploading new posts from the web.
- Channel management and caption signatures.
- Settings (daily window, reminders, notification mode).
- Recurring interval/end-condition editing.
- Batches and backups/restore.
- Calendar and statistics views.
- Whole-schedule shuffle and redistribute (bulk shift is in scope).
- Album media editing (albums are view-only for media; caption, time, channel and delete work).
- Live push updates (SSE/WebSocket).
- An "all users" admin view.
- Telegram Mini App sign-in.

## 13. Open items to settle in the implementation plan

1. **Auto-detected entities:** the carry-over rule in 6.5 preserves them regardless of how Telegram treats explicit entities. If a real-bot check shows Telegram re-detects them anyway, the carry-over can be simplified.
2. **Photo size fallback:** confirm the exact threshold at which a photo is stored as `document` (Telegram's photo-send limit) against the Bot API docs when writing `media.py`.
3. **aiohttp pin and `uv.lock`:** choose the exact version and regenerate the lock file.
4. **Thumbnail cache cleanup:** the 14-day purge in the 03:00 job is the initial policy; adjust if the cache grows noticeably.

# Telegram Channel Post Scheduler Bot

A Telegram bot for scheduling automated posts (photos, videos, albums, documents) to Telegram channels, with recurring posts, multi-channel batches, backups, and per-user isolation. Built with python-telegram-bot 20.8, APScheduler, and SQLite.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and get its token.
2. Add the bot as an **administrator** to each channel you want to post to.
3. Set the environment variables (see below) and run:

```bash
pip install python-telegram-bot==20.8 apscheduler pytz pillow
python main.py
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | yes | Bot token from @BotFather |
| `ALLOWED_USER_IDS` | strongly recommended | Comma-separated Telegram user IDs allowed to use the bot, e.g. `123456789,987654321`. **If unset, the bot is open to every Telegram user who finds it** (a warning is logged at startup). |
| `DATA_DIR` | no | Directory for the SQLite database and uploaded media (default `/data`, for Docker volume mounts) |
| `TELEGRAM_CHANNEL_ID` | no | Legacy single-channel default; the bot supports per-user multi-channel setup via `/channels` |

Find your own Telegram user ID by messaging [@userinfobot](https://t.me/userinfobot).

## Security model

- Only users in `ALLOWED_USER_IDS` can interact with the bot (enforced before any handler runs).
- Registering a channel requires the requesting user to be an administrator of that channel (verified via `getChatMember`).
- All post/batch operations are scoped to the acting user at both the handler and SQL level.

## Data

- SQLite database at `$DATA_DIR/bot_data.db` (WAL mode). Timestamps are stored as Kyiv-timezone ISO strings; a one-time migration normalizes legacy rows.
- Uploaded media under `$DATA_DIR/uploads/`, organized per date/user, cleaned up 7 days after a post is published.

## Using the bot

`/start` opens the main menu, which doubles as a live dashboard (channels, queued/scheduled counts, next post, and alert buttons for failed/overdue posts). On first run it walks you through connecting a channel — the easiest way is to **forward any post from your channel**; the bot verifies your admin rights and sends a self-deleting test post to confirm it can publish.

Most features are reachable from the menu; the everyday commands (`/mode1` bulk upload, `/mode2` upload with captions, `/schedule`, `/stats`, `/settings`, `/cancel`, `/help`) appear in Telegram's command menu. Schedule windows are configured with tap-based hour pickers (typed `10 20 2` input still works).

**Notification modes** (in `/settings`): every post · daily digest (9:00 Kyiv summary) · errors only. Failure notifications always carry Retry/Manage buttons.

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

## Commands

`/start` shows the main menu. Key commands: `/mode1` (bulk upload), `/mode2` (upload with captions), `/multibatch` (multi-channel batches), `/recurring`, `/schedule`, `/channels`, `/stats`, `/preview`, `/editposts`, `/backup`, `/restore`, `/retry`, `/overdue`, `/settings`, `/help`.

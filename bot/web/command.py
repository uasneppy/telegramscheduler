"""The /web command: sends an allowed user a single-use sign-in link for the board.

Only this handler sends Telegram messages on the board's behalf. It has no aiohttp
imports; whether the board is running is read from `bot_data["web_runner"]`,
which `main.post_init` fills (None when the board is disabled or did not start).
"""

import asyncio
import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

import config
from bot.web.auth import TOKEN_TTL_SECONDS, create_login_token
from bot.web.settings import build_login_url

logger = logging.getLogger(__name__)

NOT_ENABLED_TEXT = "The web board is not enabled on this server."
PRIVATE_ONLY_TEXT = "For your safety, /web only works in a private chat with the bot."


async def web_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with a fresh sign-in link; the token is never created for a group chat."""
    message = update.message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None or chat is None:
        return

    # A link posted in a group would be visible to everyone in it.
    if chat.type != ChatType.PRIVATE:
        await message.reply_text(PRIVATE_ONLY_TEXT)
        return

    base_url = config.WEB_BASE_URL
    if context.application.bot_data.get("web_runner") is None or not base_url:
        await message.reply_text(NOT_ENABLED_TEXT)
        return

    token = await asyncio.to_thread(create_login_token, user.id)
    # Plain text: a token can contain "_", which Markdown would read as formatting.
    # The preview is off so Telegram does not fetch the link on the user's behalf.
    await message.reply_text(
        "Your sign-in link for the web board:\n\n"
        f"{build_login_url(base_url, token)}\n\n"
        f"The link is single-use and expires in {TOKEN_TTL_SECONDS // 60} minutes. "
        "Open it in your browser and press Continue. Send /web again for a new link.",
        disable_web_page_preview=True,
    )
    logger.info("Web board sign-in link sent to user %s", user.id)

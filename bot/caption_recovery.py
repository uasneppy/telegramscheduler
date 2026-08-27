"""
Caption recovery commands.

The original "automatic caption recovery" copied captions from previously
POSTED posts onto unrelated pending posts, matched only by media type and
order - silently corrupting captions while reporting success. The Bot API
provides no way to read chat history, so real recovery from history is not
possible for a bot. The commands now honestly report that and point the user
at /edit_captions, which edits captions explicitly and safely.
"""

import logging
import os
from typing import List, Dict

from telegram import Update
from telegram.ext import ContextTypes

from .database import Database
from .utils import get_media_icon

logger = logging.getLogger(__name__)


def _get_posts_without_captions(user_id: int) -> List[Dict]:
    """List the user's pending posts that have no caption."""
    conn = Database.get_connection()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, file_path, media_type, channel_id
        FROM posts
        WHERE user_id = ?
          AND status = 'pending'
          AND (description IS NULL OR description = '')
        ORDER BY id ASC
    ''', (user_id,))

    posts = [
        {'id': row[0], 'file_path': row[1], 'media_type': row[2] or 'photo', 'channel_id': row[3]}
        for row in cursor.fetchall()
    ]
    conn.close()
    return posts


async def handle_recover_captions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /recover_captions command"""
    user = update.effective_user
    if not user:
        return

    await update.message.reply_text(
        "ℹ️ *Automatic caption recovery is disabled.*\n\n"
        "Telegram bots cannot read chat history, so captions cannot be "
        "recovered automatically - the old implementation guessed and could "
        "attach captions to the wrong posts.\n\n"
        "Use /recover\\_interactive to list posts without captions, and "
        "/edit\\_captions to add captions to them.",
        parse_mode='Markdown'
    )


async def handle_recover_captions_interactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List posts without captions and point at the caption editor."""
    user = update.effective_user
    if not user:
        return

    posts = _get_posts_without_captions(user.id)

    if not posts:
        await update.message.reply_text(
            "✅ All your pending posts already have captions!"
        )
        return

    post_list = []
    for i, post in enumerate(posts[:10], 1):
        filename = os.path.basename(post['file_path'])
        post_list.append(f"{i}. {get_media_icon(post['media_type'])} {filename} (post #{post['id']})")

    more = f"\n... and {len(posts) - 10} more" if len(posts) > 10 else ""

    await update.message.reply_text(
        f"📝 Found {len(posts)} pending posts without captions:\n\n"
        + "\n".join(post_list) + more + "\n\n"
        "Use /edit_captions to add captions to them."
    )

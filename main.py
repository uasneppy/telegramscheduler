#!/usr/bin/env python3
"""
Telegram Bot for Scheduling Channel Posts
Main entry point for the application
"""

import asyncio
import logging
import os
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler,
    TypeHandler, ApplicationHandlerStop
)
from telegram.request import HTTPXRequest

from bot.handlers import (
    start_handler, mode1_handler, mode2_handler, finish_handler,
    media_handler, schedule_handler, cancel_handler, help_handler,
    callback_query_handler, channels_handler, stats_handler, reset_handler,
    clearqueue_handler, clearscheduled_handler, multibatch_handler, retry_handler,
    bulkedit_handler, recurring_mode_handler as recurring_handler,
    backup_handler, restore_handler, overdue_handler, preview_handler, settings_handler,
    recover_captions_handler, recover_captions_interactive_handler, delete_all_captions_handler,
    edit_captions_handler, editposts_handler, status_handler
)
from bot.database import init_database
from bot.scheduler import PostScheduler
from config import BOT_TOKEN, DATABASE_PATH, UPLOADS_DIR, ALLOWED_USER_IDS, ADMIN_CHAT_ID

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# SECURITY: Suppress HTTPX request logging to prevent bot token exposure in logs
# HTTPX logs full URLs at INFO level which includes the bot token
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

async def auth_gate(update, context):
    """Reject updates from users outside ALLOWED_USER_IDS before any handler runs.

    Registered in group -1 so it runs first for every update type (messages,
    callbacks, media). ApplicationHandlerStop prevents all later handlers.
    """
    user = update.effective_user
    if user is None or user.id not in ALLOWED_USER_IDS:
        user_id = user.id if user else "unknown"
        logger.warning(f"Rejected update from unauthorized user {user_id}")
        if update.callback_query:
            await update.callback_query.answer("⛔ You are not authorized to use this bot.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("⛔ You are not authorized to use this bot.")
        raise ApplicationHandlerStop

async def post_init(application):
    """Initialize scheduler after the application starts"""
    # Reuse the application's bot (and its HTTP pool) instead of a second Bot
    scheduler = PostScheduler(bot=application.bot)
    scheduler.start()
    # Store scheduler in application context
    application.bot_data['scheduler'] = scheduler

    # Register the command menu Telegram shows when the user types "/".
    # Only the everyday commands are listed - everything else is reachable
    # from the /start menu.
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Main menu & dashboard"),
            BotCommand("mode1", "Bulk upload (auto-schedule)"),
            BotCommand("mode2", "Upload with captions"),
            BotCommand("schedule", "Schedule queued posts"),
            BotCommand("stats", "Your posting statistics"),
            BotCommand("settings", "Reminders & notifications"),
            BotCommand("cancel", "Cancel current operation"),
            BotCommand("help", "All commands & help"),
        ])
        logger.info("Registered bot command menu")
    except Exception as e:
        logger.warning(f"Could not register command menu: {e}")

async def post_shutdown(application):
    """Shut the scheduler down cleanly when the application stops"""
    scheduler = application.bot_data.get('scheduler')
    if scheduler:
        scheduler.stop()

async def error_handler(update, context):
    """Log unexpected errors and optionally forward them to the admin chat."""
    logger.error("Unhandled exception while processing an update", exc_info=context.error)
    if ADMIN_CHAT_ID:
        try:
            import traceback
            tb = ''.join(traceback.format_exception(None, context.error, context.error.__traceback__))
            summary = tb[-1500:]  # last lines carry the actual error
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"🚨 Bot error:\n\n{summary}"
            )
        except Exception as e:
            logger.error(f"Could not forward error to admin chat: {e}")

def main():
    """Main function to run the bot"""
    
    # Create uploads directory if it doesn't exist
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    
    # Initialize database
    init_database()
    
    # Create HTTP request optimized for heavy file uploads
    request = HTTPXRequest(
        connection_pool_size=50,   # Increased for heavy concurrent uploads
        pool_timeout=120.0,        # Extended timeout for large files
        read_timeout=300.0,        # 5 minutes for large file downloads
        write_timeout=300.0,       # 5 minutes for large file uploads
        connect_timeout=60.0       # 1 minute connection timeout
    )
    
    # Create the Application with custom HTTP request
    application = Application.builder().token(BOT_TOKEN).request(request).build()
    
    # Set up lifecycle callbacks
    application.post_init = post_init
    application.post_shutdown = post_shutdown

    # SECURITY: user allowlist gate runs before every other handler
    if ALLOWED_USER_IDS:
        application.add_handler(TypeHandler(Update, auth_gate), group=-1)
        logger.info(f"Access restricted to {len(ALLOWED_USER_IDS)} allowed user(s)")
    else:
        logger.warning(
            "ALLOWED_USER_IDS is not set - the bot is open to EVERY Telegram user. "
            "Set ALLOWED_USER_IDS=<your telegram user id> to restrict access."
        )

    # Add handlers
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("mode1", mode1_handler))
    application.add_handler(CommandHandler("mode2", mode2_handler))
    application.add_handler(CommandHandler("multibatch", multibatch_handler))
    application.add_handler(CommandHandler("finish", finish_handler))
    application.add_handler(CommandHandler("schedule", schedule_handler))
    application.add_handler(CommandHandler("channels", channels_handler))
    application.add_handler(CommandHandler("stats", stats_handler))
    application.add_handler(CommandHandler("reset", reset_handler))
    application.add_handler(CommandHandler("clearqueue", clearqueue_handler))
    application.add_handler(CommandHandler("clearscheduled", clearscheduled_handler))
    application.add_handler(CommandHandler("bulkedit", bulkedit_handler))
    application.add_handler(CommandHandler("recurring", recurring_handler))
    application.add_handler(CommandHandler("backup", backup_handler))
    application.add_handler(CommandHandler("restore", restore_handler))
    application.add_handler(CommandHandler("settings", settings_handler))
    application.add_handler(CommandHandler("retry", retry_handler))
    application.add_handler(CommandHandler("overdue", overdue_handler))
    application.add_handler(CommandHandler("preview", preview_handler))
    application.add_handler(CommandHandler("recover_captions", recover_captions_handler))
    application.add_handler(CommandHandler("recover_interactive", recover_captions_interactive_handler))
    application.add_handler(CommandHandler("delete_all_captions", delete_all_captions_handler))
    application.add_handler(CommandHandler("edit_captions", edit_captions_handler))
    application.add_handler(CommandHandler("editposts", editposts_handler))
    application.add_handler(CommandHandler("cancel", cancel_handler))
    application.add_handler(CommandHandler("status", status_handler))
    application.add_handler(CommandHandler("help", help_handler))

    # Forward unexpected errors to the log (and admin chat if configured)
    application.add_error_handler(error_handler)
    
    # Add callback query handler for inline keyboards
    application.add_handler(CallbackQueryHandler(callback_query_handler))
    
    # Add media handlers
    application.add_handler(MessageHandler(filters.PHOTO, media_handler))
    application.add_handler(MessageHandler(filters.VIDEO, media_handler))
    application.add_handler(MessageHandler(filters.AUDIO, media_handler))
    application.add_handler(MessageHandler(filters.ANIMATION, media_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, media_handler))
    
    # Add text message handler for descriptions and other inputs
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, media_handler))
    
    logger.info("Starting bot...")
    
    # Run the bot
    application.run_polling(allowed_updates=['message', 'callback_query'])

if __name__ == '__main__':
    main()

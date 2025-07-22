#!/usr/bin/env python3
"""
Telegram Bot for Scheduling Channel Posts
Main entry point for the application
"""

import asyncio
import logging
import os
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler
from telegram.request import HTTPXRequest

from bot.handlers import (
    start_handler, mode1_handler, mode2_handler, finish_handler,
    media_handler, schedule_handler, cancel_handler, help_handler,
    callback_query_handler, channels_handler, stats_handler, reset_handler,
    clearqueue_handler, clearscheduled_handler, multibatch_handler, retry_handler
)
from bot.database import init_database
from bot.scheduler import PostScheduler
from config import BOT_TOKEN, DATABASE_PATH, UPLOADS_DIR

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

async def post_init(application):
    """Initialize scheduler after the application starts"""
    scheduler = PostScheduler()
    scheduler.start()
    # Store scheduler in application context
    application.bot_data['scheduler'] = scheduler

def main():
    """Main function to run the bot"""
    
    # Create uploads directory if it doesn't exist
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    
    # Initialize database
    init_database()
    
    # Create HTTP request with improved connection pooling
    request = HTTPXRequest(
        connection_pool_size=20,  # Increased from default 1
        pool_timeout=30.0,        # Increased from default 1.0
        read_timeout=30.0,        # Increased from default 5.0
        write_timeout=30.0,       # Increased from default 5.0
        connect_timeout=30.0      # Increased from default 5.0
    )
    
    # Create the Application with custom HTTP request
    application = Application.builder().token(BOT_TOKEN).request(request).build()
    
    # Set up post-init callback
    application.post_init = post_init
    
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
    application.add_handler(CommandHandler("retry", retry_handler))
    application.add_handler(CommandHandler("cancel", cancel_handler))
    application.add_handler(CommandHandler("help", help_handler))
    
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

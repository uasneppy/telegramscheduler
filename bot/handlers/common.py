"""Shared imports and module-level state for all handler modules."""

"""
Telegram bot handlers for different commands and interactions
"""

import os
import json
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.ext import ContextTypes

from ..database import Database
from ..scheduler import PostScheduler
from ..caption_recovery import handle_recover_captions_command, handle_recover_captions_interactive
from ..utils import (
    generate_unique_filename, save_media, calculate_schedule_times,
    format_schedule_summary, parse_schedule_input, get_current_kyiv_time,
    parse_date_input, calculate_custom_date_schedule, generate_mini_calendar,
    format_daily_schedule, get_calendar_navigation_dates, get_media_icon,
    calculate_evenly_distributed_schedule, parse_bulk_edit_input,
    get_kyiv_timezone, escape_markdown
)
import asyncio
from config import BotStates, CHANNEL_ID

logger = logging.getLogger(__name__)

# Ensure BotStates is properly imported at module level
if not hasattr(BotStates, 'WAITING_BULK_EDIT_INPUT'):
    logger.error("BotStates.WAITING_BULK_EDIT_INPUT not found - check config.py")
    # Add fallback
    BotStates.WAITING_BULK_EDIT_INPUT = "waiting_bulk_edit_input"

BOT_STARTED_AT = datetime.now()


NOTIFY_MODE_LABELS = {
    'all': '📣 Every post',
    'digest': '📮 Daily digest',
    'errors': '🔕 Errors only',
}

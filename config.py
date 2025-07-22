"""
Configuration settings for the Telegram bot
"""

import os

# Bot configuration
BOT_TOKEN = 

# Channel configuration (optional - bot supports multi-channel)
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", None)

# File paths
DATABASE_PATH = "bot_data.db"
UPLOADS_DIR = "uploads"

# Timezone settings
TIMEZONE = "Europe/Kiev"  # Kyiv timezone

# Default scheduling settings
DEFAULT_START_HOUR = 10  # 10 AM
DEFAULT_END_HOUR = 20    # 8 PM
DEFAULT_INTERVAL_HOURS = 2  # Every 2 hours

# File size limits (in bytes)
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

# Bot states
class BotStates:
    IDLE = "idle"
    MODE1_PHOTOS = "mode1_photos"
    MODE1_SCHEDULE = "mode1_schedule"
    MODE2_PHOTOS = "mode2_photos"
    MODE2_DESCRIPTION = "mode2_description"
    MODE2_SCHEDULE = "mode2_schedule"
    WAITING_SCHEDULE_INPUT = "waiting_schedule_input"
    WAITING_CHANNEL_ID = "waiting_channel_id"
    WAITING_CHANNEL_NAME = "waiting_channel_name"
    SELECTING_CHANNEL = "selecting_channel"
    # New multi-channel batch states
    MULTI_BATCH_MENU = "multi_batch_menu"
    BATCH_MODE1_PHOTOS = "batch_mode1_photos"
    BATCH_MODE2_PHOTOS = "batch_mode2_photos"
    BATCH_MODE2_DESCRIPTION = "batch_mode2_description"
    WAITING_BATCH_NAME = "waiting_batch_name"

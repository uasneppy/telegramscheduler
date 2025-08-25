"""
Configuration settings for the Telegram bot
"""

import os

# Bot configuration
BOT_TOKEN = ""

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

# File size limits (in bytes) - REMOVED FOR UNLIMITED UPLOADS
# MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB - DISABLED
MAX_FILE_SIZE = None  # No file size limit

# Bot states
class BotStates:
    IDLE = "idle"
    MODE1_PHOTOS = "mode1_photos"
    MODE2_PHOTOS = "mode2_photos"
    MODE2_DESCRIPTION = "mode2_description"
    WAITING_SCHEDULE_INPUT = "waiting_schedule_input"
    WAITING_DATE_INPUT = "waiting_date_input"
    WAITING_DESCRIPTION_INPUT = "waiting_description_input"
    WAITING_CHANNEL_ID = "waiting_channel_id"
    WAITING_CHANNEL_NAME = "waiting_channel_name"
    RECURRING_MODE = "recurring_mode"
    RECURRING_DESCRIPTION = "recurring_description"
    RECURRING_SCHEDULE = "recurring_schedule"
    BATCH_MODE1_PHOTOS = "batch_mode1_photos"
    BATCH_MODE2_PHOTOS = "batch_mode2_photos"
    BATCH_MODE2_DESCRIPTION = "batch_mode2_description"
    WAITING_BATCH_NAME = "waiting_batch_name"
    MULTI_BATCH_MENU = "multi_batch_menu"
    WAITING_BULK_EDIT_INPUT = "waiting_bulk_edit_input"
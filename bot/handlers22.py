"""
Telegram bot handlers for different commands and interactions
"""

import os
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from .database import Database
from .scheduler import PostScheduler
from .utils import (
    generate_unique_filename, save_media, calculate_schedule_times,
    format_schedule_summary, parse_schedule_input, get_current_kyiv_time,
    parse_date_input, calculate_custom_date_schedule, generate_mini_calendar,
    format_daily_schedule, get_calendar_navigation_dates, get_media_icon,
    calculate_evenly_distributed_schedule, parse_bulk_edit_input
)
from config import BotStates, CHANNEL_ID

logger = logging.getLogger(__name__)

# Scheduler will be accessed from application context

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    
    # Create main menu keyboard
    keyboard = [
        [InlineKeyboardButton("📸 Mode 1: Bulk Upload", callback_data="main_mode1")],
        [InlineKeyboardButton("📝 Mode 2: Individual Upload", callback_data="main_mode2")],
        [InlineKeyboardButton("🔄 Recurring Posts", callback_data="main_recurring")],
        [InlineKeyboardButton("📅 Calendar View", callback_data="main_calendar")],
        [InlineKeyboardButton("📺 Manage Channels", callback_data="main_channels")],
        [InlineKeyboardButton("📊 View Statistics", callback_data="main_stats")],
        [InlineKeyboardButton("❓ Help & Commands", callback_data="main_help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_message = f"""
👋 *Welcome {user.first_name}!*

🤖 *Channel Post Scheduler Bot*

*🎯 Features:*
• *Mode 1:* Bulk photo upload with auto-scheduling
• *Mode 2:* Individual photos with custom descriptions  
• *Multi-channel:* Post to different channels
• *Recurring:* Set up automatic recurring posts
• *Smart scheduling:* Kyiv timezone, custom intervals

*🕐 Default Schedule:*
10 AM to 8 PM, every 2 hours (Kyiv time)

Choose an option below to get started:
"""
    
    await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode='Markdown')
    
    # Reset user session
    Database.update_user_session(user.id, BotStates.IDLE)

async def mode1_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /mode1 command - bulk photo upload"""
    user = update.effective_user
    
    # Check if user has channels configured
    channels = Database.get_user_channels(user.id)
    
    if not channels:
        await update.message.reply_text(
            "❌ *No channels configured!*\n\n"
            "Please add a channel first using /channels command before using Mode 1.",
            parse_mode='Markdown'
        )
        return
    
    # Note: We'll clear channel-specific posts after channel selection to ensure complete separation
    
    # Always ask user to select a channel
    await prompt_channel_selection_for_mode(update, user.id, channels, mode=1)

async def recurring_mode_handler(query, user):
    """Handle the recurring posts mode"""
    # Check if user has channels configured
    channels = Database.get_user_channels(user.id)
    
    if not channels:
        await query.edit_message_text(
            "❌ *No channels configured!*\n\n"
            "Please add a channel first using /channels command before using Recurring Mode.",
            parse_mode='Markdown'
        )
        return
    
    # Always ask user to select a channel
    await prompt_channel_selection_for_recurring_mode(query, user.id, channels)

async def prompt_channel_selection_for_recurring_mode(query, user_id, channels):
    """Prompt user to select channel for recurring mode"""
    keyboard = []
    
    for channel in channels:
        # Channels are dictionaries with channel_id and channel_name
        channel_id, channel_name = channel['channel_id'], channel['channel_name']
        display_text = f"📺 {channel_name}"
        if len(display_text) > 30:
            display_text = f"📺 {channel_name[:27]}..."
        callback_data = f"recurring_channel_{channel_id}"
        keyboard.append([InlineKeyboardButton(display_text, callback_data=callback_data)])
    
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = """
🔄 *Recurring Posts Mode*

This mode is perfect for creating posts that repeat automatically!

*How it works:*
1. Select a target channel
2. Upload ONE photo/media file
3. Add a custom description  
4. Set up recurring schedule (daily, weekly, etc.)
5. The post will repeat automatically

*Select your target channel:*
"""
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_recurring_channel_selection(query, user, channel_id):
    """Handle channel selection for recurring mode"""
    # Set the user state to recurring mode
    Database.update_user_session(user.id, BotStates.RECURRING_MODE, {'channel_id': channel_id})
    
    # Clear any existing posts for this channel and mode to ensure separation
    Database.clear_user_posts(user.id, channel_id=channel_id, mode=3)  # mode 3 for recurring
    
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = """
🔄 *Recurring Mode - Ready!*

Perfect! Now upload ONE photo or media file.

*Instructions:*
1. Send your photo/media file now
2. Add a description when prompted
3. Set up your recurring schedule
4. Your post will repeat automatically

*Send your media file now!* 📷
"""
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def mode2_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /mode2 command - individual photo upload"""
    user = update.effective_user
    
    # Check if user has channels configured
    channels = Database.get_user_channels(user.id)
    
    if not channels:
        await update.message.reply_text(
            "❌ *No channels configured!*\n\n"
            "Please add a channel first using /channels command before using Mode 2.",
            parse_mode='Markdown'
        )
        return
    
    # Note: We'll clear channel-specific posts after channel selection to ensure complete separation
    
    # Always ask user to select a channel
    await prompt_channel_selection_for_mode(update, user.id, channels, mode=2)

async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle media uploads and text messages"""
    # Add null safety checks
    if not update or not update.effective_user or not update.message:
        logger.error("Invalid update object received in media_handler")
        return
        
    user = update.effective_user
    mode, session_data = Database.get_user_session(user.id)
    
    logger.info(f"Media handler called - User: {user.id}, Mode: {mode}")
    
    if update.message.photo:
        logger.info(f"Processing photo upload for user {user.id}")
        await handle_media_upload(update, context, user, mode, session_data, 'photo')
    elif update.message.video:
        logger.info(f"Processing video upload for user {user.id}")
        await handle_media_upload(update, context, user, mode, session_data, 'video')
    elif update.message.audio:
        logger.info(f"Processing audio upload for user {user.id}")
        await handle_media_upload(update, context, user, mode, session_data, 'audio')
    elif update.message.animation:
        logger.info(f"Processing animation upload for user {user.id}")
        await handle_media_upload(update, context, user, mode, session_data, 'animation')
    elif update.message.document:
        logger.info(f"Processing document upload for user {user.id}")
        await handle_media_upload(update, context, user, mode, session_data, 'document')
    elif update.message.text:
        logger.info(f"Processing text message for user {user.id}")
        await handle_text_message(update, context, user, mode, session_data)
    else:
        logger.warning(f"No valid media or text found in message from user {user.id}")

# Keep backward compatibility
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo uploads and text messages (backward compatibility)"""
    await media_handler(update, context)

async def handle_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                            user, mode: str, session_data: dict, media_type: str):
    """Handle media upload based on current mode"""
    
    logger.info(f"handle_media_upload called - User: {user.id}, Mode: {mode}, Media Type: {media_type}")
    
    # Check for batch modes first
    if mode in [BotStates.BATCH_MODE1_PHOTOS, BotStates.BATCH_MODE2_PHOTOS]:
        logger.info(f"Handling batch media upload for user {user.id}")
        await handle_batch_media_upload_wrapper(update, context, user, mode, session_data, media_type)
        return
    
    if mode not in [BotStates.MODE1_PHOTOS, BotStates.MODE2_PHOTOS, BotStates.RECURRING_MODE]:
        logger.warning(f"Invalid mode for media upload: {mode} for user {user.id}")
        if update.message:
            await update.message.reply_text(
                "Please start with /mode1, /mode2, recurring posts, or /multibatch first to upload media."
            )
        return
    
    try:
        logger.info(f"Starting media processing for user {user.id}, type: {media_type}")
        
        # Get the media file based on type
        if media_type == 'photo':
            if not update.message.photo:
                logger.error(f"No photo found in message for user {user.id}")
                return
            media_file = update.message.photo[-1]  # Get largest photo
            original_filename = f"photo_{media_file.file_id}.jpg"
        elif media_type == 'video':
            if not update.message.video:
                logger.error(f"No video found in message for user {user.id}")
                return
            media_file = update.message.video
            original_filename = f"video_{media_file.file_id}.mp4"
        elif media_type == 'audio':
            if not update.message.audio:
                logger.error(f"No audio found in message for user {user.id}")
                return
            media_file = update.message.audio
            original_filename = f"audio_{media_file.file_id}.mp3"
        elif media_type == 'animation':
            if not update.message.animation:
                logger.error(f"No animation found in message for user {user.id}")
                return
            media_file = update.message.animation
            original_filename = f"animation_{media_file.file_id}.gif"
        elif media_type == 'document':
            if not update.message.document:
                logger.error(f"No document found in message for user {user.id}")
                return
            media_file = update.message.document
            original_filename = f"document_{media_file.file_id}_{media_file.file_name or 'file'}"
        else:
            logger.error(f"Unsupported media type: {media_type}")
            if update.message:
                await update.message.reply_text("Unsupported media type.")
            return
        
        logger.info(f"Media file extracted: {original_filename} for user {user.id}")
        
        file = await context.bot.get_file(media_file.file_id)
        logger.info(f"Got file from Telegram API for user {user.id}")
        
        # Generate unique filename
        filename = generate_unique_filename(original_filename)
        logger.info(f"Generated filename: {filename} for user {user.id}")
        
        # Download and save media
        file_data = await file.download_as_bytearray()
        logger.info(f"Downloaded file data ({len(file_data)} bytes) for user {user.id}")
        
        file_path = save_media(bytes(file_data), filename, media_type)
        logger.info(f"Saved media to: {file_path} for user {user.id}")
        
        if mode == BotStates.MODE1_PHOTOS:
            logger.info(f"Handling Mode 1 media for user {user.id}")
            await handle_mode1_media(update, user, file_path, media_type, session_data)
        elif mode == BotStates.MODE2_PHOTOS:
            logger.info(f"Handling Mode 2 media for user {user.id}")
            await handle_mode2_media(update, user, file_path, media_type, session_data)
        elif mode == BotStates.RECURRING_MODE:
            logger.info(f"Handling Recurring mode media for user {user.id}")
            await handle_recurring_media(update, user, file_path, media_type, session_data)
            
    except Exception as e:
        logger.error(f"Error handling {media_type} upload for user {user.id}: {e}", exc_info=True)
        if update.message:
            await update.message.reply_text(
                f"❌ Error processing {media_type}: {e}\nPlease try again."
            )

# Keep backward compatibility
async def handle_photo_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                            user, mode: str, session_data: dict):
    """Handle photo upload based on current mode (backward compatibility)"""
    await handle_media_upload(update, context, user, mode, session_data, 'photo')

async def handle_mode1_media(update: Update, user, file_path: str, media_type: str, session_data: dict):
    """Handle media upload in Mode 1 (bulk)"""
    
    # Get the selected channel from session data
    selected_channel_id = session_data.get('selected_channel_id')
    
    # Add media to database with the assigned channel
    post_id = Database.add_post(user.id, file_path, media_type=media_type, mode=1, channel_id=selected_channel_id)
    
    # Update session data
    media_items = session_data.get('media_items', [])
    media_items.append({
        'post_id': post_id,
        'file_path': file_path,
        'media_type': media_type,
        'uploaded_at': datetime.now().isoformat()
    })
    
    session_data['media_items'] = media_items
    Database.update_user_session(user.id, BotStates.MODE1_PHOTOS, session_data)
    
    # Format media type for display
    media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
    
    await update.message.reply_text(
        f"✅ {media_icon} {media_type.title()} {len(media_items)} uploaded successfully!\n"
        f"Total media: {len(media_items)}\n\n"
        f"Continue uploading media or use /schedule when ready."
    )

# Keep backward compatibility
async def handle_mode1_photo(update: Update, user, file_path: str, session_data: dict):
    """Handle photo upload in Mode 1 (bulk) - backward compatibility"""
    await handle_mode1_media(update, user, file_path, 'photo', session_data)

async def handle_mode2_media(update: Update, user, file_path: str, media_type: str, session_data: dict):
    """Handle media upload in Mode 2 (individual)"""
    
    # Store media path and type, ask for description
    session_data['current_media_path'] = file_path
    session_data['current_media_type'] = media_type
    Database.update_user_session(user.id, BotStates.MODE2_DESCRIPTION, session_data)
    
    # Format media type for display
    media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
    
    await update.message.reply_text(
        f"📝 {media_icon} {media_type.title()} received! Please send a description for this {media_type} (or send 'skip' for no description):"
    )

# Keep backward compatibility
async def handle_mode2_photo(update: Update, user, file_path: str, session_data: dict):
    """Handle photo upload in Mode 2 (individual) - backward compatibility"""
    await handle_mode2_media(update, user, file_path, 'photo', session_data)

async def handle_recurring_media(update: Update, user, file_path: str, media_type: str, session_data: dict):
    """Handle media upload in Recurring Mode"""
    
    # Get the selected channel from session data
    selected_channel_id = session_data.get('channel_id')
    
    if not selected_channel_id:
        await update.message.reply_text("❌ No channel selected. Please start again.")
        return
    
    # Store media path and type, ask for description
    session_data['current_media_path'] = file_path
    session_data['current_media_type'] = media_type
    session_data['channel_id'] = selected_channel_id
    Database.update_user_session(user.id, BotStates.RECURRING_DESCRIPTION, session_data)
    
    # Format media type for display
    media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
    
    await update.message.reply_text(
        f"📝 {media_icon} {media_type.title()} received for recurring posts!\n\n"
        f"Please send a description for this {media_type} (or send 'skip' for no description):"
    )

async def handle_recurring_description(update: Update, user, description: str, session_data: dict):
    """Handle description input in Recurring Mode"""
    
    file_path = session_data.get('current_media_path')
    media_type = session_data.get('current_media_type', 'photo')
    selected_channel_id = session_data.get('channel_id')
    
    if not file_path or not selected_channel_id:
        await update.message.reply_text("❌ No media or channel found. Please start again.")
        return
    
    # Process description
    final_description = None if description.lower() == 'skip' else description
    
    # Add the post to database with recurring information
    post_id = Database.add_post(
        user.id, 
        file_path, 
        description=final_description or "", 
        media_type=media_type, 
        mode=3,  # mode 3 for recurring
        channel_id=selected_channel_id
    )
    
    # Format media type for display
    media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
    
    # Create scheduling options for recurring posts
    keyboard = [
        [InlineKeyboardButton("📅 Daily", callback_data=f"recurring_schedule_daily_{post_id}")],
        [InlineKeyboardButton("📅 Every 3 Days", callback_data=f"recurring_schedule_3days_{post_id}")],
        [InlineKeyboardButton("📅 Weekly", callback_data=f"recurring_schedule_weekly_{post_id}")],
        [InlineKeyboardButton("📅 Custom Interval", callback_data=f"recurring_schedule_custom_{post_id}")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    desc_text = f"*Description:* {final_description}" if final_description else "*No description*"
    
    await update.message.reply_text(
        f"✅ {media_icon} *Recurring Post Ready!*\n\n"
        f"{desc_text}\n\n"
        f"*Choose your recurring schedule:*",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    # Update session to recurring schedule state
    session_data['recurring_post_id'] = post_id
    Database.update_user_session(user.id, BotStates.RECURRING_SCHEDULE, session_data)

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            user, mode: str, session_data: dict):
    """Handle text messages based on current mode"""
    
    text = update.message.text.strip()
    
    if mode == BotStates.MODE2_DESCRIPTION:
        await handle_mode2_description(update, user, text, session_data)
    elif mode == BotStates.BATCH_MODE2_DESCRIPTION:
        await handle_batch_mode2_description(update, user, text, session_data)
    elif mode == BotStates.RECURRING_DESCRIPTION:
        await handle_recurring_description(update, user, text, session_data)
    elif mode == BotStates.WAITING_BATCH_NAME:
        await handle_batch_name_input(update, user, text, session_data)
    elif mode == BotStates.WAITING_SCHEDULE_INPUT:
        await handle_schedule_input(update, user, text, session_data)
    elif mode == BotStates.WAITING_DATE_INPUT:
        await handle_date_input(update, user, text, session_data, context)
    elif mode == BotStates.WAITING_DESCRIPTION_INPUT:
        await handle_description_input(update, user, text, session_data, context)
    elif mode in [BotStates.WAITING_CHANNEL_ID, BotStates.WAITING_CHANNEL_NAME]:
        await handle_channel_input(update, user, text, session_data)
    elif mode == "waiting_recurring_hours":
        await handle_recurring_hours_input(update, user, text, session_data)
    elif mode == "waiting_recurring_count":
        await handle_recurring_count_input(update, user, text, session_data)
    elif mode == "waiting_recurring_date":
        await handle_recurring_date_input(update, user, text, session_data)
    elif mode == BotStates.WAITING_BULK_EDIT_INPUT:
        await handle_bulk_edit_input(update, user, text, session_data)
    else:
        await update.message.reply_text(
            "I'm not sure what to do with this message. Use /help for available commands."
        )

async def handle_mode2_description(update: Update, user, description: str, session_data: dict):
    """Handle description input in Mode 2"""
    
    file_path = session_data.get('current_media_path') or session_data.get('current_photo_path')
    media_type = session_data.get('current_media_type', 'photo')
    
    if not file_path:
        await update.message.reply_text("❌ No media found. Please upload media first.")
        return
    
    # Process description
    final_description = None if description.lower() == 'skip' else description
    
    # Get the selected channel from session data
    selected_channel_id = session_data.get('selected_channel_id')
    
    # Add media to database with the assigned channel
    post_id = Database.add_post(user.id, file_path, media_type=media_type, description=final_description, mode=2, channel_id=selected_channel_id)
    
    # Update session data
    media_items = session_data.get('media_items', [])
    media_items.append({
        'post_id': post_id,
        'file_path': file_path,
        'media_type': media_type,
        'description': final_description,
        'uploaded_at': datetime.now().isoformat()
    })
    
    session_data['media_items'] = media_items
    session_data['current_media_path'] = None
    session_data['current_media_type'] = None
    Database.update_user_session(user.id, BotStates.MODE2_PHOTOS, session_data)
    
    # Format media type for display
    media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
    desc_text = f'"{final_description}"' if final_description else "no description"
    
    await update.message.reply_text(
        f"✅ {media_icon} {media_type.title()} {len(media_items)} saved with {desc_text}!\n\n"
        f"Send another photo or use /finish when done."
    )

async def schedule_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /schedule command"""
    user = update.effective_user
    mode, session_data = Database.get_user_session(user.id)
    
    # Check if user has unscheduled photos to schedule
    pending_posts = Database.get_unscheduled_posts(user.id)
    
    if not pending_posts:
        await update.message.reply_text(
            "❌ No photos to schedule. Please upload photos first using /mode1 or /mode2."
        )
        return
    
    # Check if user has channels configured
    channels = Database.get_user_channels(user.id)
    
    if not channels:
        await update.message.reply_text(
            "❌ *No channels configured!*\n\n"
            "Please add a channel first using /channels command.",
            parse_mode='Markdown'
        )
        return
    
    # Get current scheduling config
    start_hour, end_hour, interval_hours = Database.get_scheduling_config(user.id)
    
    # Group posts by their assigned channels
    posts_by_channel = {}
    for post in pending_posts:
        channel_id = post['channel_id']
        if channel_id not in posts_by_channel:
            posts_by_channel[channel_id] = []
        posts_by_channel[channel_id].append(post)
    
    # Build channel info text showing posts per channel
    channel_info = f"\n*Posts by Channel:*\n"
    for channel_id, posts in posts_by_channel.items():
        channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
        channel_name = channel['channel_name'] if channel else f"Unknown ({channel_id})"
        channel_info += f"• {channel_name}: {len(posts)} posts\n"
    
    keyboard = []
    
    # Since posts already have channels assigned, proceed directly to scheduling
    keyboard.append([InlineKeyboardButton("✅ Schedule All Posts", callback_data="schedule_current")])
    keyboard.append([InlineKeyboardButton("⚙️ Change Settings", callback_data="schedule_custom")])
    keyboard.append([InlineKeyboardButton("📅 Custom Date", callback_data="schedule_custom_date")])
    keyboard.append([InlineKeyboardButton("🔄 Recurring Schedule", callback_data="schedule_recurring")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="schedule_cancel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Get preview of scheduled posts by channel
    posts_by_channel = Database.get_scheduled_posts_by_channel(user.id)
    preview_text = ""
    if posts_by_channel:
        preview_text = "\n\n*Current scheduled posts:*\n"
        for channel_key, posts in posts_by_channel.items():
            preview_text += f"• {channel_key}: {len(posts)} posts\n"

    message = f"""
📅 *Schedule {len(pending_posts)} Posts*

*Current Settings:*
• Start: {start_hour}:00 (Kyiv time)
• End: {end_hour}:00 (Kyiv time)  
• Interval: Every {interval_hours} hours

{channel_info}
*Preview Schedule:*
{format_schedule_summary(calculate_schedule_times(start_hour, end_hour, interval_hours, len(pending_posts)))}{preview_text}

Choose an option:
"""
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    data = query.data
    
    logger.info(f"Callback received - User: {user.id}, Data: {data}")
    
    if data == "schedule_current":
        await execute_scheduling(query, user, context)
    elif data == "schedule_custom":
        await prompt_custom_schedule(query, user)
    elif data == "schedule_custom_date":
        await prompt_custom_date_schedule(query, user)
    elif data == "schedule_recurring":
        await prompt_recurring_schedule(query, user)
    elif data == "schedule_cancel":
        await query.edit_message_text("❌ Scheduling cancelled.")
    # Remove this obsolete channel selection - posts already have channels assigned
    elif data.startswith("channels_"):
        await handle_channel_callback(query, user, data)
    elif data.startswith("select_channel_") or data.startswith("remove_channel_"):
        await handle_channel_selection(query, user, data, context)
    elif data.startswith("reset_"):
        await handle_reset_callback(query, user)
    elif data.startswith("main_"):
        await handle_main_menu_callback(query, user, data)
    elif data == "back_to_main":
        await show_main_menu(query, user)
    elif data.startswith("help_"):
        await handle_help_callback(query, user, data)
    elif data.startswith("recurring_channel_"):
        channel_id = data.replace("recurring_channel_", "")
        await handle_recurring_channel_selection(query, user, channel_id)
    elif data.startswith("recurring_"):
        await handle_recurring_callback(query, user, data)
    elif data.startswith("clearqueue_"):
        await handle_clearqueue_callback(query, user, data)
    elif data.startswith("clearscheduled_"):
        await handle_clearscheduled_callback(query, user, data)
    elif data.startswith("batch_"):
        await handle_batch_callback(query, user, data)
    elif data.startswith("batch_mode"):
        await handle_batch_mode_callback(query, user, data)
    elif data.startswith("retry_"):
        await handle_retry_callback(query, user, data)
    elif data.startswith("mode1_channel_") or data.startswith("mode2_channel_"):
        await handle_mode_channel_selection(query, user, data)
    elif data == "stats_channels":
        await stats_channels_handler(query, user)
    elif data.startswith("stats_channel_"):
        channel_id = data.replace("stats_channel_", "")
        await stats_channel_details_handler(query, user, channel_id)
    elif data.startswith("edit_mode2_"):
        channel_id = data.replace("edit_mode2_", "")
        logger.info(f"Edit Mode2 callback triggered for user {user.id}, channel {channel_id}")
        await edit_mode2_posts_handler(query, user, channel_id)
    elif data.startswith("edit_post_"):
        post_id = int(data.replace("edit_post_", ""))
        logger.info(f"Edit post callback triggered for user {user.id}, post {post_id}")
        await edit_post_handler(query, user, post_id)
    elif data.startswith("edit_schedule_"):
        post_id = int(data.replace("edit_schedule_", ""))
        logger.info(f"Edit schedule callback triggered for user {user.id}, post {post_id}")
        await edit_post_schedule_handler(query, user, post_id)
    elif data.startswith("edit_description_"):
        post_id = int(data.replace("edit_description_", ""))
        logger.info(f"Edit description callback triggered for user {user.id}, post {post_id}")
        await edit_post_description_handler(query, user, post_id)
    elif data.startswith("cal_"):
        await handle_calendar_callback(query, user, data)
    elif data.startswith("bulkedit_") or data == "bulkedit_back":
        logger.info(f"Handling bulk edit callback: {data}")
        await handle_bulk_edit_callback(query, user, data)
    else:
        logger.warning(f"Unhandled callback data: {data} from user {user.id}")

async def execute_scheduling(query, user, context=None, selected_channel_id=None):
    """Execute scheduling with current settings"""
    pending_posts = Database.get_unscheduled_posts(user.id)
    
    if not pending_posts:
        await query.edit_message_text("❌ No posts to schedule.")
        return
    
    # Get user's channels for display purposes
    channels = Database.get_user_channels(user.id)
    if not channels:
        await query.edit_message_text(
            "❌ *No channels configured!*\n\n"
            "Please add a channel first using /channels command.",
            parse_mode='Markdown'
        )
        return
    
    # Group posts by their assigned channels (they already have channel_id)
    posts_by_channel = {}
    for post in pending_posts:
        channel_id = post['channel_id']
        if channel_id not in posts_by_channel:
            posts_by_channel[channel_id] = []
        posts_by_channel[channel_id].append(post)
    
    # Get scheduling config
    start_hour, end_hour, interval_hours = Database.get_scheduling_config(user.id)
    
    # Calculate schedule times for all posts together
    schedule_times = calculate_schedule_times(start_hour, end_hour, interval_hours, len(pending_posts))
    
    # Schedule all posts
    post_ids = [post['id'] for post in pending_posts]
    scheduler_available = False
    if context and context.application and context.application.bot_data:
        scheduler = context.application.bot_data.get('scheduler')
        if scheduler:
            try:
                await scheduler.schedule_posts(post_ids, schedule_times)
                scheduler_available = True
                logger.info(f"Successfully scheduled {len(post_ids)} posts with scheduler")
            except Exception as e:
                logger.error(f"Failed to schedule posts: {e}")
        else:
            logger.warning("Scheduler not found in bot_data")
    else:
        logger.warning("Context, application, or bot_data not available for scheduler")
    
    if not scheduler_available:
        logger.warning("Scheduler not available - posts added to database but not scheduled")
        # Still update the database with scheduled times manually if scheduler fails
        conn = Database.get_connection()
        cursor = conn.cursor()
        for post_id, scheduled_time in zip(post_ids, schedule_times):
            cursor.execute(
                'UPDATE posts SET scheduled_time = ? WHERE id = ?',
                (scheduled_time.isoformat(), post_id)
            )
        conn.commit()
        conn.close()
        logger.info(f"Manually updated {len(post_ids)} posts with scheduled times")
    
    # Build summary message showing channels
    channel_summary = ""
    for channel_id, posts in posts_by_channel.items():
        channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
        channel_name = channel['channel_name'] if channel else f"Unknown ({channel_id})"
        channel_summary += f"• *{channel_name}*: {len(posts)} posts\n"
    
    await query.edit_message_text(
        f"✅ *Successfully scheduled {len(pending_posts)} posts!*\n\n"
        f"*Channels:*\n{channel_summary}\n"
        f"*Schedule:*\n{format_schedule_summary(schedule_times)}\n"
        f"You'll receive notifications when each post is published.",
        parse_mode='Markdown'
    )
    
    # Clear only the posts that were just scheduled (channel-specific clearing)
    # Get the unique channels that were scheduled
    scheduled_channels = set(post['channel_id'] for post in pending_posts)
    for channel_id in scheduled_channels:
        Database.clear_queued_posts(user.id, channel_id)
    
    # Reset user session
    Database.update_user_session(user.id, BotStates.IDLE)

async def prompt_custom_schedule(query, user):
    """Prompt user for custom schedule settings"""
    Database.update_user_session(user.id, BotStates.WAITING_SCHEDULE_INPUT)
    
    await query.edit_message_text(
        "⚙️ *Custom Schedule Settings*\n\n"
        "Please send your schedule in this format:\n"
        "`start_hour end_hour interval_hours`\n\n"
        "*Examples:*\n"
        "• `10 20 2` - 10am to 8pm, every 2 hours\n"
        "• `9 18 3` - 9am to 6pm, every 3 hours\n"
        "• `8 22 1` - 8am to 10pm, every hour\n\n"
        "*Note:* Times are in Kyiv timezone (24-hour format)",
        parse_mode='Markdown'
    )

async def prompt_custom_date_schedule(query, user):
    """Prompt user for custom date settings"""
    Database.update_user_session(user.id, BotStates.WAITING_DATE_INPUT)
    
    await query.edit_message_text(
        "📅 *Custom Date Scheduling*\n\n"
        "Please send your start date and time settings in this format:\n"
        "`YYYY-MM-DD HH:MM interval_hours`\n\n"
        "*Examples:*\n"
        "• `2025-07-25 10:00 2` - Start July 25th at 10am, every 2 hours\n"
        "• `2025-07-30 14:30 3` - Start July 30th at 2:30pm, every 3 hours\n"
        "• `2025-08-01 09:00 1` - Start August 1st at 9am, every hour\n\n"
        "*Note:* Times are in Kyiv timezone",
        parse_mode='Markdown'
    )

async def handle_date_input(update: Update, user, text: str, session_data: dict, context=None):
    """Handle custom date input for both bulk scheduling and individual post editing"""
    
    # Check if we're editing a specific post
    editing_post_id = session_data.get('editing_post_id')
    
    if editing_post_id:
        # Handle individual post editing
        await handle_individual_post_edit(update, user, text, editing_post_id, context)
        return
    
    # Handle bulk scheduling (original functionality)
    valid, start_datetime, interval_hours, message = parse_date_input(text)
    
    if not valid:
        await update.message.reply_text(f"❌ {message}\n\nPlease try again:")
        return
    
    # Get pending posts
    pending_posts = Database.get_unscheduled_posts(user.id)
    
    if not pending_posts:
        await update.message.reply_text("❌ No posts to schedule.")
        Database.update_user_session(user.id, BotStates.IDLE)
        return
    
    # Calculate schedule times starting from the custom date
    schedule_times = calculate_custom_date_schedule(start_datetime, interval_hours, len(pending_posts))
    
    # Execute the scheduling with custom dates
    await execute_custom_date_scheduling(update, user, pending_posts, schedule_times, context)

async def handle_individual_post_edit(update: Update, user, text: str, post_id: int, context=None):
    """Handle editing of individual post schedule"""
    # Parse the date/time input (simpler format for individual posts)
    try:
        from datetime import datetime
        from bot.utils import get_kyiv_timezone
        
        # Expected format: YYYY-MM-DD HH:MM
        if len(text.strip()) != 16 or text.count('-') != 2 or text.count(':') != 1:
            await update.message.reply_text(
                "❌ Invalid format. Please use: `YYYY-MM-DD HH:MM`\n\n"
                "Example: `2025-07-25 14:30`"
            )
            return
        
        # Parse the datetime
        naive_dt = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
        kyiv_tz = get_kyiv_timezone()
        scheduled_dt = kyiv_tz.localize(naive_dt)
        
        # Validate future date
        from bot.utils import get_current_kyiv_time
        current_time = get_current_kyiv_time()
        
        if scheduled_dt <= current_time:
            await update.message.reply_text(
                "❌ The scheduled time must be in the future.\n\n"
                "Please enter a future date and time."
            )
            return
        
        # Update the post schedule in database
        success = Database.update_post_schedule(post_id, scheduled_dt)
        
        if not success:
            await update.message.reply_text("❌ Failed to update post schedule. Please try again.")
            return
        
        # Update the scheduler
        scheduler = None
        if context and context.application and context.application.bot_data:
            scheduler = context.application.bot_data.get('scheduler')
        
        if scheduler:
            try:
                await scheduler.schedule_posts([post_id], [scheduled_dt])
            except Exception as e:
                logger.error(f"Failed to reschedule post {post_id}: {e}")
        
        # Get post details for confirmation
        conn = Database.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT description, channel_id FROM posts 
            WHERE id = ? AND user_id = ?
        ''', (post_id, user.id))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            description, channel_id = row
            
            # Get channel name
            channels = Database.get_user_channels(user.id)
            channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
            channel_name = channel['channel_name'] if channel else channel_id
            
            desc_text = description[:50] + "..." if description and len(description) > 50 else description or "No description"
            
            await update.message.reply_text(
                f"✅ *Post Updated Successfully!*\n\n"
                f"*Post #{post_id}:* {desc_text}\n"
                f"*Channel:* {channel_name}\n"
                f"*New Schedule:* {scheduled_dt.strftime('%Y-%m-%d %H:%M')} (Kyiv)\n\n"
                "The post will be published at the new time.",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                f"✅ Post #{post_id} schedule updated to {scheduled_dt.strftime('%Y-%m-%d %H:%M')} (Kyiv)!"
            )
        
        # Reset user session
        Database.update_user_session(user.id, BotStates.IDLE)
        
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid date/time format. Please use: `YYYY-MM-DD HH:MM`\n\n"
            "Example: `2025-07-25 14:30`"
        )
    except Exception as e:
        logger.error(f"Error updating post schedule: {e}")
        await update.message.reply_text(
            "❌ Error updating post schedule. Please try again."
        )

async def execute_custom_date_scheduling(update: Update, user, pending_posts: list, schedule_times: list, context=None):
    """Execute scheduling with custom dates"""
    try:
        # Get the scheduler from context or create new one
        scheduler = None
        if context and context.application and context.application.bot_data:
            scheduler = context.application.bot_data.get('scheduler')
            
        if not scheduler:
            from .scheduler import PostScheduler
            scheduler = PostScheduler()
        
        # Prepare post IDs and schedule times for batch scheduling
        post_ids = []
        valid_schedule_times = []
        
        for i, post in enumerate(pending_posts):
            if i < len(schedule_times):
                scheduled_time = schedule_times[i]
                
                # Update the post with the scheduled time
                Database.update_post_schedule(post['id'], scheduled_time)
                
                # Add to batch scheduling lists
                post_ids.append(post['id'])
                valid_schedule_times.append(scheduled_time)
        
        # Schedule all posts at once using the scheduler
        if post_ids and valid_schedule_times:
            await scheduler.schedule_posts(post_ids, valid_schedule_times)
        
        # Get channels for summary
        channels = Database.get_user_channels(user.id)
        channel_summary = ""
        posts_by_channel = {}
        for post in pending_posts:
            channel_id = post['channel_id']
            if channel_id not in posts_by_channel:
                posts_by_channel[channel_id] = []
            posts_by_channel[channel_id].append(post)
        
        for channel_id, posts in posts_by_channel.items():
            channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
            channel_name = channel['channel_name'] if channel else f"Unknown ({channel_id})"
            channel_summary += f"• {channel_name}: {len(posts)} posts\n"
        
        await update.message.reply_text(
            f"✅ Successfully scheduled {len(pending_posts)} posts with custom dates!\n\n"
            f"Channels:\n{channel_summary}\n"
            f"Schedule:\n{format_schedule_summary(schedule_times)}\n"
            f"You'll receive notifications when each post is published."
        )
        
        # Clear only the posts that were just scheduled (channel-specific clearing)
        # Get the unique channels that were scheduled
        scheduled_channels = set(post['channel_id'] for post in pending_posts)
        for channel_id in scheduled_channels:
            Database.clear_queued_posts(user.id, channel_id)
        
        # Reset user session
        Database.update_user_session(user.id, BotStates.IDLE)
        
    except Exception as e:
        logger.error(f"Error in custom date scheduling: {e}")
        await update.message.reply_text(
            f"❌ Error scheduling posts: {str(e)}\n\n"
            "Please try again or contact support."
        )
        Database.update_user_session(user.id, BotStates.IDLE)

async def handle_schedule_input(update: Update, user, text: str, session_data: dict):
    """Handle custom schedule input"""
    valid, start_hour, end_hour, interval_hours, message = parse_schedule_input(text)
    
    if not valid:
        await update.message.reply_text(f"❌ {message}\n\nPlease try again:")
        return
    
    # Update scheduling config
    Database.update_scheduling_config(user.id, start_hour, end_hour, interval_hours)
    
    # Get pending posts
    pending_posts = Database.get_pending_posts(user.id)
    
    if not pending_posts:
        await update.message.reply_text("❌ No posts to schedule.")
        Database.update_user_session(user.id, BotStates.IDLE)
        return
    
    # Calculate and show preview
    schedule_times = calculate_schedule_times(start_hour, end_hour, interval_hours, len(pending_posts))
    
    # Check channels for confirmation
    channels = Database.get_user_channels(user.id)
    
    if len(channels) > 1:
        keyboard = [
            [InlineKeyboardButton("📺 Select Channel & Confirm", callback_data="schedule_select_channel")],
            [InlineKeyboardButton("❌ Cancel", callback_data="schedule_cancel")]
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("✅ Confirm Schedule", callback_data="schedule_current")],
            [InlineKeyboardButton("❌ Cancel", callback_data="schedule_cancel")]
        ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"✅ *Schedule Updated!*\n\n"
        f"*New Settings:*\n"
        f"• Start: {start_hour}:00 (Kyiv time)\n"
        f"• End: {end_hour}:00 (Kyiv time)\n"
        f"• Interval: Every {interval_hours} hours\n\n"
        f"*Preview for {len(pending_posts)} posts:*\n"
        f"{format_schedule_summary(schedule_times)}",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def finish_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /finish command for Mode 2 and batch modes"""
    user = update.effective_user
    mode, session_data = Database.get_user_session(user.id)
    
    # Check for batch modes first
    if mode in [BotStates.BATCH_MODE1_PHOTOS, BotStates.BATCH_MODE2_PHOTOS]:
        await batch_finish_handler(update, context)
        return
    
    if mode != BotStates.MODE2_PHOTOS:
        await update.message.reply_text(
            "This command is only available in Mode 2 or batch modes. Use /mode2 or /multibatch to start."
        )
        return
    
    media_items = session_data.get('media_items', [])
    
    if not media_items:
        await update.message.reply_text(
            "❌ No media uploaded yet. Upload some media first!"
        )
        return
    
    await update.message.reply_text(
        f"✅ *Mode 2 Complete!*\n\n"
        f"You've uploaded {len(media_items)} media files with descriptions.\n"
        f"Use /schedule to set posting times.",
        parse_mode='Markdown'
    )
    
    # Update session to idle
    Database.update_user_session(user.id, BotStates.IDLE)

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel command"""
    user = update.effective_user
    mode, session_data = Database.get_user_session(user.id)
    
    if mode == BotStates.IDLE:
        await update.message.reply_text("Nothing to cancel.")
        return
    
    # Cancel any scheduled posts
    # Get scheduler from application context
    if context and context.application:
        scheduler = context.application.bot_data.get('scheduler')
        if scheduler:
            scheduler.cancel_user_posts(user.id)
    
    # Reset user session
    Database.update_user_session(user.id, BotStates.IDLE)
    
    await update.message.reply_text(
        "❌ *Operation Cancelled*\n\n"
        "All pending posts have been cleared.\n"
        "Use /mode1 or /mode2 to start again.",
        parse_mode='Markdown'
    )

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    user = update.effective_user
    
    # Create inline keyboard with options
    keyboard = [
        [InlineKeyboardButton("📸 Mode 1 Help", callback_data="help_mode1"),
         InlineKeyboardButton("📝 Mode 2 Help", callback_data="help_mode2")],
        [InlineKeyboardButton("🔄 Recurring Help", callback_data="help_recurring"),
         InlineKeyboardButton("📺 Channels Help", callback_data="help_channels")],
        [InlineKeyboardButton("🔧 Management Help", callback_data="help_management"),
         InlineKeyboardButton("📊 Batches Help", callback_data="help_batches")],
        [InlineKeyboardButton("📅 View Scheduled Posts", callback_data="help_scheduled_posts")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    help_text = """
❓ *Help & Commands*

*🎯 Quick Start:*
1. Add a channel using 📺 Manage Channels
2. Choose your upload mode (Mode 1, Mode 2, or Recurring)
3. Upload your media files (photos, videos, audio, documents)
4. Schedule them with flexible timing options

*📱 Core Commands:*
• `/start` - Main menu with all options
• `/mode1` - Bulk media upload (auto descriptions)
• `/mode2` - Individual media upload (custom descriptions)
• `/recurring` - Single post recurring scheduler
• `/schedule` - Schedule uploaded media
• `/channels` - Manage multiple channels
• `/stats` - Detailed statistics & post management

*🔧 Management Commands:*
• `/multibatch` - Advanced multi-channel batch system
• `/bulkedit` - Redistribute scheduled posts evenly across time range
• `/retry` - Retry failed posts (individual/bulk/by channel)
• `/clearqueue` - Clear pending (unscheduled) posts
• `/clearscheduled` - Clear scheduled posts
• `/reset` - Clear all user data
• `/cancel` - Cancel current operation
• `/help` - This comprehensive help

*🕐 Scheduling Options:*
• Immediate posting
• Hourly intervals (10 AM - 8 PM Kyiv time)
• Custom date/time with intervals
• Recurring posts (daily, weekly, custom)
• Multi-channel batch scheduling

*📱 Supported Media:*
Photos 📸, Videos 🎥, Audio 🎵, GIFs 🎬, Documents 📄

Choose a topic for detailed help:
"""
    
    await update.message.reply_text(help_text, reply_markup=reply_markup, parse_mode='Markdown')

async def channels_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /channels command - manage user channels"""
    user = update.effective_user
    
    # Get user's channels
    channels = Database.get_user_channels(user.id)
    
    keyboard = []
    
    if channels:
        keyboard.append([InlineKeyboardButton("📋 View All Channels", callback_data="channels_list")])
        keyboard.append([InlineKeyboardButton("➕ Add New Channel", callback_data="channels_add")])
        keyboard.append([InlineKeyboardButton("🗑️ Remove Channel", callback_data="channels_remove")])
    else:
        keyboard.append([InlineKeyboardButton("➕ Add Your First Channel", callback_data="channels_add")])
    
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="channels_cancel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if channels:
        message = f"""
📺 *Channel Management*

You have {len(channels)} channel(s) configured.

*Channels:*
"""
        for i, channel in enumerate(channels[:5], 1):  # Show max 5 channels
            message += f"{i}. 📺 {channel['channel_name']} ({channel['channel_id']})\n"
        
        if len(channels) > 5:
            message += f"... and {len(channels) - 5} more\n"
            
        message += "\nChoose an action:"
    else:
        message = """
📺 *Channel Management*

You haven't added any channels yet. Add your first channel to start posting!

A channel ID can be:
• @channelname (for public channels)
• -1001234567890 (for private channels/groups)

You need to be an admin of the channel and add your bot as an admin too.
"""
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_channel_input(update: Update, user, text: str, session_data: dict):
    """Handle channel ID or name input"""
    mode, _ = Database.get_user_session(user.id)
    
    if mode == BotStates.WAITING_CHANNEL_ID:
        # Validate channel ID format
        channel_id = text.strip()
        if not (channel_id.startswith('@') or (channel_id.startswith('-') and channel_id[1:].isdigit())):
            await update.message.reply_text(
                "❌ Invalid channel ID format.\n\n"
                "Please provide:\n"
                "• @channelname (for public channels)\n"
                "• -1001234567890 (for private channels)\n\n"
                "Try again:"
            )
            return
        
        # Store channel ID and ask for name
        session_data['new_channel_id'] = channel_id
        Database.update_user_session(user.id, BotStates.WAITING_CHANNEL_NAME, session_data)
        
        await update.message.reply_text(
            f"✅ Channel ID saved: {channel_id}\n\n"
            "Now enter a friendly name for this channel:"
        )
        
    elif mode == BotStates.WAITING_CHANNEL_NAME:
        channel_name = text.strip()
        channel_id = session_data.get('new_channel_id')
        
        if not channel_name:
            await update.message.reply_text("Please enter a valid channel name:")
            return
        
        # Add the channel
        success = Database.add_user_channel(user.id, channel_id, channel_name, False)
        
        if success:
            await update.message.reply_text(
                f"✅ *Channel Added Successfully!*\n\n"
                f"*Name:* {channel_name}\n"
                f"*ID:* {channel_id}\n\n"
                f"You can now use this channel for posting. Use /channels to manage your channels.",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "❌ Failed to add channel. It might already exist or there was an error."
            )
        
        # Reset session
        Database.update_user_session(user.id, BotStates.IDLE)

async def handle_channel_callback(query, user, data):
    """Handle channel management callbacks"""
    action = data.replace("channels_", "")
    
    if action == "add":
        Database.update_user_session(user.id, BotStates.WAITING_CHANNEL_ID)
        await query.edit_message_text(
            "📺 *Add New Channel*\n\n"
            "Please send the channel ID or username:\n\n"
            "*Format:*\n"
            "• @channelname (for public channels)\n"
            "• -1001234567890 (for private channels/groups)\n\n"
            "*Important:* Make sure:\n"
            "1. You are an admin of the channel\n"
            "2. Your bot is added as an admin with posting permissions",
            parse_mode='Markdown'
        )
        
    elif action == "list":
        channels = Database.get_user_channels(user.id)
        if not channels:
            await query.edit_message_text("❌ No channels configured.")
            return
            
        message = "📺 *Your Channels:*\n\n"
        for i, channel in enumerate(channels, 1):
            message += f"{i}. 📺 Active\n"
            message += f"   *Name:* {channel['channel_name']}\n"
            message += f"   *ID:* {channel['channel_id']}\n\n"
            
        await query.edit_message_text(message, parse_mode='Markdown')
        
    elif action == "remove":
        channels = Database.get_user_channels(user.id)
        if not channels:
            await query.edit_message_text("❌ No channels to remove.")
            return
            
        keyboard = []
        for channel in channels:
            keyboard.append([InlineKeyboardButton(
                f"🗑️ {channel['channel_name']}", 
                callback_data=f"remove_channel_{channel['channel_id']}"
            )])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="channels_cancel")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "🗑️ *Remove Channel*\n\nSelect a channel to remove:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        

    elif action == "cancel":
        Database.update_user_session(user.id, BotStates.IDLE)
        await query.edit_message_text("❌ Channel management cancelled.")

async def handle_channel_selection(query, user, data, context=None):
    """Handle channel selection for posting"""
    if data.startswith("remove_channel_"):
        channel_id = data.replace("remove_channel_", "")
        success = Database.remove_user_channel(user.id, channel_id)
        
        if success:
            await query.edit_message_text(f"✅ Channel {channel_id} removed successfully!")
        else:
            await query.edit_message_text(f"❌ Failed to remove channel {channel_id}.")
            

            
    # Remove obsolete schedule_to_ handling - posts already have channels assigned

async def prompt_channel_selection(update, user_id: int, pending_posts: list):
    """Show channel selection for scheduling"""
    channels = Database.get_user_channels(user_id)
    
    if not channels:
        await update.reply_text(
            "❌ *No channels configured!*\n\n"
            "Please add a channel first using /channels command.",
            parse_mode='Markdown'
        )
        return None
        
    if len(channels) == 1:
        # Auto-select the only channel
        return channels[0]['channel_id']
        
    # Show channel selection
    keyboard = []
    for channel in channels:
        keyboard.append([InlineKeyboardButton(
            f"📺 {channel['channel_name']}", 
            callback_data=f"schedule_to_{channel['channel_id']}"
        )])
    
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="schedule_cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = f"""
📺 *Select Channel for Posting*

Choose which channel to post your {len(pending_posts)} photos:
"""
    
    await update.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')
    return "selecting"  # Indicates user needs to select

async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command - show user statistics (same as View Statistics button)"""
    user = update.effective_user
    stats = Database.get_user_stats(user.id)
    
    # Get user channels for channel-specific buttons
    channels = Database.get_user_channels(user.id)
    
    keyboard = []
    
    # Add channel-specific buttons if channels exist
    if channels:
        keyboard.append([InlineKeyboardButton("📺 View Channel Details", callback_data="stats_channels")])
    
    keyboard.extend([
        [InlineKeyboardButton("🔄 Refresh Stats", callback_data="main_stats")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Format post statistics
    post_stats = stats['posts']
    queued = post_stats.get('queued', 0)
    scheduled = post_stats.get('scheduled', 0)
    posted = post_stats.get('posted', 0)
    failed = post_stats.get('failed', 0)
    
    # Build compact channel breakdown
    channel_breakdown = {}
    for row in stats['channel_details']:
        channel_name, channel_id, status, mode, count = row
        channel_key = f"{channel_name}"
        
        if channel_key not in channel_breakdown:
            channel_breakdown[channel_key] = {'total': 0, 'scheduled': 0}
        
        channel_breakdown[channel_key]['total'] += count
        if status == 'scheduled':
            channel_breakdown[channel_key]['scheduled'] += count
    
    # Build channel summary (compact)
    channel_summary = ""
    if channel_breakdown:
        channel_summary = "\n*📺 Channels:*\n"
        for channel, data in channel_breakdown.items():
            channel_summary += f"• {channel}: {data['total']} posts ({data['scheduled']} scheduled)\n"
    
    # Build next posts preview (compact - only first 2)
    next_posts_text = ""
    if stats['next_posts']:
        next_posts_text = "\n*⏰ Next Posts:*\n"
        for post in stats['next_posts'][:2]:
            scheduled_time, channel_name, channel_id, media_type = post
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(scheduled_time)
                time_str = dt.strftime("%m/%d %H:%M")
                media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
                channel_display = channel_name if channel_name != 'Unknown Channel' else channel_id
                next_posts_text += f"• {time_str} - {media_icon} {channel_display}\n"
            except (ValueError, TypeError, AttributeError) as e:
                next_posts_text += f"• {scheduled_time} - {media_type} {channel_name or channel_id}\n"
    
    # Build message without any markdown formatting to avoid parsing errors
    message = f"📊 Statistics Summary\n\n"
    message += f"📈 Posts Overview:\n"
    message += f"• Queued: {queued} | Scheduled: {scheduled}\n"
    message += f"• Posted: {posted} | Failed: {failed}\n"
    
    # Add channel summary if available (without markdown)
    if channel_summary:
        clean_summary = channel_summary.replace('*', '').replace('_', '').strip()
        if clean_summary:
            message += f"\n{clean_summary}\n"
    
    # Add next posts if available (without markdown)
    if next_posts_text:
        clean_next_posts = next_posts_text.replace('*', '').replace('_', '').strip()
        if clean_next_posts:
            message += f"\n{clean_next_posts}\n"
    
    # Add current mode (without markdown)
    message += f"\n💡 Current Mode: {stats['current_mode']}"

    await update.message.reply_text(message, reply_markup=reply_markup)

async def clearqueue_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /clearqueue command - clear all queued posts"""
    user = update.effective_user
    
    # Get count of pending posts
    pending_posts = Database.get_pending_posts(user.id)
    
    if not pending_posts:
        await update.message.reply_text(
            "📭 *No queued posts found!*\n\n"
            "You don't have any photos waiting to be scheduled.",
            parse_mode='Markdown'
        )
        return
    
    # Show confirmation with inline keyboard
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Clear All", callback_data="clearqueue_confirm")],
        [InlineKeyboardButton("❌ No, Keep Them", callback_data="clearqueue_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🗑️ *Clear Queue Confirmation*\n\n"
        f"You have *{len(pending_posts)} photos* waiting to be scheduled.\n\n"
        f"⚠️ *Are you sure you want to clear all queued posts?*\n"
        f"This action cannot be undone!",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def clearscheduled_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /clearscheduled command - clear all scheduled posts"""
    user = update.effective_user
    
    # Get scheduled posts by channel
    scheduled_posts_by_channel = Database.get_scheduled_posts_by_channel(user.id)
    total_scheduled = sum(len(posts) for posts in scheduled_posts_by_channel.values())
    
    if total_scheduled == 0:
        await update.message.reply_text(
            "📅 *No Scheduled Posts*\n\n"
            "You don't have any scheduled posts to clear.\n\n"
            "Use /schedule to set up automatic posting!",
            parse_mode='Markdown'
        )
        return
    
    # Build channel breakdown for display
    channel_breakdown = ""
    for channel_id, posts in scheduled_posts_by_channel.items():
        if posts:
            # Get channel name
            channels = Database.get_user_channels(user.id)
            channel_name = next((ch['channel_name'] for ch in channels if ch['channel_id'] == channel_id), channel_id)
            channel_breakdown += f"• {channel_name}: {len(posts)} posts\n"
    
    # Show options: clear all or select channel
    keyboard = [
        [InlineKeyboardButton("🗑 Clear All Scheduled", callback_data="clearscheduled_confirm_all")],
        [InlineKeyboardButton("📺 Select Channel", callback_data="clearscheduled_select_channel")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = f"""
⚠️ *Clear Scheduled Posts*

You have *{total_scheduled} scheduled posts* across your channels:

{channel_breakdown}
Choose an option:
"""
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /reset command - clear all user data"""
    user = update.effective_user
    
    keyboard = [
        [InlineKeyboardButton("⚠️ Yes, Clear Everything", callback_data="reset_confirm")],
        [InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = f"""
⚠️ *Reset All Data*

This will permanently delete:
• All your uploaded photos
• All scheduled posts
• Your channel configurations
• Your scheduling settings
• Your session data

*This action cannot be undone!*

Are you sure you want to continue?
"""
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_reset_callback(query, user):
    """Handle reset confirmation callbacks"""
    if query.data == "reset_confirm":
        # Cancel all scheduled posts first - this will be handled differently
        # We'll clear database posts and let scheduler handle cleanup
        
        # Clear all user data
        Database.clear_all_user_data(user.id)
        
        await query.edit_message_text(
            "✅ *All your data has been cleared!*\n\n"
            "You can start fresh by using /start command.",
            parse_mode='Markdown'
        )
        
    elif query.data == "reset_cancel":
        await query.edit_message_text("❌ Reset cancelled. Your data is safe.")

async def handle_clearqueue_callback(query, user, data):
    """Handle clearqueue confirmation callbacks"""
    action = data.replace("clearqueue_", "")
    
    if action == "confirm":
        # Clear all queued posts
        cleared_count = Database.clear_queued_posts(user.id)
        
        if cleared_count > 0:
            await query.edit_message_text(
                f"✅ *Queue Cleared Successfully!*\n\n"
                f"Removed *{cleared_count} photos* from your queue.\n\n"
                f"You can now upload new photos and schedule them.",
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                "📭 *No photos to clear.*\n\n"
                "Your queue was already empty.",
                parse_mode='Markdown'
            )
    
    elif action == "cancel":
        await query.edit_message_text("❌ Queue clearing cancelled.")

async def handle_main_menu_callback(query, user, data):
    """Handle main menu button callbacks"""
    action = data.replace("main_", "")
    
    if action == "mode1":
        # Check if user has channels configured
        channels = Database.get_user_channels(user.id)
        
        if not channels:
            await query.edit_message_text(
                "❌ *No channels configured!*\n\n"
                "Please add a channel first using /channels command before using Mode 1.",
                parse_mode='Markdown'
            )
            return
        
        # Always ask user to select a channel
        await prompt_channel_selection_for_mode_inline(query, user.id, channels, mode=1)
        
    elif action == "mode2":
        # Check if user has channels configured
        channels = Database.get_user_channels(user.id)
        
        if not channels:
            await query.edit_message_text(
                "❌ *No channels configured!*\n\n"
                "Please add a channel first using /channels command before using Mode 2.",
                parse_mode='Markdown'
            )
            return
        
        # Always ask user to select a channel
        await prompt_channel_selection_for_mode_inline(query, user.id, channels, mode=2)
        
    elif action == "channels":
        # Show channels management
        await channels_handler_inline(query, user)
        
    elif action == "stats":
        # Show statistics
        await stats_handler_inline(query, user)
        
    elif action == "help":
        # Show help
        await help_handler_inline(query, user)
        
    elif action == "recurring":
        # Start Recurring Mode
        await recurring_mode_handler(query, user)
        
    elif action == "calendar":
        # Show calendar view
        await calendar_view_handler(query, user)

async def channels_handler_inline(query, user):
    """Handle inline channels management"""
    channels = Database.get_user_channels(user.id)
    
    keyboard = []
    
    if channels:
        keyboard.append([InlineKeyboardButton("📋 View All Channels", callback_data="channels_list")])
        keyboard.append([InlineKeyboardButton("➕ Add New Channel", callback_data="channels_add")])
        keyboard.append([InlineKeyboardButton("🗑️ Remove Channel", callback_data="channels_remove")])
    else:
        keyboard.append([InlineKeyboardButton("➕ Add Your First Channel", callback_data="channels_add")])
    
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if channels:
        message = f"📺 *Channel Management*\n\n" \
                 f"*Channels configured:* {len(channels)}\n\n" \
                 f"*Your Channels:*\n"
        
        for i, channel in enumerate(channels[:5], 1):
            message += f"{i}. 📺 {channel['channel_name']} ({channel['channel_id']})\n"
        
        if len(channels) > 5:
            message += f"... and {len(channels) - 5} more\n"
    else:
        message = "📺 *Channel Management*\n\n" \
                 "No channels configured yet.\n\n" \
                 "*Channel ID formats:*\n" \
                 "• @channelname (public)\n" \
                 "• -1001234567890 (private)\n\n" \
                 "You must be admin and add the bot as admin too."
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def stats_handler_inline(query, user):
    """Handle inline statistics display"""
    stats = Database.get_user_stats(user.id)
    
    # Get user channels for channel-specific buttons
    channels = Database.get_user_channels(user.id)
    
    keyboard = []
    
    # Add channel-specific buttons if channels exist
    if channels:
        keyboard.append([InlineKeyboardButton("📺 View Channel Details", callback_data="stats_channels")])
    
    keyboard.extend([
        [InlineKeyboardButton("🔄 Refresh Stats", callback_data="main_stats")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Format post statistics
    post_stats = stats['posts']
    queued = post_stats.get('queued', 0)
    scheduled = post_stats.get('scheduled', 0)
    posted = post_stats.get('posted', 0)
    failed = post_stats.get('failed', 0)
    
    # Build compact channel breakdown
    channel_breakdown = {}
    for row in stats['channel_details']:
        channel_name, channel_id, status, mode, count = row
        channel_key = f"{channel_name}"
        
        if channel_key not in channel_breakdown:
            channel_breakdown[channel_key] = {'total': 0, 'scheduled': 0}
        
        channel_breakdown[channel_key]['total'] += count
        if status == 'scheduled':
            channel_breakdown[channel_key]['scheduled'] += count
    
    # Build channel summary (compact)
    channel_summary = ""
    if channel_breakdown:
        channel_summary = "\n*📺 Channels:*\n"
        for channel, data in channel_breakdown.items():
            channel_summary += f"• {channel}: {data['total']} posts ({data['scheduled']} scheduled)\n"
    
    # Build next posts preview (compact - only first 2)
    next_posts_text = ""
    if stats['next_posts']:
        next_posts_text = "\n*⏰ Next Posts:*\n"
        for post in stats['next_posts'][:2]:
            scheduled_time, channel_name, channel_id, media_type = post
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(scheduled_time)
                time_str = dt.strftime("%m/%d %H:%M")
                media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
                channel_display = channel_name if channel_name != 'Unknown Channel' else channel_id
                next_posts_text += f"• {time_str} - {media_icon} {channel_display}\n"
            except (ValueError, TypeError, AttributeError) as e:
                next_posts_text += f"• {scheduled_time} - {media_type} {channel_name or channel_id}\n"
    
    # Build message without any markdown formatting to avoid parsing errors
    message = f"📊 Statistics Summary\n\n"
    message += f"📈 Posts Overview:\n"
    message += f"• Queued: {queued} | Scheduled: {scheduled}\n"
    message += f"• Posted: {posted} | Failed: {failed}\n"
    message += f"• Total: {stats['total_posts']}\n"
    
    # Add channel summary if available (without markdown)
    if channel_summary:
        clean_summary = channel_summary.replace('*', '').replace('_', '').strip()
        if clean_summary:
            message += f"\n{clean_summary}\n"
    
    # Add next posts if available (without markdown)
    if next_posts_text:
        clean_next_posts = next_posts_text.replace('*', '').replace('_', '').strip()
        if clean_next_posts:
            message += f"\n{clean_next_posts}\n"
    
    # Add mode information (without markdown)
    message += f"\n🔄 Mode: {stats['current_mode'].replace('_', ' ').title()}\n"
    message += f"🔄 Recurring: {stats['recurring_count']}\n"
    message += f"📦 Batches: {stats['batches_count']}"
    
    await query.edit_message_text(message, reply_markup=reply_markup)

async def stats_channels_handler(query, user):
    """Show channel selection for detailed stats"""
    channels = Database.get_user_channels(user.id)
    
    if not channels:
        await query.edit_message_text(
            "❌ *No channels configured!*\n\n"
            "Please add a channel first using /channels command.",
            parse_mode='Markdown'
        )
        return
    
    keyboard = []
    for channel in channels:
        keyboard.append([InlineKeyboardButton(
            f"📺 {channel['channel_name']}", 
            callback_data=f"stats_channel_{channel['channel_id']}"
        )])
    
    keyboard.append([InlineKeyboardButton("🔙 Back to Stats", callback_data="main_stats")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "*📺 Channel Details*\n\n"
        "Select a channel to view all posts and their schedules:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def stats_channel_details_handler(query, user, channel_id):
    """Show detailed posts for a specific channel"""
    # Get channel info
    channels = Database.get_user_channels(user.id)
    channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
    
    if not channel:
        await query.edit_message_text("❌ Channel not found!")
        return
    
    # Get all posts for this channel
    posts = Database.get_channel_posts(user.id, channel_id)
    
    if not posts:
        keyboard = [[InlineKeyboardButton("🔙 Back to Channels", callback_data="stats_channels")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"*📺 {channel['channel_name']}*\n\n"
            "No posts found for this channel.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return
    
    # Group posts by status
    queued_posts = [p for p in posts if p['status'] == 'pending' and not p['scheduled_time']]
    scheduled_posts = [p for p in posts if p['status'] == 'pending' and p['scheduled_time']]
    posted_posts = [p for p in posts if p['status'] == 'posted']
    failed_posts = [p for p in posts if p['status'] == 'failed']
    
    # Build message
    message = f"*📺 {channel['channel_name']}*\n\n"
    message += f"*📊 Summary:*\n"
    message += f"• Queued: {len(queued_posts)}\n"
    message += f"• Scheduled: {len(scheduled_posts)}\n" 
    message += f"• Posted: {len(posted_posts)}\n"
    message += f"• Failed: {len(failed_posts)}\n\n"
    
    # Show upcoming scheduled posts (first 10)
    if scheduled_posts:
        message += "*⏰ Upcoming Posts:*\n"
        for i, post in enumerate(scheduled_posts):
            try:
                from datetime import datetime
                scheduled_dt = datetime.fromisoformat(post['scheduled_time'])
                time_str = scheduled_dt.strftime("%m/%d %H:%M")
                media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(post['media_type'], '📁')
                
                # Show full description without truncation
                desc = post['description'] or "No description"
                
                message += f"{i+1}. {time_str} {media_icon} - {desc}\n"
            except (ValueError, TypeError, AttributeError) as e:
                message += f"{i+1}. {post['scheduled_time']} - {post['media_type']}\n"
    
    # Create keyboard with navigation options
    keyboard = []
    
    # Add individual post management buttons for Mode 2 posts
    mode2_posts = [p for p in scheduled_posts if p['mode'] == 2]
    if mode2_posts:
        keyboard.append([InlineKeyboardButton("✏️ Edit Mode 2 Posts", callback_data=f"edit_mode2_{channel_id}")])
    
    keyboard.extend([
        [InlineKeyboardButton("🔙 Back to Channels", callback_data="stats_channels")],
        [InlineKeyboardButton("📊 Back to Stats", callback_data="main_stats")]
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def edit_mode2_posts_handler(query, user, channel_id):
    """Show Mode 2 posts for editing"""
    try:
        logger.info(f"edit_mode2_posts_handler called for user {user.id}, channel {channel_id}")
        
        # Get channel info
        channels = Database.get_user_channels(user.id)
        channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
        
        if not channel:
            logger.warning(f"Channel {channel_id} not found for user {user.id}")
            await query.edit_message_text("❌ Channel not found!")
            return
        
        # Get Mode 2 posts for this channel (both pending and failed posts can be edited)
        posts = Database.get_channel_posts(user.id, channel_id)
        mode2_posts = [p for p in posts if p['mode'] == 2 and p['status'] in ['pending', 'failed']]
        
        if not mode2_posts:
            keyboard = [[InlineKeyboardButton("🔙 Back to Channel", callback_data=f"stats_channel_{channel_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"*✏️ Edit Mode 2 Posts - {channel['channel_name']}*\n\n"
                "No Mode 2 scheduled posts found for editing.",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return
        
        # Build post list with edit buttons
        message = f"*✏️ Edit Mode 2 Posts - {channel['channel_name']}*\n\n"
        message += f"Select a post to edit its schedule:\n\n"
        
        keyboard = []
        for i, post in enumerate(mode2_posts[:20]):  # Limit to 20 posts
            try:
                # Handle posts with and without scheduled times (for retried posts)
                if post['scheduled_time']:
                    from datetime import datetime
                    if isinstance(post['scheduled_time'], str):
                        scheduled_dt = datetime.fromisoformat(post['scheduled_time'])
                    else:
                        scheduled_dt = post['scheduled_time']
                    time_str = scheduled_dt.strftime("%m/%d %H:%M")
                else:
                    time_str = "Not scheduled"
                
                # Add status indicator for failed posts
                status_icon = "⚠️" if post['status'] == 'failed' else ""
                
                # Truncate description if too long
                desc = post['description'][:25] + "..." if post['description'] and len(post['description']) > 25 else post['description'] or "No description"
                
                message += f"{i+1}. {status_icon}*{time_str}* - {desc}\n"
                
                keyboard.append([InlineKeyboardButton(
                    f"✏️ Edit #{i+1}",
                    callback_data=f"edit_post_{post['id']}"
                )])
            except Exception as e:
                logger.error(f"Error formatting post {post.get('id', 'unknown')}: {e}")
                message += f"{i+1}. Error formatting post\n"
        
        if len(mode2_posts) > 20:
            message += f"\n... and {len(mode2_posts) - 20} more posts (use /stats for full list)"
        
        keyboard.append([InlineKeyboardButton("🔙 Back to Channel", callback_data=f"stats_channel_{channel_id}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        logger.info(f"Successfully displayed edit interface for {len(mode2_posts)} posts")
        
    except Exception as e:
        logger.error(f"Error in edit_mode2_posts_handler for user {user.id}: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error loading edit interface: {e}")

async def edit_post_handler(query, user, post_id):
    """Handle editing of individual posts"""
    try:
        logger.info(f"edit_post_handler called for user {user.id}, post {post_id}")
        
        # Get post details with more flexible query to handle retried posts
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT file_path, media_type, description, scheduled_time, channel_id, mode, status
            FROM posts 
            WHERE id = ? AND user_id = ?
        ''', (post_id, user.id))
        
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            logger.warning(f"Post {post_id} not found for user {user.id}")
            await query.edit_message_text("❌ Post not found!")
            return
        
        file_path, media_type, description, scheduled_time, channel_id, mode, status = row
        
        # Check if post can be edited
        if status not in ['pending', 'failed']:
            await query.edit_message_text("❌ Only pending or failed posts can be edited!")
            return
        
        # Get channel name
        channels = Database.get_user_channels(user.id)
        channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
        channel_name = channel['channel_name'] if channel else channel_id
        
        # Handle scheduled_time that might be NULL (from retried posts)
        if scheduled_time:
            try:
                from datetime import datetime
                if isinstance(scheduled_time, str):
                    scheduled_dt = datetime.fromisoformat(scheduled_time)
                else:
                    scheduled_dt = scheduled_time
                time_str = scheduled_dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError, AttributeError) as e:
                logger.warning(f"Could not parse scheduled_time for post {post_id}: {e}")
                time_str = "Not scheduled"
        else:
            time_str = "Not scheduled"
        
        media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
        
        message = f"*✏️ Edit Post #{post_id}*\n\n"
        message += f"*📺 Channel:* {channel_name}\n"
        message += f"*📁 Type:* {media_icon} {media_type.title()}\n"
        message += f"*📝 Description:* {description or 'No description'}\n"
        message += f"*⏰ Current Schedule:* {time_str}\n\n"
        message += "*What would you like to edit?*"
        
        keyboard = [
            [InlineKeyboardButton("⏰ Edit Schedule", callback_data=f"edit_schedule_{post_id}")],
            [InlineKeyboardButton("📝 Edit Description", callback_data=f"edit_description_{post_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"edit_mode2_{channel_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        logger.info(f"Successfully displayed edit options for post {post_id}")
        
    except Exception as e:
        logger.error(f"Error in edit_post_handler for user {user.id}, post {post_id}: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error loading post editor: {e}")

async def edit_post_schedule_handler(query, user, post_id):
    """Handle editing of post schedule"""
    try:
        logger.info(f"edit_post_schedule_handler called for user {user.id}, post {post_id}")
        
        # Get post details with more flexible query
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT file_path, media_type, description, scheduled_time, channel_id, mode, status
            FROM posts 
            WHERE id = ? AND user_id = ?
        ''', (post_id, user.id))
        
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            logger.warning(f"Post {post_id} not found for user {user.id}")
            await query.edit_message_text("❌ Post not found!")
            return
        
        file_path, media_type, description, scheduled_time, channel_id, mode, status = row
        
        # Check if post can be edited
        if status not in ['pending', 'failed']:
            await query.edit_message_text("❌ Only pending or failed posts can be edited!")
            return
        
        # Get channel name
        channels = Database.get_user_channels(user.id)
        channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
        channel_name = channel['channel_name'] if channel else channel_id
        
        # Handle scheduled_time that might be NULL (from retried posts)
        if scheduled_time:
            try:
                from datetime import datetime
                if isinstance(scheduled_time, str):
                    scheduled_dt = datetime.fromisoformat(scheduled_time)
                else:
                    scheduled_dt = scheduled_time
                time_str = scheduled_dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError, AttributeError) as e:
                logger.warning(f"Could not parse scheduled_time for post {post_id}: {e}")
                time_str = "Not scheduled"
        else:
            time_str = "Not scheduled"
        
        media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
        
        # Store post ID in session for editing
        from config import BotStates
        Database.update_user_session(user.id, BotStates.WAITING_DATE_INPUT, {'editing_post_id': post_id})
        
        message = f"*⏰ Edit Schedule - Post #{post_id}*\n\n"
        message += f"*📺 Channel:* {channel_name}\n"
        message += f"*📁 Type:* {media_icon} {media_type.title()}\n"
        message += f"*📝 Description:* {description or 'No description'}\n"
        message += f"*⏰ Current Schedule:* {time_str}\n\n"
        message += "*Enter new date and time:*\n"
        message += "`YYYY-MM-DD HH:MM`\n\n"
        message += "*Example:* `2025-07-25 14:30`\n"
        message += "*(Time in Kyiv timezone)*"
        
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"edit_post_{post_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        logger.info(f"Successfully displayed schedule edit interface for post {post_id}")
        
    except Exception as e:
        logger.error(f"Error in edit_post_schedule_handler for user {user.id}, post {post_id}: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error loading schedule editor: {e}")

async def edit_post_description_handler(query, user, post_id):
    """Handle editing of post description"""
    try:
        logger.info(f"edit_post_description_handler called for user {user.id}, post {post_id}")
        
        # Get post details with more flexible query
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT file_path, media_type, description, scheduled_time, channel_id, mode, status
            FROM posts 
            WHERE id = ? AND user_id = ?
        ''', (post_id, user.id))
        
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            logger.warning(f"Post {post_id} not found for user {user.id}")
            await query.edit_message_text("❌ Post not found!")
            return
        
        file_path, media_type, description, scheduled_time, channel_id, mode, status = row
        
        # Check if post can be edited
        if status not in ['pending', 'failed']:
            await query.edit_message_text("❌ Only pending or failed posts can be edited!")
            return
        
        # Get channel name
        channels = Database.get_user_channels(user.id)
        channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
        channel_name = channel['channel_name'] if channel else channel_id
        
        # Handle scheduled_time that might be NULL (from retried posts)
        if scheduled_time:
            try:
                from datetime import datetime
                if isinstance(scheduled_time, str):
                    scheduled_dt = datetime.fromisoformat(scheduled_time)
                else:
                    scheduled_dt = scheduled_time
                time_str = scheduled_dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError, AttributeError) as e:
                logger.warning(f"Could not parse scheduled_time for post {post_id}: {e}")
                time_str = "Not scheduled"
        else:
            time_str = "Not scheduled"
        
        media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
        
        # Store post ID in session for editing
        from config import BotStates
        Database.update_user_session(user.id, BotStates.WAITING_DESCRIPTION_INPUT, {'editing_post_id': post_id})
        
        message = f"*📝 Edit Description - Post #{post_id}*\n\n"
        message += f"*📺 Channel:* {channel_name}\n"
        message += f"*📁 Type:* {media_icon} {media_type.title()}\n"
        message += f"*📝 Current Description:* {description or 'No description'}\n"
        message += f"*⏰ Schedule:* {time_str}\n\n"
        message += "*Enter new description:*\n"
        message += "Type your new description or send 'skip' to remove description."
        
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"edit_post_{post_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        logger.info(f"Successfully displayed description edit interface for post {post_id}")
        
    except Exception as e:
        logger.error(f"Error in edit_post_description_handler for user {user.id}, post {post_id}: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error loading description editor: {e}")

async def handle_description_input(update: Update, user, text: str, session_data: dict, context=None):
    """Handle description input for post editing"""
    
    # Check if we're editing a specific post
    editing_post_id = session_data.get('editing_post_id')
    
    if editing_post_id:
        # Handle individual post description editing
        await handle_individual_post_description_edit(update, user, text, editing_post_id, context)
        return

async def handle_individual_post_description_edit(update: Update, user, text: str, post_id: int, context=None):
    """Handle editing of individual post description"""
    try:
        # Process description input
        new_description = text.strip() if text.strip().lower() != 'skip' else None
        
        # Update the post description in database
        success = Database.update_post_description(post_id, new_description)
        
        if not success:
            await update.message.reply_text("❌ Failed to update post description. Please try again.")
            return
        
        # Get post details for confirmation
        conn = Database.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT description, channel_id FROM posts 
            WHERE id = ? AND user_id = ?
        ''', (post_id, user.id))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            description, channel_id = row
            
            # Get channel name
            channels = Database.get_user_channels(user.id)
            channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
            channel_name = channel['channel_name'] if channel else channel_id
            
            desc_text = description or 'No description'
            
            await update.message.reply_text(
                f"✅ *Post Description Updated Successfully!*\n\n"
                f"*Post #{post_id}:* {channel_name}\n"
                f"*New Description:* {desc_text}\n\n"
                "The post description has been updated.",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                f"✅ Post #{post_id} description updated successfully!"
            )
        
        # Reset user session
        Database.update_user_session(user.id, BotStates.IDLE)
        
    except Exception as e:
        logger.error(f"Error updating post description: {e}")
        await update.message.reply_text(
            "❌ Error updating post description. Please try again."
        )

async def help_handler_inline(query, user):
    """Handle inline help display"""
    keyboard = [
        [InlineKeyboardButton("📸 Mode 1 Help", callback_data="help_mode1")],
        [InlineKeyboardButton("📝 Mode 2 Help", callback_data="help_mode2")],
        [InlineKeyboardButton("📺 Channels Help", callback_data="help_channels")],
        [InlineKeyboardButton("🔄 Recurring Help", callback_data="help_recurring")],
        [InlineKeyboardButton("📅 View Scheduled Posts", callback_data="help_scheduled_posts")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = """
❓ *Help & Commands*

*🎯 Quick Start:*
1. Add a channel using 📺 Manage Channels
2. Choose Mode 1 (bulk) or Mode 2 (individual)
3. Upload your photos
4. Schedule them automatically

*📱 Commands:*
• `/start` - Main menu
• `/mode1` - Bulk photo upload
• `/mode2` - Individual photo upload
• `/schedule` - Schedule uploaded photos
• `/channels` - Manage channels
• `/stats` - View statistics
• `/reset` - Clear all data
• `/cancel` - Cancel operation
• `/help` - Show help

Choose a topic for detailed help:
"""
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def show_main_menu(query, user):
    """Show the main menu"""
    keyboard = [
        [InlineKeyboardButton("📸 Mode 1: Bulk Upload", callback_data="main_mode1")],
        [InlineKeyboardButton("📝 Mode 2: Individual Upload", callback_data="main_mode2")],
        [InlineKeyboardButton("🔄 Recurring Posts", callback_data="main_recurring")],
        [InlineKeyboardButton("📅 Calendar View", callback_data="main_calendar")],
        [InlineKeyboardButton("📺 Manage Channels", callback_data="main_channels")],
        [InlineKeyboardButton("📊 View Statistics", callback_data="main_stats")],
        [InlineKeyboardButton("❓ Help & Commands", callback_data="main_help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = f"""
👋 *Welcome back {user.first_name}!*

🤖 *Channel Post Scheduler Bot*

*🎯 Features:*
• *Mode 1:* Bulk photo upload with auto-scheduling
• *Mode 2:* Individual photos with custom descriptions  
• *Multi-channel:* Post to different channels
• *Recurring:* Set up automatic recurring posts
• *Smart scheduling:* Kyiv timezone, custom intervals

*🕐 Default Schedule:*
10 AM to 8 PM, every 2 hours (Kyiv time)

Choose an option below:
"""
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def help_scheduled_posts_handler(query, user):
    """Display channel selection for viewing scheduled posts"""
    channels = Database.get_user_channels(user.id)
    
    if not channels:
        keyboard = [
            [InlineKeyboardButton("➕ Add Your First Channel", callback_data="channels_add")],
            [InlineKeyboardButton("🔙 Back to Help", callback_data="main_help")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message = """
📅 *Scheduled Posts*

❌ No channels configured yet.

Add a channel first to view your scheduled posts.
"""
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        return
    
    keyboard = []
    for channel in channels:
        keyboard.append([InlineKeyboardButton(
            f"📺 {channel['channel_name']}", 
            callback_data=f"help_channel_posts_{channel['channel_id']}"
        )])
    
    keyboard.append([InlineKeyboardButton("🔙 Back to Help", callback_data="main_help")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = f"""
📅 *Scheduled Posts*

Select a channel to view scheduled posts:

*Available Channels:* {len(channels)}
"""
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def help_channel_posts_handler(query, user, channel_id):
    """Display scheduled posts for a specific channel"""
    # Get channel info
    channels = Database.get_user_channels(user.id)
    channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
    
    if not channel:
        await query.edit_message_text("❌ Channel not found.")
        return
    
    # Get scheduled posts for this channel
    scheduled_posts = Database.get_pending_posts(user_id=user.id, channel_id=channel_id)
    scheduled_posts = [post for post in scheduled_posts if post['scheduled_time']]
    
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Channels", callback_data="help_scheduled_posts")],
        [InlineKeyboardButton("🏠 Back to Help", callback_data="main_help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if not scheduled_posts:
        message = f"""
📅 *Scheduled Posts for {channel['channel_name']}*

❌ No scheduled posts for this channel.

Use Mode 1 or Mode 2 to upload and schedule content.
"""
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        return
    
    message = f"""
📅 *Scheduled Posts for {channel['channel_name']}*

*Total scheduled:* {len(scheduled_posts)} posts

"""
    
    # Sort posts by scheduled time
    scheduled_posts.sort(key=lambda x: x['scheduled_time'])
    
    for i, post in enumerate(scheduled_posts, 1):  # Show ALL posts
        # Format date and time
        scheduled_dt = post['scheduled_time']
        date_str = scheduled_dt.strftime("%m/%d")
        time_str = scheduled_dt.strftime("%H:%M")
        
        # Get media type icon
        media_icons = {
            'photo': '📸',
            'video': '🎥', 
            'audio': '🎵',
            'animation': '🎬',
            'document': '📄'
        }
        media_icon = media_icons.get(post['media_type'], '📁')
        
        # Show full description without truncation
        description = post['description'] or 'No description'
        
        # Add recurring indicator
        recurring_indicator = " 🔄" if post['is_recurring'] else ""
        
        message += f"{i}. {media_icon} *{date_str} {time_str}*{recurring_indicator}\n"
        message += f"   {description}\n\n"
    
    message += "*💡 Tip:* Use /stats for detailed analytics"
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_help_callback(query, user, data):
    """Handle help topic callbacks"""
    topic = data.replace("help_", "")
    
    if topic == "scheduled_posts":
        await help_scheduled_posts_handler(query, user)
        return
    elif topic.startswith("channel_posts_"):
        channel_id = topic.replace("channel_posts_", "")
        await help_channel_posts_handler(query, user, channel_id)
        return
    
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Help", callback_data="main_help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if topic == "mode1":
        message = """
📸 **Mode 1: Bulk Photo Upload Help**

**Perfect for:** Multiple photos without descriptions

**📋 Steps:**
1. Use `/mode1` or click Mode 1 button
2. Send photos one by one (just send, no text)
3. When done, use `/schedule` command
4. Choose your scheduling options
5. Select target channel

**⚡ Features:**
• Fast bulk uploading
• Automatic time distribution
• One-click scheduling
• Perfect for photo dumps

**💡 Tips:**
• Send photos in the order you want them posted
• Use /cancel to start over
• Maximum file size: 10MB per photo
"""
        
    elif topic == "mode2":
        message = """
📝 **Mode 2: Individual Photo Upload Help**

**Perfect for:** Photos with custom descriptions

**📋 Steps:**
1. Use `/mode2` or click Mode 2 button
2. Send a photo
3. Type description for that photo
4. Repeat for more photos
5. Use `/finish` when done uploading
6. Use `/schedule` to set posting times

**⚡ Features:**
• Custom descriptions per photo
• Review each post individually
• Edit descriptions before scheduling
• Perfect for curated content

**💡 Tips:**
• Descriptions can be any length
• Use /finish before scheduling
• Can mix photos with/without descriptions
"""
        
    elif topic == "channels":
        message = """
📺 **Channels Management Help**

**🔧 Setup:**
1. Go to your Telegram channel
2. Add your bot as administrator
3. Give it "Post Messages" permission
4. Use `/channels` to add the channel

**📝 Channel ID Formats:**
• **Public:** @channelname
• **Private:** -1001234567890 (get from web.telegram.org)

**⚙️ Features:**
• Multiple channels per user
• Set default channel
• Switch between channels
• Remove unused channels

**💡 Tips:**
• First channel is auto-set as default
• Bot needs admin rights to post
• Can post to groups too (same setup)
"""
        
    elif topic == "recurring":
        message = """
🔄 **Recurring Posts Help**

**🎯 Two Recurring Modes:**
• **Bulk recurring:** Upload multiple posts, schedule as recurring set
• **Individual recurring:** `/recurring` command for single post repeating

**📋 How to set up bulk recurring:**
1. Upload photos (Mode 1 or 2)
2. Use `/schedule` command
3. Choose "Recurring Schedule" option
4. Set interval (hours between posts)
5. Set end condition (count or date)

**📋 How to set up individual recurring:**
1. Use `/recurring` command
2. Upload one media file with description
3. Choose frequency (daily, weekly, custom)
4. Set end condition

**⚙️ Options:**
• **Interval:** 1-168 hours (1 week max)
• **End by count:** Stop after X posts
• **End by date:** Stop on specific date
• **No end:** Continue until manually stopped

**💡 Use cases:**
• Daily motivational quotes
• Weekly product showcases
• Regular announcements
• Automated content feeds
"""
        
    elif topic == "management":
        message = """
🔧 **Management Commands Help**

**📊 Statistics & Monitoring:**
• `/stats` - Detailed statistics with channel breakdowns
• View queued, scheduled, posted, and failed posts
• Access individual post editing and management

**🔄 Post Recovery:**
• `/retry` - Retry failed posts (individual/bulk/by channel)
• Smart retry logic resets failed posts to pending

**🗑️ Clearing Commands:**
• `/clearqueue` - Clear pending (unscheduled) posts
• `/clearscheduled` - Clear scheduled posts (all or by channel)
• `/reset` - Clear ALL user data (complete reset)

**⚙️ Advanced Features:**
• Custom date scheduling (YYYY-MM-DD HH:MM format)
• Multi-channel post management
• Timezone-aware scheduling (Kyiv time)
• Failed post automatic detection and recovery

**💡 Pro Tips:**
• Use `/stats` regularly to monitor post performance
• `/retry` is perfect for network failures or API issues
• Clear commands have confirmation dialogs for safety
"""
        
    elif topic == "batches":
        message = """
📊 **Batch System Help**

**🎯 What are batches?**
Advanced multi-channel posting system for complex campaigns

**📋 How to use `/multibatch`:**
1. Create new batch with name
2. Add posts using Mode 1 or Mode 2 workflows
3. Assign posts to different channels
4. Schedule entire batch with unified timing

**⚡ Batch Features:**
• Multiple channels in one batch
• Mixed Mode 1 and Mode 2 content
• Independent scheduling per batch
• Batch-wide management and editing

**🔧 Batch Management:**
• View all batches with post counts
• Edit batch contents before scheduling
• Delete unused batches
• Schedule batches independently

**💡 Perfect for:**
• Multi-channel marketing campaigns
• Coordinated product launches
• Event announcements across channels
• Complex content distribution strategies

**⚙️ Advanced:**
• Batches are completely isolated from regular modes
• Can run multiple batches simultaneously
• Each batch maintains its own scheduling queue
"""
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def prompt_recurring_schedule(query, user):
    """Prompt user for recurring schedule setup"""
    pending_posts = Database.get_pending_posts(user.id)
    
    keyboard = [
        [
            InlineKeyboardButton("📆 Daily (24h)", callback_data="recurring_daily"),
            InlineKeyboardButton("📅 Every 2 Days", callback_data="recurring_2days")
        ],
        [
            InlineKeyboardButton("📆 Weekly (168h)", callback_data="recurring_weekly"),
            InlineKeyboardButton("🕰️ Custom Hours", callback_data="recurring_custom")
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="schedule_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = f"""
🔄 **Setup Recurring Schedule**

**Posts to schedule:** {len(pending_posts)}

**Quick Options:**
• **Daily:** Post once every 24 hours
• **Every 2 Days:** Post every 48 hours  
• **Weekly:** Post once every 7 days
• **Custom:** Set your own interval (1-168 hours)

**Next:** Choose end condition (count or date)

Select posting frequency:
"""
    
    await query.edit_message_text(message, reply_markup=reply_markup)

async def handle_recurring_callback(query, user, data):
    """Handle recurring schedule callbacks"""
    action = data.replace("recurring_", "")
    
    # Handle individual recurring post schedule options (new format)
    if action.startswith("recur_"):
        recur_action = action.replace("recur_", "")
        
        if recur_action == "daily":
            interval_hours = 24
        elif recur_action == "2days":
            interval_hours = 48
        elif recur_action == "weekly":
            interval_hours = 168
        elif recur_action == "custom":
            # Handle custom interval for individual recurring post
            Database.update_user_session(user.id, "waiting_recurring_hours", {"action": "recur_custom"})
            await query.edit_message_text(
                "🕰️ *Custom Interval for Individual Post*\n\n"
                "Enter the number of hours between posts (1-168):\n\n"
                "*Examples:*\n"
                "• `6` - Every 6 hours\n"
                "• `12` - Twice daily\n"
                "• `72` - Every 3 days\n\n"
                "*Send the number of hours:*",
                parse_mode='Markdown'
            )
            return
        else:
            return
        
        # Show end condition options for individual post
        keyboard = [
            [
                InlineKeyboardButton("🔢 End after X posts", callback_data=f"recur_count_{interval_hours}"),
                InlineKeyboardButton("📅 End on date", callback_data=f"recur_date_{interval_hours}")
            ],
            [
                InlineKeyboardButton("∞ Never end", callback_data=f"recur_never_{interval_hours}")
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="recurring_schedule")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        interval_text = f"{interval_hours} hours"
        if interval_hours == 24:
            interval_text = "24 hours (daily)"
        elif interval_hours == 48:
            interval_text = "48 hours (every 2 days)"
        elif interval_hours == 168:
            interval_text = "168 hours (weekly)"
        
        message = f"""
🔄 *Individual Post Recurring Schedule*

*Interval:* {interval_text}
*Post:* Your uploaded content will repeat automatically

*How should it end?*

• *Count:* Stop after a specific number of posts
• *Date:* Stop on a specific date  
• *Never:* Continue until manually stopped

Choose end condition:
"""
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        return
    
    # Handle individual recurring post end conditions (new format)
    elif action.startswith("recur_count_"):
        interval = int(action.split("_")[2])
        Database.update_user_session(user.id, "waiting_recurring_count", {"interval_hours": interval, "mode": "individual"})
        await query.edit_message_text(
            f"🔢 *Set Post Count Limit*\n\n"
            f"*Interval:* Every {interval} hours\n\n"
            f"How many times should this post be repeated?\n\n"
            f"*Examples:*\n"
            f"• `5` - Post will be shared 5 times\n"
            f"• `10` - Post will be shared 10 times\n"
            f"• `30` - Post will be shared 30 times\n\n"
            f"*Send the number of repetitions:*",
            parse_mode='Markdown'
        )
        return
        
    elif action.startswith("recur_date_"):
        interval = int(action.split("_")[2])
        Database.update_user_session(user.id, "waiting_recurring_date", {"interval_hours": interval, "mode": "individual"})
        await query.edit_message_text(
            f"📅 *Set End Date*\n\n"
            f"*Interval:* Every {interval} hours\n\n"
            f"When should the recurring posts stop?\n\n"
            f"*Format:* YYYY-MM-DD HH:MM\n"
            f"*Examples:*\n"
            f"• `2025-08-01 12:00` - Stop on August 1st at noon\n"
            f"• `2025-12-31 23:59` - Stop on New Year's Eve\n\n"
            f"*Send the end date and time (Kyiv timezone):*",
            parse_mode='Markdown'
        )
        return
        
    elif action.startswith("recur_never_"):
        interval = int(action.split("_")[2])
        await setup_individual_recurring_post(query, user, interval, None, None)
        return
    
    # Handle channel selection for individual recurring posts (new format)
    elif action.startswith("recur_channel_"):
        parts = action.split("_")
        channel_id = parts[2]
        interval_hours = int(parts[3])
        
        # Parse the end condition from callback data
        if len(parts) > 4:
            if parts[4] == "count":
                recurring_count = int(parts[5])
                recurring_end_date = None
            elif parts[4] == "date":
                recurring_count = None
                from datetime import datetime
                recurring_end_date = datetime.fromisoformat(parts[5])
            elif parts[4] == "never":
                recurring_count = None
                recurring_end_date = None
        else:
            recurring_count = None
            recurring_end_date = None
        
        # Execute individual recurring setup with selected channel
        await setup_individual_recurring_post_with_channel(query, user, interval_hours, channel_id, recurring_count, recurring_end_date)
        return
    
    if action.startswith("channel_"):
        # Handle channel selection for recurring posts
        parts = action.split("_")
        channel_id = parts[1]
        interval_hours = int(parts[2])
        
        # Parse the end condition from callback data
        if len(parts) > 3:
            if parts[3] == "count":
                recurring_count = int(parts[4])
                recurring_end_date = None
            elif parts[3] == "date":
                recurring_count = None
                from datetime import datetime
                recurring_end_date = datetime.fromisoformat(parts[4])
            elif parts[3] == "never":
                recurring_count = None
                recurring_end_date = None
        else:
            recurring_count = None
            recurring_end_date = None
        
        # Execute the recurring setup with selected channel
        await setup_recurring_posts_with_channel(query, user, interval_hours, channel_id, recurring_count, recurring_end_date)
        return
    
    elif action.startswith("count_"):
        interval = int(action.split("_")[1])
        # Get pending posts to find the channel they were uploaded for
        pending_posts = Database.get_pending_posts(user.id, unscheduled_only=True)
        if pending_posts:
            channel_id = pending_posts[0]['channel_id']
            Database.update_user_session(user.id, "waiting_recurring_count", {"interval_hours": interval, "channel_id": channel_id})
        else:
            Database.update_user_session(user.id, "waiting_recurring_count", {"interval_hours": interval})
        
        await query.edit_message_text(
            f"🔢 **Set Post Count Limit**\n\n"
            f"**Interval:** Every {interval} hours\n\n"
            f"How many times should each post be repeated?\n\n"
            f"**Examples:**\n"
            f"• `5` - Each post will be shared 5 times\n"
            f"• `10` - Each post will be shared 10 times\n"
            f"• `30` - Each post will be shared 30 times\n\n"
            f"**Send the number of repetitions:**"
        )
        return
        
    elif action.startswith("date_"):
        interval = int(action.split("_")[1])
        # Get pending posts to find the channel they were uploaded for
        pending_posts = Database.get_pending_posts(user.id, unscheduled_only=True)
        if pending_posts:
            channel_id = pending_posts[0]['channel_id']
            Database.update_user_session(user.id, "waiting_recurring_date", {"interval_hours": interval, "channel_id": channel_id})
        else:
            Database.update_user_session(user.id, "waiting_recurring_date", {"interval_hours": interval})
            
        await query.edit_message_text(
            f"📅 **Set End Date**\n\n"
            f"**Interval:** Every {interval} hours\n\n"
            f"When should the recurring posts stop?\n\n"
            f"**Format:** YYYY-MM-DD HH:MM\n"
            f"**Examples:**\n"
            f"• `2025-08-01 12:00` - Stop on August 1st at noon\n"
            f"• `2025-12-31 23:59` - Stop on New Year's Eve\n\n"
            f"**Send the end date and time (Kyiv timezone):**"
        )
        return
        
    elif action.startswith("never_"):
        interval = int(action.split("_")[1])
        # Get pending posts to find the channel they were uploaded for
        pending_posts = Database.get_pending_posts(user.id, unscheduled_only=True)
        
        if not pending_posts:
            await query.edit_message_text("❌ No posts to schedule.")
            return
            
        # Use the channel from the first pending post (they should all be from the same channel in recurring workflow)
        channel_id = pending_posts[0]['channel_id']
        await setup_recurring_posts_with_channel(query, user, interval, channel_id, None, None)
        return
    
    # Handle initial options - extract interval from action type
    if action == "daily":
        interval_hours = 24
        interval_text = "24 hours (daily)"
    elif action == "2days":
        interval_hours = 48
        interval_text = "48 hours (every 2 days)"
    elif action == "weekly":
        interval_hours = 168
        interval_text = "168 hours (weekly)"
    elif action == "custom":
        session_data = {"action": "recurring_setup"}
        Database.update_user_session(user.id, "waiting_recurring_hours", session_data)
        await query.edit_message_text(
            "🕰️ **Custom Interval**\n\n"
            "Enter the number of hours between posts (1-168):\n\n"
            "**Examples:**\n"
            "• `6` - Every 6 hours\n"
            "• `12` - Twice daily\n"
            "• `72` - Every 3 days\n\n"
            "**Send the number of hours:**"
        )
        return
    else:
        # Fallback for unknown actions
        interval_hours = 24
        interval_text = "24 hours (daily)"
    
    # Show end condition options
    keyboard = [
        [
            InlineKeyboardButton("🔢 End after X posts", callback_data=f"recurring_count_{interval_hours}"),
            InlineKeyboardButton("📅 End on specific date", callback_data=f"recurring_date_{interval_hours}")
        ],
        [
            InlineKeyboardButton("∞ Never end (manual stop)", callback_data=f"recurring_never_{interval_hours}")
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="schedule_recurring")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = f"""
🔄 **Recurring Schedule Setup**

**Interval:** {interval_text}
**Posts:** Will repeat infinitely until stopped

**How should it end?**

• **Count:** Stop after a specific number of posts
• **Date:** Stop on a specific date  
• **Never:** Continue until manually stopped

Choose end condition:
"""
    
    await query.edit_message_text(message, reply_markup=reply_markup)

def setup_recurring_posts_direct(user, interval_hours, recurring_count=None, recurring_end_date=None):
    """Set up recurring posts with specified parameters (direct version)"""
    # Only get unscheduled posts to avoid using all previous uploads  
    pending_posts = Database.get_pending_posts(user.id, unscheduled_only=True)
    
    if not pending_posts:
        return False
    
    # Get target channel  
    channels = Database.get_user_channels(user.id)
    
    if len(channels) == 1:
        target_channel_id = channels[0]['channel_id']
    else:
        return False  # Need channel selection or no channels
    
    # Create new recurring posts 
    first_post_time = get_current_kyiv_time() + timedelta(minutes=1)
    
    for i, post in enumerate(pending_posts):
        post_start_time = first_post_time + timedelta(minutes=i)
        
        Database.add_post(
            user_id=user.id,
            file_path=post['file_path'],
            description=post['description'],
            scheduled_time=post_start_time,
            mode=post['mode'],
            channel_id=target_channel_id,
            is_recurring=True,
            recurring_interval_hours=interval_hours,
            recurring_end_date=recurring_end_date,
            recurring_count=recurring_count
        )
    
    # Clear old posts
    Database.clear_user_posts(user.id, mode=None)
    
    # Clear any remaining pending posts from queue after successful recurring scheduling
    Database.clear_queued_posts(user.id)
    Database.update_user_session(user.id, BotStates.IDLE)
    
    return True

async def setup_recurring_posts_with_channel(query, user, interval_hours, channel_id, recurring_count=None, recurring_end_date=None):
    """Set up recurring posts with a specific channel"""
    # Only get unscheduled posts for the specific channel to avoid using all previous uploads
    pending_posts = Database.get_pending_posts(user.id, channel_id=channel_id, unscheduled_only=True)
    
    if not pending_posts:
        await query.edit_message_text("❌ No posts to schedule.")
        return
    
    from bot.utils import get_current_kyiv_time
    from datetime import timedelta
    
    # Get channel name for display
    channels = Database.get_user_channels(user.id)
    selected_channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
    channel_name = selected_channel['channel_name'] if selected_channel else channel_id
    
    # Create new recurring posts 
    first_post_time = get_current_kyiv_time() + timedelta(minutes=1)
    
    for i, post in enumerate(pending_posts):
        post_start_time = first_post_time + timedelta(minutes=i)
        
        Database.add_post(
            user_id=user.id,
            file_path=post['file_path'],
            media_type=post.get('media_type', 'photo'),
            description=post['description'],
            scheduled_time=post_start_time,
            mode=post['mode'],
            channel_id=channel_id,
            is_recurring=True,
            recurring_interval_hours=interval_hours,
            recurring_end_date=recurring_end_date,
            recurring_count=recurring_count
        )
    
    # Clear only the specific channel's posts that were used for recurring setup
    Database.clear_queued_posts(user.id, channel_id)
    
    interval_text = f"{interval_hours} hours"
    if interval_hours == 24:
        interval_text = "24 hours (daily)"
    elif interval_hours == 48:
        interval_text = "48 hours (every 2 days)"  
    elif interval_hours == 168:
        interval_text = "168 hours (weekly)"
    
    end_info = ""
    if recurring_count:
        end_info = f"• **Repetitions:** {recurring_count} times per post\n"
    elif recurring_end_date:
        end_info = f"• **End Date:** {recurring_end_date.strftime('%Y-%m-%d %H:%M')} (Kyiv)\n"
    else:
        end_info = f"• **Duration:** Infinite (manual stop required)\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ *Recurring Schedule Activated!*\n\n"
        f"*🔄 Posts:* {len(pending_posts)} media files\n"
        f"*📺 Channel:* {channel_name}\n" 
        f"*⏰ Interval:* Every {interval_text}\n"
        f"*🚀 First post:* {first_post_time.strftime('%Y-%m-%d %H:%M')} (Kyiv)\n"
        f"{end_info}\n"
        f"*📱 Notifications:* You'll get notified for each post\n\n"
        f"Use /stats to monitor your recurring posts.\n"
        f"Use /reset to stop all recurring posts.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    Database.update_user_session(user.id, BotStates.IDLE)

async def setup_recurring_posts(query, user, interval_hours, recurring_count=None, recurring_end_date=None):
    """Set up recurring posts with specified parameters"""
    # Only get unscheduled posts to avoid using all previous uploads
    pending_posts = Database.get_pending_posts(user.id, unscheduled_only=True)
    
    if not pending_posts:
        await query.edit_message_text("❌ No posts to schedule.")
        return
    
    # Get target channel  
    channels = Database.get_user_channels(user.id)
    
    if len(channels) > 1:
        # Show channel selection for recurring posts
        keyboard = []
        for channel in channels:
            status = "⭐ " if channel['is_default'] else ""
            callback_data = f"recurring_channel_{channel['channel_id']}_{interval_hours}"
            if recurring_count:
                callback_data += f"_count_{recurring_count}"
            elif recurring_end_date:
                callback_data += f"_date_{recurring_end_date.isoformat()}"
            else:
                callback_data += "_never"
                
            keyboard.append([InlineKeyboardButton(
                f"{status}{channel['channel_name']}", 
                callback_data=callback_data
            )])
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="schedule_cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"📺 **Select Channel for Recurring Posts**\n\n"
            f"Choose which channel to post your {len(pending_posts)} recurring posts:",
            reply_markup=reply_markup
        )
        return
    elif len(channels) == 1:
        target_channel_id = channels[0]['channel_id']
        channel_name = channels[0]['channel_name']
    else:
        await query.edit_message_text("❌ No channels configured!")
        return
    
    # Create new recurring posts 
    first_post_time = get_current_kyiv_time() + timedelta(minutes=1)
    
    for i, post in enumerate(pending_posts):
        post_start_time = first_post_time + timedelta(minutes=i)
        
        Database.add_post(
            user_id=user.id,
            file_path=post['file_path'],
            description=post['description'],
            scheduled_time=post_start_time,
            mode=post['mode'],
            channel_id=target_channel_id,
            is_recurring=True,
            recurring_interval_hours=interval_hours,
            recurring_end_date=recurring_end_date,
            recurring_count=recurring_count
        )
    
    # Clear only posts from the selected channel
    Database.clear_queued_posts(user.id, target_channel_id)
    
    interval_text = f"{interval_hours} hours"
    if interval_hours == 24:
        interval_text = "24 hours (daily)"
    elif interval_hours == 48:
        interval_text = "48 hours (every 2 days)"  
    elif interval_hours == 168:
        interval_text = "168 hours (weekly)"
    
    end_info = ""
    if recurring_count:
        end_info = f"• **Repetitions:** {recurring_count} times per post\n"
    elif recurring_end_date:
        end_info = f"• **End Date:** {recurring_end_date.strftime('%Y-%m-%d %H:%M')} (Kyiv)\n"
    else:
        end_info = f"• **Duration:** Infinite (manual stop required)\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ **Recurring Schedule Activated!**\n\n"
        f"**🔄 Posts:** {len(pending_posts)} photos\n"
        f"**📺 Channel:** {channel_name}\n" 
        f"**⏰ Interval:** Every {interval_text}\n"
        f"**🚀 First post:** {first_post_time.strftime('%Y-%m-%d %H:%M')} (Kyiv)\n"
        f"{end_info}\n"
        f"**📱 Notifications:** You'll get notified for each post\n\n"
        f"Use /stats to monitor your recurring posts.\n"
        f"Use /reset to stop all recurring posts.",
        reply_markup=reply_markup
    )
    
    Database.update_user_session(user.id, BotStates.IDLE)

async def setup_individual_recurring_post(query, user, interval_hours, recurring_count=None, recurring_end_date=None):
    """Set up recurring posts for individual mode"""
    # Get the post data from the current recurring mode session
    session = Database.get_user_session(user.id)
    if not session or session.get('mode') != 'RECURRING_MODE':
        await query.edit_message_text("❌ No post to schedule for recurring.")
        return
    
    session_data = session.get('data', {})
    file_path = session_data.get('file_path')
    media_type = session_data.get('media_type', 'photo')
    description = session_data.get('description', '')
    
    if not file_path:
        await query.edit_message_text("❌ No media file found for recurring schedule.")
        return
    
    # Get target channel
    channels = Database.get_user_channels(user.id)
    
    if len(channels) > 1:
        # Show channel selection for individual recurring post
        keyboard = []
        for channel in channels:
            callback_data = f"recur_channel_{channel['channel_id']}_{interval_hours}"
            if recurring_count:
                callback_data += f"_count_{recurring_count}"
            elif recurring_end_date:
                callback_data += f"_date_{recurring_end_date.isoformat()}"
            else:
                callback_data += "_never"
                
            keyboard.append([InlineKeyboardButton(
                f"📺 {channel['channel_name']}", 
                callback_data=callback_data
            )])
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="recurring_schedule")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"📺 *Select Channel for Recurring Post*\n\n"
            f"Choose which channel to post your recurring content:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return
    elif len(channels) == 1:
        target_channel_id = channels[0]['channel_id']
        channel_name = channels[0]['channel_name']
    else:
        await query.edit_message_text("❌ No channels configured!")
        return
    
    from bot.utils import get_current_kyiv_time
    from datetime import timedelta
    
    # Create the recurring post starting from now
    first_post_time = get_current_kyiv_time() + timedelta(minutes=1)
    
    Database.add_post(
        user_id=user.id,
        file_path=file_path,
        media_type=media_type,
        description=description,
        scheduled_time=first_post_time,
        mode=3,  # Recurring mode
        channel_id=target_channel_id,
        is_recurring=True,
        recurring_interval_hours=interval_hours,
        recurring_end_date=recurring_end_date,
        recurring_count=recurring_count
    )
    
    interval_text = f"{interval_hours} hours"
    if interval_hours == 24:
        interval_text = "24 hours (daily)"
    elif interval_hours == 48:
        interval_text = "48 hours (every 2 days)"  
    elif interval_hours == 168:
        interval_text = "168 hours (weekly)"
    
    end_info = ""
    if recurring_count:
        end_info = f"• *Repetitions:* {recurring_count} times\n"
    elif recurring_end_date:
        end_info = f"• *End Date:* {recurring_end_date.strftime('%Y-%m-%d %H:%M')} (Kyiv)\n"
    else:
        end_info = f"• *Duration:* Infinite (manual stop required)\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ *Individual Recurring Post Activated!*\n\n"
        f"*📺 Channel:* {channel_name}\n" 
        f"*⏰ Interval:* Every {interval_text}\n"
        f"*🚀 First post:* {first_post_time.strftime('%Y-%m-%d %H:%M')} (Kyiv)\n"
        f"{end_info}\n"
        f"*📱 Notifications:* You'll get notified for each post\n\n"
        f"Use /stats to monitor your recurring posts.\n"
        f"Use /reset to stop all recurring posts.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    # Clear the session since we've scheduled the post
    Database.update_user_session(user.id, BotStates.IDLE)

async def setup_individual_recurring_post_with_channel(query, user, interval_hours, channel_id, recurring_count=None, recurring_end_date=None):
    """Set up individual recurring post with specific channel"""
    # Get the post data from the current recurring mode session
    session = Database.get_user_session(user.id)
    if not session or session.get('mode') != 'RECURRING_MODE':
        await query.edit_message_text("❌ No post to schedule for recurring.")
        return
    
    session_data = session.get('data', {})
    file_path = session_data.get('file_path')
    media_type = session_data.get('media_type', 'photo')
    description = session_data.get('description', '')
    
    if not file_path:
        await query.edit_message_text("❌ No media file found for recurring schedule.")
        return
    
    from bot.utils import get_current_kyiv_time
    from datetime import timedelta
    
    # Get channel name for display
    channels = Database.get_user_channels(user.id)
    selected_channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
    channel_name = selected_channel['channel_name'] if selected_channel else channel_id
    
    # Create the recurring post starting from now
    first_post_time = get_current_kyiv_time() + timedelta(minutes=1)
    
    Database.add_post(
        user_id=user.id,
        file_path=file_path,
        media_type=media_type,
        description=description,
        scheduled_time=first_post_time,
        mode=3,  # Recurring mode
        channel_id=channel_id,
        is_recurring=True,
        recurring_interval_hours=interval_hours,
        recurring_end_date=recurring_end_date,
        recurring_count=recurring_count
    )
    
    interval_text = f"{interval_hours} hours"
    if interval_hours == 24:
        interval_text = "24 hours (daily)"
    elif interval_hours == 48:
        interval_text = "48 hours (every 2 days)"  
    elif interval_hours == 168:
        interval_text = "168 hours (weekly)"
    
    end_info = ""
    if recurring_count:
        end_info = f"• *Repetitions:* {recurring_count} times\n"
    elif recurring_end_date:
        end_info = f"• *End Date:* {recurring_end_date.strftime('%Y-%m-%d %H:%M')} (Kyiv)\n"
    else:
        end_info = f"• *Duration:* Infinite (manual stop required)\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ *Individual Recurring Post Activated!*\n\n"
        f"*📺 Channel:* {channel_name}\n" 
        f"*⏰ Interval:* Every {interval_text}\n"
        f"*🚀 First post:* {first_post_time.strftime('%Y-%m-%d %H:%M')} (Kyiv)\n"
        f"{end_info}\n"
        f"*📱 Notifications:* You'll get notified for each post\n\n"
        f"Use /stats to monitor your recurring posts.\n"
        f"Use /reset to stop all recurring posts.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    # Clear the session since we've scheduled the post
    Database.update_user_session(user.id, BotStates.IDLE)

async def handle_recurring_hours_input(update: Update, user, text: str, session_data: dict):
    """Handle custom recurring hours input"""
    try:
        hours = int(text.strip())
        if hours < 1 or hours > 168:
            await update.message.reply_text(
                "❌ Invalid hours. Please enter a number between 1 and 168 hours.\n\n"
                "**Examples:**\n"
                "• `6` - Every 6 hours\n"
                "• `24` - Daily\n"
                "• `168` - Weekly\n\n"
                "Try again:"
            )
            return
        
        # Check if this is for individual recurring post (recur_custom mode)
        if session_data.get('action') == 'recur_custom':
            # Show end condition options for individual post
            keyboard = [
                [
                    InlineKeyboardButton("🔢 End after X posts", callback_data=f"recur_count_{hours}"),
                    InlineKeyboardButton("📅 End on date", callback_data=f"recur_date_{hours}")
                ],
                [
                    InlineKeyboardButton("∞ Never end", callback_data=f"recur_never_{hours}")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="recurring_schedule")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            message = f"""
🔄 *Individual Post Recurring Schedule*

*Interval:* Every {hours} hours
*Post:* Your uploaded content will repeat automatically

*How should it end?*

• *Count:* Stop after a specific number of posts
• *Date:* Stop on a specific date  
• *Never:* Continue until manually stopped

Choose end condition:
"""
            
            await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            # Show end condition options for bulk posts
            keyboard = [
                [
                    InlineKeyboardButton("🔢 End after X posts", callback_data=f"recurring_count_{hours}"),
                    InlineKeyboardButton("📅 End on specific date", callback_data=f"recurring_date_{hours}")
                ],
                [
                    InlineKeyboardButton("∞ Never end (manual stop)", callback_data=f"recurring_never_{hours}")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="schedule_recurring")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            message = f"""
🔄 **Recurring Schedule Setup**

**Interval:** Every {hours} hours
**Posts:** Will repeat infinitely until stopped

**How should it end?**

• **Count:** Stop after a specific number of posts
• **Date:** Stop on a specific date  
• **Never:** Continue until manually stopped

Choose end condition:
"""
            
            await update.message.reply_text(message, reply_markup=reply_markup)
        
        Database.update_user_session(user.id, BotStates.IDLE)
        
    except ValueError:
        await update.message.reply_text(
            "❌ Please enter a valid number of hours (1-168).\n\n"
            "Examples: 6, 12, 24, 48, 168\n\n"
            "Try again:"
        )

async def handle_recurring_count_input(update: Update, user, text: str, session_data: dict):
    """Handle recurring count input"""
    try:
        count = int(text.strip())
        if count < 1 or count > 1000:
            await update.message.reply_text(
                "❌ Invalid count. Please enter a number between 1 and 1000.\n\n"
                "**Examples:**\n"
                "• `5` - Each post shared 5 times\n"
                "• `10` - Each post shared 10 times\n"
                "• `30` - Each post shared 30 times\n\n"
                "Try again:"
            )
            return
        
        interval_hours = session_data.get('interval_hours')
        
        # Check if this is for individual recurring post
        if session_data.get('mode') == 'individual':
            # Set up individual recurring post with count
            await setup_individual_recurring_post(None, user, interval_hours, count, None)
            await update.message.reply_text(
                f"✅ *Individual recurring post setup complete!*\n\n"
                f"Post will be repeated {count} times every {interval_hours} hours.",
                parse_mode='Markdown'
            )
        else:
            # Set up bulk recurring posts
            success = await setup_recurring_posts_direct(user, interval_hours, count, None)
            
            if success:
                # Send confirmation message
                await update.message.reply_text(
                    f"✅ **Recurring posts setup complete!**\n\n"
                    f"Each post will be repeated {count} times every {interval_hours} hours."
                )
            else:
                await update.message.reply_text(
                    "❌ Failed to set up recurring posts. Please try again."
                )
        
    except ValueError:
        await update.message.reply_text(
            "❌ Please enter a valid number.\n\n"
            "Examples: 5, 10, 20, 30\n\n"
            "Try again:"
        )

async def handle_recurring_date_input(update: Update, user, text: str, session_data: dict):
    """Handle recurring end date input"""
    try:
        from datetime import datetime
        from bot.utils import get_kyiv_timezone
        
        # Parse the date string
        date_str = text.strip()
        
        # Try different date formats
        date_formats = [
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%m/%d/%Y %H:%M",
            "%d.%m.%Y %H:%M"
        ]
        
        end_date = None
        for fmt in date_formats:
            try:
                end_date = datetime.strptime(date_str, fmt)
                break
            except ValueError:
                continue
        
        if not end_date:
            await update.message.reply_text(
                "❌ Invalid date format. Please use one of these formats:\n\n"
                "• `YYYY-MM-DD HH:MM` (recommended)\n"
                "• `YYYY-MM-DD` (will use 00:00)\n"
                "• `MM/DD/YYYY HH:MM`\n\n"
                "**Examples:**\n"
                "• `2025-08-01 12:00`\n"
                "• `2025-12-31 23:59`\n\n"
                "Try again:"
            )
            return
        
        # Add timezone info
        kyiv_tz = get_kyiv_timezone()
        end_date = kyiv_tz.localize(end_date)
        
        # Check if date is in the future
        current_time = get_current_kyiv_time()
        if end_date <= current_time:
            await update.message.reply_text(
                "❌ End date must be in the future.\n\n"
                f"Current time: {current_time.strftime('%Y-%m-%d %H:%M')} (Kyiv)\n"
                f"Your date: {end_date.strftime('%Y-%m-%d %H:%M')} (Kyiv)\n\n"
                "Please enter a future date:"
            )
            return
        
        interval_hours = session_data.get('interval_hours')
        
        # Check if this is for individual recurring post
        if session_data.get('mode') == 'individual':
            # Set up individual recurring post with end date
            await setup_individual_recurring_post(None, user, interval_hours, None, end_date)
            await update.message.reply_text(
                f"✅ *Individual recurring post setup complete!*\n\n"
                f"Post will repeat every {interval_hours} hours until {end_date.strftime('%Y-%m-%d %H:%M')} (Kyiv time).",
                parse_mode='Markdown'
            )
        else:
            # Set up bulk recurring posts
            success = await setup_recurring_posts_direct(user, interval_hours, None, end_date)
            
            if success:
                # Send confirmation message  
                await update.message.reply_text(
                    f"✅ **Recurring posts setup complete!**\n\n"
                    f"Posts will repeat every {interval_hours} hours until {end_date.strftime('%Y-%m-%d %H:%M')} (Kyiv time)."
                )
            else:
                await update.message.reply_text(
                    "❌ Failed to set up recurring posts. Please try again."
                )
        
    except Exception as e:
        await update.message.reply_text(
            "❌ Error processing date. Please use this format:\n\n"
            "`YYYY-MM-DD HH:MM`\n\n"
            "**Examples:**\n"
            "• `2025-08-01 12:00`\n"
            "• `2025-12-31 23:59`\n\n"
            "Try again:"
        )


# New Multi-Channel Batch Management Handlers

async def multibatch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /multibatch command - multi-channel batch scheduling"""
    user = update.effective_user
    
    # Check if user has channels configured
    channels = Database.get_user_channels(user.id)
    
    if not channels:
        await update.message.reply_text(
            "❌ *No channels configured!*\n\n"
            "Please add channels first using /channels command.",
            parse_mode='Markdown'
        )
        return
    
    # Get existing batches
    batches = Database.get_user_batches(user.id)
    
    # Update user session
    Database.update_user_session(user.id, BotStates.MULTI_BATCH_MENU, {
        'start_time': datetime.now().isoformat()
    })
    
    keyboard = [
        [InlineKeyboardButton("📦 Create New Batch", callback_data="batch_create")],
        [InlineKeyboardButton("📋 View My Batches", callback_data="batch_list")],
        [InlineKeyboardButton("📅 Schedule All Batches", callback_data="batch_schedule_all")],
        [InlineKeyboardButton("🗑️ Clear All Batches", callback_data="batch_clear_all")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    batch_summary = ""
    if batches:
        batch_summary = f"\n\n*Current Batches:* {len(batches)}"
        for batch in batches[:3]:  # Show first 3 batches
            batch_summary += f"\n• {batch['batch_name']} → {batch['channel_name']} ({batch['post_count']} posts)"
        if len(batches) > 3:
            batch_summary += f"\n• ... and {len(batches) - 3} more"
    
    message = f"""
🔥 *Multi-Channel Batch Scheduler*

Create separate batches of posts for different channels! This lets you:
• Upload photos for multiple channels at once
• Schedule each batch independently
• Mix Mode 1 (bulk) and Mode 2 (with descriptions)

*Available Channels:* {len(channels)}{batch_summary}

Choose an option:
"""
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_batch_callback(query, user, data):
    """Handle batch management callbacks"""
    if data == "batch_create":
        await prompt_batch_creation(query, user)
    elif data == "batch_list":
        await show_batch_list(query, user)
    elif data == "batch_schedule_all":
        await schedule_all_batches(query, user)
    elif data == "batch_clear_all":
        await confirm_clear_all_batches(query, user)
    elif data.startswith("batch_select_"):
        batch_id = int(data.replace("batch_select_", ""))
        await show_batch_details(query, user, batch_id)
    elif data.startswith("batch_delete_"):
        batch_id = int(data.replace("batch_delete_", ""))
        await delete_batch_confirm(query, user, batch_id)
    elif data.startswith("batch_schedule_"):
        batch_id = int(data.replace("batch_schedule_", ""))
        await schedule_single_batch(query, user, batch_id)
    elif data.startswith("batch_channel_"):
        channel_id = data.replace("batch_channel_", "")
        await create_batch_for_channel(query, user, channel_id)
    elif data == "batch_back":
        # Show main batch menu again
        channels = Database.get_user_channels(user.id)
        batches = Database.get_user_batches(user.id)
        
        keyboard = [
            [InlineKeyboardButton("📦 Create New Batch", callback_data="batch_create")],
            [InlineKeyboardButton("📋 View My Batches", callback_data="batch_list")],
            [InlineKeyboardButton("📅 Schedule All Batches", callback_data="batch_schedule_all")],
            [InlineKeyboardButton("🗑️ Clear All Batches", callback_data="batch_clear_all")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        batch_summary = ""
        if batches:
            batch_summary = f"\n\n*Current Batches:* {len(batches)}"
            for batch in batches[:3]:
                batch_summary += f"\n• {batch['batch_name']} → {batch['channel_name']} ({batch['post_count']} posts)"
            if len(batches) > 3:
                batch_summary += f"\n• ... and {len(batches) - 3} more"
        
        message = f"""
🔥 *Multi-Channel Batch Scheduler*

Create separate batches of posts for different channels! This lets you:
• Upload photos for multiple channels at once
• Schedule each batch independently
• Mix Mode 1 (bulk) and Mode 2 (with descriptions)

*Available Channels:* {len(channels)}{batch_summary}

Choose an option:
"""
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
    elif data.startswith("batch_clear_confirmed"):
        # Clear all batches
        batches = Database.get_user_batches(user.id)
        for batch in batches:
            Database.delete_batch(batch['id'])
        await query.edit_message_text("✅ All batches cleared successfully!")
    elif data.startswith("batch_delete_confirmed_"):
        batch_id = int(data.replace("batch_delete_confirmed_", ""))
        success = Database.delete_batch(batch_id)
        if success:
            await query.edit_message_text("✅ Batch deleted successfully!")
        else:
            await query.edit_message_text("❌ Failed to delete batch.")

async def handle_batch_mode_callback(query, user, data):
    """Handle batch mode selection callbacks"""
    if data.startswith("batch_mode1_"):
        batch_id = int(data.replace("batch_mode1_", ""))
        await start_batch_mode1(query, user, batch_id)
    elif data.startswith("batch_mode2_"):
        batch_id = int(data.replace("batch_mode2_", ""))
        await start_batch_mode2(query, user, batch_id)

async def prompt_batch_creation(query, user):
    """Prompt user to create a new batch"""
    channels = Database.get_user_channels(user.id)
    
    keyboard = []
    for channel in channels:
        status = "⭐ " if channel['is_default'] else ""
        keyboard.append([InlineKeyboardButton(
            f"{status}{channel['channel_name']}", 
            callback_data=f"batch_channel_{channel['channel_id']}"
        )])
    
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="batch_back")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "*📦 Create New Batch*\n\n"
        "Select which channel this batch will be for:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def create_batch_for_channel(query, user, channel_id):
    """Create a new batch for the selected channel"""
    Database.update_user_session(user.id, BotStates.WAITING_BATCH_NAME, {
        'channel_id': channel_id,
        'start_time': datetime.now().isoformat()
    })
    
    # Get channel name
    channels = Database.get_user_channels(user.id)
    channel_name = next((c['channel_name'] for c in channels if c['channel_id'] == channel_id), channel_id)
    
    await query.edit_message_text(
        f"*📦 Create Batch for {channel_name}*\n\n"
        "Please send a name for this batch.\n\n"
        "*Examples:*\n"
        "• Morning Posts\n"
        "• Product Launch\n"
        "• Weekly Updates\n\n"
        "Send the batch name:",
        parse_mode='Markdown'
    )

async def handle_batch_name_input(update: Update, user, text: str, session_data: dict):
    """Handle batch name input"""
    batch_name = text.strip()
    channel_id = session_data.get('channel_id')
    
    if not batch_name or len(batch_name) > 50:
        await update.message.reply_text(
            "❌ Batch name must be between 1 and 50 characters.\n\nTry again:"
        )
        return
    
    try:
        batch_id = Database.create_batch(user.id, batch_name, channel_id)
        
        # Get channel name
        channels = Database.get_user_channels(user.id)
        channel_name = next((c['channel_name'] for c in channels if c['channel_id'] == channel_id), channel_id)
        
        keyboard = [
            [
                InlineKeyboardButton("📸 Mode 1 (Bulk)", callback_data=f"batch_mode1_{batch_id}"),
                InlineKeyboardButton("📝 Mode 2 (Descriptions)", callback_data=f"batch_mode2_{batch_id}")
            ],
            [InlineKeyboardButton("🔙 Back to Batches", callback_data="batch_list")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"✅ *Batch Created!*\n\n"
            f"*Name:* {batch_name}\n"
            f"*Channel:* {channel_name}\n\n"
            "Now choose how to add posts:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
        Database.update_user_session(user.id, BotStates.MULTI_BATCH_MENU, {
            'current_batch_id': batch_id,
            'batch_name': batch_name
        })
        
    except Exception as e:
        await update.message.reply_text(
            f"❌ Error creating batch: {e}\n\nTry again with a different name:"
        )

async def start_batch_mode1(query, user, batch_id):
    """Start Mode 1 (bulk) for a specific batch"""
    Database.update_user_session(user.id, BotStates.BATCH_MODE1_PHOTOS, {
        'batch_id': batch_id,
        'media_items': [],
        'start_time': datetime.now().isoformat()
    })
    
    await query.edit_message_text(
        "*📸 Batch Mode 1: Bulk Upload*\n\n"
        "Send me all the photos/media you want for this batch:\n"
        "• Upload one by one or as albums\n"
        "• All types supported (photos, videos, etc.)\n"
        "• Use /finish when done uploading\n\n"
        "🔄 Ready to receive media...",
        parse_mode='Markdown'
    )

async def start_batch_mode2(query, user, batch_id):
    """Start Mode 2 (with descriptions) for a specific batch"""
    Database.update_user_session(user.id, BotStates.BATCH_MODE2_PHOTOS, {
        'batch_id': batch_id,
        'media_items': [],
        'current_media_path': None,
        'start_time': datetime.now().isoformat()
    })
    
    await query.edit_message_text(
        "*📝 Batch Mode 2: With Descriptions*\n\n"
        "Upload media one by one with descriptions:\n"
        "1. Send a photo/video/document\n"
        "2. I'll ask for a description\n"
        "3. Repeat for each item\n"
        "4. Use /finish when done\n\n"
        "📸 Send your first media...",
        parse_mode='Markdown'
    )

async def show_batch_list(query, user):
    """Show list of user's batches"""
    batches = Database.get_user_batches(user.id)
    
    if not batches:
        keyboard = [[InlineKeyboardButton("📦 Create First Batch", callback_data="batch_create")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "*📋 No Batches Yet*\n\n"
            "Create your first batch to start organizing posts by channel!",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return
    
    keyboard = []
    for batch in batches:
        status_icon = "✅" if batch['status'] == 'scheduled' else "📦"
        keyboard.append([InlineKeyboardButton(
            f"{status_icon} {batch['batch_name']} → {batch['channel_name']} ({batch['post_count']})",
            callback_data=f"batch_select_{batch['id']}"
        )])
    
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="batch_back")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"*📋 Your Batches ({len(batches)})*\n\n"
        "Select a batch to view details or schedule:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def show_batch_details(query, user, batch_id):
    """Show details of a specific batch"""
    batches = Database.get_user_batches(user.id)
    batch = next((b for b in batches if b['id'] == batch_id), None)
    
    if not batch:
        await query.edit_message_text("❌ Batch not found.")
        return
    
    posts = Database.get_batch_posts(batch_id)
    
    keyboard = []
    if posts:
        keyboard.append([InlineKeyboardButton("📅 Schedule This Batch", callback_data=f"batch_schedule_{batch_id}")])
    keyboard.extend([
        [InlineKeyboardButton("🗑️ Delete Batch", callback_data=f"batch_delete_{batch_id}")],
        [InlineKeyboardButton("🔙 Back to List", callback_data="batch_list")]
    ])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    status_text = "✅ Scheduled" if batch['status'] == 'scheduled' else "📦 Pending"
    post_summary = ""
    if posts:
        media_types = {}
        for post in posts:
            media_type = post['media_type']
            media_types[media_type] = media_types.get(media_type, 0) + 1
        
        post_summary = "\n\n*Contents:*\n"
        for media_type, count in media_types.items():
            icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
            post_summary += f"• {icon} {count} {media_type}{'s' if count > 1 else ''}\n"
    
    await query.edit_message_text(
        f"*📦 Batch Details*\n\n"
        f"*Name:* {batch['batch_name']}\n"
        f"*Channel:* {batch['channel_name']}\n"
        f"*Status:* {status_text}\n"
        f"*Posts:* {len(posts)}{post_summary}",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def schedule_single_batch(query, user, batch_id):
    """Schedule a single batch"""
    posts = Database.get_batch_posts(batch_id)
    
    if not posts:
        await query.edit_message_text("❌ No posts in this batch to schedule.")
        return
    
    # Get scheduling config
    start_hour, end_hour, interval_hours = Database.get_scheduling_config(user.id)
    
    # Calculate schedule times
    from bot.utils import calculate_schedule_times
    schedule_times = calculate_schedule_times(start_hour, end_hour, interval_hours, len(posts))
    
    # Schedule the batch
    Database.schedule_batch(batch_id, schedule_times)
    
    # Import scheduler and schedule posts
    from bot.scheduler import PostScheduler
    scheduler = PostScheduler()
    post_ids = [post['id'] for post in posts]
    await scheduler.schedule_posts(post_ids, schedule_times)
    
    batch = Database.get_user_batches(user.id)
    batch = next((b for b in batch if b['id'] == batch_id), None)
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Batches", callback_data="batch_list")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ *Batch Scheduled!*\n\n"
        f"*Batch:* {batch['batch_name'] if batch else 'Unknown'}\n"
        f"*Posts:* {len(posts)}\n"
        f"*First post:* {schedule_times[0].strftime('%Y-%m-%d %H:%M')} (Kyiv)\n"
        f"*Last post:* {schedule_times[-1].strftime('%Y-%m-%d %H:%M')} (Kyiv)\n\n"
        "All posts are now scheduled for automatic posting!",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    

async def batch_finish_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /finish command for batch modes"""
    user = update.effective_user
    mode, session_data = Database.get_user_session(user.id)
    
    if mode not in [BotStates.BATCH_MODE1_PHOTOS, BotStates.BATCH_MODE2_PHOTOS]:
        await update.message.reply_text("Use /finish only when uploading to a batch.")
        return
    
    batch_id = session_data.get('batch_id')
    media_items = session_data.get('media_items', [])
    
    if not media_items:
        await update.message.reply_text("❌ No media uploaded yet. Upload some media first.")
        return
    
    # Get batch info
    batches = Database.get_user_batches(user.id)
    batch = next((b for b in batches if b['id'] == batch_id), None)
    
    keyboard = [
        [InlineKeyboardButton("📅 Schedule This Batch", callback_data=f"batch_schedule_{batch_id}")],
        [InlineKeyboardButton("📦 Create Another Batch", callback_data="batch_create")],
        [InlineKeyboardButton("📋 View All Batches", callback_data="batch_list")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"✅ *Batch Complete!*\n\n"
        f"*Batch:* {batch['batch_name'] if batch else 'Unknown'}\n"
        f"*Channel:* {batch['channel_name'] if batch else 'Unknown'}\n"
        f"*Media uploaded:* {len(media_items)}\n\n"
        "What would you like to do next?",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    Database.update_user_session(user.id, BotStates.MULTI_BATCH_MENU, {})

# Additional batch helper functions

async def handle_batch_media_upload_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                                  user, mode: str, session_data: dict, media_type: str):
    """Handle media upload for batch modes"""
    
    if mode not in [BotStates.BATCH_MODE1_PHOTOS, BotStates.BATCH_MODE2_PHOTOS]:
        return
    
    batch_id = session_data.get('batch_id')
    if not batch_id:
        await update.message.reply_text("❌ No batch selected. Please start again.")
        return
    
    try:
        # Get the media file based on type
        if media_type == 'photo':
            media_file = update.message.photo[-1]
            original_filename = f"photo_{media_file.file_id}.jpg"
        elif media_type == 'video':
            media_file = update.message.video
            original_filename = f"video_{media_file.file_id}.mp4"
        elif media_type == 'audio':
            media_file = update.message.audio
            original_filename = f"audio_{media_file.file_id}.mp3"
        elif media_type == 'animation':
            media_file = update.message.animation
            original_filename = f"animation_{media_file.file_id}.gif"
        elif media_type == 'document':
            media_file = update.message.document
            original_filename = f"document_{media_file.file_id}_{media_file.file_name or 'file'}"
        else:
            await update.message.reply_text("Unsupported media type.")
            return
        
        file = await context.bot.get_file(media_file.file_id)
        
        # Generate unique filename and save
        from bot.utils import generate_unique_filename, save_media
        filename = generate_unique_filename(original_filename)
        file_data = await file.download_as_bytearray()
        file_path = save_media(bytes(file_data), filename, media_type)
        
        if mode == BotStates.BATCH_MODE1_PHOTOS:
            await handle_batch_mode1_media(update, user, file_path, media_type, session_data, batch_id)
        elif mode == BotStates.BATCH_MODE2_PHOTOS:
            await handle_batch_mode2_media(update, user, file_path, media_type, session_data)
            
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error handling batch {media_type} upload: {e}")
        await update.message.reply_text(
            f"❌ Error processing {media_type}: {e}\nPlease try again."
        )

async def handle_batch_mode1_media(update: Update, user, file_path: str, media_type: str, session_data: dict, batch_id: int):
    """Handle media upload in Batch Mode 1"""
    
    # Add media to batch
    post_id = Database.add_post_to_batch(user.id, file_path, batch_id, media_type=media_type, mode=1)
    
    # Update session data
    media_items = session_data.get('media_items', [])
    media_items.append({
        'post_id': post_id,
        'file_path': file_path,
        'media_type': media_type,
        'uploaded_at': datetime.now().isoformat()
    })
    
    session_data['media_items'] = media_items
    Database.update_user_session(user.id, BotStates.BATCH_MODE1_PHOTOS, session_data)
    
    # Format media type for display
    media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
    
    await update.message.reply_text(
        f"✅ {media_icon} {media_type.title()} {len(media_items)} added to batch!\n"
        f"Total in batch: {len(media_items)}\n\n"
        f"Continue uploading or use /finish when ready."
    )

async def handle_batch_mode2_media(update: Update, user, file_path: str, media_type: str, session_data: dict):
    """Handle media upload in Batch Mode 2"""
    
    # Store media path and type, ask for description
    session_data['current_media_path'] = file_path
    session_data['current_media_type'] = media_type
    Database.update_user_session(user.id, BotStates.BATCH_MODE2_DESCRIPTION, session_data)
    
    # Format media type for display
    media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
    
    await update.message.reply_text(
        f"📝 {media_icon} {media_type.title()} received! Please send a description (or 'skip'):"
    )

async def handle_batch_mode2_description(update: Update, user, description: str, session_data: dict):
    """Handle description input in Batch Mode 2"""
    
    file_path = session_data.get('current_media_path')
    media_type = session_data.get('current_media_type', 'photo')
    batch_id = session_data.get('batch_id')
    
    if not file_path or not batch_id:
        await update.message.reply_text("❌ No media or batch found. Please start again.")
        return
    
    # Process description
    final_description = None if description.lower() == 'skip' else description
    
    # Add media to batch
    post_id = Database.add_post_to_batch(user.id, file_path, batch_id, media_type=media_type, description=final_description, mode=2)
    
    # Update session data
    media_items = session_data.get('media_items', [])
    media_items.append({
        'post_id': post_id,
        'file_path': file_path,
        'media_type': media_type,
        'description': final_description,
        'uploaded_at': datetime.now().isoformat()
    })
    
    session_data['media_items'] = media_items
    session_data['current_media_path'] = None
    session_data['current_media_type'] = None
    Database.update_user_session(user.id, BotStates.BATCH_MODE2_PHOTOS, session_data)
    
    # Format media type for display
    media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
    desc_text = f'"{final_description}"' if final_description else "no description"
    
    await update.message.reply_text(
        f"✅ {media_icon} {media_type.title()} {len(media_items)} saved with {desc_text}!\n\n"
        f"Send another media or use /finish when done."
    )

async def schedule_all_batches(query, user):
    """Schedule all pending batches"""
    batches = Database.get_user_batches(user.id)
    pending_batches = [b for b in batches if b['status'] == 'pending' and b['post_count'] > 0]
    
    if not pending_batches:
        await query.edit_message_text("❌ No pending batches to schedule.")
        return
    
    # Schedule each batch
    total_scheduled = 0
    for batch in pending_batches:
        posts = Database.get_batch_posts(batch['id'])
        if posts:
            # Get scheduling config
            start_hour, end_hour, interval_hours = Database.get_scheduling_config(user.id)
            
            # Calculate schedule times
            from bot.utils import calculate_schedule_times
            schedule_times = calculate_schedule_times(start_hour, end_hour, interval_hours, len(posts))
            
            # Schedule the batch
            Database.schedule_batch(batch['id'], schedule_times)
            
            # Import scheduler and schedule posts
            from bot.scheduler import PostScheduler
            scheduler = PostScheduler()
            post_ids = [post['id'] for post in posts]
            await scheduler.schedule_posts(post_ids, schedule_times)
            
            total_scheduled += len(posts)
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Batches", callback_data="batch_list")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ *All Batches Scheduled!*\n\n"
        f"*Batches:* {len(pending_batches)}\n"
        f"*Total Posts:* {total_scheduled}\n\n"
        "All posts are now scheduled for automatic posting!",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    # Clear any remaining pending posts from queue after successful batch scheduling
    Database.clear_queued_posts(user.id)

async def confirm_clear_all_batches(query, user):
    """Confirm clearing all batches"""
    batches = Database.get_user_batches(user.id)
    
    if not batches:
        await query.edit_message_text("❌ No batches to clear.")
        return
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, Clear All", callback_data="batch_clear_confirmed"),
            InlineKeyboardButton("❌ Cancel", callback_data="batch_list")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    total_posts = sum(b['post_count'] for b in batches)
    
    await query.edit_message_text(
        f"⚠️ *Confirm Clear All Batches*\n\n"
        f"This will delete:\n"
        f"• {len(batches)} batches\n"
        f"• {total_posts} posts\n"
        f"• All media files\n\n"
        "This action cannot be undone. Continue?",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def delete_batch_confirm(query, user, batch_id):
    """Confirm batch deletion"""
    batches = Database.get_user_batches(user.id)
    batch = next((b for b in batches if b['id'] == batch_id), None)
    
    if not batch:
        await query.edit_message_text("❌ Batch not found.")
        return
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"batch_delete_confirmed_{batch_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"batch_select_{batch_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"⚠️ *Confirm Delete Batch*\n\n"
        f"*Batch:* {batch['batch_name']}\n"
        f"*Posts:* {batch['post_count']}\n\n"
        "This will delete all posts and media files in this batch. Continue?",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def prompt_channel_selection_for_mode(update, user_id: int, channels: list, mode: int):
    """Show channel selection for mode setup"""
    keyboard = []
    for channel in channels:
        keyboard.append([InlineKeyboardButton(
            f"📺 {channel['channel_name']}", 
            callback_data=f"mode{mode}_channel_{channel['channel_id']}"
        )])
    
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    mode_name = "Bulk Upload" if mode == 1 else "Individual Upload"
    message = f"📺 *Select Channel for Mode {mode} ({mode_name}):*\n\n"
    for i, channel in enumerate(channels, 1):
        message += f"{i}. {channel['channel_name']}\n   ID: `{channel['channel_id']}`\n\n"
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def prompt_channel_selection_for_mode_inline(query, user_id: int, channels: list, mode: int):
    """Show channel selection for mode setup (inline version)"""
    keyboard = []
    for channel in channels:
        keyboard.append([InlineKeyboardButton(
            f"📺 {channel['channel_name']}", 
            callback_data=f"mode{mode}_channel_{channel['channel_id']}"
        )])
    
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    mode_name = "Bulk Upload" if mode == 1 else "Individual Upload"
    message = f"📺 *Select Channel for Mode {mode} ({mode_name}):*\n\n"
    for i, channel in enumerate(channels, 1):
        message += f"{i}. {channel['channel_name']}\n   ID: `{channel['channel_id']}`\n\n"
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_mode_channel_selection(query, user, data):
    """Handle channel selection for mode setup"""
    parts = data.split("_")
    if len(parts) < 3:
        await query.edit_message_text("❌ Invalid selection.")
        return
    
    mode = int(parts[0].replace("mode", ""))
    channel_id = "_".join(parts[2:])  # Handle channel IDs with underscores
    
    # Get channel info
    channels = Database.get_user_channels(user.id)
    selected_channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
    
    if not selected_channel:
        await query.edit_message_text("❌ Channel not found.")
        return
    
    # Clear any existing pending posts for this user, mode, and channel to ensure complete separation
    Database.clear_user_posts(user.id, mode=mode, channel_id=channel_id)
    
    # Set up the mode with the selected channel
    if mode == 1:
        Database.update_user_session(user.id, BotStates.MODE1_PHOTOS, {
            'media_items': [],
            'start_time': datetime.now().isoformat(),
            'selected_channel_id': selected_channel['channel_id']
        })
        
        message = f"""
📸 *Mode 1: Bulk Photo Upload*

*Target Channel:* {selected_channel['channel_name']} ({selected_channel['channel_id']})

Please send me all the photos you want to schedule. You can:
• Send photos one by one
• Send multiple photos as an album
• Send as many as you need

When you're done uploading, use /schedule to set your posting schedule.
Use /cancel to abort this mode.

🔄 Ready to receive photos...
"""
        
    else:  # mode == 2
        Database.update_user_session(user.id, BotStates.MODE2_PHOTOS, {
            'media_items': [],
            'current_media_path': None,
            'start_time': datetime.now().isoformat(),
            'selected_channel_id': selected_channel['channel_id']
        })
        
        message = f"""
📝 *Mode 2: Individual Photo Upload*

*Target Channel:* {selected_channel['channel_name']} ({selected_channel['channel_id']})

Upload photos one by one with custom descriptions:

1. Send a photo
2. I'll ask for a description
3. Repeat for each photo
4. Use /finish when done
5. Then /schedule to set posting times

Use /cancel to abort this mode.

📸 Send your first photo...
"""
    
    await query.edit_message_text(message, parse_mode='Markdown')

async def handle_clearscheduled_callback(query, user, data):
    """Handle clearscheduled confirmation callbacks"""
    if data == "clearscheduled_confirm_all":
        # Clear all scheduled posts
        cleared_count = Database.clear_scheduled_posts(user.id)
        
        # Also cancel the scheduled jobs from the scheduler
        try:
            from bot.scheduler import PostScheduler
            scheduler = PostScheduler()
            scheduler.cancel_user_posts(user.id)
        except Exception as e:
            pass  # Continue even if scheduler cancel fails
        
        await query.edit_message_text(
            f"✅ *Scheduled Posts Cleared*\n\n"
            f"Successfully cleared *{cleared_count} scheduled posts* and removed all media files.\n\n"
            f"Your posting schedule has been reset. Use /mode1 or /mode2 to upload new content!",
            parse_mode='Markdown'
        )
    
    elif data == "clearscheduled_select_channel":
        # Show channel selection for clearing specific channel
        channels = Database.get_user_channels(user.id)
        
        if not channels:
            await query.edit_message_text(
                "❌ *No Channels Found*\n\n"
                "You need to add channels first using /channels command.",
                parse_mode='Markdown'
            )
            return
        
        # Get scheduled posts by channel to show only channels with scheduled posts
        scheduled_posts_by_channel = Database.get_scheduled_posts_by_channel(user.id)
        channels_with_posts = [ch for ch in channels if ch['channel_id'] in scheduled_posts_by_channel and scheduled_posts_by_channel[ch['channel_id']]]
        
        if not channels_with_posts:
            await query.edit_message_text(
                "📅 *No Channels with Scheduled Posts*\n\n"
                "None of your channels have scheduled posts to clear.",
                parse_mode='Markdown'
            )
            return
        
        keyboard = []
        for channel in channels_with_posts:
            posts_count = len(scheduled_posts_by_channel.get(channel['channel_id'], []))
            keyboard.append([InlineKeyboardButton(
                f"🗑 {channel['channel_name']} ({posts_count} posts)", 
                callback_data=f"clearscheduled_channel_{channel['channel_id']}"
            )])
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "📺 *Select Channel to Clear*\n\n"
            "Choose which channel's scheduled posts to clear:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    elif data.startswith("clearscheduled_channel_"):
        channel_id = data.replace("clearscheduled_channel_", "")
        
        # Get channel info
        channels = Database.get_user_channels(user.id)
        channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
        
        if not channel:
            await query.edit_message_text("❌ Channel not found.")
            return
        
        # Clear scheduled posts for this channel
        cleared_count = Database.clear_scheduled_posts(user.id, channel_id)
        
        await query.edit_message_text(
            f"✅ *Channel Scheduled Posts Cleared*\n\n"
            f"Successfully cleared *{cleared_count} scheduled posts* from channel:\n"
            f"*{channel['channel_name']}* ({channel_id})\n\n"
            f"Media files have been removed. Other channels' schedules remain intact.",
            parse_mode='Markdown'
        )

async def retry_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /retry command to retry failed posts"""
    user = update.effective_user
    
    # Get all failed posts for the user
    failed_posts = Database.get_failed_posts(user.id)
    
    if not failed_posts:
        await update.message.reply_text(
            "✅ **No Failed Posts**\n\n"
            "You don't have any failed posts to retry.\n"
            "All your posts were published successfully!",
            parse_mode='Markdown'
        )
        return
    
    # Group failed posts by channel for better organization
    posts_by_channel = {}
    channels = Database.get_user_channels(user.id)
    channel_names = {ch['channel_id']: ch['channel_name'] for ch in channels}
    
    for post in failed_posts:
        channel_id = post['channel_id']
        channel_name = channel_names.get(channel_id, f"Channel {channel_id}")
        
        if channel_name not in posts_by_channel:
            posts_by_channel[channel_name] = []
        posts_by_channel[channel_name].append(post)
    
    # Create inline keyboard for retry options
    keyboard = []
    
    # Add individual post retry buttons (limit to 10 most recent)
    post_count = 0
    for channel_name, posts in posts_by_channel.items():
        for post in posts[:5]:  # Limit to 5 posts per channel for keyboard space
            if post_count >= 10:  # Total limit of 10 posts
                break
            
            media_type = post['media_type'].capitalize()
            description_preview = (post['description'][:20] + "...") if post['description'] and len(post['description']) > 20 else (post['description'] or "No description")
            
            button_text = f"🔄 {media_type} - {description_preview}"
            keyboard.append([InlineKeyboardButton(
                button_text, 
                callback_data=f"retry_post_{post['id']}"
            )])
            post_count += 1
        
        if post_count >= 10:
            break
    
    # Add "Retry All" option if there are multiple failed posts
    if len(failed_posts) > 1:
        keyboard.append([InlineKeyboardButton(
            f"🔄 Retry All ({len(failed_posts)} posts)", 
            callback_data="retry_all"
        )])
    
    # Add channel-specific retry options if there are multiple channels
    if len(posts_by_channel) > 1:
        keyboard.append([InlineKeyboardButton("📺 Retry by Channel", callback_data="retry_by_channel")])
    
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Create summary message
    total_failed = len(failed_posts)
    channels_text = ", ".join(posts_by_channel.keys())
    
    message = f"""
🔄 **Failed Posts Recovery**

Found **{total_failed}** failed posts across channels:
{channels_text}

**Options:**
• **Individual:** Select specific posts to retry
• **Bulk:** Retry all failed posts at once
• **Channel:** Retry all posts from specific channels

**What happens when you retry:**
• Failed posts are reset to pending status
• They will be rescheduled automatically
• Original scheduling time and descriptions are preserved

Choose an option below:
"""
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_retry_callback(query, user, data):
    """Handle retry-related callback queries"""
    if data == "retry_all":
        # Retry all failed posts for the user
        failed_posts = Database.get_failed_posts(user.id)
        
        if not failed_posts:
            await query.edit_message_text("✅ No failed posts found to retry.")
            return
        
        success_count = 0
        for post in failed_posts:
            if Database.retry_failed_post(post['id']):
                success_count += 1
        
        await query.edit_message_text(
            f"✅ **Retry Complete**\n\n"
            f"Successfully reset **{success_count}** failed posts to pending status.\n"
            f"They will be automatically rescheduled and posted.\n\n"
            f"Use /stats to monitor their progress.",
            parse_mode='Markdown'
        )
        
    elif data.startswith("retry_post_"):
        post_id = int(data.replace("retry_post_", ""))
        
        if Database.retry_failed_post(post_id):
            await query.edit_message_text(
                f"✅ **Post Retry Successful**\n\n"
                f"Post #{post_id} has been reset to pending status and will be rescheduled automatically.\n\n"
                f"Use /stats to monitor its progress.",
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                f"❌ **Retry Failed**\n\n"
                f"Could not retry post #{post_id}. It may not exist or is not in failed status.",
                parse_mode='Markdown'
            )
            
    elif data == "retry_by_channel":
        # Show channel selection for retry
        failed_posts = Database.get_failed_posts(user.id)
        
        if not failed_posts:
            await query.edit_message_text("✅ No failed posts found to retry.")
            return
        
        # Group posts by channel
        posts_by_channel = {}
        channels = Database.get_user_channels(user.id)
        channel_names = {ch['channel_id']: ch['channel_name'] for ch in channels}
        
        for post in failed_posts:
            channel_id = post['channel_id']
            if channel_id not in posts_by_channel:
                posts_by_channel[channel_id] = []
            posts_by_channel[channel_id].append(post)
        
        # Create keyboard for channel selection
        keyboard = []
        for channel_id, posts in posts_by_channel.items():
            channel_name = channel_names.get(channel_id, f"Channel {channel_id}")
            post_count = len(posts)
            keyboard.append([InlineKeyboardButton(
                f"🔄 {channel_name} ({post_count} posts)", 
                callback_data=f"retry_channel_{channel_id}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="retry_back")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "📺 **Select Channel to Retry**\n\n"
            "Choose which channel's failed posts to retry:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
    elif data.startswith("retry_channel_"):
        channel_id = data.replace("retry_channel_", "")
        
        # Retry all failed posts for specific channel
        failed_posts = Database.get_failed_posts(user.id, channel_id)
        
        if not failed_posts:
            await query.edit_message_text("✅ No failed posts found for this channel.")
            return
        
        success_count = 0
        for post in failed_posts:
            if Database.retry_failed_post(post['id']):
                success_count += 1
        
        # Get channel name
        channels = Database.get_user_channels(user.id)
        channel_name = next((ch['channel_name'] for ch in channels if ch['channel_id'] == channel_id), f"Channel {channel_id}")
        
        await query.edit_message_text(
            f"✅ **Channel Retry Complete**\n\n"
            f"Successfully reset **{success_count}** failed posts from **{channel_name}** to pending status.\n"
            f"They will be automatically rescheduled and posted.\n\n"
            f"Use /stats to monitor their progress.",
            parse_mode='Markdown'
        )

async def calendar_view_handler(query, user):
    """Display calendar view with scheduled posts"""
    from .utils import get_current_kyiv_time
    
    current_time = get_current_kyiv_time()
    await show_calendar_month(query, user, current_time.year, current_time.month)

async def show_calendar_month(query, user, year: int, month: int):
    """Show calendar for specific month with scheduled posts"""
    from calendar import monthrange
    
    # Get posts for the entire month
    start_date = datetime(year, month, 1)
    last_day = monthrange(year, month)[1]
    end_date = datetime(year, month, last_day)
    
    posts_by_date = Database.get_posts_by_date_range(user.id, start_date, end_date)
    
    # Generate calendar view
    calendar_text = generate_mini_calendar(year, month, posts_by_date)
    
    # Add summary
    total_posts = sum(len(posts) for posts in posts_by_date.values())
    calendar_text += f"\n📊 *Total posts this month:* {total_posts}\n"
    
    if total_posts > 0:
        calendar_text += "\n*📅 Click on a date below to see detailed schedule:*\n"
    
    # Create navigation and date selection buttons
    prev_month, next_month = get_calendar_navigation_dates(datetime(year, month, 1))
    
    keyboard = []
    
    # Navigation buttons
    keyboard.append([
        InlineKeyboardButton("⬅️ Previous", callback_data=f"cal_nav_{prev_month.year}_{prev_month.month}"),
        InlineKeyboardButton("➡️ Next", callback_data=f"cal_nav_{next_month.year}_{next_month.month}")
    ])
    
    # Date selection buttons for days with posts
    if posts_by_date:
        date_buttons = []
        for date_str in sorted(posts_by_date.keys())[:12]:  # Limit to 12 buttons
            date_obj = datetime.strptime(date_str, '%Y-%m-%d')
            day_num = date_obj.day
            post_count = len(posts_by_date[date_str])
            date_buttons.append(InlineKeyboardButton(
                f"{day_num} ({post_count})", 
                callback_data=f"cal_day_{date_str}"
            ))
        
        # Arrange date buttons in rows of 4
        for i in range(0, len(date_buttons), 4):
            keyboard.append(date_buttons[i:i+4])
    
    # Quick actions
    keyboard.append([
        InlineKeyboardButton("📅 Today", callback_data="cal_today"),
        InlineKeyboardButton("📊 This Week", callback_data="cal_week")
    ])
    
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(calendar_text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_calendar_callback(query, user, data):
    """Handle calendar-related callbacks"""
    if data.startswith("cal_nav_"):
        # Navigation: cal_nav_YYYY_MM
        parts = data.split("_")
        year, month = int(parts[2]), int(parts[3])
        await show_calendar_month(query, user, year, month)
        
    elif data.startswith("cal_day_"):
        # Day view: cal_day_YYYY-MM-DD
        date_str = data.replace("cal_day_", "")
        await show_calendar_day(query, user, date_str)
        
    elif data == "cal_today":
        from .utils import get_current_kyiv_time
        current_time = get_current_kyiv_time()
        today_str = current_time.strftime('%Y-%m-%d')
        await show_calendar_day(query, user, today_str)
        
    elif data == "cal_week":
        await show_calendar_week(query, user)

async def show_calendar_day(query, user, date_str: str):
    """Show detailed schedule for a specific day"""
    try:
        date_obj = datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        await query.edit_message_text("❌ Invalid date format.")
        return
    
    # Get posts for this specific day
    posts_by_date = Database.get_posts_by_date_range(user.id, date_obj, date_obj)
    posts = posts_by_date.get(date_str, [])
    
    # Format the day schedule
    schedule_text = format_daily_schedule(date_str, posts)
    
    # Add quick stats
    if posts:
        channel_counts = {}
        for post in posts:
            channel = post['channel_name']
            channel_counts[channel] = channel_counts.get(channel, 0) + 1
        
        schedule_text += "\n*📊 Channels Summary:*\n"
        for channel, count in channel_counts.items():
            schedule_text += f"• {channel}: {count} posts\n"
    
    # Navigation buttons
    keyboard = [
        [InlineKeyboardButton("📅 Back to Calendar", callback_data=f"cal_nav_{date_obj.year}_{date_obj.month}")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(schedule_text, reply_markup=reply_markup, parse_mode='Markdown')

async def show_calendar_week(query, user):
    """Show weekly overview of scheduled posts"""
    from .utils import get_current_kyiv_time
    
    current_time = get_current_kyiv_time()
    
    # Get start of week (Monday)
    days_since_monday = current_time.weekday()
    week_start = current_time - timedelta(days=days_since_monday)
    week_end = week_start + timedelta(days=6)
    
    # Get posts for the week
    posts_by_date = Database.get_posts_by_date_range(user.id, week_start, week_end)
    
    # Format week view
    week_text = f"📅 *Week of {week_start.strftime('%B %d')} - {week_end.strftime('%B %d, %Y')}*\n\n"
    
    total_posts = 0
    for i in range(7):
        day = week_start + timedelta(days=i)
        date_str = day.strftime('%Y-%m-%d')
        day_name = day.strftime('%A')
        day_posts = posts_by_date.get(date_str, [])
        
        if day_posts:
            total_posts += len(day_posts)
            week_text += f"📅 *{day_name} ({day.day})*: {len(day_posts)} posts\n"
            
            # Show first few posts
            for post in day_posts[:3]:
                time_str = post['scheduled_time'].strftime('%H:%M')
                icon = get_media_icon(post['media_type'])
                week_text += f"  🕐 {time_str} {icon} → {post['channel_name'][:20]}\n"
            
            if len(day_posts) > 3:
                week_text += f"  ... and {len(day_posts) - 3} more\n"
            week_text += "\n"
        else:
            week_text += f"📅 *{day_name} ({day.day})*: No posts\n"
    
    week_text += f"\n📊 *Total posts this week:* {total_posts}"
    
    # Navigation buttons
    keyboard = [
        [InlineKeyboardButton("📅 Back to Calendar", callback_data=f"cal_nav_{current_time.year}_{current_time.month}")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(week_text, reply_markup=reply_markup, parse_mode='Markdown')

# Bulk Edit Functionality

async def bulkedit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /bulkedit command to redistribute scheduled posts"""
    user = update.effective_user
    
    # Check if user has scheduled posts
    posts = Database.get_scheduled_posts_for_channel(user.id)
    
    if not posts:
        await update.message.reply_text(
            "❌ *No scheduled posts found!*\n\n"
            "You need to have posts scheduled before you can bulk edit them.\n"
            "Upload and schedule posts using /mode1 or /mode2 first.",
            parse_mode='Markdown'
        )
        return
    
    # Get user channels
    channels = Database.get_user_channels(user.id)
    
    if not channels:
        await update.message.reply_text(
            "❌ *No channels configured!*\n\n"
            "Please add a channel first using /channels command.",
            parse_mode='Markdown'
        )
        return
    
    # Group posts by channel and mode
    posts_by_channel = {}
    posts_by_mode = {"mode1": [], "mode2": [], "recurring": [], "multibatch": []}
    
    for post in posts:
        channel_id = post['channel_id']
        if channel_id not in posts_by_channel:
            posts_by_channel[channel_id] = []
        posts_by_channel[channel_id].append(post)
        
        # Group by mode (based on description patterns and recurring status)
        if post.get('is_recurring'):
            posts_by_mode["recurring"].append(post)
        elif post.get('description') and len(post['description']) > 50:
            posts_by_mode["mode2"].append(post)  # Mode 2 typically has custom descriptions
        elif post.get('batch_id'):
            posts_by_mode["multibatch"].append(post)
        else:
            posts_by_mode["mode1"].append(post)  # Mode 1 typically has auto descriptions or short ones
    
    # Build info text with channel and mode breakdown
    info_text = "*📋 Your scheduled posts:*\n\n"
    
    # Channel breakdown
    info_text += "*📺 By Channel:*\n"
    for channel_id, channel_posts in posts_by_channel.items():
        channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
        channel_name = channel['channel_name'] if channel else f"Unknown ({channel_id})"
        info_text += f"• {channel_name}: {len(channel_posts)} posts\n"
    
    # Mode breakdown
    info_text += "\n*📱 By Upload Mode:*\n"
    if posts_by_mode["mode1"]:
        info_text += f"• 📸 Mode 1 (Bulk): {len(posts_by_mode['mode1'])} posts\n"
    if posts_by_mode["mode2"]:
        info_text += f"• 📝 Mode 2 (Custom): {len(posts_by_mode['mode2'])} posts\n"
    if posts_by_mode["recurring"]:
        info_text += f"• 🔄 Recurring: {len(posts_by_mode['recurring'])} posts\n"
    if posts_by_mode["multibatch"]:
        info_text += f"• 🔧 Multi-batch: {len(posts_by_mode['multibatch'])} posts\n"
    
    # Create keyboard with all selection options
    keyboard = []
    
    # Option to edit all posts
    keyboard.append([InlineKeyboardButton(f"🔄 All Posts ({len(posts)})", callback_data="bulkedit_all")])
    
    # Mode-based options
    keyboard.append([InlineKeyboardButton("📱 Select by Upload Mode", callback_data="bulkedit_modes")])
    
    # Channel-based options  
    keyboard.append([InlineKeyboardButton("📺 Select by Channel", callback_data="bulkedit_channels")])
    
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="back_to_main")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = f"""
🔄 *Bulk Edit - Redistribute Posts*

{info_text}

*⏰ What this does:*
• Spreads posts evenly across time range
• Maintains channel assignments
• Preserves post content and descriptions

*Choose selection method:*
"""
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_bulk_edit_callback(query, user, data):
    """Handle bulk edit callback queries"""
    try:
        await query.answer()
    except Exception:
        pass  # Query might already be answered
        
    if data == "bulkedit_all":
        # Redistribute all scheduled posts
        posts = Database.get_scheduled_posts_for_channel(user.id)
        await prompt_bulk_edit_settings(query, user, posts, "All Posts")
    
    elif data == "bulkedit_modes":
        # Show mode selection menu
        await show_mode_selection_menu(query, user)
        
    elif data == "bulkedit_channels":
        # Show channel selection menu
        await show_channel_selection_menu(query, user)
    
    elif data.startswith("bulkedit_mode_"):
        if data.startswith("bulkedit_mode_all_"):
            # Redistribute all posts from specific mode
            mode = data.replace("bulkedit_mode_all_", "")
            await handle_mode_all_selection(query, user, mode)
        elif data.startswith("bulkedit_mode_channel_"):
            # Redistribute posts from specific mode and channel
            parts = data.replace("bulkedit_mode_channel_", "").split("_", 1)
            if len(parts) >= 2:
                mode = parts[0]
                channel_id = parts[1]
                await handle_mode_channel_selection(query, user, mode, channel_id)
        else:
            # Show channel selection for specific mode
            mode = data.replace("bulkedit_mode_", "")
            await handle_mode_selection(query, user, mode)
    
    elif data.startswith("bulkedit_channel_"):
        # Redistribute posts for specific channel
        channel_id = data.replace("bulkedit_channel_", "")
        posts = Database.get_scheduled_posts_for_channel(user.id, channel_id)
        
        # Get channel name
        channels = Database.get_user_channels(user.id)
        channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
        channel_name = channel['channel_name'] if channel else f"Channel {channel_id}"
        
        await prompt_bulk_edit_settings(query, user, posts, channel_name)
    
    elif data == "bulkedit_back":
        # Go back to main bulk edit menu - restart the bulkedit process  
        posts = Database.get_scheduled_posts_for_channel(user.id)
        channels = Database.get_user_channels(user.id)
        
        if not posts:
            await query.edit_message_text(
                "❌ *No scheduled posts found!*\n\n"
                "You need to have posts scheduled before you can bulk edit them.",
                parse_mode='Markdown'
            )
            return
        
        # Rebuild the main menu (same logic as bulkedit_handler)
        posts_by_channel = {}
        posts_by_mode = {"mode1": [], "mode2": [], "recurring": [], "multibatch": []}
        
        for post in posts:
            channel_id = post['channel_id']
            if channel_id not in posts_by_channel:
                posts_by_channel[channel_id] = []
            posts_by_channel[channel_id].append(post)
            
            # Group by mode
            if post.get('is_recurring'):
                posts_by_mode["recurring"].append(post)
            elif post.get('description') and len(post['description']) > 50:
                posts_by_mode["mode2"].append(post)
            elif post.get('batch_id'):
                posts_by_mode["multibatch"].append(post)
            else:
                posts_by_mode["mode1"].append(post)
        
        # Build info text
        info_text = "*📋 Your scheduled posts:*\n\n"
        info_text += "*📺 By Channel:*\n"
        for channel_id, channel_posts in posts_by_channel.items():
            channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
            channel_name = channel['channel_name'] if channel else f"Unknown ({channel_id})"
            info_text += f"• {channel_name}: {len(channel_posts)} posts\n"
        
        info_text += "\n*📱 By Upload Mode:*\n"
        if posts_by_mode["mode1"]:
            info_text += f"• 📸 Mode 1 (Bulk): {len(posts_by_mode['mode1'])} posts\n"
        if posts_by_mode["mode2"]:
            info_text += f"• 📝 Mode 2 (Custom): {len(posts_by_mode['mode2'])} posts\n"
        if posts_by_mode["recurring"]:
            info_text += f"• 🔄 Recurring: {len(posts_by_mode['recurring'])} posts\n"
        if posts_by_mode["multibatch"]:
            info_text += f"• 🔧 Multi-batch: {len(posts_by_mode['multibatch'])} posts\n"
        
        keyboard = [
            [InlineKeyboardButton(f"🔄 All Posts ({len(posts)})", callback_data="bulkedit_all")],
            [InlineKeyboardButton("📱 Select by Upload Mode", callback_data="bulkedit_modes")],
            [InlineKeyboardButton("📺 Select by Channel", callback_data="bulkedit_channels")],
            [InlineKeyboardButton("❌ Cancel", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message = f"""
🔄 *Bulk Edit - Redistribute Posts*

{info_text}

*⏰ What this does:*
• Spreads posts evenly across time range
• Maintains channel assignments
• Preserves post content and descriptions

*Choose selection method:*
"""
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def show_mode_selection_menu(query, user):
    """Show mode selection menu for bulk edit"""
    posts = Database.get_scheduled_posts_for_channel(user.id)
    
    # Group posts by mode
    posts_by_mode = {"mode1": [], "mode2": [], "recurring": [], "multibatch": []}
    
    for post in posts:
        if post.get('is_recurring'):
            posts_by_mode["recurring"].append(post)
        elif post.get('description') and len(post['description']) > 50:
            posts_by_mode["mode2"].append(post)
        elif post.get('batch_id'):
            posts_by_mode["multibatch"].append(post)
        else:
            posts_by_mode["mode1"].append(post)
    
    keyboard = []
    
    if posts_by_mode["mode1"]:
        keyboard.append([InlineKeyboardButton(
            f"📸 Mode 1 - Bulk Upload ({len(posts_by_mode['mode1'])} posts)", 
            callback_data="bulkedit_mode_mode1"
        )])
    
    if posts_by_mode["mode2"]:
        keyboard.append([InlineKeyboardButton(
            f"📝 Mode 2 - Custom Descriptions ({len(posts_by_mode['mode2'])} posts)", 
            callback_data="bulkedit_mode_mode2"
        )])
    
    if posts_by_mode["recurring"]:
        keyboard.append([InlineKeyboardButton(
            f"🔄 Recurring Posts ({len(posts_by_mode['recurring'])} posts)", 
            callback_data="bulkedit_mode_recurring"
        )])
    
    if posts_by_mode["multibatch"]:
        keyboard.append([InlineKeyboardButton(
            f"🔧 Multi-batch Posts ({len(posts_by_mode['multibatch'])} posts)", 
            callback_data="bulkedit_mode_multibatch"
        )])
    
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="bulkedit_back")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="back_to_main")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = """
🔄 *Select Posts by Upload Mode*

Choose which type of posts you want to redistribute:

*📸 Mode 1:* Bulk uploaded posts with auto descriptions
*📝 Mode 2:* Posts with custom descriptions  
*🔄 Recurring:* Automatically repeating posts
*🔧 Multi-batch:* Advanced batch scheduled posts
"""
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def show_channel_selection_menu(query, user):
    """Show channel selection menu for bulk edit"""
    posts = Database.get_scheduled_posts_for_channel(user.id)
    channels = Database.get_user_channels(user.id)
    
    # Group posts by channel
    posts_by_channel = {}
    for post in posts:
        channel_id = post['channel_id']
        if channel_id not in posts_by_channel:
            posts_by_channel[channel_id] = []
        posts_by_channel[channel_id].append(post)
    
    keyboard = []
    
    for channel_id, channel_posts in posts_by_channel.items():
        channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
        channel_name = channel['channel_name'] if channel else f"Channel {channel_id}"
        keyboard.append([InlineKeyboardButton(
            f"📺 {channel_name} ({len(channel_posts)} posts)", 
            callback_data=f"bulkedit_channel_{channel_id}"
        )])
    
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="bulkedit_back")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="back_to_main")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = """
🔄 *Select Posts by Channel*

Choose which channel's posts you want to redistribute:
"""
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_mode_selection(query, user, mode):
    """Handle selection of posts by mode - show channel options for this mode"""  
    try:
        await query.answer()
    except Exception:
        pass
        
    posts = Database.get_scheduled_posts_for_channel(user.id)
    channels = Database.get_user_channels(user.id)
    
    # Filter posts by mode
    filtered_posts = []
    mode_name = ""
    
    if mode == "mode1":
        mode_name = "Mode 1 (Bulk Upload)"
        for post in posts:
            if (not post.get('is_recurring') and 
                not post.get('batch_id') and 
                (not post.get('description') or len(post['description']) <= 50)):
                filtered_posts.append(post)
    
    elif mode == "mode2":
        mode_name = "Mode 2 (Custom Descriptions)"
        for post in posts:
            if (not post.get('is_recurring') and 
                not post.get('batch_id') and 
                post.get('description') and len(post['description']) > 50):
                filtered_posts.append(post)
    
    elif mode == "recurring":
        mode_name = "Recurring Posts"
        for post in posts:
            if post.get('is_recurring'):
                filtered_posts.append(post)
    
    elif mode == "multibatch":
        mode_name = "Multi-batch Posts"
        for post in posts:
            if post.get('batch_id'):
                filtered_posts.append(post)
    
    if not filtered_posts:
        await query.answer("❌ No posts found for this mode!", show_alert=True)
        return
    
    # Group the filtered posts by channel
    posts_by_channel = {}
    for post in filtered_posts:
        channel_id = post['channel_id']
        if channel_id not in posts_by_channel:
            posts_by_channel[channel_id] = []
        posts_by_channel[channel_id].append(post)
    
    # Create keyboard with channel options for this mode
    keyboard = []
    
    # Option to redistribute all posts from this mode across all channels
    keyboard.append([InlineKeyboardButton(
        f"🔄 All {mode_name} Posts ({len(filtered_posts)})", 
        callback_data=f"bulkedit_mode_all_{mode}"
    )])
    
    # Individual channel options for this mode
    for channel_id, channel_posts in posts_by_channel.items():
        channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
        channel_name = channel['channel_name'] if channel else f"Channel {channel_id}"
        keyboard.append([InlineKeyboardButton(
            f"📺 {channel_name} ({len(channel_posts)} {mode_name.split('(')[0].strip()} posts)", 
            callback_data=f"bulkedit_mode_channel_{mode}_{channel_id}"
        )])
    
    keyboard.append([InlineKeyboardButton("🔙 Back to Modes", callback_data="bulkedit_modes")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="back_to_main")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = f"""
🔄 *{mode_name} - Select Channel*

*📊 Total {mode_name.lower()} posts:* {len(filtered_posts)}

Choose which posts to redistribute:

*🔄 All:* Redistribute all {mode_name.lower()} posts across all channels
*📺 By Channel:* Redistribute {mode_name.lower()} posts from specific channels only
"""
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_mode_all_selection(query, user, mode):
    """Handle selection of all posts from a specific mode"""
    posts = Database.get_scheduled_posts_for_channel(user.id)
    
    # Filter posts by mode (same logic as handle_mode_selection)
    filtered_posts = []
    mode_name = ""
    
    if mode == "mode1":
        mode_name = "All Mode 1 (Bulk Upload) Posts"
        for post in posts:
            if (not post.get('is_recurring') and 
                not post.get('batch_id') and 
                (not post.get('description') or len(post['description']) <= 50)):
                filtered_posts.append(post)
    
    elif mode == "mode2":
        mode_name = "All Mode 2 (Custom Descriptions) Posts"
        for post in posts:
            if (not post.get('is_recurring') and 
                not post.get('batch_id') and 
                post.get('description') and len(post['description']) > 50):
                filtered_posts.append(post)
    
    elif mode == "recurring":
        mode_name = "All Recurring Posts"
        for post in posts:
            if post.get('is_recurring'):
                filtered_posts.append(post)
    
    elif mode == "multibatch":
        mode_name = "All Multi-batch Posts"
        for post in posts:
            if post.get('batch_id'):
                filtered_posts.append(post)
    
    if not filtered_posts:
        await query.answer("❌ No posts found for this mode!", show_alert=True)
        return
    
    await prompt_bulk_edit_settings(query, user, filtered_posts, mode_name)

async def handle_mode_channel_selection(query, user, mode, channel_id):
    """Handle selection of posts from specific mode and channel"""
    posts = Database.get_scheduled_posts_for_channel(user.id, channel_id)
    channels = Database.get_user_channels(user.id)
    
    # Filter posts by mode and channel
    filtered_posts = []
    mode_name = ""
    
    # Get channel name
    channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
    channel_name = channel['channel_name'] if channel else f"Channel {channel_id}"
    
    if mode == "mode1":
        mode_name = f"Mode 1 Posts from {channel_name}"
        for post in posts:
            if (not post.get('is_recurring') and 
                not post.get('batch_id') and 
                (not post.get('description') or len(post['description']) <= 50)):
                filtered_posts.append(post)
    
    elif mode == "mode2":
        mode_name = f"Mode 2 Posts from {channel_name}"
        for post in posts:
            if (not post.get('is_recurring') and 
                not post.get('batch_id') and 
                post.get('description') and len(post['description']) > 50):
                filtered_posts.append(post)
    
    elif mode == "recurring":
        mode_name = f"Recurring Posts from {channel_name}"
        for post in posts:
            if post.get('is_recurring'):
                filtered_posts.append(post)
    
    elif mode == "multibatch":
        mode_name = f"Multi-batch Posts from {channel_name}"
        for post in posts:
            if post.get('batch_id'):
                filtered_posts.append(post)
    
    if not filtered_posts:
        await query.answer("❌ No posts found for this mode and channel combination!", show_alert=True)
        return
    
    await prompt_bulk_edit_settings(query, user, filtered_posts, mode_name)

async def prompt_bulk_edit_settings(query, user, posts, scope_name):
    """Prompt user for bulk edit time range settings"""
    Database.update_user_session(user.id, BotStates.WAITING_BULK_EDIT_INPUT, {
        'posts': [post['id'] for post in posts],
        'scope': scope_name
    })
    
    # Get current scheduling config as default
    start_hour, end_hour, interval_hours = Database.get_scheduling_config(user.id)
    
    message = f"""
⏰ *Bulk Edit: {scope_name}*

*📊 Posts to redistribute:* {len(posts)}

*Enter your schedule parameters:*
`start_hour end_hour [interval] [YYYY-MM-DD]`

*Examples:*
• `10 20` - 10 AM to 8 PM, auto intervals (starting tomorrow)
• `10 20 2` - 10 AM to 8 PM, every 2 hours (starting tomorrow)
• `10 20 2025-07-25` - 10 AM to 8 PM, auto intervals, July 25th
• `10 20 2 2025-07-25` - 10 AM to 8 PM, every 2 hours, July 25th
• `9 18 1` - 9 AM to 6 PM, every 1 hour (starting tomorrow)

*Current default:* `{start_hour} {end_hour} {interval_hours}`

*⚡ How it works:*
• Auto intervals: Posts spread evenly across time range
• Fixed intervals: Posts every X hours within range
• If no date specified, starts tomorrow
• Times are in Kyiv timezone
"""
    
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="back_to_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_bulk_edit_input(update: Update, user, text: str, session_data: dict):
    """Handle bulk edit time range, interval, and date input"""
    valid, start_hour, end_hour, interval_hours, channel_id, start_date, message = parse_bulk_edit_input(text)
    
    if not valid:
        await update.message.reply_text(f"❌ {message}\n\nPlease try again:")
        return
    
    # Get posts from session
    post_ids = session_data.get('posts', [])
    scope_name = session_data.get('scope', 'Posts')
    
    if not post_ids:
        await update.message.reply_text("❌ No posts found in session. Please start over.")
        Database.update_user_session(user.id, BotStates.IDLE)
        return
    
    # Get posts details
    posts = Database.get_scheduled_posts_for_channel(user.id)
    posts_to_update = [post for post in posts if post['id'] in post_ids]
    
    if not posts_to_update:
        await update.message.reply_text("❌ No valid posts found to update.")
        Database.update_user_session(user.id, BotStates.IDLE)
        return
    
    # Calculate new evenly distributed schedule
    interval_to_use = interval_hours if interval_hours > 0 else None
    new_schedule_times = calculate_evenly_distributed_schedule(start_hour, end_hour, len(posts_to_update), start_date, interval_to_use)
    
    # Prepare updates
    post_schedule_updates = []
    for i, post in enumerate(posts_to_update):
        if i < len(new_schedule_times):
            post_schedule_updates.append((post['id'], new_schedule_times[i]))
    
    # Execute bulk update
    updated_count = Database.bulk_update_post_schedules(post_schedule_updates)
    
    if updated_count > 0:
        # Update scheduler jobs
        try:
            scheduler = PostScheduler()
            # Cancel existing jobs for these posts
            for post in posts_to_update:
                await scheduler.cancel_post_job(post['id'])
            
            # Create new scheduled jobs
            for post_id, new_time in post_schedule_updates:
                await scheduler.schedule_single_post(post_id, new_time)
                
        except Exception as e:
            logger.error(f"Error updating scheduler jobs: {e}")
    
    # Generate preview of new schedule
    preview_text = "\n*📅 New Schedule Preview:*\n"
    for i, (post_id, new_time) in enumerate(post_schedule_updates[:5]):  # Show first 5
        time_str = new_time.strftime("%Y-%m-%d %H:%M")
        preview_text += f"• Post #{post_id}: {time_str}\n"
    
    if len(post_schedule_updates) > 5:
        preview_text += f"... and {len(post_schedule_updates) - 5} more posts\n"
    
    # Create success message with date and interval info
    date_info = "starting tomorrow" if start_date is None else f"starting {start_date.strftime('%Y-%m-%d')}"
    interval_info = "auto intervals" if interval_hours == 0 or interval_hours is None else f"every {interval_hours} hour(s)"
    
    success_message = f"""
✅ *Bulk Edit Complete!*

*📊 Scope:* {scope_name}
*⏰ Time Range:* {start_hour}:00 - {end_hour}:00 (Kyiv time)
*⏱️ Interval:* {interval_info}
*📅 Start Date:* {date_info.title()}
*📝 Posts Updated:* {updated_count} of {len(posts_to_update)}

{preview_text}

*🎯 Result:* Posts are now distributed with {interval_info} across your time window, {date_info}.
"""
    
    keyboard = [
        [InlineKeyboardButton("📅 View Calendar", callback_data="main_calendar")],
        [InlineKeyboardButton("📊 View Statistics", callback_data="main_stats")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(success_message, reply_markup=reply_markup, parse_mode='Markdown')
    
    # Reset user session
    Database.update_user_session(user.id, BotStates.IDLE)
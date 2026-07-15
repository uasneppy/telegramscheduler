"""
Telegram bot handlers for different commands and interactions
"""

import os
import json
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from .database import Database
from .scheduler import PostScheduler
from .caption_recovery import handle_recover_captions_command, handle_recover_captions_interactive
from .utils import (
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

# Scheduler will be accessed from application context

async def extract_and_save_media(update: Update, user_id: int, media_type: str) -> str:
    """Extract media from update message and save to disk"""
    message = update.message
    
    if media_type == 'photo':
        if not message.photo:
            return None
        media_file = message.photo[-1]
        filename = generate_unique_filename(f"photo_{media_file.file_id}.jpg")
    elif media_type == 'video':
        if not message.video:
            return None
        media_file = message.video
        filename = generate_unique_filename(f"video_{media_file.file_id}.mp4")
    elif media_type == 'audio':
        if not message.audio:
            return None
        media_file = message.audio
        filename = generate_unique_filename(f"audio_{media_file.file_id}.mp3")
    elif media_type == 'animation':
        if not message.animation:
            return None
        media_file = message.animation
        filename = generate_unique_filename(f"animation_{media_file.file_id}.gif")
    elif media_type in ['document', 'document_image', 'document_video']:
        if not message.document:
            return None
        media_file = message.document
        original_name = media_file.file_name or f"document_{media_file.file_id}"
        filename = generate_unique_filename(original_name)
        media_type = 'document'  # Normalize for storage
    else:
        return None
    
    file = await media_file.get_file()
    file_data = await file.download_as_bytearray()
    file_path = save_media(bytes(file_data), filename, media_type, user_id)
    
    return file_path


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    if not update or not update.effective_user or not update.message:
        logger.error("Invalid update in start_handler")
        return
    
    user = update.effective_user
    
    # Create main menu keyboard
    keyboard = [
        [InlineKeyboardButton("📸 Mode 1: Bulk Upload", callback_data="main_mode1")],
        [InlineKeyboardButton("📝 Mode 2: Individual Upload", callback_data="main_mode2")],
        [InlineKeyboardButton("🎯 Mode 3: Guided Captioning", callback_data="main_mode3")],
        [InlineKeyboardButton("🔄 Recurring Posts", callback_data="main_recurring")],
        [InlineKeyboardButton("👁️ Preview Posts", callback_data="main_preview")],
        [InlineKeyboardButton("📅 Calendar View", callback_data="main_calendar")],
        [InlineKeyboardButton("⏰ Manage Overdue", callback_data="main_overdue")],
        [InlineKeyboardButton("✏️ Edit Posts", callback_data="main_editposts")],
        [InlineKeyboardButton("🔄 Manage Recurring", callback_data="recurring_manage_menu")],
        [InlineKeyboardButton("🔁 Reschedule All", callback_data="main_reschedule")],
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
• *Quality preservation:* Send as documents for uncompressed media

*💡 For uncompressed media:* Send images/videos as documents
*🕐 Default Schedule:* 10 AM to 8 PM, every 2 hours (Kyiv time)

Choose an option below to get started:
"""
    
    await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode='Markdown')
    
    # Reset user session
    Database.update_user_session(user.id, BotStates.IDLE)

async def mode1_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /mode1 command - bulk photo upload"""
    if not update or not update.effective_user or not update.message:
        logger.error("Invalid update in mode1_handler")
        return
    
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
    
    # Send quality tip after channel selection
    await update.message.reply_text(
        "💡 *Quality Tip:* For uncompressed media that preserves original file size and quality, "
        "send your images and videos as documents instead of photos/videos.",
        parse_mode='Markdown'
    )

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
    # SECURITY CHECK: Verify user owns the channel before proceeding
    if not Database.user_has_channel(user.id, channel_id):
        logger.error(f"Security violation: User {user.id} attempted to access channel {channel_id} for recurring mode")
        await query.edit_message_text(
            "❌ *Security Error*\n\nYou don't have access to this channel.",
            parse_mode='Markdown'
        )
        return
    
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
    if not update or not update.effective_user or not update.message:
        logger.error("Invalid update in mode2_handler")
        return
    
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
    
    # Send quality tip after channel selection
    await update.message.reply_text(
        "💡 *Quality Tip:* For uncompressed media that preserves original file size and quality, "
        "send your images and videos as documents instead of photos/videos.",
        parse_mode='Markdown'
    )

async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle media uploads, media groups, and text messages"""
    # Add null safety checks
    if not update or not update.effective_user or not update.message:
        logger.error("Invalid update object received in media_handler")
        return
        
    user = update.effective_user
    mode, session_data = Database.get_user_session(user.id)
    
    logger.info(f"Media handler called - User: {user.id}, Mode: {mode}")
    
    # Check if this is part of a media group (album)
    media_group_id = update.message.media_group_id
    
    if media_group_id and mode == BotStates.MODE2_PHOTOS:
        logger.info(f"Processing media group {media_group_id} for user {user.id}")
        await handle_media_group(update, context, user, mode, session_data, media_group_id)
    # PRIORITY: Documents first (uncompressed media) before other media types
    elif update.message.document:
        logger.info(f"Processing document upload for user {user.id} (uncompressed)")
        # Check if document is an image, video, or other media type
        document = update.message.document
        mime_type = document.mime_type or ''
        
        if mime_type.startswith('image/'):
            logger.info(f"Uncompressed image document detected for user {user.id}")
            await handle_media_upload(update, context, user, mode, session_data, 'document_image')
        elif mime_type.startswith('video/'):
            logger.info(f"Uncompressed video document detected for user {user.id}")
            await handle_media_upload(update, context, user, mode, session_data, 'document_video')
        else:
            await handle_media_upload(update, context, user, mode, session_data, 'document')
    elif update.message.photo:
        logger.info(f"Processing photo upload for user {user.id} (compressed by Telegram)")
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
    
    # Handle media replacement in edit posts menu
    if mode == BotStates.EDIT_POST_MEDIA:
        logger.info(f"Handling media replacement for user {user.id}")
        try:
            file_path = await extract_and_save_media(update, user.id, media_type)
            if file_path:
                handled = await handle_editposts_media_input(update, user, file_path, media_type, session_data)
                if handled:
                    return
        except Exception as e:
            logger.error(f"Error handling media replacement: {e}", exc_info=True)
            await update.message.reply_text(f"Error replacing media: {e}")
        return
    
    # Handle Mode 3 uploading phase
    if mode == BotStates.MODE3_UPLOADING:
        logger.info(f"Handling Mode 3 media upload for user {user.id}")
        try:
            file_path = await extract_and_save_media(update, user.id, media_type)
            if file_path:
                await handle_mode3_media_upload(update, user, file_path, media_type, session_data)
                return
        except Exception as e:
            logger.error(f"Error handling Mode 3 media upload: {e}", exc_info=True)
            await update.message.reply_text(f"Error uploading media: {e}")
        return
    
    if mode not in [BotStates.MODE1_PHOTOS, BotStates.MODE2_PHOTOS, BotStates.RECURRING_MODE]:
        logger.warning(f"Invalid mode for media upload: {mode} for user {user.id}")
        if update.message:
            await update.message.reply_text(
                "Please start with /mode1, /mode2, /mode3, recurring posts, or /multibatch first to upload media."
            )
        return
    
    try:
        logger.info(f"Starting media processing for user {user.id}, type: {media_type}")
        
        # Get the media file based on type
        if media_type == 'photo':
            if not update.message.photo:
                logger.error(f"No photo found in message for user {user.id}")
                return
            media_file = update.message.photo[-1]  # Get largest photo (still compressed by Telegram)
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
        elif media_type in ['document', 'document_image', 'document_video']:
            if not update.message.document:
                logger.error(f"No document found in message for user {user.id}")
                return
            media_file = update.message.document
            # Preserve original filename and extension for uncompressed media
            original_filename = f"uncompressed_{media_file.file_id}_{media_file.file_name or 'file'}"
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
        
        # Stream download and save media (optimized for heavy files)
        from bot.utils import save_media_streaming
        
        try:
            file_path = await save_media_streaming(file, filename, media_type, user.id)
            logger.info(f"Streamed media to: {file_path} for user {user.id}")
        except Exception as e:
            logger.error(f"Streaming failed, falling back to byte array download: {e}")
            # Fallback to traditional method for smaller files or if streaming fails
            file_data = await file.download_as_bytearray()
            logger.info(f"Downloaded file data ({len(file_data)} bytes) for user {user.id}")
            file_path = save_media(bytes(file_data), filename, media_type, user.id)
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
        
        # Preserve user progress - don't clear session or existing uploads
        error_message = f"❌ Error processing this {media_type}: {str(e)}"
        
        # Add helpful context based on error type
        if "File is too big" in str(e):
            error_message += "\n\n💡 This file exceeds Telegram's limits (50MB for documents, 20MB for photos/videos)."
            error_message += "\n\n✅ Your previous uploads are safe - continue with other files or proceed to scheduling."
        elif "Invalid" in str(e):
            error_message += "\n\n💡 File format may be corrupted or unsupported."
            error_message += "\n\n✅ Your previous uploads are safe - try another file or continue."
        else:
            error_message += "\n\n✅ Your previous uploads are safe - you can continue with other files or proceed to scheduling."
        
        # Show current progress and next steps based on mode
        if mode == BotStates.MODE1_PHOTOS:
            # Show current Mode 1 progress
            current_uploads = Database.get_pending_posts(user.id, channel_id=session_data.get('selected_channel_id'), unscheduled_only=True)
            if current_uploads:
                media_summary = {}
                for post in current_uploads:
                    media_type_key = post['media_type']
                    media_summary[media_type_key] = media_summary.get(media_type_key, 0) + 1
                
                error_message += f"\n\n*Current uploads ({len(current_uploads)} files):*\n"
                for media_type_key, count in media_summary.items():
                    icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type_key, '📁')
                    error_message += f"• {icon} {count} {media_type_key}{'s' if count > 1 else ''}\n"
            
            error_message += "\n\n📤 Continue uploading more files or use /schedule when ready."
            
        elif mode == BotStates.MODE2_PHOTOS:
            # Show current Mode 2 progress
            current_uploads = Database.get_pending_posts(user.id, channel_id=session_data.get('selected_channel_id'), unscheduled_only=True)
            if current_uploads:
                error_message += f"\n\n*Current uploads ({len(current_uploads)} files ready):*"
                for i, post in enumerate(current_uploads[-3:], 1):  # Show last 3
                    icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(post['media_type'], '📁')
                    desc_preview = post['description'][:30] + "..." if post['description'] and len(post['description']) > 30 else post['description'] or "No description"
                    desc_preview = escape_markdown(desc_preview)
                    error_message += f"\n• {icon} {desc_preview}"
            
            error_message += "\n\n📤 Continue uploading more files or use /schedule when ready."
            
        elif mode == BotStates.RECURRING_MODE:
            error_message += "\n\n📤 Try uploading a different file for your recurring post."
        
        if update.message:
            await update.message.reply_text(error_message, parse_mode='Markdown')

# Keep backward compatibility
async def handle_photo_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                            user, mode: str, session_data: dict):
    """Handle photo upload based on current mode (backward compatibility)"""
    await handle_media_upload(update, context, user, mode, session_data, 'photo')

async def handle_mode1_media(update: Update, user, file_path: str, media_type: str, session_data: dict):
    """Handle media upload in Mode 1 (bulk)"""
    
    # Get the selected channel from session data
    selected_channel_id = session_data.get('selected_channel_id')
    
    # DEFENSE IN DEPTH: Verify user still owns the channel from session data
    if selected_channel_id and not Database.user_has_channel(user.id, selected_channel_id):
        logger.error(f"Security violation: User {user.id} session contains channel {selected_channel_id} they don't own")
        await update.message.reply_text("❌ Security Error: Invalid channel in session. Please restart with /mode1.")
        return
    
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
    
    # Show progress with current uploads
    media_summary = {}
    for item in media_items:
        media_type_key = item['media_type']
        media_summary[media_type_key] = media_summary.get(media_type_key, 0) + 1
    
    # Add quality indicator for display
    quality_text = ""
    if media_type in ['document_image', 'document_video']:
        quality_text = " (uncompressed)"
    elif media_type == 'photo':
        quality_text = " (compressed)"
    
    progress_text = f"✅ {media_icon} {media_type.replace('document_', '').title()}{quality_text} uploaded! ({len(media_items)} total)\n\n"
    progress_text += "*Current uploads:*\n"
    for media_type_key, count in media_summary.items():
        icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type_key, '📁')
        progress_text += f"• {icon} {count} {media_type_key}{'s' if count > 1 else ''}\n"
    
    progress_text += "\n📤 Continue uploading more files or use /schedule when ready."
    
    await update.message.reply_text(
        progress_text,
        parse_mode='Markdown'
    )

# Keep backward compatibility
async def handle_mode1_photo(update: Update, user, file_path: str, session_data: dict):
    """Handle photo upload in Mode 1 (bulk) - backward compatibility"""
    await handle_mode1_media(update, user, file_path, 'photo', session_data)

async def handle_mode2_media(update: Update, user, file_path: str, media_type: str, session_data: dict):
    """Handle media upload in Mode 2 (individual) - instant save with caption"""
    import json
    
    # Get caption from the message (if any)
    caption = update.message.caption if update.message.caption else None
    
    # Get caption entities (formatting like bold, italic, etc.) from Telegram's native formatting
    caption_entities = update.message.caption_entities
    caption_entities_json = None
    if caption_entities:
        # Serialize entities to JSON for storage
        caption_entities_json = json.dumps([
            {'type': e.type, 'offset': e.offset, 'length': e.length, 
             'url': e.url, 'user': e.user.id if e.user else None, 'language': e.language}
            for e in caption_entities
        ])
    
    # Debug logging for caption handling
    logger.info(f"Mode 2 media upload: User {user.id}, caption='{caption}', entities={len(caption_entities) if caption_entities else 0}")
    
    # Get the selected channel from session data
    selected_channel_id = session_data.get('selected_channel_id')
    
    if not selected_channel_id:
        await update.message.reply_text("❌ No channel selected. Please use /mode2 to start again.")
        return
    
    # DEFENSE IN DEPTH: Verify user still owns the channel from session data
    if not Database.user_has_channel(user.id, selected_channel_id):
        logger.error(f"Security violation: User {user.id} session contains channel {selected_channel_id} they don't own")
        await update.message.reply_text("❌ Security Error: Invalid channel in session. Please restart with /mode2.")
        return
    
    # Save media instantly to database with caption entities for native formatting support
    post_id = Database.add_post(user.id, file_path, media_type=media_type, description=caption, mode=2, 
                                 channel_id=selected_channel_id, caption_entities=caption_entities_json)
    
    # Update session data with saved media
    media_items = session_data.get('media_items', [])
    media_items.append({
        'post_id': post_id,
        'file_path': file_path,
        'media_type': media_type,
        'description': caption,
        'uploaded_at': datetime.now().isoformat()
    })
    
    session_data['media_items'] = media_items
    # Keep user in MODE2_PHOTOS state for continued uploads
    Database.update_user_session(user.id, BotStates.MODE2_PHOTOS, session_data)
    
    # Format media type for display
    media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
    # Escape markdown in user-provided caption to prevent parsing errors
    escaped_caption = escape_markdown(caption) if caption else None
    desc_text = f'"{escaped_caption}"' if escaped_caption else "no caption"
    
    # Show comprehensive progress for Mode 2
    media_summary = {}
    for item in media_items:
        media_type_key = item['media_type']
        media_summary[media_type_key] = media_summary.get(media_type_key, 0) + 1
    
    progress_text = f"✅ {media_icon} {media_type.title()} saved with {desc_text}! ({len(media_items)} total)\n\n"
    progress_text += "*Ready to schedule:*\n"
    for media_type_key, count in media_summary.items():
        icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type_key, '📁')
        progress_text += f"• {icon} {count} {media_type_key}{'s' if count > 1 else ''}\n"
    
    progress_text += f"\n📤 Send more media or use /schedule when ready."
    
    await update.message.reply_text(
        progress_text,
        parse_mode='Markdown'
    )

# Keep backward compatibility
async def handle_mode2_photo(update: Update, user, file_path: str, session_data: dict):
    """Handle photo upload in Mode 2 (individual) - backward compatibility"""
    await handle_mode2_media(update, user, file_path, 'photo', session_data)

async def handle_media_group(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                           user, mode: str, session_data: dict, media_group_id: str):
    """Handle media group (album) uploads in Mode 2"""
    
    # Get or initialize media group tracking
    if 'media_groups' not in context.user_data:
        context.user_data['media_groups'] = {}
    
    # Track this message in the media group
    if media_group_id not in context.user_data['media_groups']:
        context.user_data['media_groups'][media_group_id] = {
            'messages': [],
            'processed': False
        }
    
    # Add this message to the group
    context.user_data['media_groups'][media_group_id]['messages'].append(update.message)
    
    # Set a delay to process the complete group (Telegram sends group messages quickly)
    if not context.user_data['media_groups'][media_group_id]['processed']:
        # Schedule processing after a short delay to collect all messages
        import asyncio
        asyncio.create_task(process_media_group_delayed(
            context, user, mode, session_data, media_group_id, 1.0
        ))
        context.user_data['media_groups'][media_group_id]['processed'] = True

async def process_media_group_delayed(context, user, mode: str, session_data: dict, 
                                    media_group_id: str, delay: float):
    """Process a complete media group after delay - create single unified post"""
    await asyncio.sleep(delay)
    
    if 'media_groups' not in context.user_data:
        return
        
    group_data = context.user_data['media_groups'].get(media_group_id)
    if not group_data:
        return
    
    messages = group_data['messages']
    if not messages:
        return
    
    logger.info(f"Processing media group {media_group_id} with {len(messages)} items for user {user.id}")
    
    # Get the selected channel from session data
    selected_channel_id = session_data.get('selected_channel_id')
    
    if not selected_channel_id:
        # Send error to the first message in the group
        await messages[0].reply_text("❌ No channel selected. Please use /mode2 to start again.")
        return
    
    # Validate album constraints
    if len(messages) > 10:
        await messages[0].reply_text(
            "❌ *Album too large!*\n\n"
            f"Telegram albums support maximum 10 items, but you sent {len(messages)}.\n"
            "Please send 10 or fewer media files together.",
            parse_mode='Markdown'
        )
        del context.user_data['media_groups'][media_group_id]
        return
    
    media_bundle = []
    media_summary = {}
    unsupported_types = []
    
    # Process each message in the group
    for message in messages:
        try:
            # Determine media type and get media file
            media_file = None
            media_type = None
            original_filename = None
            
            if message.photo:
                media_file = message.photo[-1]
                media_type = 'photo'
                original_filename = f"photo_{media_file.file_id}.jpg"
            elif message.video:
                media_file = message.video
                media_type = 'video'
                original_filename = f"video_{media_file.file_id}.mp4"
            elif message.document and message.document.mime_type:
                # Check if document is image or video
                mime_type = message.document.mime_type
                if mime_type.startswith('image/'):
                    media_file = message.document
                    media_type = 'document_image'
                    original_filename = f"doc_img_{media_file.file_id}_{media_file.file_name or 'image'}"
                elif mime_type.startswith('video/'):
                    media_file = message.document
                    media_type = 'document_video'
                    original_filename = f"doc_vid_{media_file.file_id}_{media_file.file_name or 'video'}"
                else:
                    unsupported_types.append('document')
                    continue
            else:
                unsupported_types.append('other')
                continue
            
            if not media_file:
                logger.warning(f"No supported media found in group message for user {user.id}")
                continue
            
            # Download and save the media
            file = await context.bot.get_file(media_file.file_id)
            filename = generate_unique_filename(original_filename)
            
            # Use streaming download for efficiency
            try:
                from bot.utils import save_media_streaming
                file_path = await save_media_streaming(file, filename, media_type)
            except Exception as e:
                logger.error(f"Streaming failed, falling back to byte array download: {e}")
                file_data = await file.download_as_bytearray()
                file_path = save_media(bytes(file_data), filename, media_type)
            
            # Add to media bundle
            media_bundle.append({
                'file_path': file_path,
                'media_type': media_type,
                'original_caption': message.caption if message.caption else None
            })
            
            # Update media summary for display
            display_type = media_type.replace('document_', '') if media_type.startswith('document_') else media_type
            media_summary[display_type] = media_summary.get(display_type, 0) + 1
            
            logger.info(f"Added {media_type} to album bundle: {file_path}")
            
        except Exception as e:
            logger.error(f"Error processing media group item for user {user.id}: {e}")
    
    if not media_bundle:
        await messages[0].reply_text(
            "❌ No supported media found in album.\n\n"
            "Albums support photos and videos only.",
            parse_mode='Markdown'
        )
        del context.user_data['media_groups'][media_group_id]
        return
    
    # Show warning for unsupported types
    if unsupported_types:
        await messages[0].reply_text(
            "⚠️ *Some files skipped*\n\n"
            "Albums only support photos and videos. Other file types are saved separately.\n"
            "Processed photos and videos as one album below.",
            parse_mode='Markdown'
        )
    
    # Create single album post in database
    import json
    media_bundle_json = json.dumps(media_bundle)
    
    # Use the first file as the primary file_path for compatibility
    primary_file_path = media_bundle[0]['file_path']
    
    post_id = Database.add_post(
        user.id, 
        primary_file_path, 
        media_type='album', 
        description=None,  # Will be set when user provides caption
        mode=2, 
        channel_id=selected_channel_id,
        media_bundle_json=media_bundle_json
    )
    
    # Store album post in session for caption handling
    session_data['pending_album_post_id'] = post_id
    session_data['pending_album_items'] = len(media_bundle)
    
    # Update session data with the album info
    media_items = session_data.get('media_items', [])
    media_items.append({
        'post_id': post_id,
        'file_path': primary_file_path,
        'media_type': 'album',
        'description': None,  # Pending caption
        'uploaded_at': datetime.now().isoformat(),
        'album_size': len(media_bundle)
    })
    
    session_data['media_items'] = media_items
    Database.update_user_session(user.id, BotStates.MODE2_PHOTOS, session_data)
    
    # Send confirmation message
    progress_text = f"✅ *Album created!* {len(media_bundle)} items ready to post as one\n\n"
    progress_text += "*Album contents:*\n"
    for media_type_key, count in media_summary.items():
        icon = {'photo': '📸', 'video': '🎥', 'image': '📸', 'video': '🎥'}.get(media_type_key, '📁')
        progress_text += f"• {icon} {count} {media_type_key}{'s' if count > 1 else ''}\n"
    
    progress_text += f"\n💬 *Send a caption for this album* or use /schedule to post without caption."
    
    # Reply to the first message in the group
    await messages[0].reply_text(progress_text, parse_mode='Markdown')
    
    # Clean up the group tracking
    del context.user_data['media_groups'][media_group_id]

async def handle_album_caption_input(update: Update, user, text: str, session_data: dict):
    """Handle caption input for album posts"""
    
    pending_album_post_id = session_data.get('pending_album_post_id')
    album_items_count = session_data.get('pending_album_items', 0)
    
    if not pending_album_post_id:
        logger.error(f"No pending album post found for user {user.id}")
        return
    
    # Validate caption length (Telegram limit is 1024 characters)
    if len(text) > 1024:
        await update.message.reply_text(
            "❌ *Caption too long!*\n\n"
            f"Telegram captions support maximum 1024 characters.\n"
            f"Your caption is {len(text)} characters.\n\n"
            "Please send a shorter caption.",
            parse_mode='Markdown'
        )
        return
    
    # Update the album post with the caption
    conn = Database.get_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        UPDATE posts 
        SET description = ?
        WHERE id = ? AND user_id = ?
    ''', (text, pending_album_post_id, user.id))
    
    conn.commit()
    conn.close()
    
    # Clear pending album from session
    session_data.pop('pending_album_post_id', None)
    session_data.pop('pending_album_items', None)
    
    # Update the media_items in session to reflect the caption
    media_items = session_data.get('media_items', [])
    for item in media_items:
        if item.get('post_id') == pending_album_post_id:
            item['description'] = text
            break
    
    session_data['media_items'] = media_items
    Database.update_user_session(user.id, BotStates.MODE2_PHOTOS, session_data)
    
    logger.info(f"Updated album post {pending_album_post_id} with caption for user {user.id}")
    
    # Send confirmation - escape user-provided caption to prevent Markdown parsing errors
    escaped_text = escape_markdown(text)
    await update.message.reply_text(
        f"✅ *Album caption saved!*\n\n"
        f'📝 Caption: "{escaped_text}"\n'
        f"📱 Album size: {album_items_count} items\n\n"
        f"📤 Send more media or use /schedule when ready.",
        parse_mode='Markdown'
    )

async def preview_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /preview command - show post previews"""
    user = update.effective_user
    
    # Check if user has pending posts
    pending_posts = Database.get_pending_posts(user.id, unscheduled_only=False)
    
    if not pending_posts:
        await update.message.reply_text(
            "📭 No posts found for preview.\n\n"
            "Upload some media first using /mode1 or /mode2, then you can preview them here.",
            parse_mode='Markdown'
        )
        return
    
    # Set user to preview mode
    Database.update_user_session(user.id, BotStates.PREVIEW_MODE)
    
    # Show first post preview
    await show_post_preview(update, user.id, 0, pending_posts)

async def show_post_preview(update_or_query, user_id: int, post_index: int, posts_list: list):
    """Show a single post preview with navigation and editing options"""
    
    if post_index < 0 or post_index >= len(posts_list):
        # Handle out of bounds
        post_index = 0 if post_index < 0 else len(posts_list) - 1
    
    post = posts_list[post_index]
    
    # Format post details
    media_icon = get_media_icon(post['media_type'])
    description_text = f'"{post["description"]}"' if post.get('description') else "_No caption_"
    
    # Get channel name
    channel_name = "Unknown"
    if post.get('channel_id'):
        channels = Database.get_user_channels(user_id)
        for channel in channels:
            if channel['channel_id'] == post['channel_id']:
                channel_name = channel['channel_name']
                break
    
    # Format scheduled time
    if post.get('scheduled_time'):
        scheduled_text = f"📅 Scheduled: {post['scheduled_time'].strftime('%Y-%m-%d %H:%M')} (Kyiv)"
    else:
        scheduled_text = "⏰ Not scheduled yet"
    
    preview_text = f"""
👁️ *Post Preview* ({post_index + 1}/{len(posts_list)})

{media_icon} *Media:* {post['media_type'].title()}
📺 *Channel:* {channel_name}
📝 *Caption:* {description_text}
{scheduled_text}

*Post ID:* #{post['id']}
"""
    
    # Create navigation and editing keyboard
    keyboard = []
    
    # Navigation row
    nav_row = []
    if post_index > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"preview_nav_{post_index-1}"))
    if post_index < len(posts_list) - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"preview_nav_{post_index+1}"))
    if nav_row:
        keyboard.append(nav_row)
    
    # Editing options
    keyboard.extend([
        [InlineKeyboardButton("✏️ Edit Caption", callback_data=f"edit_caption_{post['id']}")],
        [InlineKeyboardButton("🗑️ Delete Post", callback_data=f"delete_post_{post['id']}")],
        [InlineKeyboardButton("📤 Send Preview", callback_data=f"send_preview_{post['id']}")],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_preview_{post_index}")],
        [InlineKeyboardButton("🏠 Back to Menu", callback_data="main_menu")]
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Send or edit message
    if hasattr(update_or_query, 'message'):
        # It's an Update object
        await update_or_query.message.reply_text(
            preview_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        # It's a CallbackQuery object
        try:
            await update_or_query.edit_message_text(
                preview_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        except Exception:
            # If edit fails, send new message
            await update_or_query.message.reply_text(
                preview_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )

async def send_post_preview_to_user(update_or_query, post_id: int, user_id: int):
    """Send the actual media preview to user"""
    
    # Get post details
    conn = Database.get_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT file_path, media_type, description
        FROM posts 
        WHERE id = ? AND user_id = ?
    ''', (post_id, user_id))
    
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        await update_or_query.answer("❌ Post not found!", show_alert=True)
        return
    
    file_path, media_type, description = row
    
    if not os.path.exists(file_path):
        await update_or_query.answer("❌ Media file not found!", show_alert=True)
        return
    
    try:
        # Send the actual media as preview
        with open(file_path, 'rb') as media_file:
            if media_type == 'photo':
                await update_or_query.message.reply_photo(
                    photo=media_file,
                    caption=f"🔍 *Preview*\n\n{description or 'No caption'}" if description else "🔍 *Preview*\n\nNo caption",
                    parse_mode='Markdown'
                )
            elif media_type == 'video':
                await update_or_query.message.reply_video(
                    video=media_file,
                    caption=f"🔍 *Preview*\n\n{description or 'No caption'}" if description else "🔍 *Preview*\n\nNo caption",
                    parse_mode='Markdown'
                )
            elif media_type == 'audio':
                await update_or_query.message.reply_audio(
                    audio=media_file,
                    caption=f"🔍 *Preview*\n\n{description or 'No caption'}" if description else "🔍 *Preview*\n\nNo caption",
                    parse_mode='Markdown'
                )
            elif media_type == 'animation':
                await update_or_query.message.reply_animation(
                    animation=media_file,
                    caption=f"🔍 *Preview*\n\n{description or 'No caption'}" if description else "🔍 *Preview*\n\nNo caption",
                    parse_mode='Markdown'
                )
            elif media_type == 'document':
                await update_or_query.message.reply_document(
                    document=media_file,
                    caption=f"🔍 *Preview*\n\n{description or 'No caption'}" if description else "🔍 *Preview*\n\nNo caption",
                    parse_mode='Markdown'
                )
        
        await update_or_query.answer("✅ Preview sent!")
        
    except Exception as e:
        logger.error(f"Error sending preview for post {post_id}: {e}")
        await update_or_query.answer("❌ Error sending preview!", show_alert=True)

async def handle_caption_edit_input(update: Update, user, text: str, session_data: dict):
    """Handle caption editing input"""
    
    post_id = session_data.get('editing_post_id')
    if not post_id:
        await update.message.reply_text("❌ No post being edited. Please start again.")
        return
    
    # Update post caption in database
    conn = Database.get_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        UPDATE posts 
        SET description = ? 
        WHERE id = ? AND user_id = ?
    ''', (text.strip(), post_id, user.id))
    
    conn.commit()
    conn.close()
    
    await update.message.reply_text(
        f"✅ Caption updated successfully!\n\n"
        f"*New caption:* {text}\n\n"
        f"Use /preview to see your updated post.",
        parse_mode='Markdown'
    )
    
    # Reset user session
    Database.update_user_session(user.id, BotStates.IDLE)

async def delete_post_from_preview(post_id: int, user_id: int):
    """Delete a post from preview"""
    
    # Get post details for cleanup
    conn = Database.get_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT file_path FROM posts 
        WHERE id = ? AND user_id = ?
    ''', (post_id, user_id))
    
    row = cursor.fetchone()
    
    if row:
        file_path = row[0]
        
        # Delete the post from database
        cursor.execute('DELETE FROM posts WHERE id = ? AND user_id = ?', (post_id, user_id))
        conn.commit()
        
        # Try to delete the file
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"Deleted media file: {file_path}")
        except Exception as e:
            logger.error(f"Error deleting file {file_path}: {e}")
    
    conn.close()
    return row is not None

async def handle_preview_navigation(query, user, post_index: int):
    """Handle preview navigation callbacks"""
    
    # Get updated posts list
    pending_posts = Database.get_pending_posts(user.id, unscheduled_only=False)
    
    if not pending_posts:
        await query.edit_message_text("📭 No posts found for preview.")
        return
    
    # Show the requested post
    await show_post_preview(query, user.id, post_index, pending_posts)

async def handle_edit_caption_callback(query, user, post_id: int):
    """Handle edit caption callback"""
    
    # Set user session to caption editing mode
    session_data = {'editing_post_id': post_id}
    Database.update_user_session(user.id, BotStates.WAITING_CAPTION_EDIT, session_data)
    
    await query.edit_message_text(
        f"✏️ *Edit Caption*\n\n"
        f"Send me the new caption for post #{post_id}.\n"
        f"The current caption will be completely replaced.",
        parse_mode='Markdown'
    )

async def handle_delete_post_callback(query, user, post_id: int):
    """Handle delete post callback"""
    
    # Delete the post
    deleted = await delete_post_from_preview(post_id, user.id)
    
    if deleted:
        await query.edit_message_text(
            f"🗑️ *Post Deleted*\n\n"
            f"Post #{post_id} has been deleted successfully.\n\n"
            f"Use /preview to see your remaining posts.",
            parse_mode='Markdown'
        )
    else:
        await query.edit_message_text(
            f"❌ *Delete Failed*\n\n"
            f"Could not delete post #{post_id}. It may have already been removed.",
            parse_mode='Markdown'
        )

async def main_preview_handler(query, user):
    """Handle preview posts from main menu - show channel selection first"""
    
    # Check if user has any posts at all
    all_posts = Database.get_pending_posts(user.id, unscheduled_only=False)
    
    if not all_posts:
        await query.edit_message_text(
            "📭 *No posts found for preview.*\n\n"
            "Upload some media first using Mode 1 or Mode 2, then you can preview them here.\n\n"
            "Click the button below to go back to the main menu.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back to Menu", callback_data="main_menu")]]),
            parse_mode='Markdown'
        )
        return
    
    # Get user's channels
    channels = Database.get_user_channels(user.id)
    
    if not channels:
        await query.edit_message_text(
            "❌ *No channels configured!*\n\n"
            "Please add a channel first using /channels command before previewing posts.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back to Menu", callback_data="main_menu")]]),
            parse_mode='Markdown'
        )
        return
    
    # Show channel selection for preview
    await show_preview_channel_selection(query, user, channels)

async def show_preview_channel_selection(query, user, channels):
    """Show channel selection for preview posts"""
    keyboard = []
    
    # Add option to see all posts across all channels
    keyboard.append([InlineKeyboardButton("📺 All Channels", callback_data="preview_channel_all")])
    
    # Add each channel
    for channel in channels:
        channel_id, channel_name = channel['channel_id'], channel['channel_name']
        
        # Get post count for this channel
        channel_posts = Database.get_pending_posts(user.id, channel_id=channel_id, unscheduled_only=False)
        post_count = len(channel_posts)
        
        if post_count > 0:
            display_text = f"📺 {channel_name} ({post_count} posts)"
            if len(display_text) > 35:
                display_text = f"📺 {channel_name[:30]}... ({post_count})"
        else:
            display_text = f"📺 {channel_name} (0 posts)"
        
        callback_data = f"preview_channel_{channel_id}"
        keyboard.append([InlineKeyboardButton(display_text, callback_data=callback_data)])
    
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = """
👁️ *Preview Posts*

Select which channel's posts you want to preview:

*Options:*
• **All Channels** - See posts from all your channels
• **Specific Channel** - See posts from one channel only

Choose a channel below:
"""
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_preview_channel_selection(query, user, channel_selection):
    """Handle channel selection for preview posts"""
    if channel_selection == "all":
        # Show all posts across all channels
        pending_posts = Database.get_pending_posts(user.id, unscheduled_only=False)
        channel_name = "All Channels"
    else:
        # Show posts for specific channel
        channel_id = channel_selection
        pending_posts = Database.get_pending_posts(user.id, channel_id=channel_id, unscheduled_only=False)
        
        # Get channel name for display
        channels = Database.get_user_channels(user.id)
        channel_name = next((ch['channel_name'] for ch in channels if ch['channel_id'] == channel_id), "Unknown Channel")
    
    if not pending_posts:
        await query.edit_message_text(
            f"📭 *No posts found for {channel_name}.*\n\n"
            "Upload some media for this channel first using Mode 1 or Mode 2.\n\n"
            "Click the button below to go back to channel selection.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Choose Another Channel", callback_data="main_preview")],
                [InlineKeyboardButton("🏠 Back to Menu", callback_data="back_to_main")]
            ]),
            parse_mode='Markdown'
        )
        return
    
    # Set user to preview mode
    Database.update_user_session(user.id, BotStates.PREVIEW_MODE, {'selected_channel': channel_selection})
    
    # Show first post preview with channel context
    await show_post_preview_for_channel(query, user.id, 0, pending_posts, channel_name, channel_selection)

async def show_post_preview_for_channel(query, user_id: int, post_index: int, posts_list: list, channel_name: str, channel_selection: str):
    """Show a single post preview with navigation and editing options for a specific channel"""
    
    if post_index < 0 or post_index >= len(posts_list):
        # Handle out of bounds
        post_index = 0 if post_index < 0 else len(posts_list) - 1
    
    post = posts_list[post_index]
    
    # Format post details
    media_icon = get_media_icon(post['media_type'])
    description_text = f'"{post["description"]}"' if post.get('description') else "_No caption_"
    
    # Format scheduled time
    if post.get('scheduled_time'):
        scheduled_text = f"📅 Scheduled: {post['scheduled_time'].strftime('%Y-%m-%d %H:%M')} (Kyiv)"
    else:
        scheduled_text = "⏰ Not scheduled yet"
    
    preview_text = f"""
👁️ *Post Preview* ({post_index + 1}/{len(posts_list)})
🔍 *Viewing:* {channel_name}

{media_icon} *Media:* {post['media_type'].title()}
📝 *Caption:* {description_text}
{scheduled_text}

*Post ID:* #{post['id']}
"""
    
    # Create navigation and editing keyboard
    keyboard = []
    
    # Navigation row
    nav_row = []
    if post_index > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"preview_nav_channel_{channel_selection}_{post_index-1}"))
    if post_index < len(posts_list) - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"preview_nav_channel_{channel_selection}_{post_index+1}"))
    if nav_row:
        keyboard.append(nav_row)
    
    # Editing options
    keyboard.extend([
        [InlineKeyboardButton("✏️ Edit Caption", callback_data=f"edit_caption_{post['id']}")],
        [InlineKeyboardButton("🗑️ Delete Post", callback_data=f"delete_post_{post['id']}")],
        [InlineKeyboardButton("📤 Send Preview", callback_data=f"send_preview_{post['id']}")],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_preview_channel_{channel_selection}_{post_index}")],
        [InlineKeyboardButton("📺 Choose Channel", callback_data="main_preview")],
        [InlineKeyboardButton("🏠 Back to Menu", callback_data="back_to_main")]
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(preview_text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_preview_navigation_for_channel(query, user, channel_selection, post_index):
    """Handle navigation for channel-specific preview posts"""
    if channel_selection == "all":
        # Show all posts across all channels
        pending_posts = Database.get_pending_posts(user.id, unscheduled_only=False)
        channel_name = "All Channels"
    else:
        # Show posts for specific channel
        channel_id = channel_selection
        pending_posts = Database.get_pending_posts(user.id, channel_id=channel_id, unscheduled_only=False)
        
        # Get channel name for display
        channels = Database.get_user_channels(user.id)
        channel_name = next((ch['channel_name'] for ch in channels if ch['channel_id'] == channel_id), "Unknown Channel")
    
    if not pending_posts:
        await query.edit_message_text(
            f"📭 *No posts found for {channel_name}.*\n\n"
            "Upload some media for this channel first using Mode 1 or Mode 2.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Choose Another Channel", callback_data="main_preview")],
                [InlineKeyboardButton("🏠 Back to Menu", callback_data="back_to_main")]
            ]),
            parse_mode='Markdown'
        )
        return
    
    # Show the requested post
    await show_post_preview_for_channel(query, user.id, post_index, pending_posts, channel_name, channel_selection)

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
        description=final_description, 
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
    
    # MODE2_DESCRIPTION is no longer used since we do instant saving with captions
    # However, we need to handle album captions in MODE2_PHOTOS state
    if mode == BotStates.MODE2_PHOTOS and session_data.get('pending_album_post_id'):
        await handle_album_caption_input(update, user, text, session_data)
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
    elif mode == "waiting_recurring_start_time":
        handled = await handle_recurring_start_time_input(update, user, text, session_data)
        if handled:
            return
    elif mode == 'RECURRING_EDIT_CAPTION':
        handled = await handle_recurring_caption_input(update, user, text, session_data)
        if handled:
            return
    elif mode == 'RECURRING_EDIT_END_DATE':
        handled = await handle_recurring_end_date_input(update, user, text, session_data)
        if handled:
            return
    elif mode == BotStates.WAITING_BULK_EDIT_INPUT:
        await handle_bulk_edit_input(update, user, text, session_data)
    elif mode == "waiting_backup_name":
        await handle_backup_name_input(update, user, text)
    elif mode == "awaiting_reschedule_settings":
        await handle_reschedule_settings_input(update, user, text, context)
    elif mode == BotStates.WAITING_CAPTION_EDIT:
        await handle_caption_edit_input(update, user, text, session_data)
    elif mode == "awaiting_caption_input":
        await handle_new_caption_input(update, user, text, session_data)
    elif mode in [BotStates.EDIT_POST_CAPTION, BotStates.EDIT_POST_SCHEDULE]:
        # Handle comprehensive post editing inputs
        handled = await handle_editposts_input(update, user, session_data, context)
        if not handled:
            await update.message.reply_text(
                "I'm not sure what to do with this message. Use /help for available commands."
            )
    elif mode == BotStates.MODE3_CAPTIONING:
        # Handle Mode 3 caption input
        await handle_mode3_caption_input(update, user, session_data, context)
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
    
    # Show comprehensive progress for Mode 2
    media_summary = {}
    for item in media_items:
        media_type_key = item['media_type']
        media_summary[media_type_key] = media_summary.get(media_type_key, 0) + 1
    
    progress_text = f"✅ {media_icon} {media_type.title()} saved with {desc_text}! ({len(media_items)} total)\n\n"
    progress_text += "*Ready to schedule:*\n"
    for media_type_key, count in media_summary.items():
        icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type_key, '📁')
        progress_text += f"• {icon} {count} {media_type_key}{'s' if count > 1 else ''}\n"
    
    progress_text += f"\n📤 Continue uploading or use /schedule when ready."
    
    await update.message.reply_text(
        progress_text,
        parse_mode='Markdown'
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
    keyboard.append([InlineKeyboardButton("⏭️ Next Available Slot", callback_data="schedule_next_slot")])
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
    elif data == "schedule_next_slot":
        await execute_next_slot_scheduling(query, user, context)
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
        # Reset user state when going back to main menu
        Database.update_user_session(user.id, BotStates.IDLE)
        await show_main_menu(query, user)
    elif data.startswith("help_"):
        await handle_help_callback(query, user, data)
    elif data.startswith("recurring_channel_"):
        channel_id = data.replace("recurring_channel_", "")
        await handle_recurring_channel_selection(query, user, channel_id)
    elif data.startswith("recurring_"):
        await handle_recurring_callback(query, user, data, context)
    elif data.startswith("clearqueue_"):
        await handle_clearqueue_callback(query, user, data)
    elif data.startswith("clearscheduled_"):
        await handle_clearscheduled_callback(query, user, data)
    elif data.startswith("batch_mode"):
        await handle_batch_mode_callback(query, user, data)
    elif data.startswith("batch_"):
        await handle_batch_callback(query, user, data)
    elif data.startswith("retry_"):
        await handle_retry_callback(query, user, data)
    elif data.startswith("reschedule_"):
        await handle_reschedule_action_callback(query, user, data, context)
    elif data.startswith("mode1_channel_") or data.startswith("mode2_channel_") or data.startswith("mode3_channel_"):
        # Parse the mode and channel from the callback data
        parts = data.split("_", 2)  # Split into max 3 parts: mode, "channel", channel_id
        if len(parts) >= 3:
            mode = int(parts[0].replace("mode", ""))
            channel_id = parts[2]  # The channel ID (could contain underscores)
            await handle_mode_channel_selection(query, user, mode, channel_id)
        else:
            await query.edit_message_text("❌ Invalid selection.")
    elif data == "mode3_done_uploading":
        await handle_mode3_done_uploading(query, user)
    elif data == "mode3_skip_caption":
        await handle_mode3_skip_caption(query, user)
    elif data == "mode3_schedule":
        await handle_mode3_schedule(query, user)
    elif data == "mode3_cancel":
        await handle_mode3_cancel(query, user)
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
    elif data.startswith("backup_") or data == "backup_menu":
        await handle_backup_callback(query, user, data)
    elif data.startswith("restore_"):
        await handle_restore_callback(query, user, data)
    elif data.startswith("overdue_"):
        await handle_overdue_callback(query, user, data)
    elif data.startswith("preview_nav_channel_"):
        # Handle channel-specific navigation: preview_nav_channel_{channel_id}_{post_index}
        parts = data.replace("preview_nav_channel_", "").split("_")
        if len(parts) >= 2:
            channel_selection = "_".join(parts[:-1])  # Channel ID might contain underscores
            post_index = int(parts[-1])
            await handle_preview_navigation_for_channel(query, user, channel_selection, post_index)
    elif data.startswith("preview_nav_"):
        post_index = int(data.replace("preview_nav_", ""))
        await handle_preview_navigation(query, user, post_index)
    elif data.startswith("preview_channel_"):
        channel_selection = data.replace("preview_channel_", "")
        await handle_preview_channel_selection(query, user, channel_selection)
    elif data.startswith("edit_caption_"):
        post_id = int(data.replace("edit_caption_", ""))
        await handle_edit_caption_callback(query, user, post_id)
    elif data.startswith("delete_post_"):
        post_id = int(data.replace("delete_post_", ""))
        await handle_delete_post_callback(query, user, post_id)
    elif data.startswith("send_preview_"):
        post_id = int(data.replace("send_preview_", ""))
        await send_post_preview_to_user(query, post_id, user.id)
    elif data.startswith("refresh_preview_channel_"):
        # Handle channel-specific refresh: refresh_preview_channel_{channel_id}_{post_index}
        parts = data.replace("refresh_preview_channel_", "").split("_")
        if len(parts) >= 2:
            channel_selection = "_".join(parts[:-1])  # Channel ID might contain underscores
            post_index = int(parts[-1])
            await handle_preview_navigation_for_channel(query, user, channel_selection, post_index)
    elif data.startswith("refresh_preview_"):
        post_index = int(data.replace("refresh_preview_", ""))
        await handle_preview_navigation(query, user, post_index)
    elif data.startswith("settings_"):
        await handle_settings_callback(query, user, data)
    elif data.startswith("delete_captions_"):
        await handle_delete_captions_callback(query, user, data)
    elif data.startswith("edit_captions_"):
        await handle_edit_captions_callback(query, user, data)
    # Comprehensive edit posts menu callbacks
    elif data == "editposts_menu":
        await show_editposts_menu(query, user)
    elif data.startswith("editposts_channel_"):
        channel_id = data.replace("editposts_channel_", "")
        await handle_editposts_channel_selection(query, user, channel_id)
    elif data.startswith("editposts_nav_"):
        new_index = int(data.replace("editposts_nav_", ""))
        await handle_editposts_navigation(query, user, new_index)
    elif data == "editposts_info":
        await query.answer("Navigate using Prev/Next buttons")
    elif data.startswith("editposts_preview_"):
        post_id = int(data.replace("editposts_preview_", ""))
        await handle_editposts_preview(query, user, post_id)
    elif data.startswith("editposts_caption_"):
        post_id = int(data.replace("editposts_caption_", ""))
        await handle_editposts_caption(query, user, post_id)
    elif data.startswith("editposts_media_"):
        post_id = int(data.replace("editposts_media_", ""))
        await handle_editposts_media(query, user, post_id)
    elif data.startswith("editposts_schedule_"):
        post_id = int(data.replace("editposts_schedule_", ""))
        await handle_editposts_schedule(query, user, post_id)
    elif data.startswith("editposts_schedquick_"):
        parts = data.replace("editposts_schedquick_", "").split("_")
        post_id = int(parts[0])
        hours = int(parts[1])
        await handle_editposts_schedule_quick(query, user, post_id, hours, context)
    elif data.startswith("editposts_schedcustom_"):
        post_id = int(data.replace("editposts_schedcustom_", ""))
        await handle_editposts_schedule_custom(query, user, post_id)
    elif data.startswith("editposts_delete_"):
        post_id = int(data.replace("editposts_delete_", ""))
        await handle_editposts_delete(query, user, post_id)
    elif data.startswith("editposts_confirmdelete_"):
        post_id = int(data.replace("editposts_confirmdelete_", ""))
        await handle_editposts_confirm_delete(query, user, post_id, context)
    elif data.startswith("editposts_cancel_"):
        post_id = int(data.replace("editposts_cancel_", ""))
        await handle_editposts_cancel(query, user, post_id)
    # Recurring posts management callbacks
    elif data == "recurring_manage_menu":
        await show_recurring_posts_menu(query, user)
    elif data.startswith("recur_manage_ch_"):
        channel_id = data.replace("recur_manage_ch_", "")
        await handle_recurring_channel_posts(query, user, channel_id)
    elif data.startswith("recur_nav_"):
        new_index = int(data.replace("recur_nav_", ""))
        await handle_recurring_navigation(query, user, new_index)
    elif data.startswith("recur_preview_"):
        post_id = int(data.replace("recur_preview_", ""))
        await handle_recurring_preview(query, user, post_id)
    elif data.startswith("recur_editcap_"):
        post_id = int(data.replace("recur_editcap_", ""))
        await handle_recurring_edit_caption(query, user, post_id)
    elif data.startswith("recur_editint_"):
        post_id = int(data.replace("recur_editint_", ""))
        await handle_recurring_edit_interval(query, user, post_id)
    elif data.startswith("recur_setint_"):
        parts = data.replace("recur_setint_", "").split("_")
        post_id = int(parts[0])
        interval = int(parts[1])
        await handle_recurring_set_interval(query, user, post_id, interval)
    elif data.startswith("recur_editend_"):
        post_id = int(data.replace("recur_editend_", ""))
        await handle_recurring_edit_end(query, user, post_id)
    elif data.startswith("recur_setend_"):
        parts = data.replace("recur_setend_", "").split("_", 1)
        post_id = int(parts[0])
        end_type = parts[1] if len(parts) > 1 else "never"
        await handle_recurring_set_end(query, user, post_id, end_type, context)
    elif data.startswith("recur_delete_"):
        post_id = int(data.replace("recur_delete_", ""))
        await handle_recurring_delete(query, user, post_id)
    elif data.startswith("recur_confirmdel_"):
        post_id = int(data.replace("recur_confirmdel_", ""))
        await handle_recurring_confirm_delete(query, user, post_id, context)
    elif data.startswith("recur_back_"):
        post_id = int(data.replace("recur_back_", ""))
        await handle_recurring_back_to_post(query, user, post_id)
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
    total_posts = len(post_ids)
    
    # For large batches, show progress message first
    if total_posts > 20:
        await query.edit_message_text(
            f"⏳ *Scheduling {total_posts} posts...*\n\n"
            f"This may take a moment. Please wait.",
            parse_mode='Markdown'
        )
    
    # First, save all scheduled times to database (fast, reliable)
    conn = Database.get_connection()
    cursor = conn.cursor()
    for post_id, scheduled_time in zip(post_ids, schedule_times):
        cursor.execute(
            'UPDATE posts SET scheduled_time = ? WHERE id = ?',
            (scheduled_time.isoformat(), post_id)
        )
    conn.commit()
    conn.close()
    logger.info(f"Saved {total_posts} post schedule times to database")
    
    # Then try to register with APScheduler (monitoring will catch any missed ones)
    if context and context.application and context.application.bot_data:
        scheduler = context.application.bot_data.get('scheduler')
        if scheduler:
            try:
                await scheduler.schedule_posts(post_ids, schedule_times)
                logger.info(f"Successfully registered {total_posts} posts with scheduler")
            except Exception as e:
                logger.warning(f"Scheduler registration partial/failed: {e} - monitor will handle remaining")
    
    # Build summary message showing channels
    channel_summary = ""
    for channel_id, posts in posts_by_channel.items():
        channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
        channel_name = channel['channel_name'] if channel else f"Unknown ({channel_id})"
        channel_summary += f"• *{channel_name}*: {len(posts)} posts\n"
    
    summary_text = format_schedule_summary(schedule_times)
    
    # Ensure message doesn't exceed Telegram's limit
    msg = (
        f"✅ *Successfully scheduled {total_posts} posts!*\n\n"
        f"*Channels:*\n{channel_summary}\n"
        f"*Schedule:*\n{summary_text}\n"
        f"You'll receive notifications when each post is published."
    )
    
    if len(msg) > 4000:
        msg = (
            f"✅ *Successfully scheduled {total_posts} posts!*\n\n"
            f"*Channels:*\n{channel_summary}\n"
            f"*First post:* {schedule_times[0].strftime('%Y-%m-%d %I:%M %p')}\n"
            f"*Last post:* {schedule_times[-1].strftime('%Y-%m-%d %I:%M %p')}\n\n"
            f"You'll receive notifications when each post is published."
        )
    
    try:
        await query.edit_message_text(msg, parse_mode='Markdown')
    except Exception:
        await query.message.reply_text(msg, parse_mode='Markdown')
    
    # Clear only the posts that were just scheduled (channel-specific clearing)
    # Get the unique channels that were scheduled
    scheduled_channels = set(post['channel_id'] for post in pending_posts)
    for channel_id in scheduled_channels:
        Database.clear_queued_posts(user.id, channel_id)
    
    # Reset user session
    Database.update_user_session(user.id, BotStates.IDLE)

async def execute_next_slot_scheduling(query, user, context=None):
    """Execute scheduling starting from the next available slot while respecting default schedule hours"""
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
    
    # Find the latest scheduled post time to start from there
    from bot.utils import get_current_kyiv_time
    from datetime import timedelta
    
    now = get_current_kyiv_time()
    
    # Get the latest scheduled post time for this user
    latest_scheduled_time = Database.get_latest_scheduled_time(user.id)
    
    if latest_scheduled_time:
        # Start from after the latest scheduled post
        # Ensure timezone compatibility
        if latest_scheduled_time.tzinfo is None:
            from bot.utils import get_kyiv_timezone
            kyiv_tz = get_kyiv_timezone()
            latest_scheduled_time = kyiv_tz.localize(latest_scheduled_time)
        
        # Calculate next slot after the latest scheduled post
        start_date = latest_scheduled_time + timedelta(hours=interval_hours)
        
        # If the next slot is outside the schedule window, move to next day's start
        if start_date.hour < start_hour or start_date.hour >= end_hour:
            # Move to next valid day at start_hour
            next_day = start_date.replace(hour=start_hour, minute=0, second=0, microsecond=0)
            while next_day <= start_date:
                next_day += timedelta(days=1)
            start_date = next_day.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        else:
            # Align to schedule grid (start_hour + n * interval_hours)
            hours_from_start = start_date.hour - start_hour
            aligned_offset = ((hours_from_start + interval_hours - 1) // interval_hours) * interval_hours
            aligned_hour = start_hour + aligned_offset
            
            if aligned_hour >= end_hour:
                # Move to next day
                start_date = start_date.replace(hour=start_hour, minute=0, second=0, microsecond=0) + timedelta(days=1)
            else:
                start_date = start_date.replace(hour=aligned_hour, minute=0, second=0, microsecond=0)
        
        # CRITICAL: If start_date is still in the past, fall through to current time logic
        if start_date <= now:
            logger.info(f"Calculated start_date {start_date} is in the past, using current time logic")
            latest_scheduled_time = None  # Force fallback to current time logic
    
    if not latest_scheduled_time:
        # No scheduled posts yet, use current time logic
        current_hour = now.hour
        current_minute = now.minute
        
        # Determine the starting date and hour
        if current_hour >= end_hour or (current_hour == end_hour - 1 and current_minute > 0):
            # Past today's schedule window, start tomorrow
            start_date = now.replace(hour=start_hour, minute=0, second=0, microsecond=0) + timedelta(days=1)
        elif current_hour < start_hour:
            # Before today's schedule window, start today at start_hour
            start_date = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        else:
            # Within today's schedule window, find next slot
            # Round up to the next interval
            hours_since_start = current_hour - start_hour
            next_slot_offset = ((hours_since_start // interval_hours) + 1) * interval_hours
            next_hour = start_hour + next_slot_offset
            
            if next_hour >= end_hour:
                # Next slot would be past today's window, start tomorrow
                start_date = now.replace(hour=start_hour, minute=0, second=0, microsecond=0) + timedelta(days=1)
            else:
                # Use the next available slot today
                start_date = now.replace(hour=next_hour, minute=0, second=0, microsecond=0)
    
    # Calculate schedule times from the determined start date
    from bot.utils import calculate_schedule_times
    schedule_times = calculate_schedule_times(start_hour, end_hour, interval_hours, len(pending_posts), start_date)
    
    # Schedule all posts
    post_ids = [post['id'] for post in pending_posts]
    total_posts = len(post_ids)
    
    logger.info(f"execute_next_slot_scheduling: Attempting to schedule {total_posts} posts")
    
    # For large batches, show progress message first
    if total_posts > 20:
        await query.edit_message_text(
            f"⏳ *Scheduling {total_posts} posts...*\n\n"
            f"This may take a moment. Please wait.",
            parse_mode='Markdown'
        )
    
    # First, save all scheduled times to database (fast, reliable)
    conn = Database.get_connection()
    cursor = conn.cursor()
    for post_id, scheduled_time in zip(post_ids, schedule_times):
        cursor.execute(
            'UPDATE posts SET scheduled_time = ? WHERE id = ?',
            (scheduled_time.isoformat(), post_id)
        )
    conn.commit()
    conn.close()
    logger.info(f"Saved {total_posts} post schedule times to database (next slot)")
    
    # Then try to register with APScheduler (monitoring will catch any missed ones)
    if context and context.application and context.application.bot_data:
        scheduler = context.application.bot_data.get('scheduler')
        if scheduler:
            try:
                await scheduler.schedule_posts(post_ids, schedule_times)
                logger.info(f"Successfully registered {total_posts} posts with scheduler (next slot)")
            except Exception as e:
                logger.warning(f"Scheduler registration partial/failed: {e} - monitor will handle remaining")
    
    # Build summary message showing channels
    channel_summary = ""
    for channel_id, posts in posts_by_channel.items():
        channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
        channel_name = channel['channel_name'] if channel else f"Unknown ({channel_id})"
        channel_summary += f"• *{channel_name}*: {len(posts)} posts\n"
    
    # Format first post time for clarity
    first_time_str = schedule_times[0].strftime("%B %d at %H:%M")
    
    # Create a more informative message about the scheduling strategy
    if latest_scheduled_time:
        latest_str = latest_scheduled_time.strftime("%B %d at %H:%M")
        strategy_msg = f"⏭️ *Starting after your last scheduled post:*\n" \
                      f"Last scheduled: {latest_str}\n" \
                      f"Next available: {first_time_str} (Kyiv time)\n\n"
    else:
        strategy_msg = f"⏭️ *Starting from next available slot:*\n" \
                      f"First post: {first_time_str} (Kyiv time)\n\n"
    
    summary_text = format_schedule_summary(schedule_times)
    
    msg = (
        f"✅ *Successfully scheduled {total_posts} posts!*\n\n"
        f"{strategy_msg}"
        f"*Channels:*\n{channel_summary}\n"
        f"*Schedule:*\n{summary_text}\n"
        f"You'll receive notifications when each post is published."
    )
    
    if len(msg) > 4000:
        msg = (
            f"✅ *Successfully scheduled {total_posts} posts!*\n\n"
            f"{strategy_msg}"
            f"*Channels:*\n{channel_summary}\n"
            f"*First post:* {schedule_times[0].strftime('%Y-%m-%d %I:%M %p')}\n"
            f"*Last post:* {schedule_times[-1].strftime('%Y-%m-%d %I:%M %p')}\n\n"
            f"You'll receive notifications when each post is published."
        )
    
    try:
        await query.edit_message_text(msg, parse_mode='Markdown')
    except Exception:
        await query.message.reply_text(msg, parse_mode='Markdown')
    
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
    
    # Enforce default schedule window constraints for custom dates
    default_start, default_end, default_interval = Database.get_scheduling_config(user.id)
    start_hour = start_datetime.hour
    
    if start_hour < default_start or start_hour >= default_end:
        await update.message.reply_text(
            f"❌ *Schedule window violation!*\n\n"
            f"Your custom time: {start_datetime.strftime('%H:%M')}\n"
            f"Your default window: {default_start}:00 - {default_end}:00\n\n"
            f"*Custom schedules must respect your default window.*\n"
            f"Please use a time within your configured range, or use /schedule to change your defaults.\n\n"
            f"Try again:",
            parse_mode='Markdown'
        )
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
        
        # Enforce default schedule window constraints
        default_start, default_end, default_interval = Database.get_scheduling_config(user.id)
        scheduled_hour = scheduled_dt.hour
        
        if scheduled_hour < default_start or scheduled_hour >= default_end:
            await update.message.reply_text(
                f"❌ *Schedule window violation!*\n\n"
                f"Your time: {scheduled_dt.strftime('%H:%M')}\n"
                f"Your default window: {default_start}:00 - {default_end}:00\n\n"
                f"*All schedules must respect your default window.*\n"
                f"Please use a time within your configured range, or use /schedule to change your defaults.",
                parse_mode='Markdown'
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
            # Try to get scheduler from context if available
            if context and context.application and context.application.bot_data:
                scheduler = context.application.bot_data.get('scheduler')
            if not scheduler:
                from .scheduler import PostScheduler
                scheduler = PostScheduler()
                logger.warning("Using fallback scheduler instance - jobs may not persist")
        
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
• `/recover_captions` - Automatically recover lost captions from chat history
• `/recover_interactive` - Interactive caption recovery with manual input
• `/delete_all_captions` - Remove all captions from all your posts
• `/edit_captions` - Edit captions for scheduled posts one by one
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

        if not channel_id:
            logger.error(f"Channel ID missing from session for user {user.id} during channel name entry")
            await update.message.reply_text(
                "❌ Channel ID missing from session. Please restart the channel setup with /channels."
            )
            Database.update_user_session(user.id, BotStates.IDLE)
            return

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
        
        # SECURITY CHECK: Verify user owns the channel before removal
        if not Database.user_has_channel(user.id, channel_id):
            logger.error(f"Security violation: User {user.id} attempted to remove channel {channel_id} they don't own")
            await query.edit_message_text(
                "❌ *Security Error*\n\nYou don't have permission to remove this channel.",
                parse_mode='Markdown'
            )
            return
        
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
    
    # Handle "main_menu" specifically - return to main menu
    if action == "menu":
        logger.info(f"Processing main_menu (from handle_main_menu_callback) for user {user.id}")
        Database.update_user_session(user.id, BotStates.IDLE)
        await show_main_menu(query, user)
        return
    
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
    
    elif action == "mode3":
        # Check if user has channels configured
        channels = Database.get_user_channels(user.id)
        
        if not channels:
            await query.edit_message_text(
                "❌ *No channels configured!*\n\n"
                "Please add a channel first using /channels command before using Mode 3.",
                parse_mode='Markdown'
            )
            return
        
        # Always ask user to select a channel
        await prompt_channel_selection_for_mode_inline(query, user.id, channels, mode=3)
        
    elif action == "channels":
        # Show channels management
        await channels_handler_inline(query, user)
        
    elif action == "stats":
        # Show statistics
        await stats_handler_inline(query, user)
        
    elif action == "reschedule":
        # Show reschedule options
        await handle_reschedule_callback(query, user)
        
    elif action == "help":
        # Show help
        await help_handler_inline(query, user)
        
    elif action == "recurring":
        # Start Recurring Mode
        await recurring_mode_handler(query, user)
        
    elif action == "calendar":
        # Show calendar view
        await calendar_view_handler(query, user)
        
    elif action == "overdue":
        # Show overdue posts management
        await handle_main_overdue_callback(query, user)
    
    elif action == "preview":
        # Show post previews
        await main_preview_handler(query, user)
    
    elif action == "editposts":
        # Show edit posts menu
        await show_editposts_menu(query, user)

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
    # SECURITY CHECK: Verify user owns the channel before showing details
    if not Database.user_has_channel(user.id, channel_id):
        logger.error(f"Security violation: User {user.id} attempted to view stats for channel {channel_id} they don't own")
        await query.edit_message_text(
            "❌ *Security Error*\n\nYou don't have access to this channel.",
            parse_mode='Markdown'
        )
        return
    
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
                
                # Show full description without truncation, escaped for Markdown
                desc = escape_markdown(post['description'] or "No description")
                
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
        
        # SECURITY CHECK: Verify user owns the channel before editing posts
        if not Database.user_has_channel(user.id, channel_id):
            logger.error(f"Security violation: User {user.id} attempted to edit posts for channel {channel_id} they don't own")
            await query.edit_message_text(
                "❌ *Security Error*\n\nYou don't have access to this channel.",
                parse_mode='Markdown'
            )
            return
        
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
                
                # Truncate description if too long, escaped for Markdown
                raw_desc = post['description'][:25] + "..." if post['description'] and len(post['description']) > 25 else post['description'] or "No description"
                desc = escape_markdown(raw_desc)
                
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
        [InlineKeyboardButton("🎯 Mode 3: Guided Captioning", callback_data="main_mode3")],
        [InlineKeyboardButton("🔄 Recurring Posts", callback_data="main_recurring")],
        [InlineKeyboardButton("👁️ Preview Posts", callback_data="main_preview")],
        [InlineKeyboardButton("📅 Calendar View", callback_data="main_calendar")],
        [InlineKeyboardButton("⏰ Manage Overdue", callback_data="main_overdue")],
        [InlineKeyboardButton("✏️ Edit Posts", callback_data="main_editposts")],
        [InlineKeyboardButton("🔄 Manage Recurring", callback_data="recurring_manage_menu")],
        [InlineKeyboardButton("🔁 Reschedule All", callback_data="main_reschedule")],
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
• *Quality preservation:* Send as documents for uncompressed media

*💡 For uncompressed media:* Send images/videos as documents
*🕐 Default Schedule:* 10 AM to 8 PM, every 2 hours (Kyiv time)

Choose an option below:
"""
    
    try:
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        logger.info(f"Main menu successfully displayed for user {user.id}")
    except Exception as e:
        logger.error(f"Failed to edit message in show_main_menu for user {user.id}: {e}")
        # Try sending a new message instead
        try:
            await query.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')
            logger.info(f"Sent new main menu message for user {user.id}")
        except Exception as e2:
            logger.error(f"Failed to send new message in show_main_menu for user {user.id}: {e2}")
            raise e2

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
        [InlineKeyboardButton("🗑 Delete a Post", callback_data=f"help_delete_post|{channel_id}|0")],
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
        description = escape_markdown(post['description'] or 'No description')

        # Add recurring indicator
        recurring_indicator = " 🔄" if post['is_recurring'] else ""

        message += f"{i}. {media_icon} *{date_str} {time_str}*{recurring_indicator}\n"
        message += f"   ID: #{post['id']}\n"
        message += f"   {description}\n\n"
    
    message += "*💡 Tip:* Use /stats for detailed analytics"

    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')


async def help_delete_post_handler(query, user, channel_id, page: int = 0):
    """Display paginated scheduled posts for deletion"""
    # SECURITY CHECK: Verify user owns the channel before showing posts
    if not Database.user_has_channel(user.id, channel_id):
        logger.error(
            f"Security violation: User {user.id} attempted to access delete menu for channel {channel_id} they don't own"
        )
        await query.edit_message_text(
            "❌ *Security Error*\n\nYou don't have access to this channel.",
            parse_mode='Markdown'
        )
        return

    scheduled_posts = Database.get_scheduled_posts_for_channel(user.id, channel_id)

    if not scheduled_posts:
        keyboard = [
            [InlineKeyboardButton("🔙 Back to Channel", callback_data=f"help_channel_posts_{channel_id}")],
            [InlineKeyboardButton("🏠 Back to Help", callback_data="main_help")]
        ]
        message = (
            "📅 *No Scheduled Posts*\n\n"
            "There are no scheduled posts to delete for this channel."
        )
        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return

    kyiv_tz = get_kyiv_timezone()

    def sort_key(post):
        dt = post.get('scheduled_time')
        if not dt:
            return float('inf')
        if dt.tzinfo is None:
            dt = kyiv_tz.localize(dt)
        else:
            dt = dt.astimezone(kyiv_tz)
        return dt.timestamp()

    scheduled_posts.sort(key=sort_key)

    page_size = 5
    total_pages = (len(scheduled_posts) + page_size - 1) // page_size
    page = max(0, min(page, max(total_pages - 1, 0)))
    start_index = page * page_size
    end_index = start_index + page_size
    page_posts = scheduled_posts[start_index:end_index]

    channels = Database.get_user_channels(user.id)
    channel_info = next((ch for ch in channels if ch['channel_id'] == channel_id), {})
    channel_name = escape_markdown(channel_info.get('channel_name', channel_id))

    keyboard = []
    for post in page_posts:
        scheduled_time = post.get('scheduled_time')
        if scheduled_time:
            if scheduled_time.tzinfo is None:
                scheduled_time = kyiv_tz.localize(scheduled_time)
            else:
                scheduled_time = scheduled_time.astimezone(kyiv_tz)
            time_str = scheduled_time.strftime("%b %d %H:%M")
        else:
            time_str = "Not scheduled"

        media_icon = get_media_icon(post.get('media_type'))
        button_text = f"{media_icon} #{post['id']} • {time_str}"
        keyboard.append([
            InlineKeyboardButton(button_text, callback_data=f"help_delete_confirm|{channel_id}|{post['id']}")
        ])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"help_delete_post|{channel_id}|{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"help_delete_post|{channel_id}|{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("🔙 Back to Channel", callback_data=f"help_channel_posts_{channel_id}")])
    keyboard.append([InlineKeyboardButton("🏠 Back to Help", callback_data="main_help")])

    message = (
        "🗑 *Delete Scheduled Post*\n\n"
        f"*Channel:* {channel_name}\n"
        f"*Scheduled Posts:* {len(scheduled_posts)}\n"
        f"*Page:* {page + 1}/{total_pages}\n\n"
        "Select a post to delete. This will remove the scheduled post and its media file."
    )

    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


async def help_delete_post_confirm(query, user, channel_id, post_id: int):
    """Confirm deletion of a scheduled post"""
    post = Database.get_post_by_id(post_id)

    if not post or post['user_id'] != user.id or post.get('channel_id') != channel_id or not post.get('scheduled_time'):
        await query.edit_message_text(
            "❌ *Post Not Found*\n\nThe scheduled post could not be found. It may have already been deleted.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data=f"help_delete_post|{channel_id}|0")]
            ]),
            parse_mode='Markdown'
        )
        return

    kyiv_tz = get_kyiv_timezone()
    scheduled_time = post.get('scheduled_time')
    if scheduled_time:
        if scheduled_time.tzinfo is None:
            scheduled_time = kyiv_tz.localize(scheduled_time)
        else:
            scheduled_time = scheduled_time.astimezone(kyiv_tz)
        scheduled_str = scheduled_time.strftime("%Y-%m-%d %H:%M Kyiv")
    else:
        scheduled_str = "Not scheduled"

    description = escape_markdown(post.get('description') or 'No description')

    channels = Database.get_user_channels(user.id)
    channel_info = next((ch for ch in channels if ch['channel_id'] == channel_id), {})
    channel_name = escape_markdown(channel_info.get('channel_name', channel_id))

    media_icon = get_media_icon(post.get('media_type'))

    message = (
        "🗑 *Confirm Deletion*\n\n"
        f"Are you sure you want to delete scheduled post #{post_id}?\n\n"
        f"*Channel:* {channel_name}\n"
        f"*Scheduled:* {scheduled_str}\n"
        f"*Type:* {media_icon}\n\n"
        "*Caption:*\n"
        f"{description}\n\n"
        "This action permanently removes the post and its media file from the schedule."
    )

    keyboard = [
        [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"help_delete_execute|{channel_id}|{post_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"help_delete_post|{channel_id}|0")]
    ]

    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


async def help_delete_post_execute(query, user, channel_id, post_id: int):
    """Execute deletion of a scheduled post"""
    post = Database.get_post_by_id(post_id)

    if not post or post['user_id'] != user.id or post.get('channel_id') != channel_id or not post.get('scheduled_time'):
        await query.edit_message_text(
            "❌ *Delete Failed*\n\nThe post could not be deleted because it was not found.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data=f"help_delete_post|{channel_id}|0")]
            ]),
            parse_mode='Markdown'
        )
        return

    scheduler = None
    try:
        bot = query.get_bot()
        if bot and bot.application and bot.application.bot_data:
            scheduler = bot.application.bot_data.get('scheduler')
    except AttributeError:
        scheduler = None

    if scheduler:
        try:
            await scheduler.cancel_post_job(post_id)
        except Exception as e:
            logger.error(f"Error cancelling scheduler job for post {post_id}: {e}")

    deleted = Database.delete_scheduled_post(user.id, post_id)

    if deleted:
        try:
            await query.answer("Post deleted", show_alert=False)
        except Exception:
            pass

        await help_delete_post_handler(query, user, channel_id, page=0)
    else:
        await query.edit_message_text(
            "❌ *Delete Failed*\n\nAn unexpected error occurred while deleting the post.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data=f"help_delete_post|{channel_id}|0")]
            ]),
            parse_mode='Markdown'
        )


async def handle_help_callback(query, user, data):
    """Handle help topic callbacks"""
    topic = data.replace("help_", "")

    if topic.startswith("delete_post|"):
        _, channel_id, page = topic.split("|", 2)
        await help_delete_post_handler(query, user, channel_id, int(page))
        return
    if topic.startswith("delete_confirm|"):
        _, channel_id, post_id = topic.split("|", 2)
        await help_delete_post_confirm(query, user, channel_id, int(post_id))
        return
    if topic.startswith("delete_execute|"):
        _, channel_id, post_id = topic.split("|", 2)
        await help_delete_post_execute(query, user, channel_id, int(post_id))
        return

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

async def handle_recurring_callback(query, user, data, context=None):
    """Handle recurring schedule callbacks"""
    action = data.replace("recurring_", "")
    
    # Handle start time selection for recurring posts
    if action == "start_now":
        await handle_recurring_start_choice(query, user, "now", context)
        return
    elif action == "start_custom":
        await handle_recurring_start_choice(query, user, "custom", context)
        return
    
    # Handle recurring post schedule options (new format with post_id)
    if action.startswith("schedule_"):
        # Extract schedule type and post_id from callback data like "schedule_daily_123"
        parts = action.split("_", 2)  # Split into ["schedule", "daily", "123"]
        if len(parts) >= 3:
            schedule_type = parts[1]  # "daily", "3days", "weekly", "custom"
            post_id = int(parts[2])   # The post ID
            
            if schedule_type == "daily":
                interval_hours = 24
            elif schedule_type == "3days":
                interval_hours = 72
            elif schedule_type == "weekly":
                interval_hours = 168
            elif schedule_type == "custom":
                # Handle custom interval for specific recurring post
                Database.update_user_session(user.id, "waiting_recurring_hours", {
                    "action": "schedule_custom", 
                    "post_id": post_id
                })
                await query.edit_message_text(
                    "🕰️ *Custom Interval for Recurring Post*\n\n"
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
                await query.answer("❌ Invalid schedule type!", show_alert=True)
                return
            
            # Show end condition options for this specific post
            keyboard = [
                [
                    InlineKeyboardButton("🔢 End after X posts", callback_data=f"recurring_count_{interval_hours}_{post_id}"),
                    InlineKeyboardButton("📅 End on date", callback_data=f"recurring_date_{interval_hours}_{post_id}")
                ],
                [
                    InlineKeyboardButton("∞ Never end", callback_data=f"recurring_never_{interval_hours}_{post_id}")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            interval_text = f"{interval_hours} hours"
            if interval_hours == 24:
                interval_text = "24 hours (daily)"
            elif interval_hours == 72:
                interval_text = "72 hours (every 3 days)"
            elif interval_hours == 168:
                interval_text = "168 hours (weekly)"
            
            await query.edit_message_text(
                f"🔄 *Recurring Schedule Setup*\n\n"
                f"*Interval:* Every {interval_text}\n"
                f"*Post ID:* {post_id}\n\n"
                f"*How should it end?*",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return
    
    # Handle individual recurring post schedule options (old format)
    elif action.startswith("recur_"):
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
    
    # Handle new format end conditions with post_id
    elif action.startswith("count_"):
        # Parse "count_{interval_hours}" or "count_{interval_hours}_{post_id}"
        parts = action.split("_")
        if len(parts) >= 2:
            interval_hours = int(parts[1])
            
            if len(parts) >= 3:
                # Has post_id
                post_id = int(parts[2])
                Database.update_user_session(user.id, "waiting_recurring_count", {
                    "interval_hours": interval_hours, 
                    "post_id": post_id,
                    "mode": "individual_with_id"
                })
                await query.edit_message_text(
                    f"🔢 *Set Post Count Limit*\n\n"
                    f"*Interval:* Every {interval_hours} hours\n"
                    f"*Post ID:* {post_id}\n\n"
                    f"How many times should this post be repeated?\n\n"
                    f"*Examples:*\n"
                    f"• `5` - Post will be shared 5 times\n"
                    f"• `10` - Post will be shared 10 times\n"
                    f"• `30` - Post will be shared 30 times\n\n"
                    f"*Send the number of repetitions:*",
                    parse_mode='Markdown'
                )
            else:
                # No post_id - individual mode
                Database.update_user_session(user.id, "waiting_recurring_count", {
                    "interval_hours": interval_hours, 
                    "mode": "individual"
                })
                await query.edit_message_text(
                    f"🔢 *Set Post Count Limit*\n\n"
                    f"*Interval:* Every {interval_hours} hours\n\n"
                    f"How many times should this post be repeated?\n\n"
                    f"*Examples:*\n"
                    f"• `5` - Post will be shared 5 times\n"
                    f"• `10` - Post will be shared 10 times\n"
                    f"• `30` - Post will be shared 30 times\n\n"
                    f"*Send the number of repetitions:*",
                    parse_mode='Markdown'
                )
            return
    
    elif action.startswith("date_"):
        # Parse "date_{interval_hours}" or "date_{interval_hours}_{post_id}"
        parts = action.split("_")
        if len(parts) >= 2:
            interval_hours = int(parts[1])
            
            if len(parts) >= 3:
                # Has post_id
                post_id = int(parts[2])
                Database.update_user_session(user.id, "waiting_recurring_date", {
                    "interval_hours": interval_hours, 
                    "post_id": post_id,
                    "mode": "individual_with_id"
                })
                await query.edit_message_text(
                    f"📅 *Set End Date*\n\n"
                    f"*Interval:* Every {interval_hours} hours\n"
                    f"*Post ID:* {post_id}\n\n"
                    f"When should the recurring posts stop?\n\n"
                    f"*Format:* YYYY-MM-DD HH:MM\n"
                    f"*Examples:*\n"
                    f"• `2025-08-01 12:00` - Stop on August 1st at noon\n"
                    f"• `2025-12-31 23:59` - Stop on New Year's Eve\n\n"
                    f"*Send the end date and time (Kyiv timezone):*",
                    parse_mode='Markdown'
                )
            else:
                # No post_id - individual mode
                Database.update_user_session(user.id, "waiting_recurring_date", {
                    "interval_hours": interval_hours, 
                    "mode": "individual"
                })
                await query.edit_message_text(
                    f"📅 *Set End Date*\n\n"
                    f"*Interval:* Every {interval_hours} hours\n\n"
                    f"When should the recurring posts stop?\n\n"
                    f"*Format:* YYYY-MM-DD HH:MM\n"
                    f"*Examples:*\n"
                    f"• `2025-08-01 12:00` - Stop on August 1st at noon\n"
                    f"• `2025-12-31 23:59` - Stop on New Year's Eve\n\n"
                    f"*Send the end date and time (Kyiv timezone):*",
                    parse_mode='Markdown'
                )
            return
    
    elif action.startswith("never_"):
        # Parse "never_{interval_hours}" or "never_{interval_hours}_{post_id}"
        parts = action.split("_")
        if len(parts) >= 2:
            interval_hours = int(parts[1])
            
            if len(parts) >= 3:
                # Has post_id
                post_id = int(parts[2])
                await show_recurring_start_time_options(query, user, {
                    'interval_hours': interval_hours,
                    'post_id': post_id,
                    'end_type': 'never',
                    'recurring_count': None,
                    'recurring_end_date': None,
                    'mode': 'individual_with_id'
                })
            else:
                # No post_id - individual mode
                await show_recurring_start_time_options(query, user, {
                    'interval_hours': interval_hours,
                    'end_type': 'never',
                    'recurring_count': None,
                    'recurring_end_date': None,
                    'mode': 'individual'
                })
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
        # Store config in session and show start time options
        await show_recurring_start_time_options(query, user, {
            'interval_hours': interval,
            'end_type': 'never',
            'recurring_count': None,
            'recurring_end_date': None,
            'mode': 'individual'
        })
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

async def setup_individual_recurring_post(query, user, interval_hours, recurring_count=None, recurring_end_date=None, first_post_time=None):
    """Set up recurring posts for individual mode"""
    # Get the post data from the current recurring mode session
    mode, session_data = Database.get_user_session(user.id)
    if mode != 'RECURRING_MODE':
        await query.edit_message_text("❌ No post to schedule for recurring.")
        return
    
    # Check both key names - 'file_path' and 'current_media_path' for compatibility
    file_path = session_data.get('file_path') or session_data.get('current_media_path')
    media_type = session_data.get('media_type') or session_data.get('current_media_type', 'photo')
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
    
    # Use provided first_post_time or default to 1 minute from now
    if first_post_time is None:
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

async def setup_individual_recurring_post_with_channel(query, user, interval_hours, channel_id, recurring_count=None, recurring_end_date=None, first_post_time=None):
    """Set up individual recurring post with specific channel"""
    # Get the post data from the current recurring mode session
    mode, session_data = Database.get_user_session(user.id)
    if mode != 'RECURRING_MODE':
        await query.edit_message_text("❌ No post to schedule for recurring.")
        return
    
    # Check both key names - 'file_path' and 'current_media_path' for compatibility
    file_path = session_data.get('file_path') or session_data.get('current_media_path')
    media_type = session_data.get('media_type') or session_data.get('current_media_type', 'photo')
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
    
    # Use provided first_post_time or default to 1 minute from now
    if first_post_time is None:
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


async def show_recurring_start_time_options(query, user, config: dict):
    """Show start time options for recurring posts"""
    from bot.utils import get_current_kyiv_time
    
    # Get current session data to preserve media info
    current_mode, current_session = Database.get_user_session(user.id)
    
    # Merge the recurring config with existing session data (preserve media info)
    merged_session = {**current_session, 'recurring_config': config}
    
    # Store the merged session
    Database.update_user_session(user.id, "RECURRING_START_TIME", merged_session)
    
    current_time = get_current_kyiv_time()
    interval_hours = config.get('interval_hours', 24)
    
    interval_text = f"{interval_hours} hours"
    if interval_hours == 24:
        interval_text = "daily"
    elif interval_hours == 48:
        interval_text = "every 2 days"
    elif interval_hours == 168:
        interval_text = "weekly"
    
    end_type = config.get('end_type', 'never')
    if end_type == 'never':
        end_text = "Never (manual stop)"
    elif config.get('recurring_count'):
        end_text = f"After {config['recurring_count']} posts"
    elif config.get('recurring_end_date'):
        # Handle both string and datetime objects
        end_date = config['recurring_end_date']
        if isinstance(end_date, str):
            from datetime import datetime
            end_date = datetime.fromisoformat(end_date)
        end_text = f"On {end_date.strftime('%Y-%m-%d %H:%M')}"
    else:
        end_text = "Never"
    
    keyboard = [
        [InlineKeyboardButton("🚀 Start Now (in 1 minute)", callback_data="recurring_start_now")],
        [InlineKeyboardButton("📅 Custom Date & Time", callback_data="recurring_start_custom")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"⏰ *When should the first post be sent?*\n\n"
        f"*Interval:* Every {interval_text}\n"
        f"*End condition:* {end_text}\n"
        f"*Current time:* {current_time.strftime('%Y-%m-%d %H:%M')} (Kyiv)\n\n"
        f"Choose when to start:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def show_recurring_start_time_options_from_message(update: Update, user, config: dict):
    """Show start time options for recurring posts (from text message input)"""
    from bot.utils import get_current_kyiv_time
    
    # Get current session data to preserve media info
    current_mode, current_session = Database.get_user_session(user.id)
    
    # Merge the recurring config with existing session data (preserve media info)
    merged_session = {**current_session, 'recurring_config': config}
    
    # Store the merged session
    Database.update_user_session(user.id, "RECURRING_START_TIME", merged_session)
    
    current_time = get_current_kyiv_time()
    interval_hours = config.get('interval_hours', 24)
    
    interval_text = f"{interval_hours} hours"
    if interval_hours == 24:
        interval_text = "daily"
    elif interval_hours == 48:
        interval_text = "every 2 days"
    elif interval_hours == 168:
        interval_text = "weekly"
    
    end_type = config.get('end_type', 'never')
    if end_type == 'never':
        end_text = "Never (manual stop)"
    elif config.get('recurring_count'):
        end_text = f"After {config['recurring_count']} posts"
    elif config.get('recurring_end_date'):
        # Handle both string and datetime objects
        end_date = config['recurring_end_date']
        if isinstance(end_date, str):
            from datetime import datetime
            end_date = datetime.fromisoformat(end_date)
        end_text = f"On {end_date.strftime('%Y-%m-%d %H:%M')}"
    else:
        end_text = "Never"
    
    keyboard = [
        [InlineKeyboardButton("🚀 Start Now (in 1 minute)", callback_data="recurring_start_now")],
        [InlineKeyboardButton("📅 Custom Date & Time", callback_data="recurring_start_custom")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"⏰ *When should the first post be sent?*\n\n"
        f"*Interval:* Every {interval_text}\n"
        f"*End condition:* {end_text}\n"
        f"*Current time:* {current_time.strftime('%Y-%m-%d %H:%M')} (Kyiv)\n\n"
        f"Choose when to start:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def handle_recurring_start_choice(query, user, start_type: str, context=None):
    """Handle when user selects start time option"""
    from bot.utils import get_current_kyiv_time
    from datetime import timedelta
    
    mode, session_data = Database.get_user_session(user.id)
    if mode != 'RECURRING_START_TIME':
        await query.answer("Session expired. Please start again.")
        return
    
    # Extract recurring config from merged session
    recurring_config = session_data.get('recurring_config', {})
    
    if start_type == "now":
        # Start in 1 minute
        first_post_time = get_current_kyiv_time() + timedelta(minutes=1)
        await finalize_recurring_post(query, user, session_data, recurring_config, first_post_time, context)
    
    elif start_type == "custom":
        # Ask for custom datetime input
        current_time = get_current_kyiv_time()
        Database.update_user_session(user.id, "waiting_recurring_start_time", session_data)
        
        await query.edit_message_text(
            f"📅 *Set Start Date & Time*\n\n"
            f"*Current time:* {current_time.strftime('%Y-%m-%d %H:%M')} (Kyiv)\n\n"
            f"Enter when the first post should be sent:\n\n"
            f"*Format:* `YYYY-MM-DD HH:MM`\n\n"
            f"*Examples:*\n"
            f"• `{(current_time + timedelta(hours=2)).strftime('%Y-%m-%d %H:%M')}` - In 2 hours\n"
            f"• `{(current_time + timedelta(days=1)).strftime('%Y-%m-%d')} 10:00` - Tomorrow at 10 AM\n"
            f"• `{(current_time + timedelta(days=7)).strftime('%Y-%m-%d')} 14:00` - Next week at 2 PM\n\n"
            f"*Send the date and time (Kyiv timezone):*",
            parse_mode='Markdown'
        )


async def handle_recurring_start_time_input(update: Update, user, text: str, session_data: dict):
    """Handle custom start time input for recurring posts"""
    from bot.utils import get_current_kyiv_time, get_kyiv_timezone
    from datetime import datetime
    
    text = text.strip()
    current_time = get_current_kyiv_time()
    kyiv_tz = get_kyiv_timezone()
    
    try:
        # Parse the datetime input
        if ' ' in text:
            start_time = datetime.strptime(text, '%Y-%m-%d %H:%M')
        else:
            # If only date provided, default to current hour
            start_time = datetime.strptime(text, '%Y-%m-%d')
            start_time = start_time.replace(hour=current_time.hour, minute=0)
        
        # Make it timezone-aware
        start_time = kyiv_tz.localize(start_time)
        
        # Validate it's in the future
        if start_time <= current_time:
            await update.message.reply_text(
                f"❌ Start time must be in the future.\n\n"
                f"*Current time:* {current_time.strftime('%Y-%m-%d %H:%M')} (Kyiv)\n\n"
                f"Please enter a future date and time:",
                parse_mode='Markdown'
            )
            return True
        
        # Finalize the recurring post
        await finalize_recurring_post_from_message(update, user, session_data, start_time)
        return True
        
    except ValueError as e:
        await update.message.reply_text(
            f"❌ Invalid date format.\n\n"
            f"Please use: `YYYY-MM-DD HH:MM`\n"
            f"Example: `{current_time.strftime('%Y-%m-%d %H:%M')}`\n\n"
            f"Try again:",
            parse_mode='Markdown'
        )
        return True


async def finalize_recurring_post(query, user, session_data: dict, recurring_config: dict, first_post_time, context=None):
    """Finalize and create the recurring post"""
    from datetime import datetime
    
    post_id = recurring_config.get('post_id')
    interval_hours = recurring_config.get('interval_hours')
    recurring_count = recurring_config.get('recurring_count')
    recurring_end_date = recurring_config.get('recurring_end_date')
    mode = recurring_config.get('mode', 'individual')
    
    # Handle existing post (from individual mode with post_id)
    if post_id and mode == 'individual_with_id':
        try:
            # Update the existing post to be recurring
            conn = Database.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE posts SET 
                    is_recurring = TRUE,
                    recurring_interval_hours = ?,
                    recurring_count = ?,
                    recurring_end_date = ?,
                    scheduled_time = ?
                WHERE id = ? AND user_id = ?
            ''', (interval_hours, recurring_count, 
                  recurring_end_date.isoformat() if recurring_end_date else None,
                  first_post_time.isoformat(), post_id, user.id))
            conn.commit()
            conn.close()
            
            # Schedule via shared scheduler
            if context and 'scheduler' in context.application.bot_data:
                scheduler = context.application.bot_data['scheduler']
                scheduler._schedule_single_post(post_id, first_post_time)
                logger.info(f"Scheduled recurring post {post_id} via shared scheduler")
            
            interval_text = f"{interval_hours} hours"
            if interval_hours == 24:
                interval_text = "24 hours (daily)"
            elif interval_hours == 48:
                interval_text = "48 hours (every 2 days)"
            elif interval_hours == 168:
                interval_text = "168 hours (weekly)"
            
            end_info = ""
            if recurring_count:
                end_info = f"*End Condition:* After {recurring_count} posts\n"
            elif recurring_end_date:
                end_info = f"*End Condition:* On {recurring_end_date.strftime('%Y-%m-%d %H:%M')}\n"
            else:
                end_info = "*End Condition:* Never (runs until manually stopped)\n"
            
            keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"✅ *Recurring Post Scheduled!*\n\n"
                f"*Post ID:* {post_id}\n"
                f"*Interval:* Every {interval_text}\n"
                f"{end_info}"
                f"*First Post:* {first_post_time.strftime('%Y-%m-%d %H:%M')} (Kyiv time)\n\n"
                f"Your recurring post will start at the scheduled time!",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            
            Database.update_user_session(user.id, BotStates.IDLE)
            return
            
        except Exception as e:
            logger.error(f"Failed to set up recurring post {post_id}: {e}")
            await query.edit_message_text(
                f"❌ *Error Setting Up Recurring Post*\n\n"
                f"Failed to configure the recurring schedule. Please try again.",
                parse_mode='Markdown'
            )
            return
    
    # Handle individual mode (new post creation)
    elif mode == 'individual':
        # Parse end date if it's a string
        if recurring_end_date and isinstance(recurring_end_date, str):
            recurring_end_date = datetime.fromisoformat(recurring_end_date)
        
        # Get media info from session_data
        file_path = session_data.get('file_path') or session_data.get('current_media_path')
        media_type = session_data.get('media_type') or session_data.get('current_media_type', 'photo')
        description = session_data.get('description', '')
        
        if not file_path:
            await query.edit_message_text("❌ No media file found. Please start again.")
            Database.update_user_session(user.id, BotStates.IDLE)
            return
        
        # Get channel from session (user selected it earlier) or fall back to first channel
        target_channel_id = session_data.get('channel_id')
        channels = Database.get_user_channels(user.id)
        
        if not channels:
            await query.edit_message_text("❌ No channels configured!")
            Database.update_user_session(user.id, BotStates.IDLE)
            return
        
        # Find the selected channel or use the first one
        if target_channel_id:
            target_channel = next((ch for ch in channels if ch['channel_id'] == target_channel_id), channels[0])
        else:
            target_channel = channels[0]
        
        Database.add_post(
            user_id=user.id,
            file_path=file_path,
            media_type=media_type,
            description=description,
            scheduled_time=first_post_time,
            mode=3,
            channel_id=target_channel['channel_id'],
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
            f"*📺 Channel:* {target_channel['channel_name']}\n"
            f"*⏰ Interval:* Every {interval_text}\n"
            f"*🚀 First post:* {first_post_time.strftime('%Y-%m-%d %H:%M')} (Kyiv)\n"
            f"{end_info}\n"
            f"Use /stats to monitor your recurring posts.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
        Database.update_user_session(user.id, BotStates.IDLE)
        return
    
    # Default: show error
    await query.edit_message_text("❌ Error: Unknown recurring mode. Please try again.")
    Database.update_user_session(user.id, BotStates.IDLE)


async def finalize_recurring_post_from_message(update: Update, user, session_data: dict, first_post_time):
    """Finalize recurring post creation from text message (custom start time)"""
    from datetime import datetime
    
    # Extract recurring config from session_data
    recurring_config = session_data.get('recurring_config', {})
    
    post_id = recurring_config.get('post_id')
    interval_hours = recurring_config.get('interval_hours')
    recurring_count = recurring_config.get('recurring_count')
    recurring_end_date = recurring_config.get('recurring_end_date')
    mode = recurring_config.get('mode', 'individual')
    
    # Handle existing post (from individual mode with post_id)
    if post_id and mode == 'individual_with_id':
        try:
            # Update the existing post to be recurring
            conn = Database.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE posts SET 
                    is_recurring = TRUE,
                    recurring_interval_hours = ?,
                    recurring_count = ?,
                    recurring_end_date = ?,
                    scheduled_time = ?
                WHERE id = ? AND user_id = ?
            ''', (interval_hours, recurring_count, 
                  recurring_end_date.isoformat() if recurring_end_date else None,
                  first_post_time.isoformat(), post_id, user.id))
            conn.commit()
            conn.close()
            
            interval_text = f"{interval_hours} hours"
            if interval_hours == 24:
                interval_text = "24 hours (daily)"
            elif interval_hours == 48:
                interval_text = "48 hours (every 2 days)"
            elif interval_hours == 168:
                interval_text = "168 hours (weekly)"
            
            end_info = ""
            if recurring_count:
                end_info = f"*End Condition:* After {recurring_count} posts\n"
            elif recurring_end_date:
                end_info = f"*End Condition:* On {recurring_end_date.strftime('%Y-%m-%d %H:%M')}\n"
            else:
                end_info = "*End Condition:* Never (runs until manually stopped)\n"
            
            keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"✅ *Recurring Post Scheduled!*\n\n"
                f"*Post ID:* {post_id}\n"
                f"*Interval:* Every {interval_text}\n"
                f"{end_info}"
                f"*First Post:* {first_post_time.strftime('%Y-%m-%d %H:%M')} (Kyiv time)\n\n"
                f"Your recurring post will start at the scheduled time!",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            
            Database.update_user_session(user.id, BotStates.IDLE)
            return
            
        except Exception as e:
            logger.error(f"Failed to set up recurring post {post_id}: {e}")
            await update.message.reply_text(
                f"❌ *Error Setting Up Recurring Post*\n\n"
                f"Failed to configure the recurring schedule. Please try again.",
                parse_mode='Markdown'
            )
            return
    
    # Handle individual mode - need to create new post via message
    elif mode == 'individual':
        # Parse end date if it's a string
        if recurring_end_date and isinstance(recurring_end_date, str):
            recurring_end_date = datetime.fromisoformat(recurring_end_date)
        
        # Get media info from session_data (it was preserved when we merged sessions)
        file_path = session_data.get('file_path') or session_data.get('current_media_path')
        media_type = session_data.get('media_type') or session_data.get('current_media_type', 'photo')
        description = session_data.get('description', '')
        
        if not file_path:
            await update.message.reply_text("❌ No media file found. Please start again.")
            Database.update_user_session(user.id, BotStates.IDLE)
            return
        
        # Get channel from session (user selected it earlier) or fall back to first channel
        target_channel_id = session_data.get('channel_id')
        channels = Database.get_user_channels(user.id)
        
        if not channels:
            await update.message.reply_text("❌ No channels configured!")
            Database.update_user_session(user.id, BotStates.IDLE)
            return
        
        # Find the selected channel or use the first one
        if target_channel_id:
            target_channel = next((ch for ch in channels if ch['channel_id'] == target_channel_id), channels[0])
        else:
            target_channel = channels[0]
        
        Database.add_post(
            user_id=user.id,
            file_path=file_path,
            media_type=media_type,
            description=description,
            scheduled_time=first_post_time,
            mode=3,
            channel_id=target_channel['channel_id'],
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
        
        await update.message.reply_text(
            f"✅ *Individual Recurring Post Activated!*\n\n"
            f"*📺 Channel:* {target_channel['channel_name']}\n"
            f"*⏰ Interval:* Every {interval_text}\n"
            f"*🚀 First post:* {first_post_time.strftime('%Y-%m-%d %H:%M')} (Kyiv)\n"
            f"{end_info}\n"
            f"Use /stats to monitor your recurring posts.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
        Database.update_user_session(user.id, BotStates.IDLE)
        return
    
    await update.message.reply_text("❌ Error: Unknown recurring mode. Please try again.")
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
        
        # Get post_id if present (from schedule_custom flow)
        post_id = session_data.get('post_id')
        
        # Check if this is for a specific post (has post_id from schedule_custom)
        if session_data.get('action') == 'schedule_custom' and post_id:
            # Show end condition options with post_id preserved
            keyboard = [
                [
                    InlineKeyboardButton("🔢 End after X posts", callback_data=f"recurring_count_{hours}_{post_id}"),
                    InlineKeyboardButton("📅 End on date", callback_data=f"recurring_date_{hours}_{post_id}")
                ],
                [
                    InlineKeyboardButton("∞ Never end", callback_data=f"recurring_never_{hours}_{post_id}")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            message = f"""
🔄 *Recurring Schedule Setup*

*Post ID:* {post_id}
*Interval:* Every {hours} hours

*How should it end?*

• *Count:* Stop after a specific number of posts
• *Date:* Stop on a specific date  
• *Never:* Continue until manually stopped

Choose end condition:
"""
            
            await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')
            # Keep session with post_id for the flow
            Database.update_user_session(user.id, "RECURRING_END_CONDITION", {
                "post_id": post_id,
                "interval_hours": hours
            })
        elif session_data.get('action') == 'recur_custom':
            # Show end condition options for individual post (no post_id yet)
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
            Database.update_user_session(user.id, BotStates.IDLE)
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
            # Show start time options instead of directly setting up
            await show_recurring_start_time_options_from_message(update, user, {
                'interval_hours': interval_hours,
                'end_type': 'count',
                'recurring_count': count,
                'recurring_end_date': None,
                'mode': 'individual'
            })
            return
        elif session_data.get('mode') == 'individual_with_id':
            # Set up specific recurring post with count
            post_id = session_data.get('post_id')
            if post_id:
                try:
                    from bot.utils import get_current_kyiv_time
                    from datetime import timedelta
                    
                    # Update the post to be recurring with count limit
                    conn = Database.get_connection()
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE posts SET 
                            is_recurring = TRUE,
                            recurring_interval_hours = ?,
                            recurring_count = ?,
                            recurring_end_date = NULL
                        WHERE id = ?
                    ''', (interval_hours, count, post_id))
                    conn.commit()
                    conn.close()
                    
                    # Schedule the first occurrence
                    next_time = get_current_kyiv_time() + timedelta(minutes=1)
                    Database.update_post_schedule(post_id, next_time)
                    
                    # Post will be picked up by the monitoring system within 5 minutes
                    logger.info(f"Recurring post {post_id} saved to database with count={count}, will be picked up by monitor")
                    
                    await update.message.reply_text(
                        f"✅ *Recurring Post Scheduled!*\n\n"
                        f"*Post ID:* {post_id}\n"
                        f"*Interval:* Every {interval_hours} hours\n"
                        f"*Count Limit:* {count} repetitions\n"
                        f"*First Post:* {next_time.strftime('%Y-%m-%d %H:%M')} (Kyiv time)\n\n"
                        f"Your recurring post will start shortly!",
                        parse_mode='Markdown'
                    )
                    
                    # Reset user session
                    Database.update_user_session(user.id, BotStates.IDLE)
                    return
                    
                except Exception as e:
                    logger.error(f"Failed to set up recurring post {post_id}: {e}")
                    await update.message.reply_text(
                        f"❌ *Error Setting Up Recurring Post*\n\n"
                        f"Failed to configure the recurring schedule. Please try again.",
                        parse_mode='Markdown'
                    )
                    return
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
            # Show start time options instead of directly setting up
            await show_recurring_start_time_options_from_message(update, user, {
                'interval_hours': interval_hours,
                'end_type': 'date',
                'recurring_count': None,
                'recurring_end_date': end_date.isoformat(),
                'mode': 'individual'
            })
            return
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
    # SECURITY CHECK: Verify user owns the channel before creating batch
    if not Database.user_has_channel(user.id, channel_id):
        logger.error(f"Security violation: User {user.id} attempted to create batch for channel {channel_id} they don't own")
        await query.edit_message_text(
            "❌ *Security Error*\n\nYou don't have access to this channel.",
            parse_mode='Markdown'
        )
        return
    
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
    # Note: This function needs context parameter to access shared scheduler
    from bot.scheduler import PostScheduler
    scheduler = PostScheduler()
    logger.warning("Using fallback scheduler instance in batch scheduling - jobs may not persist")
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
        
        # Generate unique filename and save with streaming
        from bot.utils import generate_unique_filename, save_media, save_media_streaming
        filename = generate_unique_filename(original_filename)
        
        try:
            file_path = await save_media_streaming(file, filename, media_type)
        except Exception as e:
            logger.error(f"Batch streaming failed, using fallback: {e}")
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
        
        # Preserve batch progress - don't lose existing uploads
        error_message = f"❌ Error processing this {media_type}: {str(e)}"
        
        if "File is too big" in str(e):
            error_message += "\n\n💡 This file exceeds Telegram's limits."
        
        error_message += "\n\n✅ Your batch progress is safe - previous uploads remain in the batch."
        error_message += "\n\n📤 Continue uploading more files or use /finish when your batch is ready."
        
        await update.message.reply_text(error_message)


# Caption Recovery Handlers (aliases for easy import)
async def recover_captions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /recover_captions command"""
    await handle_recover_captions_command(update, context)

async def recover_captions_interactive_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /recover_captions_interactive command"""
    await handle_recover_captions_interactive(update, context)

async def edit_captions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /edit_captions command - interactive caption editing for scheduled posts"""
    user = update.effective_user
    
    # Check if user has channels configured
    channels = Database.get_user_channels(user.id)
    
    if not channels:
        await update.message.reply_text(
            "❌ *No channels configured!*\n\n"
            "Please add a channel first using /channels command before editing captions.",
            parse_mode='Markdown'
        )
        return
    
    # Create channel selection keyboard
    keyboard = []
    for channel in channels:
        channel_id, channel_name = channel['channel_id'], channel['channel_name']
        display_text = f"📺 {channel_name}"
        if len(display_text) > 30:
            display_text = f"📺 {channel_name[:27]}..."
        callback_data = f"edit_captions_channel_{channel_id}"
        keyboard.append([InlineKeyboardButton(display_text, callback_data=callback_data)])
    
    keyboard.append([InlineKeyboardButton("🚫 Cancel", callback_data="back_to_main")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = """
✏️ *Edit Captions*

This tool lets you edit captions for your scheduled posts one by one.

*How it works:*
1. Select a channel
2. View scheduled posts in chronological order
3. Edit captions for each post
4. Navigate through posts with Previous/Next buttons

*Select a channel to start:*
"""
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def delete_all_captions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /delete_all_captions command"""
    user = update.effective_user
    
    # Create confirmation keyboard
    keyboard = [
        [
            InlineKeyboardButton("❌ Delete All Captions", callback_data="delete_captions_confirm"),
            InlineKeyboardButton("🚫 Cancel", callback_data="delete_captions_cancel")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Get count of posts with captions first
    from .database import Database
    conn = Database.get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            SELECT COUNT(*) FROM posts 
            WHERE user_id = ? AND description IS NOT NULL AND description != ''
        ''', (user.id,))
        
        posts_with_captions = cursor.fetchone()[0]
        conn.close()
        
        if posts_with_captions == 0:
            await update.message.reply_text(
                "📝 *No Captions Found*\n\n"
                "You don't have any posts with captions to delete.",
                parse_mode='Markdown'
            )
            return
        
        confirmation_message = f"""
⚠️ *Delete All Captions*

You currently have **{posts_with_captions}** posts with captions.

**This action will:**
• Remove ALL captions from ALL your posts
• Keep your media files and schedule intact
• Cannot be undone automatically

Are you sure you want to delete all captions?
"""
        
        await update.message.reply_text(
            confirmation_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
    except Exception as e:
        conn.close()
        logger.error(f"Error checking captions for user {user.id}: {e}")
        await update.message.reply_text(
            "❌ Error checking your captions. Please try again later.",
            parse_mode='Markdown'
        )

async def handle_delete_captions_callback(query, user, data):
    """Handle delete captions confirmation callback"""
    if data == "delete_captions_confirm":
        # Perform the deletion
        from .database import Database
        deleted_count = Database.delete_all_captions(user.id)
        
        if deleted_count > 0:
            message = f"""
✅ *Captions Deleted Successfully*

**{deleted_count}** captions have been removed from your posts.

Your media files and schedules remain intact. You can use `/recover_captions` or `/recover_interactive` if you need to restore any captions later.
"""
        else:
            message = """
📝 *No Captions to Delete*

No captions were found to delete. All your posts are already without captions.
"""
        
        await query.edit_message_text(message, parse_mode='Markdown')
        
    elif data == "delete_captions_cancel":
        await query.edit_message_text(
            "🚫 *Operation Cancelled*\n\n"
            "Your captions remain unchanged.",
            parse_mode='Markdown'
        )

async def handle_edit_captions_callback(query, user, data):
    """Handle edit captions callback"""
    if data.startswith("edit_captions_channel_"):
        channel_id = data.replace("edit_captions_channel_", "")
        await start_caption_editing_for_channel(query, user, channel_id)
    elif data.startswith("edit_captions_nav_"):
        # Parse navigation data: edit_captions_nav_{channel_id}_{index}_{action}
        parts = data.replace("edit_captions_nav_", "").split("_")
        if len(parts) >= 3:
            channel_id = "_".join(parts[:-2])  # Channel ID might contain underscores
            post_index = int(parts[-2])
            action = parts[-1]  # next, prev, skip, done
            await handle_caption_editing_navigation(query, user, channel_id, post_index, action)
    elif data.startswith("edit_captions_edit_"):
        # Parse edit data: edit_captions_edit_{channel_id}_{post_index}
        parts = data.replace("edit_captions_edit_", "").split("_")
        if len(parts) >= 2:
            channel_id = "_".join(parts[:-1])  # Channel ID might contain underscores
            post_index = int(parts[-1])
            await prompt_caption_input(query, user, channel_id, post_index)
    elif data.startswith("edit_captions_done_"):
        channel_id = data.replace("edit_captions_done_", "")
        await query.edit_message_text(
            "✅ *Caption Editing Complete*\n\n"
            "All your caption edits have been saved successfully!",
            parse_mode='Markdown'
        )

async def start_caption_editing_for_channel(query, user, channel_id):
    """Start caption editing for a specific channel"""
    # SECURITY CHECK: Verify user owns the channel before editing captions
    if not Database.user_has_channel(user.id, channel_id):
        logger.error(f"Security violation: User {user.id} attempted to edit captions for channel {channel_id} they don't own")
        await query.edit_message_text(
            "❌ *Security Error*\n\nYou don't have access to this channel.",
            parse_mode='Markdown'
        )
        return
    
    # Get scheduled posts for this channel
    scheduled_posts = Database.get_scheduled_posts_for_channel(user.id, channel_id)
    
    if not scheduled_posts:
        await query.edit_message_text(
            "📭 *No Scheduled Posts*\n\n"
            f"No scheduled posts found for this channel.\n\n"
            "Use /schedule to schedule some posts first.",
            parse_mode='Markdown'
        )
        return
    
    # Start with the first post (index 0)
    await show_post_for_caption_editing(query, user, channel_id, 0, scheduled_posts)

async def show_post_for_caption_editing(query, user, channel_id, post_index, posts_list=None):
    """Show a specific post for caption editing"""
    if posts_list is None:
        posts_list = Database.get_scheduled_posts_for_channel(user.id, channel_id)
    
    if post_index >= len(posts_list) or post_index < 0:
        await query.edit_message_text(
            "✅ *Caption Editing Complete*\n\n"
            "You've reviewed all scheduled posts for this channel!",
            parse_mode='Markdown'
        )
        return
    
    post = posts_list[post_index]
    
    # Get channel name for display
    channels = Database.get_user_channels(user.id)
    channel_name = next((ch['channel_name'] for ch in channels if ch['channel_id'] == channel_id), "Unknown Channel")
    
    # Format scheduled time
    scheduled_time = post['scheduled_time']
    if scheduled_time:
        from .utils import get_kyiv_timezone
        kyiv_tz = get_kyiv_timezone()
        if scheduled_time.tzinfo is None:
            scheduled_time = kyiv_tz.localize(scheduled_time)
        else:
            scheduled_time = scheduled_time.astimezone(kyiv_tz)
        time_str = scheduled_time.strftime("%Y-%m-%d %H:%M Kyiv")
    else:
        time_str = "Not scheduled"
    
    # Get media type icon
    media_icon = get_media_icon(post['media_type'])
    
    # Current caption - escaped for Markdown display
    current_caption = escape_markdown(post['description'] or "No caption")
    
    # Create navigation buttons
    keyboard = []
    
    # Navigation row
    nav_buttons = []
    if post_index > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"edit_captions_nav_{channel_id}_{post_index}_prev"))
    
    nav_buttons.append(InlineKeyboardButton("⏭️ Skip", callback_data=f"edit_captions_nav_{channel_id}_{post_index}_skip"))
    
    if post_index < len(posts_list) - 1:
        nav_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"edit_captions_nav_{channel_id}_{post_index}_next"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    # Action buttons
    keyboard.append([
        InlineKeyboardButton("✏️ Edit Caption", callback_data=f"edit_captions_edit_{channel_id}_{post_index}"),
        InlineKeyboardButton("✅ Done", callback_data=f"edit_captions_done_{channel_id}")
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = f"""
✏️ *Edit Captions* - Post {post_index + 1}/{len(posts_list)}

📺 *Channel:* {channel_name}
{media_icon} *Post #{post['id']}*
📅 *Scheduled:* {time_str}

*Current Caption:*
{current_caption}

Choose an action or navigate to another post:
"""
    
    # Store editing state for text input handling
    Database.update_user_session(user.id, "editing_caption", {
        'channel_id': channel_id,
        'post_index': post_index,
        'post_id': post['id']
    })
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_caption_editing_navigation(query, user, channel_id, post_index, action):
    """Handle navigation during caption editing"""
    posts_list = Database.get_scheduled_posts_for_channel(user.id, channel_id)
    
    if action == "next":
        new_index = post_index + 1
    elif action == "prev":
        new_index = post_index - 1
    elif action == "skip":
        new_index = post_index + 1
    elif action == "cancel":
        # Return to post display without editing
        new_index = post_index
    else:
        new_index = post_index
    
    await show_post_for_caption_editing(query, user, channel_id, new_index, posts_list)

async def prompt_caption_input(query, user, channel_id, post_index):
    """Prompt user to enter a new caption"""
    posts_list = Database.get_scheduled_posts_for_channel(user.id, channel_id)
    
    if post_index >= len(posts_list):
        await query.edit_message_text("❌ Post not found.")
        return
    
    post = posts_list[post_index]
    
    # Get channel name for display
    channels = Database.get_user_channels(user.id)
    channel_name = next((ch['channel_name'] for ch in channels if ch['channel_id'] == channel_id), "Unknown Channel")
    
    # Get media type icon
    media_icon = get_media_icon(post['media_type'])
    
    # Current caption - escaped for Markdown display
    current_caption = escape_markdown(post['description'] or "No caption")
    
    keyboard = [[
        InlineKeyboardButton("🚫 Cancel", callback_data=f"edit_captions_nav_{channel_id}_{post_index}_cancel")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = f"""
✏️ *Edit Caption* - Post {post_index + 1}/{len(posts_list)}

📺 *Channel:* {channel_name}
{media_icon} *Post #{post['id']}*

*Current Caption:*
{current_caption}

*Type your new caption:*
Send your new caption as a text message. It can be as long or short as you want.

To remove the caption entirely, send: **REMOVE**
"""
    
    # Set user state to await caption input
    Database.update_user_session(user.id, "awaiting_caption_input", {
        'channel_id': channel_id,
        'post_index': post_index,
        'post_id': post['id']
    })
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_new_caption_input(update: Update, user, text: str, session_data: dict):
    """Handle new caption input during caption editing"""
    if not session_data or 'post_id' not in session_data:
        await update.message.reply_text(
            "❌ Error: Session data not found. Please start over with /edit_captions."
        )
        return
    
    post_id = session_data['post_id']
    channel_id = session_data['channel_id']
    post_index = session_data['post_index']
    
    # Check if user wants to remove caption
    if text.upper() == "REMOVE":
        new_caption = None
        action_text = "removed"
    else:
        new_caption = text
        action_text = "updated"
    
    # Update the caption in database
    success = Database.update_post_description(post_id, new_caption)
    
    if success:
        # Show success message and move to next post
        await update.message.reply_text(
            f"✅ *Caption {action_text.title()}!*\n\n"
            f"Post #{post_id} caption has been {action_text}.\n\n"
            f"Moving to next post...",
            parse_mode='Markdown'
        )
        
        # Move to next post automatically
        posts_list = Database.get_scheduled_posts_for_channel(user.id, channel_id)
        next_index = post_index + 1
        
        if next_index >= len(posts_list):
            # We're done with all posts
            Database.update_user_session(user.id, BotStates.IDLE)
            await update.message.reply_text(
                "🎉 *All Done!*\n\n"
                "You've finished editing captions for all scheduled posts in this channel!",
                parse_mode='Markdown'
            )
        else:
            # Show next post for editing
            await asyncio.sleep(1)  # Brief pause before showing next post
            await show_post_for_caption_editing_via_message(update, user, channel_id, next_index, posts_list)
    else:
        await update.message.reply_text(
            f"❌ Error updating caption for post #{post_id}. Please try again.",
            parse_mode='Markdown'
        )

async def show_post_for_caption_editing_via_message(update, user, channel_id, post_index, posts_list=None):
    """Show post for caption editing via new message (not query edit)"""
    if posts_list is None:
        posts_list = Database.get_scheduled_posts_for_channel(user.id, channel_id)
    
    if post_index >= len(posts_list) or post_index < 0:
        Database.update_user_session(user.id, BotStates.IDLE)
        await update.message.reply_text(
            "✅ *Caption Editing Complete*\n\n"
            "You've reviewed all scheduled posts for this channel!",
            parse_mode='Markdown'
        )
        return
    
    post = posts_list[post_index]
    
    # Get channel name for display
    channels = Database.get_user_channels(user.id)
    channel_name = next((ch['channel_name'] for ch in channels if ch['channel_id'] == channel_id), "Unknown Channel")
    
    # Format scheduled time
    scheduled_time = post['scheduled_time']
    if scheduled_time:
        from .utils import get_kyiv_timezone
        kyiv_tz = get_kyiv_timezone()
        if scheduled_time.tzinfo is None:
            scheduled_time = kyiv_tz.localize(scheduled_time)
        else:
            scheduled_time = scheduled_time.astimezone(kyiv_tz)
        time_str = scheduled_time.strftime("%Y-%m-%d %H:%M Kyiv")
    else:
        time_str = "Not scheduled"
    
    # Get media type icon
    media_icon = get_media_icon(post['media_type'])
    
    # Current caption - escaped for Markdown display
    current_caption = escape_markdown(post['description'] or "No caption")
    
    # Create navigation buttons
    keyboard = []
    
    # Navigation row
    nav_buttons = []
    if post_index > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"edit_captions_nav_{channel_id}_{post_index}_prev"))
    
    nav_buttons.append(InlineKeyboardButton("⏭️ Skip", callback_data=f"edit_captions_nav_{channel_id}_{post_index}_skip"))
    
    if post_index < len(posts_list) - 1:
        nav_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"edit_captions_nav_{channel_id}_{post_index}_next"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    # Action buttons
    keyboard.append([
        InlineKeyboardButton("✏️ Edit Caption", callback_data=f"edit_captions_edit_{channel_id}_{post_index}"),
        InlineKeyboardButton("✅ Done", callback_data=f"edit_captions_done_{channel_id}")
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = f"""
✏️ *Edit Captions* - Post {post_index + 1}/{len(posts_list)}

📺 *Channel:* {channel_name}
{media_icon} *Post #{post['id']}*
📅 *Scheduled:* {time_str}

*Current Caption:*
{current_caption}

Choose an action or navigate to another post:
"""
    
    # Store editing state for text input handling
    Database.update_user_session(user.id, "editing_caption", {
        'channel_id': channel_id,
        'post_index': post_index,
        'post_id': post['id']
    })
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

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
            # Note: This function needs context parameter to access shared scheduler
            from bot.scheduler import PostScheduler
            scheduler = PostScheduler()
            logger.warning("Using fallback scheduler instance in batch scheduling - jobs may not persist")
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
    
    mode_names = {1: "Bulk Upload", 2: "Individual Upload", 3: "Guided Captioning"}
    mode_name = mode_names.get(mode, "Upload")
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
    
    mode_names = {1: "Bulk Upload", 2: "Individual Upload", 3: "Guided Captioning"}
    mode_name = mode_names.get(mode, "Upload")
    message = f"📺 *Select Channel for Mode {mode} ({mode_name}):*\n\n"
    for i, channel in enumerate(channels, 1):
        message += f"{i}. {channel['channel_name']}\n   ID: `{channel['channel_id']}`\n\n"
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_mode_channel_selection(query, user, mode, channel_id):
    """Handle channel selection for mode setup"""
    try:
        logger.info(f"handle_mode_channel_selection called - User: {user.id}, Mode: {mode}, Channel: {channel_id}")
        
        # SECURITY CHECK: Verify user owns the channel before proceeding
        if not Database.user_has_channel(user.id, channel_id):
            logger.error(f"Security violation: User {user.id} attempted to access channel {channel_id} for mode {mode}")
            await query.edit_message_text(
                "❌ *Security Error*\n\nYou don't have access to this channel.",
                parse_mode='Markdown'
            )
            return
        
        # Get channel info (now that we've verified ownership)
        channels = Database.get_user_channels(user.id)
        selected_channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
        
        if not selected_channel:
            logger.warning(f"Channel {channel_id} not found for user {user.id} after security check")
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
            
            message = f"""📸 *Mode 1: Bulk Photo Upload*

*Target Channel:* {selected_channel['channel_name']} ({selected_channel['channel_id']})

Please send me all the photos you want to schedule. You can:
• Send photos one by one
• Send multiple photos as an album
• Send as many as you need

When you're done uploading, use /schedule to set your posting schedule.
Use /cancel to abort this mode.

🔄 Ready to receive photos..."""
            
        elif mode == 2:
            Database.update_user_session(user.id, BotStates.MODE2_PHOTOS, {
                'media_items': [],
                'current_media_path': None,
                'start_time': datetime.now().isoformat(),
                'selected_channel_id': selected_channel['channel_id']
            })
            
            message = f"""📝 *Mode 2: Individual Photo Upload*

*Target Channel:* {selected_channel['channel_name']} ({selected_channel['channel_id']})

Upload photos one by one with custom descriptions:

1. Send a photo
2. I'll ask for a description
3. Repeat for each photo
4. Use /finish when done
5. Then /schedule to set posting times

Use /cancel to abort this mode.

📸 Send your first photo..."""
        
        else:  # mode == 3
            Database.update_user_session(user.id, BotStates.MODE3_UPLOADING, {
                'media_items': [],
                'start_time': datetime.now().isoformat(),
                'selected_channel_id': selected_channel['channel_id']
            })
            
            keyboard = [[InlineKeyboardButton("✅ Done Uploading", callback_data="mode3_done_uploading")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            message = f"""🎯 *Mode 3: Guided Captioning*

*Target Channel:* {selected_channel['channel_name']} ({selected_channel['channel_id']})

*Step 1: Upload all your media*

Send me all the photos/videos you want to schedule. When you're done, tap "Done Uploading" and I'll guide you through adding captions one by one with previews.

Use /cancel to abort this mode.

📸 Start uploading your media..."""
            
            await query.answer()
            await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
            return
        
        logger.info(f"About to edit message for user {user.id}, mode {mode}, channel {channel_id}")
        
        # First answer the callback to remove loading state
        try:
            await query.answer()
        except Exception as answer_error:
            logger.warning(f"Could not answer callback query: {answer_error}")
        
        # Then edit the message
        await query.edit_message_text(message, parse_mode='Markdown')
        logger.info(f"Successfully edited message and set up mode {mode} for user {user.id} on channel {channel_id}")
        
    except Exception as e:
        logger.error(f"Error in handle_mode_channel_selection: {e}", exc_info=True)
        try:
            # Answer the callback first
            await query.answer("❌ Error setting up mode", show_alert=True)
        except Exception:
            pass
        
        try:
            await query.edit_message_text(f"❌ Error setting up mode: {e}")
        except Exception as e2:
            logger.error(f"Failed to send error message: {e2}")
            # Try sending a new message if editing fails
            try:
                await query.message.reply_text(f"❌ Error setting up mode: {e}")
            except Exception as e3:
                logger.error(f"Failed to send fallback message: {e3}")

async def handle_clearscheduled_callback(query, user, data):
    """Handle clearscheduled confirmation callbacks"""
    if data == "clearscheduled_confirm_all":
        # Clear all scheduled posts
        cleared_count = Database.clear_scheduled_posts(user.id)
        
        # Also cancel the scheduled jobs from the scheduler
        try:
            # Note: This function needs context parameter to access shared scheduler
            from bot.scheduler import PostScheduler
            scheduler = PostScheduler()
            logger.warning("Using fallback scheduler instance for cancellation - may not affect active jobs")
            scheduler.cancel_user_posts(user.id)
        except Exception as e:
            logger.warning(f"Failed to cancel scheduled jobs: {e}")
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
        
        # SECURITY CHECK: Verify user owns the channel before clearing posts
        if not Database.user_has_channel(user.id, channel_id):
            logger.error(f"Security violation: User {user.id} attempted to clear scheduled posts for channel {channel_id} they don't own")
            await query.edit_message_text(
                "❌ *Security Error*\n\nYou don't have access to this channel.",
                parse_mode='Markdown'
            )
            return
        
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
        
        # SECURITY CHECK: Verify user owns the channel before retrying posts
        if not Database.user_has_channel(user.id, channel_id):
            logger.error(f"Security violation: User {user.id} attempted to retry posts for channel {channel_id} they don't own")
            await query.edit_message_text(
                "❌ *Security Error*\n\nYou don't have access to this channel.",
                parse_mode='Markdown'
            )
            return
        
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
        logger.info(f"Processing bulk edit callback: {data} for user {user.id}")
        await query.answer()
    except Exception as e:
        logger.error(f"Error answering callback query: {e}")
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
                await handle_bulk_edit_mode_channel_selection(query, user, mode, channel_id)
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

async def handle_bulk_edit_mode_channel_selection(query, user, mode, channel_id):
    """Handle selection of posts from specific mode and channel for bulk editing"""
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
    try:
        # Check if BotStates has the required attribute
        if not hasattr(BotStates, 'WAITING_BULK_EDIT_INPUT'):
            logger.error("BotStates.WAITING_BULK_EDIT_INPUT not available")
            await query.edit_message_text(
                "❌ Configuration error. Please restart the bot.",
                parse_mode='Markdown'
            )
            return
        
        Database.update_user_session(user.id, BotStates.WAITING_BULK_EDIT_INPUT, {
            'posts': [post['id'] for post in posts],
            'scope': scope_name
        })
    except Exception as e:
        logger.error(f"Error setting up bulk edit session: {e}")
        await query.edit_message_text(
            f"❌ Error setting up bulk edit: {e}",
            parse_mode='Markdown'
        )
        return
    
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
• End hour is inclusive (last post can be right at that time)
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
    
    # Get user's default schedule window configuration
    default_start, default_end, default_interval = Database.get_scheduling_config(user.id)
    
    # Enforce default schedule window constraints
    if start_hour < default_start or end_hour > default_end:
        await update.message.reply_text(
            f"❌ *Schedule window violation!*\n\n"
            f"Your request: {start_hour}:00 - {end_hour}:00\n"
            f"Your default window: {default_start}:00 - {default_end}:00\n\n"
            f"*All schedules must respect your default window.*\n"
            f"Please enter times within your configured range, or use /schedule to change your defaults.\n\n"
            f"Try again:",
            parse_mode='Markdown'
        )
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
        # Note: Database is already updated with new scheduled times.
        # The post monitoring system (runs every 5 minutes) will automatically detect 
        # posts that have scheduled_time but no active APScheduler job, and reschedule them.
        # This is more reliable than trying to access the scheduler from a message handler.
        logger.info(f"Bulk edit: Updated {len(post_schedule_updates)} posts in database. Monitor will reschedule them.")
    
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

async def backup_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /backup command - create backup of scheduled posts"""
    user = update.effective_user
    
    # Check if user has scheduled posts
    posts = Database.get_scheduled_posts_for_channel(user.id)
    
    if not posts:
        await update.message.reply_text(
            "❌ *No scheduled posts to backup!*\n\n"
            "You need to have scheduled posts before creating a backup.",
            parse_mode='Markdown'
        )
        return
    
    # Show backup creation menu
    keyboard = [
        [InlineKeyboardButton("📦 Create New Backup", callback_data="backup_create")],
        [InlineKeyboardButton("📋 View Existing Backups", callback_data="backup_list")],
        [InlineKeyboardButton("❌ Cancel", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"📦 *Backup Manager*\n\n"
        f"*Current Schedule:* {len(posts)} posts\n\n"
        f"Choose an option:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def restore_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /restore command - restore posts from backup"""
    user = update.effective_user
    
    # Get user backups
    backups = Database.get_user_backups(user.id)
    
    if not backups:
        await update.message.reply_text(
            "❌ *No backups found!*\n\n"
            "Create a backup first using /backup command.",
            parse_mode='Markdown'
        )
        return
    
    # Show backup list for restoration
    keyboard = []
    for backup in backups:
        callback_data = f"restore_select_{backup['name']}"
        display_name = backup['name']
        if len(display_name) > 25:
            display_name = display_name[:22] + "..."
        
        keyboard.append([InlineKeyboardButton(
            f"📦 {display_name} ({backup['post_count']} posts)", 
            callback_data=callback_data
        )])
    
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="back_to_main")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🔄 *Restore from Backup*\n\n"
        f"*Available Backups:* {len(backups)}\n\n"
        f"Select a backup to restore:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def handle_backup_callback(query, user, data):
    """Handle backup-related callback queries"""
    try:
        await query.answer()
    except Exception:
        pass
    
    if data == "backup_create":
        # Set state to wait for backup name
        Database.update_user_session(user.id, "waiting_backup_name")
        await query.edit_message_text(
            "📦 *Create New Backup*\n\n"
            "Enter a name for your backup:\n\n"
            "*Examples:*\n"
            "• `July Schedule`\n"
            "• `Vacation Posts`\n"
            "• `Weekly Backup`\n\n"
            "*Note:* Backup names must be unique. If a backup with the same name exists, it will be replaced.",
            parse_mode='Markdown'
        )
    
    elif data == "backup_list":
        # Show existing backups
        backups = Database.get_user_backups(user.id)
        
        if not backups:
            await query.edit_message_text(
                "📭 *No Backups Found*\n\n"
                "You haven't created any backups yet.",
                parse_mode='Markdown'
            )
            return
        
        keyboard = []
        for backup in backups:
            created_date = backup['created_at'][:10]  # YYYY-MM-DD format
            callback_data = f"backup_view_{backup['name']}"
            display_name = backup['name']
            if len(display_name) > 20:
                display_name = display_name[:17] + "..."
                
            keyboard.append([InlineKeyboardButton(
                f"📦 {display_name} - {backup['post_count']} posts ({created_date})", 
                callback_data=callback_data
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="backup_menu")])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="back_to_main")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"📋 *Your Backups*\n\n"
            f"*Total Backups:* {len(backups)}\n\n"
            f"Click on a backup to view details:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    elif data.startswith("backup_view_"):
        backup_name = data.replace("backup_view_", "")
        backups = Database.get_user_backups(user.id)
        backup = next((b for b in backups if b['name'] == backup_name), None)
        
        if backup:
            keyboard = [
                [InlineKeyboardButton("🔄 Restore This Backup", callback_data=f"restore_select_{backup_name}")],
                [InlineKeyboardButton("🗑️ Delete Backup", callback_data=f"backup_delete_{backup_name}")],
                [InlineKeyboardButton("🔙 Back to List", callback_data="backup_list")],
                [InlineKeyboardButton("❌ Cancel", callback_data="back_to_main")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"📦 *Backup Details*\n\n"
                f"*Name:* {backup['name']}\n"
                f"*Created:* {backup['created_at'][:16]}\n"
                f"*Posts:* {backup['post_count']}\n\n"
                f"What would you like to do?",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text("❌ Backup not found.")
    
    elif data.startswith("backup_delete_"):
        backup_name = data.replace("backup_delete_", "")
        
        # Show confirmation
        keyboard = [
            [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"backup_confirm_delete_{backup_name}")],
            [InlineKeyboardButton("❌ No, Cancel", callback_data=f"backup_view_{backup_name}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"🗑️ *Delete Backup*\n\n"
            f"Are you sure you want to delete backup '{backup_name}'?\n\n"
            f"⚠️ *This action cannot be undone!*",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    elif data.startswith("backup_confirm_delete_"):
        backup_name = data.replace("backup_confirm_delete_", "")
        success = Database.delete_backup(user.id, backup_name)
        
        if success:
            await query.edit_message_text(
                f"✅ *Backup Deleted*\n\n"
                f"Backup '{backup_name}' has been successfully deleted.",
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                f"❌ *Error*\n\n"
                f"Failed to delete backup '{backup_name}'.",
                parse_mode='Markdown'
            )
    
    elif data == "backup_menu":
        # Return to main backup menu
        posts = Database.get_scheduled_posts_for_channel(user.id)
        
        keyboard = [
            [InlineKeyboardButton("📦 Create New Backup", callback_data="backup_create")],
            [InlineKeyboardButton("📋 View Existing Backups", callback_data="backup_list")],
            [InlineKeyboardButton("❌ Cancel", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"📦 *Backup Manager*\n\n"
            f"*Current Schedule:* {len(posts)} posts\n\n"
            f"Choose an option:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

async def handle_restore_callback(query, user, data):
    """Handle restore-related callback queries"""
    try:
        await query.answer()
    except Exception:
        pass
    
    if data.startswith("restore_select_"):
        backup_name = data.replace("restore_select_", "")
        
        # Check current scheduled posts
        current_posts = Database.get_scheduled_posts_for_channel(user.id)
        
        keyboard = [
            [InlineKeyboardButton("🔄 Add to Current Schedule", callback_data=f"restore_add_{backup_name}")],
            [InlineKeyboardButton("🔄 Add + Include Missing Files", callback_data=f"restore_add_missing_{backup_name}")],
        ]
        
        if current_posts:
            keyboard.insert(0, [InlineKeyboardButton("🔄 Replace Current Schedule", callback_data=f"restore_replace_{backup_name}")])
            keyboard.insert(1, [InlineKeyboardButton("🔄 Replace + Include Missing Files", callback_data=f"restore_replace_missing_{backup_name}")])
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="back_to_main")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        current_info = f"{len(current_posts)} posts" if current_posts else "No posts"
        
        await query.edit_message_text(
            f"🔄 *Restore Backup: {backup_name}*\n\n"
            f"*Current Schedule:* {current_info}\n\n"
            f"How would you like to restore?\n\n"
            f"• *Replace:* Delete current posts and restore backup\n"
            f"• *Add:* Keep current posts and add backup posts\n"
            f"• *Include Missing Files:* Restore posts even if media files are missing (marked as failed)\n\n"
            f"Choose restoration mode:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    elif data.startswith("restore_replace_") or data.startswith("restore_add_"):
        replace_mode = data.startswith("restore_replace_")
        include_missing = "_missing_" in data
        
        # Extract backup name by removing all possible prefixes
        backup_name = data
        for prefix in ["restore_replace_missing_", "restore_replace_", "restore_add_missing_", "restore_add_"]:
            if backup_name.startswith(prefix):
                backup_name = backup_name[len(prefix):]
                break
        
        # Perform restore
        success, restored_count, message = Database.restore_backup(user.id, backup_name, replace_mode, include_missing)
        
        if success:
            mode_text = "replaced" if replace_mode else "added to"
            
            keyboard = [
                [InlineKeyboardButton("📅 View Calendar", callback_data="main_calendar")],
                [InlineKeyboardButton("📊 View Statistics", callback_data="main_stats")],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"✅ *Backup Restored Successfully!*\n\n"
                f"*Backup:* {backup_name}\n"
                f"*Result:* {message}\n"
                f"*Mode:* Posts {mode_text} your schedule\n\n"
                f"Your posts have been restored and will be posted according to their original schedule.",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                f"❌ *Restore Failed*\n\n"
                f"*Error:* {message}\n\n"
                f"Please try again or contact support if the issue persists.",
                parse_mode='Markdown'
            )

async def handle_backup_name_input(update: Update, user, text: str):
    """Handle backup name input"""
    backup_name = text.strip()
    
    if not backup_name:
        await update.message.reply_text(
            "❌ Backup name cannot be empty. Please enter a valid name:"
        )
        return
    
    if len(backup_name) > 50:
        await update.message.reply_text(
            "❌ Backup name too long (max 50 characters). Please enter a shorter name:"
        )
        return
    
    # Create backup
    success = Database.create_backup(user.id, backup_name)
    
    if success:
        keyboard = [
            [InlineKeyboardButton("📋 View Backups", callback_data="backup_list")],
            [InlineKeyboardButton("📦 Create Another", callback_data="backup_create")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"✅ *Backup Created Successfully!*\n\n"
            f"*Name:* {backup_name}\n"
            f"*Posts Backed Up:* {len(Database.get_scheduled_posts_for_channel(user.id))}\n\n"
            f"Your scheduled posts are now safely backed up and can be restored anytime.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            f"❌ *Backup Failed*\n\n"
            f"Could not create backup '{backup_name}'. This might be due to:\n"
            f"• Backup name already exists\n"
            f"• Database error\n\n"
            f"Please try again with a different name:"
        )
    
    # Reset user session
    Database.update_user_session(user.id, BotStates.IDLE)

async def overdue_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /overdue command - show and manage overdue posts"""
    user = update.effective_user
    
    # Get overdue posts for the user
    overdue_posts = Database.get_overdue_posts(user.id)
    
    if not overdue_posts:
        keyboard = [
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "✅ *No Overdue Posts*\n\n"
            "All your scheduled posts are on time! There are no posts that have missed their scheduled time.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return
    
    # Group overdue posts by channel
    channels_with_overdue = {}
    for post in overdue_posts:
        channel_id = post['channel_id']
        if channel_id not in channels_with_overdue:
            channels_with_overdue[channel_id] = {
                'posts': [],
                'channel_name': channel_id  # Will be updated below
            }
        channels_with_overdue[channel_id]['posts'].append(post)
    
    # Get channel names
    user_channels = Database.get_user_channels(user.id)
    channel_names = {channel['channel_id']: channel['channel_name'] for channel in user_channels}
    
    for channel_id in channels_with_overdue:
        if channel_id in channel_names:
            channels_with_overdue[channel_id]['channel_name'] = channel_names[channel_id]
    
    # Create inline keyboard for channel selection
    keyboard = []
    
    for channel_id, channel_data in channels_with_overdue.items():
        channel_name = channel_data['channel_name']
        post_count = len(channel_data['posts'])
        keyboard.append([
            InlineKeyboardButton(
                f"📺 {channel_name} ({post_count} overdue)",
                callback_data=f"overdue_channel_{channel_id}"
            )
        ])
    
    keyboard.extend([
        [InlineKeyboardButton("🔄 Reschedule All", callback_data="overdue_reschedule_all")],
        [InlineKeyboardButton("📬 Post All Now", callback_data="overdue_post_all")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    total_overdue = len(overdue_posts)
    channel_summary = []
    for channel_id, channel_data in channels_with_overdue.items():
        channel_name = channel_data['channel_name']
        count = len(channel_data['posts'])
        channel_summary.append(f"• *{channel_name}:* {count} posts")
    
    message = f"""
⏰ *Overdue Posts Found*

*Total overdue posts:* {total_overdue}

*Breakdown by channel:*
{chr(10).join(channel_summary)}

*What would you like to do?*

• *Select a channel* to manage individual posts
• *Reschedule All* to move all posts to next available slots
• *Post All Now* to immediately post all overdue content

Choose an option below:
"""
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_overdue_callback(query, user, data):
    """Handle overdue post management callbacks"""
    action = data.replace("overdue_", "")
    
    if action == "reschedule_all":
        # Reschedule all overdue posts
        overdue_posts = Database.get_overdue_posts(user.id)
        if not overdue_posts:
            await query.edit_message_text("✅ No overdue posts found.")
            return
        
        post_ids = [post['id'] for post in overdue_posts]
        updated_count = Database.reschedule_overdue_posts_to_next_slots(user.id, post_ids)
        
        if updated_count > 0:
            keyboard = [
                [InlineKeyboardButton("📊 View Stats", callback_data="main_stats")],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"✅ *Rescheduling Complete*\n\n"
                f"*Posts rescheduled:* {updated_count}\n"
                f"*Status:* All overdue posts moved to next available time slots\n"
                f"*Queue:* Existing scheduled posts automatically shifted forward\n\n"
                f"Your posting schedule has been updated to accommodate the overdue content.",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text("❌ Failed to reschedule posts. Please try again.")
    
    elif action == "post_all":
        # Post all overdue posts immediately
        overdue_posts = Database.get_overdue_posts(user.id)
        if not overdue_posts:
            await query.edit_message_text("✅ No overdue posts found.")
            return
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Yes, Post All", callback_data="overdue_confirm_post_all"),
                InlineKeyboardButton("❌ Cancel", callback_data="overdue_main")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"⚠️ *Confirm Immediate Posting*\n\n"
            f"*Posts to be posted:* {len(overdue_posts)}\n"
            f"*Action:* All overdue posts will be posted immediately\n\n"
            f"This action cannot be undone. Are you sure?",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    elif action == "confirm_post_all":
        # Actually post all overdue posts immediately using scheduler
        overdue_posts = Database.get_overdue_posts(user.id)
        posted_count = 0
        failed_count = 0
        
        # Actually post all overdue posts immediately using scheduler
        # Note: This callback function needs access to context to get the shared scheduler
        from .scheduler import PostScheduler
        scheduler = PostScheduler()
        logger.warning("Using fallback scheduler instance for overdue posting - using private method")
        
        for post in overdue_posts:
            try:
                # Actually post the content to Telegram
                await scheduler._post_to_channel(post['id'])
                posted_count += 1
            except Exception as e:
                logger.error(f"Failed to post overdue post {post['id']}: {e}")
                failed_count += 1
        
        keyboard = [
            [InlineKeyboardButton("📊 View Stats", callback_data="main_stats")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        status_message = f"✅ *Immediate Posting Complete*\n\n"
        if posted_count > 0:
            status_message += f"*Successfully processed:* {posted_count} posts\n"
        if failed_count > 0:
            status_message += f"*Failed to process:* {failed_count} posts\n"
        status_message += f"\nAll overdue content has been processed."
        
        await query.edit_message_text(
            status_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    elif action.startswith("post_channel_"):
        # Post all overdue posts for a specific channel immediately
        channel_id = action.replace("post_channel_", "")
        overdue_posts = Database.get_overdue_posts(user.id, channel_id)
        if not overdue_posts:
            await query.edit_message_text("✅ No overdue posts found for this channel.")
            return

        from .scheduler import PostScheduler
        scheduler = PostScheduler()
        logger.warning("Using fallback scheduler instance for channel overdue posting")
        posted_count = 0
        failed_count = 0
        for post in overdue_posts:
            try:
                await scheduler._post_to_channel(post['id'])
                posted_count += 1
            except Exception as e:
                logger.error(f"Failed to post overdue post {post['id']}: {e}")
                failed_count += 1

        keyboard = [
            [InlineKeyboardButton("🔄 Refresh Overdue", callback_data="overdue_refresh")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        status_message = f"✅ *Channel Posting Complete*\n\n"
        if posted_count > 0:
            status_message += f"*Successfully processed:* {posted_count} posts\n"
        if failed_count > 0:
            status_message += f"*Failed:* {failed_count} posts\n"
        await query.edit_message_text(status_message, reply_markup=reply_markup, parse_mode='Markdown')

    elif action.startswith("post_") and not action == "post_all":
        # Post a specific overdue post immediately
        try:
            post_id = int(action.replace("post_", ""))
        except ValueError:
            logger.error(f"Invalid overdue post action: {action}")
            await query.edit_message_text("❌ Invalid action. Please try again.")
            return
        
        try:
            # Actually post the content to Telegram instead of just marking as posted
            from .scheduler import PostScheduler
            scheduler = PostScheduler()
            logger.warning("Using fallback scheduler instance for individual overdue posting")
            await scheduler._post_to_channel(post_id)
            
            keyboard = [
                [InlineKeyboardButton("🔄 Refresh Overdue", callback_data="overdue_refresh")],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"✅ *Post Processed*\n\n"
                f"*Post ID:* {post_id}\n"
                f"*Status:* Marked as posted\n\n"
                f"The overdue post has been processed.",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Failed to process overdue post {post_id}: {e}")
            await query.edit_message_text(
                f"❌ *Failed to Process Post*\n\n"
                f"*Post ID:* {post_id}\n"
                f"*Error:* Could not process the post\n\n"
                f"Please try again."
            )
    
    elif action.startswith("reschedule_channel_"):
        # Reschedule all overdue posts for a specific channel
        channel_id = action.replace("reschedule_channel_", "")
        overdue_posts = Database.get_overdue_posts(user.id, channel_id)
        if not overdue_posts:
            await query.edit_message_text("✅ No overdue posts found for this channel.")
            return
        post_ids = [post['id'] for post in overdue_posts]
        updated_count = Database.reschedule_overdue_posts_to_next_slots(user.id, post_ids, channel_id)
        keyboard = [
            [InlineKeyboardButton("🔄 Refresh Overdue", callback_data="overdue_refresh")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if updated_count > 0:
            await query.edit_message_text(
                f"✅ *Rescheduling Complete*\n\n*Posts rescheduled:* {updated_count}",
                reply_markup=reply_markup, parse_mode='Markdown'
            )
        else:
            await query.edit_message_text("❌ Failed to reschedule posts. Please try again.")

    elif action.startswith("reschedule_"):
        # Reschedule a specific overdue post
        try:
            post_id = int(action.replace("reschedule_", ""))
        except ValueError:
            logger.error(f"Invalid overdue reschedule action: {action}")
            await query.edit_message_text("❌ Invalid action. Please try again.")
            return
        
        updated_count = Database.reschedule_overdue_posts_to_next_slots(user.id, [post_id])
        
        if updated_count > 0:
            keyboard = [
                [InlineKeyboardButton("🔄 Refresh Overdue", callback_data="overdue_refresh")],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"✅ *Post Rescheduled*\n\n"
                f"*Post ID:* {post_id}\n"
                f"*Status:* Moved to next available time slot\n\n"
                f"The post will be published at its new scheduled time.",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                f"❌ *Failed to Reschedule*\n\n"
                f"*Post ID:* {post_id}\n"
                f"Could not reschedule the post. Please try again."
            )
    
    elif action == "main":
        # Return to main overdue view - need to create a new message since we can't call command handler
        keyboard = [
            [InlineKeyboardButton("🔄 Check Overdue", callback_data="overdue_refresh")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "⏰ *Overdue Posts Management*\n\n"
            "Use 'Check Overdue' to see any posts that have missed their scheduled time.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    elif action == "refresh":
        # Refresh the overdue view - simulate calling the handler
        keyboard = [
            [InlineKeyboardButton("🔄 Check Again", callback_data="overdue_refresh")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "⏰ *Overdue Posts Management*\n\n"
            "Use 'Check Again' to refresh the overdue posts list.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    elif action.startswith("channel_"):
        # Show individual overdue posts for a specific channel
        channel_id = action.replace("channel_", "")
        await show_channel_overdue_posts(query, user, channel_id)

async def show_channel_overdue_posts(query, user, channel_id: str):
    """Show individual overdue posts for a specific channel"""
    overdue_posts = Database.get_overdue_posts(user.id, channel_id)
    
    if not overdue_posts:
        keyboard = [
            [InlineKeyboardButton("🔄 Refresh", callback_data="overdue_refresh")],
            [InlineKeyboardButton("🔙 Back", callback_data="overdue_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "✅ *No Overdue Posts*\n\n"
            "This channel has no overdue posts.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return
    
    # Get channel name
    user_channels = Database.get_user_channels(user.id)
    channel_name = channel_id
    for channel in user_channels:
        if channel['channel_id'] == channel_id:
            channel_name = channel['channel_name']
            break
    
    # Create keyboard with individual post controls
    keyboard = []
    
    from .utils import get_media_icon
    for i, post in enumerate(overdue_posts[:10]):  # Limit to 10 posts to avoid message size issues
        post_id = post['id']
        media_icon = get_media_icon(post['media_type'])
        scheduled_time = post['scheduled_time']
        time_str = scheduled_time.strftime("%m/%d %H:%M") if scheduled_time else "Unknown"
        
        # Add buttons for each post
        keyboard.append([
            InlineKeyboardButton(f"📬 Post #{post_id}", callback_data=f"overdue_post_{post_id}"),
            InlineKeyboardButton(f"🔄 Reschedule #{post_id}", callback_data=f"overdue_reschedule_{post_id}")
        ])
    
    # Add bulk actions for this channel
    keyboard.extend([
        [
            InlineKeyboardButton(f"🔄 Reschedule All ({len(overdue_posts)})", callback_data=f"overdue_reschedule_channel_{channel_id}"),
            InlineKeyboardButton(f"📬 Post All ({len(overdue_posts)})", callback_data=f"overdue_post_channel_{channel_id}")
        ],
        [InlineKeyboardButton("🔙 Back to Channels", callback_data="overdue_main")]
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = f"""
📺 *{channel_name} - Overdue Posts*

*Total overdue:* {len(overdue_posts)}

For each post, you can:
• *Post* - Publish immediately
• *Reschedule* - Move to next available slot

Or use bulk actions for all posts in this channel.

*Individual Posts:*
"""
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_main_overdue_callback(query, user):
    """Handle main menu overdue callback"""
    # Check for overdue posts
    overdue_posts = Database.get_overdue_posts(user.id)
    
    if not overdue_posts:
        keyboard = [
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "✅ *No Overdue Posts*\n\n"
            "All your scheduled posts are on time! There are no posts that have missed their scheduled time.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return
    
    # Show overdue posts summary with management options
    channels_with_overdue = {}
    for post in overdue_posts:
        channel_id = post['channel_id']
        if channel_id not in channels_with_overdue:
            channels_with_overdue[channel_id] = []
        channels_with_overdue[channel_id].append(post)
    
    # Get channel names
    user_channels = Database.get_user_channels(user.id)
    channel_names = {channel['channel_id']: channel['channel_name'] for channel in user_channels}
    
    # Create inline keyboard for channel selection
    keyboard = []
    
    for channel_id, posts in channels_with_overdue.items():
        channel_name = channel_names.get(channel_id, channel_id)
        post_count = len(posts)
        keyboard.append([
            InlineKeyboardButton(
                f"📺 {channel_name} ({post_count} overdue)",
                callback_data=f"overdue_channel_{channel_id}"
            )
        ])
    
    keyboard.extend([
        [InlineKeyboardButton("🔄 Reschedule All", callback_data="overdue_reschedule_all")],
        [InlineKeyboardButton("📬 Post All Now", callback_data="overdue_post_all")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    total_overdue = len(overdue_posts)
    channel_summary = []
    for channel_id, posts in channels_with_overdue.items():
        channel_name = channel_names.get(channel_id, channel_id)
        count = len(posts)
        channel_summary.append(f"• *{channel_name}:* {count} posts")
    
    message = f"""
⏰ *Overdue Posts Found*

*Total overdue posts:* {total_overdue}

*Breakdown by channel:*
{chr(10).join(channel_summary)}

*What would you like to do?*

• *Select a channel* to manage individual posts
• *Reschedule All* to move all posts to next available slots
• *Post All Now* to immediately post all overdue content

Choose an option below:
"""
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')


# Reschedule handlers
async def handle_reschedule_callback(query, user):
    """Handle reschedule all posts callback"""
    from bot.database import Database
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    
    # Check if user has channels configured
    channels = Database.get_user_channels(user.id)
    
    if not channels:
        await query.edit_message_text(
            "❌ *No channels configured!*\\n\\n"
            "Please add a channel first using /channels command before rescheduling.",
            parse_mode="Markdown"
        )
        return
    
    # Check for pending posts
    pending_posts = Database.get_pending_posts(user.id)
    
    if not pending_posts:
        keyboard = [
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "📅 *No Pending Posts*\\n\\n"
            "You dont have any pending posts to reschedule.\\n\\n"
            "Upload some posts first, then come back to reschedule them!",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return
    
    # Show reschedule options
    keyboard = [
        [InlineKeyboardButton("🔁 All Posts", callback_data="reschedule_all")],
        [InlineKeyboardButton("⚙️ Custom Hours", callback_data="reschedule_custom")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = f"""
🔁 *Reschedule All Posts*

*Current Status:*
• *Pending Posts:* {len(pending_posts)}
• *Channels:* {len(channels)}

*Reschedule Options:*

🔁 **All Posts** - Reschedule all pending posts starting from today using default schedule (10 AM - 8 PM, 2 hour intervals)

⚙️ **Custom Hours** - Set custom start time, end time, and intervals for rescheduling

*Note:* This will reschedule ALL pending posts starting from today with the new schedule. Current scheduled times will be replaced.
"""
    
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode="Markdown")



async def handle_reschedule_action_callback(query, user, data, context):
    """Handle reschedule action callbacks"""
    from bot.database import Database
    
    action = data.replace("reschedule_", "")
    
    if action == "all":
        # Reschedule all posts with default settings
        try:
            scheduler = context.bot_data.get('scheduler')

            if scheduler:
                rescheduled_count = await scheduler.reschedule_all_posts_from_today(
                    user.id,
                    start_hour=10,
                    end_hour=20,
                    interval_hours=2
                )
            else:
                logger.warning("Scheduler not available in bot_data during reschedule_all action; falling back to database-only update")
                rescheduled_count = Database.reschedule_all_posts_from_today(
                    user.id,
                    start_hour=10,
                    end_hour=20,
                    interval_hours=2
                )

            if rescheduled_count > 0:
                logger.info(f"Rescheduled {rescheduled_count} posts for user {user.id} with default settings")
                
                keyboard = [
                    [InlineKeyboardButton("📊 View Stats", callback_data="main_stats")],
                    [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                message = f"""
✅ *Rescheduling Complete!*

*Results:*
• *Posts Rescheduled:* {rescheduled_count}
• *New Schedule:* 10 AM - 8 PM (Kyiv time)
• *Interval:* Every 2 hours
• *Start Date:* Today or tomorrow

All your pending posts have been rescheduled with the new times starting from today!
"""
            else:
                keyboard = [
                    [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                message = """
❌ *No Posts to Reschedule*

No pending posts were found to reschedule.
"""
            
            await query.edit_message_text(message, reply_markup=reply_markup, parse_mode="Markdown")
            
        except Exception as e:
            logger.error(f"Error during reschedule: {e}")
            keyboard = [
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"❌ *Error during rescheduling:*\n\n{str(e)}",
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            
    elif action == "custom":
        # Set user state for custom reschedule input  
        Database.update_user_session(user.id, "awaiting_reschedule_settings", {})
        
        keyboard = [
            [InlineKeyboardButton("🔙 Cancel", callback_data="main_reschedule")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message = """
⚙️ *Custom Reschedule Settings*

Please enter your custom schedule settings in this format:

`start_hour end_hour interval_hours`

*Examples:*
• `9 18 3` - 9 AM to 6 PM, every 3 hours
• `8 22 1` - 8 AM to 10 PM, every hour  
• `12 16 2` - 12 PM to 4 PM, every 2 hours

*Send your settings now:*
"""
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode="Markdown")



async def handle_reschedule_settings_input(update, user, text, context):
    """Handle custom reschedule settings input"""
    from bot.database import Database
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    
    try:
        # Parse the input (start_hour end_hour interval_hours)
        parts = text.strip().split()
        
        if len(parts) != 3:
            await update.message.reply_text(
                "❌ *Invalid format!*\\n\\n"
                "Please use format: `start_hour end_hour interval_hours`\\n\\n"
                "*Example:* `9 18 3`",
                parse_mode="Markdown"
            )
            return
        
        start_hour = int(parts[0])
        end_hour = int(parts[1])
        interval_hours = int(parts[2])
        
        # Validate input
        if not (0 <= start_hour <= 23):
            await update.message.reply_text("❌ Start hour must be between 0-23")
            return
            
        if not (0 <= end_hour <= 23):
            await update.message.reply_text("❌ End hour must be between 0-23")
            return
            
        if start_hour >= end_hour:
            await update.message.reply_text("❌ Start hour must be less than end hour")
            return
        
        # Calculate the time window
        time_window = end_hour - start_hour
        
        if not (1 <= interval_hours <= time_window):
            await update.message.reply_text(
                f"❌ Interval must be between 1-{time_window} hours for your time window ({start_hour}:00-{end_hour}:00)"
            )
            return
        
        # Clear user session
        Database.update_user_session(user.id, "idle", {})
        
        scheduler = context.bot_data.get('scheduler')

        if scheduler:
            rescheduled_count = await scheduler.reschedule_all_posts_from_today(
                user.id,
                start_hour=start_hour,
                end_hour=end_hour,
                interval_hours=interval_hours
            )
        else:
            logger.warning("Scheduler not available in bot_data during custom reschedule; falling back to database-only update")
            rescheduled_count = Database.reschedule_all_posts_from_today(
                user.id,
                start_hour=start_hour,
                end_hour=end_hour,
                interval_hours=interval_hours
            )

        if rescheduled_count > 0:
            logger.info(f"Rescheduled {rescheduled_count} posts for user {user.id} with custom settings {start_hour}-{end_hour}/{interval_hours}h")
        
        keyboard = [
            [InlineKeyboardButton("📊 View Stats", callback_data="main_stats")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if rescheduled_count > 0:
            message = f"""
✅ *Custom Rescheduling Complete!*

*Results:*
• *Posts Rescheduled:* {rescheduled_count}
• *New Schedule:* {start_hour}:00 - {end_hour}:00 (Kyiv time)
• *Interval:* Every {interval_hours} hour(s)
• *Start Date:* Today or tomorrow

All your pending posts have been rescheduled with your custom settings!
"""
        else:
            message = """
❌ *No Posts to Reschedule*

No pending posts were found to reschedule.
"""
        
        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")
        
    except ValueError:
        await update.message.reply_text(
            "❌ *Invalid numbers!*\\n\\n"
            "Please enter valid numbers for hours.\\n\\n"
            "*Example:* `9 18 3`",
            parse_mode="Markdown"
        )
    except Exception as e:
        # Clear user session
        Database.update_user_session(user.id, "idle", {})
        
        keyboard = [
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"❌ *Error during rescheduling:*\\n\\n{str(e)}",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )


async def settings_handler(update, context):
    """Handle /settings command for reminder configuration"""
    user = update.effective_user
    
    # Get current settings
    enabled, threshold, last_sent = Database.get_reminder_settings(user.id)
    
    # Create inline keyboard for settings
    keyboard = [
        [
            InlineKeyboardButton(
                f"🔔 Reminders: {'ON' if enabled else 'OFF'}", 
                callback_data=f"settings_toggle_reminder"
            )
        ],
        [
            InlineKeyboardButton("➖", callback_data="settings_threshold_dec"),
            InlineKeyboardButton(f"Threshold: {threshold} posts", callback_data="settings_threshold_info"),
            InlineKeyboardButton("➕", callback_data="settings_threshold_inc")
        ],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Format last reminder time
    last_reminder_text = "Never"
    if last_sent:
        from datetime import datetime
        time_since = datetime.now() - last_sent
        hours_ago = int(time_since.total_seconds() / 3600)
        if hours_ago < 1:
            last_reminder_text = "Less than 1 hour ago"
        elif hours_ago < 24:
            last_reminder_text = f"{hours_ago} hours ago"
        else:
            days_ago = hours_ago // 24
            last_reminder_text = f"{days_ago} days ago"
    
    message = f"""
⚙️ *Reminder Settings*

*Current Configuration:*
• *Reminders:* {'Enabled ✅' if enabled else 'Disabled ❌'}
• *Alert Threshold:* {threshold} posts
• *Last Reminder:* {last_reminder_text}

*How it works:*
When your unscheduled posts drop to or below {threshold}, you'll receive a reminder notification.

Reminders are checked hourly and sent maximum once per day.

Use the buttons below to adjust your settings:
"""
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')


async def handle_settings_callback(query, user, data):
    """Handle settings callback interactions"""
    action = data.replace("settings_", "")
    
    if action == "toggle_reminder":
        # Toggle reminder status
        enabled, threshold, _ = Database.get_reminder_settings(user.id)
        new_enabled = not enabled
        Database.update_reminder_settings(user.id, enabled=new_enabled)
        
        # Update the message
        keyboard = [
            [
                InlineKeyboardButton(
                    f"🔔 Reminders: {'ON' if new_enabled else 'OFF'}", 
                    callback_data=f"settings_toggle_reminder"
                )
            ],
            [
                InlineKeyboardButton("➖", callback_data="settings_threshold_dec"),
                InlineKeyboardButton(f"Threshold: {threshold} posts", callback_data="settings_threshold_info"),
                InlineKeyboardButton("➕", callback_data="settings_threshold_inc")
            ],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Format last reminder time
        _, _, last_sent = Database.get_reminder_settings(user.id)
        last_reminder_text = "Never"
        if last_sent:
            from datetime import datetime
            time_since = datetime.now() - last_sent
            hours_ago = int(time_since.total_seconds() / 3600)
            if hours_ago < 1:
                last_reminder_text = "Less than 1 hour ago"
            elif hours_ago < 24:
                last_reminder_text = f"{hours_ago} hours ago"
            else:
                days_ago = hours_ago // 24
                last_reminder_text = f"{days_ago} days ago"
        
        message = f"""
⚙️ *Reminder Settings*

*Current Configuration:*
• *Reminders:* {'Enabled ✅' if new_enabled else 'Disabled ❌'}
• *Alert Threshold:* {threshold} posts
• *Last Reminder:* {last_reminder_text}

*How it works:*
When your unscheduled posts drop to or below {threshold}, you'll receive a reminder notification.

Reminders are checked hourly and sent maximum once per day.

Use the buttons below to adjust your settings:
"""
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        
    elif action == "threshold_inc":
        # Increase threshold
        enabled, threshold, _ = Database.get_reminder_settings(user.id)
        new_threshold = min(threshold + 1, 50)  # Max 50
        Database.update_reminder_settings(user.id, threshold=new_threshold)
        
        # Update the message
        keyboard = [
            [
                InlineKeyboardButton(
                    f"🔔 Reminders: {'ON' if enabled else 'OFF'}", 
                    callback_data=f"settings_toggle_reminder"
                )
            ],
            [
                InlineKeyboardButton("➖", callback_data="settings_threshold_dec"),
                InlineKeyboardButton(f"Threshold: {new_threshold} posts", callback_data="settings_threshold_info"),
                InlineKeyboardButton("➕", callback_data="settings_threshold_inc")
            ],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Format last reminder time
        _, _, last_sent = Database.get_reminder_settings(user.id)
        last_reminder_text = "Never"
        if last_sent:
            from datetime import datetime
            time_since = datetime.now() - last_sent
            hours_ago = int(time_since.total_seconds() / 3600)
            if hours_ago < 1:
                last_reminder_text = "Less than 1 hour ago"
            elif hours_ago < 24:
                last_reminder_text = f"{hours_ago} hours ago"
            else:
                days_ago = hours_ago // 24
                last_reminder_text = f"{days_ago} days ago"
        
        message = f"""
⚙️ *Reminder Settings*

*Current Configuration:*
• *Reminders:* {'Enabled ✅' if enabled else 'Disabled ❌'}
• *Alert Threshold:* {new_threshold} posts
• *Last Reminder:* {last_reminder_text}

*How it works:*
When your unscheduled posts drop to or below {new_threshold}, you'll receive a reminder notification.

Reminders are checked hourly and sent maximum once per day.

Use the buttons below to adjust your settings:
"""
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        
    elif action == "threshold_dec":
        # Decrease threshold
        enabled, threshold, _ = Database.get_reminder_settings(user.id)
        new_threshold = max(threshold - 1, 1)  # Min 1
        Database.update_reminder_settings(user.id, threshold=new_threshold)
        
        # Update the message
        keyboard = [
            [
                InlineKeyboardButton(
                    f"🔔 Reminders: {'ON' if enabled else 'OFF'}", 
                    callback_data=f"settings_toggle_reminder"
                )
            ],
            [
                InlineKeyboardButton("➖", callback_data="settings_threshold_dec"),
                InlineKeyboardButton(f"Threshold: {new_threshold} posts", callback_data="settings_threshold_info"),
                InlineKeyboardButton("➕", callback_data="settings_threshold_inc")
            ],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Format last reminder time
        _, _, last_sent = Database.get_reminder_settings(user.id)
        last_reminder_text = "Never"
        if last_sent:
            from datetime import datetime
            time_since = datetime.now() - last_sent
            hours_ago = int(time_since.total_seconds() / 3600)
            if hours_ago < 1:
                last_reminder_text = "Less than 1 hour ago"
            elif hours_ago < 24:
                last_reminder_text = f"{hours_ago} hours ago"
            else:
                days_ago = hours_ago // 24
                last_reminder_text = f"{days_ago} days ago"
        
        message = f"""
⚙️ *Reminder Settings*

*Current Configuration:*
• *Reminders:* {'Enabled ✅' if enabled else 'Disabled ❌'}
• *Alert Threshold:* {new_threshold} posts
• *Last Reminder:* {last_reminder_text}

*How it works:*
When your unscheduled posts drop to or below {new_threshold}, you'll receive a reminder notification.

Reminders are checked hourly and sent maximum once per day.

Use the buttons below to adjust your settings:
"""
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        
    elif action == "threshold_info":
        # Just show info, no change
        await query.answer("Threshold determines when you receive low post alerts", show_alert=True)


# ============== COMPREHENSIVE POST EDITING MENU ==============

async def editposts_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /editposts command - show comprehensive edit menu for Mode 2 posts"""
    if not update or not update.effective_user or not update.message:
        logger.error("Invalid update in editposts_handler")
        return
    
    user = update.effective_user
    logger.info(f"editposts_handler called for user {user.id}")
    
    # Get user's channels
    channels = Database.get_user_channels(user.id)
    
    if not channels:
        await update.message.reply_text(
            "❌ *No channels configured!*\n\n"
            "Please add a channel first using /channels command.",
            parse_mode='Markdown'
        )
        return
    
    # Show channel selection for editing
    keyboard = []
    for channel in channels:
        # Count Mode 2 scheduled posts for this channel
        posts = Database.get_user_mode2_scheduled_posts(user.id, channel['channel_id'])
        post_count = len(posts)
        
        keyboard.append([InlineKeyboardButton(
            f"📺 {channel['channel_name']} ({post_count} posts)",
            callback_data=f"editposts_channel_{channel['channel_id']}"
        )])
    
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "✏️ *Edit Scheduled Posts*\n\n"
        "Select a channel to edit its scheduled Mode 2 posts:\n\n"
        "*Features:*\n"
        "• Edit captions (with formatting preserved)\n"
        "• Replace media files\n"
        "• Reschedule posts\n"
        "• Delete posts\n"
        "• Preview media before changes",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def handle_editposts_channel_selection(query, user, channel_id):
    """Handle channel selection in edit posts menu"""
    try:
        logger.info(f"handle_editposts_channel_selection for user {user.id}, channel {channel_id}")
        
        # Verify user owns the channel
        if not Database.user_has_channel(user.id, channel_id):
            await query.edit_message_text("❌ You don't have access to this channel.")
            return
        
        # Get channel info
        channels = Database.get_user_channels(user.id)
        channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
        channel_name = channel['channel_name'] if channel else channel_id
        
        # Get Mode 2 posts for this channel
        posts = Database.get_user_mode2_scheduled_posts(user.id, channel_id)
        
        if not posts:
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="editposts_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"✏️ *Edit Posts - {channel_name}*\n\n"
                "No scheduled Mode 2 posts found for editing.",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return
        
        # Store the posts in session for navigation
        post_ids = [p['id'] for p in posts]
        session_data = {
            'edit_channel_id': channel_id,
            'edit_post_ids': post_ids,
            'edit_current_index': 0
        }
        Database.update_user_session(user.id, BotStates.EDIT_POSTS_MENU, session_data)
        
        # Show first post
        await show_edit_post_details(query, user, posts[0], 0, len(posts), channel_name)
        
    except Exception as e:
        logger.error(f"Error in handle_editposts_channel_selection: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error loading posts: {e}")


async def show_edit_post_details(query, user, post, current_index, total_posts, channel_name):
    """Show detailed edit interface for a single post with navigation"""
    try:
        post_id = post['id']
        media_type = post.get('media_type', 'photo')
        description = post.get('description') or 'No caption'
        scheduled_time = post.get('scheduled_time')
        
        # Format scheduled time
        if scheduled_time:
            if isinstance(scheduled_time, str):
                scheduled_dt = datetime.fromisoformat(scheduled_time)
            else:
                scheduled_dt = scheduled_time
            time_str = scheduled_dt.strftime("%Y-%m-%d %H:%M")
        else:
            time_str = "Not scheduled"
        
        media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄', 
                      'document_image': '📸', 'document_video': '🎥'}.get(media_type, '📁')
        
        # Truncate long descriptions for display
        display_desc = description[:100] + "..." if len(description) > 100 else description
        
        message = f"✏️ *Edit Post {current_index + 1}/{total_posts}*\n\n"
        message += f"*📺 Channel:* {channel_name}\n"
        message += f"*📁 Type:* {media_icon} {media_type.replace('document_', '').title()}\n"
        message += f"*📝 Caption:* {escape_markdown(display_desc)}\n"
        message += f"*⏰ Scheduled:* {time_str}\n\n"
        message += "*Select an action:*"
        
        # Build keyboard with all editing options
        keyboard = []
        
        # Navigation row (only if multiple posts)
        if total_posts > 1:
            nav_row = []
            if current_index > 0:
                nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"editposts_nav_{current_index - 1}"))
            nav_row.append(InlineKeyboardButton(f"{current_index + 1}/{total_posts}", callback_data="editposts_info"))
            if current_index < total_posts - 1:
                nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"editposts_nav_{current_index + 1}"))
            keyboard.append(nav_row)
        
        # Edit options
        keyboard.append([InlineKeyboardButton("👁️ Preview Media", callback_data=f"editposts_preview_{post_id}")])
        keyboard.append([InlineKeyboardButton("📝 Edit Caption", callback_data=f"editposts_caption_{post_id}")])
        keyboard.append([InlineKeyboardButton("🖼️ Replace Media", callback_data=f"editposts_media_{post_id}")])
        keyboard.append([InlineKeyboardButton("⏰ Reschedule", callback_data=f"editposts_schedule_{post_id}")])
        keyboard.append([InlineKeyboardButton("🗑️ Delete Post", callback_data=f"editposts_delete_{post_id}")])
        keyboard.append([InlineKeyboardButton("🔙 Back to Channels", callback_data="editposts_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error in show_edit_post_details: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error displaying post: {e}")


async def handle_editposts_navigation(query, user, new_index):
    """Handle navigation between posts in edit menu"""
    try:
        # Get session data
        mode, session_data = Database.get_user_session(user.id)
        
        if not session_data or 'edit_post_ids' not in session_data:
            await query.edit_message_text("❌ Session expired. Please use /editposts again.")
            return
        
        post_ids = session_data['edit_post_ids']
        channel_id = session_data['edit_channel_id']
        
        if new_index < 0 or new_index >= len(post_ids):
            await query.answer("No more posts in this direction")
            return
        
        # Update session with new index
        session_data['edit_current_index'] = new_index
        Database.update_user_session(user.id, BotStates.EDIT_POSTS_MENU, session_data)
        
        # Get post data
        post = Database.get_post_by_id(post_ids[new_index])
        if not post:
            await query.edit_message_text("❌ Post not found. It may have been deleted.")
            return
        
        # Get channel name
        channels = Database.get_user_channels(user.id)
        channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
        channel_name = channel['channel_name'] if channel else channel_id
        
        await show_edit_post_details(query, user, post, new_index, len(post_ids), channel_name)
        
    except Exception as e:
        logger.error(f"Error in handle_editposts_navigation: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error navigating: {e}")


async def handle_editposts_preview(query, user, post_id):
    """Send media preview to user"""
    try:
        post = Database.get_post_by_id(post_id)
        
        if not post or post['user_id'] != user.id:
            await query.answer("Post not found or access denied", show_alert=True)
            return
        
        file_path = post['file_path']
        media_type = post['media_type'] or 'photo'
        description = post['description'] or 'No caption'
        
        if not os.path.exists(file_path):
            await query.answer("Media file not found on server", show_alert=True)
            return
        
        # Send the media as a preview - escape user description to avoid Markdown parsing errors
        escaped_description = escape_markdown(description)
        preview_caption = f"*Preview Post #{post_id}*\n{escaped_description}"
        
        with open(file_path, 'rb') as media_file:
            if media_type == 'photo':
                await query.message.reply_photo(photo=media_file, caption=preview_caption, parse_mode='Markdown')
            elif media_type == 'video':
                await query.message.reply_video(video=media_file, caption=preview_caption, parse_mode='Markdown')
            elif media_type == 'audio':
                await query.message.reply_audio(audio=media_file, caption=preview_caption, parse_mode='Markdown')
            elif media_type == 'animation':
                await query.message.reply_animation(animation=media_file, caption=preview_caption, parse_mode='Markdown')
            else:
                await query.message.reply_document(document=media_file, caption=preview_caption, parse_mode='Markdown')
        
        await query.answer("Preview sent!")
        
    except Exception as e:
        logger.error(f"Error in handle_editposts_preview: {e}", exc_info=True)
        await query.answer(f"Error sending preview: {e}", show_alert=True)


async def handle_editposts_caption(query, user, post_id):
    """Start caption editing for a post"""
    try:
        post = Database.get_post_by_id(post_id)
        
        if not post or post['user_id'] != user.id:
            await query.edit_message_text("❌ Post not found or access denied")
            return
        
        # Store editing context in session
        session_data = {
            'editing_post_id': post_id,
            'editing_type': 'caption'
        }
        Database.update_user_session(user.id, BotStates.EDIT_POST_CAPTION, session_data)
        
        current_caption = post['description'] or 'No caption'
        
        message = f"📝 *Edit Caption - Post #{post_id}*\n\n"
        message += f"*Current Caption:*\n{escape_markdown(current_caption)}\n\n"
        message += "*Send your new caption:*\n"
        message += "• Use Telegram's formatting menu to format text\n"
        message += "• Bold, italic, and other formatting will be preserved\n"
        message += "• Send 'skip' to remove the caption"
        
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"editposts_cancel_{post_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error in handle_editposts_caption: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error: {e}")


async def handle_editposts_media(query, user, post_id):
    """Start media replacement for a post"""
    try:
        post = Database.get_post_by_id(post_id)
        
        if not post or post['user_id'] != user.id:
            await query.edit_message_text("❌ Post not found or access denied")
            return
        
        # Store editing context in session
        session_data = {
            'editing_post_id': post_id,
            'editing_type': 'media'
        }
        Database.update_user_session(user.id, BotStates.EDIT_POST_MEDIA, session_data)
        
        media_type = post['media_type'] or 'photo'
        media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
        
        message = f"🖼️ *Replace Media - Post #{post_id}*\n\n"
        message += f"*Current Media:* {media_icon} {media_type.title()}\n\n"
        message += "*Send new media file:*\n"
        message += "• Send a photo, video, audio, animation, or document\n"
        message += "• For best quality, send as document (uncompressed)\n"
        message += "• The new file will replace the current one"
        
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"editposts_cancel_{post_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error in handle_editposts_media: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error: {e}")


async def handle_editposts_schedule(query, user, post_id):
    """Start schedule editing for a post"""
    try:
        post = Database.get_post_by_id(post_id)
        
        if not post or post['user_id'] != user.id:
            await query.edit_message_text("❌ Post not found or access denied")
            return
        
        scheduled_time = post.get('scheduled_time')
        if scheduled_time:
            if isinstance(scheduled_time, str):
                scheduled_dt = datetime.fromisoformat(scheduled_time)
            else:
                scheduled_dt = scheduled_time
            time_str = scheduled_dt.strftime("%Y-%m-%d %H:%M")
        else:
            time_str = "Not scheduled"
        
        # Quick time options
        current_time = get_current_kyiv_time()
        
        keyboard = [
            [InlineKeyboardButton("+1 Hour", callback_data=f"editposts_schedquick_{post_id}_1"),
             InlineKeyboardButton("+2 Hours", callback_data=f"editposts_schedquick_{post_id}_2")],
            [InlineKeyboardButton("+4 Hours", callback_data=f"editposts_schedquick_{post_id}_4"),
             InlineKeyboardButton("+8 Hours", callback_data=f"editposts_schedquick_{post_id}_8")],
            [InlineKeyboardButton("+1 Day", callback_data=f"editposts_schedquick_{post_id}_24"),
             InlineKeyboardButton("+2 Days", callback_data=f"editposts_schedquick_{post_id}_48")],
            [InlineKeyboardButton("📅 Custom Time", callback_data=f"editposts_schedcustom_{post_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"editposts_cancel_{post_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message = f"⏰ *Reschedule Post #{post_id}*\n\n"
        message += f"*Current Schedule:* {time_str}\n"
        message += f"*Current Time (Kyiv):* {current_time.strftime('%Y-%m-%d %H:%M')}\n\n"
        message += "*Choose new time:*"
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error in handle_editposts_schedule: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error: {e}")


async def handle_editposts_schedule_quick(query, user, post_id, hours, context):
    """Handle quick schedule adjustment"""
    try:
        post = Database.get_post_by_id(post_id)
        
        if not post or post['user_id'] != user.id:
            await query.answer("Post not found or access denied", show_alert=True)
            return
        
        # Calculate new time from now
        current_time = get_current_kyiv_time()
        new_time = current_time + timedelta(hours=hours)
        
        # Update database
        success = Database.update_post_schedule(post_id, new_time)
        
        if success:
            # Update scheduler if available
            if context and context.application and context.application.bot_data:
                scheduler = context.application.bot_data.get('scheduler')
                if scheduler:
                    scheduler.schedule_post(post_id, new_time)
            
            await query.answer(f"Rescheduled to {new_time.strftime('%Y-%m-%d %H:%M')}", show_alert=True)
            
            # Refresh the post display
            mode, session_data = Database.get_user_session(user.id)
            if session_data and 'edit_post_ids' in session_data:
                current_index = session_data.get('edit_current_index', 0)
                channel_id = session_data.get('edit_channel_id')
                channels = Database.get_user_channels(user.id)
                channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
                channel_name = channel['channel_name'] if channel else channel_id
                
                # Get updated post
                updated_post = Database.get_post_by_id(post_id)
                await show_edit_post_details(query, user, updated_post, current_index, len(session_data['edit_post_ids']), channel_name)
        else:
            await query.answer("Failed to update schedule", show_alert=True)
        
    except Exception as e:
        logger.error(f"Error in handle_editposts_schedule_quick: {e}", exc_info=True)
        await query.answer(f"Error: {e}", show_alert=True)


async def handle_editposts_schedule_custom(query, user, post_id):
    """Start custom schedule input for a post"""
    try:
        # Store editing context
        session_data = {
            'editing_post_id': post_id,
            'editing_type': 'schedule'
        }
        Database.update_user_session(user.id, BotStates.EDIT_POST_SCHEDULE, session_data)
        
        message = f"📅 *Custom Schedule - Post #{post_id}*\n\n"
        message += "*Enter new date and time:*\n"
        message += "`YYYY-MM-DD HH:MM`\n\n"
        message += "*Example:* `2025-12-30 14:30`\n"
        message += "*(Time in Kyiv timezone)*"
        
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"editposts_cancel_{post_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error in handle_editposts_schedule_custom: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error: {e}")


async def handle_editposts_delete(query, user, post_id):
    """Show delete confirmation for a post"""
    try:
        post = Database.get_post_by_id(post_id)
        
        if not post or post['user_id'] != user.id:
            await query.edit_message_text("❌ Post not found or access denied")
            return
        
        description = post['description'] or 'No caption'
        display_desc = description[:50] + "..." if len(description) > 50 else description
        
        message = f"🗑️ *Delete Post #{post_id}?*\n\n"
        message += f"*Caption:* {escape_markdown(display_desc)}\n\n"
        message += "⚠️ *This action cannot be undone!*\n"
        message += "The post will be removed from the schedule."
        
        keyboard = [
            [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"editposts_confirmdelete_{post_id}"),
             InlineKeyboardButton("❌ No, Keep", callback_data=f"editposts_cancel_{post_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error in handle_editposts_delete: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error: {e}")


async def handle_editposts_confirm_delete(query, user, post_id, context):
    """Execute post deletion"""
    try:
        post = Database.get_post_by_id(post_id)
        
        if not post or post['user_id'] != user.id:
            await query.answer("Post not found or access denied", show_alert=True)
            return
        
        # Delete the post
        success = Database.delete_post(post_id, user.id)
        
        if success:
            # Remove from scheduler if available
            if context and context.application and context.application.bot_data:
                scheduler = context.application.bot_data.get('scheduler')
                if scheduler:
                    try:
                        scheduler.scheduler.remove_job(f"post_{post_id}")
                    except:
                        pass  # Job might not exist
            
            # Delete media file
            file_path = post.get('file_path')
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass
            
            await query.answer("Post deleted successfully!", show_alert=True)
            
            # Update session and navigate
            mode, session_data = Database.get_user_session(user.id)
            if session_data and 'edit_post_ids' in session_data:
                post_ids = session_data['edit_post_ids']
                
                # Remove deleted post from list
                if post_id in post_ids:
                    post_ids.remove(post_id)
                
                if not post_ids:
                    # No more posts, go back to channel selection
                    await query.edit_message_text(
                        "✅ Post deleted! No more posts to edit.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="editposts_menu")]])
                    )
                else:
                    # Navigate to next post or previous
                    current_index = session_data.get('edit_current_index', 0)
                    if current_index >= len(post_ids):
                        current_index = len(post_ids) - 1
                    
                    session_data['edit_post_ids'] = post_ids
                    session_data['edit_current_index'] = current_index
                    Database.update_user_session(user.id, BotStates.EDIT_POSTS_MENU, session_data)
                    
                    # Show next post
                    next_post = Database.get_post_by_id(post_ids[current_index])
                    channel_id = session_data.get('edit_channel_id')
                    channels = Database.get_user_channels(user.id)
                    channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
                    channel_name = channel['channel_name'] if channel else channel_id
                    
                    await show_edit_post_details(query, user, next_post, current_index, len(post_ids), channel_name)
        else:
            await query.answer("Failed to delete post", show_alert=True)
        
    except Exception as e:
        logger.error(f"Error in handle_editposts_confirm_delete: {e}", exc_info=True)
        await query.answer(f"Error: {e}", show_alert=True)


async def handle_editposts_cancel(query, user, post_id):
    """Cancel editing and return to post details"""
    try:
        # Reset state to menu
        mode, session_data = Database.get_user_session(user.id)
        
        if session_data and 'edit_post_ids' in session_data:
            Database.update_user_session(user.id, BotStates.EDIT_POSTS_MENU, session_data)
            
            current_index = session_data.get('edit_current_index', 0)
            channel_id = session_data.get('edit_channel_id')
            
            post = Database.get_post_by_id(post_id)
            if post:
                channels = Database.get_user_channels(user.id)
                channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
                channel_name = channel['channel_name'] if channel else channel_id
                
                await show_edit_post_details(query, user, post, current_index, len(session_data['edit_post_ids']), channel_name)
                return
        
        # Fallback to menu
        await show_editposts_menu(query, user)
        
    except Exception as e:
        logger.error(f"Error in handle_editposts_cancel: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error: {e}")


async def show_editposts_menu(query, user):
    """Show the main edit posts menu with channel selection"""
    try:
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
            posts = Database.get_user_mode2_scheduled_posts(user.id, channel['channel_id'])
            post_count = len(posts)
            
            keyboard.append([InlineKeyboardButton(
                f"📺 {channel['channel_name']} ({post_count} posts)",
                callback_data=f"editposts_channel_{channel['channel_id']}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "✏️ *Edit Scheduled Posts*\n\n"
            "Select a channel to edit its scheduled Mode 2 posts:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error in show_editposts_menu: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error: {e}")


async def handle_editposts_input(update: Update, user, session_data: dict, context=None):
    """Handle text/media input during post editing"""
    import json
    
    editing_post_id = session_data.get('editing_post_id')
    editing_type = session_data.get('editing_type')
    
    if not editing_post_id:
        return False
    
    if editing_type == 'caption':
        # Handle caption editing
        new_caption = update.message.text.strip() if update.message.text else None
        
        # Skip means remove caption
        if new_caption and new_caption.lower() == 'skip':
            new_caption = None
            caption_entities_json = None
        else:
            # Get caption entities for formatting
            caption_entities = update.message.entities
            caption_entities_json = None
            if caption_entities:
                caption_entities_json = json.dumps([
                    {'type': e.type, 'offset': e.offset, 'length': e.length, 
                     'url': e.url, 'user': e.user.id if e.user else None, 'language': e.language}
                    for e in caption_entities
                ])
        
        # Update the post
        success = Database.update_post_description(editing_post_id, new_caption, caption_entities_json)
        
        if success:
            # Reset session and confirm
            mode, old_session = Database.get_user_session(user.id)
            if 'edit_post_ids' in old_session:
                Database.update_user_session(user.id, BotStates.EDIT_POSTS_MENU, old_session)
            
            await update.message.reply_text(
                f"✅ *Caption Updated Successfully!*\n\n"
                f"*Post #{editing_post_id}* has been updated.\n"
                f"Use /editposts to continue editing.",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text("❌ Failed to update caption. Please try again.")
        
        return True
    
    elif editing_type == 'schedule':
        # Handle custom schedule input
        text = update.message.text.strip() if update.message.text else ""
        
        try:
            # Parse the datetime
            tz = get_kyiv_timezone()
            new_time = datetime.strptime(text, "%Y-%m-%d %H:%M")
            new_time = tz.localize(new_time)
            
            # Update the post
            success = Database.update_post_schedule(editing_post_id, new_time)
            
            if success:
                # Update scheduler
                if context and context.application and context.application.bot_data:
                    scheduler = context.application.bot_data.get('scheduler')
                    if scheduler:
                        scheduler.schedule_post(editing_post_id, new_time)
                
                # Reset session
                mode, old_session = Database.get_user_session(user.id)
                if 'edit_post_ids' in old_session:
                    Database.update_user_session(user.id, BotStates.EDIT_POSTS_MENU, old_session)
                
                await update.message.reply_text(
                    f"✅ *Schedule Updated!*\n\n"
                    f"*Post #{editing_post_id}* rescheduled to:\n"
                    f"`{new_time.strftime('%Y-%m-%d %H:%M')}` (Kyiv time)\n\n"
                    f"Use /editposts to continue editing.",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text("❌ Failed to update schedule. Please try again.")
            
        except ValueError:
            await update.message.reply_text(
                "❌ *Invalid date format!*\n\n"
                "Please use: `YYYY-MM-DD HH:MM`\n"
                "Example: `2025-12-30 14:30`",
                parse_mode='Markdown'
            )
        
        return True
    
    return False


# ============= MODE 3: GUIDED CAPTIONING HANDLERS =============

async def handle_mode3_media_upload(update: Update, user, file_path: str, media_type: str, session_data: dict):
    """Handle media upload during Mode 3 uploading phase"""
    media_items = session_data.get('media_items', [])
    
    media_items.append({
        'file_path': file_path,
        'media_type': media_type
    })
    
    session_data['media_items'] = media_items
    Database.update_user_session(user.id, BotStates.MODE3_UPLOADING, session_data)
    
    media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
    
    keyboard = [[InlineKeyboardButton("✅ Done Uploading", callback_data="mode3_done_uploading")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"{media_icon} Media {len(media_items)} uploaded!\n\n"
        f"Continue uploading or tap *Done Uploading* when ready to add captions.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def handle_mode3_done_uploading(query, user):
    """Handle completion of Mode 3 uploading phase, start captioning"""
    mode, session_data = Database.get_user_session(user.id)
    
    if mode != BotStates.MODE3_UPLOADING:
        await query.answer("Not in Mode 3 uploading state.")
        return
    
    media_items = session_data.get('media_items', [])
    
    if not media_items:
        await query.answer("No media uploaded yet!")
        await query.edit_message_text(
            "❌ *No Media Uploaded*\n\n"
            "Please upload at least one photo or video first.",
            parse_mode='Markdown'
        )
        return
    
    # Switch to captioning phase
    session_data['current_caption_index'] = 0
    Database.update_user_session(user.id, BotStates.MODE3_CAPTIONING, session_data)
    
    await query.answer()
    
    # Show first media for captioning
    await show_mode3_caption_prompt(query, user, session_data, 0)


async def show_mode3_caption_prompt(query_or_message, user, session_data: dict, index: int):
    """Show media preview and ask for caption"""
    media_items = session_data.get('media_items', [])
    total = len(media_items)
    
    if index >= total:
        # All done - proceed to scheduling
        await finish_mode3_captioning(query_or_message, user, session_data)
        return
    
    item = media_items[index]
    file_path = item['file_path']
    media_type = item['media_type']
    
    media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
    
    keyboard = [
        [InlineKeyboardButton("⏭️ Skip (No Caption)", callback_data="mode3_skip_caption")],
        [InlineKeyboardButton("❌ Cancel Mode 3", callback_data="mode3_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    caption_text = (
        f"🎯 *Guided Captioning* - Media {index + 1}/{total}\n\n"
        f"Type your caption for this {media_type}, or tap *Skip* to post without caption.\n\n"
        f"💡 Tip: Use Telegram's formatting menu for bold, italic, etc."
    )
    
    # Send the media preview with caption prompt
    chat_id = user.id
    
    # Determine if we're working with a query or message
    if hasattr(query_or_message, 'message'):
        # It's a callback query
        bot = query_or_message.message.get_bot()
        # Delete the old message first
        try:
            await query_or_message.message.delete()
        except:
            pass
    else:
        # It's a message
        bot = query_or_message.get_bot()
    
    try:
        if media_type == 'photo':
            with open(file_path, 'rb') as f:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=caption_text,
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
        elif media_type == 'video':
            with open(file_path, 'rb') as f:
                await bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    caption=caption_text,
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
        elif media_type == 'animation':
            with open(file_path, 'rb') as f:
                await bot.send_animation(
                    chat_id=chat_id,
                    animation=f,
                    caption=caption_text,
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
        elif media_type == 'document':
            with open(file_path, 'rb') as f:
                await bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    caption=caption_text,
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
        else:
            # Fallback to text message with reference
            await bot.send_message(
                chat_id=chat_id,
                text=f"{media_icon} {caption_text}\n\n(Preview not available for this media type)",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
    except Exception as e:
        logger.error(f"Error sending Mode 3 preview: {e}", exc_info=True)
        await bot.send_message(
            chat_id=chat_id,
            text=f"{media_icon} {caption_text}\n\n(Error loading preview: {e})",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )


async def handle_mode3_caption_input(update: Update, user, session_data: dict, context):
    """Handle caption text input for Mode 3"""
    text = update.message.text.strip() if update.message.text else ""
    caption_entities = update.message.entities if update.message.entities else None
    
    index = session_data.get('current_caption_index', 0)
    media_items = session_data.get('media_items', [])
    selected_channel_id = session_data.get('selected_channel_id')
    
    if index >= len(media_items):
        await update.message.reply_text("❌ Error: Invalid media index.")
        return
    
    item = media_items[index]
    file_path = item['file_path']
    media_type = item['media_type']
    
    # Serialize caption entities if present
    entities_json = None
    if caption_entities:
        entities_list = []
        for entity in caption_entities:
            user_data = None
            if hasattr(entity, 'user') and entity.user:
                user_data = entity.user.id
            entities_list.append({
                'type': entity.type,
                'offset': entity.offset,
                'length': entity.length,
                'url': getattr(entity, 'url', None),
                'user': user_data,
                'language': getattr(entity, 'language', None),
                'custom_emoji_id': getattr(entity, 'custom_emoji_id', None)
            })
        entities_json = json.dumps(entities_list)
    
    # Save post to database
    post_id = Database.add_post(
        user.id, 
        file_path, 
        media_type=media_type, 
        description=text if text else None,
        mode=3, 
        channel_id=selected_channel_id,
        caption_entities=entities_json
    )
    
    # Mark this item as saved with its post_id
    media_items[index]['post_id'] = post_id
    media_items[index]['caption'] = text if text else None
    
    # Move to next item
    session_data['current_caption_index'] = index + 1
    session_data['media_items'] = media_items
    Database.update_user_session(user.id, BotStates.MODE3_CAPTIONING, session_data)
    
    # Show next media or finish
    await show_mode3_caption_prompt(update.message, user, session_data, index + 1)


async def handle_mode3_skip_caption(query, user):
    """Handle skip caption button in Mode 3"""
    mode, session_data = Database.get_user_session(user.id)
    
    if mode != BotStates.MODE3_CAPTIONING:
        await query.answer("Not in captioning mode.")
        return
    
    index = session_data.get('current_caption_index', 0)
    media_items = session_data.get('media_items', [])
    selected_channel_id = session_data.get('selected_channel_id')
    
    if index >= len(media_items):
        await query.answer("All media processed.")
        return
    
    item = media_items[index]
    file_path = item['file_path']
    media_type = item['media_type']
    
    # Save post to database without caption
    post_id = Database.add_post(
        user.id, 
        file_path, 
        media_type=media_type, 
        description=None,
        mode=3, 
        channel_id=selected_channel_id
    )
    
    # Mark this item as saved
    media_items[index]['post_id'] = post_id
    media_items[index]['caption'] = None
    
    # Move to next item
    session_data['current_caption_index'] = index + 1
    session_data['media_items'] = media_items
    Database.update_user_session(user.id, BotStates.MODE3_CAPTIONING, session_data)
    
    await query.answer(f"Skipped caption for media {index + 1}")
    
    # Show next media or finish
    await show_mode3_caption_prompt(query, user, session_data, index + 1)


async def finish_mode3_captioning(query_or_message, user, session_data: dict):
    """Finish Mode 3 captioning and show scheduling options"""
    media_items = session_data.get('media_items', [])
    selected_channel_id = session_data.get('selected_channel_id')
    
    # Count posts saved
    saved_posts = [item for item in media_items if 'post_id' in item]
    
    # Get channel name
    channels = Database.get_user_channels(user.id)
    channel = next((ch for ch in channels if ch['channel_id'] == selected_channel_id), None)
    channel_name = channel['channel_name'] if channel else selected_channel_id
    
    keyboard = [
        [InlineKeyboardButton("📅 Schedule Posts", callback_data="mode3_schedule")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message_text = (
        f"✅ *All Captions Added!*\n\n"
        f"*Media:* {len(saved_posts)} items\n"
        f"*Channel:* {channel_name}\n\n"
        f"Tap *Schedule Posts* to set posting times."
    )
    
    # Reset session state but keep media info for scheduling
    session_data['mode3_ready_to_schedule'] = True
    Database.update_user_session(user.id, BotStates.IDLE, session_data)
    
    # Determine if we're working with a query or message
    if hasattr(query_or_message, 'message'):
        bot = query_or_message.message.get_bot()
        chat_id = query_or_message.message.chat_id
    else:
        bot = query_or_message.get_bot()
        chat_id = query_or_message.chat_id
    
    await bot.send_message(
        chat_id=chat_id,
        text=message_text,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def handle_mode3_schedule(query, user):
    """Handle Mode 3 scheduling - use same scheduling logic as Mode 2"""
    mode, session_data = Database.get_user_session(user.id)
    
    media_items = session_data.get('media_items', [])
    selected_channel_id = session_data.get('selected_channel_id')
    
    # Get pending posts for this channel (unscheduled only)
    pending_posts = Database.get_pending_posts(user.id, channel_id=selected_channel_id, unscheduled_only=True)
    
    if not pending_posts:
        await query.answer("No posts to schedule.")
        await query.edit_message_text(
            "❌ *No Posts to Schedule*\n\n"
            "All posts may have already been scheduled.",
            parse_mode='Markdown'
        )
        return
    
    # Use same scheduling interface as other modes
    keyboard = [
        [InlineKeyboardButton("✅ Schedule All Posts", callback_data="schedule_current")],
        [InlineKeyboardButton("⏭️ Next Available Slot", callback_data="schedule_next_slot")],
        [InlineKeyboardButton("⚙️ Change Settings", callback_data="schedule_custom")],
        [InlineKeyboardButton("📅 Custom Date", callback_data="schedule_custom_date")],
        [InlineKeyboardButton("❌ Cancel", callback_data="schedule_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.answer()
    await query.edit_message_text(
        f"📅 *Schedule {len(pending_posts)} Posts*\n\n"
        f"Choose scheduling option:\n\n"
        f"• *Schedule All:* Use default settings\n"
        f"• *Next Available Slot:* Start from next time slot\n"
        f"• *Change Settings:* Adjust interval and hours\n"
        f"• *Custom Date:* Pick a specific start date",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def handle_mode3_cancel(query, user):
    """Cancel Mode 3 and clean up"""
    mode, session_data = Database.get_user_session(user.id)
    
    media_items = session_data.get('media_items', [])
    
    # Clean up uploaded files that haven't been saved to database
    for item in media_items:
        if 'post_id' not in item:
            file_path = item.get('file_path')
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass
    
    # Reset session
    Database.update_user_session(user.id, BotStates.IDLE)
    
    await query.answer("Mode 3 cancelled")
    try:
        await query.message.delete()
    except:
        pass
    
    # Show main menu
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.message.chat.send_message(
        "❌ *Mode 3 Cancelled*\n\nUploaded media has been removed.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def handle_editposts_media_input(update: Update, user, file_path: str, media_type: str, session_data: dict):
    """Handle media replacement input"""
    editing_post_id = session_data.get('editing_post_id')
    
    if not editing_post_id or session_data.get('editing_type') != 'media':
        return False
    
    # Get old file path for cleanup
    post = Database.get_post_by_id(editing_post_id)
    old_file_path = post.get('file_path') if post else None
    
    # Update the post with new media
    success = Database.update_post_media(editing_post_id, file_path, media_type)
    
    if success:
        # Delete old file
        if old_file_path and os.path.exists(old_file_path) and old_file_path != file_path:
            try:
                os.remove(old_file_path)
            except:
                pass
        
        # Reset session
        mode, old_session = Database.get_user_session(user.id)
        if 'edit_post_ids' in old_session:
            Database.update_user_session(user.id, BotStates.EDIT_POSTS_MENU, old_session)
        
        media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(media_type, '📁')
        
        await update.message.reply_text(
            f"✅ *Media Replaced Successfully!*\n\n"
            f"*Post #{editing_post_id}* now has new {media_icon} {media_type}.\n\n"
            f"Use /editposts to continue editing.",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ Failed to replace media. Please try again.")
    
    return True


# ==================== RECURRING POSTS MANAGEMENT ====================

async def show_recurring_posts_menu(query, user):
    """Show the main recurring posts management menu"""
    try:
        recurring_posts = Database.get_user_recurring_posts(user.id)
        
        if not recurring_posts:
            keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                "🔄 *No Recurring Posts*\n\n"
                "You don't have any active recurring posts.\n\n"
                "Use *Recurring Mode* from the main menu to create one!",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return
        
        channels = {}
        for post in recurring_posts:
            channel_id = post['channel_id']
            channel_name = post.get('channel_name', channel_id)
            if channel_id not in channels:
                channels[channel_id] = {'name': channel_name, 'count': 0}
            channels[channel_id]['count'] += 1
        
        keyboard = []
        for channel_id, info in channels.items():
            keyboard.append([InlineKeyboardButton(
                f"📺 {info['name']} ({info['count']} recurring)",
                callback_data=f"recur_manage_ch_{channel_id}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"🔄 *Manage Recurring Posts*\n\n"
            f"You have *{len(recurring_posts)}* active recurring post(s).\n\n"
            f"Select a channel to view and manage recurring posts:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error in show_recurring_posts_menu: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error: {escape_markdown(str(e))}", parse_mode='Markdown')


async def handle_recurring_channel_posts(query, user, channel_id: str):
    """Show recurring posts for a specific channel with navigation"""
    try:
        posts = Database.get_user_recurring_posts(user.id, channel_id)
        
        if not posts:
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="recurring_manage_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                "❌ No recurring posts found for this channel.",
                reply_markup=reply_markup
            )
            return
        
        session_data = {
            'recurring_posts': [p['id'] for p in posts],
            'current_index': 0,
            'channel_id': channel_id
        }
        Database.update_user_session(user.id, 'RECURRING_MANAGE', session_data)
        
        await show_recurring_post_detail(query, user, posts[0], 0, len(posts))
        
    except Exception as e:
        logger.error(f"Error in handle_recurring_channel_posts: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error: {escape_markdown(str(e))}", parse_mode='Markdown')


async def show_recurring_post_detail(query, user, post: dict, index: int, total: int):
    """Show details of a recurring post with edit/delete options"""
    try:
        post_id = post['id']
        interval = post.get('recurring_interval_hours', 24)
        end_date = post.get('recurring_end_date')
        count_limit = post.get('recurring_count')
        posted_count = post.get('recurring_posted_count', 0)
        description = post.get('description', '')
        media_type = post.get('media_type', 'photo')
        next_post = post.get('scheduled_time')
        channel_name = post.get('channel_name', post.get('channel_id', 'Unknown'))
        
        interval_text = f"{interval} hours"
        if interval == 24:
            interval_text = "Daily (24h)"
        elif interval == 48:
            interval_text = "Every 2 days"
        elif interval == 168:
            interval_text = "Weekly"
        
        if count_limit:
            remaining = count_limit - posted_count
            end_text = f"{remaining} posts remaining (of {count_limit})"
        elif end_date:
            end_text = f"Until {end_date.strftime('%Y-%m-%d %H:%M')}"
        else:
            end_text = "Never (infinite)"
        
        caption_text = escape_markdown(description[:100] + "..." if len(description) > 100 else description) if description else "_No caption_"
        next_text = next_post.strftime('%Y-%m-%d %H:%M') if next_post else "Not scheduled"
        
        media_icons = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}
        media_icon = media_icons.get(media_type, '📁')
        
        nav_buttons = []
        if index > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"recur_nav_{index-1}"))
        if index < total - 1:
            nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"recur_nav_{index+1}"))
        
        keyboard = []
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        keyboard.extend([
            [
                InlineKeyboardButton("👁️ Preview", callback_data=f"recur_preview_{post_id}"),
                InlineKeyboardButton("✏️ Caption", callback_data=f"recur_editcap_{post_id}")
            ],
            [
                InlineKeyboardButton("⏱️ Interval", callback_data=f"recur_editint_{post_id}"),
                InlineKeyboardButton("🔚 End Condition", callback_data=f"recur_editend_{post_id}")
            ],
            [InlineKeyboardButton("🗑️ Delete", callback_data=f"recur_delete_{post_id}")],
            [InlineKeyboardButton("🔙 Back to Channels", callback_data="recurring_manage_menu")]
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"🔄 *Recurring Post \\#{post_id}* ({index+1}/{total})\n\n"
            f"*📺 Channel:* {escape_markdown(channel_name)}\n"
            f"*{media_icon} Type:* {media_type.capitalize()}\n"
            f"*⏱️ Interval:* {interval_text}\n"
            f"*📊 Posted:* {posted_count} times\n"
            f"*🔚 Ends:* {end_text}\n"
            f"*📅 Next Post:* {next_text}\n\n"
            f"*📝 Caption:*\n{caption_text}",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error in show_recurring_post_detail: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error: {escape_markdown(str(e))}", parse_mode='Markdown')


async def handle_recurring_navigation(query, user, new_index: int):
    """Handle navigation between recurring posts"""
    try:
        mode, session_data = Database.get_user_session(user.id)
        if mode != 'RECURRING_MANAGE':
            await query.answer("Session expired. Please start again.")
            return
        
        post_ids = session_data.get('recurring_posts', [])
        
        if not post_ids or new_index < 0 or new_index >= len(post_ids):
            await query.answer("Invalid navigation")
            return
        
        session_data['current_index'] = new_index
        Database.update_user_session(user.id, 'RECURRING_MANAGE', session_data)
        
        post_id = post_ids[new_index]
        posts = Database.get_user_recurring_posts(user.id, session_data.get('channel_id'))
        post = next((p for p in posts if p['id'] == post_id), None)
        
        if not post:
            await query.answer("Post not found")
            return
        
        await show_recurring_post_detail(query, user, post, new_index, len(post_ids))
        
    except Exception as e:
        logger.error(f"Error in handle_recurring_navigation: {e}", exc_info=True)
        await query.answer(f"Error: {str(e)[:50]}")


async def handle_recurring_preview(query, user, post_id: int):
    """Preview the media of a recurring post"""
    try:
        posts = Database.get_user_recurring_posts(user.id)
        post = next((p for p in posts if p['id'] == post_id), None)
        
        if not post:
            await query.answer("Post not found")
            return
        
        file_path = post.get('file_path')
        media_type = post.get('media_type', 'photo')
        description = post.get('description', '')
        caption_entities_json = post.get('caption_entities')
        
        if not file_path or not os.path.exists(file_path):
            await query.answer("Media file not found")
            return
        
        caption_entities = None
        if caption_entities_json:
            import json
            try:
                entities_data = json.loads(caption_entities_json)
                caption_entities = [
                    MessageEntity(
                        type=e['type'],
                        offset=e['offset'],
                        length=e['length'],
                        url=e.get('url'),
                        language=e.get('language')
                    ) for e in entities_data
                ]
            except:
                pass
        
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data=f"recur_back_{post_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        with open(file_path, 'rb') as f:
            if media_type == 'photo':
                await query.message.chat.send_photo(
                    photo=f,
                    caption=description if description else None,
                    caption_entities=caption_entities,
                    reply_markup=reply_markup
                )
            elif media_type == 'video':
                await query.message.chat.send_video(
                    video=f,
                    caption=description if description else None,
                    caption_entities=caption_entities,
                    reply_markup=reply_markup
                )
            elif media_type == 'audio':
                await query.message.chat.send_audio(
                    audio=f,
                    caption=description if description else None,
                    caption_entities=caption_entities,
                    reply_markup=reply_markup
                )
            elif media_type == 'animation':
                await query.message.chat.send_animation(
                    animation=f,
                    caption=description if description else None,
                    caption_entities=caption_entities,
                    reply_markup=reply_markup
                )
            else:
                await query.message.chat.send_document(
                    document=f,
                    caption=description if description else None,
                    caption_entities=caption_entities,
                    reply_markup=reply_markup
                )
        
        await query.answer("Preview sent!")
        
    except Exception as e:
        logger.error(f"Error in handle_recurring_preview: {e}", exc_info=True)
        await query.answer(f"Error: {str(e)[:50]}")


async def handle_recurring_edit_caption(query, user, post_id: int):
    """Start editing the caption of a recurring post"""
    try:
        session_data = {
            'editing_recurring_id': post_id,
            'editing_type': 'caption'
        }
        Database.update_user_session(user.id, 'RECURRING_EDIT_CAPTION', session_data)
        
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"recur_back_{post_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"✏️ *Edit Caption for Recurring Post \\#{post_id}*\n\n"
            f"Send the new caption with formatting \\(bold, italic, etc\\.\\)\\.\n\n"
            f"Send `skip` to remove the caption\\.\n\n"
            f"*Formatting Tips:*\n"
            f"• Use Telegram's built\\-in formatting\n"
            f"• Bold, italic, underline, strikethrough all work\n"
            f"• Links and code blocks are preserved",
            reply_markup=reply_markup,
            parse_mode='MarkdownV2'
        )
        
    except Exception as e:
        logger.error(f"Error in handle_recurring_edit_caption: {e}", exc_info=True)
        await query.answer(f"Error: {str(e)[:50]}")


async def handle_recurring_edit_interval(query, user, post_id: int):
    """Show interval editing options"""
    try:
        keyboard = [
            [
                InlineKeyboardButton("6h", callback_data=f"recur_setint_{post_id}_6"),
                InlineKeyboardButton("12h", callback_data=f"recur_setint_{post_id}_12"),
                InlineKeyboardButton("24h", callback_data=f"recur_setint_{post_id}_24")
            ],
            [
                InlineKeyboardButton("48h", callback_data=f"recur_setint_{post_id}_48"),
                InlineKeyboardButton("72h", callback_data=f"recur_setint_{post_id}_72"),
                InlineKeyboardButton("168h (weekly)", callback_data=f"recur_setint_{post_id}_168")
            ],
            [InlineKeyboardButton("🔙 Cancel", callback_data=f"recur_back_{post_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"⏱️ *Change Interval for Post \\#{post_id}*\n\n"
            f"Select new posting interval:",
            reply_markup=reply_markup,
            parse_mode='MarkdownV2'
        )
        
    except Exception as e:
        logger.error(f"Error in handle_recurring_edit_interval: {e}", exc_info=True)
        await query.answer(f"Error: {str(e)[:50]}")


async def handle_recurring_set_interval(query, user, post_id: int, interval_hours: int):
    """Apply new interval to recurring post"""
    try:
        success = Database.update_recurring_post_interval(post_id, interval_hours, user_id=user.id)
        
        if success:
            await query.answer(f"Interval updated to {interval_hours}h!")
            
            posts = Database.get_user_recurring_posts(user.id)
            post = next((p for p in posts if p['id'] == post_id), None)
            if post:
                await show_recurring_post_detail(query, user, post, 0, 1)
        else:
            await query.answer("Failed to update interval")
        
    except Exception as e:
        logger.error(f"Error in handle_recurring_set_interval: {e}", exc_info=True)
        await query.answer(f"Error: {str(e)[:50]}")


async def handle_recurring_edit_end(query, user, post_id: int):
    """Show end condition editing options"""
    try:
        keyboard = [
            [InlineKeyboardButton("🔄 Never (Infinite)", callback_data=f"recur_setend_{post_id}_never")],
            [
                InlineKeyboardButton("5 times", callback_data=f"recur_setend_{post_id}_count_5"),
                InlineKeyboardButton("10 times", callback_data=f"recur_setend_{post_id}_count_10")
            ],
            [
                InlineKeyboardButton("20 times", callback_data=f"recur_setend_{post_id}_count_20"),
                InlineKeyboardButton("50 times", callback_data=f"recur_setend_{post_id}_count_50")
            ],
            [InlineKeyboardButton("📅 Custom Date", callback_data=f"recur_setend_{post_id}_date")],
            [InlineKeyboardButton("🔙 Cancel", callback_data=f"recur_back_{post_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"🔚 *Change End Condition for Post \\#{post_id}*\n\n"
            f"Select when this recurring post should stop:",
            reply_markup=reply_markup,
            parse_mode='MarkdownV2'
        )
        
    except Exception as e:
        logger.error(f"Error in handle_recurring_edit_end: {e}", exc_info=True)
        await query.answer(f"Error: {str(e)[:50]}")


async def handle_recurring_set_end(query, user, post_id: int, end_type: str, context=None):
    """Apply new end condition to recurring post"""
    try:
        if end_type == "never":
            success = Database.update_recurring_post_end_condition(post_id, None, None, user_id=user.id)
            msg = "End condition set to: Never"
        elif end_type.startswith("count_"):
            count = int(end_type.replace("count_", ""))
            success = Database.update_recurring_post_end_condition(post_id, count, None, user_id=user.id)
            msg = f"End condition set to: {count} times"
        elif end_type == "date":
            session_data = {
                'editing_recurring_id': post_id,
                'editing_type': 'end_date'
            }
            Database.update_user_session(user.id, 'RECURRING_EDIT_END_DATE', session_data)
            
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"recur_back_{post_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"📅 *Set End Date for Post \\#{post_id}*\n\n"
                f"Enter the end date in format:\n"
                f"`YYYY\\-MM\\-DD HH:MM`\n\n"
                f"Example: `2026\\-02\\-15 18:00`",
                reply_markup=reply_markup,
                parse_mode='MarkdownV2'
            )
            return
        else:
            await query.answer("Invalid end type")
            return
        
        if success:
            await query.answer(msg)
            
            posts = Database.get_user_recurring_posts(user.id)
            post = next((p for p in posts if p['id'] == post_id), None)
            if post:
                await show_recurring_post_detail(query, user, post, 0, 1)
        else:
            await query.answer("Failed to update end condition")
        
    except Exception as e:
        logger.error(f"Error in handle_recurring_set_end: {e}", exc_info=True)
        await query.answer(f"Error: {str(e)[:50]}")


async def handle_recurring_delete(query, user, post_id: int):
    """Confirm deletion of recurring post"""
    try:
        keyboard = [
            [
                InlineKeyboardButton("✅ Yes, Delete", callback_data=f"recur_confirmdel_{post_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"recur_back_{post_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"🗑️ *Delete Recurring Post \\#{post_id}?*\n\n"
            f"This will stop all future posts and remove the media file\\.\n\n"
            f"*This action cannot be undone\\!*",
            reply_markup=reply_markup,
            parse_mode='MarkdownV2'
        )
        
    except Exception as e:
        logger.error(f"Error in handle_recurring_delete: {e}", exc_info=True)
        await query.answer(f"Error: {str(e)[:50]}")


async def handle_recurring_confirm_delete(query, user, post_id: int, context=None):
    """Actually delete the recurring post"""
    try:
        posts = Database.get_user_recurring_posts(user.id)
        post = next((p for p in posts if p['id'] == post_id), None)
        
        if not post:
            await query.answer("Post not found")
            return
        
        file_path = post.get('file_path')
        
        if context and context.application.bot_data.get('scheduler'):
            try:
                scheduler = context.application.bot_data['scheduler']
                scheduler.cancel_post(post_id)
            except:
                pass
        
        success = Database.delete_post(post_id)
        
        if success:
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass
            
            await query.answer("Recurring post deleted!")
            await show_recurring_posts_menu(query, user)
        else:
            await query.answer("Failed to delete post")
        
    except Exception as e:
        logger.error(f"Error in handle_recurring_confirm_delete: {e}", exc_info=True)
        await query.answer(f"Error: {str(e)[:50]}")


async def handle_recurring_back_to_post(query, user, post_id: int):
    """Go back to viewing a specific recurring post"""
    try:
        posts = Database.get_user_recurring_posts(user.id)
        post = next((p for p in posts if p['id'] == post_id), None)
        
        if not post:
            await query.answer("Post not found")
            await show_recurring_posts_menu(query, user)
            return
        
        Database.update_user_session(user.id, BotStates.IDLE)
        await show_recurring_post_detail(query, user, post, 0, 1)
        
    except Exception as e:
        logger.error(f"Error in handle_recurring_back_to_post: {e}", exc_info=True)
        await query.answer(f"Error: {str(e)[:50]}")


async def handle_recurring_caption_input(update: Update, user, text: str, session_data: dict):
    """Handle caption input for recurring post editing"""
    import json
    
    post_id = session_data.get('editing_recurring_id')
    
    if not post_id:
        return False
    
    if text.lower() == 'skip':
        new_caption = None
        caption_entities_json = None
    else:
        new_caption = text
        caption_entities = update.message.entities
        caption_entities_json = None
        if caption_entities:
            caption_entities_json = json.dumps([
                {'type': e.type, 'offset': e.offset, 'length': e.length, 
                 'url': e.url, 'user': e.user.id if e.user else None, 'language': e.language}
                for e in caption_entities
            ])
    
    success = Database.update_post_description(post_id, new_caption, caption_entities_json)
    
    if success:
        Database.update_user_session(user.id, BotStates.IDLE)
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Post", callback_data=f"recur_back_{post_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"✅ *Caption Updated\\!*\n\n"
            f"Recurring post \\#{post_id} caption has been changed\\.",
            reply_markup=reply_markup,
            parse_mode='MarkdownV2'
        )
    else:
        await update.message.reply_text("❌ Failed to update caption. Please try again.")
    
    return True


async def handle_recurring_end_date_input(update: Update, user, text: str, session_data: dict):
    """Handle end date input for recurring post editing"""
    from datetime import datetime
    from bot.utils import get_kyiv_timezone
    
    post_id = session_data.get('editing_recurring_id')
    
    if not post_id:
        return False
    
    date_str = text.strip()
    
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
            "❌ Invalid date format\\. Please use:\n\n"
            "`YYYY\\-MM\\-DD HH:MM`\n\n"
            "Example: `2026\\-02\\-15 18:00`",
            parse_mode='MarkdownV2'
        )
        return True
    
    kyiv_tz = get_kyiv_timezone()
    end_date = kyiv_tz.localize(end_date)
    
    current_time = get_current_kyiv_time()
    if end_date <= current_time:
        await update.message.reply_text(
            f"❌ End date must be in the future\\.\n\n"
            f"Current time: {escape_markdown(current_time.strftime('%Y-%m-%d %H:%M'))} \\(Kyiv\\)",
            parse_mode='MarkdownV2'
        )
        return True
    
    success = Database.update_recurring_post_end_condition(post_id, None, end_date, user_id=user.id)
    
    if success:
        Database.update_user_session(user.id, BotStates.IDLE)
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Post", callback_data=f"recur_back_{post_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"✅ *End Date Updated\\!*\n\n"
            f"Recurring post \\#{post_id} will now end on:\n"
            f"*{escape_markdown(end_date.strftime('%Y-%m-%d %H:%M'))}* \\(Kyiv time\\)",
            reply_markup=reply_markup,
            parse_mode='MarkdownV2'
        )
    else:
        await update.message.reply_text("❌ Failed to update end date. Please try again.")
    
    return True


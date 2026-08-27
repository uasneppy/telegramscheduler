"""Auto-split from the former bot/handlers.py - see __init__.py."""

from .common import *  # noqa: F401,F403
from .common import logger  # noqa: F401



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
            batch_summary += f"\n• {escape_markdown(batch['batch_name'])} → {escape_markdown(batch['channel_name'])} ({batch['post_count']} posts)"
        if len(batches) > 3:
            batch_summary += f"\n• ... and {len(batches) - 3} more"
    
    message = f"""
🔥 *Multi-Channel Batch Scheduler*

Create separate batches of posts for different channels! This lets you:
• Upload photos for multiple channels at once
• Schedule each batch independently
• Mix bulk uploads and uploads with captions

*Available Channels:* {len(channels)}{batch_summary}

Choose an option:
"""
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_batch_callback(query, user, data, context=None):
    """Handle batch management callbacks"""
    if data == "batch_create":
        await prompt_batch_creation(query, user)
    elif data == "batch_list":
        await show_batch_list(query, user)
    elif data == "batch_schedule_all":
        await schedule_all_batches(query, user, context)
    elif data == "batch_clear_all":
        await confirm_clear_all_batches(query, user)
    elif data.startswith("batch_select_"):
        batch_id = int(data.replace("batch_select_", ""))
        await show_batch_details(query, user, batch_id)
    elif data.startswith("batch_delete_confirmed_"):
        batch_id = int(data.replace("batch_delete_confirmed_", ""))
        success = Database.delete_batch(batch_id, user.id)
        if success:
            await query.edit_message_text("✅ Batch deleted successfully!")
        else:
            await query.edit_message_text("❌ Failed to delete batch.")
    elif data.startswith("batch_delete_"):
        batch_id = int(data.replace("batch_delete_", ""))
        await delete_batch_confirm(query, user, batch_id)
    elif data.startswith("batch_schedule_"):
        batch_id = int(data.replace("batch_schedule_", ""))
        await schedule_single_batch(query, user, batch_id, context)
    elif data.startswith("batch_channel_"):
        channel_id = data.replace("batch_channel_", "")
        await create_batch_for_channel(query, user, channel_id)
    elif data == "batch_back":
        await show_multibatch_menu(query, user)
    elif data.startswith("batch_clear_confirmed"):
        # Clear all batches
        batches = Database.get_user_batches(user.id)
        for batch in batches:
            Database.delete_batch(batch['id'], user.id)
        await query.edit_message_text("✅ All batches cleared successfully!")

async def show_multibatch_menu(query, user):
    """Show the multi-channel batch menu (shared by /multibatch inline paths)"""
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
            batch_summary += f"\n• {escape_markdown(batch['batch_name'])} → {escape_markdown(batch['channel_name'])} ({batch['post_count']} posts)"
        if len(batches) > 3:
            batch_summary += f"\n• ... and {len(batches) - 3} more"

    message = f"""
🔥 *Multi-Channel Batch Scheduler*

Create separate batches of posts for different channels! This lets you:
• Upload photos for multiple channels at once
• Schedule each batch independently
• Mix bulk uploads and uploads with captions

*Available Channels:* {len(channels)}{batch_summary}

Choose an option:
"""

    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

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
            f"*Name:* {escape_markdown(batch_name)}\n"
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
        "*📸 Batch Bulk Upload*\n\n"
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
        "*📝 Batch Batch Upload with Captions*\n\n"
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

    posts = Database.get_batch_posts(batch_id, user_id=user.id)

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
        f"*Name:* {escape_markdown(batch['batch_name'])}\n"
        f"*Channel:* {batch['channel_name']}\n"
        f"*Status:* {status_text}\n"
        f"*Posts:* {len(posts)}{post_summary}",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def schedule_single_batch(query, user, batch_id, context=None):
    """Schedule a single batch"""
    # SECURITY: batch_id comes from forgeable callback data - verify ownership first
    batches = Database.get_user_batches(user.id)
    batch = next((b for b in batches if b['id'] == batch_id), None)
    if not batch:
        await query.edit_message_text("❌ Batch not found.")
        return

    posts = Database.get_batch_posts(batch_id, user_id=user.id)

    if not posts:
        await query.edit_message_text("❌ No posts in this batch to schedule.")
        return

    # Get scheduling config
    start_hour, end_hour, interval_hours = Database.get_scheduling_config(user.id)

    # Calculate schedule times
    from bot.utils import calculate_schedule_times
    schedule_times = calculate_schedule_times(start_hour, end_hour, interval_hours, len(posts))

    # Schedule the batch
    Database.schedule_batch(batch_id, schedule_times, user_id=user.id)

    scheduler = get_shared_scheduler(context)
    post_ids = [post['id'] for post in posts]
    if scheduler:
        await scheduler.schedule_posts(post_ids, schedule_times)
    else:
        # Scheduled times are stored in the DB; the monitor job will pick them up
        logger.error("Shared scheduler unavailable in schedule_single_batch - relying on overdue monitor")
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Batches", callback_data="batch_list")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ *Batch Scheduled!*\n\n"
        f"*Batch:* {escape_markdown(batch['batch_name']) if batch else 'Unknown'}\n"
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
        f"*Batch:* {escape_markdown(batch['batch_name']) if batch else 'Unknown'}\n"
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

async def schedule_all_batches(query, user, context=None):
    """Schedule all pending batches"""
    batches = Database.get_user_batches(user.id)
    pending_batches = [b for b in batches if b['status'] == 'pending' and b['post_count'] > 0]
    
    if not pending_batches:
        await query.edit_message_text("❌ No pending batches to schedule.")
        return
    
    # Compute ONE combined slot sequence and hand out consecutive slots to
    # each batch - scheduling every batch from the same base time would give
    # them all identical slots and post everything simultaneously
    from bot.utils import calculate_schedule_times
    start_hour, end_hour, interval_hours = Database.get_scheduling_config(user.id)

    batch_posts = [(batch, Database.get_batch_posts(batch['id'], user_id=user.id))
                   for batch in pending_batches]
    total_posts = sum(len(posts) for _batch, posts in batch_posts)

    if total_posts == 0:
        await query.edit_message_text("❌ No posts found in the pending batches.")
        return

    all_slots = calculate_schedule_times(start_hour, end_hour, interval_hours, total_posts)
    scheduler = get_shared_scheduler(context)
    if not scheduler:
        # Scheduled times are stored in the DB; the monitor job will pick them up
        logger.error("Shared scheduler unavailable in schedule_all_batches - relying on overdue monitor")

    total_scheduled = 0
    slot_index = 0
    for batch, posts in batch_posts:
        if not posts:
            continue
        schedule_times = all_slots[slot_index:slot_index + len(posts)]
        slot_index += len(posts)

        # Schedule the batch
        Database.schedule_batch(batch['id'], schedule_times, user_id=user.id)

        post_ids = [post['id'] for post in posts]
        if scheduler:
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
        f"*Batch:* {escape_markdown(batch['batch_name'])}\n"
        f"*Posts:* {batch['post_count']}\n\n"
        "This will delete all posts and media files in this batch. Continue?",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

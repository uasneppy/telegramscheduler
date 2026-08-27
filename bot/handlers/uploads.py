"""Auto-split from the former bot/handlers.py - see __init__.py."""

from .common import *  # noqa: F401,F403
from .common import logger  # noqa: F401


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
            "Please add a channel first before uploading.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add a channel", callback_data="channels_add")],
                [InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]
            ]),
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
            "Please add a channel first before uploading.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add a channel", callback_data="channels_add")],
                [InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]
            ]),
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
    
    # While waiting for a channel ID, a forwarded channel post identifies the
    # channel directly - much easier than finding the -100... ID by hand
    if mode == BotStates.WAITING_CHANNEL_ID:
        forwarded_chat = None
        origin = getattr(update.message, 'forward_origin', None)
        if origin is not None and getattr(origin, 'chat', None) is not None:
            forwarded_chat = origin.chat
        elif getattr(update.message, 'forward_from_chat', None) is not None:
            forwarded_chat = update.message.forward_from_chat
        if forwarded_chat is not None and forwarded_chat.type == 'channel':
            await handle_forwarded_channel(update, user, forwarded_chat, session_data)
            return

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
                "Please pick an upload mode first - use /start and choose 'Bulk upload' or 'With captions' (or /mode1, /mode2)."
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
    
    # Build a single live status message instead of one reply per file -
    # a 30-photo upload used to produce 30 bot messages of chat noise
    media_summary = {}
    for item in media_items:
        media_type_key = item['media_type']
        media_summary[media_type_key] = media_summary.get(media_type_key, 0) + 1

    summary_bits = []
    for media_type_key, count in media_summary.items():
        icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(
            media_type_key.replace('document_image', 'document').replace('document_video', 'document'), '📁')
        summary_bits.append(f"{icon} {count} {media_type_key.replace('document_', '')}{'s' if count > 1 else ''}")

    progress_text = (
        f"📥 *Received {len(media_items)} item{'s' if len(media_items) > 1 else ''}* "
        f"({', '.join(summary_bits)})\n\n"
        "Keep sending media, or:"
    )
    reply_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Schedule now", callback_data="upload_done"),
         InlineKeyboardButton("🔀 Shuffle", callback_data="queue_shuffle")],
        [InlineKeyboardButton("👁 Preview", callback_data="main_preview"),
         InlineKeyboardButton("🗑 Discard all", callback_data="upload_discard")],
    ])

    status_msg_id = session_data.get('upload_status_msg_id')
    edited = False
    if status_msg_id:
        try:
            await update.get_bot().edit_message_text(
                progress_text, chat_id=update.effective_chat.id,
                message_id=status_msg_id, reply_markup=reply_markup, parse_mode='Markdown'
            )
            edited = True
        except Exception:
            edited = False  # message deleted / too old - send a fresh one

    if not edited:
        sent = await update.message.reply_text(progress_text, reply_markup=reply_markup, parse_mode='Markdown')
        session_data['upload_status_msg_id'] = sent.message_id
        Database.update_user_session(user.id, BotStates.MODE1_PHOTOS, session_data)

# Keep backward compatibility
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
             'url': e.url, 'user': e.user.id if e.user else None, 'language': e.language,
                     'custom_emoji_id': e.custom_emoji_id}
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
            'processed': False,
            'last_update': asyncio.get_running_loop().time()
        }

    # Add this message to the group and refresh the debounce timer
    context.user_data['media_groups'][media_group_id]['messages'].append(update.message)
    context.user_data['media_groups'][media_group_id]['last_update'] = asyncio.get_running_loop().time()

    # Set a delay to process the complete group (Telegram sends group messages quickly)
    if not context.user_data['media_groups'][media_group_id]['processed']:
        # Schedule processing after a debounced delay to collect all messages
        task = asyncio.create_task(process_media_group_delayed(
            context, user, mode, session_data, media_group_id, 1.0
        ))
        # Surface exceptions - a bare fire-and-forget task swallows them silently
        task.add_done_callback(
            lambda t: t.exception() and logger.error(
                f"Media group {media_group_id} processing failed: {t.exception()}", exc_info=t.exception()
            )
        )
        context.user_data['media_groups'][media_group_id]['processed'] = True

async def process_media_group_delayed(context, user, mode: str, session_data: dict,
                                    media_group_id: str, delay: float):
    """Process a complete media group after delay - create single unified post"""
    # Debounce: wait until no new album item has arrived for `delay` seconds
    # (a fixed sleep splits slow-arriving albums into multiple posts)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15.0  # hard cap so we never wait forever
    while loop.time() < deadline:
        group = context.user_data.get('media_groups', {}).get(media_group_id)
        if not group:
            return
        quiet_for = loop.time() - group['last_update']
        if quiet_for >= delay:
            break
        await asyncio.sleep(delay - quiet_for)

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
            
            # Use streaming download for efficiency (pass user_id so album
            # files land in the per-user organized directory like other media)
            try:
                from bot.utils import save_media_streaming
                file_path = await save_media_streaming(file, filename, media_type, user_id=user.id)
            except Exception as e:
                logger.error(f"Streaming failed, falling back to byte array download: {e}")
                file_data = await file.download_as_bytearray()
                file_path = save_media(bytes(file_data), filename, media_type, user_id=user.id)

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
        f"✅ *Upload Complete!*\n\n"
        f"You've uploaded {len(media_items)} media files with descriptions.\n"
        f"Use /schedule to set posting times.",
        parse_mode='Markdown'
    )
    
    # Update session to idle
    Database.update_user_session(user.id, BotStates.IDLE)


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

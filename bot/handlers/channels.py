"""Auto-split from the former bot/handlers.py - see __init__.py."""

from .common import *  # noqa: F401,F403
from .common import logger  # noqa: F401


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
            "Upload some media for this channel first using bulk upload (/mode1) or upload with captions (/mode2).\n\n"
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
            "Upload some media for this channel first using bulk upload (/mode1) or upload with captions (/mode2).",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Choose Another Channel", callback_data="main_preview")],
                [InlineKeyboardButton("🏠 Back to Menu", callback_data="back_to_main")]
            ]),
            parse_mode='Markdown'
        )
        return
    
    # Show the requested post
    await show_post_preview_for_channel(query, user.id, post_index, pending_posts, channel_name, channel_selection)

async def channels_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /channels command - manage user channels"""
    user = update.effective_user
    
    # Get user's channels
    channels = Database.get_user_channels(user.id)
    
    keyboard = []
    
    if channels:
        keyboard.append([InlineKeyboardButton("📋 View All Channels", callback_data="channels_list")])
        keyboard.append([InlineKeyboardButton("➕ Add New Channel", callback_data="channels_add"),
                         InlineKeyboardButton("🗑️ Remove Channel", callback_data="channels_remove")])
        keyboard.append([InlineKeyboardButton("✍️ Caption signatures", callback_data="channels_signatures")])
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

        await verify_and_register_channel(update, user, channel_id, channel_name)

async def handle_channel_signature_input(update: Update, user, text: str, session_data: dict):
    """Save (or clear) the caption signature for the channel being edited."""
    channel_id = session_data.get('signature_channel_id')
    if not channel_id:
        await update.message.reply_text("❌ Session expired. Please use /channels again.")
        Database.update_user_session(user.id, BotStates.IDLE)
        return

    signature = None if text.strip() == '-' else text.strip()
    if signature and len(signature) > 300:
        await update.message.reply_text(
            "❌ Signature too long (max 300 characters) - captions are capped at 1024. Try a shorter one:"
        )
        return

    Database.set_channel_signature(user.id, channel_id, signature)
    Database.update_user_session(user.id, BotStates.IDLE)

    nav = InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Other signatures", callback_data="channels_signatures"),
         InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]
    ])
    if signature:
        await update.message.reply_text(
            f"✅ *Signature saved!*\n\nEvery post to this channel will now end with:\n\n"
            f"{escape_markdown(signature)}",
            reply_markup=nav, parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("✅ Signature removed.", reply_markup=nav)

async def handle_forwarded_channel(update: Update, user, forwarded_chat, session_data: dict):
    """Register a channel identified by a forwarded post from it."""
    channel_id = str(forwarded_chat.id)
    channel_name = forwarded_chat.title or channel_id
    await verify_and_register_channel(update, user, channel_id, channel_name)

async def verify_and_register_channel(update: Update, user, channel_id: str, channel_name: str):
    """Verify admin rights, send a self-deleting test post, then register.

    Shared by the typed-ID path and the forwarded-post path. Verifying and
    test-posting at setup time means posting problems surface immediately,
    not at the scheduled time.
    """
    bot = update.get_bot()
    chat_ref = int(channel_id) if channel_id.lstrip('-').isdigit() else channel_id

    # SECURITY: verify the requesting user actually administers this channel
    # before registering it - otherwise any user could post through the bot
    # to any channel where the bot is admin.
    try:
        member = await bot.get_chat_member(chat_id=chat_ref, user_id=user.id)
        if member.status not in ('administrator', 'creator'):
            logger.warning(
                f"User {user.id} tried to register channel {channel_id} "
                f"where their status is '{member.status}'"
            )
            await update.message.reply_text(
                "❌ You must be an *administrator* of that channel to register it.\n\n"
                "Ask a channel admin to promote you, then try again.",
                parse_mode='Markdown'
            )
            Database.update_user_session(user.id, BotStates.IDLE)
            return
    except Exception as e:
        logger.warning(f"Channel verification failed for user {user.id}, channel {channel_id}: {e}")
        await update.message.reply_text(
            "❌ *Could not verify the channel.*\n\n"
            "Make sure:\n"
            "1. The channel ID is correct\n"
            "2. This bot is added to the channel as an *admin*\n"
            "3. You are an *admin* of the channel\n\n"
            "Then try again with /channels.",
            parse_mode='Markdown'
        )
        Database.update_user_session(user.id, BotStates.IDLE)
        return

    # Confirm the bot can actually post: send a test message and delete it
    posting_ok = True
    try:
        test_msg = await bot.send_message(chat_id=chat_ref, text="✅ Scheduler bot connected (test - auto-deleted)")
        try:
            await bot.delete_message(chat_id=chat_ref, message_id=test_msg.message_id)
        except Exception:
            pass  # posting worked; failing to delete is cosmetic
    except Exception as e:
        posting_ok = False
        logger.warning(f"Test post to channel {channel_id} failed: {e}")

    # Add the channel
    success = Database.add_user_channel(user.id, channel_id, channel_name, False)

    nav = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Start uploading", callback_data="main_mode1"),
         InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]
    ])

    if success and posting_ok:
        await update.message.reply_text(
            f"✅ *Channel Added Successfully!*\n\n"
            f"*Name:* {escape_markdown(channel_name)}\n"
            f"*ID:* `{channel_id}`\n\n"
            f"Test post confirmed - I can publish to this channel. 🎉",
            reply_markup=nav,
            parse_mode='Markdown'
        )
    elif success:
        await update.message.reply_text(
            f"⚠️ *Channel added, but the test post failed.*\n\n"
            f"*Name:* {escape_markdown(channel_name)}\n"
            f"*ID:* `{channel_id}`\n\n"
            f"I verified you're an admin, but I couldn't post to the channel myself. "
            f"Give me the *Post messages* permission in the channel's admin settings, "
            f"otherwise scheduled posts will fail.",
            reply_markup=nav,
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
            "*Easiest way:* forward me any post from your channel - "
            "I'll detect it automatically. 📩\n\n"
            "Or send the channel ID:\n"
            "• `@channelname` (public channels)\n"
            "• `-1001234567890` (private channels)\n\n"
            "*Before that, make sure:*\n"
            "1. You are an admin of the channel\n"
            "2. This bot is added as an admin with posting permission\n\n"
            "I'll verify both and send a self-deleting test post.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]),
            parse_mode='Markdown'
        )
        
    elif action == "signatures":
        # Pick which channel's signature to edit
        channels = Database.get_user_channels(user.id)
        keyboard = []
        for channel in channels:
            marker = " ✍️" if channel.get('signature') else ""
            keyboard.append([InlineKeyboardButton(
                f"📺 {channel['channel_name']}{marker}",
                callback_data=f"channels_sig_{channel['channel_id']}"
            )])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="main_channels")])
        await query.edit_message_text(
            "✍️ *Caption Signatures*\n\n"
            "A signature is appended to every post published to the channel - "
            "a channel link, hashtags, a call to subscribe.\n\n"
            "Channels marked ✍️ already have one. Pick a channel:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )

    elif action.startswith("sig_"):
        channel_id = action.replace("sig_", "")
        # SECURITY CHECK: channel id comes from callback data
        if not Database.user_has_channel(user.id, channel_id):
            await query.edit_message_text("❌ You don't have access to this channel.")
            return
        current = Database.get_channel_signature(user.id, channel_id)
        Database.update_user_session(user.id, "waiting_channel_signature", {'signature_channel_id': channel_id})
        current_text = f"*Current signature:*\n{escape_markdown(current)}\n\n" if current else "No signature set yet.\n\n"
        await query.edit_message_text(
            f"✍️ *Signature for this channel*\n\n"
            f"{current_text}"
            f"Send the new signature text (it will be appended to every "
            f"caption after a blank line), or send `-` to remove it.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]),
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

async def channels_handler_inline(query, user):
    """Handle inline channels management"""
    channels = Database.get_user_channels(user.id)
    
    keyboard = []
    
    if channels:
        keyboard.append([InlineKeyboardButton("📋 View All Channels", callback_data="channels_list")])
        keyboard.append([InlineKeyboardButton("➕ Add New Channel", callback_data="channels_add"),
                         InlineKeyboardButton("🗑️ Remove Channel", callback_data="channels_remove")])
        keyboard.append([InlineKeyboardButton("✍️ Caption signatures", callback_data="channels_signatures")])
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
    
    # Show upcoming scheduled posts (first 10 - unbounded output exceeds
    # Telegram's 4096-char message limit and the send fails)
    if scheduled_posts:
        message += "*⏰ Upcoming Posts:*\n"
        for i, post in enumerate(scheduled_posts[:10]):
            try:
                from datetime import datetime
                scheduled_dt = datetime.fromisoformat(post['scheduled_time'])
                time_str = scheduled_dt.strftime("%m/%d %H:%M")
                media_icon = {'photo': '📸', 'video': '🎥', 'audio': '🎵', 'animation': '🎬', 'document': '📄'}.get(post['media_type'], '📁')

                desc = escape_markdown((post['description'] or "No description")[:60])

                message += f"{i+1}. {time_str} {media_icon} - {desc}\n"
            except (ValueError, TypeError, AttributeError) as e:
                message += f"{i+1}. {post['scheduled_time']} - {post['media_type']}\n"
        if len(scheduled_posts) > 10:
            message += f"_... and {len(scheduled_posts) - 10} more_\n"
    
    # Create keyboard with navigation options
    keyboard = []
    
    # Add individual post management buttons for Mode 2 posts
    mode2_posts = [p for p in scheduled_posts if p['mode'] == 2]
    if mode2_posts:
        keyboard.append([InlineKeyboardButton("✏️ Edit Captioned Posts", callback_data=f"edit_mode2_{channel_id}")])
    
    keyboard.extend([
        [InlineKeyboardButton("🔙 Back to Channels", callback_data="stats_channels")],
        [InlineKeyboardButton("📊 Back to Stats", callback_data="main_stats")]
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
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

Use /mode1 or /mode2 to upload and schedule content.
"""
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        return
    
    message = f"""
📅 *Scheduled Posts for {channel['channel_name']}*

*Total scheduled:* {len(scheduled_posts)} posts

"""
    
    # Sort posts by scheduled time
    scheduled_posts.sort(key=lambda x: x['scheduled_time'])

    # Cap the listing - an unbounded loop exceeds Telegram's 4096-char message
    # limit once there are more than a screenful of posts, and the send fails
    max_shown = 20
    for i, post in enumerate(scheduled_posts[:max_shown], 1):
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

        # Truncate long descriptions so the message stays under the size limit
        description = escape_markdown((post['description'] or 'No description')[:80])

        # Add recurring indicator
        recurring_indicator = " 🔄" if post['is_recurring'] else ""

        message += f"{i}. {media_icon} *{date_str} {time_str}*{recurring_indicator}\n"
        message += f"   ID: #{post['id']}\n"
        message += f"   {description}\n\n"

    if len(scheduled_posts) > max_shown:
        message += f"_... and {len(scheduled_posts) - max_shown} more posts_\n\n"

    message += "*💡 Tip:* Use /stats for detailed analytics"

    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

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
            
            message = f"""📸 *Bulk Upload*

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
            
            message = f"""📝 *Upload with Captions*

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
    
    from ..utils import get_media_icon
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

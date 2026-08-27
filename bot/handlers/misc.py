"""Auto-split from the former bot/handlers.py - see __init__.py."""

from .common import *  # noqa: F401,F403
from .common import logger  # noqa: F401


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
    elif mode == "waiting_channel_signature":
        await handle_channel_signature_input(update, user, text, session_data)
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
        success = Database.update_post_schedule(post_id, scheduled_dt, user_id=user.id)
        
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
        "All pending posts have been cleared.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Bulk upload", callback_data="main_mode1"),
             InlineKeyboardButton("📝 With captions", callback_data="main_mode2")],
            [InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]
        ]),
        parse_mode='Markdown'
    )

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
    
    nav = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Upload more", callback_data="main_mode1"),
         InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]
    ])

    if action == "confirm":
        # Clear all queued posts
        cleared_count = Database.clear_queued_posts(user.id)

        if cleared_count > 0:
            await query.edit_message_text(
                f"✅ *Queue Cleared Successfully!*\n\n"
                f"Removed *{cleared_count} posts* from your queue.",
                reply_markup=nav,
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                "📭 *No posts to clear.*\n\n"
                "Your queue was already empty.",
                reply_markup=nav,
                parse_mode='Markdown'
            )

    elif action == "cancel":
        await query.edit_message_text("❌ Queue clearing cancelled.", reply_markup=nav)

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
        success = Database.update_post_description(post_id, new_description, user_id=user.id)
        
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

async def retry_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /retry command to retry failed posts"""
    user = update.effective_user

    message, reply_markup = build_retry_menu(user)
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

def build_retry_menu(user):
    """Build the failed-posts recovery menu: (message, reply_markup).

    Shared by the /retry command, the dashboard alert button, and the digest.
    """
    failed_posts = Database.get_failed_posts(user.id)

    if not failed_posts:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]])
        return (
            "✅ **No Failed Posts**\n\n"
            "You don't have any failed posts to retry.\n"
            "All your posts were published successfully!",
            keyboard
        )

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

    return message, reply_markup

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
            if Database.retry_failed_post(post['id'], user.id):
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

        if Database.retry_failed_post(post_id, user.id):
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
            if Database.retry_failed_post(post['id'], user.id):
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

async def show_mode_selection_menu(query, user):
    """Show mode selection menu for bulk edit"""
    posts = Database.get_scheduled_posts_for_channel(user.id)
    
    # Group posts by mode
    posts_by_mode = {"mode1": [], "mode2": [], "recurring": [], "multibatch": []}
    
    for post in posts:
        if post.get('is_recurring'):
            posts_by_mode["recurring"].append(post)
        elif post.get('batch_id'):
            posts_by_mode["multibatch"].append(post)
        elif post.get('description') and len(post['description']) > 50:
            posts_by_mode["mode2"].append(post)
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
            f"Are you sure you want to delete backup '{escape_markdown(backup_name)}'?\n\n"
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
                f"Backup '{escape_markdown(backup_name)}' has been successfully deleted.",
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                f"❌ *Error*\n\n"
                f"Failed to delete backup '{escape_markdown(backup_name)}'.",
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
            f"🔄 *Restore Backup: {escape_markdown(backup_name)}*\n\n"
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

def build_settings_view(user):
    """Build the settings panel: (message, reply_markup).

    Single renderer shared by /settings and every settings button, so the
    panel always looks the same after any change.
    """
    enabled, threshold, last_sent = Database.get_reminder_settings(user.id)
    notify_mode = Database.get_notify_mode(user.id)

    keyboard = [
        [InlineKeyboardButton(
            f"🔔 Reminders: {'ON' if enabled else 'OFF'}",
            callback_data="settings_toggle_reminder"
        )],
        [
            InlineKeyboardButton("➖", callback_data="settings_threshold_dec"),
            InlineKeyboardButton(f"Threshold: {threshold} posts", callback_data="settings_threshold_info"),
            InlineKeyboardButton("➕", callback_data="settings_threshold_inc")
        ],
        [InlineKeyboardButton(
            f"Notifications: {NOTIFY_MODE_LABELS[notify_mode]}",
            callback_data="settings_notify_cycle"
        )],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Format last reminder time
    last_reminder_text = "Never"
    if last_sent:
        time_since = get_current_kyiv_time() - last_sent
        hours_ago = int(time_since.total_seconds() / 3600)
        if hours_ago < 1:
            last_reminder_text = "Less than 1 hour ago"
        elif hours_ago < 24:
            last_reminder_text = f"{hours_ago} hours ago"
        else:
            last_reminder_text = f"{hours_ago // 24} days ago"

    notify_help = {
        'all': "you get a message for every published post",
        'digest': "you get one summary per day at 9:00 (failures still alert immediately)",
        'errors': "you are only notified when something fails",
    }[notify_mode]

    message = f"""
⚙️ *Settings*

*Low-queue reminders:*
• *Reminders:* {'Enabled ✅' if enabled else 'Disabled ❌'}
• *Alert threshold:* {threshold} posts
• *Last reminder:* {last_reminder_text}

When your unscheduled posts drop to or below {threshold}, you'll get a reminder (checked hourly, max once per day).

*Notifications:* {NOTIFY_MODE_LABELS[notify_mode]}
Right now {notify_help}.

Tap a button to change a setting:
"""
    return message, reply_markup

async def settings_handler(update, context):
    """Handle /settings command"""
    user = update.effective_user
    message, reply_markup = build_settings_view(user)
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_settings_callback(query, user, data):
    """Handle settings button presses: apply the change, re-render the panel."""
    action = data.replace("settings_", "")

    if action == "toggle_reminder":
        enabled, _threshold, _ = Database.get_reminder_settings(user.id)
        Database.update_reminder_settings(user.id, enabled=not enabled)
    elif action == "threshold_inc":
        _enabled, threshold, _ = Database.get_reminder_settings(user.id)
        Database.update_reminder_settings(user.id, threshold=min(threshold + 1, 50))
    elif action == "threshold_dec":
        _enabled, threshold, _ = Database.get_reminder_settings(user.id)
        Database.update_reminder_settings(user.id, threshold=max(threshold - 1, 1))
    elif action == "notify_cycle":
        order = ['all', 'digest', 'errors']
        current = Database.get_notify_mode(user.id)
        Database.set_notify_mode(user.id, order[(order.index(current) + 1) % len(order)])
    elif action == "threshold_info":
        await query.answer("Reminder fires when unscheduled posts drop to this number")
        return
    else:
        logger.warning(f"Unknown settings action: {action}")
        return

    message, reply_markup = build_settings_view(user)
    try:
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
    except Exception as e:
        # "message is not modified" is harmless; anything else should log
        if 'not modified' not in str(e).lower():
            raise

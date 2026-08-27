"""Auto-split from the former bot/handlers.py - see __init__.py."""

from .common import *  # noqa: F401,F403
from .common import logger  # noqa: F401


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
    elif action in ("start_in1h", "start_today18", "start_tomorrow10"):
        await handle_recurring_start_choice(query, user, action.replace("start_", ""), context)
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
    # Exclude the specific recur_* prefixes handled by their own branches below,
    # which this generic prefix match would otherwise shadow
    elif action.startswith("recur_") and not action.startswith(("recur_count_", "recur_date_", "recur_never_", "recur_channel_")):
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

async def setup_individual_recurring_post_with_channel(query, user, interval_hours, channel_id, recurring_count=None, recurring_end_date=None, first_post_time=None):
    """Set up individual recurring post with specific channel"""
    # Get the post data from the current recurring mode session.
    # Don't gate on session mode here: by this point the flow has moved through
    # several states (waiting_recurring_count, RECURRING_START_TIME, ...) -
    # the file_path check below is the real validation.
    mode, session_data = Database.get_user_session(user.id)

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
        [InlineKeyboardButton("⏱ In 1 hour", callback_data="recurring_start_in1h"),
         InlineKeyboardButton("🌆 Today 18:00", callback_data="recurring_start_today18")],
        [InlineKeyboardButton("🌅 Tomorrow 10:00", callback_data="recurring_start_tomorrow10"),
         InlineKeyboardButton("📅 Custom...", callback_data="recurring_start_custom")],
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
        [InlineKeyboardButton("⏱ In 1 hour", callback_data="recurring_start_in1h"),
         InlineKeyboardButton("🌆 Today 18:00", callback_data="recurring_start_today18")],
        [InlineKeyboardButton("🌅 Tomorrow 10:00", callback_data="recurring_start_tomorrow10"),
         InlineKeyboardButton("📅 Custom...", callback_data="recurring_start_custom")],
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

    elif start_type in ("in1h", "today18", "tomorrow10"):
        # Quick presets - no typing needed
        now = get_current_kyiv_time()
        if start_type == "in1h":
            first_post_time = now + timedelta(hours=1)
        elif start_type == "today18":
            first_post_time = now.replace(hour=18, minute=0, second=0, microsecond=0)
            if first_post_time <= now:
                first_post_time += timedelta(days=1)
        else:  # tomorrow10
            first_post_time = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
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
                        WHERE id = ? AND user_id = ?
                    ''', (interval_hours, count, post_id, user.id))
                    conn.commit()
                    conn.close()
                    
                    # Schedule the first occurrence
                    next_time = get_current_kyiv_time() + timedelta(minutes=1)
                    Database.update_post_schedule(post_id, next_time, user_id=user.id)
                    
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
            # Set up bulk recurring posts (synchronous helper - do not await)
            success = setup_recurring_posts_direct(user, interval_hours, count, None)
            
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
            # Set up bulk recurring posts (synchronous helper - do not await)
            success = setup_recurring_posts_direct(user, interval_hours, None, end_date)
            
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
            f"🔄 *Recurring Post #{post_id}* ({index+1}/{total})\n\n"
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
                await scheduler.cancel_post_job(post_id)
            except Exception as e:
                logger.warning(f"Could not cancel job for post {post_id}: {e}")

        success = Database.delete_post(post_id, user.id)
        
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
                 'url': e.url, 'user': e.user.id if e.user else None, 'language': e.language,
                     'custom_emoji_id': e.custom_emoji_id}
                for e in caption_entities
            ])
    
    success = Database.update_post_description(post_id, new_caption, caption_entities_json, user_id=user.id)

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

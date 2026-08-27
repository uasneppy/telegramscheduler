"""Auto-split from the former bot/handlers.py - see __init__.py."""

from .common import *  # noqa: F401,F403
from .common import logger  # noqa: F401


# Scheduler will be accessed from application context

def get_shared_scheduler(context):
    """Return the application's running PostScheduler, or None if unavailable.

    Never construct a fallback PostScheduler() here: a fresh instance is
    never started, so jobs added to it silently never fire.
    """
    if context and getattr(context, 'application', None) and context.application.bot_data:
        return context.application.bot_data.get('scheduler')
    return None

async def schedule_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /schedule command"""
    user = update.effective_user

    message, reply_markup = build_schedule_options(user)
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

def build_schedule_options(user):
    """Build the scheduling options panel: (message, reply_markup).

    Shared by the /schedule command and the upload status message's
    "Schedule now" button.
    """
    # Check if user has unscheduled posts to schedule
    pending_posts = Database.get_unscheduled_posts(user.id)

    if not pending_posts:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Bulk upload", callback_data="main_mode1"),
             InlineKeyboardButton("📝 With captions", callback_data="main_mode2")],
            [InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]
        ])
        return ("❌ No posts to schedule yet.\n\nUpload some media first:", keyboard)

    # Check if user has channels configured
    channels = Database.get_user_channels(user.id)

    if not channels:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add a channel", callback_data="channels_add")],
            [InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]
        ])
        return ("❌ *No channels configured!*\n\nAdd a channel first:", keyboard)

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
    keyboard.append([InlineKeyboardButton("⏭️ Next Available Slot", callback_data="schedule_next_slot"),
                     InlineKeyboardButton("🔀 Shuffle order", callback_data="queue_shuffle")])
    keyboard.append([InlineKeyboardButton("⚙️ Change Settings", callback_data="schedule_custom"),
                     InlineKeyboardButton("📅 Custom Date", callback_data="schedule_custom_date")])
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

    return message, reply_markup

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
            'UPDATE posts SET scheduled_time = ? WHERE id = ? AND user_id = ?',
            (scheduled_time.isoformat(), post_id, user.id)
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
    
    done_nav = InlineKeyboardMarkup([
        [InlineKeyboardButton("👁 Preview schedule", callback_data="main_preview"),
         InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]
    ])
    try:
        await query.edit_message_text(msg, reply_markup=done_nav, parse_mode='Markdown')
    except Exception:
        await query.message.reply_text(msg, reply_markup=done_nav, parse_mode='Markdown')
    
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
            'UPDATE posts SET scheduled_time = ? WHERE id = ? AND user_id = ?',
            (scheduled_time.isoformat(), post_id, user.id)
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
    
    done_nav = InlineKeyboardMarkup([
        [InlineKeyboardButton("👁 Preview schedule", callback_data="main_preview"),
         InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]
    ])
    try:
        await query.edit_message_text(msg, reply_markup=done_nav, parse_mode='Markdown')
    except Exception:
        await query.message.reply_text(msg, reply_markup=done_nav, parse_mode='Markdown')
    
    # Clear only the posts that were just scheduled (channel-specific clearing)
    # Get the unique channels that were scheduled
    scheduled_channels = set(post['channel_id'] for post in pending_posts)
    for channel_id in scheduled_channels:
        Database.clear_queued_posts(user.id, channel_id)
    
    # Reset user session
    Database.update_user_session(user.id, BotStates.IDLE)

def _hour_grid(hours, prefix, columns=6):
    """Build an inline-keyboard grid of hour buttons."""
    rows = []
    for i in range(0, len(hours), columns):
        rows.append([InlineKeyboardButton(f"{h:02d}", callback_data=f"{prefix}{h}")
                     for h in hours[i:i + columns]])
    return rows

async def prompt_custom_schedule(query, user):
    """Prompt user for custom schedule settings - tap-based picker.

    Typed "start end interval" input still works as a fallback.
    """
    Database.update_user_session(user.id, BotStates.WAITING_SCHEDULE_INPUT)

    keyboard = _hour_grid(list(range(0, 24)), "cfg_s_")
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])

    await query.edit_message_text(
        "⚙️ *Schedule Settings - Step 1 of 3*\n\n"
        "*Tap the hour of the first post of the day* (Kyiv time):\n\n"
        "_Or type all three at once, e.g. `10 20 2` = 10:00-20:00 every 2 hours._",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def handle_schedule_config_callback(query, user, data):
    """Tap-based schedule config: cfg_s_{h} -> cfg_e_{h} -> cfg_i_{n}."""
    mode, session_data = Database.get_user_session(user.id)

    if data.startswith("cfg_s_"):
        start_hour = int(data.replace("cfg_s_", ""))
        session_data['cfg_start'] = start_hour
        Database.update_user_session(user.id, BotStates.WAITING_SCHEDULE_INPUT, session_data)

        keyboard = _hour_grid(list(range(start_hour + 1, 24)), "cfg_e_")
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="schedule_custom"),
                         InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        await query.edit_message_text(
            f"⚙️ *Schedule Settings - Step 2 of 3*\n\n"
            f"Daily window starts at *{start_hour:02d}:00*.\n"
            f"*Tap the hour it should end:*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )

    elif data.startswith("cfg_e_"):
        end_hour = int(data.replace("cfg_e_", ""))
        start_hour = session_data.get('cfg_start')
        if start_hour is None or end_hour <= start_hour:
            await query.answer("Please pick the start hour first", show_alert=True)
            return
        session_data['cfg_end'] = end_hour
        Database.update_user_session(user.id, BotStates.WAITING_SCHEDULE_INPUT, session_data)

        window = end_hour - start_hour
        intervals = [i for i in (1, 2, 3, 4, 6, 8, 12) if i <= window]
        keyboard = [[InlineKeyboardButton(f"every {i}h", callback_data=f"cfg_i_{i}")
                     for i in intervals[j:j + 4]] for j in range(0, len(intervals), 4)]
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"cfg_s_{start_hour}"),
                         InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        await query.edit_message_text(
            f"⚙️ *Schedule Settings - Step 3 of 3*\n\n"
            f"Daily window: *{start_hour:02d}:00 - {end_hour:02d}:00*.\n"
            f"*Tap the posting interval:*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )

    elif data.startswith("cfg_i_"):
        interval_hours = int(data.replace("cfg_i_", ""))
        start_hour = session_data.get('cfg_start')
        end_hour = session_data.get('cfg_end')
        if start_hour is None or end_hour is None:
            await query.answer("Please pick the hours first", show_alert=True)
            return

        Database.update_scheduling_config(user.id, start_hour, end_hour, interval_hours)
        session_data.pop('cfg_start', None)
        session_data.pop('cfg_end', None)
        Database.update_user_session(user.id, BotStates.IDLE, session_data)

        pending_posts = Database.get_unscheduled_posts(user.id)
        preview = ""
        keyboard = [[InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]]
        if pending_posts:
            schedule_times = calculate_schedule_times(start_hour, end_hour, interval_hours, len(pending_posts))
            preview = f"\n*Preview for your {len(pending_posts)} queued posts:*\n{format_schedule_summary(schedule_times)}\n"
            keyboard.insert(0, [InlineKeyboardButton("✅ Schedule posts now", callback_data="schedule_current")])

        await query.edit_message_text(
            f"✅ *Schedule saved!*\n\n"
            f"*Window:* {start_hour:02d}:00 - {end_hour:02d}:00 (Kyiv)\n"
            f"*Interval:* every {interval_hours}h\n"
            f"{preview}",
            reply_markup=InlineKeyboardMarkup(keyboard),
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

async def execute_custom_date_scheduling(update: Update, user, pending_posts: list, schedule_times: list, context=None):
    """Execute scheduling with custom dates"""
    try:
        scheduler = get_shared_scheduler(context)
        if not scheduler:
            # Scheduled times are stored in the DB; the monitor job will pick them up
            logger.error("Shared scheduler unavailable in execute_custom_date_scheduling - relying on overdue monitor")

        # Prepare post IDs and schedule times for batch scheduling
        post_ids = []
        valid_schedule_times = []

        for i, post in enumerate(pending_posts):
            if i < len(schedule_times):
                scheduled_time = schedule_times[i]

                # Update the post with the scheduled time
                Database.update_post_schedule(post['id'], scheduled_time, user_id=user.id)

                # Add to batch scheduling lists
                post_ids.append(post['id'])
                valid_schedule_times.append(scheduled_time)

        # Schedule all posts at once using the scheduler
        if scheduler and post_ids and valid_schedule_times:
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

async def handle_clearscheduled_callback(query, user, data, context=None):
    """Handle clearscheduled confirmation callbacks"""
    if data == "clearscheduled_confirm_all":
        # Cancel the scheduler jobs FIRST (while the rows still exist to find
        # them), without touching the unscheduled queue
        try:
            scheduler = get_shared_scheduler(context)
            if scheduler:
                scheduler.cancel_user_posts(user.id, clear_queue=False)
            else:
                logger.error("Shared scheduler unavailable in clearscheduled - active jobs not cancelled")
        except Exception as e:
            logger.warning(f"Failed to cancel scheduled jobs: {e}")

        # Then clear the scheduled posts from the database
        cleared_count = Database.clear_scheduled_posts(user.id)
        
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
        
        # Cancel this channel's scheduler jobs before deleting the rows
        try:
            scheduler = get_shared_scheduler(context)
            if scheduler:
                for post in Database.get_pending_posts(user.id, channel_id=channel_id):
                    await scheduler.cancel_post_job(post['id'])
        except Exception as e:
            logger.warning(f"Failed to cancel channel jobs: {e}")

        # Clear scheduled posts for this channel
        cleared_count = Database.clear_scheduled_posts(user.id, channel_id)
        
        await query.edit_message_text(
            f"✅ *Channel Scheduled Posts Cleared*\n\n"
            f"Successfully cleared *{cleared_count} scheduled posts* from channel:\n"
            f"*{channel['channel_name']}* ({channel_id})\n\n"
            f"Media files have been removed. Other channels' schedules remain intact.",
            parse_mode='Markdown'
        )

async def calendar_view_handler(query, user):
    """Display calendar view with scheduled posts"""
    from ..utils import get_current_kyiv_time
    
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
        from ..utils import get_current_kyiv_time
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
    from ..utils import get_current_kyiv_time
    
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
        elif post.get('batch_id'):
            posts_by_mode["multibatch"].append(post)
        elif post.get('description') and len(post['description']) > 50:
            posts_by_mode["mode2"].append(post)
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
        info_text += f"• 📸 Bulk uploads: {len(posts_by_mode['mode1'])} posts\n"
    if posts_by_mode["mode2"]:
        info_text += f"• 📝 With captions: {len(posts_by_mode['mode2'])} posts\n"
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
            elif post.get('batch_id'):
                posts_by_mode["multibatch"].append(post)
            elif post.get('description') and len(post['description']) > 50:
                posts_by_mode["mode2"].append(post)
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
            info_text += f"• 📸 Bulk uploads: {len(posts_by_mode['mode1'])} posts\n"
        if posts_by_mode["mode2"]:
            info_text += f"• 📝 With captions: {len(posts_by_mode['mode2'])} posts\n"
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

async def handle_overdue_callback(query, user, data, context=None):
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
        
        # Actually post all overdue posts immediately using the shared scheduler
        scheduler = get_shared_scheduler(context)
        if not scheduler:
            logger.error("Shared scheduler unavailable for overdue posting")
            await query.edit_message_text("❌ Scheduler unavailable. Please try again.")
            return

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

        scheduler = get_shared_scheduler(context)
        if not scheduler:
            logger.error("Shared scheduler unavailable for channel overdue posting")
            await query.edit_message_text("❌ Scheduler unavailable. Please try again.")
            return
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
            # SECURITY: post_id comes from forgeable callback data - verify ownership first
            post = Database.get_post_by_id(post_id)
            if not post or post.get('user_id') != user.id:
                await query.edit_message_text("❌ Post not found or access denied.")
                return

            # Actually post the content to Telegram instead of just marking as posted
            scheduler = get_shared_scheduler(context)
            if not scheduler:
                logger.error("Shared scheduler unavailable for individual overdue posting")
                await query.edit_message_text("❌ Scheduler unavailable. Please try again.")
                return
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
    
    elif action in ("main", "refresh"):
        # Show the actual current overdue posts (same view as the main menu button)
        await handle_main_overdue_callback(query, user)

    elif action.startswith("channel_"):
        # Show individual overdue posts for a specific channel
        channel_id = action.replace("channel_", "")
        await show_channel_overdue_posts(query, user, channel_id)

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
            "❌ *No channels configured!*\n\n"
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
            "📅 *No Pending Posts*\n\n"
            "You dont have any pending posts to reschedule.\n\n"
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
                "❌ *Invalid format!*\n\n"
                "Please use format: `start_hour end_hour interval_hours`\n\n"
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
            "❌ *Invalid numbers!*\n\n"
            "Please enter valid numbers for hours.\n\n"
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
            f"❌ *Error during rescheduling:*\n\n{str(e)}",
            reply_markup=reply_markup,
            parse_mode="Markdown"
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

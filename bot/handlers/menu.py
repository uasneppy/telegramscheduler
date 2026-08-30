"""Auto-split from the former bot/handlers.py - see __init__.py."""

from .common import *  # noqa: F401,F403
from .common import logger  # noqa: F401



def build_main_menu(user):
    """Build the main menu as a live dashboard: (message, reply_markup).

    Shared by /start and every "back to menu" navigation so both stay in sync.
    """
    stats = Database.get_dashboard_stats(user.id)

    # First run: no channels yet - show guided onboarding instead of the menu
    if stats['channels'] == 0:
        keyboard = [
            [InlineKeyboardButton("➕ Add my first channel", callback_data="channels_add")],
            [InlineKeyboardButton("❓ Help", callback_data="main_help")]
        ]
        message = (
            f"👋 *Welcome, {escape_markdown(user.first_name or 'there')}!*\n\n"
            "I schedule posts to your Telegram channels - bulk uploads, "
            "captions, albums, recurring posts and more.\n\n"
            "*Let's set up your first channel:*\n\n"
            "*1.* Add this bot as an *administrator* to your channel "
            "(with permission to post)\n"
            "*2.* Tap the button below\n"
            "*3.* Forward me any post from the channel - or send its "
            "`@name` / `-100...` ID\n\n"
            "I'll verify you're an admin and send a quick self-deleting "
            "test post to confirm everything works."
        )
        return message, InlineKeyboardMarkup(keyboard)

    # Dashboard summary line(s)
    summary = (
        f"📺 *{stats['channels']}* channels · "
        f"📥 *{stats['queued']}* queued · "
        f"📅 *{stats['scheduled']}* scheduled"
    )
    if stats['next_post']:
        np = stats['next_post']
        summary += (f"\n⏭ Next post: *{np['time'].strftime('%d.%m %H:%M')}* → "
                    f"{escape_markdown(np['channel_name'])}")

    keyboard = [
        [InlineKeyboardButton("📤 Bulk upload", callback_data="main_mode1"),
         InlineKeyboardButton("📝 With captions", callback_data="main_mode2")],
        [InlineKeyboardButton("🎯 Guided captioning", callback_data="main_mode3"),
         InlineKeyboardButton("📦 Batches", callback_data="main_multibatch")],
        [InlineKeyboardButton("🔄 Recurring posts", callback_data="main_recurring"),
         InlineKeyboardButton("♻️ Manage recurring", callback_data="recurring_manage_menu")],
    ]
    # Alert rows appear only when there is something to act on
    if stats['failed']:
        keyboard.append([InlineKeyboardButton(
            f"⚠️ {stats['failed']} failed - review & retry", callback_data="main_retry")])
    if stats['overdue']:
        keyboard.append([InlineKeyboardButton(
            f"⏰ {stats['overdue']} overdue - manage", callback_data="main_overdue")])
    keyboard.extend([
        [InlineKeyboardButton("👁 Preview", callback_data="main_preview"),
         InlineKeyboardButton("📅 Calendar", callback_data="main_calendar")],
        [InlineKeyboardButton("✏️ Edit posts", callback_data="main_editposts"),
         InlineKeyboardButton("🔁 Reschedule all", callback_data="main_reschedule")],
        [InlineKeyboardButton("🔀 Shuffle scheduled", callback_data="shuffle_scheduled")],
        [InlineKeyboardButton("📺 Channels", callback_data="main_channels"),
         InlineKeyboardButton("📊 Statistics", callback_data="main_stats")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="main_settings"),
         InlineKeyboardButton("❓ Help", callback_data="main_help")],
    ])

    message = (
        f"👋 *Hi, {escape_markdown(user.first_name or 'there')}!*\n\n"
        f"{summary}\n\n"
        "Choose an action:"
    )
    return message, InlineKeyboardMarkup(keyboard)

async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status - operational health overview."""
    import shutil
    from config import DATA_DIR, DATABASE_PATH, MAX_USER_STORAGE_MB
    from bot.utils import get_user_disk_usage

    user = update.effective_user

    uptime = datetime.now() - BOT_STARTED_AT
    days, rem = divmod(int(uptime.total_seconds()), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    uptime_str = (f"{days}d " if days else "") + f"{hours}h {minutes:02d}m"

    scheduler = get_shared_scheduler(context)
    job_count = scheduler.get_scheduled_jobs_count() if scheduler else "unavailable"

    try:
        db_size_mb = os.path.getsize(DATABASE_PATH) / (1024 * 1024)
        db_size_str = f"{db_size_mb:.1f} MB"
    except OSError:
        db_size_str = "unknown"

    try:
        disk = shutil.disk_usage(DATA_DIR)
        disk_str = f"{disk.free / (1024**3):.1f} GB free of {disk.total / (1024**3):.1f} GB"
    except OSError:
        disk_str = "unknown"

    my_usage_mb = get_user_disk_usage(user.id) / (1024 * 1024)
    quota_str = f"{my_usage_mb:.0f} MB" + (f" of {MAX_USER_STORAGE_MB} MB" if MAX_USER_STORAGE_MB else " (no quota)")

    stats = Database.get_dashboard_stats(user.id)

    await update.message.reply_text(
        f"🩺 *Bot Status*\n\n"
        f"*Uptime:* {uptime_str}\n"
        f"*Scheduler jobs:* {job_count}\n"
        f"*Database:* {db_size_str}\n"
        f"*Disk:* {disk_str}\n"
        f"*Your media storage:* {quota_str}\n\n"
        f"*Your posts:* {stats['queued']} queued · {stats['scheduled']} scheduled · "
        f"{stats['failed']} failed · {stats['overdue']} overdue",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]]),
        parse_mode='Markdown'
    )

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    if not update or not update.effective_user or not update.message:
        logger.error("Invalid update in start_handler")
        return

    user = update.effective_user

    message, reply_markup = build_main_menu(user)
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

    # Reset user session
    Database.update_user_session(user.id, BotStates.IDLE)

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    user = update.effective_user
    
    # Create inline keyboard with options
    keyboard = [
        [InlineKeyboardButton("📸 Bulk Upload Help", callback_data="help_mode1"),
         InlineKeyboardButton("📝 Upload-with-Captions Help", callback_data="help_mode2")],
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
2. Choose your upload mode (bulk, with captions, or recurring)
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
• `/recover_captions` - Info about caption recovery (list posts missing captions)
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
                "Please add a channel first before uploading.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add a channel", callback_data="channels_add")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]
                ]),
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
                "Please add a channel first before uploading.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add a channel", callback_data="channels_add")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]
                ]),
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
                "Please add a channel first before uploading.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add a channel", callback_data="channels_add")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]
                ]),
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

    elif action == "multibatch":
        # Multi-channel batch scheduler
        await show_multibatch_menu(query, user)

    elif action == "retry":
        # Failed posts recovery (dashboard alert button / digest link)
        message, reply_markup = build_retry_menu(user)
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

    elif action == "settings":
        message, reply_markup = build_settings_view(user)
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

async def help_handler_inline(query, user):
    """Handle inline help display"""
    keyboard = [
        [InlineKeyboardButton("📸 Bulk Upload Help", callback_data="help_mode1")],
        [InlineKeyboardButton("📝 Upload-with-Captions Help", callback_data="help_mode2")],
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
    """Show the main menu (live dashboard)"""
    message, reply_markup = build_main_menu(user)

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
📸 **Bulk Upload Help**

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
📝 **Upload with Captions Help**

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

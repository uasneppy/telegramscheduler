"""Auto-split from the former bot/handlers.py - see __init__.py."""

from .common import *  # noqa: F401,F403
from .common import logger  # noqa: F401


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
        SELECT file_path, media_type, description, media_bundle_json
        FROM posts
        WHERE id = ? AND user_id = ?
    ''', (post_id, user_id))

    row = cursor.fetchone()
    conn.close()

    if not row:
        await update_or_query.answer("❌ Post not found!", show_alert=True)
        return

    file_path, media_type, description, media_bundle_json = row

    # Albums: preview as a media group
    if media_type == 'album' and media_bundle_json:
        try:
            from telegram import InputMediaPhoto, InputMediaVideo
            bundle = json.loads(media_bundle_json)
            open_files = []
            media_group = []
            try:
                for i, item in enumerate(bundle[:10]):
                    if not os.path.exists(item['file_path']):
                        continue
                    f = open(item['file_path'], 'rb')
                    open_files.append(f)
                    caption = f"🔍 Preview\n\n{description or 'No caption'}" if i == 0 else None
                    if item['media_type'] in ('photo', 'document_image'):
                        media_group.append(InputMediaPhoto(media=f, caption=caption))
                    elif item['media_type'] in ('video', 'document_video'):
                        media_group.append(InputMediaVideo(media=f, caption=caption))
                if media_group:
                    await update_or_query.message.reply_media_group(media=media_group)
                    await update_or_query.answer("✅ Preview sent!")
                else:
                    await update_or_query.answer("❌ Album media files not found!", show_alert=True)
            finally:
                for f in open_files:
                    f.close()
        except Exception as e:
            logger.error(f"Error sending album preview for post {post_id}: {e}")
            await update_or_query.answer("❌ Error sending preview!", show_alert=True)
        return

    if not os.path.exists(file_path):
        await update_or_query.answer("❌ Media file not found!", show_alert=True)
        return

    try:
        # Send the actual media as preview
        with open(file_path, 'rb') as media_file:
            if media_type in ('photo', 'document_image'):
                await update_or_query.message.reply_photo(
                    photo=media_file,
                    caption=f"🔍 Preview\n\n{description or 'No caption'}" if description else "🔍 Preview\n\nNo caption"
                )
            elif media_type in ('video', 'document_video'):
                await update_or_query.message.reply_video(
                    video=media_file,
                    caption=f"🔍 Preview\n\n{description or 'No caption'}" if description else "🔍 Preview\n\nNo caption"
                )
            elif media_type == 'audio':
                await update_or_query.message.reply_audio(
                    audio=media_file,
                    caption=f"🔍 Preview\n\n{description or 'No caption'}" if description else "🔍 Preview\n\nNo caption"
                )
            elif media_type == 'animation':
                await update_or_query.message.reply_animation(
                    animation=media_file,
                    caption=f"🔍 Preview\n\n{description or 'No caption'}" if description else "🔍 Preview\n\nNo caption"
                )
            else:
                # 'document' and any unknown media type: send as a document so
                # the preview never silently sends nothing
                await update_or_query.message.reply_document(
                    document=media_file,
                    caption=f"🔍 Preview\n\n{description or 'No caption'}" if description else "🔍 Preview\n\nNo caption"
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

async def main_preview_handler(query, user):
    """Handle preview posts from main menu - show channel selection first"""
    
    # Check if user has any posts at all
    all_posts = Database.get_pending_posts(user.id, unscheduled_only=False)
    
    if not all_posts:
        await query.edit_message_text(
            "📭 *No posts found for preview.*\n\n"
            "Upload some media first using /mode1 or /mode2, then you can preview them here.\n\n"
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
                f"*✏️ Edit Captioned Posts - {channel['channel_name']}*\n\n"
                "No Mode 2 scheduled posts found for editing.",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return
        
        # Build post list with edit buttons
        message = f"*✏️ Edit Captioned Posts - {channel['channel_name']}*\n\n"
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
    from ..database import Database
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
        from ..database import Database
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
        # Leave the caption-editing state so later text input isn't misrouted
        Database.update_user_session(user.id, BotStates.IDLE)
        await query.edit_message_text(
            "✅ *Caption Editing Complete*\n\n"
            "All your caption edits have been saved successfully!",
            parse_mode='Markdown'
        )

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
        from ..utils import get_kyiv_timezone
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
    success = Database.update_post_description(post_id, new_caption, user_id=user.id)
    
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
        from ..utils import get_kyiv_timezone
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
        "Select a channel to edit its scheduled captioned posts:\n\n"
        "*Features:*\n"
        "• Edit captions (with formatting preserved)\n"
        "• Replace media files\n"
        "• Reschedule posts\n"
        "• Delete posts\n"
        "• Preview media before changes",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


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
        keyboard.append([InlineKeyboardButton("👁️ Preview Media", callback_data=f"editposts_preview_{post_id}"),
                         InlineKeyboardButton("📝 Edit Caption", callback_data=f"editposts_caption_{post_id}")])
        keyboard.append([InlineKeyboardButton("🖼️ Replace Media", callback_data=f"editposts_media_{post_id}"),
                         InlineKeyboardButton("⏰ Reschedule", callback_data=f"editposts_schedule_{post_id}")])
        keyboard.append([InlineKeyboardButton("📌 Post next (in 1 min)", callback_data=f"editposts_postnext_{post_id}")])
        keyboard.append([InlineKeyboardButton("🗑️ Delete Post", callback_data=f"editposts_delete_{post_id}"),
                         InlineKeyboardButton("🔙 Back", callback_data="editposts_menu")])
        
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


async def handle_editposts_post_next(query, user, post_id, context):
    """'Post next' button: publish this post about a minute from now."""
    try:
        post = Database.get_post_by_id(post_id)

        if not post or post['user_id'] != user.id:
            await query.answer("Post not found or access denied", show_alert=True)
            return

        new_time = get_current_kyiv_time() + timedelta(minutes=1)
        success = Database.update_post_schedule(post_id, new_time, user_id=user.id)

        if not success:
            await query.answer("Failed to reschedule", show_alert=True)
            return

        scheduler = get_shared_scheduler(context)
        if scheduler:
            await scheduler.schedule_single_post(post_id, new_time)

        await query.answer(f"📌 Post #{post_id} will be published at {new_time.strftime('%H:%M')}", show_alert=True)

        # Refresh the card
        mode, session_data = Database.get_user_session(user.id)
        if session_data and 'edit_post_ids' in session_data:
            current_index = session_data.get('edit_current_index', 0)
            channel_id = session_data.get('edit_channel_id')
            channels = Database.get_user_channels(user.id)
            channel = next((ch for ch in channels if ch['channel_id'] == channel_id), None)
            channel_name = channel['channel_name'] if channel else channel_id
            updated_post = Database.get_post_by_id(post_id)
            await show_edit_post_details(query, user, updated_post, current_index,
                                         len(session_data['edit_post_ids']), channel_name)

    except Exception as e:
        logger.error(f"Error in handle_editposts_post_next: {e}", exc_info=True)
        await query.answer(f"Error: {e}", show_alert=True)

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
        success = Database.update_post_schedule(post_id, new_time, user_id=user.id)
        
        if success:
            # Update scheduler if available
            if context and context.application and context.application.bot_data:
                scheduler = context.application.bot_data.get('scheduler')
                if scheduler:
                    await scheduler.schedule_single_post(post_id, new_time)
            
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
            "Select a channel to edit its scheduled captioned posts:",
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
                     'url': e.url, 'user': e.user.id if e.user else None, 'language': e.language,
                     'custom_emoji_id': e.custom_emoji_id}
                    for e in caption_entities
                ])
        
        # Update the post
        success = Database.update_post_description(editing_post_id, new_caption, caption_entities_json, user_id=user.id)
        
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
            success = Database.update_post_schedule(editing_post_id, new_time, user_id=user.id)
            
            if success:
                # Update scheduler
                if context and context.application and context.application.bot_data:
                    scheduler = context.application.bot_data.get('scheduler')
                    if scheduler:
                        await scheduler.schedule_single_post(editing_post_id, new_time)
                
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


async def handle_editposts_media_input(update: Update, user, file_path: str, media_type: str, session_data: dict):
    """Handle media replacement input"""
    editing_post_id = session_data.get('editing_post_id')
    
    if not editing_post_id or session_data.get('editing_type') != 'media':
        return False
    
    # Get old file path for cleanup
    post = Database.get_post_by_id(editing_post_id)
    old_file_path = post.get('file_path') if post else None
    
    # Update the post with new media
    success = Database.update_post_media(editing_post_id, file_path, media_type, user_id=user.id)
    
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

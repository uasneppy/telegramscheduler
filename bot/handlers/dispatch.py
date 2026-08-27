"""Auto-split from the former bot/handlers.py - see __init__.py."""

from .common import *  # noqa: F401,F403
from .common import logger  # noqa: F401


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks"""
    query = update.callback_query
    await query.answer()

    user = query.from_user
    data = query.data

    logger.info(f"Callback received - User: {user.id}, Data: {data}")

    try:
        await _dispatch_callback(query, user, data, context)
    except Exception as e:
        # Without this, any exception leaves the button silently dead
        logger.error(f"Error handling callback {data!r} from user {user.id}: {e}", exc_info=True)
        try:
            await query.message.reply_text("❌ Something went wrong handling that button. Please try again.")
        except Exception:
            pass

async def _dispatch_callback(query, user, data, context):
    """Route callback data to the matching handler"""
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
        await query.edit_message_text(
            "❌ Scheduling cancelled.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]])
        )
    elif data.startswith("cfg_s_") or data.startswith("cfg_e_") or data.startswith("cfg_i_"):
        # Tap-based schedule configuration picker
        await handle_schedule_config_callback(query, user, data)
    elif data == "upload_done":
        # "Schedule now" button on the upload status message
        message, reply_markup = build_schedule_options(user)
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
    elif data == "queue_shuffle":
        # Randomize which queued post lands in which time slot
        count = Database.shuffle_queued_posts(user.id)
        if count == 0:
            await query.answer("Nothing to shuffle - the queue is empty", show_alert=True)
        else:
            await query.answer(f"🔀 Shuffled {count} queued posts!")
    elif data == "upload_discard":
        # Confirm before discarding the queued uploads
        await query.edit_message_text(
            "🗑 *Discard all queued (unscheduled) posts?*\n\n"
            "Scheduled posts are not affected.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, discard", callback_data="clearqueue_confirm"),
                 InlineKeyboardButton("❌ Keep them", callback_data="clearqueue_cancel")]
            ]),
            parse_mode='Markdown'
        )
    elif data == "cancel":
        # Generic cancel button used by several menus
        Database.update_user_session(user.id, BotStates.IDLE)
        await show_main_menu(query, user)
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
        await handle_clearscheduled_callback(query, user, data, context)
    elif data.startswith("batch_mode"):
        await handle_batch_mode_callback(query, user, data)
    elif data.startswith("batch_"):
        await handle_batch_callback(query, user, data, context)
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
        await handle_overdue_callback(query, user, data, context)
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
    elif data.startswith("editposts_postnext_"):
        post_id = int(data.replace("editposts_postnext_", ""))
        await handle_editposts_post_next(query, user, post_id, context)
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
    elif data.startswith("recur_count_") or data.startswith("recur_date_") or data.startswith("recur_never_") or data.startswith("recur_channel_"):
        # End-condition / channel buttons emit bare "recur_*" data; handle_recurring_callback
        # strips a "recurring_" prefix before matching, so re-add it here
        await handle_recurring_callback(query, user, "recurring_" + data, context)
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

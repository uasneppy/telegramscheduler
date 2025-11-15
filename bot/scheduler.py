"""
Post scheduler for handling automatic posting
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
import pytz

from telegram import Bot
from telegram.error import TelegramError
from telegram.request import HTTPXRequest

from .database import Database
from .utils import get_kyiv_timezone, get_current_kyiv_time, cleanup_old_media_files, cleanup_empty_directories
from config import BOT_TOKEN, CHANNEL_ID

logger = logging.getLogger(__name__)

class PostScheduler:
    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone=get_kyiv_timezone())
        
        # Create HTTP request with improved connection pooling
        request = HTTPXRequest(
            connection_pool_size=50,   # Increased for heavy file posting
            pool_timeout=120.0,        # Extended timeout for large files
            read_timeout=600.0,        # 10 minutes for large file uploads
            write_timeout=600.0,       # 10 minutes for large file uploads 
            connect_timeout=60.0       # 1 minute connection timeout
        )
        
        self.bot = Bot(token=BOT_TOKEN, request=request)
        
    def start(self):
        """Start the scheduler"""
        self.scheduler.start()
        logger.info("Post scheduler started")
        
        # Schedule existing pending posts
        self._schedule_existing_posts()
        
        # Schedule daily cleanup at 3 AM Kyiv time
        self.scheduler.add_job(
            self._daily_cleanup,
            'cron',
            hour=3,
            minute=0,
            timezone=get_kyiv_timezone(),
            id='daily_cleanup'
        )
        logger.info("Scheduled daily media cleanup at 3:00 AM Kyiv time")
        
        # Schedule hourly reminder check
        self.scheduler.add_job(
            self._check_and_send_reminders,
            'interval',
            hours=1,
            timezone=get_kyiv_timezone(),
            id='reminder_check'
        )
        logger.info("Scheduled hourly check for post reminders")
        
        # Schedule post monitoring every 5 minutes
        self.scheduler.add_job(
            self._monitor_scheduled_posts,
            'interval',
            minutes=5,
            timezone=get_kyiv_timezone(),
            id='post_monitor'
        )
        logger.info("Scheduled post monitoring every 5 minutes")
    
    def stop(self):
        """Stop the scheduler"""
        self.scheduler.shutdown()
        logger.info("Post scheduler stopped")
    
    def _schedule_existing_posts(self):
        """Schedule all existing pending posts from database"""
        pending_posts = Database.get_pending_posts()
        
        for post in pending_posts:
            if post['scheduled_time']:
                # Handle both timezone-aware and timezone-naive datetimes from database
                scheduled_time = post['scheduled_time']
                if scheduled_time.tzinfo is None:
                    # Database times without timezone info are assumed to be in Kyiv timezone
                    kyiv_tz = get_kyiv_timezone()
                    scheduled_time = kyiv_tz.localize(scheduled_time)
                
                self._schedule_single_post(post['id'], scheduled_time)
    
    async def schedule_posts(self, post_ids: list, scheduled_times: list):
        """Schedule multiple posts with delays to prevent connection pool exhaustion"""
        if len(post_ids) != len(scheduled_times):
            raise ValueError("Number of posts and scheduled times must match")
        
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        for i, (post_id, scheduled_time) in enumerate(zip(post_ids, scheduled_times)):
            self._schedule_single_post(post_id, scheduled_time)
            
            # Update the database with the scheduled time
            cursor.execute(
                'UPDATE posts SET scheduled_time = ? WHERE id = ?',
                (scheduled_time.isoformat(), post_id)
            )
            
            # Add a small delay between scheduling operations to prevent overwhelming the connection pool
            if i < len(post_ids) - 1:  # Don't wait after the last one
                await asyncio.sleep(0.1)  # 100ms delay between scheduling operations
        
        conn.commit()
        conn.close()
        logger.info(f"Scheduled {len(post_ids)} posts")
    
    def _schedule_single_post(self, post_id: int, scheduled_time: datetime):
        """Schedule a single post with proper timezone handling"""
        job_id = f"post_{post_id}"
        
        # Ensure scheduled_time is timezone-aware
        if scheduled_time.tzinfo is None:
            # If timezone-naive, assume it's in Kyiv timezone
            kyiv_tz = get_kyiv_timezone()
            scheduled_time = kyiv_tz.localize(scheduled_time)
        
        # Check if the time is in the past
        current_time = get_current_kyiv_time()
        if scheduled_time <= current_time:
            logger.warning(f"Post {post_id} scheduled for past time {scheduled_time}, skipping")
            return
        
        # Remove existing job if it exists
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
        
        # Add new job with timezone-aware datetime
        self.scheduler.add_job(
            self._post_to_channel,
            trigger=DateTrigger(run_date=scheduled_time),
            args=[post_id],
            id=job_id,
            replace_existing=True
        )
        
        logger.info(f"Scheduled post {post_id} for {scheduled_time}")
    
    async def _post_to_channel(self, post_id: int):
        """Post a single message to the channel with enhanced error handling and recovery"""
        retry_count = 0
        max_retries = 3
        user_id = None
        file_path = None
        channel_id = None

        while retry_count <= max_retries:
            try:
                # Add a small delay at the start to prevent overwhelming Telegram API
                await asyncio.sleep(1.0)  # 1 second delay before each post
                
                # Get complete post details from database using new get_post_by_id method
                post_data = Database.get_post_by_id(post_id)
                
                if not post_data or post_data['status'] != 'pending':
                    logger.warning(f"Post {post_id} not found or already processed")
                    return
                
                file_path = post_data['file_path']
                media_type = post_data['media_type'] or 'photo'  # Default to photo for backward compatibility
                description = post_data['description']
                user_id = post_data['user_id']
                channel_id = post_data['channel_id']
                media_bundle_json = post_data['media_bundle_json']
                
                # Debug logging for caption handling
                logger.info(f"Post {post_id}: Retrieved description='{description}' (type: {type(description).__name__})")
                
                # Check if channel_id is provided
                if not channel_id:
                    logger.error(f"No channel specified for post {post_id}")
                    Database.mark_post_as_failed(post_id, "No channel specified")
                    await self.bot.send_message(
                        chat_id=user_id,
                        text=f"❌ Post #{post_id} failed: No channel specified. Please set up channels first."
                    )
                    return
                
                # SECURITY CHECK: Verify user owns the channel before posting
                if not Database.user_has_channel(user_id, channel_id):
                    error_msg = f"Security violation: User {user_id} does not own channel {channel_id}"
                    logger.error(f"SECURITY ALERT: Post {post_id} - {error_msg}")
                    Database.mark_post_as_failed(post_id, "Channel access denied - security violation")
                    await self.bot.send_message(
                        chat_id=user_id,
                        text=f"❌ Post #{post_id} failed: You don't have permission to post to this channel. Security violation detected."
                    )
                    return
                    
                target_channel = channel_id
                
                # Handle album posts separately
                if media_type == 'album' and media_bundle_json:
                    await self._post_album_to_channel(post_id, media_bundle_json, description, target_channel, user_id)
                    return
                
                # Check if file exists before trying to open it
                if not os.path.exists(file_path):
                    logger.error(f"File not found for post {post_id}: {file_path}")
                    Database.mark_post_as_failed(post_id, f"File not found: {file_path}")
                    await self.bot.send_message(
                        chat_id=user_id,
                        text=f"❌ Post #{post_id} failed: Media file not found."
                    )
                    return
                
                # Send media to channel based on type
                with open(file_path, 'rb') as media_file:
                    if media_type == 'photo':
                        logger.info(f"Post {post_id}: Sending photo with caption='{description}' to {target_channel}")
                        await self.bot.send_photo(
                            chat_id=target_channel,
                            photo=media_file,
                            caption=description
                        )
                    elif media_type == 'video':
                        logger.info(f"Post {post_id}: Sending video with caption='{description}' to {target_channel}")
                        await self.bot.send_video(
                            chat_id=target_channel,
                            video=media_file,
                            caption=description
                        )
                    elif media_type == 'audio':
                        await self.bot.send_audio(
                            chat_id=target_channel,
                            audio=media_file,
                            caption=description
                        )
                    elif media_type == 'animation':
                        await self.bot.send_animation(
                            chat_id=target_channel,
                            animation=media_file,
                            caption=description
                        )
                    elif media_type in ['document', 'document_image', 'document_video']:
                        # Send as document to preserve original quality and file size
                        logger.info(f"Post {post_id}: Sending document with caption='{description}' to {target_channel}")
                        await self.bot.send_document(
                            chat_id=target_channel,
                            document=media_file,
                            caption=description
                        )
                    else:
                        # Default to document for unknown types (preserves quality)
                        await self.bot.send_document(
                            chat_id=target_channel,
                            document=media_file,
                            caption=description
                        )
                
                # Check if this is a recurring post by querying the specific post
                conn = Database.get_connection()
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT is_recurring, recurring_interval_hours, recurring_end_date, 
                           recurring_count, recurring_posted_count
                    FROM posts 
                    WHERE id = ?
                ''', (post_id,))
                
                recurring_row = cursor.fetchone()
                conn.close()
                
                if recurring_row and recurring_row[0]:  # is_recurring is True
                    is_recurring, interval_hours, end_date, total_count, posted_count = recurring_row
                    current_post = {
                        'id': post_id,
                        'is_recurring': is_recurring,
                        'recurring_interval_hours': interval_hours,
                        'recurring_end_date': end_date,
                        'recurring_count': total_count,
                        'recurring_posted_count': posted_count,
                        'user_id': user_id,
                        'file_path': file_path,
                        'media_type': media_type,
                        'description': description,
                        'channel_id': channel_id
                    }
                    await self._handle_recurring_post(current_post)
                else:
                    # Mark as posted for non-recurring posts
                    Database.mark_post_as_posted(post_id)
                
                logger.info(f"Successfully posted {post_id} to channel")
                
                # Notify user
                try:
                    recurring_text = " (recurring)" if recurring_row and recurring_row[0] else ""
                    await self.bot.send_message(
                        chat_id=user_id,
                        text=f"✅ Post #{post_id} has been successfully published to the channel!{recurring_text}"
                    )
                except Exception as e:
                    logger.warning(f"Could not notify user {user_id}: {e}")
                    
                # Success - break out of retry loop
                break
                
            except TelegramError as e:
                error_msg = str(e)
                logger.error(f"Telegram error posting {post_id} (attempt {retry_count + 1}/{max_retries + 1}): {error_msg}")
                
                # Diagnose the error and determine if retry is needed
                diagnosis = await self._diagnose_telegram_error(e, post_id)
                
                if diagnosis['retry_possible'] and retry_count < max_retries:
                    retry_count += 1
                    wait_time = diagnosis.get('wait_time', 2 ** retry_count)
                    logger.info(f"Will retry post {post_id} in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    # Final failure - notify user with diagnosis
                    if user_id is not None:
                        await self._notify_post_failure(post_id, user_id, diagnosis)
                    Database.mark_post_as_failed(post_id, diagnosis['error_message'])
                    break

            except FileNotFoundError as e:
                logger.error(f"File not found for post {post_id}: {e}")
                if user_id is not None:
                    await self._notify_file_error(post_id, user_id, file_path)
                Database.mark_post_as_failed(post_id, "File not found")
                break

            except Exception as e:
                logger.error(f"Unexpected error posting {post_id} (attempt {retry_count + 1}/{max_retries + 1}): {e}")
                
                if retry_count < max_retries:
                    retry_count += 1
                    wait_time = 2 ** retry_count  # Exponential backoff
                    logger.info(f"Will retry post {post_id} in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    if user_id is not None:
                        await self._notify_unexpected_error(post_id, user_id, str(e))
                    Database.mark_post_as_failed(post_id, f"Unexpected error: {e}")
                    break
    
    def cancel_user_posts(self, user_id: int):
        """Cancel all scheduled posts for a user"""
        pending_posts = Database.get_pending_posts(user_id)
        
        for post in pending_posts:
            job_id = f"post_{post['id']}"
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
        
        Database.clear_user_posts(user_id)
        logger.info(f"Cancelled all posts for user {user_id}")
    
    def get_scheduled_jobs_count(self) -> int:
        """Get number of scheduled jobs"""
        return len(self.scheduler.get_jobs())
    
    async def _daily_cleanup(self):
        """Perform daily cleanup of old media files"""
        try:
            logger.info("Starting daily media cleanup...")
            
            # Clean up old media files
            cleaned_files = cleanup_old_media_files()
            
            # Clean up empty directories
            cleaned_dirs = await cleanup_empty_directories()
            
            logger.info(f"Daily cleanup completed: {cleaned_files} files and {cleaned_dirs} directories cleaned")
            
        except Exception as e:
            logger.error(f"Daily cleanup failed: {e}")
    
    async def _monitor_scheduled_posts(self):
        """Monitor scheduled posts and detect/recover from issues"""
        try:
            logger.info("Running scheduled post monitoring...")
            
            # Get posts that should have been posted but weren't
            overdue_posts = []
            try:
                # Get all users and check their overdue posts
                conn = Database.get_connection()
                cursor = conn.cursor()
                cursor.execute('SELECT DISTINCT user_id FROM posts WHERE status = "pending" AND scheduled_time IS NOT NULL')
                user_ids = [row[0] for row in cursor.fetchall()]
                conn.close()
                
                for user_id in user_ids:
                    user_overdue = Database.get_overdue_posts(user_id)
                    overdue_posts.extend(user_overdue)
            except Exception as e:
                logger.error(f"Error fetching overdue posts: {e}")
                
            # Also check for posts that have jobs but weren't detected as overdue
            job_posts = set()
            for job in self.scheduler.get_jobs():
                if job.id.startswith('post_'):
                    try:
                        # Extract the part after 'post_' and ensure it's a valid integer
                        post_id_str = job.id.replace('post_', '')
                        # Only process if it's actually a number (skip system jobs like 'post_monitor')
                        if post_id_str.isdigit():
                            post_id = int(post_id_str)
                            job_posts.add(post_id)
                    except ValueError:
                        # Skip invalid job IDs (like 'post_monitor', 'post_cleanup', etc.)
                        continue
            
            # Get all pending posts that should have active jobs
            conn = Database.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, scheduled_time FROM posts 
                WHERE status = 'pending' AND scheduled_time IS NOT NULL
            ''')
            pending_posts_with_times = cursor.fetchall()
            conn.close()
            
            for post_id, scheduled_time_str in pending_posts_with_times:
                if post_id not in job_posts and scheduled_time_str:
                    # Post has a scheduled time but no active job - reschedule it
                    try:
                        scheduled_time = datetime.fromisoformat(scheduled_time_str)
                        if scheduled_time.tzinfo is None:
                            kyiv_tz = get_kyiv_timezone()
                            scheduled_time = kyiv_tz.localize(scheduled_time)
                        
                        # Only reschedule if it's in the future
                        current_time = get_current_kyiv_time()
                        if scheduled_time > current_time:
                            self._schedule_single_post(post_id, scheduled_time)
                            logger.info(f"Rescheduled missing job for post {post_id}")
                    except Exception as e:
                        logger.error(f"Error rescheduling post {post_id}: {e}")
            
            if overdue_posts:
                logger.warning(f"Found {len(overdue_posts)} overdue posts")
                
                for post in overdue_posts:
                    post_id = post['id']
                    user_id = post['user_id']
                    scheduled_time = post['scheduled_time']
                    
                    # Check if job exists in scheduler
                    job_id = f"post_{post_id}"
                    job = self.scheduler.get_job(job_id)
                    
                    if not job:
                        # Job is missing - reschedule it
                        logger.error(f"Post {post_id} has no scheduled job - rescheduling")
                        
                        # Schedule immediately with a small delay
                        new_time = get_current_kyiv_time() + timedelta(seconds=10)
                        self._schedule_single_post(post_id, new_time)
                        Database.update_post_schedule(post_id, new_time)
                        
                        # Notify user
                        try:
                            await self.bot.send_message(
                                chat_id=user_id,
                                text=f"⚠️ Post #{post_id} was delayed. Rescheduling for immediate posting.",
                                parse_mode='Markdown'
                            )
                        except Exception as e:
                            logger.error(f"Could not notify user {user_id}: {e}")
                    else:
                        # Job exists but didn't execute - might be a scheduler issue
                        logger.warning(f"Post {post_id} has job but didn't execute")
            
            # Check scheduler health
            if not self.scheduler.running:
                logger.error("Scheduler is not running! Attempting restart...")
                self.scheduler.start()
            
            # Log monitoring stats
            job_count = len(self.scheduler.get_jobs())
            logger.info(f"Post monitor completed. Active jobs: {job_count}, Overdue posts: {len(overdue_posts)}")
            
        except Exception as e:
            logger.error(f"Error during post monitoring: {e}")
    
    async def _check_and_send_reminders(self):
        """Check for users with low post counts and send reminders"""
        try:
            logger.info("Checking for users who need post reminders...")
            
            # Get users who need reminders
            users_to_remind = Database.get_users_for_reminders()
            
            for user_id, post_count in users_to_remind:
                try:
                    # Get reminder settings for context
                    enabled, threshold, _ = Database.get_reminder_settings(user_id)
                    
                    # Send reminder message
                    message = (
                        f"⚠️ *Low Post Alert!*\n\n"
                        f"You currently have only *{post_count}* unscheduled posts remaining.\n\n"
                        f"Your reminder threshold is set to {threshold} posts.\n"
                        f"Consider uploading more content to maintain consistent posting!\n\n"
                        f"Use /mode1 or /mode2 to upload new posts.\n"
                        f"Use /settings to adjust reminder preferences."
                    )
                    
                    await self.bot.send_message(
                        chat_id=user_id,
                        text=message,
                        parse_mode='Markdown'
                    )
                    
                    # Update last reminder sent
                    Database.update_last_reminder_sent(user_id)
                    logger.info(f"Sent reminder to user {user_id} (posts: {post_count})")
                    
                except Exception as e:
                    logger.error(f"Error sending reminder to user {user_id}: {e}")
            
            if users_to_remind:
                logger.info(f"Sent reminders to {len(users_to_remind)} users")
            else:
                logger.info("No users need reminders at this time")
                
        except Exception as e:
            logger.error(f"Error during reminder check: {e}")

    async def _handle_recurring_post(self, post):
        """Handle recurring post logic after successful posting"""
        post_id = post['id']
        
        # Increment the posted count
        Database.increment_recurring_post_count(post_id)
        
        # Check if we should schedule the next occurrence
        should_continue = True
        
        # Check count limit
        if post['recurring_count'] and post['recurring_posted_count'] + 1 >= post['recurring_count']:
            should_continue = False
        
        # Check end date
        if post['recurring_end_date'] and get_current_kyiv_time() >= post['recurring_end_date']:
            should_continue = False
        
        if should_continue and post['recurring_interval_hours']:
            # Schedule next occurrence
            next_time = get_current_kyiv_time() + timedelta(hours=post['recurring_interval_hours'])
            
            # Create new job for next occurrence
            job_id = f"post_{post_id}"
            self.scheduler.add_job(
                self._post_to_channel,
                'date',
                run_date=next_time,
                args=[post_id],
                id=job_id,
                replace_existing=True
            )
            
            logger.info(f"Scheduled next recurring post {post_id} for {next_time}")
        else:
            # Mark as completed
            Database.mark_post_as_posted(post_id)
            logger.info(f"Recurring post {post_id} completed")

    async def _post_album_to_channel(self, post_id: int, media_bundle_json: str, description: str, 
                                   target_channel: str, user_id: int):
        """Post an album (multiple media) to channel using sendMediaGroup"""
        open_files = []  # Track open file handles
        
        try:
            import json
            from telegram import InputMediaPhoto, InputMediaVideo
            
            # Parse media bundle
            media_bundle = json.loads(media_bundle_json)
            
            if not media_bundle or len(media_bundle) == 0:
                logger.error(f"Empty media bundle for album post {post_id}")
                Database.mark_post_as_failed(post_id, "Empty media bundle")
                return
            
            if len(media_bundle) > 10:
                logger.error(f"Album post {post_id} has {len(media_bundle)} items, exceeding Telegram's 10-item limit")
                Database.mark_post_as_failed(post_id, "Album too large (>10 items)")
                return
            
            # Prepare media group for Telegram
            media_group = []
            missing_files = []
            
            for i, media_item in enumerate(media_bundle):
                file_path = media_item['file_path']
                media_type = media_item['media_type']
                
                # Check if file exists
                if not os.path.exists(file_path):
                    missing_files.append(file_path)
                    continue
                
                # Open file and keep it open until after send_media_group
                f = open(file_path, 'rb')
                open_files.append(f)
                
                # Determine InputMedia type for Telegram
                if media_type in ['photo', 'document_image']:
                    media_obj = InputMediaPhoto(
                        media=f,
                        caption=description if i == 0 else None  # Caption only on first item
                    )
                elif media_type in ['video', 'document_video']:
                    media_obj = InputMediaVideo(
                        media=f,
                        caption=description if i == 0 else None  # Caption only on first item
                    )
                else:
                    logger.warning(f"Unsupported media type for album: {media_type}, skipping")
                    f.close()  # Close the file since we're not using it
                    open_files.pop()  # Remove from tracking
                    continue
                
                media_group.append(media_obj)
            
            # Check for missing files
            if missing_files:
                error_msg = f"Missing files: {', '.join(missing_files)}"
                logger.error(f"Album post {post_id}: {error_msg}")
                Database.mark_post_as_failed(post_id, error_msg)
                await self.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ Album post #{post_id} failed: Some media files not found."
                )
                return
            
            # Check if we have any valid media
            if not media_group:
                logger.error(f"No valid media found for album post {post_id}")
                Database.mark_post_as_failed(post_id, "No valid media files")
                await self.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ Album post #{post_id} failed: No valid media files found."
                )
                return
            
            # Send media group to channel
            logger.info(f"Post {post_id}: Sending album with {len(media_group)} items to {target_channel}")
            logger.info(f"Post {post_id}: Album caption='{description}' on first media")
            
            await self.bot.send_media_group(
                chat_id=target_channel,
                media=media_group
            )
            
            # Mark post as successfully posted
            Database.mark_post_as_posted(post_id)
            
            # Set cleanup date for media files (7 days from now)
            from datetime import datetime, timedelta
            cleanup_date = datetime.now() + timedelta(days=7)
            if hasattr(Database, 'set_post_cleanup_date'):
                Database.set_post_cleanup_date(post_id, cleanup_date)
            
            logger.info(f"Album post {post_id} sent successfully with {len(media_group)} items")
            
        except Exception as e:
            logger.error(f"Failed to post album {post_id}: {e}")
            Database.mark_post_as_failed(post_id, f"Album posting error: {str(e)}")
            await self.bot.send_message(
                chat_id=user_id,
                text=f"❌ Album post #{post_id} failed: {str(e)}"
            )
        finally:
            # Always close all opened files
            for f in open_files:
                try:
                    f.close()
                except Exception as e:
                    logger.warning(f"Error closing file {f.name}: {e}")

    async def cancel_post_job(self, post_id: int):
        """Cancel a scheduled job for a specific post"""
        job_id = f"post_{post_id}"
        try:
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
                logger.info(f"Cancelled job for post {post_id}")
            else:
                logger.debug(f"No job found to cancel for post {post_id}")
        except Exception as e:
            logger.error(f"Error cancelling job for post {post_id}: {e}")

    async def schedule_single_post(self, post_id: int, scheduled_time: datetime):
        """Schedule a single post for a specific time"""
        try:
            self._schedule_single_post(post_id, scheduled_time)
            logger.info(f"Scheduled single post {post_id} for {scheduled_time}")
        except Exception as e:
            logger.error(f"Error scheduling single post {post_id}: {e}")
            raise

    async def _handle_post_failure(self, post_id: int, user_id: int, failure_reason: str):
        """Handle post failure with retry logic"""
        retry_count = Database.increment_retry_count(post_id)
        max_retries = 3
        
        if retry_count < max_retries:
            # Calculate retry delay using exponential backoff
            delay_minutes = 2 ** retry_count  # 2, 4, 8 minutes
            retry_time = get_current_kyiv_time() + timedelta(minutes=delay_minutes)
            
            # Schedule retry
            self._schedule_single_post(post_id, retry_time)
            
            logger.info(f"Scheduled retry {retry_count}/{max_retries} for post {post_id} at {retry_time}")
            
            # Notify user about retry
            try:
                await self.bot.send_message(
                    chat_id=user_id,
                    text=f"⚠️ Post #{post_id} failed but will retry in {delay_minutes} minutes (attempt {retry_count}/{max_retries})"
                )
            except Exception:
                pass
        else:
            # Maximum retries exceeded, mark as permanently failed
            Database.mark_post_as_failed(post_id, failure_reason)
            
            # Notify user of permanent failure
            try:
                await self.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ Post #{post_id} permanently failed after {max_retries} attempts: {failure_reason}"
                )
            except Exception:
                pass

    async def schedule_retry_posts(self):
        """Schedule retries for eligible failed posts"""
        retry_posts = Database.get_posts_for_retry()
        
        for post in retry_posts:
            retry_count = Database.increment_retry_count(post['id'])
            max_retries = 3
            
            if retry_count <= max_retries:
                # Calculate retry delay
                delay_minutes = 2 ** (retry_count - 1)  # 1, 2, 4 minutes
                retry_time = get_current_kyiv_time() + timedelta(minutes=delay_minutes)
                
                # Schedule retry
                self._schedule_single_post(post['id'], retry_time)
                
                logger.info(f"Scheduled automatic retry for post {post['id']} at {retry_time}")
                
                # Notify user
                try:
                    await self.bot.send_message(
                        chat_id=post['user_id'],
                        text=f"🔄 Automatically retrying post #{post['id']} in {delay_minutes} minutes"
                    )
                except Exception:
                    pass
            else:
                # Mark as permanently failed
                Database.mark_post_as_failed(post['id'], post['failure_reason'])

    async def _diagnose_telegram_error(self, error: Exception, post_id: int) -> dict:
        """Diagnose Telegram errors and provide actionable solutions"""
        error_msg = str(error).lower()
        
        diagnosis = {
            'retry_possible': False,
            'wait_time': 5,
            'error_message': str(error),
            'solution': None,
            'user_action_required': False
        }
        
        # Rate limiting errors
        if 'too many requests' in error_msg or 'retry after' in error_msg:
            # Extract wait time from error message if available
            import re
            match = re.search(r'retry after (\d+)', error_msg)
            wait_time = int(match.group(1)) if match else 30
            
            diagnosis.update({
                'retry_possible': True,
                'wait_time': wait_time + 1,  # Add 1 second buffer
                'solution': 'Telegram API rate limit reached. Will automatically retry.',
                'error_type': 'rate_limit'
            })
            
        # Bot blocked by user/channel
        elif 'bot was blocked' in error_msg or 'forbidden' in error_msg:
            diagnosis.update({
                'retry_possible': False,
                'solution': 'The bot has been blocked or removed from the channel. Please re-add the bot as an admin.',
                'user_action_required': True,
                'error_type': 'bot_blocked'
            })
            
        # Chat not found
        elif 'chat not found' in error_msg or 'chat_id is invalid' in error_msg:
            diagnosis.update({
                'retry_possible': False,
                'solution': 'Channel not found. Please verify the channel ID and ensure the bot is added as an admin.',
                'user_action_required': True,
                'error_type': 'chat_not_found'
            })
            
        # File size errors
        elif 'file too large' in error_msg or 'file size' in error_msg:
            diagnosis.update({
                'retry_possible': False,
                'solution': 'File size exceeds Telegram limit (50MB for bots). Please use a smaller file.',
                'user_action_required': True,
                'error_type': 'file_too_large'
            })
            
        # Network errors
        elif 'network' in error_msg or 'timeout' in error_msg or 'connection' in error_msg:
            diagnosis.update({
                'retry_possible': True,
                'wait_time': 10,
                'solution': 'Network connectivity issue. Will automatically retry.',
                'error_type': 'network_error'
            })
            
        # Bad request errors
        elif 'bad request' in error_msg:
            if 'caption' in error_msg:
                diagnosis.update({
                    'retry_possible': False,
                    'solution': 'Caption is too long (max 1024 characters) or contains invalid formatting.',
                    'user_action_required': True,
                    'error_type': 'invalid_caption'
                })
            else:
                diagnosis.update({
                    'retry_possible': False,
                    'solution': 'Invalid request parameters. Please check the post content.',
                    'user_action_required': True,
                    'error_type': 'bad_request'
                })
        
        # Default case - retry with exponential backoff
        else:
            diagnosis.update({
                'retry_possible': True,
                'wait_time': 5,
                'solution': 'Unknown error occurred. Will attempt retry.',
                'error_type': 'unknown'
            })
        
        return diagnosis
    
    async def _notify_post_failure(self, post_id: int, user_id: int, diagnosis: dict):
        """Send detailed failure notification with actionable steps"""
        try:
            message = f"""
🚨 **Post #{post_id} Failed to Publish**

**Error Type:** {diagnosis.get('error_type', 'Unknown').replace('_', ' ').title()}
**Error:** {diagnosis['error_message'][:200]}

**📋 Diagnosis:**
{diagnosis.get('solution', 'An unexpected error occurred.')}

"""
            
            if diagnosis.get('user_action_required'):
                message += """
**⚠️ Action Required:**
Please resolve the issue above and try again.
You can use /retry to attempt posting again.
"""
            else:
                message += """
**ℹ️ Status:**
The system has attempted automatic recovery but was unsuccessful.
"""
            
            message += f"""
**🔧 Troubleshooting Steps:**
1. Check if the bot is still admin in your channel
2. Verify channel ID is correct
3. Ensure file size is under 50MB
4. Check caption length (max 1024 chars)

Use /help for more assistance.
"""
            
            await self.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Failed to notify user {user_id} about post {post_id} failure: {e}")
    
    async def _notify_file_error(self, post_id: int, user_id: int, file_path: str = None):
        """Notify user about file-related errors"""
        try:
            message = f"""
🚨 **Post #{post_id} Failed - File Error**

**Problem:** The media file for this post could not be found.

**Possible Causes:**
• File was deleted after scheduling
• File path changed
• Storage issue

**File Path:** {file_path if file_path else 'Unknown'}

**💡 Solution:**
Please re-upload the media and schedule it again.
Use /mode1 or /mode2 to upload new content.
"""
            
            await self.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Failed to notify user {user_id} about file error: {e}")
    
    async def _notify_unexpected_error(self, post_id: int, user_id: int, error: str):
        """Notify user about unexpected errors"""
        try:
            message = f"""
⚠️ **Post #{post_id} Encountered an Issue**

An unexpected error occurred while trying to publish your post.

**Error Details:** {error[:200]}

**🔄 What We Did:**
• Attempted automatic retry 3 times
• Checked network connectivity
• Verified post parameters

**📋 Next Steps:**
1. Check bot status with /start
2. Verify channel settings with /channels
3. Try posting manually with /retry
4. Contact support if issue persists

The post has been marked for manual review.
"""
            
            await self.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Failed to notify user {user_id} about unexpected error: {e}")
    
    async def reschedule_all_posts_from_today(self, user_id: int, start_hour: int, end_hour: int, interval_hours: int, channel_id: str = None) -> int:
        """Reschedule all pending posts starting from today with custom hours"""
        try:
            # Cancel existing scheduled jobs first
            pending_posts = Database.get_pending_posts(user_id, channel_id)
            for post in pending_posts:
                job_id = f"post_{post['id']}"
                if self.scheduler.get_job(job_id):
                    self.scheduler.remove_job(job_id)
            
            # Reschedule in database
            rescheduled_count = Database.reschedule_all_posts_from_today(user_id, start_hour, end_hour, interval_hours, channel_id)
            
            if rescheduled_count > 0:
                # Re-add to scheduler with new times
                updated_posts = Database.get_pending_posts(user_id, channel_id)
                for post in updated_posts:
                    if post['scheduled_time']:
                        self._schedule_single_post(post['id'], post['scheduled_time'])
                
                logger.info(f"Rescheduled {rescheduled_count} posts for user {user_id}")
            
            return rescheduled_count
            
        except Exception as e:
            logger.error(f"Error rescheduling posts for user {user_id}: {e}")
            return 0

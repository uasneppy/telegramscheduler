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
from .utils import get_kyiv_timezone, get_current_kyiv_time
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
    
    def stop(self):
        """Stop the scheduler"""
        self.scheduler.shutdown()
        logger.info("Post scheduler stopped")
    
    def _schedule_existing_posts(self):
        """Schedule all existing pending posts from database"""
        pending_posts = Database.get_pending_posts()
        
        for post in pending_posts:
            if post['scheduled_time']:
                self._schedule_single_post(post['id'], post['scheduled_time'])
    
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
        """Schedule a single post"""
        job_id = f"post_{post_id}"
        
        # Remove existing job if it exists
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
        
        # Add new job
        self.scheduler.add_job(
            self._post_to_channel,
            trigger=DateTrigger(run_date=scheduled_time),
            args=[post_id],
            id=job_id,
            replace_existing=True
        )
        
        logger.info(f"Scheduled post {post_id} for {scheduled_time}")
    
    async def _post_to_channel(self, post_id: int):
        """Post a single message to the channel with rate limiting"""
        try:
            # Add a small delay at the start to prevent overwhelming Telegram API
            await asyncio.sleep(1.0)  # 1 second delay before each post
            # Get post details from database
            conn = Database.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT file_path, media_type, description, user_id, channel_id
                FROM posts 
                WHERE id = ? AND status = 'pending'
            ''', (post_id,))
            
            row = cursor.fetchone()
            conn.close()
            
            if not row:
                logger.warning(f"Post {post_id} not found or already processed")
                return
            
            file_path, media_type, description, user_id, channel_id = row
            media_type = media_type or 'photo'  # Default to photo for backward compatibility
            
            # Check if channel_id is provided
            if not channel_id:
                logger.error(f"No channel specified for post {post_id}")
                Database.mark_post_as_failed(post_id)
                await self.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ Post #{post_id} failed: No channel specified. Please set up channels first."
                )
                return
                
            target_channel = channel_id
            
            # Check if file exists before trying to open it
            if not os.path.exists(file_path):
                logger.error(f"File not found for post {post_id}: {file_path}")
                Database.mark_post_as_failed(post_id)
                await self.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ Post #{post_id} failed: Media file not found."
                )
                return
            
            # Send media to channel based on type
            with open(file_path, 'rb') as media_file:
                if media_type == 'photo':
                    await self.bot.send_photo(
                        chat_id=target_channel,
                        photo=media_file,
                        caption=description
                    )
                elif media_type == 'video':
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
                elif media_type == 'document':
                    await self.bot.send_document(
                        chat_id=target_channel,
                        document=media_file,
                        caption=description
                    )
                else:
                    # Default to document for unknown types
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
                
        except TelegramError as e:
            logger.error(f"Telegram error posting {post_id}: {e}")
            Database.mark_post_as_failed(post_id)
            
            # Notify user of failure
            try:
                await self.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ Failed to post #{post_id}: {e}"
                )
            except Exception:
                pass
                
        except Exception as e:
            logger.error(f"Unexpected error posting {post_id}: {e}")
            Database.mark_post_as_failed(post_id)
    
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

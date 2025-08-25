"""
Database operations for storing posts and scheduling information
"""

import sqlite3
import json
import logging
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from config import DATABASE_PATH

logger = logging.getLogger(__name__)

def init_database():
    """Initialize the SQLite database with required tables"""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    # Create posts table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            description TEXT,
            scheduled_time TIMESTAMP,
            status TEXT DEFAULT 'pending',
            mode INTEGER NOT NULL,
            is_recurring BOOLEAN DEFAULT FALSE,
            recurring_interval_hours INTEGER DEFAULT NULL,
            recurring_end_date TIMESTAMP DEFAULT NULL,
            recurring_count INTEGER DEFAULT NULL,
            recurring_posted_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            posted_at TIMESTAMP NULL
        )
    ''')
    
    # Add channel_id column if it doesn't exist (migration)
    try:
        cursor.execute('ALTER TABLE posts ADD COLUMN channel_id TEXT')
        logger.info("Added channel_id column to posts table")
    except sqlite3.OperationalError:
        # Column already exists, this is fine
        pass
    
    # Add recurring posts columns if they don't exist (migration)
    recurring_columns = [
        ('is_recurring', 'BOOLEAN DEFAULT FALSE'),
        ('recurring_interval_hours', 'INTEGER DEFAULT NULL'),
        ('recurring_end_date', 'TIMESTAMP DEFAULT NULL'),
        ('recurring_count', 'INTEGER DEFAULT NULL'),
        ('recurring_posted_count', 'INTEGER DEFAULT 0')
    ]
    
    for column_name, column_def in recurring_columns:
        try:
            cursor.execute(f'ALTER TABLE posts ADD COLUMN {column_name} {column_def}')
            logger.info(f"Added {column_name} column to posts table")
        except sqlite3.OperationalError:
            # Column already exists, this is fine
            pass
    
    # Add media_type column if it doesn't exist (migration)
    try:
        cursor.execute('ALTER TABLE posts ADD COLUMN media_type TEXT DEFAULT "photo"')
        logger.info("Added media_type column to posts table")
    except sqlite3.OperationalError:
        # Column already exists, this is fine
        pass
    
    # Create user_sessions table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_sessions (
            user_id INTEGER PRIMARY KEY,
            current_mode TEXT DEFAULT 'idle',
            session_data TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create scheduling_config table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scheduling_config (
            user_id INTEGER PRIMARY KEY,
            start_hour INTEGER DEFAULT 10,
            end_hour INTEGER DEFAULT 20,
            interval_hours INTEGER DEFAULT 2,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create user_channels table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            channel_id TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            is_default BOOLEAN DEFAULT FALSE,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, channel_id)
        )
    ''')
    
    # Create post_batches table for multi-channel batch management
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS post_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            batch_name TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, batch_name)
        )
    ''')
    
    # Add batch_id column to posts table if it doesn't exist (migration)
    try:
        cursor.execute('ALTER TABLE posts ADD COLUMN batch_id INTEGER REFERENCES post_batches(id)')
        logger.info("Added batch_id column to posts table")
    except sqlite3.OperationalError:
        # Column already exists, this is fine
        pass
    
    # Create post_backups table for backup functionality
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS post_backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            backup_name TEXT NOT NULL,
            backup_data TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, backup_name)
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")

class Database:
    @staticmethod
    def get_connection():
        """Get database connection"""
        return sqlite3.connect(DATABASE_PATH)
    
    @staticmethod
    def add_post(user_id: int, file_path: str, media_type: str = 'photo', description: Optional[str] = None, 
                 scheduled_time: Optional[datetime] = None, mode: int = 1, channel_id: Optional[str] = None,
                 is_recurring: bool = False, recurring_interval_hours: Optional[int] = None,
                 recurring_end_date: Optional[datetime] = None, recurring_count: Optional[int] = None) -> int:
        """Add a new post to the database"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO posts (user_id, file_path, media_type, description, scheduled_time, mode, channel_id,
                             is_recurring, recurring_interval_hours, recurring_end_date, recurring_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, file_path, media_type, description, scheduled_time, mode, channel_id,
              is_recurring, recurring_interval_hours, recurring_end_date, recurring_count))
        
        post_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        logger.info(f"Added post {post_id} for user {user_id} (recurring: {is_recurring})")
        return post_id
    
    @staticmethod
    def get_pending_posts(user_id: Optional[int] = None, channel_id: Optional[str] = None, unscheduled_only: bool = False) -> List[Dict]:
        """Get all pending posts, optionally filtered by user, channel, or unscheduled status"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        # Build query conditions
        conditions = ["status = 'pending'"]
        params = []
        
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        
        if unscheduled_only:
            conditions.append("scheduled_time IS NULL")
        
        where_clause = " AND ".join(conditions)
        
        cursor.execute(f'''
            SELECT id, user_id, file_path, media_type, description, scheduled_time, mode, channel_id,
                   is_recurring, recurring_interval_hours, recurring_end_date, recurring_count, recurring_posted_count
            FROM posts 
            WHERE {where_clause}
            ORDER BY scheduled_time ASC
        ''', params)
        
        posts = []
        for row in cursor.fetchall():
            posts.append({
                'id': row[0],
                'user_id': row[1],
                'file_path': row[2],
                'media_type': row[3] or 'photo',
                'description': row[4],
                'scheduled_time': datetime.fromisoformat(row[5]) if row[5] else None,
                'mode': row[6],
                'channel_id': row[7],
                'is_recurring': bool(row[8]) if row[8] is not None else False,
                'recurring_interval_hours': row[9],
                'recurring_end_date': datetime.fromisoformat(row[10]) if row[10] else None,
                'recurring_count': row[11],
                'recurring_posted_count': row[12] or 0
            })
        
        conn.close()
        return posts
    
    @staticmethod
    def mark_post_as_posted(post_id: int):
        """Mark a post as successfully posted"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE posts 
            SET status = 'posted', posted_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (post_id,))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Marked post {post_id} as posted")
    
    @staticmethod
    def mark_post_as_failed(post_id: int):
        """Mark a post as failed"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE posts 
            SET status = 'failed'
            WHERE id = ?
        ''', (post_id,))
        
        conn.commit()
        conn.close()
        
        logger.warning(f"Marked post {post_id} as failed")
    
    @staticmethod
    def get_failed_posts(user_id: int, channel_id: Optional[str] = None) -> List[Dict]:
        """Get all failed posts for a user, optionally filtered by channel"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        conditions = ["user_id = ?", "status = 'failed'"]
        params = [user_id]
        
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        
        where_clause = " AND ".join(conditions)
        
        cursor.execute(f'''
            SELECT id, file_path, media_type, description, scheduled_time, mode, 
                   channel_id, created_at, posted_at
            FROM posts 
            WHERE {where_clause}
            ORDER BY created_at DESC
        ''', params)
        
        posts = []
        for row in cursor.fetchall():
            post_id, file_path, media_type, description, scheduled_time, mode, channel_id, created_at, posted_at = row
            posts.append({
                'id': post_id,
                'file_path': file_path,
                'media_type': media_type or 'photo',
                'description': description,
                'scheduled_time': scheduled_time,
                'mode': mode,
                'channel_id': channel_id,
                'created_at': created_at,
                'posted_at': posted_at
            })
        
        conn.close()
        return posts
    
    @staticmethod
    def get_overdue_posts(user_id: int, channel_id: Optional[str] = None) -> List[Dict]:
        """Get all overdue posts for a user (scheduled time is in the past but status is still pending)"""
        from .utils import get_kyiv_timezone
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        # Get current time in Kyiv timezone
        kyiv_tz = get_kyiv_timezone()
        current_time = datetime.now(kyiv_tz).replace(tzinfo=None)
        
        conditions = [
            "user_id = ?", 
            "status = 'pending'", 
            "scheduled_time IS NOT NULL",
            "scheduled_time < ?"
        ]
        params = [user_id, current_time.isoformat()]
        
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        
        where_clause = " AND ".join(conditions)
        
        cursor.execute(f'''
            SELECT id, file_path, media_type, description, scheduled_time, mode, 
                   channel_id, created_at, is_recurring
            FROM posts 
            WHERE {where_clause}
            ORDER BY scheduled_time ASC
        ''', params)
        
        posts = []
        for row in cursor.fetchall():
            post_id, file_path, media_type, description, scheduled_time, mode, channel_id, created_at, is_recurring = row
            posts.append({
                'id': post_id,
                'file_path': file_path,
                'media_type': media_type or 'photo',
                'description': description,
                'scheduled_time': datetime.fromisoformat(scheduled_time) if scheduled_time else None,
                'mode': mode,
                'channel_id': channel_id,
                'created_at': created_at,
                'is_recurring': bool(is_recurring) if is_recurring is not None else False
            })
        
        conn.close()
        return posts
    
    @staticmethod
    def reschedule_overdue_posts_to_next_slots(user_id: int, overdue_post_ids: List[int], channel_id: Optional[str] = None) -> int:
        """Reschedule overdue posts to the next available time slots, moving the queue forward"""
        from .utils import get_kyiv_timezone
        
        if not overdue_post_ids:
            return 0
            
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        try:
            # Get user's scheduling configuration
            start_hour, end_hour, interval_hours = Database.get_scheduling_config(user_id)
            schedule_config = {
                'start_hour': start_hour,
                'end_hour': end_hour,
                'interval_hours': interval_hours
            }
            
            # Get all future scheduled posts for the user (and channel if specified)
            conditions = ["user_id = ?", "status = 'pending'", "scheduled_time IS NOT NULL"]
            params = [user_id]
            
            kyiv_tz = get_kyiv_timezone()
            current_time = datetime.now(kyiv_tz).replace(tzinfo=None)
            conditions.append("scheduled_time >= ?")
            params.append(current_time.isoformat())
            
            if channel_id:
                conditions.append("channel_id = ?")
                params.append(channel_id)
            
            where_clause = " AND ".join(conditions)
            
            cursor.execute(f'''
                SELECT id, scheduled_time FROM posts 
                WHERE {where_clause}
                ORDER BY scheduled_time ASC
            ''', params)
            
            future_posts = cursor.fetchall()
            
            # Calculate new schedule times starting from the next available slot
            kyiv_tz = get_kyiv_timezone()
            now = datetime.now(kyiv_tz).replace(tzinfo=None)
            
            # Find next valid scheduling time
            next_time = now.replace(minute=0, second=0, microsecond=0)
            
            # Ensure we're within the daily schedule
            if next_time.hour < schedule_config['start_hour']:
                next_time = next_time.replace(hour=schedule_config['start_hour'])
            elif next_time.hour >= schedule_config['end_hour']:
                next_time = next_time.replace(hour=schedule_config['start_hour']) + timedelta(days=1)
            else:
                # Round up to the next interval
                hours_since_start = next_time.hour - schedule_config['start_hour']
                intervals_passed = hours_since_start // schedule_config['interval_hours']
                next_time = next_time.replace(hour=schedule_config['start_hour'] + (intervals_passed + 1) * schedule_config['interval_hours'])
            
            # If there are existing scheduled posts, start after the last one
            if future_posts:
                last_scheduled = datetime.fromisoformat(future_posts[-1][1])
                # Add the interval to get the next slot after existing posts
                next_time = last_scheduled + timedelta(hours=schedule_config['interval_hours'])
                
                # Ensure it's within daily schedule bounds
                while next_time.hour < schedule_config['start_hour'] or next_time.hour >= schedule_config['end_hour']:
                    next_time = next_time.replace(hour=schedule_config['start_hour'])
                    if next_time <= last_scheduled:
                        next_time += timedelta(days=1)
            
            # Reschedule overdue posts to new time slots
            updated_count = 0
            new_schedules = []
            
            for post_id in overdue_post_ids:
                # Update the post's scheduled time
                cursor.execute('''
                    UPDATE posts SET scheduled_time = ? 
                    WHERE id = ? AND user_id = ? AND status = 'pending'
                ''', (next_time.isoformat(), post_id, user_id))
                
                if cursor.rowcount > 0:
                    new_schedules.append((post_id, next_time))
                    updated_count += 1
                    
                    # Calculate next time slot
                    next_time += timedelta(hours=schedule_config['interval_hours'])
                    
                    # Handle day boundaries
                    while next_time.hour < schedule_config['start_hour'] or next_time.hour >= schedule_config['end_hour']:
                        next_time = next_time.replace(hour=schedule_config['start_hour'])
                        if next_time.date() == (next_time - timedelta(hours=schedule_config['interval_hours'])).date():
                            next_time += timedelta(days=1)
            
            # Now shift all existing future posts forward
            shift_hours = len(overdue_post_ids) * schedule_config['interval_hours']
            
            for post_id, old_time_str in future_posts:
                old_time = datetime.fromisoformat(old_time_str)
                new_time = old_time + timedelta(hours=shift_hours)
                
                # Ensure the new time is within daily bounds, adjusting to next valid day if needed
                while new_time.hour < schedule_config['start_hour'] or new_time.hour >= schedule_config['end_hour']:
                    days_to_add = 1
                    new_time = new_time.replace(hour=schedule_config['start_hour']) + timedelta(days=days_to_add)
                
                cursor.execute('''
                    UPDATE posts SET scheduled_time = ? 
                    WHERE id = ? AND user_id = ?
                ''', (new_time.isoformat(), post_id, user_id))
            
            conn.commit()
            logger.info(f"Rescheduled {updated_count} overdue posts and shifted {len(future_posts)} future posts for user {user_id}")
            
            return updated_count
            
        except Exception as e:
            logger.error(f"Error rescheduling overdue posts: {e}")
            conn.rollback()
            return 0
        finally:
            conn.close()
    
    @staticmethod
    def get_post_by_id(post_id: int) -> Dict:
        """Get a single post by its ID"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, user_id, file_path, media_type, description, scheduled_time, 
                   status, mode, channel_id, is_recurring, created_at, posted_at
            FROM posts 
            WHERE id = ?
        ''', (post_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return None
            
        post_id, user_id, file_path, media_type, description, scheduled_time, status, mode, channel_id, is_recurring, created_at, posted_at = row
        return {
            'id': post_id,
            'user_id': user_id,
            'file_path': file_path,
            'media_type': media_type or 'photo',
            'description': description,
            'scheduled_time': datetime.fromisoformat(scheduled_time) if scheduled_time else None,
            'status': status,
            'mode': mode,
            'channel_id': channel_id,
            'is_recurring': bool(is_recurring) if is_recurring is not None else False,
            'created_at': created_at,
            'posted_at': posted_at
        }
    
    @staticmethod
    def retry_failed_post(post_id: int) -> bool:
        """Reset a failed post back to pending status for retry"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        # Check if the post exists and is actually failed
        cursor.execute('SELECT status FROM posts WHERE id = ?', (post_id,))
        result = cursor.fetchone()
        
        if not result:
            conn.close()
            return False
            
        if result[0] != 'failed':
            conn.close()
            return False
            
        # Reset the post to pending status
        cursor.execute('''
            UPDATE posts 
            SET status = 'pending', posted_at = NULL
            WHERE id = ?
        ''', (post_id,))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Reset failed post {post_id} back to pending for retry")
        return True
    
    @staticmethod
    def update_user_session(user_id: int, mode: str, session_data: Optional[Dict] = None):
        """Update user session state"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        session_json = json.dumps(session_data) if session_data else None
        
        cursor.execute('''
            INSERT OR REPLACE INTO user_sessions (user_id, current_mode, session_data, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ''', (user_id, mode, session_json))
        
        conn.commit()
        conn.close()
    
    @staticmethod
    def get_user_session(user_id: int) -> Tuple[str, Dict]:
        """Get user session state"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT current_mode, session_data
            FROM user_sessions
            WHERE user_id = ?
        ''', (user_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            mode = row[0]
            session_data = json.loads(row[1]) if row[1] else {}
            return mode, session_data
        
        return 'idle', {}
    
    @staticmethod
    def clear_user_posts(user_id: int, mode: int = None, channel_id: str = None, scheduled_only: bool = False):
        """Clear pending posts for a user, optionally filtered by mode and channel
        
        Args:
            user_id: User ID
            mode: Optional mode filter
            channel_id: Optional channel filter  
            scheduled_only: If True, only clear posts with scheduled_time IS NULL (unscheduled posts only)
        """
        from .utils import delete_media_file
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        # Build query conditions - only clear unscheduled posts to preserve scheduled ones
        conditions = ["user_id = ?", "status = 'pending'", "scheduled_time IS NULL"]
        params = [user_id]
        
        if mode:
            conditions.append("mode = ?")
            params.append(mode)
        
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        
        where_clause = " AND ".join(conditions)
        
        # First get the file paths for cleanup
        cursor.execute(f'''
            SELECT id, file_path FROM posts 
            WHERE {where_clause}
        ''', params)
        
        posts_to_clear = cursor.fetchall()
        count = len(posts_to_clear)
        
        # Delete the physical files
        for post_id, file_path in posts_to_clear:
            delete_media_file(file_path)
        
        # Then delete the database records
        cursor.execute(f'''
            DELETE FROM posts 
            WHERE {where_clause}
        ''', params)
        
        conn.commit()
        conn.close()
        
        # Enhanced logging for better debugging
        filter_info = []
        if mode:
            filter_info.append(f"mode {mode}")
        if channel_id:
            filter_info.append(f"channel {channel_id}")
        filter_str = f" ({', '.join(filter_info)})" if filter_info else ""
        
        logger.info(f"Cleared {count} unscheduled pending posts for user {user_id}{filter_str} (scheduled posts preserved)")
    
    @staticmethod
    def get_unscheduled_posts(user_id: int) -> List[Dict]:
        """Get all unscheduled (queued) posts for a user"""
        return Database.get_pending_posts(user_id=user_id, unscheduled_only=True)
    
    @staticmethod
    def clear_queued_posts(user_id: int, channel_id: str = None) -> int:
        """Clear all queued (pending) posts for a user and return count of cleared posts"""
        from .utils import delete_media_file
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        # Build query conditions for unscheduled posts
        conditions = ["user_id = ?", "status = 'pending'", "scheduled_time IS NULL"]
        params = [user_id]
        
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        
        where_clause = " AND ".join(conditions)
        
        # First get the file paths and IDs of pending posts that haven't been scheduled yet
        cursor.execute(f'''
            SELECT id, file_path FROM posts 
            WHERE {where_clause}
        ''', params)
        
        unscheduled_posts = cursor.fetchall()
        count = len(unscheduled_posts)
        
        # Log details about what's being cleared
        channel_info = f" for channel {channel_id}" if channel_id else ""
        if count > 0:
            post_ids = [str(post[0]) for post in unscheduled_posts]
            logger.info(f"Clearing {count} queued (unscheduled) posts for user {user_id}{channel_info}: IDs {', '.join(post_ids)}")
        else:
            logger.info(f"No queued posts to clear for user {user_id}{channel_info}")
        
        # Delete the physical files
        for post_id, file_path in unscheduled_posts:
            delete_media_file(file_path)
        
        # Then delete the database records - only unscheduled pending posts
        cursor.execute(f'''
            DELETE FROM posts 
            WHERE {where_clause}
        ''', params)
        
        # Log how many scheduled posts remain
        cursor.execute('''
            SELECT COUNT(*) FROM posts 
            WHERE user_id = ? AND status = 'pending' AND scheduled_time IS NOT NULL
        ''', (user_id,))
        scheduled_remaining = cursor.fetchone()[0]
        
        conn.commit()
        conn.close()
        
        logger.info(f"Cleared {count} queued posts for user {user_id}{channel_info}. {scheduled_remaining} scheduled posts remain.")
        return count
    
    @staticmethod
    def clear_scheduled_posts(user_id: int, channel_id: Optional[str] = None) -> int:
        """Clear all scheduled posts for a user and return count of cleared posts"""
        from .utils import delete_media_file
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        # Build query conditions for scheduled posts
        conditions = ["user_id = ?", "status = 'pending'", "scheduled_time IS NOT NULL"]
        params = [user_id]
        
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        
        where_clause = " AND ".join(conditions)
        
        # First get the file paths and IDs of scheduled posts
        cursor.execute(f'''
            SELECT id, file_path FROM posts 
            WHERE {where_clause}
        ''', params)
        
        scheduled_posts = cursor.fetchall()
        count = len(scheduled_posts)
        
        # Log details about what's being cleared
        channel_info = f" for channel {channel_id}" if channel_id else ""
        if count > 0:
            post_ids = [str(post[0]) for post in scheduled_posts]
            logger.info(f"Clearing {count} scheduled posts for user {user_id}{channel_info}: IDs {', '.join(post_ids)}")
        else:
            logger.info(f"No scheduled posts to clear for user {user_id}{channel_info}")
        
        # Delete the physical files
        for post_id, file_path in scheduled_posts:
            delete_media_file(file_path)
        
        # Then delete the database records - only scheduled pending posts
        cursor.execute(f'''
            DELETE FROM posts 
            WHERE {where_clause}
        ''', params)
        
        # Log how many queued posts remain
        cursor.execute('''
            SELECT COUNT(*) FROM posts 
            WHERE user_id = ? AND status = 'pending' AND scheduled_time IS NULL
        ''', (user_id,))
        queued_remaining = cursor.fetchone()[0]
        
        conn.commit()
        conn.close()
        
        logger.info(f"Cleared {count} scheduled posts for user {user_id}{channel_info}. {queued_remaining} queued posts remain.")
        return count
    
    @staticmethod
    def update_scheduling_config(user_id: int, start_hour: int, end_hour: int, interval_hours: int):
        """Update scheduling configuration for a user"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO scheduling_config 
            (user_id, start_hour, end_hour, interval_hours, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (user_id, start_hour, end_hour, interval_hours))
        
        conn.commit()
        conn.close()
    
    @staticmethod
    def get_scheduling_config(user_id: int) -> Tuple[int, int, int]:
        """Get scheduling configuration for a user"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT start_hour, end_hour, interval_hours
            FROM scheduling_config
            WHERE user_id = ?
        ''', (user_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return row[0], row[1], row[2]
        
        # Return defaults
        from config import DEFAULT_START_HOUR, DEFAULT_END_HOUR, DEFAULT_INTERVAL_HOURS
        return DEFAULT_START_HOUR, DEFAULT_END_HOUR, DEFAULT_INTERVAL_HOURS
    
    @staticmethod
    def add_user_channel(user_id: int, channel_id: str, channel_name: str, is_default: bool = False) -> bool:
        """Add a new channel for a user"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT OR REPLACE INTO user_channels 
                (user_id, channel_id, channel_name, is_default, is_active)
                VALUES (?, ?, ?, FALSE, TRUE)
            ''', (user_id, channel_id, channel_name))
            
            conn.commit()
            conn.close()
            
            logger.info(f"Added channel {channel_id} for user {user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to add channel: {e}")
            conn.close()
            return False
    
    @staticmethod
    def get_user_channels(user_id: int) -> List[Dict]:
        """Get all channels for a user"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, channel_id, channel_name, is_default, is_active
            FROM user_channels
            WHERE user_id = ? AND is_active = TRUE
            ORDER BY channel_name ASC
        ''', (user_id,))
        
        channels = []
        for row in cursor.fetchall():
            channels.append({
                'id': row[0],
                'channel_id': row[1],
                'channel_name': row[2],
                'is_default': bool(row[3]),
                'is_active': bool(row[4])
            })
        
        conn.close()
        return channels
    

    

    
    @staticmethod
    def remove_user_channel(user_id: int, channel_id: str) -> bool:
        """Remove a channel for a user"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                UPDATE user_channels 
                SET is_active = FALSE 
                WHERE user_id = ? AND channel_id = ?
            ''', (user_id, channel_id))
            
            conn.commit()
            conn.close()
            
            logger.info(f"Removed channel {channel_id} for user {user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to remove channel: {e}")
            conn.close()
            return False

    @staticmethod
    def clear_all_user_data(user_id: int):
        """Clear all data for a specific user (posts, sessions, channels, config)"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        # Clear all user posts
        cursor.execute('DELETE FROM posts WHERE user_id = ?', (user_id,))
        
        # Clear user session
        cursor.execute('DELETE FROM user_sessions WHERE user_id = ?', (user_id,))
        
        # Clear user scheduling config
        cursor.execute('DELETE FROM scheduling_config WHERE user_id = ?', (user_id,))
        
        # Clear user channels
        cursor.execute('DELETE FROM user_channels WHERE user_id = ?', (user_id,))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Cleared all data for user {user_id}")

    @staticmethod
    def get_user_stats(user_id: int) -> dict:
        """Get comprehensive user statistics"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        # Get detailed stats by channel
        cursor.execute('''
            SELECT 
                COALESCE(uc.channel_name, 'Unknown Channel') as channel_name,
                COALESCE(p.channel_id, 'No Channel') as channel_id,
                CASE 
                    WHEN p.status = 'pending' AND p.scheduled_time IS NULL THEN 'queued'
                    WHEN p.status = 'pending' AND p.scheduled_time IS NOT NULL THEN 'scheduled'
                    ELSE p.status
                END as effective_status,
                p.mode,
                COUNT(*) as count
            FROM posts p
            LEFT JOIN user_channels uc ON p.channel_id = uc.channel_id AND p.user_id = uc.user_id
            WHERE p.user_id = ?
            GROUP BY p.channel_id, uc.channel_name, effective_status, p.mode
            ORDER BY uc.channel_name, p.mode, effective_status
        ''', (user_id,))
        
        channel_details = cursor.fetchall()
        
        # Get overall post counts
        cursor.execute('''
            SELECT 
                CASE 
                    WHEN status = 'pending' AND scheduled_time IS NULL THEN 'queued'
                    WHEN status = 'pending' AND scheduled_time IS NOT NULL THEN 'scheduled'
                    ELSE status
                END as effective_status,
                COUNT(*) 
            FROM posts 
            WHERE user_id = ? 
            GROUP BY effective_status
        ''', (user_id,))
        
        post_stats = dict(cursor.fetchall())
        
        # Get active channels
        cursor.execute('''
            SELECT channel_id, channel_name, is_default
            FROM user_channels 
            WHERE user_id = ? AND is_active = TRUE
            ORDER BY is_default DESC, channel_name ASC
        ''', (user_id,))
        
        channels_info = cursor.fetchall()
        
        # Get next scheduled posts
        cursor.execute('''
            SELECT p.scheduled_time, uc.channel_name, p.channel_id, p.media_type
            FROM posts p
            LEFT JOIN user_channels uc ON p.channel_id = uc.channel_id AND p.user_id = uc.user_id
            WHERE p.user_id = ? AND p.status = 'pending' AND p.scheduled_time IS NOT NULL
            ORDER BY p.scheduled_time ASC
            LIMIT 5
        ''', (user_id,))
        
        next_posts = cursor.fetchall()
        
        # Get session info
        cursor.execute('SELECT current_mode, session_data FROM user_sessions WHERE user_id = ?', (user_id,))
        session_row = cursor.fetchone()
        current_mode = session_row[0] if session_row else 'idle'
        session_data = session_row[1] if session_row else '{}'
        
        # Get recurring posts count
        cursor.execute('''
            SELECT COUNT(*) 
            FROM posts 
            WHERE user_id = ? AND is_recurring = TRUE AND status = 'pending'
        ''', (user_id,))
        recurring_count = cursor.fetchone()[0]
        
        # Get batches count
        cursor.execute('SELECT COUNT(*) FROM post_batches WHERE user_id = ?', (user_id,))
        batches_count = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            'posts': post_stats,
            'channel_details': channel_details,
            'channels_info': channels_info,
            'next_posts': next_posts,
            'current_mode': current_mode,
            'session_data': session_data,
            'recurring_count': recurring_count,
            'batches_count': batches_count,
            'total_posts': sum(post_stats.values()),
            'total_channels': len(channels_info)
        }

    @staticmethod
    def get_all_active_users() -> list:
        """Get list of all users who have any data in the system"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT DISTINCT user_id FROM (
                SELECT user_id FROM posts
                UNION
                SELECT user_id FROM user_sessions
                UNION
                SELECT user_id FROM user_channels
                UNION
                SELECT user_id FROM scheduling_config
            )
        ''')
        
        users = [row[0] for row in cursor.fetchall()]
        conn.close()
        
        return users

    @staticmethod
    def get_scheduled_posts_by_channel(user_id: int) -> Dict[str, List[Dict]]:
        """Get scheduled posts grouped by channel"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT p.id, p.file_path, p.media_type, p.description, p.scheduled_time, 
                   p.channel_id, p.is_recurring, p.recurring_interval_hours, 
                   p.recurring_count, p.recurring_end_date,
                   uc.channel_name
            FROM posts p
            LEFT JOIN user_channels uc ON p.channel_id = uc.channel_id AND p.user_id = uc.user_id
            WHERE p.user_id = ? AND p.status = 'pending'
            ORDER BY p.scheduled_time ASC
        ''', (user_id,))
        
        posts_by_channel = {}
        for row in cursor.fetchall():
            channel_id = row[5]
            channel_name = row[10] if row[10] else channel_id
            channel_key = f"{channel_name} ({channel_id})"
            
            if channel_key not in posts_by_channel:
                posts_by_channel[channel_key] = []
            
            posts_by_channel[channel_key].append({
                'id': row[0],
                'file_path': row[1],
                'media_type': row[2],
                'description': row[3],
                'scheduled_time': datetime.fromisoformat(row[4]) if row[4] else None,
                'channel_id': row[5],
                'is_recurring': bool(row[6]),
                'recurring_interval_hours': row[7],
                'recurring_count': row[8],
                'recurring_end_date': datetime.fromisoformat(row[9]) if row[9] else None
            })
        
        conn.close()
        return posts_by_channel

    @staticmethod
    def increment_recurring_post_count(post_id: int):
        """Increment the recurring post count for a post"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE posts 
            SET recurring_posted_count = recurring_posted_count + 1
            WHERE id = ?
        ''', (post_id,))
        
        conn.commit()
        conn.close()

    @staticmethod
    def get_recurring_posts() -> List[Dict]:
        """Get all active recurring posts"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, user_id, file_path, description, scheduled_time, mode, channel_id,
                   recurring_interval_hours, recurring_end_date, recurring_count, recurring_posted_count
            FROM posts 
            WHERE is_recurring = TRUE AND status = 'pending'
        ''')
        
        posts = []
        for row in cursor.fetchall():
            posts.append({
                'id': row[0],
                'user_id': row[1],
                'file_path': row[2],
                'description': row[3],
                'scheduled_time': datetime.fromisoformat(row[4]) if row[4] else None,
                'mode': row[5],
                'channel_id': row[6],
                'recurring_interval_hours': row[7],
                'recurring_end_date': datetime.fromisoformat(row[8]) if row[8] else None,
                'recurring_count': row[9],
                'recurring_posted_count': row[10] or 0
            })
        
        conn.close()
        return posts

    @staticmethod
    def create_batch(user_id: int, batch_name: str, channel_id: str) -> int:
        """Create a new post batch"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO post_batches (user_id, batch_name, channel_id)
            VALUES (?, ?, ?)
        ''', (user_id, batch_name, channel_id))
        
        batch_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        logger.info(f"Created batch {batch_id} '{batch_name}' for user {user_id}")
        return batch_id

    @staticmethod
    def get_user_batches(user_id: int) -> List[Dict]:
        """Get all batches for a user"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT b.id, b.batch_name, b.channel_id, b.status, b.created_at,
                   c.channel_name, COUNT(p.id) as post_count
            FROM post_batches b
            LEFT JOIN user_channels c ON b.channel_id = c.channel_id AND b.user_id = c.user_id
            LEFT JOIN posts p ON b.id = p.batch_id AND p.status = 'pending'
            WHERE b.user_id = ?
            GROUP BY b.id
            ORDER BY b.created_at DESC
        ''', (user_id,))
        
        batches = []
        for row in cursor.fetchall():
            batches.append({
                'id': row[0],
                'batch_name': row[1],
                'channel_id': row[2],
                'status': row[3],
                'created_at': row[4],
                'channel_name': row[5] or row[2],
                'post_count': row[6]
            })
        
        conn.close()
        return batches

    @staticmethod
    def get_batch_posts(batch_id: int) -> List[Dict]:
        """Get all posts in a specific batch"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, user_id, file_path, media_type, description, scheduled_time, mode, channel_id,
                   is_recurring, recurring_interval_hours, recurring_end_date, recurring_count, recurring_posted_count
            FROM posts 
            WHERE batch_id = ? AND status = 'pending'
            ORDER BY id ASC
        ''', (batch_id,))
        
        posts = []
        for row in cursor.fetchall():
            posts.append({
                'id': row[0],
                'user_id': row[1],
                'file_path': row[2],
                'media_type': row[3] or 'photo',
                'description': row[4],
                'scheduled_time': datetime.fromisoformat(row[5]) if row[5] else None,
                'mode': row[6],
                'channel_id': row[7],
                'is_recurring': bool(row[8]) if row[8] is not None else False,
                'recurring_interval_hours': row[9],
                'recurring_end_date': datetime.fromisoformat(row[10]) if row[10] else None,
                'recurring_count': row[11],
                'recurring_posted_count': row[12] or 0
            })
        
        conn.close()
        return posts

    @staticmethod
    def add_post_to_batch(user_id: int, file_path: str, batch_id: int, media_type: str = 'photo', 
                         description: Optional[str] = None, mode: int = 1) -> int:
        """Add a post to a specific batch"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        # Get batch info to set channel_id
        cursor.execute('SELECT channel_id FROM post_batches WHERE id = ?', (batch_id,))
        batch_info = cursor.fetchone()
        if not batch_info:
            conn.close()
            raise ValueError(f"Batch {batch_id} not found")
        
        channel_id = batch_info[0]
        
        cursor.execute('''
            INSERT INTO posts (user_id, file_path, media_type, description, mode, channel_id, batch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, file_path, media_type, description, mode, channel_id, batch_id))
        
        post_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        logger.info(f"Added post {post_id} to batch {batch_id} for user {user_id}")
        return post_id

    @staticmethod
    def schedule_batch(batch_id: int, scheduled_times: List[datetime]):
        """Schedule all posts in a batch"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        # Get all posts in the batch
        cursor.execute('''
            SELECT id FROM posts 
            WHERE batch_id = ? AND status = 'pending'
            ORDER BY id ASC
        ''', (batch_id,))
        
        post_ids = [row[0] for row in cursor.fetchall()]
        
        # Schedule each post
        for i, post_id in enumerate(post_ids):
            if i < len(scheduled_times):
                cursor.execute('''
                    UPDATE posts 
                    SET scheduled_time = ?
                    WHERE id = ?
                ''', (scheduled_times[i].isoformat(), post_id))
        
        # Mark batch as scheduled
        cursor.execute('''
            UPDATE post_batches 
            SET status = 'scheduled'
            WHERE id = ?
        ''', (batch_id,))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Scheduled batch {batch_id} with {len(scheduled_times)} times")

    @staticmethod
    def update_post_schedule(post_id: int, scheduled_time: datetime):
        """Update the scheduled time for a specific post"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                UPDATE posts 
                SET scheduled_time = ?
                WHERE id = ?
            ''', (scheduled_time.isoformat(), post_id))
            
            conn.commit()
            rows_affected = cursor.rowcount
            conn.close()
            
            if rows_affected > 0:
                logger.info(f"Updated scheduled time for post {post_id} to {scheduled_time}")
                return True
            else:
                logger.warning(f"No rows affected when updating post {post_id}")
                return False
                
        except Exception as e:
            logger.error(f"Error updating post schedule for post {post_id}: {e}")
            conn.close()
            return False

    @staticmethod
    def update_post_description(post_id: int, description: str):
        """Update the description for a specific post"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                UPDATE posts 
                SET description = ?
                WHERE id = ?
            ''', (description, post_id))
            
            conn.commit()
            rows_affected = cursor.rowcount
            conn.close()
            
            if rows_affected > 0:
                logger.info(f"Updated description for post {post_id}")
                return True
            else:
                logger.warning(f"No rows affected when updating description for post {post_id}")
                return False
                
        except Exception as e:
            logger.error(f"Error updating post description for post {post_id}: {e}")
            conn.close()
            return False

    @staticmethod
    def get_channel_posts(user_id: int, channel_id: str) -> List[Dict]:
        """Get all posts for a specific channel with their details"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, file_path, media_type, description, scheduled_time, status, 
                   mode, is_recurring, created_at, posted_at
            FROM posts 
            WHERE user_id = ? AND channel_id = ?
            ORDER BY 
                CASE 
                    WHEN scheduled_time IS NOT NULL THEN scheduled_time 
                    ELSE created_at 
                END ASC
        ''', (user_id, channel_id))
        
        rows = cursor.fetchall()
        conn.close()
        
        posts = []
        for row in rows:
            post_id, file_path, media_type, description, scheduled_time, status, mode, is_recurring, created_at, posted_at = row
            posts.append({
                'id': post_id,
                'file_path': file_path,
                'media_type': media_type or 'photo',
                'description': description,
                'scheduled_time': scheduled_time,
                'status': status,
                'mode': mode,
                'is_recurring': bool(is_recurring),
                'created_at': created_at,
                'posted_at': posted_at
            })
        
        return posts

    @staticmethod
    def delete_batch(batch_id: int) -> bool:
        """Delete a batch and all its posts"""
        from .utils import delete_media_file
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        # Get file paths of posts in the batch
        cursor.execute('''
            SELECT file_path FROM posts 
            WHERE batch_id = ? AND status = 'pending'
        ''', (batch_id,))
        
        file_paths = [row[0] for row in cursor.fetchall()]
        
        # Delete the physical files
        for file_path in file_paths:
            delete_media_file(file_path)
        
        # Delete posts in the batch
        cursor.execute('DELETE FROM posts WHERE batch_id = ?', (batch_id,))
        
        # Delete the batch itself
        cursor.execute('DELETE FROM post_batches WHERE id = ?', (batch_id,))
        
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        
        if success:
            logger.info(f"Deleted batch {batch_id}")
        return success

    @staticmethod
    def get_pending_posts_by_batch(user_id: int) -> Dict[str, List[Dict]]:
        """Get pending posts grouped by batch"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT p.id, p.file_path, p.media_type, p.description, p.mode,
                   b.batch_name, b.channel_id, c.channel_name
            FROM posts p
            LEFT JOIN post_batches b ON p.batch_id = b.id
            LEFT JOIN user_channels c ON b.channel_id = c.channel_id AND b.user_id = c.user_id
            WHERE p.user_id = ? AND p.status = 'pending'
            ORDER BY b.batch_name, p.id
        ''', (user_id,))
        
        posts_by_batch = {}
        for row in cursor.fetchall():
            batch_name = row[5] or "Unassigned"
            channel_name = row[7] or row[6] or "Unknown"
            batch_key = f"{batch_name} → {channel_name}"
            
            if batch_key not in posts_by_batch:
                posts_by_batch[batch_key] = []
            
            posts_by_batch[batch_key].append({
                'id': row[0],
                'file_path': row[1],
                'media_type': row[2] or 'photo',
                'description': row[3],
                'mode': row[4],
                'batch_name': batch_name,
                'channel_id': row[6],
                'channel_name': channel_name
            })
        
        conn.close()
        return posts_by_batch

    @staticmethod
    def get_posts_by_date_range(user_id: int, start_date: datetime, end_date: datetime) -> Dict[str, List[Dict]]:
        """Get scheduled posts grouped by date for calendar view"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT p.id, p.scheduled_time, p.media_type, p.description, p.channel_id, p.is_recurring,
                   uc.channel_name, p.mode
            FROM posts p
            LEFT JOIN user_channels uc ON p.channel_id = uc.channel_id AND p.user_id = uc.user_id
            WHERE p.user_id = ? AND p.status = 'pending' AND p.scheduled_time IS NOT NULL
            AND DATE(p.scheduled_time) BETWEEN DATE(?) AND DATE(?)
            ORDER BY p.scheduled_time ASC
        ''', (user_id, start_date.isoformat(), end_date.isoformat()))
        
        posts_by_date = {}
        for row in cursor.fetchall():
            scheduled_time = datetime.fromisoformat(row[1])
            date_key = scheduled_time.strftime('%Y-%m-%d')
            
            if date_key not in posts_by_date:
                posts_by_date[date_key] = []
            
            posts_by_date[date_key].append({
                'id': row[0],
                'scheduled_time': scheduled_time,
                'media_type': row[2] or 'photo',
                'description': row[3],
                'channel_id': row[4],
                'channel_name': row[6] or row[4],
                'is_recurring': bool(row[5]),
                'mode': row[7]
            })
        
        conn.close()
        return posts_by_date

    @staticmethod
    def get_scheduled_posts_for_channel(user_id: int, channel_id: str = None) -> List[Dict]:
        """Get all scheduled posts for a user, optionally filtered by channel"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        conditions = ["user_id = ?", "status = 'pending'", "scheduled_time IS NOT NULL"]
        params = [user_id]
        
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        
        where_clause = " AND ".join(conditions)
        
        cursor.execute(f'''
            SELECT id, file_path, media_type, description, scheduled_time, 
                   channel_id, mode, is_recurring
            FROM posts 
            WHERE {where_clause}
            ORDER BY scheduled_time ASC
        ''', params)
        
        posts = []
        for row in cursor.fetchall():
            posts.append({
                'id': row[0],
                'file_path': row[1],
                'media_type': row[2] or 'photo',
                'description': row[3],
                'scheduled_time': datetime.fromisoformat(row[4]) if row[4] else None,
                'channel_id': row[5],
                'mode': row[6],
                'is_recurring': bool(row[7]) if row[7] is not None else False
            })
        
        conn.close()
        return posts

    @staticmethod
    def bulk_update_post_schedules(post_schedules: List[tuple]) -> int:
        """Bulk update scheduled times for multiple posts
        
        Args:
            post_schedules: List of tuples (post_id, scheduled_time)
        
        Returns:
            Number of posts updated
        """
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        updated_count = 0
        
        try:
            for post_id, scheduled_time in post_schedules:
                cursor.execute('''
                    UPDATE posts 
                    SET scheduled_time = ?
                    WHERE id = ? AND status = 'pending'
                ''', (scheduled_time.isoformat(), post_id))
                
                if cursor.rowcount > 0:
                    updated_count += 1
            
            conn.commit()
            logger.info(f"Bulk updated schedules for {updated_count} posts")
            
        except Exception as e:
            logger.error(f"Error in bulk update: {e}")
            conn.rollback()
            updated_count = 0
        finally:
            conn.close()
        
        return updated_count

    @staticmethod
    def create_backup(user_id: int, backup_name: str) -> bool:
        """Create a backup of all scheduled posts for a user
        
        Args:
            user_id: The user ID
            backup_name: Name for the backup
            
        Returns:
            True if successful, False otherwise
        """
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        try:
            # Get all scheduled posts for the user
            cursor.execute('''
                SELECT id, file_path, media_type, description, scheduled_time, mode, channel_id,
                       is_recurring, recurring_interval_hours, recurring_end_date, 
                       recurring_count, recurring_posted_count, batch_id
                FROM posts 
                WHERE user_id = ? AND status = 'pending'
                ORDER BY scheduled_time ASC
            ''', (user_id,))
            
            posts = []
            for row in cursor.fetchall():
                posts.append({
                    'id': row[0],
                    'file_path': row[1],
                    'media_type': row[2],
                    'description': row[3],
                    'scheduled_time': row[4],
                    'mode': row[5],
                    'channel_id': row[6],
                    'is_recurring': bool(row[7]) if row[7] is not None else False,
                    'recurring_interval_hours': row[8],
                    'recurring_end_date': row[9],
                    'recurring_count': row[10],
                    'recurring_posted_count': row[11],
                    'batch_id': row[12]
                })
            
            # Store backup data as JSON
            backup_data = json.dumps(posts, default=str)
            
            # Insert or replace backup
            cursor.execute('''
                INSERT OR REPLACE INTO post_backups (user_id, backup_name, backup_data)
                VALUES (?, ?, ?)
            ''', (user_id, backup_name, backup_data))
            
            conn.commit()
            logger.info(f"Created backup '{backup_name}' for user {user_id} with {len(posts)} posts")
            return True
            
        except Exception as e:
            logger.error(f"Error creating backup: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    @staticmethod
    def restore_backup(user_id: int, backup_name: str, replace_existing: bool = False, restore_missing_files: bool = False) -> tuple:
        """Restore posts from a backup
        
        Args:
            user_id: The user ID
            backup_name: Name of the backup to restore
            replace_existing: Whether to clear existing scheduled posts first
            restore_missing_files: Whether to restore posts even if files are missing
            
        Returns:
            Tuple of (success: bool, restored_count: int, message: str)
        """
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        try:
            # Get backup data
            cursor.execute('''
                SELECT backup_data FROM post_backups 
                WHERE user_id = ? AND backup_name = ?
            ''', (user_id, backup_name))
            
            result = cursor.fetchone()
            if not result:
                return False, 0, f"Backup '{backup_name}' not found"
            
            backup_data = json.loads(result[0])
            
            if replace_existing:
                # Clear existing scheduled posts
                cursor.execute('''
                    DELETE FROM posts 
                    WHERE user_id = ? AND status = 'pending'
                ''', (user_id,))
                logger.info(f"Cleared existing scheduled posts for user {user_id}")
            
            # Restore posts from backup
            restored_count = 0
            skipped_count = 0
            missing_files_count = 0
            
            for post_data in backup_data:
                try:
                    # Check if file still exists
                    file_path = post_data['file_path']
                    file_exists = os.path.exists(file_path)
                    
                    # Try to find file with just filename if full path doesn't exist
                    if not file_exists and '/' in file_path:
                        filename = os.path.basename(file_path)
                        new_path = f"uploads/{filename}"
                        if os.path.exists(new_path):
                            file_path = new_path
                            file_exists = True
                            logger.info(f"Found file at new path: {new_path}")
                    
                    if not file_exists and not restore_missing_files:
                        skipped_count += 1
                        logger.warning(f"Skipping post - file not found: {post_data['file_path']}")
                        continue
                    
                    # Determine status based on file existence
                    status = 'pending' if file_exists else 'failed'
                    if not file_exists:
                        missing_files_count += 1
                        logger.warning(f"Restoring post with missing file as failed: {post_data['file_path']}")
                    
                    cursor.execute('''
                        INSERT INTO posts (
                            user_id, file_path, media_type, description, scheduled_time, 
                            mode, channel_id, is_recurring, recurring_interval_hours, 
                            recurring_end_date, recurring_count, recurring_posted_count, 
                            batch_id, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        user_id,
                        file_path,  # Use the potentially corrected path
                        post_data.get('media_type', 'photo'),
                        post_data['description'],
                        post_data['scheduled_time'],
                        post_data['mode'],
                        post_data['channel_id'],
                        post_data['is_recurring'],
                        post_data.get('recurring_interval_hours'),
                        post_data.get('recurring_end_date'),
                        post_data.get('recurring_count'),
                        post_data.get('recurring_posted_count', 0),
                        post_data.get('batch_id'),
                        status
                    ))
                    restored_count += 1
                    
                except Exception as post_error:
                    logger.error(f"Error restoring individual post: {post_error}")
                    skipped_count += 1
            
            conn.commit()
            
            message = f"Restored {restored_count} posts"
            if missing_files_count > 0:
                message += f" ({missing_files_count} with missing files marked as failed)"
            if skipped_count > 0:
                message += f" ({skipped_count} skipped - files missing)"
            
            logger.info(f"Restored backup '{backup_name}' for user {user_id}: {message}")
            return True, restored_count, message
            
        except Exception as e:
            logger.error(f"Error restoring backup: {e}")
            conn.rollback()
            return False, 0, f"Error restoring backup: {str(e)}"
        finally:
            conn.close()

    @staticmethod
    def get_user_backups(user_id: int) -> List[Dict]:
        """Get list of backups for a user
        
        Args:
            user_id: The user ID
            
        Returns:
            List of backup info dictionaries
        """
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT backup_name, created_at, backup_data
            FROM post_backups 
            WHERE user_id = ?
            ORDER BY created_at DESC
        ''', (user_id,))
        
        backups = []
        for row in cursor.fetchall():
            backup_data = json.loads(row[2])
            backups.append({
                'name': row[0],
                'created_at': row[1],
                'post_count': len(backup_data)
            })
        
        conn.close()
        return backups

    @staticmethod
    def delete_backup(user_id: int, backup_name: str) -> bool:
        """Delete a backup
        
        Args:
            user_id: The user ID
            backup_name: Name of the backup to delete
            
        Returns:
            True if successful, False otherwise
        """
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                DELETE FROM post_backups 
                WHERE user_id = ? AND backup_name = ?
            ''', (user_id, backup_name))
            
            success = cursor.rowcount > 0
            conn.commit()
            
            if success:
                logger.info(f"Deleted backup '{backup_name}' for user {user_id}")
            
            return success
            
        except Exception as e:
            logger.error(f"Error deleting backup: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()



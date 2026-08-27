"""
Database operations for storing posts and scheduling information
"""

import sqlite3
import json
import logging
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

from config import DATABASE_PATH, UPLOADS_DIR
from .utils import get_kyiv_timezone

logger = logging.getLogger(__name__)


# --- Canonical datetime handling -------------------------------------------
# All timestamps are stored as Kyiv-timezone-aware ISO strings
# ("2025-07-12T10:00:00+03:00"). Legacy rows in other formats (naive
# space-separated strings from the old sqlite adapter / CURRENT_TIMESTAMP)
# are rewritten once by the migration in init_database.

def serialize_dt(dt) -> Optional[str]:
    """Serialize a datetime (or parseable string) to canonical Kyiv ISO."""
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = parse_dt(dt)
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = get_kyiv_timezone().localize(dt)
    return dt.astimezone(get_kyiv_timezone()).isoformat()


def parse_dt(value, naive_tz: str = 'kyiv') -> Optional[datetime]:
    """Parse a stored timestamp into a Kyiv-aware datetime.

    naive_tz picks the assumed zone for legacy naive values:
    'kyiv' for user-facing schedule fields, 'utc' for values written by
    SQLite's CURRENT_TIMESTAMP (posted_at / created_at).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip())
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        if naive_tz == 'utc':
            import pytz
            dt = pytz.utc.localize(dt)
        else:
            dt = get_kyiv_timezone().localize(dt)
    return dt.astimezone(get_kyiv_timezone())


def kyiv_now_iso() -> str:
    """Current time as canonical Kyiv ISO string."""
    return datetime.now(get_kyiv_timezone()).isoformat()

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
            posted_at TIMESTAMP NULL,
            cleanup_date TIMESTAMP NULL
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
    
    # Add cleanup_date column if it doesn't exist (migration)
    try:
        cursor.execute('ALTER TABLE posts ADD COLUMN cleanup_date TIMESTAMP NULL')
        logger.info("Added cleanup_date column to posts table")
    except sqlite3.OperationalError:
        # Column already exists, this is fine
        pass
    
    # Add retry tracking columns if they don't exist (migration)
    retry_columns = [
        ('retry_count', 'INTEGER DEFAULT 0'),
        ('last_retry_at', 'TEXT'),
        ('failure_reason', 'TEXT')
    ]
    
    for column_name, column_def in retry_columns:
        try:
            cursor.execute(f'ALTER TABLE posts ADD COLUMN {column_name} {column_def}')
            logger.info(f"Added {column_name} column to posts table")
        except sqlite3.OperationalError:
            # Column already exists, this is fine
            pass
    
    # Add media_bundle_json column if it doesn't exist (migration for album support)
    try:
        cursor.execute('ALTER TABLE posts ADD COLUMN media_bundle_json TEXT')
        logger.info("Added media_bundle_json column to posts table")
    except sqlite3.OperationalError:
        # Column already exists, this is fine
        pass
    
    # Add caption_entities column if it doesn't exist (migration for native formatting support)
    try:
        cursor.execute('ALTER TABLE posts ADD COLUMN caption_entities TEXT')
        logger.info("Added caption_entities column to posts table")
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
            reminder_enabled BOOLEAN DEFAULT TRUE,
            reminder_threshold INTEGER DEFAULT 5,
            last_reminder_sent TEXT DEFAULT NULL,
            notify_mode TEXT DEFAULT 'all',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Add reminder/notification columns if they don't exist (migration)
    reminder_columns = [
        ('reminder_enabled', 'BOOLEAN DEFAULT TRUE'),
        ('reminder_threshold', 'INTEGER DEFAULT 5'),
        ('last_reminder_sent', 'TEXT DEFAULT NULL'),
        ("notify_mode", "TEXT DEFAULT 'all'")
    ]

    for column_name, column_def in reminder_columns:
        try:
            cursor.execute(f'ALTER TABLE scheduling_config ADD COLUMN {column_name} {column_def}')
            logger.info(f"Added {column_name} column to scheduling_config table")
        except sqlite3.OperationalError:
            # Column already exists, this is fine
            pass
    
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

    # Indexes for the hot query patterns (scheduler polling, per-user views)
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_posts_status_sched ON posts(status, scheduled_time)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_posts_user_status ON posts(user_id, status)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_posts_batch ON posts(batch_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_posts_channel ON posts(channel_id)')

    # Tiny key/value table to track one-time data migrations
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    _migrate_datetime_formats(cursor)

    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")


def _migrate_datetime_formats(cursor):
    """One-time rewrite of legacy timestamp formats to canonical Kyiv ISO.

    Historical rows hold a mix of: naive space-separated strings from the old
    sqlite datetime adapter (Kyiv wall time), naive UTC from CURRENT_TIMESTAMP
    (posted_at/created_at), and aware ISO strings. Mixed formats break the
    lexicographic comparisons used in scheduled_time queries.
    """
    cursor.execute("SELECT value FROM schema_meta WHERE key = 'dt_normalized_v1'")
    if cursor.fetchone():
        return

    # (table, column, assumed zone for naive legacy values)
    targets = [
        ('posts', 'scheduled_time', 'kyiv'),
        ('posts', 'recurring_end_date', 'kyiv'),
        ('posts', 'cleanup_date', 'kyiv'),
        ('posts', 'last_retry_at', 'kyiv'),
        ('posts', 'posted_at', 'utc'),
        ('posts', 'created_at', 'utc'),
        ('scheduling_config', 'last_reminder_sent', 'kyiv'),
    ]
    rewritten = 0
    for table, column, naive_tz in targets:
        key = 'user_id' if table == 'scheduling_config' else 'id'
        cursor.execute(f"SELECT {key}, {column} FROM {table} WHERE {column} IS NOT NULL")
        for row_id, raw in cursor.fetchall():
            parsed = parse_dt(raw, naive_tz=naive_tz)
            if parsed is None:
                logger.warning(f"dt migration: could not parse {table}.{column}={raw!r} (row {row_id})")
                continue
            canonical = parsed.isoformat()
            if canonical != raw:
                cursor.execute(f"UPDATE {table} SET {column} = ? WHERE {key} = ?", (canonical, row_id))
                rewritten += 1

    cursor.execute("INSERT INTO schema_meta (key, value) VALUES ('dt_normalized_v1', 'done')")
    logger.info(f"Normalized {rewritten} legacy timestamp values to Kyiv ISO format")

class Database:
    @staticmethod
    def get_connection():
        """Get database connection (WAL, busy timeout, enforced foreign keys)"""
        conn = sqlite3.connect(DATABASE_PATH, timeout=15.0)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=15000')
        conn.execute('PRAGMA foreign_keys=ON')
        return conn
    
    @staticmethod
    def add_post(user_id: int, file_path: str, media_type: str = 'photo', description: Optional[str] = None, 
                 scheduled_time: Optional[datetime] = None, mode: int = 1, channel_id: Optional[str] = None,
                 is_recurring: bool = False, recurring_interval_hours: Optional[int] = None,
                 recurring_end_date: Optional[datetime] = None, recurring_count: Optional[int] = None,
                 media_bundle_json: Optional[str] = None, caption_entities: Optional[str] = None) -> int:
        """Add a new post to the database"""
        
        # SECURITY CHECK: Verify user owns the channel before creating the post
        if channel_id and not Database.user_has_channel(user_id, channel_id):
            error_msg = f"Security violation: User {user_id} attempted to create post for channel {channel_id} they don't own"
            logger.error(f"SECURITY ALERT: {error_msg}")
            raise ValueError("Channel access denied - you don't have permission to post to this channel")
        
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO posts (user_id, file_path, media_type, description, scheduled_time, mode, channel_id,
                             is_recurring, recurring_interval_hours, recurring_end_date, recurring_count, 
                             media_bundle_json, caption_entities)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, file_path, media_type, description, serialize_dt(scheduled_time), mode, channel_id,
              is_recurring, recurring_interval_hours, serialize_dt(recurring_end_date), recurring_count,
              media_bundle_json, caption_entities))
        
        post_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        logger.info(f"Added post {post_id} for user {user_id} (recurring: {is_recurring})")
        return post_id
    
    @staticmethod
    def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
        """Parse datetime values stored as ISO strings into timezone-aware datetimes."""
        if not value:
            return None

        if isinstance(value, datetime):
            # Ensure timezone-awareness
            if value.tzinfo is None:
                return get_kyiv_timezone().localize(value)
            return value

        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = get_kyiv_timezone().localize(parsed)
            return parsed
        except (ValueError, TypeError):
            logger.warning(f"Unable to parse datetime value: {value}")
            return None

    @staticmethod
    def get_post_by_id(post_id: int) -> Optional[Dict]:
        """Get a complete post by ID"""
        conn = Database.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, user_id, file_path, media_type, description, scheduled_time, mode,
                   channel_id, is_recurring, recurring_interval_hours, recurring_end_date,
                   recurring_count, media_bundle_json, status, created_at, posted_at,
                   batch_id, retry_count, last_retry_at, failure_reason, cleanup_date, caption_entities
            FROM posts
            WHERE id = ?
        ''', (post_id,))

        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        return {
            'id': row[0],
            'user_id': row[1],
            'file_path': row[2],
            'media_type': row[3],
            'description': row[4],
            'scheduled_time': Database._parse_datetime(row[5]),
            'mode': row[6],
            'channel_id': row[7],
            'is_recurring': bool(row[8]) if row[8] is not None else False,
            'recurring_interval_hours': row[9],
            'recurring_end_date': Database._parse_datetime(row[10]),
            'recurring_count': row[11],
            'media_bundle_json': row[12],
            'status': row[13],
            'created_at': Database._parse_datetime(row[14]),
            'posted_at': Database._parse_datetime(row[15]),
            'batch_id': row[16],
            'retry_count': row[17],
            'last_retry_at': Database._parse_datetime(row[18]),
            'failure_reason': row[19],
            'cleanup_date': Database._parse_datetime(row[20]),
            'caption_entities': row[21]
        }

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
                   is_recurring, recurring_interval_hours, recurring_end_date, recurring_count, recurring_posted_count, media_bundle_json
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
                'recurring_posted_count': row[12] or 0,
                'media_bundle_json': row[13]
            })
        
        conn.close()
        return posts
    
    @staticmethod
    def claim_post_for_sending(post_id: int) -> bool:
        """Atomically move a post from 'pending' to 'sending'.

        Returns False if the post was already claimed/processed - this is what
        prevents the monitor job and a slow in-flight upload from publishing
        the same post twice.
        """
        conn = Database.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE posts
            SET status = 'sending', last_retry_at = ?
            WHERE id = ? AND status = 'pending'
        ''', (kyiv_now_iso(), post_id))

        conn.commit()
        claimed = cursor.rowcount > 0
        conn.close()
        return claimed

    @staticmethod
    def reset_stale_sending_posts(max_age_minutes: int = 30) -> int:
        """Return posts stuck in 'sending' (e.g. after a crash mid-upload) to 'pending'."""
        conn = Database.get_connection()
        cursor = conn.cursor()

        cutoff = (datetime.now(get_kyiv_timezone()) - timedelta(minutes=max_age_minutes)).isoformat()
        # last_retry_at doubles as the claim timestamp; fall back to scheduled_time
        cursor.execute('''
            UPDATE posts
            SET status = 'pending'
            WHERE status = 'sending'
              AND COALESCE(last_retry_at, scheduled_time) < ?
        ''', (cutoff,))

        conn.commit()
        count = cursor.rowcount
        conn.close()

        if count:
            logger.warning(f"Reset {count} stale 'sending' posts back to pending")
        return count

    @staticmethod
    def reschedule_recurring_post(post_id: int, next_time: datetime):
        """Set the next occurrence for a recurring post and return it to 'pending'."""
        conn = Database.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE posts
            SET scheduled_time = ?, status = 'pending'
            WHERE id = ?
        ''', (serialize_dt(next_time), post_id))

        conn.commit()
        conn.close()
        logger.info(f"Rescheduled recurring post {post_id} for {next_time}")

    @staticmethod
    def purge_deleted_posts() -> int:
        """Hard-delete soft-deleted posts and their media files."""
        from .utils import delete_media_file
        conn = Database.get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT id, file_path, media_bundle_json FROM posts WHERE status = 'deleted'")
        rows = cursor.fetchall()
        if not rows:
            conn.close()
            return 0

        file_paths = []
        for _post_id, file_path, bundle_json in rows:
            if file_path:
                file_paths.append(file_path)
            if bundle_json:
                try:
                    for item in json.loads(bundle_json):
                        if item.get('file_path'):
                            file_paths.append(item['file_path'])
                except (ValueError, TypeError):
                    pass

        cursor.execute("DELETE FROM posts WHERE status = 'deleted'")
        conn.commit()
        conn.close()

        # Delete files only after the rows are gone so a failure never leaves
        # rows pointing at missing files
        for path in file_paths:
            delete_media_file(path)

        logger.info(f"Purged {len(rows)} soft-deleted posts and {len(file_paths)} media files")
        return len(rows)

    @staticmethod
    def mark_post_as_posted(post_id: int):
        """Mark a post as successfully posted"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE posts
            SET status = 'posted', posted_at = ?
            WHERE id = ?
        ''', (kyiv_now_iso(), post_id))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Marked post {post_id} as posted")
    
    @staticmethod
    def mark_post_as_failed(post_id: int, failure_reason: Optional[str] = None):
        """Mark a post as failed"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE posts 
            SET status = 'failed', failure_reason = ?
            WHERE id = ?
        ''', (failure_reason, post_id))
        
        conn.commit()
        conn.close()
        
        logger.warning(f"Marked post {post_id} as failed: {failure_reason}")
    
    @staticmethod
    def set_post_cleanup_date(post_id: int, cleanup_date: datetime):
        """Set the cleanup date for a posted file"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE posts
            SET cleanup_date = ?
            WHERE id = ?
        ''', (serialize_dt(cleanup_date), post_id))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Set cleanup date for post {post_id} to {cleanup_date}")
    
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
    def increment_retry_count(post_id: int) -> int:
        """Increment retry count for a post and return new count"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE posts
            SET retry_count = retry_count + 1,
                last_retry_at = ?,
                status = 'pending'
            WHERE id = ?
        ''', (kyiv_now_iso(), post_id))
        
        # Get the new retry count
        cursor.execute('SELECT retry_count FROM posts WHERE id = ?', (post_id,))
        result = cursor.fetchone()
        retry_count = result[0] if result else 0
        
        conn.commit()
        conn.close()
        
        logger.info(f"Incremented retry count for post {post_id} to {retry_count}")
        return retry_count
    
    @staticmethod
    def reschedule_all_posts_from_today(user_id: int, start_hour: int, end_hour: int, interval_hours: int, channel_id: Optional[str] = None) -> int:
        """Reschedule all pending posts starting from today with custom hours, interleaving channels"""
        from .utils import get_kyiv_timezone, calculate_schedule_times
        from collections import defaultdict
        
        # Validate input parameters
        if not isinstance(start_hour, int) or not (0 <= start_hour <= 23):
            logger.error(f"Invalid start_hour: {start_hour}. Must be 0-23.")
            return 0
        
        if not isinstance(end_hour, int) or not (0 <= end_hour <= 23):
            logger.error(f"Invalid end_hour: {end_hour}. Must be 0-23.")
            return 0
        
        if start_hour >= end_hour:
            logger.error(f"Invalid schedule: start_hour ({start_hour}) must be less than end_hour ({end_hour})")
            return 0
        
        if not isinstance(interval_hours, int) or interval_hours < 1:
            logger.error(f"Invalid interval_hours: {interval_hours}. Must be >= 1.")
            return 0
        
        time_window = end_hour - start_hour
        if interval_hours > time_window:
            logger.error(f"Invalid interval_hours: {interval_hours}. Cannot exceed time window ({time_window} hours)")
            return 0
        
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        try:
            # Get all pending posts for the user with their channel info
            conditions = ["user_id = ?", "status = 'pending'"]
            params = [user_id]
            
            if channel_id:
                conditions.append("channel_id = ?")
                params.append(channel_id)
            
            where_clause = " AND ".join(conditions)
            
            cursor.execute(f'''
                SELECT id, channel_id FROM posts 
                WHERE {where_clause}
                ORDER BY created_at ASC
            ''', params)
            
            posts_data = cursor.fetchall()
            
            if not posts_data:
                conn.close()
                return 0
            
            # Group posts by channel for simultaneous scheduling
            posts_by_channel = defaultdict(list)
            for post_id, channel_id_val in posts_data:
                posts_by_channel[channel_id_val].append(post_id)
            
            # Calculate how many time slots we need (based on max posts per channel)
            max_posts_per_channel = max(len(posts) for posts in posts_by_channel.values())
            
            # Calculate schedule times for the number of time slots we need.
            # Work in naive Kyiv wall-clock time (timedelta on pytz-aware
            # datetimes keeps a stale offset across DST transitions); localize
            # at write time below.
            kyiv_tz = get_kyiv_timezone()
            current_time = datetime.now(kyiv_tz).replace(tzinfo=None)

            # First, try to use remaining slots today
            next_slot = current_time.replace(minute=0, second=0, microsecond=0)
            
            # Find next available slot (round up to next interval)
            max_iterations = 48  # Prevent infinite loop
            iterations = 0
            while iterations < max_iterations:
                iterations += 1
                
                # Check if current slot is valid
                if (next_slot > current_time and 
                    start_hour <= next_slot.hour < end_hour and
                    (next_slot.hour - start_hour) % interval_hours == 0):
                    break
                    
                next_slot += timedelta(hours=1)
                
                # If we've gone past end_hour, move to tomorrow's start_hour
                if next_slot.hour >= end_hour:
                    next_slot = (next_slot + timedelta(days=1)).replace(hour=start_hour, minute=0, second=0, microsecond=0)
                    break
            
            # Start scheduling from the next available slot (today if possible, tomorrow if not)
            today = next_slot
            
            # Use a custom schedule generation that respects our starting point
            time_slot_times = []
            current_slot = today
            
            for i in range(max_posts_per_channel):
                time_slot_times.append(current_slot)
                
                # Calculate next slot
                current_slot += timedelta(hours=interval_hours)
                
                # If we go past or reach end_hour, move to next day's start_hour
                if current_slot.hour >= end_hour or current_slot.hour < start_hour:
                    # Move to the next day at start_hour
                    current_slot = current_slot.replace(hour=start_hour, minute=0, second=0, microsecond=0)
                    # If we haven't actually moved forward a day yet, add a day
                    if current_slot <= time_slot_times[-1]:
                        current_slot += timedelta(days=1)
            
            # Localize each wall-clock slot with its correct DST offset
            time_slot_times = [kyiv_tz.localize(t) for t in time_slot_times]

            # Schedule all channels simultaneously at each time slot
            total_posts_scheduled = 0
            for time_slot_index in range(max_posts_per_channel):
                scheduled_time = time_slot_times[time_slot_index]
                
                # At each time slot, schedule one post from each channel (if available)
                for channel, posts in posts_by_channel.items():
                    if time_slot_index < len(posts):
                        post_id = posts[time_slot_index]
                        cursor.execute('''
                            UPDATE posts
                            SET scheduled_time = ?, retry_count = 0, failure_reason = NULL
                            WHERE id = ? AND user_id = ?
                        ''', (serialize_dt(scheduled_time), post_id, user_id))
                        total_posts_scheduled += 1
            
            logger.info(f"Scheduled {total_posts_scheduled} posts across {len(posts_by_channel)} channels for simultaneous posting - all channels post at same time slots")
            
            conn.commit()
            conn.close()
            
            logger.info(f"Rescheduled {total_posts_scheduled} posts for user {user_id} starting from {today} with simultaneous channel scheduling")
            return total_posts_scheduled
            
        except Exception as e:
            logger.error(f"Error rescheduling posts: {e}")
            conn.rollback()
            conn.close()
            return 0
    
    @staticmethod
    def get_overdue_posts(user_id: int, channel_id: Optional[str] = None) -> List[Dict]:
        """Get all overdue posts for a user (scheduled time is in the past but status is still pending)"""
        from .utils import get_kyiv_timezone
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        # Get current time in Kyiv timezone (timezone-aware)
        kyiv_tz = get_kyiv_timezone()
        current_time = datetime.now(kyiv_tz)
        
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
            SELECT id, user_id, file_path, media_type, description, scheduled_time, mode, 
                   channel_id, created_at, is_recurring
            FROM posts 
            WHERE {where_clause}
            ORDER BY scheduled_time ASC
        ''', params)
        
        posts = []
        for row in cursor.fetchall():
            post_id, user_id, file_path, media_type, description, scheduled_time, mode, channel_id, created_at, is_recurring = row
            posts.append({
                'id': post_id,
                'user_id': user_id,
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
        
        # Validate input
        if not overdue_post_ids:
            logger.info("No overdue posts to reschedule")
            return 0
        
        if not isinstance(overdue_post_ids, list):
            logger.error(f"Invalid overdue_post_ids type: {type(overdue_post_ids)}. Must be a list.")
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
            current_time = datetime.now(kyiv_tz)
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
            now = datetime.now(kyiv_tz)
            
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
                next_hour = schedule_config['start_hour'] + (intervals_passed + 1) * schedule_config['interval_hours']
                
                # If next_hour exceeds end_hour, move to next day
                if next_hour >= schedule_config['end_hour']:
                    next_time = next_time.replace(hour=schedule_config['start_hour']) + timedelta(days=1)
                else:
                    next_time = next_time.replace(hour=next_hour)
            
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
            
            # Now shift all existing future posts forward by calculating proper next slots
            for post_id, old_time_str in future_posts:
                old_time = datetime.fromisoformat(old_time_str)
                
                # Calculate how many interval slots to shift
                new_time = old_time
                for _ in range(len(overdue_post_ids)):
                    new_time += timedelta(hours=schedule_config['interval_hours'])
                    
                    # Handle day boundaries properly
                    if new_time.hour >= schedule_config['end_hour'] or new_time.hour < schedule_config['start_hour']:
                        # Move to next valid day at start_hour
                        days_forward = 1
                        new_time = new_time.replace(hour=schedule_config['start_hour'], minute=0, second=0, microsecond=0)
                        if new_time.date() == old_time.date():
                            new_time += timedelta(days=days_forward)
                
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
    def retry_failed_post(post_id: int, user_id: int) -> bool:
        """Reset a failed post back to pending status for retry.

        Requires user_id because post_id can come from forgeable callback data.
        Single conditional UPDATE avoids the check-then-write race.
        """
        conn = Database.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE posts
            SET status = 'pending', posted_at = NULL
            WHERE id = ? AND user_id = ? AND status = 'failed'
        ''', (post_id, user_id))

        conn.commit()
        success = cursor.rowcount > 0
        conn.close()

        if success:
            logger.info(f"Reset failed post {post_id} back to pending for retry")
        return success
    
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
    def _clear_pending_posts(user_id: int, scheduled: bool, mode: Optional[int] = None,
                             channel_id: Optional[str] = None) -> int:
        """Delete a user's pending posts (scheduled or unscheduled) and their media.

        Rows are deleted and committed BEFORE files are removed, so a failure
        can never leave rows pointing at missing files. Album bundle files are
        removed as well.
        """
        from .utils import delete_media_file

        conn = Database.get_connection()
        cursor = conn.cursor()

        conditions = ["user_id = ?", "status = 'pending'",
                      "scheduled_time IS NOT NULL" if scheduled else "scheduled_time IS NULL"]
        params = [user_id]

        if mode:
            conditions.append("mode = ?")
            params.append(mode)
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)

        where_clause = " AND ".join(conditions)

        cursor.execute(f'SELECT id, file_path, media_bundle_json FROM posts WHERE {where_clause}', params)
        rows = cursor.fetchall()
        count = len(rows)

        cursor.execute(f'DELETE FROM posts WHERE {where_clause}', params)
        conn.commit()
        conn.close()

        # Now that the rows are gone, remove the files (including album bundles)
        for _post_id, file_path, bundle_json in rows:
            if file_path:
                delete_media_file(file_path)
            if bundle_json:
                try:
                    for item in json.loads(bundle_json):
                        if item.get('file_path'):
                            delete_media_file(item['file_path'])
                except (ValueError, TypeError):
                    pass

        kind = "scheduled" if scheduled else "unscheduled"
        filter_info = [f"mode {mode}"] if mode else []
        if channel_id:
            filter_info.append(f"channel {channel_id}")
        filter_str = f" ({', '.join(filter_info)})" if filter_info else ""
        logger.info(f"Cleared {count} {kind} pending posts for user {user_id}{filter_str}")
        return count

    @staticmethod
    def clear_user_posts(user_id: int, mode: Optional[int] = None, channel_id: Optional[str] = None):
        """Clear a user's unscheduled pending posts (scheduled posts are preserved)."""
        return Database._clear_pending_posts(user_id, scheduled=False, mode=mode, channel_id=channel_id)
    
    @staticmethod
    def get_unscheduled_posts(user_id: int) -> List[Dict]:
        """Get all unscheduled (queued) posts for a user"""
        return Database.get_pending_posts(user_id=user_id, unscheduled_only=True)
    
    @staticmethod
    def clear_queued_posts(user_id: int, channel_id: Optional[str] = None) -> int:
        """Clear all queued (unscheduled pending) posts for a user; returns count."""
        return Database._clear_pending_posts(user_id, scheduled=False, channel_id=channel_id)

    @staticmethod
    def clear_scheduled_posts(user_id: int, channel_id: Optional[str] = None) -> int:
        """Clear all scheduled pending posts for a user; returns count."""
        return Database._clear_pending_posts(user_id, scheduled=True, channel_id=channel_id)

    @staticmethod
    def delete_scheduled_post(user_id: int, post_id: int) -> bool:
        """Delete a single scheduled post for a user"""
        from .utils import delete_media_file

        conn = Database.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT file_path, media_bundle_json
            FROM posts
            WHERE id = ? AND user_id = ? AND status = 'pending' AND scheduled_time IS NOT NULL
        ''', (post_id, user_id))

        row = cursor.fetchone()

        if not row:
            conn.close()
            logger.warning(f"Attempted to delete nonexistent scheduled post {post_id} for user {user_id}")
            return False

        file_path, media_bundle_json = row

        file_paths = set()
        if file_path:
            file_paths.add(file_path)

        if media_bundle_json:
            try:
                bundle = json.loads(media_bundle_json)
                for item in bundle:
                    bundle_path = item.get('file_path') if isinstance(item, dict) else None
                    if bundle_path:
                        file_paths.add(bundle_path)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to decode media bundle for post {post_id}: {e}")

        for path in file_paths:
            delete_media_file(path)

        cursor.execute('DELETE FROM posts WHERE id = ? AND user_id = ?', (post_id, user_id))
        conn.commit()
        conn.close()

        logger.info(f"Deleted scheduled post {post_id} for user {user_id}")
        return True

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
    def get_reminder_settings(user_id: int) -> Tuple[bool, int, Optional[datetime]]:
        """Get reminder settings for a user"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT reminder_enabled, reminder_threshold, last_reminder_sent 
            FROM scheduling_config WHERE user_id = ?
        ''', (user_id,))
        
        settings = cursor.fetchone()
        conn.close()
        
        if settings:
            enabled, threshold, last_sent = settings
            last_sent_dt = parse_dt(last_sent) if last_sent else None
            return enabled if enabled is not None else True, threshold if threshold is not None else 5, last_sent_dt
        else:
            # Return default values
            return True, 5, None
    
    @staticmethod
    def update_reminder_settings(user_id: int, enabled: Optional[bool] = None, threshold: Optional[int] = None):
        """Update reminder settings for a user"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        # Get current settings
        cursor.execute('SELECT user_id FROM scheduling_config WHERE user_id = ?', (user_id,))
        exists = cursor.fetchone()
        
        if exists:
            # Update existing settings
            updates = []
            params = []
            
            if enabled is not None:
                updates.append("reminder_enabled = ?")
                params.append(enabled)
            if threshold is not None:
                updates.append("reminder_threshold = ?")
                params.append(threshold)
            
            if updates:
                params.append(user_id)
                query = f"UPDATE scheduling_config SET {', '.join(updates)} WHERE user_id = ?"
                cursor.execute(query, params)
        else:
            # Create new settings with defaults
            cursor.execute('''
                INSERT INTO scheduling_config (user_id, reminder_enabled, reminder_threshold)
                VALUES (?, ?, ?)
            ''', (user_id, enabled if enabled is not None else True, 
                  threshold if threshold is not None else 5))
        
        conn.commit()
        conn.close()
    
    @staticmethod
    def get_notify_mode(user_id: int) -> str:
        """Get the user's notification mode: 'all', 'digest', or 'errors'."""
        conn = Database.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT notify_mode FROM scheduling_config WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        conn.close()
        mode = row[0] if row and row[0] else 'all'
        return mode if mode in ('all', 'digest', 'errors') else 'all'

    @staticmethod
    def set_notify_mode(user_id: int, mode: str):
        """Set the user's notification mode ('all' | 'digest' | 'errors')."""
        if mode not in ('all', 'digest', 'errors'):
            raise ValueError(f"Invalid notify mode: {mode}")
        conn = Database.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO scheduling_config (user_id, notify_mode) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET notify_mode = excluded.notify_mode
        ''', (user_id, mode))
        conn.commit()
        conn.close()

    @staticmethod
    def get_digest_data(hours: int = 24) -> Dict[int, Dict]:
        """Per-user posted/failed counts for the daily digest.

        Returns {user_id: {'posted': n, 'failed': n}} covering users on
        'digest' notification mode with any activity in the window.
        """
        conn = Database.get_connection()
        cursor = conn.cursor()

        since = (datetime.now(get_kyiv_timezone()) - timedelta(hours=hours)).isoformat()
        cursor.execute('''
            SELECT p.user_id,
                   SUM(CASE WHEN p.status = 'posted' AND p.posted_at >= ? THEN 1 ELSE 0 END),
                   SUM(CASE WHEN p.status = 'failed' THEN 1 ELSE 0 END)
            FROM posts p
            JOIN scheduling_config sc ON sc.user_id = p.user_id
            WHERE sc.notify_mode = 'digest'
            GROUP BY p.user_id
        ''', (since,))

        result = {}
        for user_id, posted, failed in cursor.fetchall():
            if posted or failed:
                result[user_id] = {'posted': posted or 0, 'failed': failed or 0}
        conn.close()
        return result

    @staticmethod
    def get_dashboard_stats(user_id: int) -> Dict:
        """Compact stats for the main-menu dashboard, in one query pass."""
        conn = Database.get_connection()
        cursor = conn.cursor()

        now_iso = datetime.now(get_kyiv_timezone()).isoformat()
        cursor.execute('''
            SELECT
                (SELECT COUNT(*) FROM user_channels WHERE user_id = ? AND is_active = TRUE),
                (SELECT COUNT(*) FROM posts WHERE user_id = ? AND status = 'pending' AND scheduled_time IS NULL),
                (SELECT COUNT(*) FROM posts WHERE user_id = ? AND status IN ('pending','sending') AND scheduled_time IS NOT NULL),
                (SELECT COUNT(*) FROM posts WHERE user_id = ? AND status = 'failed'),
                (SELECT COUNT(*) FROM posts WHERE user_id = ? AND status = 'pending'
                    AND scheduled_time IS NOT NULL AND scheduled_time < ?)
        ''', (user_id, user_id, user_id, user_id, user_id, now_iso))
        channels, queued, scheduled, failed, overdue = cursor.fetchone()

        cursor.execute('''
            SELECT p.scheduled_time, COALESCE(c.channel_name, p.channel_id)
            FROM posts p
            LEFT JOIN user_channels c ON c.channel_id = p.channel_id AND c.user_id = p.user_id
            WHERE p.user_id = ? AND p.status = 'pending' AND p.scheduled_time >= ?
            ORDER BY p.scheduled_time ASC LIMIT 1
        ''', (user_id, now_iso))
        row = cursor.fetchone()
        conn.close()

        next_post = None
        if row:
            next_time = parse_dt(row[0])
            if next_time:
                next_post = {'time': next_time, 'channel_name': row[1]}

        return {
            'channels': channels, 'queued': queued, 'scheduled': scheduled,
            'failed': failed, 'overdue': overdue, 'next_post': next_post,
        }

    @staticmethod
    def update_last_reminder_sent(user_id: int):
        """Update the timestamp of the last reminder sent"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE scheduling_config
            SET last_reminder_sent = ?
            WHERE user_id = ?
        ''', (kyiv_now_iso(), user_id))
        
        conn.commit()
        conn.close()
    
    @staticmethod
    def get_all_overdue_posts() -> List[dict]:
        """Get all posts that should have been posted but are still pending (system-wide)"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        # Get posts that are overdue by more than 5 minutes (Kyiv-aware, matching stored format)
        cutoff_time = (datetime.now(get_kyiv_timezone()) - timedelta(minutes=5)).isoformat()
        
        cursor.execute('''
            SELECT id, user_id, scheduled_time, channel_id, description
            FROM posts
            WHERE status = 'pending' 
            AND scheduled_time IS NOT NULL
            AND scheduled_time < ?
            AND is_recurring = 0
        ''', (cutoff_time,))
        
        posts = []
        for row in cursor.fetchall():
            posts.append({
                'id': row[0],
                'user_id': row[1],
                'scheduled_time': datetime.fromisoformat(row[2]),
                'channel_id': row[3],
                'description': row[4]
            })
        
        conn.close()
        return posts
    
    @staticmethod
    def get_users_for_reminders() -> List[Tuple[int, int]]:
        """Get all users who have reminder enabled and check their post counts"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        # Get users with reminders enabled
        cursor.execute('''
            SELECT sc.user_id, sc.reminder_threshold, sc.last_reminder_sent,
                   COUNT(p.id) as post_count
            FROM scheduling_config sc
            LEFT JOIN posts p ON sc.user_id = p.user_id 
                AND p.status = 'pending' 
                AND p.scheduled_time IS NULL
            WHERE sc.reminder_enabled = 1
            GROUP BY sc.user_id
        ''')
        
        users_to_remind = []
        now = datetime.now(get_kyiv_timezone())

        for row in cursor.fetchall():
            user_id, threshold, last_sent, post_count = row
            # Only remind if post count is below threshold
            if post_count <= threshold:
                # Check if we haven't sent a reminder recently (within 24 hours)
                if last_sent:
                    last_sent_dt = parse_dt(last_sent)
                    if last_sent_dt and (now - last_sent_dt).total_seconds() < 86400:  # 24 hours
                        continue
                users_to_remind.append((user_id, post_count))
        
        conn.close()
        return users_to_remind
    
    @staticmethod
    def add_user_channel(user_id: int, channel_id: str, channel_name: str, is_default: bool = False) -> bool:
        """Add a new channel for a user"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        try:
            # Upsert: re-adding an existing channel updates its name without
            # resetting is_default/is_active or changing the row id (which
            # INSERT OR REPLACE would do)
            cursor.execute('''
                INSERT INTO user_channels
                (user_id, channel_id, channel_name, is_default, is_active)
                VALUES (?, ?, ?, ?, TRUE)
                ON CONFLICT(user_id, channel_id) DO UPDATE SET
                    channel_name = excluded.channel_name,
                    is_active = TRUE
            ''', (user_id, channel_id, channel_name, is_default))

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
    def user_has_channel(user_id: int, channel_id: str) -> bool:
        """Verify that a user owns/has access to a specific channel"""
        if not channel_id:  # None or empty string
            return False
            
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT COUNT(*) FROM user_channels 
                WHERE user_id = ? AND channel_id = ? AND is_active = TRUE
            ''', (user_id, channel_id))
            
            result = cursor.fetchone()
            conn.close()
            
            has_channel = bool(result and result[0] > 0)
            if not has_channel:
                logger.warning(f"Security check failed: User {user_id} does not have access to channel {channel_id}")
            
            return has_channel
            
        except Exception as e:
            logger.error(f"Error checking channel ownership: {e}")
            conn.close()
            return False
    
    @staticmethod
    def remove_user_channel(user_id: int, channel_id: str) -> bool:
        """Remove a channel for a user"""
        # Security check: verify user owns the channel before removal
        if not Database.user_has_channel(user_id, channel_id):
            logger.error(f"Security violation: User {user_id} attempted to remove channel {channel_id} they don't own")
            return False
            
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
            WHERE p.user_id = ? AND p.status = 'pending' AND p.scheduled_time IS NOT NULL
            ORDER BY p.scheduled_time ASC
        ''', (user_id,))
        
        posts_by_channel = {}
        for row in cursor.fetchall():
            channel_id = row[5]
            
            if channel_id not in posts_by_channel:
                posts_by_channel[channel_id] = []
            
            posts_by_channel[channel_id].append({
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
    def get_user_recurring_posts(user_id: int, channel_id: str = None) -> List[Dict]:
        """Get all active recurring posts for a specific user, optionally filtered by channel"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        if channel_id:
            cursor.execute('''
                SELECT p.id, p.file_path, p.media_type, p.description, p.scheduled_time, 
                       p.channel_id, p.recurring_interval_hours, p.recurring_end_date, 
                       p.recurring_count, p.recurring_posted_count, c.channel_name,
                       p.caption_entities
                FROM posts p
                LEFT JOIN user_channels c ON p.channel_id = c.channel_id AND p.user_id = c.user_id
                WHERE p.user_id = ? AND p.channel_id = ? AND p.is_recurring = TRUE AND p.status = 'pending'
                ORDER BY p.scheduled_time ASC
            ''', (user_id, channel_id))
        else:
            cursor.execute('''
                SELECT p.id, p.file_path, p.media_type, p.description, p.scheduled_time, 
                       p.channel_id, p.recurring_interval_hours, p.recurring_end_date, 
                       p.recurring_count, p.recurring_posted_count, c.channel_name,
                       p.caption_entities
                FROM posts p
                LEFT JOIN user_channels c ON p.channel_id = c.channel_id AND p.user_id = c.user_id
                WHERE p.user_id = ? AND p.is_recurring = TRUE AND p.status = 'pending'
                ORDER BY p.scheduled_time ASC
            ''', (user_id,))
        
        posts = []
        for row in cursor.fetchall():
            posts.append({
                'id': row[0],
                'file_path': row[1],
                'media_type': row[2],
                'description': row[3],
                'scheduled_time': datetime.fromisoformat(row[4]) if row[4] else None,
                'channel_id': row[5],
                'recurring_interval_hours': row[6],
                'recurring_end_date': datetime.fromisoformat(row[7]) if row[7] else None,
                'recurring_count': row[8],
                'recurring_posted_count': row[9] or 0,
                'channel_name': row[10] or row[5],
                'caption_entities': row[11]
            })
        
        conn.close()
        return posts

    @staticmethod
    def update_recurring_post_interval(post_id: int, interval_hours: int, user_id: int = None) -> bool:
        """Update the recurring interval for a post (with user isolation)"""
        try:
            conn = Database.get_connection()
            cursor = conn.cursor()
            
            if user_id:
                cursor.execute('''
                    UPDATE posts 
                    SET recurring_interval_hours = ?
                    WHERE id = ? AND user_id = ?
                ''', (interval_hours, post_id, user_id))
            else:
                cursor.execute('''
                    UPDATE posts 
                    SET recurring_interval_hours = ?
                    WHERE id = ?
                ''', (interval_hours, post_id))
            
            rows_affected = cursor.rowcount
            conn.commit()
            conn.close()
            return rows_affected > 0
        except Exception as e:
            logger.error(f"Error updating recurring interval: {e}")
            return False

    @staticmethod
    def update_recurring_post_end_condition(post_id: int, recurring_count: int = None, recurring_end_date = None, user_id: int = None) -> bool:
        """Update the end condition for a recurring post (with user isolation)"""
        try:
            conn = Database.get_connection()
            cursor = conn.cursor()
            
            end_date_str = recurring_end_date.isoformat() if recurring_end_date else None
            
            if user_id:
                cursor.execute('''
                    UPDATE posts 
                    SET recurring_count = ?, recurring_end_date = ?
                    WHERE id = ? AND user_id = ?
                ''', (recurring_count, end_date_str, post_id, user_id))
            else:
                cursor.execute('''
                    UPDATE posts 
                    SET recurring_count = ?, recurring_end_date = ?
                    WHERE id = ?
                ''', (recurring_count, end_date_str, post_id))
            
            rows_affected = cursor.rowcount
            conn.commit()
            conn.close()
            return rows_affected > 0
        except Exception as e:
            logger.error(f"Error updating recurring end condition: {e}")
            return False

    @staticmethod
    def create_batch(user_id: int, batch_name: str, channel_id: str) -> int:
        """Create a new post batch"""
        
        # SECURITY CHECK: Verify user owns the channel before creating the batch
        if not Database.user_has_channel(user_id, channel_id):
            error_msg = f"Security violation: User {user_id} attempted to create batch for channel {channel_id} they don't own"
            logger.error(f"SECURITY ALERT: {error_msg}")
            raise ValueError("Channel access denied - you don't have permission to create batches for this channel")
        
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
    def get_batch_posts(batch_id: int, user_id: Optional[int] = None) -> List[Dict]:
        """Get all posts in a specific batch.

        Pass user_id whenever batch_id originates from user input/callback data.
        """
        conn = Database.get_connection()
        cursor = conn.cursor()

        query = '''
            SELECT id, user_id, file_path, media_type, description, scheduled_time, mode, channel_id,
                   is_recurring, recurring_interval_hours, recurring_end_date, recurring_count, recurring_posted_count
            FROM posts
            WHERE batch_id = ? AND status = 'pending'
        '''
        params = [batch_id]
        if user_id is not None:
            query += ' AND user_id = ?'
            params.append(user_id)
        query += ' ORDER BY id ASC'
        cursor.execute(query, params)
        
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
        
        # Get batch info and verify user owns the batch
        cursor.execute('SELECT channel_id, user_id FROM post_batches WHERE id = ?', (batch_id,))
        batch_info = cursor.fetchone()
        if not batch_info:
            conn.close()
            raise ValueError(f"Batch {batch_id} not found")
        
        channel_id, batch_owner_id = batch_info
        
        # SECURITY CHECK: Verify user owns the batch
        if batch_owner_id != user_id:
            conn.close()
            error_msg = f"Security violation: User {user_id} attempted to add post to batch {batch_id} owned by user {batch_owner_id}"
            logger.error(f"SECURITY ALERT: {error_msg}")
            raise ValueError("Batch access denied - you don't have permission to add posts to this batch")
        
        # Additional security check: Verify user still owns the channel (in case permissions changed)
        if not Database.user_has_channel(user_id, channel_id):
            conn.close()
            error_msg = f"Security violation: User {user_id} attempted to add post to batch {batch_id} for channel {channel_id} they no longer own"
            logger.error(f"SECURITY ALERT: {error_msg}")
            raise ValueError("Channel access denied - you no longer have permission to post to this channel")
        
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
    def schedule_batch(batch_id: int, scheduled_times: List[datetime], user_id: Optional[int] = None) -> bool:
        """Schedule all posts in a batch.

        Pass user_id whenever batch_id originates from user input/callback data;
        the batch's ownership is then verified before any writes.
        """
        conn = Database.get_connection()
        cursor = conn.cursor()

        if user_id is not None:
            cursor.execute('SELECT 1 FROM post_batches WHERE id = ? AND user_id = ?', (batch_id, user_id))
            if cursor.fetchone() is None:
                conn.close()
                logger.warning(f"User {user_id} attempted to schedule batch {batch_id} they don't own")
                return False

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
                ''', (serialize_dt(scheduled_times[i]), post_id))

        # Mark batch as scheduled
        cursor.execute('''
            UPDATE post_batches
            SET status = 'scheduled'
            WHERE id = ?
        ''', (batch_id,))

        conn.commit()
        conn.close()

        logger.info(f"Scheduled batch {batch_id} with {len(scheduled_times)} times")
        return True

    @staticmethod
    def update_post_schedule(post_id: int, scheduled_time: datetime, user_id: Optional[int] = None):
        """Update the scheduled time for a specific post.

        Pass user_id whenever post_id originates from user input/callback data;
        omit it only for trusted internal calls (scheduler).
        """
        conn = Database.get_connection()
        cursor = conn.cursor()

        try:
            query = 'UPDATE posts SET scheduled_time = ? WHERE id = ?'
            params = [serialize_dt(scheduled_time), post_id]
            if user_id is not None:
                query += ' AND user_id = ?'
                params.append(user_id)
            cursor.execute(query, params)

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
    def update_post_description(post_id: int, description: str, caption_entities: Optional[str] = None,
                                user_id: Optional[int] = None):
        """Update the description and caption entities for a specific post.

        Pass user_id whenever post_id originates from user input/callback data.
        """
        conn = Database.get_connection()
        cursor = conn.cursor()

        try:
            query = 'UPDATE posts SET description = ?, caption_entities = ? WHERE id = ?'
            params = [description, caption_entities, post_id]
            if user_id is not None:
                query += ' AND user_id = ?'
                params.append(user_id)
            cursor.execute(query, params)

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
    def update_post_media(post_id: int, file_path: str, media_type: str, user_id: Optional[int] = None):
        """Update the media file for a specific post.

        Pass user_id whenever post_id originates from user input/callback data.
        """
        conn = Database.get_connection()
        cursor = conn.cursor()

        try:
            query = 'UPDATE posts SET file_path = ?, media_type = ? WHERE id = ?'
            params = [file_path, media_type, post_id]
            if user_id is not None:
                query += ' AND user_id = ?'
                params.append(user_id)
            cursor.execute(query, params)

            conn.commit()
            rows_affected = cursor.rowcount
            conn.close()
            
            if rows_affected > 0:
                logger.info(f"Updated media for post {post_id}")
                return True
            else:
                logger.warning(f"No rows affected when updating media for post {post_id}")
                return False
                
        except Exception as e:
            logger.error(f"Error updating post media for post {post_id}: {e}")
            conn.close()
            return False
    
    @staticmethod
    def delete_post(post_id: int, user_id: int):
        """Soft delete a post by marking its status as deleted"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                UPDATE posts 
                SET status = 'deleted'
                WHERE id = ? AND user_id = ?
            ''', (post_id, user_id))
            
            conn.commit()
            rows_affected = cursor.rowcount
            conn.close()
            
            if rows_affected > 0:
                logger.info(f"Deleted post {post_id} for user {user_id}")
                return True
            else:
                logger.warning(f"No rows affected when deleting post {post_id}")
                return False
                
        except Exception as e:
            logger.error(f"Error deleting post {post_id}: {e}")
            conn.close()
            return False
    
    @staticmethod
    def get_user_mode2_scheduled_posts(user_id: int, channel_id: Optional[str] = None) -> List[Dict]:
        """Get all scheduled Mode 2 posts for a user, optionally filtered by channel"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        if channel_id:
            cursor.execute('''
                SELECT id, user_id, file_path, media_type, description, scheduled_time, mode, 
                       channel_id, caption_entities, status
                FROM posts 
                WHERE user_id = ? AND mode = 2 AND status = 'pending' AND channel_id = ?
                ORDER BY scheduled_time ASC
            ''', (user_id, channel_id))
        else:
            cursor.execute('''
                SELECT id, user_id, file_path, media_type, description, scheduled_time, mode, 
                       channel_id, caption_entities, status
                FROM posts 
                WHERE user_id = ? AND mode = 2 AND status = 'pending'
                ORDER BY scheduled_time ASC
            ''', (user_id,))
        
        posts = []
        for row in cursor.fetchall():
            posts.append({
                'id': row[0],
                'user_id': row[1],
                'file_path': row[2],
                'media_type': row[3] or 'photo',
                'description': row[4],
                'scheduled_time': Database._parse_datetime(row[5]),
                'mode': row[6],
                'channel_id': row[7],
                'caption_entities': row[8],
                'status': row[9]
            })
        
        conn.close()
        return posts

    @staticmethod
    def delete_all_captions(user_id: int) -> int:
        """Delete all captions (set description to NULL) for all posts of a user
        
        Returns:
            Number of posts that had their captions deleted
        """
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        try:
            # Count posts with captions first
            cursor.execute('''
                SELECT COUNT(*) FROM posts 
                WHERE user_id = ? AND description IS NOT NULL AND description != ''
            ''', (user_id,))
            
            posts_with_captions = cursor.fetchone()[0]
            
            if posts_with_captions == 0:
                conn.close()
                return 0
            
            # Delete all captions for this user
            cursor.execute('''
                UPDATE posts 
                SET description = NULL
                WHERE user_id = ? AND description IS NOT NULL AND description != ''
            ''', (user_id,))
            
            conn.commit()
            rows_affected = cursor.rowcount
            conn.close()
            
            logger.info(f"Deleted captions from {rows_affected} posts for user {user_id}")
            return rows_affected
                
        except Exception as e:
            logger.error(f"Error deleting captions for user {user_id}: {e}")
            conn.close()
            return 0

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
    def delete_batch(batch_id: int, user_id: int) -> bool:
        """Delete a batch and all its posts (only if owned by user_id)"""
        from .utils import delete_media_file
        conn = Database.get_connection()
        cursor = conn.cursor()

        # SECURITY: batch_id comes from forgeable callback data - verify ownership first
        cursor.execute('SELECT 1 FROM post_batches WHERE id = ? AND user_id = ?', (batch_id, user_id))
        if cursor.fetchone() is None:
            conn.close()
            logger.warning(f"User {user_id} attempted to delete batch {batch_id} they don't own")
            return False

        # Get file paths of posts in the batch
        cursor.execute('''
            SELECT file_path FROM posts
            WHERE batch_id = ? AND user_id = ? AND status = 'pending'
        ''', (batch_id, user_id))

        file_paths = [row[0] for row in cursor.fetchall()]

        # Delete the physical files
        for file_path in file_paths:
            delete_media_file(file_path)

        # Delete posts in the batch
        cursor.execute('DELETE FROM posts WHERE batch_id = ? AND user_id = ?', (batch_id, user_id))

        # Delete the batch itself
        cursor.execute('DELETE FROM post_batches WHERE id = ? AND user_id = ?', (batch_id, user_id))
        
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
    def get_scheduled_posts_for_channel(user_id: int, channel_id: Optional[str] = None) -> List[Dict]:
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
    def get_latest_scheduled_time(user_id: int, channel_id: Optional[str] = None) -> Optional[datetime]:
        """Get the latest scheduled time for a user's posts, optionally filtered by channel"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        # Build query conditions
        conditions = ["user_id = ?", "status = 'pending'", "scheduled_time IS NOT NULL"]
        params = [user_id]
        
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        
        where_clause = " AND ".join(conditions)
        
        cursor.execute(f'''
            SELECT MAX(scheduled_time) FROM posts 
            WHERE {where_clause}
        ''', params)
        
        result = cursor.fetchone()[0]
        conn.close()
        
        if result:
            return datetime.fromisoformat(result)
        return None

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
                    # SECURITY: re-verify channel ownership at restore time -
                    # the user may have lost the channel since the backup was made
                    backup_channel = post_data.get('channel_id')
                    if backup_channel and not Database.user_has_channel(user_id, backup_channel):
                        skipped_count += 1
                        logger.warning(
                            f"Skipping restored post for user {user_id}: no longer owns channel {backup_channel}"
                        )
                        continue

                    # Check if file still exists
                    file_path = post_data['file_path']
                    file_exists = os.path.exists(file_path)
                    
                    # Try to find file with just filename if full path doesn't exist
                    if not file_exists and '/' in file_path:
                        filename = os.path.basename(file_path)
                        new_path = os.path.join(UPLOADS_DIR, filename)
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
                        serialize_dt(post_data['scheduled_time']),
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



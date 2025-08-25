"""
Utility functions for the bot
"""

import os
import uuid
import logging
from datetime import datetime, timedelta
import pytz
import calendar
from typing import List, Tuple, Dict, Optional
from PIL import Image
from config import UPLOADS_DIR, TIMEZONE, MAX_FILE_SIZE
import aiofiles

logger = logging.getLogger(__name__)

def get_kyiv_timezone():
    """Get Kyiv timezone object"""
    return pytz.timezone(TIMEZONE)

def get_current_kyiv_time():
    """Get current time in Kyiv timezone"""
    return datetime.now(get_kyiv_timezone())

def generate_unique_filename(original_filename: str) -> str:
    """Generate a unique filename for uploaded files"""
    file_extension = os.path.splitext(original_filename)[1]
    unique_id = str(uuid.uuid4())
    return f"{unique_id}{file_extension}"

async def save_media_streaming(telegram_file, filename: str, media_type: str = 'photo') -> str:
    """Stream download and save media directly to disk for heavy files (memory optimized)"""
    file_path = os.path.join(UPLOADS_DIR, filename)
    
    try:
        # Stream download directly to file to avoid loading into memory
        logger.info(f"Starting streaming download for {media_type}: {filename}")
        await telegram_file.download_to_drive(file_path)
        
        # Get file size after download
        file_size = os.path.getsize(file_path)
        file_size_mb = file_size / (1024 * 1024)
        logger.info(f"Successfully streamed {media_type} file: {file_size_mb:.2f} MB -> {file_path}")
        
        return file_path
        
    except Exception as e:
        logger.error(f"Failed to stream download {media_type} file {filename}: {e}")
        # Clean up partial file if it exists
        if os.path.exists(file_path):
            os.remove(file_path)
        raise ValueError(f"Could not download file: {e}")

def save_media(file_data: bytes, filename: str, media_type: str = 'photo') -> str:
    """Save media data to the uploads directory with optimized heavy file handling (fallback method)"""
    # File size limit removed for unlimited uploads
    # if len(file_data) > MAX_FILE_SIZE:
    #     raise ValueError(f"File size exceeds maximum limit of {MAX_FILE_SIZE} bytes")
    
    # Log file size for monitoring (but don't restrict)
    file_size_mb = len(file_data) / (1024 * 1024)
    logger.info(f"Processing {media_type} file: {file_size_mb:.2f} MB")
    
    file_path = os.path.join(UPLOADS_DIR, filename)
    
    # Use buffered writing for large files to prevent memory issues
    try:
        with open(file_path, 'wb') as f:
            # Write in chunks for memory efficiency with large files
            chunk_size = 64 * 1024  # 64KB chunks
            if len(file_data) > chunk_size:
                for i in range(0, len(file_data), chunk_size):
                    f.write(file_data[i:i + chunk_size])
            else:
                f.write(file_data)
    except IOError as e:
        logger.error(f"Failed to save {media_type} file {filename}: {e}")
        raise ValueError(f"Could not save file: {e}")
    
    # Verify the image can be opened only for photos (with memory optimization)
    if media_type == 'photo':
        try:
            # Use lazy loading for large images to prevent memory issues
            with Image.open(file_path) as img:
                # Don't load the entire image into memory for verification
                img.load = lambda: None  # Disable auto-loading
                img.verify()
        except Exception as e:
            logger.warning(f"Image verification failed for {filename}: {e}")
            # Don't delete the file for verification failures - user might still want it
            # os.remove(file_path)
            # raise ValueError(f"Invalid image file: {e}")
            logger.info(f"Saved {media_type} file despite verification warning: {file_path}")
    
    logger.info(f"Saved {media_type}: {file_path}")
    return file_path

def save_photo(file_data: bytes, filename: str) -> str:
    """Save photo data to the uploads directory (backward compatibility)"""
    return save_media(file_data, filename, 'photo')

def delete_media_file(file_path: str) -> bool:
    """Delete a media file from the uploads directory"""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Deleted media file: {file_path}")
            return True
        else:
            logger.warning(f"File not found: {file_path}")
            return False
    except Exception as e:
        logger.error(f"Error deleting file {file_path}: {e}")
        return False

def get_media_type_from_extension(filename: str) -> str:
    """Get media type from file extension"""
    ext = os.path.splitext(filename)[1].lower()
    if ext in ['.jpg', '.jpeg', '.png', '.webp']:
        return 'photo'
    elif ext in ['.mp4', '.avi', '.mov', '.mkv', '.webm']:
        return 'video'
    elif ext in ['.mp3', '.wav', '.ogg', '.m4a', '.aac']:
        return 'audio'
    elif ext in ['.gif']:
        return 'animation'
    else:
        return 'document'

def calculate_schedule_times(start_hour: int, end_hour: int, interval_hours: int, 
                           num_posts: int, start_date: Optional[datetime] = None) -> List[datetime]:
    """Calculate schedule times for posts"""
    if start_date is None:
        start_date = get_current_kyiv_time().replace(hour=0, minute=0, second=0, microsecond=0)
        start_date += timedelta(days=1)  # Start from tomorrow
    
    schedule_times = []
    current_time = start_date.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    
    posts_scheduled = 0
    
    while posts_scheduled < num_posts:
        # Check if current time is within the daily window
        if start_hour <= current_time.hour < end_hour:
            schedule_times.append(current_time)
            posts_scheduled += 1
            current_time += timedelta(hours=interval_hours)
        else:
            # Move to the next day at start hour
            current_time = current_time.replace(hour=start_hour, minute=0, second=0, microsecond=0)
            current_time += timedelta(days=1)
    
    return schedule_times

def format_schedule_summary(schedule_times: List[datetime]) -> str:
    """Format schedule times for display"""
    if not schedule_times:
        return "No posts scheduled."
    
    summary = f"📅 Schedule Summary ({len(schedule_times)} posts):\n\n"
    
    current_date = None
    for i, time in enumerate(schedule_times):
        if current_date != time.date():
            current_date = time.date()
            summary += f"\n📅 {time.strftime('%B %d, %Y')}:\n"
        
        summary += f"  {i+1}. {time.strftime('%I:%M %p')}\n"
    
    return summary

def parse_date_input(text: str) -> Tuple[bool, Optional[datetime], int, str]:
    """Parse custom date input string"""
    try:
        parts = text.strip().split()
        if len(parts) != 3:
            return False, None, 0, "Please provide date, time and interval (e.g., '2025-07-25 10:00 2')"
        
        date_str, time_str, interval_str = parts
        
        # Parse date
        try:
            date_parts = date_str.split('-')
            if len(date_parts) != 3:
                return False, None, 0, "Date must be in YYYY-MM-DD format"
            year, month, day = map(int, date_parts)
        except ValueError:
            return False, None, 0, "Invalid date format. Use YYYY-MM-DD"
        
        # Parse time
        try:
            time_parts = time_str.split(':')
            if len(time_parts) != 2:
                return False, None, 0, "Time must be in HH:MM format"
            hour, minute = map(int, time_parts)
            if not (0 <= hour <= 23) or not (0 <= minute <= 59):
                return False, None, 0, "Invalid time. Hour: 0-23, Minute: 0-59"
        except ValueError:
            return False, None, 0, "Invalid time format. Use HH:MM"
        
        # Parse interval
        try:
            interval_hours = int(interval_str)
            if interval_hours <= 0:
                return False, None, 0, "Interval must be a positive number"
        except ValueError:
            return False, None, 0, "Interval must be a number"
        
        # Create datetime in Kyiv timezone
        kyiv_tz = get_kyiv_timezone()
        start_datetime = kyiv_tz.localize(datetime(year, month, day, hour, minute))
        
        # Check if date is in the past
        current_time = get_current_kyiv_time()
        if start_datetime <= current_time:
            return False, None, 0, "Start time must be in the future"
        
        return True, start_datetime, interval_hours, "Valid"
        
    except Exception as e:
        return False, None, 0, f"Error parsing input: {str(e)}"

def calculate_custom_date_schedule(start_datetime: datetime, interval_hours: int, num_posts: int) -> List[datetime]:
    """Calculate schedule times starting from a custom date"""
    schedule_times = []
    current_time = start_datetime
    
    for i in range(num_posts):
        schedule_times.append(current_time)
        current_time += timedelta(hours=interval_hours)
    
    return schedule_times

def calculate_evenly_distributed_schedule(start_hour: int, end_hour: int, num_posts: int, start_date: Optional[datetime] = None, interval_hours: Optional[int] = None) -> List[datetime]:
    """Calculate evenly distributed schedule times for posts within daily time window"""
    if start_date is None:
        start_date = get_current_kyiv_time().replace(hour=0, minute=0, second=0, microsecond=0)
        start_date += timedelta(days=1)  # Start from tomorrow
    
    schedule_times = []
    
    if num_posts <= 0:
        return schedule_times
    
    # Calculate daily window in hours
    daily_window_hours = end_hour - start_hour
    
    # If interval is specified, use it; otherwise auto-calculate
    if interval_hours and interval_hours > 0:
        # Use fixed interval scheduling
        posts_per_day = max(1, daily_window_hours // interval_hours + 1)
        current_date = start_date
        posts_scheduled = 0
        
        while posts_scheduled < num_posts:
            posts_today = min(num_posts - posts_scheduled, posts_per_day)
            current_hour = start_hour
            
            for i in range(posts_today):
                if current_hour <= end_hour:
                    schedule_time = current_date.replace(hour=current_hour, minute=0, second=0, microsecond=0)
                    schedule_times.append(schedule_time)
                    posts_scheduled += 1
                    current_hour += interval_hours
                else:
                    break
            
            # Move to next day
            current_date += timedelta(days=1)
    else:
        # Auto-distribute evenly
        daily_window_minutes = daily_window_hours * 60
        current_date = start_date
        posts_scheduled = 0
        
        while posts_scheduled < num_posts:
            # Calculate how many posts can fit in this day
            remaining_posts = num_posts - posts_scheduled
            
            if remaining_posts == 1:
                # If only one post left, schedule at start hour
                schedule_time = current_date.replace(hour=start_hour, minute=0, second=0, microsecond=0)
                schedule_times.append(schedule_time)
                posts_scheduled += 1
            else:
                # Calculate posts to schedule today (maximize posts per day)
                posts_today = min(remaining_posts, daily_window_hours + 1)  # +1 because we can post at end_hour too
                
                if posts_today == 1:
                    # Schedule single post at start time
                    schedule_time = current_date.replace(hour=start_hour, minute=0, second=0, microsecond=0)
                    schedule_times.append(schedule_time)
                    posts_scheduled += 1
                else:
                    # Distribute posts evenly across the day
                    interval_minutes = daily_window_minutes / (posts_today - 1) if posts_today > 1 else 0
                    
                    for i in range(posts_today):
                        minutes_from_start = int(i * interval_minutes)
                        total_minutes = start_hour * 60 + minutes_from_start
                        
                        # Convert back to hours and minutes
                        schedule_hour = total_minutes // 60
                        schedule_minute = total_minutes % 60
                        
                        # Ensure we don't exceed end_hour
                        if schedule_hour >= end_hour:
                            schedule_hour = end_hour - 1
                            schedule_minute = 59
                        
                        schedule_time = current_date.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0)
                        schedule_times.append(schedule_time)
                        posts_scheduled += 1
            
            # Move to next day
            current_date += timedelta(days=1)
    
    return schedule_times

def parse_bulk_edit_input(text: str) -> Tuple[bool, int, int, int, str, Optional[datetime], str]:
    """Parse bulk edit input for time redistribution with optional interval and start date"""
    try:
        # Expected formats:
        # "10 20" - 10am to 8pm, auto interval, starting tomorrow  
        # "10 20 2" - 10am to 8pm, 2 hour intervals, starting tomorrow
        # "10 20 2 2025-07-25" - 10am to 8pm, 2 hour intervals, starting specific date
        # "10 20 @channel" - for specific channel starting tomorrow
        # "10 20 2 2025-07-25 @channel" - specific date and channel with interval
        parts = text.strip().split()
        channel_id = ""
        start_date = None
        interval_hours = None  # Auto-calculate if not provided
        
        # Check for channel specification
        if any(part.startswith('@') for part in parts):
            channel_parts = [p for p in parts if p.startswith('@')]
            channel_id = channel_parts[0]
            parts = [p for p in parts if not p.startswith('@')]
        
        if len(parts) < 2 or len(parts) > 4:
            return False, 0, 0, 0, "", None, "Please provide start hour, end hour, and optionally interval and date.\nExamples:\n• 10 20 (auto interval, tomorrow)\n• 10 20 2 (2 hour intervals, tomorrow)\n• 10 20 2 2025-07-25 (2 hour intervals, specific date)"
        
        start_hour = int(parts[0])
        end_hour = int(parts[1])
        
        # Parse optional interval (3rd parameter)
        if len(parts) >= 3:
            try:
                # Try to parse as interval first
                interval_hours = int(parts[2])
                if not (1 <= interval_hours <= 24):
                    return False, 0, 0, 0, "", None, "Interval must be between 1 and 24 hours"
                
                # Parse optional date (4th parameter if interval was provided)
                if len(parts) == 4:
                    date_str = parts[3]
                    try:
                        start_date = datetime.strptime(date_str, '%Y-%m-%d')
                        start_date = get_kyiv_timezone().localize(start_date)
                        
                        # Check if date is not in the past
                        current_kyiv = get_current_kyiv_time().replace(hour=0, minute=0, second=0, microsecond=0)
                        if start_date < current_kyiv:
                            return False, 0, 0, 0, "", None, "Start date cannot be in the past"
                            
                    except ValueError:
                        return False, 0, 0, 0, "", None, "Invalid date format. Use YYYY-MM-DD format.\nExample: 2025-07-25"
                        
            except ValueError:
                # If 3rd parameter isn't a valid integer, try parsing as date
                try:
                    date_str = parts[2]
                    start_date = datetime.strptime(date_str, '%Y-%m-%d')
                    start_date = get_kyiv_timezone().localize(start_date)
                    interval_hours = None  # Auto-calculate
                    
                    # Check if date is not in the past
                    current_kyiv = get_current_kyiv_time().replace(hour=0, minute=0, second=0, microsecond=0)
                    if start_date < current_kyiv:
                        return False, 0, 0, 0, "", None, "Start date cannot be in the past"
                        
                except ValueError:
                    return False, 0, 0, 0, "", None, "Third parameter must be either interval (1-24) or date (YYYY-MM-DD)"
        
        # Validate time range
        if not (0 <= start_hour <= 23):
            return False, 0, 0, 0, "", None, "Start hour must be between 0 and 23"
        
        if not (0 <= end_hour <= 23):
            return False, 0, 0, 0, "", None, "End hour must be between 0 and 23"
        
        if start_hour >= end_hour:
            return False, 0, 0, 0, "", None, "Start hour must be less than end hour"
        
        if end_hour - start_hour < 1:
            return False, 0, 0, 0, "", None, "Time range must be at least 1 hour"
        
        # Validate interval if provided
        if interval_hours and interval_hours > (end_hour - start_hour):
            return False, 0, 0, 0, "", None, f"Interval ({interval_hours}h) cannot be longer than time range ({end_hour - start_hour}h)"
        
        return True, start_hour, end_hour, interval_hours or 0, channel_id or "", start_date, ""
        
    except ValueError:
        return False, 0, 0, 0, "", None, "Invalid format. Use numbers for hours/interval and YYYY-MM-DD for date.\nExample: 10 20 2 2025-07-25"
    except Exception as e:
        return False, 0, 0, 0, "", None, f"Error parsing input: {str(e)}"

def validate_schedule_params(start_hour: int, end_hour: int, interval_hours: int) -> Tuple[bool, str]:
    """Validate scheduling parameters"""
    if not (0 <= start_hour <= 23):
        return False, "Start hour must be between 0 and 23"
    
    if not (0 <= end_hour <= 23):
        return False, "End hour must be between 0 and 23"
    
    if start_hour >= end_hour:
        return False, "Start hour must be less than end hour"
    
    if not (1 <= interval_hours <= 24):
        return False, "Interval must be between 1 and 24 hours"
    
    daily_hours = end_hour - start_hour
    if interval_hours > daily_hours:
        return False, f"Interval ({interval_hours}h) is longer than daily window ({daily_hours}h)"
    
    return True, "Valid parameters"

def parse_schedule_input(text: str) -> Tuple[bool, int, int, int, str]:
    """Parse schedule input text"""
    try:
        # Expected format: "start_hour end_hour interval_hours"
        # Example: "10 20 2" for 10am to 8pm every 2 hours
        parts = text.strip().split()
        
        if len(parts) != 3:
            return False, 0, 0, 0, "Please provide 3 numbers: start_hour end_hour interval_hours\nExample: 10 20 2"
        
        start_hour = int(parts[0])
        end_hour = int(parts[1])
        interval_hours = int(parts[2])
        
        valid, message = validate_schedule_params(start_hour, end_hour, interval_hours)
        
        return valid, start_hour, end_hour, interval_hours, message
        
    except ValueError:
        return False, 0, 0, 0, "Please provide valid numbers for scheduling parameters"

def cleanup_old_files(days_old: int = 7):
    """Clean up old uploaded files"""
    cutoff_time = datetime.now() - timedelta(days=days_old)
    
    for filename in os.listdir(UPLOADS_DIR):
        if filename == '.gitkeep':
            continue
            
        file_path = os.path.join(UPLOADS_DIR, filename)
        
        if os.path.isfile(file_path):
            file_time = datetime.fromtimestamp(os.path.getctime(file_path))
            
            if file_time < cutoff_time:
                try:
                    os.remove(file_path)
                    logger.info(f"Cleaned up old file: {file_path}")
                except Exception as e:
                    logger.error(f"Failed to remove old file {file_path}: {e}")

def get_media_icon(media_type: str) -> str:
    """Get emoji icon for media type"""
    icons = {
        'photo': '📸',
        'video': '🎥',
        'audio': '🎵',
        'animation': '🎬',
        'document': '📄'
    }
    return icons.get(media_type, '📎')

def generate_mini_calendar(year: int, month: int, posts_by_date: Dict[str, List[Dict]]) -> str:
    """Generate a mini-calendar view with scheduled posts indicators"""
    cal = calendar.monthcalendar(year, month)
    month_name = calendar.month_name[month]
    
    # Calendar header
    calendar_str = f"📅 *{month_name} {year}*\n\n"
    calendar_str += "```\n"
    calendar_str += "Mo Tu We Th Fr Sa Su\n"
    
    # Calendar grid
    for week in cal:
        week_str = ""
        for day in week:
            if day == 0:
                week_str += "   "
            else:
                date_key = f"{year:04d}-{month:02d}-{day:02d}"
                if date_key in posts_by_date:
                    count = len(posts_by_date[date_key])
                    if count > 9:
                        week_str += "9+ "
                    else:
                        week_str += f"{count}● "
                else:
                    week_str += f"{day:2d} "
        calendar_str += week_str + "\n"
    
    calendar_str += "```\n"
    calendar_str += "\n*Legend:* Number = posts scheduled that day • ● = indicator\n"
    
    return calendar_str

def format_daily_schedule(date_str: str, posts: List[Dict]) -> str:
    """Format posts for a specific day"""
    if not posts:
        return f"📅 *{date_str}*\n\nNo posts scheduled for this day."
    
    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
    formatted_date = date_obj.strftime('%B %d, %Y')
    
    schedule_str = f"📅 *{formatted_date}*\n\n"
    
    # Group posts by time
    posts_by_time = {}
    for post in posts:
        time_key = post['scheduled_time'].strftime('%H:%M')
        if time_key not in posts_by_time:
            posts_by_time[time_key] = []
        posts_by_time[time_key].append(post)
    
    # Display posts ordered by time
    for time_key in sorted(posts_by_time.keys()):
        time_posts = posts_by_time[time_key]
        schedule_str += f"🕐 *{time_key}*\n"
        
        for post in time_posts:
            icon = get_media_icon(post['media_type'])
            recurring_icon = "🔄 " if post['is_recurring'] else ""
            channel_name = post['channel_name'][:15] + "..." if len(post['channel_name']) > 15 else post['channel_name']
            
            desc_preview = ""
            if post['description']:
                desc_preview = post['description'][:30] + "..." if len(post['description']) > 30 else post['description']
                desc_preview = f" - {desc_preview}"
            
            schedule_str += f"  {icon} {recurring_icon}→ {channel_name}{desc_preview}\n"
        
        schedule_str += "\n"
    
    return schedule_str

def get_calendar_navigation_dates(current_date: datetime) -> Tuple[datetime, datetime]:
    """Get previous and next month dates for navigation"""
    # Previous month
    if current_date.month == 1:
        prev_month = current_date.replace(year=current_date.year - 1, month=12)
    else:
        prev_month = current_date.replace(month=current_date.month - 1)
    
    # Next month
    if current_date.month == 12:
        next_month = current_date.replace(year=current_date.year + 1, month=1)
    else:
        next_month = current_date.replace(month=current_date.month + 1)
    
    return prev_month, next_month

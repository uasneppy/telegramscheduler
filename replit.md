# Telegram Channel Post Scheduler Bot

## Overview

This is a Telegram bot application designed to schedule automated posts to Telegram channels. The bot provides two distinct modes for uploading and scheduling content: bulk photo upload with automatic scheduling, and individual photo upload with custom descriptions. The application is built using Python with the python-telegram-bot library and SQLite for data persistence.

## System Architecture

The application follows a modular architecture with clear separation of concerns:

- **Bot Layer**: Handles Telegram API interactions and user commands
- **Database Layer**: SQLite-based data persistence for posts, user sessions, and scheduling configuration
- **Scheduler Layer**: AsyncIO-based job scheduler for automated posting
- **Utility Layer**: Helper functions for file management, timezone handling, and scheduling calculations

The architecture uses an event-driven approach where user interactions trigger handlers that update the database and schedule future posting jobs.

## Key Components

### Bot Package (`bot/`)

- **handlers.py**: Contains all Telegram command and message handlers
  - Manages user state transitions between different bot modes
  - Handles photo uploads, scheduling configuration, and user interactions
  - Implements inline keyboard interactions for user-friendly scheduling

- **database.py**: SQLite database operations and data models
  - Posts table: Stores uploaded content, descriptions, and scheduling information
  - User sessions table: Tracks current user state and session data
  - Scheduling config table: Stores user-specific scheduling preferences
  - Provides CRUD operations for all data entities

- **scheduler.py**: Asynchronous job scheduling system
  - Uses APScheduler for reliable job execution
  - Handles timezone-aware scheduling (Kyiv timezone)
  - Manages posting jobs to Telegram channels
  - Includes error handling and retry mechanisms

- **utils.py**: Utility functions for common operations
  - File management with unique filename generation
  - Image validation using PIL
  - Timezone handling for Kyiv time
  - Schedule calculation algorithms

### Configuration (`config.py`)

Centralized configuration management including:
- Bot token and channel ID configuration
- File size limits and directory paths
- Default scheduling parameters
- Bot state definitions for state machine management

### Main Entry Point (`main.py`)

Application bootstrap that:
- Initializes the database schema
- Sets up the post scheduler
- Configures Telegram bot handlers
- Manages application lifecycle

## Data Flow

1. **User Interaction**: Users interact with the bot via Telegram commands
2. **State Management**: Bot tracks user state and current operation mode
3. **Content Processing**: Photos are validated, saved to disk, and metadata stored in database
4. **Scheduling**: Posts are scheduled based on user preferences or automatic distribution
5. **Automated Posting**: Scheduler executes jobs at scheduled times, posting to the configured channel

## External Dependencies

- **python-telegram-bot**: Core Telegram Bot API wrapper
- **APScheduler**: Asynchronous job scheduling framework
- **SQLite3**: Built-in Python database for data persistence
- **PIL (Pillow)**: Image processing and validation
- **pytz**: Timezone handling and conversion

The bot requires minimal external services, with only Telegram API as the primary dependency.

## Deployment Strategy

The application is designed for simple deployment:
- Single Python process handles all operations
- SQLite database requires no separate database server
- File uploads stored locally in configurable directory
- Environment variables for sensitive configuration (bot token, channel ID)
- Logging configured for production monitoring

The stateful design with SQLite ensures data persistence across restarts, while the scheduler automatically resumes pending jobs on startup.

## User Preferences

Preferred communication style: Simple, everyday language.

## Changelog

Changelog:
- July 07, 2025. Initial setup
- July 07, 2025. Added multi-channel support system:
  - New /channels command for channel management
  - Database updated to support multiple channels per user
  - Channel selection during scheduling process
  - Default channel configuration
  - Enhanced scheduler to post to specific channels
- July 07, 2025. Implemented complete user isolation:
  - Added /stats command to view individual user statistics
  - Added /reset command for users to clear all their data
  - Enhanced database with user-specific data management functions
  - Improved scheduler to properly isolate user posts
  - All data (posts, channels, sessions, config) completely separated by user ID
- July 07, 2025. Added recurring posts functionality and enhanced UI:
  - Complete recurring posts system with interval, count, and end date options
  - Button-based interface for all major interactions (main menu, scheduling, channels)
  - Enhanced message formatting with improved readability and structure
  - Support for daily, weekly, custom interval recurring schedules
  - Smart recurring post management with automatic rescheduling
  - Comprehensive help system with detailed topic-based explanations
- July 07, 2025. Added queue clearing functionality:
  - New `/clearqueue` command to clear all pending (not scheduled) posts
  - Confirmation system with inline buttons for safety
  - Database function `clear_queued_posts()` returns count of cleared posts
  - Integrated into help system and command documentation
  - Maintains user isolation - only clears current user's queue
- July 08, 2025. Added comprehensive media support and file deletion:
  - Extended support for videos, audio, GIFs, and documents (not just photos)
  - Database updated with media_type column to track different media formats
  - New media handlers for all file types with proper file validation
  - Updated scheduler to post different media types correctly
  - Enhanced clearqueue to also delete physical media files from disk
  - Improved user interface with media type icons and better messaging
  - Backward compatibility maintained for existing photo-only functionality
- July 09, 2025. Comprehensive bug fixes and code improvements:
  - Fixed import issues: Updated handlers to use save_media instead of save_photo
  - Enhanced error handling in media upload with try-catch blocks
  - Optimized scheduler database queries to avoid inefficient operations  
  - Added file existence checks before posting to prevent FileNotFoundError
  - Improved channel handling: removed dependency on default CHANNEL_ID
  - Added proper null safety checks for context.application access
  - Enhanced scheduler error messaging for missing channels and files
  - Fixed recursive post handling logic for better performance
  - All imports and function calls now properly validated
- July 09, 2025. Enhanced channel selection and removed default channel requirement:
  - Removed dependency on default channels - users can now choose channels during scheduling
  - Modified scheduling flow to always prompt for channel selection when multiple channels exist
  - Updated immediate scheduling, custom scheduling, and recurring scheduling to support channel selection
  - Fixed multi-channel posting issue where bot only posted to default channel
  - Enhanced recurring posts to support channel selection with proper callback handling
  - Added new function setup_recurring_posts_with_channel for recurring posts with selected channels
  - Channel selection now integrated into all scheduling modes for consistent user experience
- July 09, 2025. Added scheduled posts preview and fixed message formatting:
  - Added get_scheduled_posts_by_channel() function to show existing scheduled posts per channel
  - Fixed message formatting - replaced ** ** with proper Telegram markdown (*text*)
  - Added parse_mode='Markdown' to all formatted messages for proper bold text display
  - Enhanced scheduling interface to show current scheduled posts count by channel
  - Preview feature now shows "Channel Name (ID): X posts" for each channel with scheduled content
  - All major messages now display with proper formatting instead of raw markdown syntax
  - Completed comprehensive formatting fixes across all bot messages and handlers
- July 12, 2025. Implemented comprehensive multi-channel batch scheduling system:
  - Added new `/multibatch` command for advanced multi-channel post scheduling
  - Created batch management database schema with new tables for batches and batch posts
  - Extended BotStates with batch-specific states (BATCH_MODE1_PHOTOS, BATCH_MODE2_PHOTOS, etc.)
  - Implemented complete batch lifecycle: create → populate → schedule → manage
  - Added batch handlers supporting both Mode 1 (bulk) and Mode 2 (descriptions) workflows
  - Enhanced media upload system to handle batch modes alongside existing modes
  - Integrated batch callback handling into existing inline keyboard system
  - Added batch management UI with create, view, schedule, and delete operations
  - Enabled independent batch scheduling - users can now schedule multiple channels simultaneously
  - Updated help system and command documentation to include batch functionality
  - System now supports unlimited batches across unlimited channels with full isolation
- July 12, 2025. Implemented automatic queue clearing to prevent post carryover:
  - Added automatic queue clearing after successful scheduling in all modes
  - Posts are now automatically cleared when switching between Mode 1, Mode 2, or batches
  - Queue clearing integrated into execute_scheduling, batch scheduling, and recurring posts
  - Enhanced user experience by preventing unwanted post mixing between different channels
  - Maintains proper isolation between scheduling sessions and prevents accidental cross-posting
  - Database function clear_queued_posts() now called at all scheduling completion points
- July 12, 2025. Enhanced statistics display and fixed queue clearing logic:
  - Fixed clear_queued_posts() to only clear unscheduled posts (scheduled_time IS NULL)
  - Enhanced get_user_stats() to distinguish between queued and scheduled posts
  - Updated statistics display to show separate counts for queued vs scheduled posts
  - Statistics now correctly show scheduled posts count after successful scheduling
  - Improved user experience with accurate post status reporting in /stats command
- July 12, 2025. Implemented complete channel separation and mode isolation:
  - **Major architectural change**: Posts are now assigned to channels during upload, not during scheduling
  - Enhanced Mode 1 and Mode 2 handlers to require channel selection before uploading
  - Added channel pre-selection system with prompt_channel_selection_for_mode() function
  - Modified media handlers to store channel_id with each post from the beginning
  - Removed channel overwriting in execute_scheduling() - posts retain their original channel assignments
  - Added enhanced get_pending_posts() with channel and unscheduled filtering options
  - Implemented strict channel isolation - Mode 1 posts for Channel A stay separate from Mode 2 posts for Channel B
  - Added validation to prevent mixed-channel scheduling with detailed error messages
  - Users now select target channel before uploading content, ensuring complete separation
  - **Result**: Every channel and every mode now completely isolated - no more post mixing between channels
- July 12, 2025. Enhanced comprehensive statistics system with detailed per-channel insights:
  - **Major database enhancement**: Added detailed channel statistics with post breakdown by status and mode
  - Enhanced get_user_stats() to provide per-channel detailed analytics including mode distribution
  - Added next scheduled posts preview showing upcoming posts with timestamps and target channels
  - Implemented channel-specific post counts showing queued vs scheduled posts per channel
  - Added advanced metrics including recurring posts count, batches count, and session information
  - Enhanced both /stats command and inline statistics display with comprehensive data visualization
  - Statistics now show: overall totals, per-channel breakdowns, next posts timeline, and advanced metrics
  - **Result**: Users get complete visibility into their multi-channel posting strategy with actionable insights
- July 12, 2025. Fixed critical multi-channel post preservation bug:
  - **Major bug fix**: Fixed issue where scheduling posts for one channel would wipe all pending posts for other channels
  - Modified execute_scheduling() to only clear queued posts for the specific channels being scheduled
  - Fixed recurring posts functions to use channel-specific clearing instead of clearing all user posts
  - Updated batch scheduling functions to preserve posts from other channels and modes
  - Enhanced clear_queued_posts() function utilization with proper channel_id parameter usage
  - **Result**: Users can now safely work with multiple channels simultaneously without losing posts between channels
- July 12, 2025. Fixed critical mode handler post preservation bug:
  - **Critical bug fix**: Fixed issue where starting Mode 1/Mode 2 for a new channel would wipe scheduled posts from previous channels
  - Root cause: mode1_handler and mode2_handler were calling clear_user_posts() which deleted ALL posts for that mode regardless of channel
  - Added new database function clear_unscheduled_posts_for_mode_and_channel() that only clears unscheduled posts
  - Modified both mode handlers to use the new selective clearing function instead of clearing all posts for the mode
  - **Result**: Users can now use Mode 1 for Channel A, schedule it, then use Mode 1 for Channel B without losing Channel A's scheduled posts
- July 12, 2025. Added scheduled posts clearing functionality:
  - **New command**: /clearscheduled to clear all scheduled posts across all channels
  - Added clear_scheduled_posts() database function with channel filtering support
  - Implemented channel-specific scheduled post clearing via interactive interface
  - Added comprehensive callback handlers for scheduled post management
  - Enhanced user control: can clear all scheduled posts or select specific channels
  - Integrated with existing scheduler to cancel active jobs when clearing scheduled posts
  - **Result**: Users now have complete control over both queued and scheduled posts with granular channel-level management
- July 12, 2025. Fixed double channel selection issue - eliminated redundant prompting:
  - **Major UX improvement**: Removed redundant channel selection during scheduling process
  - Modified schedule_handler to work directly with existing channel assignments from upload phase
  - Updated execute_scheduling() to handle posts that already have channel assignments
  - Removed obsolete schedule_select_channel and schedule_to_ callback handlers
  - Enhanced scheduling interface to show posts grouped by their assigned channels
  - **Result**: Users now select channels once during upload, eliminating confusion and double prompts
- July 12, 2025. Implemented comprehensive separate recurring posts feature:
  - **New independent feature**: Added dedicated /recurring command for flexible recurring post scheduling
  - **User-controlled timing**: Users can set custom start times (YYYY-MM-DD HH:MM format)
  - **Flexible frequency options**: Hourly, 6-hour, 12-hour, daily, every 2 days, weekly, or custom intervals (1-168 hours)
  - **Multiple end conditions**: End after X repetitions, end on specific date, or never end (manual stop)
  - **Channel selection**: Full integration with multi-channel system for targeted recurring posts
  - **Interactive setup**: Step-by-step configuration with inline keyboards and text input validation
  - **Separate from regular scheduling**: Completely independent from /schedule command for clear user experience
  - **Enhanced help system**: Updated /help command to include recurring posts documentation
  - **Result**: Users now have complete control over recurring post timing and frequency, separate from regular scheduling
- July 12, 2025. Fixed recurring posts bugs and enhanced main menu UI:
  - **Critical bug fix**: Fixed datetime import issue in start_recurring_posts function causing UnboundLocalError
  - **UI Enhancement**: Added recurring posts button directly to /start main menu for easy access
  - **Improved UX**: Removed recurring options from /mode1 and /mode2, making them absolute scheduling modes
  - **Fixed channel selection**: Channel selection now works properly in recurring posts flow
  - **Added callback handler**: Implemented main_recurring callback in main menu handler
  - **Result**: Recurring posts is now fully functional and easily accessible from main menu
- July 12, 2025. Cleaned up obsolete recurring post buttons from scheduling interface:
  - **Removed obsolete code**: Deleted recurring schedule buttons from /schedule interface in mode1/mode2
  - **Enhanced separation**: Mode1 and Mode2 are now completely absolute scheduling modes
  - **Improved channel isolation**: Enhanced recurring posts to only affect posts from selected channel
  - **Fixed potential data loss**: Recurring posts now properly isolate by channel to prevent wiping other posts
  - **Result**: Clean separation between absolute scheduling (mode1/mode2) and recurring posts (/recurring command)
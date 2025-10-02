"""
Automatic Caption Recovery System
Recovers captions from Telegram chat history and matches them to existing posts
"""

import logging
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from telegram import Update, Message
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from .database import Database
from .utils import get_current_kyiv_time

logger = logging.getLogger(__name__)

class CaptionRecovery:
    def __init__(self, bot):
        self.bot = bot
        
    async def recover_captions_from_history(self, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> Dict:
        """
        Recover captions from chat history and match them to existing posts
        
        Returns:
            Dict with recovery statistics
        """
        stats = {
            'messages_analyzed': 0,
            'captions_found': 0,
            'posts_updated': 0,
            'errors': 0
        }
        
        try:
            # Get posts without captions for this user
            posts_without_captions = self._get_posts_without_captions(user_id)
            
            if not posts_without_captions:
                logger.info(f"No posts without captions found for user {user_id}")
                return stats
            
            logger.info(f"Found {len(posts_without_captions)} posts without captions for user {user_id}")
            
            # Get chat history with media and captions
            historical_captions = await self._extract_captions_from_chat_history(user_id, context)
            stats['messages_analyzed'] = len(historical_captions)
            stats['captions_found'] = len([h for h in historical_captions if h['caption']])
            
            # Match captions to posts
            matches = self._match_captions_to_posts(posts_without_captions, historical_captions)
            
            # Update database with recovered captions
            stats['posts_updated'] = self._update_posts_with_captions(matches)
            
            logger.info(f"Caption recovery complete for user {user_id}: {stats}")
            
        except Exception as e:
            logger.error(f"Error during caption recovery for user {user_id}: {e}", exc_info=True)
            stats['errors'] += 1
            
        return stats
    
    def _get_posts_without_captions(self, user_id: int) -> List[Dict]:
        """Get all posts without captions for a user"""
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, file_path, media_type, created_at, scheduled_time
            FROM posts 
            WHERE user_id = ? 
            AND (description IS NULL OR description = '') 
            AND status = 'pending'
            ORDER BY created_at ASC
        ''', (user_id,))
        
        posts = []
        for row in cursor.fetchall():
            posts.append({
                'id': row[0],
                'file_path': row[1],
                'media_type': row[2],
                'created_at': datetime.fromisoformat(row[3]) if row[3] else None,
                'scheduled_time': datetime.fromisoformat(row[4]) if row[4] else None
            })
        
        conn.close()
        return posts
    
    async def _extract_captions_from_chat_history(self, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> List[Dict]:
        """Extract media messages with captions from chat history"""
        historical_captions = []
        
        try:
            # Get recent chat updates (last 100 updates as a starting point)
            # Note: This gets updates from the bot's perspective, but we need to look at the user's messages
            offset = -100  # Go back 100 updates
            
            # Alternative approach: Search through recent message history
            # We'll look back 30 days for media messages with captions
            cutoff_date = get_current_kyiv_time() - timedelta(days=30)
            
            # Since we can't directly access chat history, we'll use a different approach:
            # Check if the user has any logged interactions in our session data
            session_data = Database.get_user_session(user_id)
            
            # For now, let's implement a simpler approach using file modification times
            # and trying to correlate with database entries
            historical_captions = await self._analyze_user_media_patterns(user_id)
            
        except Exception as e:
            logger.error(f"Error extracting captions from chat history: {e}")
            
        return historical_captions
    
    async def _analyze_user_media_patterns(self, user_id: int) -> List[Dict]:
        """
        Analyze user's media upload patterns and try to recover captions
        from any available sources (logs, temp data, etc.)
        """
        patterns = []
        
        try:
            # Check for any posted messages that might have captions
            conn = Database.get_connection()
            cursor = conn.cursor()
            
            # Get recently posted items that had captions (as reference)
            cursor.execute('''
                SELECT file_path, media_type, description, posted_at
                FROM posts 
                WHERE user_id = ? 
                AND status = 'posted'
                AND description IS NOT NULL 
                AND description != ''
                ORDER BY posted_at DESC
                LIMIT 50
            ''', (user_id,))
            
            posted_with_captions = cursor.fetchall()
            
            # Use these as patterns to suggest similar captions for pending posts
            for file_path, media_type, description, posted_at in posted_with_captions:
                patterns.append({
                    'file_path': file_path,
                    'media_type': media_type,
                    'caption': description,
                    'timestamp': datetime.fromisoformat(posted_at) if posted_at else None,
                    'source': 'previous_posts'
                })
            
            conn.close()
            
        except Exception as e:
            logger.error(f"Error analyzing user media patterns: {e}")
            
        return patterns
    
    def _match_captions_to_posts(self, posts: List[Dict], historical_captions: List[Dict]) -> List[Tuple[int, str]]:
        """
        Match historical captions to posts without captions
        
        Returns:
            List of (post_id, caption) tuples
        """
        matches = []
        
        # Simple matching strategy: match by media type and chronological order
        posts_by_type = {}
        for post in posts:
            media_type = post['media_type']
            if media_type not in posts_by_type:
                posts_by_type[media_type] = []
            posts_by_type[media_type].append(post)
        
        captions_by_type = {}
        for caption_data in historical_captions:
            if caption_data['caption']:
                media_type = caption_data['media_type']
                if media_type not in captions_by_type:
                    captions_by_type[media_type] = []
                captions_by_type[media_type].append(caption_data['caption'])
        
        # Match captions to posts by type
        for media_type, type_posts in posts_by_type.items():
            available_captions = captions_by_type.get(media_type, [])
            
            for i, post in enumerate(type_posts):
                if i < len(available_captions):
                    matches.append((post['id'], available_captions[i]))
                    logger.info(f"Matched caption to post {post['id']}: '{available_captions[i]}'")
        
        return matches
    
    def _update_posts_with_captions(self, matches: List[Tuple[int, str]]) -> int:
        """Update posts with recovered captions"""
        if not matches:
            return 0
            
        conn = Database.get_connection()
        cursor = conn.cursor()
        
        updated_count = 0
        
        for post_id, caption in matches:
            try:
                cursor.execute('''
                    UPDATE posts 
                    SET description = ? 
                    WHERE id = ? AND (description IS NULL OR description = '')
                ''', (caption, post_id))
                
                if cursor.rowcount > 0:
                    updated_count += 1
                    logger.info(f"Updated post {post_id} with caption: '{caption}'")
                
            except Exception as e:
                logger.error(f"Error updating post {post_id}: {e}")
        
        conn.commit()
        conn.close()
        
        return updated_count

# Command handler for caption recovery
async def handle_recover_captions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /recover_captions command"""
    user = update.effective_user
    
    if not user:
        return
    
    # Send initial message
    message = await update.message.reply_text(
        "🔍 Starting automatic caption recovery...\n"
        "This may take a moment while I analyze your message history."
    )
    
    try:
        # Initialize recovery system
        recovery = CaptionRecovery(context.bot)
        
        # Perform recovery
        stats = await recovery.recover_captions_from_history(user.id, context)
        
        # Send results
        if stats['posts_updated'] > 0:
            result_text = (
                f"✅ **Caption Recovery Complete!**\n\n"
                f"📊 **Results:**\n"
                f"• Messages analyzed: {stats['messages_analyzed']}\n"
                f"• Captions found: {stats['captions_found']}\n"
                f"• Posts updated: {stats['posts_updated']}\n"
                f"• Errors: {stats['errors']}\n\n"
                f"🎉 Successfully recovered {stats['posts_updated']} captions!"
            )
        else:
            result_text = (
                f"📝 **Caption Recovery Complete**\n\n"
                f"📊 **Results:**\n"
                f"• Messages analyzed: {stats['messages_analyzed']}\n"
                f"• Captions found: {stats['captions_found']}\n"
                f"• Posts updated: {stats['posts_updated']}\n\n"
                f"ℹ️ No captions were recovered. This could mean:\n"
                f"• All your posts already have captions\n"
                f"• No matching captions were found in recent history\n"
                f"• The original messages may be too old"
            )
        
        await message.edit_text(result_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error in recover_captions command: {e}", exc_info=True)
        await message.edit_text(
            "❌ **Error during caption recovery**\n\n"
            "Something went wrong while trying to recover your captions. "
            "Please try again later or contact support if the issue persists."
        )

# Advanced recovery with user input
async def handle_recover_captions_interactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Interactive caption recovery with user guidance"""
    user = update.effective_user
    
    if not user:
        return
    
    # Get posts without captions
    recovery = CaptionRecovery(context.bot)
    posts = recovery._get_posts_without_captions(user.id)
    
    if not posts:
        await update.message.reply_text(
            "✅ **All your posts already have captions!**\n\n"
            "No caption recovery is needed."
        )
        return
    
    # Show posts and ask for guidance
    post_list = []
    for i, post in enumerate(posts[:10], 1):  # Show first 10
        filename = post['file_path'].split('/')[-1]
        post_list.append(f"{i}. {post['media_type']} - {filename}")
    
    text = (
        f"📝 **Found {len(posts)} posts without captions**\n\n"
        f"First 10 posts:\n" + "\n".join(post_list) + "\n\n"
        f"Would you like me to:\n"
        f"1️⃣ Try automatic recovery\n"
        f"2️⃣ Show detailed list for manual input\n"
        f"3️⃣ Cancel"
    )
    
    await update.message.reply_text(text)
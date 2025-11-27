"""
steamdb_ui.py - Interactive UI for /steam_db command
Provides persistent button panel with Price Graph, Track/Untrack, Compare, etc.
"""

import discord
from discord.ui import View, Button, Modal, TextInput, Select
from discord import ButtonStyle, Embed, File
import asyncio
import io
import logging
from typing import Optional, List, Dict
from datetime import datetime, timedelta
import csv

logger = logging.getLogger(__name__)

# ============================================
# Toggle Tracking Helper with Atomicity
# ============================================

async def toggle_tracking(
    db_pool, 
    discord_id: int, 
    appid: int, 
    guild_id: int,
    redis_client=None
) -> tuple[bool, str]:
    """
    Atomically toggle game tracking status
    
    Returns:
        (is_active: bool, game_name: str)
    """
    # Optional Redis lock for high-concurrency protection
    lock_key = f"lock:track:{appid}:{discord_id}"
    
    if redis_client:
        # Try to acquire lock with 5s TTL
        acquired = await redis_client.set(lock_key, "1", nx=True, ex=5)
        if not acquired:
            raise RuntimeError("Operation in progress, please wait")
    
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                # Get current state with row lock
                row = await conn.fetchrow('''
                    SELECT is_active, game_name 
                    FROM steam_tracked_games 
                    WHERE discord_id=$1 AND appid=$2 AND guild_id=$3 
                    FOR UPDATE
                ''', discord_id, appid, guild_id)
                
                if not row:
                    # Get game name from cache or games table
                    game_name = await conn.fetchval('''
                        SELECT COALESCE(
                            (SELECT game_name FROM steam_game_cache WHERE appid=$1),
                            (SELECT game_name FROM games WHERE appid=$1 LIMIT 1),
                            'Unknown Game'
                        )
                    ''', appid)
                    
                    # Insert new tracking entry
                    await conn.execute('''
                        INSERT INTO steam_tracked_games 
                        (discord_id, appid, game_name, guild_id, notify_threshold, is_active)
                        VALUES ($1, $2, $3, $4, 50, TRUE)
                        ON CONFLICT (discord_id, appid, guild_id) 
                        DO UPDATE SET is_active=TRUE, game_name=$3
                    ''', discord_id, appid, game_name, guild_id)
                    
                    logger.info(f"User {discord_id} started tracking {appid} ({game_name})")
                    return True, game_name
                else:
                    # Toggle existing state
                    new_state = not row['is_active']
                    await conn.execute('''
                        UPDATE steam_tracked_games 
                        SET is_active=$1, last_notified=NULL
                        WHERE discord_id=$2 AND appid=$3 AND guild_id=$4
                    ''', new_state, discord_id, appid, guild_id)
                    
                    logger.info(f"User {discord_id} {'enabled' if new_state else 'disabled'} tracking for {appid}")
                    return new_state, row['game_name']
    finally:
        if redis_client:
            await redis_client.delete(lock_key)


# ============================================
# Price Graph Generator
# ============================================

async def generate_price_graph(
    history_data: List[Dict],
    appid: int,
    game_name: str,
    days: int = 365
) -> io.BytesIO:
    """Generate matplotlib price history graph"""
    try:
        import matplotlib
        matplotlib.use('Agg')  # Non-interactive backend
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from datetime import datetime
        
        if not history_data:
            # Create empty graph
            fig, ax = plt.subplots(figsize=(12, 6))
            ax.text(0.5, 0.5, 'No price history available', 
                   ha='center', va='center', fontsize=14)
            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
        else:
            # Sort by date
            history_data.sort(key=lambda x: x['fetched_at'])
            
            dates = [x['fetched_at'] for x in history_data]
            prices_final = [x['price_final'] / 100 for x in history_data]  # Convert cents to dollars
            prices_initial = [x['price_initial'] / 100 for x in history_data]
            discounts = [x['discount_percent'] for x in history_data]
            
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), 
                                          gridspec_kw={'height_ratios': [3, 1]})
            
            # Price plot
            ax1.plot(dates, prices_initial, label='Original Price', 
                    color='#95a5a6', linewidth=2, alpha=0.7)
            ax1.plot(dates, prices_final, label='Final Price', 
                    color='#3498db', linewidth=2.5)
            
            # Highlight discount periods
            for i in range(len(dates)):
                if discounts[i] > 0:
                    ax1.scatter(dates[i], prices_final[i], 
                              color='#e74c3c', s=50, zorder=5)
            
            ax1.set_ylabel('Price (USD)', fontsize=12, fontweight='bold')
            ax1.set_title(f'{game_name} - Price History ({days} days)', 
                         fontsize=14, fontweight='bold', pad=20)
            ax1.legend(loc='upper right')
            ax1.grid(True, alpha=0.3)
            ax1.xaxis.set_major_formatter(mdates.DateFormatter('%b %d'))
            
            # Discount plot
            ax2.bar(dates, discounts, color='#e74c3c', alpha=0.7, width=1.5)
            ax2.set_ylabel('Discount %', fontsize=11)
            ax2.set_xlabel('Date', fontsize=11)
            ax2.grid(True, alpha=0.3, axis='y')
            ax2.xaxis.set_major_formatter(mdates.DateFormatter('%b %d'))
            
            plt.tight_layout()
        
        # Save to buffer
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
        buf.seek(0)
        plt.close(fig)
        
        return buf
        
    except ImportError:
        logger.error("matplotlib not installed, cannot generate graph")
        raise
    except Exception as e:
        logger.error(f"Error generating price graph: {e}")
        raise


# ============================================
# Settings Modal
# ============================================

class SettingsModal(Modal):
    def __init__(self, appid: int, current_threshold: int = 50):
        super().__init__(title="Track Settings")
        self.appid = appid
        
        self.threshold_input = TextInput(
            label="Discount Notification Threshold (%)",
            placeholder="Enter 1-99 (notify when discount >= this %)",
            default=str(current_threshold),
            min_length=1,
            max_length=2,
            required=True
        )
        self.add_item(self.threshold_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            threshold = int(self.threshold_input.value)
            if not 1 <= threshold <= 99:
                raise ValueError("Must be between 1-99")
            
            # Update in database
            db_pool = interaction.client.db_pool
            async with db_pool.acquire() as conn:
                await conn.execute('''
                    UPDATE steam_tracked_games 
                    SET notify_threshold = $1
                    WHERE discord_id=$2 AND appid=$3 AND guild_id=$4
                ''', threshold, interaction.user.id, self.appid, interaction.guild_id)
            
            await interaction.response.send_message(
                f"✅ Notification threshold updated to **{threshold}%**",
                ephemeral=True
            )
        except ValueError as e:
            await interaction.response.send_message(
                f"❌ Invalid input: {e}",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error updating settings: {e}")
            await interaction.response.send_message(
                "❌ Failed to update settings",
                ephemeral=True
            )


# ============================================
# Main Interactive View
# ============================================

class SteamDBView(View):
    """Persistent interactive UI for /steam_db command"""
    
    def __init__(
        self, 
        appid: int,
        game_name: str,
        db_pool,
        history_manager,
        redis_client=None,
        initial_tracking_state: bool = False
    ):
        super().__init__(timeout=None)  # Persistent
        self.appid = appid
        self.game_name = game_name
        self.db_pool = db_pool
        self.history = history_manager
        self.redis = redis_client
        
        # Initialize track button with correct state
        self._init_track_button(initial_tracking_state)
    
    def _init_track_button(self, is_tracked: bool):
        """Initialize track button with correct state"""
        # Remove existing track button if any
        self.clear_items()
        
        # Add all buttons in order
        self.add_item(Button(
            label="📈 Price Graph",
            style=ButtonStyle.primary,
            custom_id=f"steamdb:graph:{self.appid}",
            row=0
        ))
        
        self.add_item(Button(
            label="🛑 Untrack" if is_tracked else "🔔 Track",
            style=ButtonStyle.danger if is_tracked else ButtonStyle.success,
            custom_id=f"steamdb:track:{self.appid}",
            row=0
        ))
        
        self.add_item(Button(
            label="⚖️ Compare",
            style=ButtonStyle.secondary,
            custom_id=f"steamdb:compare:{self.appid}",
            row=0
        ))
        
        self.add_item(Button(
            label="🔥 Deals",
            style=ButtonStyle.secondary,
            custom_id=f"steamdb:deals:{self.appid}",
            row=1
        ))
        
        self.add_item(Button(
            label="📊 Export CSV",
            style=ButtonStyle.secondary,
            custom_id=f"steamdb:export:{self.appid}",
            row=1
        ))
        
        self.add_item(Button(
            label="⚙️ Settings",
            style=ButtonStyle.secondary,
            custom_id=f"steamdb:settings:{self.appid}",
            row=1
        ))
        
        # Bind callbacks
        self.children[0].callback = self.graph_callback
        self.children[1].callback = self.track_callback
        self.children[2].callback = self.compare_callback
        self.children[3].callback = self.deals_callback
        self.children[4].callback = self.export_callback
        self.children[5].callback = self.settings_callback
    
    async def check_rate_limit(self, interaction: discord.Interaction, action: str) -> bool:
        """Check Redis rate limit (5s cooldown per user per action)"""
        if not self.redis:
            return True  # No rate limiting without Redis
        
        key = f"ratelimit:{action}:{interaction.user.id}"
        exists = await self.redis.exists(key)
        
        if exists:
            ttl = await self.redis.ttl(key)
            await interaction.response.send_message(
                f"⏱️ Please wait {ttl}s before using this button again",
                ephemeral=True
            )
            return False
        
        await self.redis.setex(key, 5, "1")  # 5s cooldown
        return True
    
    # ========== Button Callbacks ==========
    
    async def graph_callback(self, interaction: discord.Interaction):
        """Generate and send price history graph"""
        if not await self.check_rate_limit(interaction, "graph"):
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Fetch price history (last 365 days)
            history_data = await self.history.get_price_history(
                self.appid, 'us', days=365
            )
            
            # Generate graph
            buf = await generate_price_graph(
                history_data, self.appid, self.game_name, days=365
            )
            
            file = File(buf, filename=f"price_history_{self.appid}.png")
            
            embed = Embed(
                title=f"📈 {self.game_name}",
                description=f"Price history for the last 365 days\n**Data points:** {len(history_data)}",
                color=0x3498db,
                timestamp=datetime.utcnow()
            )
            
            await interaction.followup.send(embed=embed, file=file, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error generating graph: {e}")
            await interaction.followup.send(
                "❌ Failed to generate price graph",
                ephemeral=True
            )
    
    async def track_callback(self, interaction: discord.Interaction):
        """Toggle tracking status"""
        if not await self.check_rate_limit(interaction, "track"):
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Toggle in database
            is_active, game_name = await toggle_tracking(
                self.db_pool,
                interaction.user.id,
                self.appid,
                interaction.guild_id,
                self.redis
            )
            
            # Update button appearance
            track_button = self.children[1]
            if is_active:
                track_button.label = "🛑 Untrack"
                track_button.style = ButtonStyle.danger
                message = f"✅ Now tracking **{game_name}**\nYou'll be notified when discounts reach your threshold"
            else:
                track_button.label = "🔔 Track"
                track_button.style = ButtonStyle.success
                message = f"🔕 Stopped tracking **{game_name}**"
            
            # Try to update original message
            try:
                await interaction.message.edit(view=self)
            except discord.Forbidden:
                logger.warning("Cannot edit message - missing permissions")
            except Exception as e:
                logger.error(f"Error editing message: {e}")
            
            await interaction.followup.send(message, ephemeral=True)
            
        except RuntimeError as e:
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
        except Exception as e:
            logger.error(f"Error toggling track: {e}")
            await interaction.followup.send(
                "❌ Failed to update tracking status",
                ephemeral=True
            )
    
    async def compare_callback(self, interaction: discord.Interaction):
        """Open comparison interface"""
        if not await self.check_rate_limit(interaction, "compare"):
            return
        
        # TODO: Implement game comparison modal/select
        await interaction.response.send_message(
            "🔧 **Compare feature coming soon!**\n"
            "This will allow you to compare prices and stats between multiple games.",
            ephemeral=True
        )
    
    async def deals_callback(self, interaction: discord.Interaction):
        """Show current best deals"""
        if not await self.check_rate_limit(interaction, "deals"):
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Get best current discounts from database
            async with self.db_pool.acquire() as conn:
                deals = await conn.fetch('''
                    SELECT DISTINCT ON (h.appid)
                        h.appid,
                        g.game_name,
                        h.discount_percent,
                        h.price_final,
                        h.price_initial,
                        h.currency
                    FROM steam_price_history h
                    LEFT JOIN games g ON h.appid = g.appid
                    WHERE h.discount_percent > 0
                      AND h.fetched_at >= NOW() - INTERVAL '24 hours'
                      AND h.cc = 'us'
                    ORDER BY h.appid, h.fetched_at DESC
                    LIMIT 10
                ''')
            
            if not deals:
                await interaction.followup.send(
                    "📭 No active deals found in the last 24 hours",
                    ephemeral=True
                )
                return
            
            embed = Embed(
                title="🔥 Current Best Deals",
                description="Top discounts from tracked games",
                color=0xe74c3c,
                timestamp=datetime.utcnow()
            )
            
            for deal in sorted(deals, key=lambda x: x['discount_percent'], reverse=True)[:10]:
                name = deal['game_name'] or f"Game {deal['appid']}"
                discount = deal['discount_percent']
                final = deal['price_final'] / 100
                initial = deal['price_initial'] / 100
                
                embed.add_field(
                    name=f"**{name}**",
                    value=f"🔥 **-{discount}%** OFF\n~~${initial:.2f}~~ → **${final:.2f}**\n[View on Steam](https://store.steampowered.com/app/{deal['appid']})",
                    inline=False
                )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error fetching deals: {e}")
            await interaction.followup.send(
                "❌ Failed to fetch deals",
                ephemeral=True
            )
    
    async def export_callback(self, interaction: discord.Interaction):
        """Export price history to CSV"""
        if not await self.check_rate_limit(interaction, "export"):
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Fetch full price history
            history_data = await self.history.get_price_history(
                self.appid, 'us', days=730  # 2 years max
            )
            
            if not history_data:
                await interaction.followup.send(
                    "📭 No price history available for export",
                    ephemeral=True
                )
                return
            
            # Generate CSV
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(['Date', 'Price (USD)', 'Original Price', 'Discount %', 'Currency'])
            
            for entry in history_data:
                writer.writerow([
                    entry['fetched_at'].strftime('%Y-%m-%d %H:%M:%S'),
                    f"${entry['price_final'] / 100:.2f}",
                    f"${entry['price_initial'] / 100:.2f}",
                    f"{entry['discount_percent']}%",
                    entry['currency']
                ])
            
            output.seek(0)
            
            # Create file
            buf = io.BytesIO(output.getvalue().encode('utf-8'))
            file = File(buf, filename=f"{self.game_name.replace(' ', '_')}_price_history.csv")
            
            embed = Embed(
                title=f"📊 Price History Export",
                description=f"**Game:** {self.game_name}\n**Records:** {len(history_data)}",
                color=0x2ecc71
            )
            
            await interaction.followup.send(embed=embed, file=file, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error exporting CSV: {e}")
            await interaction.followup.send(
                "❌ Failed to export data",
                ephemeral=True
            )
    
    async def settings_callback(self, interaction: discord.Interaction):
        """Open settings modal"""
        try:
            # Get current threshold
            async with self.db_pool.acquire() as conn:
                threshold = await conn.fetchval('''
                    SELECT notify_threshold FROM steam_tracked_games
                    WHERE discord_id=$1 AND appid=$2 AND guild_id=$3
                ''', interaction.user.id, self.appid, interaction.guild_id)
            
            modal = SettingsModal(self.appid, threshold or 50)
            await interaction.response.send_modal(modal)
            
        except Exception as e:
            logger.error(f"Error opening settings: {e}")
            await interaction.response.send_message(
                "❌ Failed to open settings",
                ephemeral=True
            )


# ============================================
# Helper to Check Initial Tracking State
# ============================================

async def get_tracking_state(
    db_pool,
    user_id: int,
    appid: int,
    guild_id: int
) -> bool:
    """Check if user is tracking this game"""
    try:
        async with db_pool.acquire() as conn:
            result = await conn.fetchval('''
                SELECT is_active FROM steam_tracked_games
                WHERE discord_id=$1 AND appid=$2 AND guild_id=$3
            ''', user_id, appid, guild_id)
            return bool(result)
    except Exception:
        return False

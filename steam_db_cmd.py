"""
steam_db_cmd.py - UPDATED with interactive UI
Now includes persistent button panel under embed
"""

import discord
from discord import Embed
from typing import Optional
from datetime import datetime
import asyncio
import logging

from steam_online import steam_online
from steam_price import steam_price
from steam_history import SteamPriceHistory
from steamdb_ui import SteamDBView, get_tracking_state
from http_session import get_http_manager

logger = logging.getLogger(__name__)


class SteamDBCommand:
    """Класс для обработки команды /steam_db"""
    
    def __init__(self, history_manager: SteamPriceHistory, redis_client=None):
        self.history = history_manager
        self.redis = redis_client
        self.http = get_http_manager()
    
    def _format_date(self, dt: datetime) -> str:
        """Форматирует дату в читаемый вид"""
        if not dt:
            return 'Unknown'
        
        now = datetime.utcnow()
        diff = now - dt.replace(tzinfo=None) if dt.tzinfo else now - dt
        
        if diff.days < 1:
            return 'Today'
        elif diff.days == 1:
            return 'Yesterday'
        elif diff.days < 7:
            return f'{diff.days} days ago'
        elif diff.days < 30:
            weeks = diff.days // 7
            return f'{weeks} week{"s" if weeks > 1 else ""} ago'
        elif diff.days < 365:
            months = diff.days // 30
            return f'{months} month{"s" if months > 1 else ""} ago'
        else:
            return dt.strftime('%B %d, %Y')
    
    def _truncate(self, text: str, max_length: int) -> str:
        """Truncate text to max_length"""
        if len(text) <= max_length:
            return text
        return text[:max_length-3] + '...'
    
    def _create_player_stats_field(self, online_data: dict) -> tuple:
        """Создает поле со статистикой игроков"""
        if 'error' in online_data:
            return ('👥 Player Statistics', '❌ Data unavailable', False)
        
        current = online_data.get('current', 0)
        peak_24h = online_data.get('peak_24h', 0)
        all_time = online_data.get('all_time_peak', 0)
        all_time_date = online_data.get('all_time_peak_date', 'Unknown')
        
        value = f"**Current:** {steam_online.format_number(current)}\n"
        
        if peak_24h > 0:
            value += f"**24h Peak:** {steam_online.format_number(peak_24h)}\n"
        
        if all_time > 0:
            value += f"**All-Time Peak:** {steam_online.format_number(all_time)}"
            if all_time_date != 'Unknown':
                value += f" ({all_time_date})"
        
        return ('👥 Player Statistics', self._truncate(value, 1024), True)
    
    def _create_price_field(self, price_data: dict) -> tuple:
        """Создает поле с информацией о текущей цене"""
        if price_data.get('is_free'):
            return ('💰 Current Price', '**Free to Play** 🎮', True)
        
        if 'error' in price_data:
            return ('💰 Current Price', '❌ Price data unavailable', True)
        
        final = price_data.get('formatted_final', 'N/A')
        initial = price_data.get('formatted_initial', 'N/A')
        discount = price_data.get('discount_percent', 0)
        
        if discount > 0:
            value = f"~~{initial}~~ → **{final}**\n"
            value += f"🔥 **-{discount}% OFF**"
        else:
            value = f"**{final}**"
        
        return ('💰 Current Price (USD)', value, True)
    
    def _create_regional_prices_field(self, regional_data: dict) -> Optional[tuple]:
        """Создает поле с региональными ценами"""
        if not regional_data or len(regional_data) < 2:
            return None
        
        interesting_regions = ['us', 'eu', 'ru', 'tr', 'ar']
        prices = []
        
        for region in interesting_regions:
            if region in regional_data:
                data = regional_data[region]
                if not data.get('is_free') and data.get('formatted_final') != 'N/A':
                    region_name = steam_price.REGIONS.get(region, {}).get('name', region.upper())
                    price = data['formatted_final']
                    
                    discount = data.get('discount_percent', 0)
                    if discount > 0:
                        prices.append(f"**{region_name}:** ~~{data['formatted_initial']}~~ {price} (-{discount}%)")
                    else:
                        prices.append(f"**{region_name}:** {price}")
        
        if prices:
            value = '\n'.join(prices[:5])
            return ('🌍 Regional Prices', self._truncate(value, 1024), False)
        
        return None
    
    def _create_discount_history_field(self, stats: dict) -> tuple:
        """Создает поле с историей скидок"""
        if not stats:
            return ('📊 Discount History', 'No historical data available', False)
        
        min_discount = stats.get('min_discount')
        min_date = stats.get('min_discount_date')
        last_discount = stats.get('last_discount')
        last_date = stats.get('last_discount_date')
        
        lines = []
        
        if min_discount and min_discount > 0:
            date_str = self._format_date(min_date)
            lines.append(f"**Best Ever:** -{min_discount}% ({date_str})")
        else:
            lines.append("**Best Ever:** Never on sale")
        
        if last_discount and last_discount > 0:
            date_str = self._format_date(last_date)
            lines.append(f"**Last Sale:** -{last_discount}% ({date_str})")
        else:
            lines.append("**Last Sale:** Not recently")
        
        total = stats.get('total_snapshots', 0)
        if total > 0:
            first_seen = stats.get('first_seen')
            lines.append(f"**Tracked:** {total} snapshots since {self._format_date(first_seen)}")
        
        value = '\n'.join(lines)
        return ('📊 Discount History', self._truncate(value, 1024), False)
    
    async def _search_game_by_name(self, game_name: str) -> Optional[int]:
        """Поиск appid по названию игры"""
        try:
            async with self.history.db_pool.acquire() as conn:
                row = await conn.fetchrow('''
                    SELECT appid FROM games 
                    WHERE LOWER(game_name) = LOWER($1)
                    LIMIT 1
                ''', game_name)
                
                if row:
                    return row['appid']
        except Exception as e:
            logger.error(f"Error searching in DB: {e}")
        
        # Fallback to Steam API
        return await steam_online.search_game_appid(game_name)
    
    async def execute(self, interaction: discord.Interaction, game_query: str):
        """
        Главная функция выполнения команды /steam_db
        
        Args:
            interaction: Discord interaction
            game_query: Название игры или appid
        """
        await interaction.response.defer()
        
        # Определяем appid
        if game_query.isdigit():
            appid = int(game_query)
        else:
            appid = await self._search_game_by_name(game_query)
            
            if not appid:
                embed = Embed(
                    title="❌ Game Not Found",
                    description=f"Could not find game: **{game_query}**\n\nTry using the Steam App ID instead.",
                    color=0xe74c3c
                )
                return await interaction.followup.send(embed=embed, ephemeral=True)
        
        # Собираем данные параллельно
        try:
            online_task = steam_online.get_online_stats(appid)
            price_us_task = steam_price.get_price_info(appid, 'us')
            regional_task = steam_price.get_regional_prices(appid, ['us', 'eu', 'ru', 'tr', 'ar'])
            stats_task = self.history.get_discount_stats(appid)
            
            online_data, price_us, regional_prices, discount_stats = await asyncio.gather(
                online_task, price_us_task, regional_task, stats_task,
                return_exceptions=True
            )
            
            # Обработка ошибок
            if isinstance(online_data, Exception):
                logger.error(f"Online data error: {online_data}")
                online_data = {'error': str(online_data)}
            if isinstance(price_us, Exception):
                logger.error(f"Price data error: {price_us}")
                price_us = {'error': str(price_us)}
            if isinstance(regional_prices, Exception):
                logger.error(f"Regional prices error: {regional_prices}")
                regional_prices = {}
            if isinstance(discount_stats, Exception):
                logger.error(f"Discount stats error: {discount_stats}")
                discount_stats = {}
            
        except Exception as e:
            logger.error(f"Error fetching game data: {e}")
            embed = Embed(
                title="❌ Error",
                description=f"Failed to fetch game data: {str(e)}",
                color=0xe74c3c
            )
            return await interaction.followup.send(embed=embed, ephemeral=True)
        
        # Сохраняем снимок цены для истории (background task)
        if price_us and not price_us.get('is_free') and not price_us.get('error'):
            asyncio.create_task(self.history.save_price_snapshot(
                appid,
                'us',
                price_us.get('price_final', 0),
                price_us.get('price_initial', 0),
                price_us.get('discount_percent', 0),
                price_us.get('currency', 'USD')
            ))
        
        # Создаем embed
        game_name = price_us.get('name', f'Game {appid}')
        game_url = f"https://store.steampowered.com/app/{appid}"
        
        # Truncate game name if too long
        display_name = self._truncate(game_name, 200)
        
        embed = Embed(
            title=f"🎮 {display_name}",
            url=game_url,
            description=self._truncate(
                f"**App ID:** `{appid}` • [Steam Store]({game_url}) • [SteamDB](https://steamdb.info/app/{appid}/)",
                4096
            ),
            color=0x1b2838,
            timestamp=datetime.utcnow()
        )
        
        # Добавляем thumbnail с иконкой игры
        header_url = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"
        embed.set_thumbnail(url=header_url)
        
        # Добавляем поля
        player_field = self._create_player_stats_field(online_data)
        if player_field:
            embed.add_field(name=player_field[0], value=player_field[1], inline=player_field[2])
        
        price_field = self._create_price_field(price_us)
        if price_field:
            embed.add_field(name=price_field[0], value=price_field[1], inline=price_field[2])
        
        # Если есть скидка, добавляем эмодзи в заголовок
        if price_us.get('discount_percent', 0) > 0:
            embed.title = f"🔥 {display_name}"
        
        regional_field = self._create_regional_prices_field(regional_prices)
        if regional_field:
            embed.add_field(name=regional_field[0], value=regional_field[1], inline=regional_field[2])
        
        history_field = self._create_discount_history_field(discount_stats)
        if history_field:
            embed.add_field(name=history_field[0], value=history_field[1], inline=history_field[2])
        
        # Footer с источниками
        sources = []
        if online_data.get('source'):
            sources.append(online_data['source'])
        sources.append('Steam Store API')
        
        footer_text = f"Data from: {', '.join(sources)} • Requested by {interaction.user.display_name}"
        embed.set_footer(
            text=self._truncate(footer_text, 2048),
            icon_url=interaction.user.display_avatar.url
        )
        
        # Check user's tracking state
        is_tracking = await get_tracking_state(
            self.history.db_pool,
            interaction.user.id,
            appid,
            interaction.guild_id
        )
        
        # Create interactive view
        view = SteamDBView(
            appid=appid,
            game_name=game_name,
            db_pool=self.history.db_pool,
            history_manager=self.history,
            redis_client=self.redis,
            initial_tracking_state=is_tracking
        )
        
        # Send with view
        message = await interaction.followup.send(embed=embed, view=view)
        
        # Store message ID for persistent view reconstruction (optional)
        try:
            async with self.history.db_pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO steam_ui_messages 
                    (message_id, channel_id, guild_id, appid, user_id, expires_at)
                    VALUES ($1, $2, $3, $4, $5, NOW() + INTERVAL '7 days')
                    ON CONFLICT (message_id) DO NOTHING
                ''', message.id, message.channel.id, interaction.guild_id, appid, interaction.user.id)
        except Exception as e:
            logger.error(f"Error storing UI message: {e}")


# Функция-обертка для использования в боте
async def handle_steam_db_command(
    interaction: discord.Interaction, 
    game: str,
    history_manager: SteamPriceHistory,
    redis_client=None
):
    """Wrapper функция для команды"""
    cmd = SteamDBCommand(history_manager, redis_client)
    await cmd.execute(interaction, game)
